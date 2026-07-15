from __future__ import annotations

import sys

from xnestdm.__main__ import main


def test_setuid_launcher_rejects_pam_service_override(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["xnestdm", "--pam-service", "login"])
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("xnestdm.__main__.os.getuid", lambda: 1000)
    monkeypatch.setattr("xnestdm.__main__.os.geteuid", lambda: 0)

    assert main() == 2
    assert (
        "cannot be used through the privileged NixOS launcher"
        in capsys.readouterr().err
    )
