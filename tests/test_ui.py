from __future__ import annotations

from pathlib import Path

import pytest

from PySide6.QtCore import QObject, QRect, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QMessageBox

from xnestdm.app import (
    CLIPBOARD_SHARING_SETTING,
    DesktopPage,
    LoginPage,
    MainWindow,
)
from xnestdm.auth import Account, AuthenticationOutcome, SessionStartOutcome
from xnestdm.xsessions import XSession


SESSION = XSession(
    "test-session",
    "Test Session",
    ("/bin/true",),
    ("Test",),
    Path("/host/test-session.desktop"),
)


class FakeHelper(QObject):
    authentication_finished = Signal(object)
    session_start_finished = Signal(object)
    session_finished = Signal(str)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.auth_requests = []
        self.session_requests = []
        self.stop_requests = 0
        self.shutdown_requests = 0

    def authenticate(self, username, password) -> None:
        self.auth_requests.append((username, password))

    def start_session(self, display, session) -> None:
        self.session_requests.append((display, session))

    def stop_session(self) -> None:
        self.stop_requests += 1

    def shutdown(self) -> None:
        self.shutdown_requests += 1


class FakeClipboardBridge(QObject):
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[str] = []
        self.stops = 0

    def start(self, display: str) -> None:
        self.starts.append(display)

    def stop(self) -> None:
        self.stops += 1


@pytest.fixture
def settings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


def test_login_form_uses_password_echo_and_emits_credentials(qapp) -> None:
    page = LoginPage("current", allow_other_users=True, sessions=[SESSION])
    submitted: list[tuple[str, str, XSession]] = []
    page.submitted.connect(
        lambda username, password, session: submitted.append(
            (username, password, session)
        )
    )
    page.username.setText("alice")
    page.password.setText("secret")

    page.login_button.click()

    assert page.password.echoMode() == page.password.EchoMode.Password
    assert submitted == [("alice", "secret", SESSION)]


def test_busy_state_and_clear_form(qapp) -> None:
    page = LoginPage("current", allow_other_users=True, sessions=[SESSION])
    page.username.setText("alice")
    page.password.setText("secret")
    page.set_busy(True, "Authenticating…")
    assert not page.login_button.isEnabled()
    assert not page.session.isEnabled()
    assert page.status.text() == "Authenticating…"

    page.clear_form()
    page.set_busy(False)
    assert page.username.text() == ""
    assert page.password.text() == ""
    assert page.login_button.isEnabled()
    assert page.session.isEnabled()


def test_desktop_host_is_padded_and_tracks_page_size(qapp) -> None:
    page = DesktopPage()
    resized: list[tuple[int, int]] = []
    page.host.resized.connect(lambda width, height: resized.append((width, height)))
    assert page.host.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    assert page.host.focusPolicy() == Qt.FocusPolicy.StrongFocus
    margins = page.layout().contentsMargins()
    assert (
        margins.left(),
        margins.top(),
        margins.right(),
        margins.bottom(),
    ) == (12, 12, 12, 12)

    page.resize(900, 700)
    page.layout().setGeometry(page.rect())
    assert page.host.geometry() == QRect(12, 12, 876, 676)

    page.resize(640, 480)
    page.layout().setGeometry(page.rect())
    assert page.host.geometry() == QRect(12, 12, 616, 456)

    qapp.sendEvent(
        page.host,
        QResizeEvent(QSize(616, 456), QSize(876, 676)),
    )
    assert resized[-1] == (616, 456)


def test_main_window_clears_password_when_auth_is_dispatched(qapp, settings) -> None:
    helper = FakeHelper()
    window = MainWindow(helper, settings=settings)  # type: ignore[arg-type]
    window.login_page.password.setText("secret")

    window._authenticate("alice", "secret", SESSION)

    assert window.login_page.password.text() == ""
    assert not window.login_page.login_button.isEnabled()
    assert helper.auth_requests == [("alice", "secret")]
    assert window.toolbar.isHidden()
    window.close()


def test_unprivileged_login_page_only_allows_current_user(qapp) -> None:
    page = LoginPage("alice", allow_other_users=False, sessions=[SESSION])
    current_user_requests: list[XSession] = []
    page.current_user_requested.connect(current_user_requests.append)

    assert not page.username.isEnabled()
    assert not page.password.isEnabled()
    assert not page.login_button.isEnabled()
    assert page.current_user_button.isEnabled()
    assert "alice" in page.current_user_button.text()

    page.current_user_button.click()
    assert current_user_requests == [SESSION]


