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
    authentication_finished = Signal(object)
    session_start_finished = Signal(object)
    session_finished = Signal(str)
    failed = Signal(str)

    def __init__(self, bootstrap: HelperBootstrap) -> None:
        super().__init__()
        self.connection: socket.socket | None = bootstrap.socket
        self.process: subprocess.Popen[bytes] | None = bootstrap.process
        self.caller = bootstrap.caller
        self.buffer = b""
        self.next_request_id = 1
        self.pending: dict[int, str] = {}
        self.closing = False
        self.notifier = QSocketNotifier(
            bootstrap.socket.fileno(), QSocketNotifier.Type.Read, self
        )
        self.notifier.activated.connect(self._read_messages)

    def authenticate(self, username: str, password: str) -> None:
        self._request("authenticate", username=username, password=password)
        password = ""

    def start_session(
        self,
        display: str,
        selected_session: XSession,
    ) -> None:
        self._request(
            "start_session",
            display=display,
            session={
                "name": selected_session.name,
                "session_id": selected_session.session_id,
                "current_desktop": selected_session.current_desktop,
                "command": list(selected_session.command),
                "user_fallback": selected_session.user_fallback,
            },
        )

    def stop_session(self) -> None:
        self._request("stop_session")

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

    def _request(self, op: str, **values: object) -> None:
        if self.connection is None:
            raise RuntimeError("Privileged helper is not connected")
        request_id = self.next_request_id
        self.next_request_id += 1
        self.pending[request_id] = op
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
            diagnostics = response.get("diagnostics")
            if isinstance(diagnostics, str) and diagnostics:
                LOG.debug("Nested session diagnostics:\n%s", diagnostics)
            message_text = response.get("message")
            self.session_finished.emit(
                message_text if isinstance(message_text, str) else ""
            )
            return
        request_id = response.get("id")
        if not isinstance(request_id, int) or request_id not in self.pending:
            raise ProtocolError("Unexpected privileged-helper response")
        op = self.pending.pop(request_id)
        ok = response.get("ok") is True
        response_message = response.get("message")
        text = response_message if isinstance(response_message, str) else ""
        if op == "authenticate":
            account_data = response.get("account")
            account = (
                Account.from_mapping(account_data)
                if ok and isinstance(account_data, dict)
                else None
            )
            self.authentication_finished.emit(
                AuthenticationOutcome(ok and account is not None, account, text)
            )
        elif op == "start_session":
            self.session_start_finished.emit(SessionStartOutcome(ok, text))

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
