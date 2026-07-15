from __future__ import annotations

import argparse
import ctypes
import logging
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pamela

from .auth import Account, EXPIRED_CODES, PamTransaction, select_pam_service
from .helper_protocol import (
    MAX_MESSAGE_SIZE,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_message,
    encode_message,
)

LOG = logging.getLogger(__name__)
DISPLAY_PATTERN = re.compile(r"^:[0-9]+(?:\.[0-9]+)?$")
PR_SET_PDEATHSIG = 1


@dataclass
class ManagedSession:
    tab_id: int
    transaction: PamTransaction | None = None
    process: subprocess.Popen[bytes] | None = None
    runtime_directory: Path | None = None
    owns_runtime_directory: bool = False
    output: deque[str] = field(default_factory=lambda: deque(maxlen=250))
    output_buffer: bytes = b""
    stop_requested: bool = False
    stop_deadline: float = 0.0


class HelperServer:
    def __init__(
        self,
        connection: socket.socket,
        caller: Account,
        pam_service: str,
        session_wrapper: tuple[str, ...],
    ) -> None:
        self.connection = connection
        self.caller = caller
        self.pam_service = pam_service
        self.session_wrapper = session_wrapper
        self.sessions: dict[int, ManagedSession] = {}
        self.request_buffer = b""
        self.running = True

    def run(self) -> int:
        self._send(
            {
                "protocol": PROTOCOL_VERSION,
                "event": "ready",
                "privileged": os.geteuid() == 0,
                "caller": self.caller.to_mapping(),
            }
        )
        try:
            while self.running:
                readers: list[Any] = [self.connection]
                readers.extend(
                    managed.process.stdout
                    for managed in self.sessions.values()
                    if managed.process is not None
                    and managed.process.stdout is not None
                )
                ready, _, _ = select.select(readers, [], [], 0.2)
                for stream in ready:
                    if stream is self.connection:
                        if not self._read_requests():
                            self.running = False
                            break
                    else:
                        managed = self._session_for_stream(stream)
                        if managed is not None:
                            self._read_session_output(managed)
                self._poll_sessions()
        finally:
            self._cleanup()
            self.connection.close()
        return 0

    def _read_requests(self) -> bool:
        chunk = self.connection.recv(MAX_MESSAGE_SIZE + 1)
        if not chunk:
            return False
        self.request_buffer += chunk
        if (
            len(self.request_buffer) > MAX_MESSAGE_SIZE
            and b"\n" not in self.request_buffer
        ):
            raise ProtocolError("Privileged-helper request is too large")
        while b"\n" in self.request_buffer:
            payload, _, self.request_buffer = self.request_buffer.partition(b"\n")
            request = decode_message(payload)
            self._dispatch(request)
        return True

    def _dispatch(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        op = request.get("op")
        if (
            not isinstance(request_id, int)
            or isinstance(request_id, bool)
            or request_id < 1
        ):
            raise ProtocolError("Invalid privileged-helper request id")
        if not isinstance(op, str):
            self._respond(request_id, False, message="Invalid helper operation")
            return
        try:
            if op == "authenticate":
                self._authenticate(request_id, request)
            elif op == "start_session":
                self._start_session(request_id, request)
            elif op == "stop_session":
                self._stop_session(request_id, request)
            elif op == "shutdown":
                self._expect_keys(request, {"protocol", "id", "op"})
                self._respond(request_id, True)
                self.running = False
            else:
                self._respond(request_id, False, message="Unknown helper operation")
        except ProtocolError as exc:
            self._respond(request_id, False, message=str(exc))

    def _authenticate(self, request_id: int, request: dict[str, Any]) -> None:
        self._expect_keys(
            request,
            {"protocol", "id", "op", "tab_id", "username", "password"},
        )
        tab_id = _tab_id(request.get("tab_id"))
        existing = self.sessions.get(tab_id)
        if existing is not None and existing.process is not None:
            raise ProtocolError("A nested session is already running")
        username = _bounded_string(request.get("username"), "username", 256)
        password = _bounded_string(request.get("password"), "password", 4096)
        if existing is not None:
            self._cleanup_session(existing)
        managed = ManagedSession(tab_id)
        self.sessions[tab_id] = managed
        try:
            managed.transaction = PamTransaction.authenticate(
                username,
                password,
                self.pam_service,
            )
        except (KeyError, pamela.PAMError) as exc:
            message = (
                "The account or password has expired"
                if isinstance(exc, pamela.PAMError) and exc.errno in EXPIRED_CODES
                else "Authentication failed"
            )
            self.sessions.pop(tab_id, None)
            self._respond(request_id, False, message=message)
        except Exception:
            LOG.exception("PAM authentication failed unexpectedly")
            self.sessions.pop(tab_id, None)
            self._respond(request_id, False, message="Authentication failed")
        else:
            if managed.transaction is None:
                raise RuntimeError("PAM authentication returned no transaction")
            self._respond(
                request_id,
                True,
                account=managed.transaction.account.to_mapping(),
            )

    def _start_session(self, request_id: int, request: dict[str, Any]) -> None:
        self._expect_keys(
            request,
            {"protocol", "id", "op", "tab_id", "display", "session"},
        )
        tab_id = _tab_id(request.get("tab_id"))
        managed = self.sessions.get(tab_id)
        if managed is None or managed.transaction is None:
            raise ProtocolError("No authenticated PAM transaction")
        if managed.process is not None:
            raise ProtocolError("A nested session is already running")
        display = _bounded_string(request.get("display"), "display", 64)
        if not DISPLAY_PATTERN.fullmatch(display):
            raise ProtocolError("Invalid nested display")
        session = request.get("session")
        if not isinstance(session, dict):
            raise ProtocolError("Invalid X session")
        self._expect_keys(
            session,
            {
                "name",
                "session_id",
                "current_desktop",
                "command",
                "user_fallback",
            },
        )
        name = _bounded_string(session.get("name"), "session name", 512)
        session_id = _bounded_string(session.get("session_id"), "session id", 256)
        current_desktop = _bounded_string(
            session.get("current_desktop"), "desktop name", 512
        )
        account = managed.transaction.account
        user_fallback = session.get("user_fallback")
        if not isinstance(user_fallback, bool):
            raise ProtocolError("Invalid user-session fallback flag")
        command = (
            _resolve_user_session(account)
            if user_fallback
            else _command(session.get("command"))
        )
        process: subprocess.Popen[bytes] | None = None
        try:
            pam_environment = managed.transaction.open(
                display,
                self.caller.username,
                session_id,
                current_desktop,
            )
            (
                managed.runtime_directory,
                managed.owns_runtime_directory,
            ) = runtime_directory(account)
            environment = user_session_environment(
                account,
                display,
                pam_environment,
                managed.runtime_directory,
                session_id,
                current_desktop,
            )
            if not os.path.isdir(account.home):
                raise RuntimeError(f"Home directory does not exist: {account.home}")
            argv = [*self.session_wrapper, *command]
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=account.home,
                env=environment,
                start_new_session=True,
                close_fds=True,
                user=account.uid,
                group=account.gid,
                extra_groups=account.groups,
            )
            if process.stdout is not None:
                os.set_blocking(process.stdout.fileno(), False)
            managed.process = process
            managed.output.clear()
            managed.output_buffer = b""
            managed.stop_requested = False
            managed.stop_deadline = 0.0
        except Exception as exc:
            LOG.exception("Could not start nested session")
            if process is not None:
                _stop_process_blocking(process)
                if process.stdout is not None:
                    process.stdout.close()
            managed.process = None
            self._cleanup_session(managed)
            self.sessions.pop(tab_id, None)
            self._respond(request_id, False, message=f"Could not start {name}: {exc}")
            return
        self._respond(request_id, True)

    def _stop_session(self, request_id: int, request: dict[str, Any]) -> None:
        self._expect_keys(request, {"protocol", "id", "op", "tab_id"})
        tab_id = _tab_id(request.get("tab_id"))
        managed = self.sessions.get(tab_id)
        self._respond(request_id, True)
        if managed is None:
            return
        if managed.process is None:
            had_transaction = managed.transaction is not None
            self._cleanup_session(managed)
            self.sessions.pop(tab_id, None)
            if had_transaction:
                self._send_session_finished(managed, 0, expected=True)
            return
        if not managed.stop_requested:
            managed.stop_requested = True
            managed.stop_deadline = time.monotonic() + 5.0
            _signal_process(managed.process, signal.SIGTERM)

    def _session_for_stream(self, stream: Any) -> ManagedSession | None:
        for managed in self.sessions.values():
            if managed.process is not None and managed.process.stdout is stream:
                return managed
        return None

    def _read_session_output(self, managed: ManagedSession) -> None:
        if managed.process is None or managed.process.stdout is None:
            return
        try:
            chunk = os.read(managed.process.stdout.fileno(), 8192)
        except BlockingIOError:
            return
        if not chunk:
            return
        managed.output_buffer += chunk
        while b"\n" in managed.output_buffer:
            raw, _, managed.output_buffer = managed.output_buffer.partition(b"\n")
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                managed.output.append(line)
                LOG.debug("session %d: %s", managed.tab_id, line)

    def _poll_sessions(self) -> None:
        for managed in tuple(self.sessions.values()):
            self._poll_session(managed)

    def _poll_session(self, managed: ManagedSession) -> None:
        if managed.process is None:
            return
        code = managed.process.poll()
        if code is None:
            if (
                managed.stop_requested
                and time.monotonic() >= managed.stop_deadline
            ):
                _signal_process(managed.process, signal.SIGKILL)
                managed.stop_deadline = float("inf")
            return
        self._read_session_output(managed)
        if managed.output_buffer:
            line = managed.output_buffer.decode("utf-8", errors="replace").rstrip()
            if line:
                managed.output.append(line)
            managed.output_buffer = b""
        expected = managed.stop_requested or code == 0
        if managed.process.stdout is not None:
            managed.process.stdout.close()
        managed.process = None
        self._close_transaction(managed)
        self._remove_runtime_directory(managed)
        self.sessions.pop(managed.tab_id, None)
        self._send_session_finished(managed, code, expected)

    def _send_session_finished(
        self, managed: ManagedSession, code: int, expected: bool
    ) -> None:
        message = (
            ""
            if expected
            else f"The nested session exited unexpectedly with status {code}"
        )
        self._send(
            {
                "protocol": PROTOCOL_VERSION,
                "event": "session_finished",
                "tab_id": managed.tab_id,
                "status": code,
                "message": message,
                "diagnostics": "\n".join(managed.output),
            }
        )
        managed.output.clear()

    def _respond(
        self,
        request_id: int,
        ok: bool,
        *,
        message: str = "",
        account: dict[str, object] | None = None,
    ) -> None:
        response: dict[str, object] = {
            "protocol": PROTOCOL_VERSION,
            "id": request_id,
            "ok": ok,
        }
        if message:
            response["message"] = message
        if account is not None:
            response["account"] = account
        self._send(response)

    def _send(self, message: dict[str, object]) -> None:
        self.connection.sendall(encode_message(message))

    @staticmethod
    def _expect_keys(request: dict[str, Any], expected: set[str]) -> None:
        if set(request) != expected:
            raise ProtocolError("Invalid fields for helper operation")

    def _close_transaction(self, managed: ManagedSession) -> None:
        transaction, managed.transaction = managed.transaction, None
        if transaction is None:
            return
        try:
            transaction.close()
        except Exception:
            LOG.exception("PAM cleanup failed")

    def _remove_runtime_directory(self, managed: ManagedSession) -> None:
        path, owned = (
            managed.runtime_directory,
            managed.owns_runtime_directory,
        )
        managed.runtime_directory = None
        managed.owns_runtime_directory = False
        if path is not None and owned:
            try:
                shutil.rmtree(path)
            except OSError:
                LOG.exception("Could not remove temporary runtime directory")

    def _cleanup_session(self, managed: ManagedSession) -> None:
        if managed.process is not None:
            _stop_process_blocking(managed.process)
        if managed.process is not None and managed.process.stdout is not None:
            managed.process.stdout.close()
        managed.process = None
        self._close_transaction(managed)
        self._remove_runtime_directory(managed)

    def _cleanup(self) -> None:
        for managed in tuple(self.sessions.values()):
            self._cleanup_session(managed)
        self.sessions.clear()


