from __future__ import annotations

from types import SimpleNamespace

import pytest
import pamela

from userdesk.auth import Account, PamTransaction, PamWorker, select_pam_service


class FakeHandle:
    def __init__(self) -> None:
        self.items: dict[int, str] = {}
        self.environment: dict[str, str] = {}
        self.opened = False
        self.closed = False

    def set_item(self, key: int, value: str) -> None:
        self.items[key] = value

    def put_env(self, key: str, value: str) -> None:
        self.environment[key] = value

    def open_session(self) -> None:
        self.opened = True

    def close_session(self) -> None:
        self.closed = True

    def get_envlist(self) -> dict[str, str]:
        return {"PAM_VALUE": "present", **self.environment}


def test_authenticate_keeps_handle_and_checks_account(monkeypatch) -> None:
    handle = FakeHandle()
    calls: dict[str, object] = {}

    def authenticate(username, password, **kwargs):
        calls.update(username=username, password=password, **kwargs)
        return handle

    account = Account("alice", 1001, 1001, "/home/alice", "/bin/sh", (1001,))
    monkeypatch.setattr("userdesk.auth.pamela.authenticate", authenticate)
    monkeypatch.setattr(Account, "from_username", lambda username: account)

    transaction = PamTransaction.authenticate("alice", "secret", "userdesk")

    assert transaction.handle is handle
    assert calls["service"] == "userdesk"
    assert calls["check"] is True
    assert calls["close"] is False


def test_open_and_close_use_same_pam_handle(monkeypatch) -> None:
    handle = FakeHandle()
    account = Account("alice", 1001, 1001, "/home/alice", "/bin/sh", (1001,))
    transaction = PamTransaction(handle, account, "userdesk")
    calls = SimpleNamespace(setcred=0, ended=0)
    monkeypatch.setattr(
        "userdesk.auth.pamela.PAM_SETCRED",
        lambda pam_handle, flags: setattr(calls, "setcred", calls.setcred + 1),
    )
    monkeypatch.setattr(
        "userdesk.auth.pamela.pam_end",
        lambda pam_handle: setattr(calls, "ended", calls.ended + 1),
    )

    environment = transaction.open(":101", "launcher")
    transaction.close()
    transaction.close()

    assert handle.opened and handle.closed
    assert environment["DISPLAY"] == ":101"
    assert calls.setcred == 1
    assert calls.ended == 1
    assert transaction.closed


def test_login_service_skips_session_hooks(monkeypatch) -> None:
    handle = FakeHandle()
    account = Account("alice", 1001, 1001, "/home/alice", "/bin/sh", (1001,))
    transaction = PamTransaction(handle, account, "login")
    monkeypatch.setattr("userdesk.auth.pamela.PAM_SETCRED", lambda *args: 0)
    monkeypatch.setattr("userdesk.auth.pamela.pam_end", lambda handle: None)

    environment = transaction.open(":101", "launcher")
    transaction.close()

    assert environment["DISPLAY"] == ":101"
    assert handle.opened is False
    assert handle.closed is False
    assert transaction.manage_session is False


def test_account_lookup_failure_ends_pam(monkeypatch) -> None:
    handle = FakeHandle()
    ended: list[object] = []
    monkeypatch.setattr("userdesk.auth.pamela.authenticate", lambda *a, **k: handle)
    monkeypatch.setattr(
        Account, "from_username", lambda username: (_ for _ in ()).throw(KeyError())
    )
    monkeypatch.setattr("userdesk.auth.pamela.pam_end", ended.append)

    with pytest.raises(KeyError):
        PamTransaction.authenticate("missing", "secret", "login")
    assert ended == [handle]


def test_pam_service_selection(monkeypatch) -> None:
    monkeypatch.setattr("userdesk.auth.os.path.exists", lambda path: True)
    assert select_pam_service(None) == "userdesk"
    assert select_pam_service("custom") == "custom"
    monkeypatch.setattr("userdesk.auth.os.path.exists", lambda path: False)
    assert select_pam_service(None) == "login"


@pytest.mark.parametrize(
    ("error_number", "expected"),
    [
        (7, "Authentication failed"),
        (12, "The account or password has expired"),
    ],
)
def test_worker_classifies_pam_failures(monkeypatch, error_number, expected) -> None:
    def fail(*args, **kwargs):
        raise pamela.PAMError(errno=error_number)

    monkeypatch.setattr(PamTransaction, "authenticate", fail)
    worker = PamWorker()
    outcomes = []
    worker.authentication_finished.connect(outcomes.append)

    worker.authenticate("alice", "bad", "userdesk")

    assert outcomes[0].ok is False
    assert outcomes[0].message == expected
