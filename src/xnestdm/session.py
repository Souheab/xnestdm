from __future__ import annotations

import ctypes
import logging
import os
import pwd
import shutil
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from .auth import Account
from .xsessions import XSession, resolve_session_command

LOG = logging.getLogger(__name__)
VIEWPORT_RESIZE_DELAY_MS = 50


class _XcbCookie(ctypes.Structure):
    _fields_ = [("sequence", ctypes.c_uint)]


class _X11Viewport:
    _CONFIGURE_WIDTH = 1 << 2
    _CONFIGURE_HEIGHT = 1 << 3

    def __init__(self, parent_window_id: int) -> None:
        application = QGuiApplication.instance()
        native_interface = (
            application.nativeInterface() if application is not None else None
        )
        connection = (
            native_interface.connection()
            if native_interface is not None and hasattr(native_interface, "connection")
            else 0
        )
        if not connection:
            raise RuntimeError("Qt is not using an X11 connection")

        self.parent_window_id = parent_window_id
        self.child_window_id = 0
        self._connection = ctypes.c_void_p(connection)
        self._xcb = ctypes.CDLL("libxcb.so.1")
        self._libc = ctypes.CDLL(None)
        self._configure_functions()

    def resize(self, width: int, height: int) -> bool:
        if not self.child_window_id:
            self.child_window_id = self._find_child_window()
        if not self.child_window_id:
            return False

        values = (ctypes.c_uint32 * 2)(width, height)
        self._xcb.xcb_configure_window(
            self._connection,
            self.child_window_id,
            self._CONFIGURE_WIDTH | self._CONFIGURE_HEIGHT,
            values,
        )
        self._xcb.xcb_flush(self._connection)
        return True

    def _find_child_window(self) -> int:
        error = ctypes.c_void_p()
        cookie = self._xcb.xcb_query_tree(self._connection, self.parent_window_id)
        reply = self._xcb.xcb_query_tree_reply(
            self._connection, cookie, ctypes.byref(error)
        )
        if error.value:
            self._libc.free(error)
        if not reply:
            return 0
        try:
            count = self._xcb.xcb_query_tree_children_length(reply)
            if count < 1:
                return 0
            children = self._xcb.xcb_query_tree_children(reply)
            return int(children[0])
        finally:
            self._libc.free(reply)

    def _configure_functions(self) -> None:
        self._xcb.xcb_query_tree.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._xcb.xcb_query_tree.restype = _XcbCookie
        self._xcb.xcb_query_tree_reply.argtypes = [
            ctypes.c_void_p,
            _XcbCookie,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._xcb.xcb_query_tree_reply.restype = ctypes.c_void_p
        self._xcb.xcb_query_tree_children_length.argtypes = [ctypes.c_void_p]
        self._xcb.xcb_query_tree_children_length.restype = ctypes.c_int
        self._xcb.xcb_query_tree_children.argtypes = [ctypes.c_void_p]
        self._xcb.xcb_query_tree_children.restype = ctypes.POINTER(ctypes.c_uint32)
        self._xcb.xcb_configure_window.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._xcb.xcb_configure_window.restype = _XcbCookie
        self._xcb.xcb_flush.argtypes = [ctypes.c_void_p]
        self._xcb.xcb_flush.restype = ctypes.c_int
        self._libc.free.argtypes = [ctypes.c_void_p]
        self._libc.free.restype = None


@dataclass(frozen=True)
class Commands:
    xephyr: str
    session_wrapper: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> "Commands":
        configured_wrapper = os.environ.get("XNESTDM_XSESSION_WRAPPER", "")
        try:
            session_wrapper = tuple(shlex.split(configured_wrapper, posix=True))
        except ValueError:
            session_wrapper = ()
        return cls(
            xephyr=_command("XNESTDM_XEPHYR", "Xephyr"),
            session_wrapper=session_wrapper,
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

        threading.Thread(target=read, name=f"xnestdm-{label}", daemon=True).start()

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
        self.runtime_directory: Path | None = None
        self._owns_runtime_directory = False
        self._state = "idle"
        self._finish_message = ""
        self._deadline = 0.0
        self._display_fd: int | None = None
        self._display_notifier: QSocketNotifier | None = None
        self._display_buffer = b""
        self._viewport: _X11Viewport | None = None
        self._pending_viewport_size: tuple[int, int] | None = None
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(VIEWPORT_RESIZE_DELAY_MS)
        self._resize_timer.timeout.connect(self._apply_viewport_resize)
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
        self._display_buffer = b""
        host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        window_id = int(host.winId())
        width = max(host.width(), 1)
        height = max(host.height(), 1)
        self._pending_viewport_size = (width, height)
        try:
            self._viewport = _X11Viewport(window_id)
        except (OSError, RuntimeError):
            LOG.exception("Could not initialize dynamic Xephyr resizing")
            self._viewport = None

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
            "xnestdm",
            "-title",
            "xnestdm",
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

    def resize_xephyr(self, width: int, height: int) -> None:
        if not self.active:
            return
        self._pending_viewport_size = (max(width, 1), max(height, 1))
        if self.display and not self._resize_timer.isActive():
            self._resize_timer.start()

    def _apply_viewport_resize(self) -> None:
        if (
            self._state not in {"xephyr-ready", "running", "ending-session"}
            or self._viewport is None
            or self._pending_viewport_size is None
        ):
            return
        width, height = self._pending_viewport_size
        if not self._viewport.resize(width, height):
            LOG.warning("Could not find the embedded Xephyr window to resize")

    def start_user_session(
        self,
        account: Account,
        pam_environment: dict[str, str],
        selected_session: XSession,
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
                selected_session,
            )
            if not os.path.isdir(account.home):
                raise RuntimeError(f"Home directory does not exist: {account.home}")

            session_command = resolve_session_command(selected_session, account)
            argv = [*self.commands.session_wrapper, *session_command]
            self.session = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=account.home,
                env=environment,
                start_new_session=True,
                **credential_arguments(account),
            )
            self.output.drain(self.session.stdout, selected_session.name)
            self.session_environment = environment
            self._state = "running"
            self.session_ready.emit()
        except Exception as exc:
            self._begin_terminate(f"Could not start {selected_session.name}: {exc}")

    def request_end_session(self) -> None:
        if self._state != "running" or self.account is None:
            self._begin_terminate("")
            return
        self._state = "ending-session"
        self._deadline = time.monotonic() + 5.0
        self._terminate_process(self.session)

    def stop(self, message: str = "") -> None:
        if not self.active:
            return
        self._begin_terminate(message)

    def shutdown_blocking(self) -> None:
        if not self.active:
            return
        self._terminate_process(self.session)
        self._terminate_process(self.xephyr)
        deadline = time.monotonic() + 1.5
        for process in (self.session, self.xephyr):
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
        self._resize_timer.start(0)
        self.xephyr_ready.emit(self.display)

    def _poll(self) -> None:
        xephyr_code = self.xephyr.poll() if self.xephyr is not None else None
        session_code = self.session.poll() if self.session is not None else None

        if (
            self._state
            in {
                "starting-xephyr",
                "xephyr-ready",
                "running",
                "ending-session",
            }
            and xephyr_code is not None
        ):
            self._begin_terminate(
                f"Xephyr exited unexpectedly with status {xephyr_code}"
            )
        elif self._state in {"running", "ending-session"} and session_code is not None:
            message = (
                f"The nested session exited unexpectedly with status {session_code}"
                if session_code != 0 and self._state != "ending-session"
                else ""
            )
            self._begin_terminate(message)
        elif self._state == "ending-session" and time.monotonic() >= self._deadline:
            self._kill_process(self.session)
            self._begin_terminate("")

        if self._state == "stopping":
            if time.monotonic() >= self._deadline:
                self._kill_process(self.session)
                self._kill_process(self.xephyr)
            all_stopped = all(
                process is None or process.poll() is not None
                for process in (self.session, self.xephyr)
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
        self._resize_timer.stop()
        self._close_display_notifier()
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
        self.runtime_directory = None
        self._owns_runtime_directory = False
        self._viewport = None
        self._pending_viewport_size = None
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


def invoking_account() -> Account:
    real_uid = os.getuid()
    # sudo starts the application with real and effective UID 0. A setuid
    # launcher keeps the caller's real UID, which is authoritative and cannot
    # be replaced by a caller-controlled SUDO_UID environment variable.
    uid_text = (
        os.environ.get("SUDO_UID") if real_uid == 0 and os.geteuid() == 0 else None
    )
    uid = int(uid_text) if uid_text and uid_text.isdigit() else real_uid
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
    selected_session: XSession,
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
            "XDG_SESSION_DESKTOP": selected_session.session_id,
            "XDG_CURRENT_DESKTOP": selected_session.current_desktop,
            "DESKTOP_SESSION": selected_session.session_id,
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
        user_bus = runtime / "bus"
        if user_bus.exists():
            environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={user_bus}"
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
    created = Path(tempfile.mkdtemp(prefix=f"xnestdm-{account.uid}-", dir="/tmp"))
    os.chown(created, account.uid, account.gid)
    os.chmod(created, 0o700)
    return created, True


def _owned_directory(path: Path, uid: int) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    return path.is_dir() and stat.st_uid == uid
