from __future__ import annotations

import os
import pwd
from pathlib import Path

from xnestdm.auth import Account
from xnestdm.session import (
    Commands,
    OutputBuffer,
    SessionController,
    credential_arguments,
    invoking_account,
    outer_x_environment,
    runtime_directory,
    user_session_environment,
)
from xnestdm.xsessions import XSession


SESSION = XSession(
    "test-session",
    "Test Session",
    ("/host/bin/start-session", "--nested"),
    ("Test", "Example"),
    Path("/host/share/xsessions/test-session.desktop"),
)


def current_account() -> Account:
    record = pwd.getpwuid(os.getuid())
    return Account(
        record.pw_name,
        record.pw_uid,
        record.pw_gid,
        record.pw_dir,
        record.pw_shell,
        tuple(os.getgrouplist(record.pw_name, record.pw_gid)),
    )


def test_commands_read_optional_host_session_wrapper(monkeypatch) -> None:
    monkeypatch.setenv("XNESTDM_XEPHYR", "/app/bin/Xephyr")
    monkeypatch.setenv("XNESTDM_XSESSION_WRAPPER", "/host/Xsession --nested")
    commands = Commands.from_environment()
    assert commands.xephyr == "/app/bin/Xephyr"
    assert commands.session_wrapper == ("/host/Xsession", "--nested")


def test_user_environment_is_sanitized(monkeypatch, tmp_path: Path) -> None:
    account = current_account()
    monkeypatch.setenv("PATH", "/packaged/bin")
    monkeypatch.setenv("XDG_DATA_DIRS", "/packaged/share")
    monkeypatch.setenv("SUDO_UID", "123")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "outer-bus")

    environment = user_session_environment(
        account,
        ":112",
        {
            "PAM_THING": "yes",
            "PATH": "/pam/bin",
            "XAUTHORITY": "/root/cookie",
        },
        tmp_path,
        SESSION,
    )

    assert environment["DISPLAY"] == ":112"
    assert environment["HOME"] == account.home
    assert environment["PAM_THING"] == "yes"
    assert environment["PATH"] == "/pam/bin"
    assert environment["XDG_DATA_DIRS"] == "/packaged/share"
    assert environment["XDG_SESSION_DESKTOP"] == "test-session"
    assert environment["XDG_CURRENT_DESKTOP"] == "Test:Example"
    assert "DBUS_SESSION_BUS_ADDRESS" not in environment
    assert "XAUTHORITY" not in environment
    assert "SUDO_UID" not in environment


def test_outer_environment_preserves_x_credentials(monkeypatch) -> None:
    account = current_account()
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "/tmp/outer-xauthority")
    environment = outer_x_environment(account)
    assert environment["DISPLAY"] == ":0"
    assert environment["XAUTHORITY"] == "/tmp/outer-xauthority"
    assert environment["USER"] == account.username


def test_credential_arguments_avoid_unprivileged_setgroups(monkeypatch) -> None:
    account = current_account()
    monkeypatch.setattr("xnestdm.session.os.geteuid", lambda: account.uid)
    assert credential_arguments(account) == {}

    monkeypatch.setattr("xnestdm.session.os.geteuid", lambda: 0)
    assert credential_arguments(account) == {
        "user": account.uid,
        "group": account.gid,
        "extra_groups": account.groups,
    }


def test_unprivileged_invoking_account_ignores_spoofed_sudo_uid(monkeypatch) -> None:
    account = current_account()
    monkeypatch.setenv("SUDO_UID", "0")
    monkeypatch.setattr("xnestdm.session.os.geteuid", lambda: account.uid)
    assert invoking_account().uid == account.uid


def test_runtime_directory_uses_owned_run_directory_or_private_temp(
    monkeypatch, tmp_path: Path
) -> None:
    account = current_account()
    monkeypatch.setattr("xnestdm.session.Path", lambda value: tmp_path)
    path, owned = runtime_directory(account)
    assert path == tmp_path
    assert owned is False


class FakeProcess:
    def __init__(self, code=None):
        self.code = code
        self.pid = 999999
        self.stdout = None

    def poll(self):
        return self.code