def user_session_environment(
    account: Account,
    display: str,
    pam_environment: dict[str, str],
    runtime: Path,
    session_id: str,
    current_desktop: str,
) -> dict[str, str]:
    environment = _base_environment()
    for key, value in pam_environment.items():
        if key and isinstance(value, str):
            environment[key] = value
    environment.update(
        {
            "HOME": account.home,
            "USER": account.username,
            "LOGNAME": account.username,
            "SHELL": account.shell,
            "DISPLAY": display,
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_SESSION_TYPE": "x11",
            "XDG_SESSION_CLASS": "user",
            "XDG_SESSION_DESKTOP": session_id,
            "XDG_CURRENT_DESKTOP": current_desktop,
            "DESKTOP_SESSION": session_id,
        }
    )
    for key in (
        "XAUTHORITY",
        "WAYLAND_DISPLAY",
        "SESSION_MANAGER",
        "SUDO_UID",
        "SUDO_GID",
        "SUDO_USER",
        "SUDO_COMMAND",
    ):
        environment.pop(key, None)
    if "DBUS_SESSION_BUS_ADDRESS" not in environment:
        bus = runtime / "bus"
        if bus.exists():
            environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return environment


def runtime_directory(account: Account) -> tuple[Path, bool]:
    standard = Path(f"/run/user/{account.uid}")
    if _owned_directory(standard, account.uid):
        return standard, False
    created = Path(tempfile.mkdtemp(prefix=f"xnestdm-{account.uid}-", dir="/tmp"))
    try:
        os.chown(created, account.uid, account.gid)
        os.chmod(created, 0o700)
    except OSError:
        shutil.rmtree(created, ignore_errors=True)
        raise
    return created, True


