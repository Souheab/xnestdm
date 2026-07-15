from __future__ import annotations

import os
import pwd
import shutil
from pathlib import Path

from userdesk.auth import Account
from userdesk.session import (
    Commands,
    OutputBuffer,
    SessionController,
    credential_arguments,
    invoking_account,
    outer_x_environment,
    runtime_directory,
    user_session_environment,
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


def test_commands_use_dbus_config_beside_nix_binary(monkeypatch) -> None:
    monkeypatch.delenv("USERDESK_DBUS_SESSION_CONFIG", raising=False)
    monkeypatch.setenv(
        "USERDESK_DBUS_RUN_SESSION", "/nix/store/example-dbus/bin/dbus-run-session"
    )
    commands = Commands.from_environment()
    assert commands.dbus_session_config == (
        "/nix/store/example-dbus/share/dbus-1/session.conf"
    )


def test_commands_fall_back_to_source_session_entry(monkeypatch) -> None:
    monkeypatch.delenv("USERDESK_SESSION_ENTRY", raising=False)
    commands = Commands.from_environment()
    assert commands.session_entry[0]
    assert commands.session_entry[1].endswith("userdesk/session_entry.py")


def test_commands_derive_nix_xfce_xinitrc(monkeypatch) -> None:
    monkeypatch.delenv("USERDESK_XFCE_XINITRC", raising=False)
    original_which = shutil.which
    monkeypatch.setattr(
        "userdesk.session.shutil.which",
        lambda command: (
            "/nix/store/example-xfce/bin/xfce4-session"
            if command == "xfce4-session"
            else original_which(command)
        ),
    )
    commands = Commands.from_environment()
    assert commands.xinitrc == ("/nix/store/example-xfce/etc/xdg/xfce4/xinitrc")


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
    )

    assert environment["DISPLAY"] == ":112"
    assert environment["HOME"] == account.home
    assert environment["PAM_THING"] == "yes"
    assert environment["PATH"] == "/packaged/bin"
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
    monkeypatch.setattr("userdesk.session.os.geteuid", lambda: account.uid)
    assert credential_arguments(account) == {}

    monkeypatch.setattr("userdesk.session.os.geteuid", lambda: 0)
    assert credential_arguments(account) == {
        "user": account.uid,
        "group": account.gid,
        "extra_groups": account.groups,
    }


def test_unprivileged_invoking_account_ignores_spoofed_sudo_uid(monkeypatch) -> None:
    account = current_account()
    monkeypatch.setenv("SUDO_UID", "0")
    monkeypatch.setattr("userdesk.session.os.geteuid", lambda: account.uid)
    assert invoking_account().uid == account.uid


def test_runtime_directory_uses_owned_run_directory_or_private_temp(
    monkeypatch, tmp_path: Path
) -> None:
    account = current_account()
    monkeypatch.setattr("userdesk.session.Path", lambda value: tmp_path)
    path, owned = runtime_directory(account)
    assert path == tmp_path
    assert owned is False


class FakeProcess:
    def __init__(self, code=None):
        self.code = code
        self.pid = 999999

    def poll(self):
        return self.code


def test_normal_xfce_exit_finishes_without_error(qapp, monkeypatch) -> None:
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