def test_current_user_path_skips_pam(qapp, monkeypatch, settings) -> None:
    window = MainWindow(settings=settings)
    starts = []
    monkeypatch.setattr(
        window.session_controller,
        "start_xephyr",
        lambda host, account: starts.append(account),
    )

    window._use_current_user(SESSION)

    assert starts == [window.current_account]
    assert window.account == window.current_account
    assert window.pam_session_required is False
    assert window.pages.currentWidget() is window.desktop_page

    sessions = []
    monkeypatch.setattr(
        window.session_controller,
        "start_user_session",
        lambda account, environment, session: sessions.append(
            (account, environment, session)
        ),
    )
    window._on_xephyr_ready(":7")

    assert sessions == [(window.current_account, {}, SESSION)]
    window.close()


def test_alternate_user_routes_session_through_helper(
    qapp, monkeypatch, settings
) -> None:
    helper = FakeHelper()
    window = MainWindow(helper, settings=settings)  # type: ignore[arg-type]
    account = Account("alice", 1001, 1001, "/home/alice", "/bin/sh", (1001,))
    starts = []
    monkeypatch.setattr(
        window.session_controller,
        "start_xephyr",
        lambda host, selected_account: starts.append(selected_account),
    )

    window._authenticate("alice", "secret", SESSION)
    helper.authentication_finished.emit(AuthenticationOutcome(True, account))

    assert starts == [account]
    assert window.helper_transaction_pending

    window._on_xephyr_ready(":9")
    assert helper.session_requests == [(":9", SESSION)]

    monkeypatch.setattr(
        window.session_controller, "mark_remote_session_started", lambda: None
    )
    helper.session_start_finished.emit(SessionStartOutcome(True))
    assert window.remote_session_active
    window.close()


def test_settings_cog_controls_clipboard_bridge_lifecycle(
    qapp, monkeypatch, settings
) -> None:
    bridge = FakeClipboardBridge()
    window = MainWindow(
        settings=settings,
        clipboard_bridge=bridge,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(window.session_controller, "start_xephyr", lambda *_: None)
    monkeypatch.setattr(
        window.session_controller, "start_user_session", lambda *_: None
    )

    assert window.settings_button.accessibleName() == "Settings"
    assert window.settings_button.toolTip() == "Settings"
    assert window.clipboard_action.text() == "Share clipboard with guest"
    assert window.clipboard_action.isCheckable()
    assert window.settings_menu.actions() == [window.clipboard_action]
    assert not window.clipboard_action.isChecked()

    window.clipboard_action.setChecked(True)
    assert settings.value(CLIPBOARD_SHARING_SETTING, type=bool) is True
    assert bridge.starts == []

    window._use_current_user(SESSION)
    assert not window.toolbar.isHidden()
    window._on_xephyr_ready(":17")
    assert bridge.starts == [":17"]

    window.clipboard_action.setChecked(False)
    assert settings.value(CLIPBOARD_SHARING_SETTING, type=bool) is False
    assert bridge.stops >= 1

    window.clipboard_action.setChecked(True)
    assert bridge.starts == [":17", ":17"]
    window.close()


def test_clipboard_choice_is_remembered(qapp, settings) -> None:
    first_bridge = FakeClipboardBridge()
    first = MainWindow(
        settings=settings,
        clipboard_bridge=first_bridge,  # type: ignore[arg-type]
    )
    first.clipboard_action.setChecked(True)
    first.close()

    restored_settings = QSettings(settings.fileName(), QSettings.Format.IniFormat)
    second = MainWindow(
        settings=restored_settings,
        clipboard_bridge=FakeClipboardBridge(),  # type: ignore[arg-type]
    )
    assert second.clipboard_action.isChecked()
    second.close()


def test_clipboard_failure_disables_only_sharing(qapp, monkeypatch, settings) -> None:
    bridge = FakeClipboardBridge()
    window = MainWindow(
        settings=settings,
        clipboard_bridge=bridge,  # type: ignore[arg-type]
    )
    stopped_sessions: list[str] = []
    warnings: list[str] = []
    monkeypatch.setattr(window.session_controller, "stop", stopped_sessions.append)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    window.clipboard_action.setChecked(True)
    window.nested_display = ":18"

    bridge.failed.emit("The clipboard helper crashed")

    assert not window.clipboard_action.isChecked()
    assert settings.value(CLIPBOARD_SHARING_SETTING, type=bool) is False
    assert warnings == ["The clipboard helper crashed"]
    assert stopped_sessions == []
    window.close()