def _base_environment() -> dict[str, str]:
    allowed = {"PATH", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "TZ", "TERM"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key == "LANG" or key.startswith("LC_")
    }
    environment.setdefault("PATH", "/run/current-system/sw/bin:/usr/bin:/bin")
    return environment


def _owned_directory(path: Path, uid: int) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    return path.is_dir() and stat.st_uid == uid


def _bounded_string(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\0" in value:
        raise ProtocolError(f"Invalid {label}")
    return value


def _tab_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProtocolError("Invalid session tab id")
    return value


def _command(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 128:
        raise ProtocolError("Invalid session command")
    command = tuple(_bounded_string(item, "session command", 4096) for item in value)
    return command


def _resolve_user_session(account: Account) -> tuple[str, ...]:
    home = Path(account.home)
    xsession = home / ".xsession"
    if xsession.is_file() and os.access(xsession, os.X_OK):
        return (str(xsession),)
    xinitrc = home / ".xinitrc"
    if xinitrc.is_file():
        return (account.shell, str(xinitrc))
    for path in (Path("/etc/X11/Xsession"), Path("/etc/X11/xinit/xinitrc")):
        if not path.is_file():
            continue
        if os.access(path, os.X_OK):
            return (str(path),)
        return (account.shell, str(path))
    raise RuntimeError(
        "No host X sessions were found and the selected user has no ~/.xsession "
        "or ~/.xinitrc"
    )


def _signal_process(process: subprocess.Popen[bytes], signum: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _stop_process_blocking(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    _signal_process(process, signal.SIGTERM)
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        _signal_process(process, signal.SIGKILL)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def _configure_parent_death_signal() -> None:
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != parent:
        raise RuntimeError("Parent exited while privileged helper was starting")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal xnestdm privileged helper")
    parser.add_argument("--socket-fd", required=True, type=int)
    parser.add_argument("--pam-service")
    parser.add_argument("--caller-uid", type=int)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s xnestdm-helper: %(message)s",
    )
    real_uid = os.getuid()
    effective_uid = os.geteuid()
    if effective_uid != 0:
        print("xnestdm-helper must run with effective UID 0", file=os.sys.stderr)
        return 2
    setuid_invocation = real_uid != effective_uid
    if setuid_invocation and (args.pam_service or args.caller_uid is not None):
        print(
            "Privileged helper overrides are not allowed through setuid",
            file=os.sys.stderr,
        )
        return 2
    if args.caller_uid is not None and (real_uid != 0 or effective_uid != 0):
        print("--caller-uid requires a root bootstrap", file=os.sys.stderr)
        return 2

    caller_uid = real_uid if setuid_invocation else args.caller_uid
    if caller_uid is None:
        caller_uid = 0
    try:
        caller = Account.from_uid(caller_uid)
        wrapper = tuple(
            shlex.split(os.environ.get("XNESTDM_XSESSION_WRAPPER", ""), posix=True)
        )
        pam_service = (
            os.environ.get("XNESTDM_PAM_SERVICE", "xnestdm")
            if setuid_invocation
            else select_pam_service(args.pam_service)
        )
        _configure_parent_death_signal()
        connection = socket.socket(fileno=args.socket_fd)
        server = HelperServer(connection, caller, pam_service, wrapper)
        signal.signal(signal.SIGTERM, lambda *_: setattr(server, "running", False))
        signal.signal(signal.SIGINT, lambda *_: setattr(server, "running", False))
        return server.run()
    except Exception:
        LOG.exception("Privileged helper failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
