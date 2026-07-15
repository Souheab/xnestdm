from __future__ import annotations

import json
import logging
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Qt, Signal
from PySide6.QtWidgets import QWidget

from .auth import Account

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Commands:
    xephyr: str
    dbus_run_session: str
    dbus_session_config: str
    session_entry: tuple[str, ...]
    shell: str
    xinitrc: str
    logout: str

    @classmethod
    def from_environment(cls) -> "Commands":
        dbus_run_session = _command("USERDESK_DBUS_RUN_SESSION", "dbus-run-session")
        default_dbus_config = (
            Path(dbus_run_session).parent.parent / "share/dbus-1/session.conf"
        )
        configured_session_entry = os.environ.get("USERDESK_SESSION_ENTRY")
        session_entry = (
            (configured_session_entry,)
            if configured_session_entry
            else (sys.executable, str(Path(__file__).with_name("session_entry.py")))
        )
        xfce_session = shutil.which("xfce4-session")
        default_xinitrc = (
            Path(xfce_session).parent.parent / "etc/xdg/xfce4/xinitrc"
            if xfce_session
            else Path("/etc/xdg/xfce4/xinitrc")
        )
        return cls(
            xephyr=_command("USERDESK_XEPHYR", "Xephyr"),
            dbus_run_session=dbus_run_session,
            dbus_session_config=os.environ.get(
                "USERDESK_DBUS_SESSION_CONFIG", str(default_dbus_config)
            ),
            session_entry=session_entry,
            shell=_command("USERDESK_SHELL", "sh"),
            xinitrc=os.environ.get("USERDESK_XFCE_XINITRC", str(default_xinitrc)),
            logout=_command("USERDESK_XFCE_LOGOUT", "xfce4-session-logout"),
        )


def _command(variable: str, fallback: str) -> str:
    configured = os.environ.get(variable)
    if configured:
        return configured
    return shutil.which(fallback) or fallback


class OutputBuffer:
    def __init__(self, limit: int = 250) -> None:
        self._lines: deque[str] = deque(maxlen=limit)
        self._lock = threading.Lock()

    def drain(self, stream: IO[bytes] | None, label: str) -> None:
        if stream is None:
            return

        def read() -> None:
            try:
                for raw_line in iter(stream.readline, b""):
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue
                    with self._lock:
                        self._lines.append(f"{label}: {line}")
                    LOG.debug("%s: %s", label, line)
            finally:
                stream.close()

        threading.Thread(target=read, name=f"userdesk-{label}", daemon=True).start()

    def tail(self, count: int = 12) -> str:
        with self._lock:
            return "\n".join(tuple(self._lines)[-count:])


