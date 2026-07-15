from __future__ import annotations

import logging
import os
import pwd
from dataclasses import dataclass
from typing import Any, Mapping

import pamela

LOG = logging.getLogger(__name__)
AUTHENTICATION_ONLY_SERVICES = {"login"}

# Linux-PAM return values for conditions worth distinguishing in the UI.
PAM_NEW_AUTHTOK_REQD = 12
PAM_ACCT_EXPIRED = 13
PAM_CRED_EXPIRED = 16
PAM_AUTHTOK_EXPIRED = 27
EXPIRED_CODES = {
    PAM_NEW_AUTHTOK_REQD,
    PAM_ACCT_EXPIRED,
    PAM_CRED_EXPIRED,
    PAM_AUTHTOK_EXPIRED,
}


@dataclass(frozen=True)
class Account:
    username: str
    uid: int
    gid: int
    home: str
    shell: str
    groups: tuple[int, ...]

    @classmethod
    def from_uid(cls, uid: int) -> "Account":
        return cls._from_record(pwd.getpwuid(uid))

    @classmethod
    def from_username(cls, username: str) -> "Account":
        return cls._from_record(pwd.getpwnam(username))

    @classmethod
    def _from_record(cls, record: Any) -> "Account":
        groups = tuple(sorted(set(os.getgrouplist(record.pw_name, record.pw_gid))))
        return cls(
            username=record.pw_name,
            uid=record.pw_uid,
            gid=record.pw_gid,
            home=record.pw_dir,
            shell=record.pw_shell or "/bin/sh",
            groups=groups,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "username": self.username,
            "uid": self.uid,
            "gid": self.gid,
            "home": self.home,
            "shell": self.shell,
            "groups": list(self.groups),
        }

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "Account":
        username = values.get("username")
        uid = values.get("uid")
        gid = values.get("gid")
        home = values.get("home")
        shell = values.get("shell")
        groups = values.get("groups")
        if not (
            isinstance(username, str)
            and isinstance(uid, int)
            and isinstance(gid, int)
            and isinstance(home, str)
            and isinstance(shell, str)
            and isinstance(groups, list)
            and all(isinstance(group, int) for group in groups)
        ):
            raise ValueError("Invalid account data from privileged helper")
        return cls(username, uid, gid, home, shell, tuple(groups))


@dataclass(frozen=True)
class AuthenticationOutcome:
    ok: bool
    account: Account | None = None
    message: str = ""


@dataclass(frozen=True)
class SessionStartOutcome:
    ok: bool
    message: str = ""


class PamTransaction:
    """Own one PAM handle from authentication through session close."""

    def __init__(self, handle: Any, account: Account, service: str):
        self.handle = handle
        self.account = account
        self.service = service
        self.manage_session = service not in AUTHENTICATION_ONLY_SERVICES
        self.session_open = False
        self.closed = False

    @classmethod
    def authenticate(
        cls, username: str, password: str, service: str
    ) -> "PamTransaction":
        handle = pamela.authenticate(
            username,
            password,
            service=service,
            check=True,
            close=False,
            resetcred=pamela.PAM_ESTABLISH_CRED,
        )
        try:
            account = Account.from_username(username)
        except Exception:
            pamela.pam_end(handle)
            raise
        return cls(handle, account, service)

    def open(
        self,
        display: str,
        invoking_user: str,
        session_id: str,
        current_desktop: str,
    ) -> dict[str, str]:
        if self.closed:
            raise RuntimeError("PAM transaction is already closed")
        self.handle.set_item(pamela.PAM_TTY, f"xnestdm/{display}")
        if invoking_user:
            self.handle.set_item(pamela.PAM_RUSER, invoking_user)

        values = {
            "DISPLAY": display,
            "XDG_SESSION_TYPE": "x11",
            "XDG_SESSION_CLASS": "user",
            "XDG_SESSION_DESKTOP": session_id,
            "XDG_CURRENT_DESKTOP": current_desktop,
            "DESKTOP_SESSION": session_id,
        }
        for key, value in values.items():
            self.handle.put_env(key, value)
        if self.manage_session:
            self.handle.open_session()
            self.session_open = True
        else:
            LOG.info(
                "Skipping PAM open_session for authentication-only service %s",
                self.service,
            )
        return dict(self.handle.get_envlist())

    def close(self) -> None:
        if self.closed:
            return
        first_error: Exception | None = None
        if self.session_open:
            try:
                self.handle.close_session()
            except Exception as exc:  # cleanup must continue
                first_error = exc
                LOG.exception("Could not close PAM session")
        try:
            pamela.PAM_SETCRED(self.handle, pamela.PAM_DELETE_CRED)
        except Exception:
            LOG.exception("Could not delete PAM credentials")
        try:
            pamela.pam_end(self.handle)
        except Exception as exc:
            first_error = first_error or exc
            LOG.exception("Could not end PAM transaction")
        self.closed = True
        if first_error:
            raise first_error


def select_pam_service(override: str | None) -> str:
    if override:
        return override
    if os.path.exists("/etc/pam.d/xnestdm"):
        return "xnestdm"
    return "login"
