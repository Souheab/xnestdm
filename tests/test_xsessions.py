from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from xnestdm.xsessions import (
    USER_XSESSION,
    discover_xsessions,
    parse_xsession,
    preferred_xsession_index,
    resolve_session_command,
    xsession_directories,
)


def write_session(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.desktop"
    path.write_text(f"[Desktop Entry]\n{body}", encoding="utf-8")
    return path


def test_xsession_directories_include_overrides_xdg_and_system_paths(
    tmp_path: Path,
) -> None:
    directories = xsession_directories(
        {
            "XNESTDM_XSESSION_DIRS": "/host/first:/host/second",
            "XDG_DATA_HOME": str(tmp_path / "data-home"),
            "XDG_DATA_DIRS": "/host/share:/other/share",
        },
        tmp_path,
    )

    assert directories == [
        Path("/host/first"),
        Path("/host/second"),
        tmp_path / "data-home/xsessions",
        Path("/host/share/xsessions"),
        Path("/other/share/xsessions"),
        Path("/usr/local/share/xsessions"),
        Path("/usr/share/xsessions"),
    ]
    assert all("wayland-sessions" not in str(path) for path in directories)


def test_parse_xsession_uses_localized_name_and_desktop_fields(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "example",
        """Type=XSession
Name=Example
Name[fr]=Exemple
Exec=/bin/sh --name %c --source %k %F %%
TryExec=/bin/sh
DesktopNames=Example;Secondary;
""",
    )

    session = parse_xsession(path, {"LANG": "fr_FR.UTF-8", "PATH": "/bin"})

    assert session is not None
    assert session.name == "Exemple"
    assert session.command == (
        "/bin/sh",
        "--name",
        "Exemple",
        "--source",
        str(path),
        "%",
    )
    assert session.desktop_names == ("Example", "Secondary")


def test_discovery_filters_invalid_entries_and_deduplicates_by_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    preferred = tmp_path / "preferred"
    secondary = tmp_path / "secondary"
    write_session(preferred, "same", "Name=Preferred\nExec=/bin/true\n")
    write_session(secondary, "same", "Name=Secondary\nExec=/bin/true\n")
    write_session(
        preferred,
        "hidden",
        "Name=Hidden\nExec=/bin/true\nHidden=true\n",
    )
    write_session(
        preferred,
        "missing",
        "Name=Missing\nExec=/missing\nTryExec=/definitely/missing\n",
    )
    monkeypatch.setattr(
        "xnestdm.xsessions.xsession_directories",
        lambda environment, home: [preferred, secondary],
    )

    sessions = discover_xsessions(
        {
            "XNESTDM_XSESSION_DIRS": f"{preferred}:{secondary}",
            "XDG_DATA_HOME": str(tmp_path / "empty"),
            "XDG_DATA_DIRS": str(tmp_path / "also-empty"),
            "PATH": "/bin",
        },
        tmp_path,
    )

    assert [(session.session_id, session.name) for session in sessions] == [
        ("same", "Preferred")
    ]


def test_discovery_returns_user_fallback_when_catalog_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "xnestdm.xsessions.xsession_directories",
        lambda environment, home: [],
    )
    sessions = discover_xsessions(
        {
            "XDG_DATA_HOME": str(tmp_path / "empty"),
            "XDG_DATA_DIRS": str(tmp_path / "also-empty"),
        },
        tmp_path,
    )
    assert sessions == [USER_XSESSION]


def test_preferred_session_matches_host_desktop_name(
    tmp_path: Path, monkeypatch
) -> None:
    directory = tmp_path / "sessions"
    write_session(
        directory,
        "plasma",
        "Name=Plasma\nExec=/bin/true\nDesktopNames=KDE;\n",
    )
    write_session(
        directory,
        "sade",
        "Name=SADE\nExec=/bin/true\nDesktopNames=none+SADE;\n",
    )
    monkeypatch.setattr(
        "xnestdm.xsessions.xsession_directories",
        lambda environment, home: [directory],
    )
    sessions = discover_xsessions(
        {
            "XNESTDM_XSESSION_DIRS": str(directory),
            "XDG_DATA_HOME": str(tmp_path / "empty"),
            "XDG_DATA_DIRS": str(tmp_path / "also-empty"),
            "PATH": "/bin",
        },
        tmp_path,
    )

    index = preferred_xsession_index(sessions, {"DESKTOP_SESSION": "none+SADE"})
    assert sessions[index].name == "SADE"


def test_user_fallback_prefers_xsession_then_xinitrc(tmp_path: Path) -> None:
    xsession = tmp_path / ".xsession"
    xsession.write_text("#!/bin/sh\n", encoding="utf-8")
    xsession.chmod(0o700)
    xinitrc = tmp_path / ".xinitrc"
    xinitrc.write_text("start-window-manager\n", encoding="utf-8")
    account = SimpleNamespace(home=str(tmp_path), shell="/bin/sh")

    assert resolve_session_command(USER_XSESSION, account) == (str(xsession),)

    xsession.chmod(0o600)
    assert resolve_session_command(USER_XSESSION, account) == (
        "/bin/sh",
        str(xinitrc),
    )