class SessionController(QObject):
    xephyr_ready = Signal(str)
    session_ready = Signal()
    finished = Signal(str)

    def __init__(self, commands: Commands | None = None) -> None:
        super().__init__()
        self.commands = commands or Commands.from_environment()
        self.output = OutputBuffer()
        self.account: Account | None = None
        self.display = ""
        self.session_environment: dict[str, str] = {}
        self.xephyr: subprocess.Popen[bytes] | None = None
        self.session: subprocess.Popen[bytes] | None = None
        self.logout_process: subprocess.Popen[bytes] | None = None
        self.runtime_directory: Path | None = None
        self._owns_runtime_directory = False
        self._state = "idle"
        self._finish_message = ""
        self._deadline = 0.0
        self._display_fd: int | None = None
        self._display_notifier: QSocketNotifier | None = None
        self._display_buffer = b""
        self._bus_fd: int | None = None
        self._bus_notifier: QSocketNotifier | None = None
        self._bus_buffer = b""
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._poll)

    @property
    def active(self) -> bool:
        return self._state != "idle"

    def start_xephyr(self, host: QWidget, account: Account) -> None:
        if self.active:
            raise RuntimeError("A nested session is already active")
        self.output = OutputBuffer()
        self.account = account
        self._state = "starting-xephyr"
        self._finish_message = ""
        host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        window_id = int(host.winId())
        width = max(host.width(), 640)
        height = max(host.height(), 480)

        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        self._display_fd = read_fd
        self._display_notifier = QSocketNotifier(
            read_fd, QSocketNotifier.Type.Read, self
        )
        self._display_notifier.activated.connect(self._read_display)

        argv = [
            self.commands.xephyr,
            "-parent",
            str(window_id),
            "-displayfd",
            str(write_fd),
            "-screen",
            f"{width}x{height}",
            "-resizeable",
            "-noreset",
            "-no-host-grab",
            "-nolisten",
            "tcp",
            "-ac",
            "-name",
            "userdesk",
            "-title",
            "Userdesk",
        ]
        identity = invoking_account()
        try:
            self.xephyr = subprocess.Popen(
                argv,
                pass_fds=(write_fd,),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=outer_x_environment(identity),
                start_new_session=True,
                **credential_arguments(identity),
            )
        except Exception as exc:
            os.close(write_fd)
            self._begin_terminate(f"Could not start Xephyr: {exc}")
            return
        finally:
            if self.xephyr is not None:
                os.close(write_fd)
        self.output.drain(self.xephyr.stderr, "Xephyr")
        self._timer.start()

    def start_user_session(
        self, account: Account, pam_environment: dict[str, str]
    ) -> None:
        if self._state != "xephyr-ready" or not self.display:
            raise RuntimeError("Xephyr is not ready")
        try:
            self.runtime_directory, self._owns_runtime_directory = runtime_directory(
                account
            )
            environment = user_session_environment(
                account,
                self.display,
                pam_environment,
                self.runtime_directory,
            )
            if not os.path.isdir(account.home):
                raise RuntimeError(f"Home directory does not exist: {account.home}")

            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            self._bus_fd = read_fd
            self._bus_notifier = QSocketNotifier(
                read_fd, QSocketNotifier.Type.Read, self
            )
            self._bus_notifier.activated.connect(self._read_bus_environment)
            argv = [
                self.commands.dbus_run_session,
                "--config-file",
                self.commands.dbus_session_config,
                "--",
                *self.commands.session_entry,
                "--notify-fd",
                str(write_fd),
                "--shell",
                self.commands.shell,
                "--xinitrc",
                self.commands.xinitrc,
            ]
            self.session = subprocess.Popen(
                argv,
                pass_fds=(write_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=account.home,
                env=environment,
                start_new_session=True,
                **credential_arguments(account),
            )
            os.close(write_fd)
            self.output.drain(self.session.stdout, "XFCE")
            self.session_environment = environment
            self._state = "starting-session"
        except Exception as exc:
            try:
                os.close(write_fd)
            except (NameError, OSError):
                pass
            self._begin_terminate(f"Could not start XFCE: {exc}")

    def request_logout(self) -> None:
        if self._state != "running" or self.account is None:
            self._begin_terminate("")
            return
        environment = dict(self.session_environment)
        if not environment.get("DBUS_SESSION_BUS_ADDRESS"):
            self._begin_terminate("")
            return
        argv = [self.commands.logout, "--logout", f"--display={self.display}"]
        try:
            self.logout_process = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=self.account.home,
                env=environment,
                start_new_session=True,
                **credential_arguments(self.account),
            )
        except Exception:
            LOG.exception("Could not request XFCE logout")
            self._begin_terminate("")
            return
        self.output.drain(self.logout_process.stderr, "logout")
        self._state = "logging-out"
        self._deadline = time.monotonic() + 5.0

    def stop(self, message: str = "") -> None:
        if not self.active:
            return
        self._begin_terminate(message)

    def shutdown_blocking(self) -> None:
        if not self.active:
            return
        self._terminate_process(self.logout_process)
        self._terminate_process(self.session)
        self._terminate_process(self.xephyr)
        deadline = time.monotonic() + 1.5
        for process in (self.logout_process, self.session, self.xephyr):
            if process is None:
                continue
            timeout = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_process(process)
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        self._finalize(emit=False)

    def _read_display(self) -> None:
        if self._display_fd is None:
            return
        try:
            chunk = os.read(self._display_fd, 128)
        except BlockingIOError:
            return
        if not chunk:
            if not self.display:
                self._begin_terminate("Xephyr closed before reporting a display")
            return
        self._display_buffer += chunk
        if b"\n" not in self._display_buffer:
            return
        line, _, _ = self._display_buffer.partition(b"\n")
        try:
            number = int(line.strip())
        except ValueError:
            self._begin_terminate("Xephyr returned an invalid display number")
            return
        self.display = f":{number}"
        self._close_display_notifier()
        self._state = "xephyr-ready"
        self.xephyr_ready.emit(self.display)

    def _read_bus_environment(self) -> None:
        if self._bus_fd is None:
            return
        try:
            chunk = os.read(self._bus_fd, 4096)
        except BlockingIOError:
            return
        if not chunk:
            if self._state == "starting-session":
                self._begin_terminate("XFCE did not publish its D-Bus environment")
            return
        self._bus_buffer += chunk
        if b"\n" not in self._bus_buffer:
            return
        line, _, _ = self._bus_buffer.partition(b"\n")
        try:
            values = json.loads(line.decode("utf-8"))
            address = str(values["DBUS_SESSION_BUS_ADDRESS"])
            if not address:
                raise ValueError("empty D-Bus address")
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._begin_terminate(f"Invalid D-Bus session information: {exc}")
            return
        self.session_environment["DBUS_SESSION_BUS_ADDRESS"] = address
        self._close_bus_notifier()
        self._state = "running"
        self.session_ready.emit()

    def _poll(self) -> None:
        xephyr_code = self.xephyr.poll() if self.xephyr is not None else None
        session_code = self.session.poll() if self.session is not None else None

        if (
            self._state
            in {
                "starting-xephyr",
                "xephyr-ready",
                "starting-session",
                "running",
                "logging-out",
            }
            and xephyr_code is not None
        ):
            self._begin_terminate(
                f"Xephyr exited unexpectedly with status {xephyr_code}"
            )
        elif (
            self._state in {"starting-session", "running", "logging-out"}
            and session_code is not None
        ):
            message = ""
            if session_code != 0 and self._state != "logging-out":
                message = f"XFCE exited unexpectedly with status {session_code}"
            self._begin_terminate(message)
        elif self._state == "logging-out" and time.monotonic() >= self._deadline:
            self._begin_terminate("")

        if self._state == "stopping":
            if time.monotonic() >= self._deadline:
                self._kill_process(self.logout_process)
                self._kill_process(self.session)
                self._kill_process(self.xephyr)
            all_stopped = all(
                process is None or process.poll() is not None
                for process in (self.logout_process, self.session, self.xephyr)
            )
            if all_stopped:
                self._finalize(emit=True)

    def _begin_terminate(self, message: str) -> None:
        if self._state == "idle":
            return
        if self._state != "stopping":
            self._finish_message = message
        elif message and not self._finish_message:
            self._finish_message = message
        self._state = "stopping"
        self._deadline = time.monotonic() + 2.0
        self._terminate_process(self.logout_process)
        self._terminate_process(self.session)
        self._terminate_process(self.xephyr)
        self._timer.start()

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    @staticmethod
    def _kill_process(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _finalize(self, emit: bool) -> None:
        message = self._finish_message
        if message:
            tail = self.output.tail()
            if tail:
                LOG.error("%s\n%s", message, tail)
        self._timer.stop()
        self._close_display_notifier()
        self._close_bus_notifier()
        if self._owns_runtime_directory and self.runtime_directory is not None:
            try:
                shutil.rmtree(self.runtime_directory)
            except OSError:
                LOG.exception("Could not remove temporary runtime directory")
        self.account = None
        self.display = ""
        self.session_environment = {}
        self.xephyr = None
        self.session = None
        self.logout_process = None
        self.runtime_directory = None
        self._owns_runtime_directory = False
        self._finish_message = ""
        self._state = "idle"
        if emit:
            self.finished.emit(message)

    def _close_display_notifier(self) -> None:
        if self._display_notifier is not None:
            self._display_notifier.setEnabled(False)
            self._display_notifier.deleteLater()
            self._display_notifier = None
        if self._display_fd is not None:
            try:
                os.close(self._display_fd)
            except OSError:
                pass
            self._display_fd = None

    def _close_bus_notifier(self) -> None:
        if self._bus_notifier is not None:
            self._bus_notifier.setEnabled(False)
            self._bus_notifier.deleteLater()
            self._bus_notifier = None
        if self._bus_fd is not None:
            try:
                os.close(self._bus_fd)
            except OSError:
                pass
            self._bus_fd = None


def invoking_account() -> Account:
    uid_text = os.environ.get("SUDO_UID") if os.geteuid() == 0 else None
    uid = int(uid_text) if uid_text and uid_text.isdigit() else os.getuid()
    record = pwd.getpwuid(uid)
    return Account(
        username=record.pw_name,
        uid=record.pw_uid,
        gid=record.pw_gid,
        home=record.pw_dir,
        shell=record.pw_shell or "/bin/sh",
        groups=tuple(sorted(set(os.getgrouplist(record.pw_name, record.pw_gid)))),
    )


def credential_arguments(account: Account) -> dict[str, object]:
    """Return Popen credential options without calling setgroups unprivileged."""
    if os.geteuid() != 0:
        if account.uid != os.geteuid():
            raise PermissionError("Switching users requires root privileges")
        return {}
    return {
        "user": account.uid,
        "group": account.gid,
        "extra_groups": account.groups,
    }


def outer_x_environment(account: Account) -> dict[str, str]:
    environment = _base_environment()
    environment.update(
        {
            "HOME": account.home,
            "USER": account.username,
            "LOGNAME": account.username,
            "SHELL": account.shell,
            "DISPLAY": os.environ["DISPLAY"],
        }
    )
    authority = os.environ.get("XAUTHORITY")
    if authority:
        environment["XAUTHORITY"] = authority
    candidate = Path(f"/run/user/{account.uid}")
    if _owned_directory(candidate, account.uid):
        environment["XDG_RUNTIME_DIR"] = str(candidate)
    return environment


def user_session_environment(
    account: Account,
    display: str,
    pam_environment: dict[str, str],
    runtime: Path,
) -> dict[str, str]:
    environment = _base_environment()
    packaged_keys = {"PATH", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS"}
    for key, value in pam_environment.items():
        if key and key not in packaged_keys and isinstance(value, str):
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
            "XDG_SESSION_DESKTOP": "xfce",
            "XDG_CURRENT_DESKTOP": "XFCE",
            "DESKTOP_SESSION": "xfce",
        }
    )
    for key in (
        "XAUTHORITY",
        "WAYLAND_DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS",
        "SESSION_MANAGER",
        "SUDO_UID",
        "SUDO_GID",
        "SUDO_USER",
        "SUDO_COMMAND",
    ):
        environment.pop(key, None)
    return environment


def _base_environment() -> dict[str, str]:
    allowed = {"PATH", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "TZ", "TERM"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key == "LANG" or key.startswith("LC_")
    }
    environment.setdefault("PATH", "/run/current-system/sw/bin:/usr/bin:/bin")
    return environment


def runtime_directory(account: Account) -> tuple[Path, bool]:
    standard = Path(f"/run/user/{account.uid}")
    if _owned_directory(standard, account.uid):
        return standard, False
    created = Path(tempfile.mkdtemp(prefix=f"userdesk-{account.uid}-", dir="/tmp"))
    os.chown(created, account.uid, account.gid)
    os.chmod(created, 0o700)
    return created, True


def _owned_directory(path: Path, uid: int) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    return path.is_dir() and stat.st_uid == uid
