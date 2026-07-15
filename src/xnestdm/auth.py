from __future__ import annotations

import logging
import os
import pwd
from dataclasses import dataclass
from typing import Any

import pamela
from PySide6.QtCore import QObject, Signal, Slot

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
    def from_username(cls, username: str) -> "Account":
        record = pwd.getpwnam(username)
        groups = tuple(sorted(set(os.getgrouplist(record.pw_name, record.pw_gid))))
        return cls(
            username=record.pw_name,
            uid=record.pw_uid,
            gid=record.pw_gid,
            home=record.pw_dir,
            shell=record.pw_shell or "/bin/sh",
            groups=groups,
        )


@dataclass(frozen=True)
class AuthenticationOutcome:
    ok: bool
    account: Account | None = None
    message: str = ""


@dataclass(frozen=True)
class SessionOpenOutcome:
    ok: bool
    environment: dict[str, str] | None = None
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


class PamWorker(QObject):
    authentication_finished = Signal(object)
    session_open_finished = Signal(object)
    session_closed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.transaction: PamTransaction | None = None

    @Slot(str, str, str)
    def authenticate(self, username: str, password: str, service: str) -> None:
        self._close(suppress=True)
        try:
            self.transaction = PamTransaction.authenticate(username, password, service)
        except KeyError:
            outcome = AuthenticationOutcome(False, message="Authentication failed")
        except pamela.PAMError as exc:
            message = (
                "The account or password has expired"
                if exc.errno in EXPIRED_CODES
                else "Authentication failed"
            )
            outcome = AuthenticationOutcome(False, message=message)
        except Exception:
            LOG.exception("PAM authentication failed unexpectedly")
            outcome = AuthenticationOutcome(False, message="Authentication failed")
        else:
            outcome = AuthenticationOutcome(True, account=self.transaction.account)
        self.authentication_finished.emit(outcome)

    @Slot(str, str, str, str)
    def open_session(
        self,
        display: str,
        invoking_user: str,
        session_id: str,
        current_desktop: str,
    ) -> None:
        if self.transaction is None:
            self.session_open_finished.emit(
                SessionOpenOutcome(False, message="No authenticated PAM transaction")
            )
            return
        try:
            environment = self.transaction.open(
                display, invoking_user, session_id, current_desktop
            )
        except Exception:
            LOG.exception("Could not open PAM session")
            self._close(suppress=True)
            outcome = SessionOpenOutcome(
                False, message="Could not open the PAM session"
            )
        else:
            outcome = SessionOpenOutcome(True, environment=environment)
        self.session_open_finished.emit(outcome)

    @Slot()
    def close_session(self) -> None:
        self._close(suppress=True)
        self.session_closed.emit()

    def _close(self, suppress: bool) -> None:
        transaction, self.transaction = self.transaction, None
        if transaction is None:
            return
        try:
            transaction.close()
        except Exception:
            if not suppress:
                raise
            LOG.exception("PAM cleanup failed")


def select_pam_service(override: str | None) -> str:
    if override:
        return override
    if os.path.exists("/etc/pam.d/xnestdm"):
        return "xnestdm"
    return "login"
