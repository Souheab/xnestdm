from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QProcess, Signal

from xnestdm.clipboard import (
    ClipboardBridge,
    _ClipboardEndpoint,
    _decode_message,
    _encode_message,
    _helper_command,
)


class FakeMimeData:
    def __init__(self, text: str = "", *, has_text: bool = True) -> None:
        self._text = text
        self._has_text = has_text

    def hasText(self) -> bool:
        return self._has_text

    def text(self) -> str:
        return self._text


class FakeClipboard(QObject):
    dataChanged = Signal()

    def __init__(self, text: str | None = None) -> None:
        super().__init__()
        self._mime_data = FakeMimeData(text or "", has_text=text is not None)
        self.writes: list[str] = []

    def mimeData(self, _mode):  # type: ignore[no-untyped-def]
        return self._mime_data

    def setText(self, text: str, _mode) -> None:  # type: ignore[no-untyped-def]
        self.writes.append(text)
        self._mime_data = FakeMimeData(text)
        self.dataChanged.emit()

    def set_external_text(self, text: str) -> None:
        self._mime_data = FakeMimeData(text)
        self.dataChanged.emit()

    def set_external_non_text(self) -> None:
        self._mime_data = FakeMimeData(has_text=False)
        self.dataChanged.emit()


class FakeProcess(QObject):
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    errorOccurred = Signal(object)
    finished = Signal(int, object)

    def __init__(self) -> None:
        super().__init__()
        self.environment = None
        self.program = ""
        self.arguments: list[str] = []
        self.channel_mode = None
        self.running = False
        self.killed = False
        self.writes: list[bytes] = []
        self.stdout = b""
        self.stderr = b""

    def setProcessEnvironment(self, environment) -> None:  # type: ignore[no-untyped-def]
        self.environment = environment

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, arguments: list[str]) -> None:
        self.arguments = arguments

    def setProcessChannelMode(self, mode) -> None:  # type: ignore[no-untyped-def]
        self.channel_mode = mode

    def start(self) -> None:
        self.running = True

    def state(self):  # type: ignore[no-untyped-def]
        if self.running:
            return QProcess.ProcessState.Running
        return QProcess.ProcessState.NotRunning

    def kill(self) -> None:
        self.killed = True
        self.running = False

    def waitForFinished(self, _timeout: int) -> bool:
        return True

    def write(self, data: QByteArray) -> int:
        encoded = bytes(data)
        self.writes.append(encoded)
        return len(encoded)

    def readAllStandardOutput(self) -> QByteArray:
        data = self.stdout
        self.stdout = b""
        return QByteArray(data)

    def readAllStandardError(self) -> QByteArray:
        data = self.stderr
        self.stderr = b""
        return QByteArray(data)

    def errorString(self) -> str:
        return "fake process error"

    def push_stdout(self, data: bytes) -> None:
        self.stdout += data
        self.readyReadStandardOutput.emit()


def test_protocol_round_trips_unicode_multiline_and_empty_text() -> None:
    for text in ("hello", "first\nsecond", "héllø 世界", ""):
        encoded = _encode_message("text", text)
        assert encoded.endswith(b"\n")
        assert _decode_message(encoded.rstrip(b"\n")) == ("text", text)

    assert _decode_message(_encode_message("ready").rstrip(b"\n")) == (
        "ready",
        None,
    )


def test_endpoint_suppresses_echoes_and_ignores_non_text(qapp) -> None:
    clipboard = FakeClipboard("host")
    endpoint = _ClipboardEndpoint(clipboard)  # type: ignore[arg-type]
    changes: list[str] = []
    endpoint.text_changed.connect(changes.append)

    endpoint.apply_text("guest\n世界")
    assert clipboard.writes == ["guest\n世界"]
    assert changes == []

    clipboard.set_external_text("")
    clipboard.set_external_text("guest\n世界")
    clipboard.set_external_non_text()
    assert changes == ["", "guest\n世界"]


def test_helper_command_reuses_installed_xnestdm_launcher(
    monkeypatch, tmp_path: Path
) -> None:
    launcher = tmp_path / "xnestdm"
    launcher.touch()
    launcher.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(launcher)])

    assert _helper_command() == (str(launcher), ["--clipboard-helper"])
    assert os.access(launcher, os.X_OK)


def test_bridge_initializes_guest_and_syncs_both_directions(qapp, monkeypatch) -> None:
    monkeypatch.setenv("XAUTHORITY", "/tmp/outer-cookie")
    clipboard = FakeClipboard("initial host text")
    process = FakeProcess()
    bridge = ClipboardBridge(
        clipboard=clipboard,  # type: ignore[arg-type]
        process_factory=lambda _parent: process,  # type: ignore[arg-type,return-value]
    )

    bridge.start(":91")

    assert bridge.active
    assert process.environment.value("DISPLAY") == ":91"
    assert process.environment.value("QT_QPA_PLATFORM") == "xcb"
    assert not process.environment.contains("XAUTHORITY")
    assert process.program == sys.executable
    assert process.arguments == ["-m", "xnestdm.clipboard", "--guest"]
    assert process.writes == []

    process.push_stdout(_encode_message("ready"))
    assert _decode_message(process.writes.pop().rstrip(b"\n")) == (
        "text",
        "initial host text",
    )

    clipboard.set_external_text("new host text")
    assert _decode_message(process.writes.pop().rstrip(b"\n")) == (
        "text",
        "new host text",
    )

    process.push_stdout(_encode_message("text", "new guest text"))
    assert clipboard.writes == ["new guest text"]
    assert process.writes == []

    clipboard.set_external_non_text()
    assert process.writes == []

    bridge.stop()
    assert process.killed
    assert not bridge.active


def test_bridge_reports_malformed_protocol_without_affecting_owner(qapp) -> None:
    clipboard = FakeClipboard()
    process = FakeProcess()
    bridge = ClipboardBridge(
        clipboard=clipboard,  # type: ignore[arg-type]
        process_factory=lambda _parent: process,  # type: ignore[arg-type,return-value]
    )
    failures: list[str] = []
    bridge.failed.connect(failures.append)
    bridge.start(":92")

    process.push_stdout(b'{"type":"text","text":3}\n')

    assert failures == ["unsupported clipboard message"]
    assert not bridge.active
    assert process.killed


def test_bridge_reports_unexpected_sidecar_exit(qapp) -> None:
    clipboard = FakeClipboard()
    process = FakeProcess()
    bridge = ClipboardBridge(
        clipboard=clipboard,  # type: ignore[arg-type]
        process_factory=lambda _parent: process,  # type: ignore[arg-type,return-value]
    )
    failures: list[str] = []
    bridge.failed.connect(failures.append)
    bridge.start(":93")
    process.running = False

    process.finished.emit(7, QProcess.ExitStatus.CrashExit)

    assert failures == ["Clipboard helper exited with status 7"]
    assert not bridge.active
