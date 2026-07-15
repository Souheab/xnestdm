from __future__ import annotations

import configparser
import locale
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol


class AccountLike(Protocol):
    home: str
    shell: str


@dataclass(frozen=True)
class XSession:
    session_id: str
    name: str
    command: tuple[str, ...]
    desktop_names: tuple[str, ...]
    source: Path | None
    user_fallback: bool = False

    @property
    def current_desktop(self) -> str:
        return ":".join(self.desktop_names) or self.session_id


USER_XSESSION = XSession(
    session_id="user-xsession",
    name="User X session",
    command=(),
    desktop_names=("user-xsession",),
    source=None,
    user_fallback=True,
)

_IGNORED_FIELD_CODES = {"%f", "%F", "%u", "%U", "%v", "%m"}
_FIELD_CODE = re.compile(r"%[A-Za-z]")


def discover_xsessions(
    environment: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> list[XSession]:
    values = environment if environment is not None else os.environ
    sessions: list[XSession] = []
    seen_ids: set[str] = set()

    for directory in xsession_directories(values, home):
        try:
            entries = sorted(directory.glob("*.desktop"), key=lambda path: path.name)
        except OSError:
            continue
        for path in entries:
            session_id = path.stem
            if session_id in seen_ids:
                continue
            session = parse_xsession(path, values)
            if session is None:
                continue
            seen_ids.add(session_id)
            sessions.append(session)

    if not sessions:
        return [USER_XSESSION]
    return sorted(
        sessions, key=lambda session: (session.name.casefold(), session.session_id)
    )


def xsession_directories(
    environment: Mapping[str, str], home: str | Path | None
) -> list[Path]:
    directories: list[Path] = []

    configured = environment.get("XNESTDM_XSESSION_DIRS", "")
    directories.extend(Path(item) for item in configured.split(os.pathsep) if item)

    if home is None:
        home = environment.get("HOME", str(Path.home()))
    data_home = environment.get("XDG_DATA_HOME")
    directories.append(
        (Path(data_home) if data_home else Path(home) / ".local/share") / "xsessions"
    )

    data_directories = environment.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    directories.extend(
        Path(item) / "xsessions" for item in data_directories.split(os.pathsep) if item
    )
    directories.extend(
        [Path("/usr/local/share/xsessions"), Path("/usr/share/xsessions")]
    )

    result: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        normalized = directory.expanduser()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def parse_xsession(
    path: Path, environment: Mapping[str, str] | None = None
) -> XSession | None:
    values = environment if environment is not None else os.environ
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
        entry = parser["Desktop Entry"]
    except (OSError, UnicodeError, configparser.Error, KeyError):
        return None

    if entry.get("Type", "Application") not in {"Application", "XSession"}:
        return None
    if _desktop_boolean(entry.get("Hidden")) or _desktop_boolean(
        entry.get("NoDisplay")
    ):
        return None

    name = _localized_value(entry, "Name", values) or path.stem
    command = _desktop_exec(entry.get("Exec", ""), name, path, entry.get("Icon"))
    if not command:
        return None

    try_exec = entry.get("TryExec", "").strip()
    if try_exec and not _executable_exists(try_exec, values.get("PATH")):
        return None

    desktop_names = tuple(
        value.strip()
        for value in entry.get("DesktopNames", "").split(";")
        if value.strip()
    ) or (path.stem,)
    return XSession(
        session_id=path.stem,
        name=name,
        command=command,
        desktop_names=desktop_names,
        source=path,
    )


def preferred_xsession_index(
    sessions: list[XSession], environment: Mapping[str, str] | None = None
) -> int:
    values = environment if environment is not None else os.environ
    candidates: list[str] = []
    for key in ("DESKTOP_SESSION", "GDMSESSION", "XDG_CURRENT_DESKTOP"):
        value = values.get(key, "")
        candidates.extend(part for part in re.split(r"[:;]", value) if part)

    normalized = {
        candidate.removesuffix(".desktop").casefold() for candidate in candidates
    }
    for index, session in enumerate(sessions):
        identities = {session.session_id.casefold(), session.name.casefold()}
        identities.update(name.casefold() for name in session.desktop_names)
        if identities & normalized:
            return index
    return 0


def resolve_session_command(session: XSession, account: AccountLike) -> tuple[str, ...]:
    if not session.user_fallback:
        return session.command

    home = Path(account.home)
    xsession = home / ".xsession"
    if xsession.is_file() and os.access(xsession, os.X_OK):
        return (str(xsession),)

    xinitrc = home / ".xinitrc"
    if xinitrc.is_file():
        return (account.shell, str(xinitrc))

    for path in (Path("/etc/X11/Xsession"), Path("/etc/X11/xinit/xinitrc")):
        if not path.is_file():
            continue
        if os.access(path, os.X_OK):
            return (str(path),)
        return (account.shell, str(path))

    raise RuntimeError(
        "No host X sessions were found and the selected user has no ~/.xsession "
        "or ~/.xinitrc"
    )


def _desktop_boolean(value: str | None) -> bool:
    return bool(value) and value.strip().casefold() in {"1", "true", "yes"}


def _localized_value(
    entry: configparser.SectionProxy,
    key: str,
    environment: Mapping[str, str],
) -> str:
    for language in _language_candidates(environment):
        localized = entry.get(f"{key}[{language}]", "").strip()
        if localized:
            return localized
    return entry.get(key, "").strip()


def _language_candidates(environment: Mapping[str, str]) -> list[str]:
    raw_values: list[str] = []
    language = environment.get("LANGUAGE", "")
    if language:
        raw_values.extend(language.split(":"))
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if environment.get(key):
            raw_values.append(environment[key])
            break
    if not raw_values:
        default_locale = locale.getlocale()[0]
        if default_locale:
            raw_values.append(default_locale)

    result: list[str] = []
    for raw in raw_values:
        normalized = raw.split(".", 1)[0].split("@", 1)[0]
        for candidate in (normalized, normalized.split("_", 1)[0]):
            if candidate and candidate not in result:
                result.append(candidate)
    return result


def _desktop_exec(
    value: str, name: str, source: Path, icon: str | None
) -> tuple[str, ...]:
    try:
        tokens = shlex.split(value, posix=True)
    except ValueError:
        return ()

    command: list[str] = []
    for token in tokens:
        if token in _IGNORED_FIELD_CODES:
            continue
        if token == "%i":
            if icon:
                command.extend(["--icon", icon])
            continue

        token = token.replace("%%", "\0")
        token = token.replace("%c", name).replace("%k", str(source))
        for code in _IGNORED_FIELD_CODES:
            token = token.replace(code, "")
        token = token.replace("%i", "")
        if _FIELD_CODE.search(token):
            return ()
        token = token.replace("\0", "%")
        if token:
            command.append(token)
    return tuple(command)


def _executable_exists(command: str, path: str | None) -> bool:
    try:
        executable = shlex.split(command, posix=True)[0]
    except (ValueError, IndexError):
        return False
    if os.path.isabs(executable):
        return os.path.isfile(executable) and os.access(executable, os.X_OK)
    return shutil.which(executable, path=path) is not None
