from __future__ import annotations

import logging
import socket
import subprocess

from PySide6.QtCore import QObject, QSocketNotifier, Signal

from .auth import Account, AuthenticationOutcome, SessionStartOutcome
from .helper_protocol import (
    MAX_MESSAGE_SIZE,
    ProtocolError,
    decode_message,
    encode_message,
    message,
)
from .helper_transport import HelperBootstrap
from .xsessions import XSession

LOG = logging.getLogger(__name__)


class HelperClient(QObject):
    authentication_finished = Signal(int, object)
    session_start_finished = Signal(int, object)
    session_finished = Signal(int, str)
    failed = Signal(str)

    def __init__(self, bootstrap: HelperBootstrap) -> None:
        super().__init__()
        self.connection: socket.socket | None = bootstrap.socket
        self.process: subprocess.Popen[bytes] | None = bootstrap.process
        self.caller = bootstrap.caller
        self.buffer = b""
        self.next_request_id = 1
        self.pending: dict[int, tuple[str, int | None]] = {}
        self.closing = False
        self.notifier = QSocketNotifier(
            bootstrap.socket.fileno(), QSocketNotifier.Type.Read, self
        )
        self.notifier.activated.connect(self._read_messages)

    def authenticate(self, tab_id: int, username: str, password: str) -> None:
        self._request(
            "authenticate",
            tab_id,
            username=username,
            password=password,
        )
        password = ""

    def start_session(
        self,
        tab_id: int,
        display: str,
        selected_session: XSession,
    ) -> None:
        self._request(
            "start_session",
            tab_id,
            display=display,
            session={
                "name": selected_session.name,
                "session_id": selected_session.session_id,
                "current_desktop": selected_session.current_desktop,
                "command": list(selected_session.command),
                "user_fallback": selected_session.user_fallback,
            },
        )

    def stop_session(self, tab_id: int) -> None:
        self._request("stop_session", tab_id)

    def shutdown(self) -> None:
        if self.connection is None:
            return
        self.closing = True
        try:
            self._request("shutdown")
        except (OSError, ProtocolError):
            pass
        self._close()
        if self.process is not None:
            try:
                self.process.wait(timeout=2.5)
            except subprocess.TimeoutExpired:
                LOG.warning("Privileged helper did not exit promptly")
        self.process = None

    def _request(
        self, op: str, tab_id: int | None = None, **values: object
    ) -> None:
        if self.connection is None:
            raise RuntimeError("Privileged helper is not connected")
        if tab_id is not None:
            if isinstance(tab_id, bool) or tab_id < 1:
                raise ValueError("Invalid session tab id")
            values["tab_id"] = tab_id
        request_id = self.next_request_id
        self.next_request_id += 1
        self.pending[request_id] = (op, tab_id)
        try:
            self.connection.sendall(encode_message(message(op, request_id, **values)))
        except Exception:
            self.pending.pop(request_id, None)
            self._fail("Privileged helper is unavailable")

    def _read_messages(self) -> None:
        if self.connection is None:
            return
        try:
            chunk = self.connection.recv(MAX_MESSAGE_SIZE + 1)
            if not chunk:
                self._fail("Privileged helper exited unexpectedly")
                return
            self.buffer += chunk
            if len(self.buffer) > MAX_MESSAGE_SIZE and b"\n" not in self.buffer:
                raise ProtocolError("Privileged-helper response is too large")
            while b"\n" in self.buffer:
                payload, _, self.buffer = self.buffer.partition(b"\n")
                self._handle(decode_message(payload))
        except (OSError, ProtocolError, ValueError):
            LOG.exception("Invalid response from privileged helper")
            self._fail("Privileged helper communication failed")

    def _handle(self, response: dict[str, object]) -> None:
        event = response.get("event")
        if event == "session_finished":
            tab_id = response.get("tab_id")
            if (
                not isinstance(tab_id, int)
                or isinstance(tab_id, bool)
                or tab_id < 1
            ):
                raise ProtocolError("Invalid session tab id from privileged helper")
            diagnostics = response.get("diagnostics")
            if isinstance(diagnostics, str) and diagnostics:
                LOG.debug("Nested session diagnostics:\n%s", diagnostics)
            message_text = response.get("message")
            self.session_finished.emit(
                tab_id,
                message_text if isinstance(message_text, str) else ""
            )
            return
        request_id = response.get("id")
        if not isinstance(request_id, int) or request_id not in self.pending:
            raise ProtocolError("Unexpected privileged-helper response")
        op, tab_id = self.pending.pop(request_id)
        ok = response.get("ok") is True
        response_message = response.get("message")
        text = response_message if isinstance(response_message, str) else ""
        if op == "authenticate":
            if tab_id is None:
                raise ProtocolError("Missing session tab id")
            account_data = response.get("account")
            account = (
                Account.from_mapping(account_data)
                if ok and isinstance(account_data, dict)
                else None
            )
            self.authentication_finished.emit(
                tab_id,
                AuthenticationOutcome(ok and account is not None, account, text)
            )
        elif op == "start_session":
            if tab_id is None:
                raise ProtocolError("Missing session tab id")
            self.session_start_finished.emit(tab_id, SessionStartOutcome(ok, text))

    def _fail(self, message_text: str) -> None:
        if self.closing:
            return
        self._close()
        self.failed.emit(message_text)

    def _close(self) -> None:
        self.notifier.setEnabled(False)
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.pending.clear()
