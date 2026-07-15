from __future__ import annotations

import json
from pathlib import Path

import pamela
import pytest

from xnestdm.auth import Account
from xnestdm.helper import HelperServer, _resolve_user_session, user_session_environment
from xnestdm.helper_protocol import PROTOCOL_VERSION


class FakeConnection:
    def __init__(self) -> None:
        self.messages = []

    def sendall(self, payload: bytes) -> None:
        self.messages.append(json.loads(payload))


class FakeTransaction:
    def __init__(self, account: Account) -> None:
        self.account = account
        self.opened = []
        self.closed = False

    def open(self, *args):
        self.opened.append(args)
        return {"PAM_VALUE": "present"}

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 999999
        self.stdout = None

    def poll(self):
        return None


def server() -> HelperServer:
    caller = Account("caller", 1000, 1000, "/home/caller", "/bin/sh", (1000,))
    return HelperServer(FakeConnection(), caller, "xnestdm", ("/host/Xsession",))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("error_number", "expected"),
    [(7, "Authentication failed"), (12, "The account or password has expired")],
)
def test_helper_classifies_authentication_failures(
    monkeypatch, error_number, expected
) -> None:
    helper = server()

    def fail(*args, **kwargs):
        raise pamela.PAMError(errno=error_number)

    monkeypatch.setattr("xnestdm.helper.PamTransaction.authenticate", fail)
    helper._authenticate(
        1,
        {
            "protocol": PROTOCOL_VERSION,
            "id": 1,
            "op": "authenticate",
            "username": "alice",
            "password": "bad",
        },
    )

    assert helper.connection.messages[-1]["ok"] is False  # type: ignore[attr-defined]
    assert helper.connection.messages[-1]["message"] == expected  # type: ignore[attr-defined]


def test_helper_starts_session_with_authenticated_credentials(
    monkeypatch, tmp_path: Path
) -> None:
    helper = server()
    account = Account("alice", 1001, 1002, "/home/alice", "/bin/sh", (7, 1002))
    transaction = FakeTransaction(account)
    helper.transaction = transaction  # type: ignore[assignment]
    launches = []

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(
        "xnestdm.helper.runtime_directory", lambda account: (tmp_path, False)
    )
    monkeypatch.setattr("xnestdm.helper.os.path.isdir", lambda path: True)
    monkeypatch.setattr("xnestdm.helper.subprocess.Popen", launch)
    helper._start_session(
        2,
        {
            "protocol": PROTOCOL_VERSION,
            "id": 2,
            "op": "start_session",
            "display": ":9",
            "session": {
                "name": "Test",
                "session_id": "test",
                "current_desktop": "Test",
                "command": ["/bin/start-test"],
                "user_fallback": False,
            },
        },
    )

    assert launches[0][0] == ["/host/Xsession", "/bin/start-test"]
    assert launches[0][1]["user"] == account.uid
    assert launches[0][1]["group"] == account.gid
    assert launches[0][1]["extra_groups"] == account.groups
    assert launches[0][1]["close_fds"] is True
    assert transaction.opened[0][0] == ":9"


def test_helper_session_environment_removes_outer_credentials(tmp_path: Path) -> None:
    account = Account("alice", 1001, 1001, "/home/alice", "/bin/sh", (1001,))
    environment = user_session_environment(
        account,
        ":4",
        {"XAUTHORITY": "/root/cookie", "PAM_VALUE": "yes"},
        tmp_path,
        "test",
        "Test",
    )

    assert environment["PAM_VALUE"] == "yes"
    assert environment["DISPLAY"] == ":4"
    assert "XAUTHORITY" not in environment


def test_helper_resolves_authenticated_users_private_xsession(tmp_path: Path) -> None:
    script = tmp_path / ".xsession"
    script.write_text("#!/bin/sh\nexec true\n")
    script.chmod(0o700)
    account = Account("alice", 1001, 1001, str(tmp_path), "/bin/sh", (1001,))

    assert _resolve_user_session(account) == (str(script),)
