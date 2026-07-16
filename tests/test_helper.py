from __future__ import annotations

import json
import os
from pathlib import Path

import pamela
import pytest

from xnestdm.auth import Account
from xnestdm.helper import (
    HelperServer,
    ManagedSession,
    _resolve_user_session,
    user_session_environment,
)
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
    def __init__(self, code=None) -> None:
        self.pid = 999999
        self.stdout = None
        self.code = code

    def poll(self):
        return self.code


def server() -> HelperServer:
    caller = Account("caller", 1000, 1000, "/home/caller", "/bin/sh", (1000,))
    return HelperServer(  # type: ignore[arg-type]
        FakeConnection(), caller, "xnestdm", ("/host/Xsession",)
    )


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
            "tab_id": 4,
            "username": "alice",
            "password": "bad",
        },
    )

    assert helper.connection.messages[-1]["ok"] is False  # type: ignore[attr-defined]
    messages = helper.connection.messages  # type: ignore[attr-defined]
    assert messages[-1]["message"] == expected


def test_helper_starts_session_with_authenticated_credentials(
    monkeypatch, tmp_path: Path
) -> None:
    helper = server()
    account = Account("alice", 1001, 1002, "/home/alice", "/bin/sh", (7, 1002))
    transaction = FakeTransaction(account)
    helper.sessions[7] = ManagedSession(
        7, transaction=transaction  # type: ignore[arg-type]
    )
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
            "tab_id": 7,
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


def test_helper_runs_and_stops_sessions_independently(
    monkeypatch, tmp_path: Path
) -> None:
    helper = server()
    first_account = Account(
        "alice", 1001, 1001, "/home/alice", "/bin/sh", (1001,)
    )
    second_account = Account(
        "bob", 1002, 1002, "/home/bob", "/bin/sh", (1002,)
    )
    helper.sessions[11] = ManagedSession(
        11, transaction=FakeTransaction(first_account)  # type: ignore[arg-type]
    )
    helper.sessions[12] = ManagedSession(
        12, transaction=FakeTransaction(second_account)  # type: ignore[arg-type]
    )
    processes = [FakeProcess(), FakeProcess()]
    monkeypatch.setattr(
        "xnestdm.helper.runtime_directory", lambda account: (tmp_path, False)
    )
    monkeypatch.setattr("xnestdm.helper.os.path.isdir", lambda path: True)
    monkeypatch.setattr(
        "xnestdm.helper.subprocess.Popen", lambda *args, **kwargs: processes.pop(0)
    )
    signaled = []
    monkeypatch.setattr(
        "xnestdm.helper._signal_process",
        lambda process, signum: signaled.append((process, signum)),
    )

    for request_id, tab_id, display in ((1, 11, ":11"), (2, 12, ":12")):
        helper._start_session(
            request_id,
            {
                "protocol": PROTOCOL_VERSION,
                "id": request_id,
                "op": "start_session",
                "tab_id": tab_id,
                "display": display,
                "session": {
                    "name": "Test",
                    "session_id": "test",
                    "current_desktop": "Test",
                    "command": ["/bin/start-test"],
                    "user_fallback": False,
                },
            },
        )

    first_process = helper.sessions[11].process
    second_process = helper.sessions[12].process
    assert first_process is not None
    assert second_process is not None

    helper._stop_session(
        3,
        {
            "protocol": PROTOCOL_VERSION,
            "id": 3,
            "op": "stop_session",
            "tab_id": 11,
        },
    )

    assert helper.sessions[11].stop_requested
    assert not helper.sessions[12].stop_requested
    assert signaled == [(first_process, 15)]


def test_helper_stops_monitoring_session_output_after_eof() -> None:
    helper = server()
    process = FakeProcess()
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stream = os.fdopen(read_fd, "rb", buffering=0)
    process.stdout = stream
    managed = ManagedSession(13, process=process)  # type: ignore[arg-type]

    helper._read_session_output(managed)

    assert stream.closed
    assert process.stdout is None


def test_stopping_pending_authentication_cleans_only_that_transaction() -> None:
    helper = server()
    account = Account("alice", 1001, 1001, "/home/alice", "/bin/sh", (1001,))
    first = FakeTransaction(account)
    second = FakeTransaction(account)
    helper.sessions[21] = ManagedSession(
        21, transaction=first  # type: ignore[arg-type]
    )
    helper.sessions[22] = ManagedSession(
        22, transaction=second  # type: ignore[arg-type]
    )

    helper._stop_session(
        1,
        {
            "protocol": PROTOCOL_VERSION,
            "id": 1,
            "op": "stop_session",
            "tab_id": 21,
        },
    )

    assert first.closed
    assert not second.closed
    assert 21 not in helper.sessions
    assert 22 in helper.sessions
    messages = helper.connection.messages  # type: ignore[attr-defined]
    assert messages[-1]["event"] == "session_finished"
    assert messages[-1]["tab_id"] == 21


def test_helper_shutdown_cleans_every_transaction() -> None:
    helper = server()
    account = Account("alice", 1001, 1001, "/home/alice", "/bin/sh", (1001,))
    transactions = [FakeTransaction(account), FakeTransaction(account)]
    helper.sessions[31] = ManagedSession(
        31, transaction=transactions[0]  # type: ignore[arg-type]
    )
    helper.sessions[32] = ManagedSession(
        32, transaction=transactions[1]  # type: ignore[arg-type]
    )

    helper._cleanup()

    assert all(transaction.closed for transaction in transactions)
    assert helper.sessions == {}


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
