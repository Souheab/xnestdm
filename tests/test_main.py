from __future__ import annotations

from xnestdm.__main__ import _drop_privileges, _prepare_helper
from xnestdm.auth import Account


def test_setuid_launcher_rejects_pam_service_override(monkeypatch) -> None:
    monkeypatch.setattr("xnestdm.__main__.os.getuid", lambda: 1000)
    monkeypatch.setattr("xnestdm.__main__.os.geteuid", lambda: 0)

    try:
        _prepare_helper("login", False)
    except RuntimeError as exc:
        assert "cannot be used through the privileged NixOS launcher" in str(exc)
    else:
        raise AssertionError("setuid PAM override was accepted")


def test_drop_privileges_sets_groups_before_ids(monkeypatch) -> None:
    account = Account("alice", 1001, 1002, "/home/alice", "/bin/fish", (7, 1002))
    calls = []
    monkeypatch.setattr(
        "xnestdm.__main__.os.initgroups", lambda *args: calls.append(("groups", *args))
    )
    monkeypatch.setattr(
        "xnestdm.__main__.os.setgid", lambda gid: calls.append(("gid", gid))
    )
    monkeypatch.setattr(
        "xnestdm.__main__.os.setuid", lambda uid: calls.append(("uid", uid))
    )
    monkeypatch.setattr("xnestdm.__main__.os.getuid", lambda: account.uid)
    monkeypatch.setattr("xnestdm.__main__.os.geteuid", lambda: account.uid)
    monkeypatch.setattr(
        "xnestdm.__main__.os.stat",
        lambda path: type("Stat", (), {"st_uid": account.uid})(),
    )

    _drop_privileges(account)

    assert calls == [
        ("groups", "alice", 1002),
        ("gid", 1002),
        ("uid", 1001),
    ]


def test_sudo_bootstraps_helper_before_dropping_gui_privileges(monkeypatch) -> None:
    account = Account("alice", 1001, 1002, "/home/alice", "/bin/sh", (1002,))
    bootstrap = object()
    calls = []
    monkeypatch.setenv("SUDO_UID", str(account.uid))
    monkeypatch.setattr("xnestdm.__main__.os.getuid", lambda: 0)
    monkeypatch.setattr("xnestdm.__main__.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "xnestdm.__main__.shutil.which", lambda command: "/pkg/xnestdm-helper"
    )
    monkeypatch.setattr("xnestdm.auth.Account.from_uid", lambda uid: account)
    monkeypatch.setattr("xnestdm.auth.select_pam_service", lambda value: "xnestdm")
    monkeypatch.setattr("xnestdm.helper_transport.configured_helper", lambda: None)

    def start_helper(*args, **kwargs):
        calls.append(("helper", args, kwargs))
        return bootstrap

    monkeypatch.setattr("xnestdm.helper_transport.start_helper", start_helper)
    monkeypatch.setattr(
        "xnestdm.__main__._drop_privileges",
        lambda selected: calls.append(("drop", selected)),
    )

    assert _prepare_helper(None, False) is bootstrap
    assert calls[0][0] == "helper"
    assert calls[0][2]["caller_uid"] == account.uid
    assert calls[1] == ("drop", account)