def test_selected_session_is_started_through_host_wrapper(
    qapp, monkeypatch, tmp_path: Path
) -> None:
    account = current_account()
    controller = SessionController(Commands("/app/bin/Xephyr", ("/host/Xsession",)))
    controller._state = "xephyr-ready"
    controller.display = ":8"
    launches: list[tuple[list[str], dict[str, object]]] = []

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(
        "xnestdm.session.runtime_directory", lambda account: (tmp_path, False)
    )
    monkeypatch.setattr("xnestdm.session.subprocess.Popen", launch)

    controller.start_user_session(account, {}, SESSION)

    assert launches[0][0] == [
        "/host/Xsession",
        "/host/bin/start-session",
        "--nested",
    ]
    assert launches[0][1]["cwd"] == account.home
    assert launches[0][1]["env"]["DISPLAY"] == ":8"
    assert controller._state == "running"


def test_end_session_terminates_session_before_xephyr(qapp, monkeypatch) -> None:
    controller = SessionController()
    controller._state = "running"
    controller.account = current_account()
    controller.session = FakeProcess()  # type: ignore[assignment]
    controller.xephyr = FakeProcess()  # type: ignore[assignment]
    terminated = []
    monkeypatch.setattr(controller, "_terminate_process", terminated.append)

    controller.request_end_session()

    assert controller._state == "ending-session"
    assert terminated == [controller.session]


def test_end_session_timeout_forces_session_and_stops_xephyr(qapp, monkeypatch) -> None:
    controller = SessionController()
    controller._state = "ending-session"
    controller._deadline = 1.0
    controller.session = FakeProcess()  # type: ignore[assignment]
    controller.xephyr = FakeProcess()  # type: ignore[assignment]
    killed = []
    terminated = []
    monkeypatch.setattr("xnestdm.session.time.monotonic", lambda: 2.0)
    monkeypatch.setattr(controller, "_kill_process", killed.append)
    monkeypatch.setattr(controller, "_terminate_process", terminated.append)

    controller._poll()

    assert killed == [controller.session]
    assert terminated == [controller.session, controller.xephyr]
    assert controller._state == "stopping"


class FakeViewport:
    def __init__(self):
        self.sizes: list[tuple[int, int]] = []

    def resize(self, width: int, height: int) -> bool:
        self.sizes.append((width, height))
        return True


def test_viewport_resize_uses_latest_size(qapp) -> None:
    controller = SessionController()
    viewport = FakeViewport()
    controller._state = "running"
    controller.display = ":7"
    controller._viewport = viewport  # type: ignore[assignment]

    controller.resize_xephyr(800, 600)
    controller.resize_xephyr(1024, 768)
    controller._resize_timer.stop()
    controller._apply_viewport_resize()

    assert viewport.sizes == [(1024, 768)]


def test_normal_session_exit_finishes_without_error(qapp, monkeypatch) -> None:
    controller = SessionController()
    controller._state = "running"
    controller.xephyr = FakeProcess()  # type: ignore[assignment]
    controller.session = FakeProcess(0)  # type: ignore[assignment]
    monkeypatch.setattr(controller, "_terminate_process", lambda process: None)
    messages: list[str] = []
    controller.finished.connect(messages.append)

    controller._poll()
    assert controller._state == "stopping"
    controller.xephyr.code = 0  # type: ignore[union-attr]
    controller._poll()

    assert messages == [""]
    assert not controller.active


def test_unexpected_xephyr_exit_reports_error(qapp, monkeypatch) -> None:
    controller = SessionController()
    controller._state = "running"
    controller.xephyr = FakeProcess(1)  # type: ignore[assignment]
    controller.session = FakeProcess()  # type: ignore[assignment]
    monkeypatch.setattr(controller, "_terminate_process", lambda process: None)

    controller._poll()

    assert controller._state == "stopping"
    assert "Xephyr exited unexpectedly" in controller._finish_message


def test_output_buffer_is_bounded() -> None:
    buffer = OutputBuffer(limit=2)
    buffer._lines.extend(["one", "two", "three"])
    assert buffer.tail() == "two\nthree"
