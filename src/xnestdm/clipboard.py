from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QCoreApplication,
    QObject,
    QProcess,
    QProcessEnvironment,
    QSocketNotifier,
    Signal,
)
from PySide6.QtGui import QClipboard, QGuiApplication

LOG = logging.getLogger(__name__)


def _encode_message(message_type: str, text: str | None = None) -> bytes:
    payload: dict[str, object] = {"type": message_type}
    if text is not None:
        payload["text"] = text
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _decode_message(line: bytes) -> tuple[str, str | None]:
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid clipboard message") from exc
    if not isinstance(payload, dict):
        raise ValueError("clipboard message must be an object")
    message_type = payload.get("type")
    if message_type == "ready" and set(payload) == {"type"}:
        return "ready", None
    if (
        message_type == "text"
        and set(payload) == {"type", "text"}
        and isinstance(payload.get("text"), str)
    ):
        return "text", payload["text"]
    raise ValueError("unsupported clipboard message")


class _ClipboardEndpoint(QObject):
    text_changed = Signal(str)

    def __init__(self, clipboard: QClipboard, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._clipboard = clipboard
        self._ignored_text: str | None = None
        self._clipboard.dataChanged.connect(self._on_data_changed)

    def current_text(self) -> str | None:
        mime_data = self._clipboard.mimeData(QClipboard.Mode.Clipboard)
        if mime_data is None or not mime_data.hasText():
            return None
        return mime_data.text()

    def apply_text(self, text: str) -> None:
        if self.current_text() == text:
            return
        self._ignored_text = text
        self._clipboard.setText(text, QClipboard.Mode.Clipboard)

    def _on_data_changed(self) -> None:
        text = self.current_text()
        if self._ignored_text is not None:
            if text == self._ignored_text:
                self._ignored_text = None
                return
            self._ignored_text = None
        if text is not None:
            self.text_changed.emit(text)


ProcessFactory = Callable[[QObject], QProcess]


def _helper_command() -> tuple[str, list[str]]:
    launcher = Path(sys.argv[0])
    if (
        launcher.name == "xnestdm"
        and launcher.is_file()
        and os.access(launcher, os.X_OK)
    ):
        return str(launcher), ["--clipboard-helper"]
    return sys.executable, ["-m", "xnestdm.clipboard", "--guest"]


class ClipboardBridge(QObject):
    failed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        clipboard: QClipboard | None = None,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        super().__init__(parent)
        selected_clipboard = clipboard or QGuiApplication.clipboard()
        if selected_clipboard is None:
            raise RuntimeError("The host clipboard is unavailable")
        self._endpoint = _ClipboardEndpoint(selected_clipboard, self)
        self._endpoint.text_changed.connect(self._send_text)
        self._process_factory = process_factory or QProcess
        self._process: QProcess | None = None
        self._stdout_buffer = b""
        self._ready = False

    @property
    def active(self) -> bool:
        return self._process is not None

    def start(self, display: str) -> None:
        self.stop()
        process = self._process_factory(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("DISPLAY", display)
        environment.insert("QT_QPA_PLATFORM", "xcb")
        environment.remove("XAUTHORITY")
        process.setProcessEnvironment(environment)
        program, arguments = _helper_command()
        process.setProgram(program)
        process.setArguments(arguments)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(
            lambda selected=process: self._read_output(selected)
        )
        process.readyReadStandardError.connect(
            lambda selected=process: self._log_stderr(selected)
        )
        process.errorOccurred.connect(
            lambda _error, selected=process: self._process_failed(selected)
        )
        process.finished.connect(
            lambda code, _status, selected=process: self._process_finished(
                selected, code
            )
        )
        self._process = process
        self._stdout_buffer = b""
        self._ready = False
        process.start()

    def stop(self) -> None:
        process = self._process
        self._process = None
        self._stdout_buffer = b""
        self._ready = False
        if process is None:
            return
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            process.waitForFinished(1000)
        process.deleteLater()

    def _send_text(self, text: str) -> None:
        if not self._ready or self._process is None:
            return
        if self._process.write(QByteArray(_encode_message("text", text))) < 0:
            self._fail(self._process, "Could not send clipboard data to the guest")

    def _read_output(self, process: QProcess) -> None:
        if process is not self._process:
            return
        self._stdout_buffer += bytes(process.readAllStandardOutput())
        while b"\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split(b"\n", 1)
            if not line:
                continue
            try:
                message_type, text = _decode_message(line)
            except ValueError as exc:
                self._fail(process, str(exc))
                return
            if message_type == "ready":
                if self._ready:
                    self._fail(process, "The clipboard helper initialized twice")
                    return
                self._ready = True
                current_text = self._endpoint.current_text()
                if current_text is not None:
                    self._send_text(current_text)
            elif not self._ready:
                self._fail(
                    process, "The clipboard helper sent data before initializing"
                )
                return
            elif text is not None:
                self._endpoint.apply_text(text)

    def _log_stderr(self, process: QProcess) -> None:
        if process is not self._process:
            return
        message = (
            bytes(process.readAllStandardError())
            .decode("utf-8", errors="replace")
            .strip()
        )
        if message:
            LOG.warning("Clipboard helper: %s", message)

    def _process_failed(self, process: QProcess) -> None:
        if process is self._process:
            self._fail(process, process.errorString() or "Clipboard helper failed")

    def _process_finished(self, process: QProcess, code: int) -> None:
        if process is self._process:
            self._fail(process, f"Clipboard helper exited with status {code}")

    def _fail(self, process: QProcess, message: str) -> None:
        if process is not self._process:
            return
        detail = (
            bytes(process.readAllStandardError())
            .decode("utf-8", errors="replace")
            .strip()
        )
        self._process = None
        self._stdout_buffer = b""
        self._ready = False
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
        process.deleteLater()
        if detail:
            LOG.warning("Clipboard helper: %s", detail)
        self.failed.emit(message)


class _GuestClipboardAgent(QObject):
    def __init__(self, clipboard: QClipboard, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._endpoint = _ClipboardEndpoint(clipboard, self)
        self._endpoint.text_changed.connect(self._send_text)
        self._input_fd = sys.stdin.buffer.fileno()
        os.set_blocking(self._input_fd, False)
        self._input_buffer = b""
        self._notifier = QSocketNotifier(
            self._input_fd, QSocketNotifier.Type.Read, self
        )
        self._notifier.activated.connect(self._read_input)
        self._write(_encode_message("ready"))

    def _read_input(self, *_args: object) -> None:
        while True:
            try:
                chunk = os.read(self._input_fd, 65536)
            except BlockingIOError:
                break
            except OSError as exc:
                self._abort(f"Could not read clipboard data: {exc}")
                return
            if not chunk:
                QCoreApplication.quit()
                return
            self._input_buffer += chunk
        while b"\n" in self._input_buffer:
            line, self._input_buffer = self._input_buffer.split(b"\n", 1)
            if not line:
                continue
            try:
                message_type, text = _decode_message(line)
            except ValueError as exc:
                self._abort(str(exc))
                return
            if message_type != "text" or text is None:
                self._abort("The host sent an unsupported clipboard message")
                return
            self._endpoint.apply_text(text)

    def _send_text(self, text: str) -> None:
        self._write(_encode_message("text", text))

    @staticmethod
    def _write(data: bytes) -> None:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            QCoreApplication.quit()

    @staticmethod
    def _abort(message: str) -> None:
        print(message, file=sys.stderr, flush=True)
        QCoreApplication.exit(2)


def _guest_main() -> int:
    application = QGuiApplication(sys.argv[:1])
    clipboard = application.clipboard()
    if clipboard is None:
        print("The guest clipboard is unavailable", file=sys.stderr)
        return 2
    agent = _GuestClipboardAgent(clipboard)
    application.setProperty("xnestdmClipboardAgent", agent)
    return application.exec()


if __name__ == "__main__":
    if sys.argv[1:] != ["--guest"]:
        print("This module is an internal xnestdm clipboard helper", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(_guest_main())
