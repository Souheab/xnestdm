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
    authentication_finished = Signal(int, object)
    session_start_finished = Signal(int, object)
    session_finished = Signal(int, str)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.auth_requests = []
        self.session_requests = []
        self.stop_requests: list[int] = []
        self.shutdown_requests = 0

    def authenticate(self, tab_id, username, password) -> None:
        self.auth_requests.append((tab_id, username, password))

    def start_session(self, tab_id, display, session) -> None:
        self.session_requests.append((tab_id, display, session))

    def stop_session(self, tab_id) -> None:
        self.stop_requests.append(tab_id)

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

    assert window.active_tab is not None
    assert window.login_page.password.text() == ""
    assert not window.login_page.login_button.isEnabled()
    assert helper.auth_requests == [(window.active_tab.tab_id, "alice", "secret")]
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
    assert window.active_tab is not None
    tab_id = window.active_tab.tab_id
    helper.authentication_finished.emit(
        tab_id, AuthenticationOutcome(True, account)
    )

    assert starts == [account]
    assert window.helper_transaction_pending

    window._on_xephyr_ready(":9")
    assert helper.session_requests == [(tab_id, ":9", SESSION)]

    monkeypatch.setattr(
        window.session_controller, "mark_remote_session_started", lambda: None
    )
    helper.session_start_finished.emit(tab_id, SessionStartOutcome(True))
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


def test_window_creates_independent_session_tabs(qapp, settings) -> None:
    window = MainWindow(settings=settings)
    first = window.active_tab
    assert first is not None
    first.login_page.username.setText("first")

    window.add_tab_button.click()
    second = window.active_tab

    assert second is not None
    assert second is not first
    assert second.tab_id > first.tab_id
    assert window.tabs.count() == 2
    assert window.tabs.tabText(0) == "New Session"
    assert first.login_page.username.text() == "first"
    assert second.login_page.username.text() == ""
    window.close()


def test_current_user_sessions_run_independently_across_tabs(
    qapp, monkeypatch, settings
) -> None:
    window = MainWindow(settings=settings)
    first = window.active_tab
    assert first is not None
    first_starts = []
    monkeypatch.setattr(
        first.session_controller,
        "start_xephyr",
        lambda host, account: first_starts.append(account),
    )
    first._use_current_user(SESSION)

    second = window._add_tab()
    second_starts = []
    monkeypatch.setattr(
        second.session_controller,
        "start_xephyr",
        lambda host, account: second_starts.append(account),
    )
    second._use_current_user(SESSION)

    assert first_starts == [window.current_account]
    assert second_starts == [window.current_account]
    assert first.account == window.current_account
    assert second.account == window.current_account
    assert window.tabs.tabText(window.tabs.indexOf(first)).endswith("Test Session")
    assert window.tabs.tabText(window.tabs.indexOf(second)).endswith("Test Session")
    window.close()


def test_clipboard_sharing_follows_only_the_active_tab(
    qapp, monkeypatch, settings
) -> None:
    bridge = FakeClipboardBridge()
    window = MainWindow(
        settings=settings,
        clipboard_bridge=bridge,  # type: ignore[arg-type]
    )
    first = window.active_tab
    assert first is not None
    monkeypatch.setattr(first.session_controller, "start_xephyr", lambda *_: None)
    monkeypatch.setattr(first.session_controller, "start_user_session", lambda *_: None)
    first._use_current_user(SESSION)
    first._on_xephyr_ready(":21")
    window.clipboard_action.setChecked(True)

    second = window._add_tab()
    monkeypatch.setattr(second.session_controller, "start_xephyr", lambda *_: None)
    monkeypatch.setattr(
        second.session_controller, "start_user_session", lambda *_: None
    )
    second._use_current_user(SESSION)
    second._on_xephyr_ready(":22")
    window.tabs.setCurrentWidget(first)

    assert bridge.starts == [":21", ":22", ":21"]
    assert window._clipboard_display == ":21"
    window.close()


def test_closing_last_running_tab_confirms_and_leaves_fresh_tab(
    qapp, monkeypatch, settings
) -> None:
    window = MainWindow(settings=settings)
    tab = window.active_tab
    assert tab is not None
    monkeypatch.setattr(tab.session_controller, "start_xephyr", lambda *_: None)
    tab._use_current_user(SESSION)
    shutdowns = []
    monkeypatch.setattr(tab, "shutdown", lambda: shutdowns.append(tab.tab_id))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    window._close_tab(0)

    assert shutdowns == [tab.tab_id]
    assert window.tabs.count() == 1
    assert window.active_tab is not tab
    assert window.tabs.tabText(0) == "New Session"
    window.close()


def test_closing_tab_during_authentication_requests_helper_cleanup(
    qapp, monkeypatch, settings
) -> None:
    helper = FakeHelper()
    window = MainWindow(helper, settings=settings)  # type: ignore[arg-type]
    tab = window.active_tab
    assert tab is not None
    tab._authenticate("alice", "secret", SESSION)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    window._close_tab(0)

    assert helper.stop_requests == [tab.tab_id]
    assert window.tabs.count() == 1
    assert window.active_tab is not tab
    window.close()


def test_helper_failure_stops_privileged_tabs_but_keeps_current_user_tabs(
    qapp, monkeypatch, settings
) -> None:
    helper = FakeHelper()
    window = MainWindow(helper, settings=settings)  # type: ignore[arg-type]
    current = window.active_tab
    assert current is not None
    monkeypatch.setattr(current.session_controller, "start_xephyr", lambda *_: None)
    current._use_current_user(SESSION)
    current.session_controller._state = "running"
    current_stops = []
    monkeypatch.setattr(current.session_controller, "stop", current_stops.append)
    monkeypatch.setattr(current.session_controller, "shutdown_blocking", lambda: None)

    privileged = window._add_tab()
    monkeypatch.setattr(
        privileged.session_controller, "start_xephyr", lambda *_: None
    )
    privileged._authenticate("alice", "secret", SESSION)
    account = Account("alice", 1001, 1001, "/home/alice", "/bin/sh", (1001,))
    helper.authentication_finished.emit(
        privileged.tab_id, AuthenticationOutcome(True, account)
    )
    privileged.session_controller._state = "running"
    privileged_stops = []
    monkeypatch.setattr(
        privileged.session_controller, "stop", privileged_stops.append
    )
    monkeypatch.setattr(
        privileged.session_controller, "shutdown_blocking", lambda: None
    )

    helper.failed.emit("Helper failed")

    assert current_stops == []
    assert privileged_stops == ["Helper failed"]
    assert not current.login_page.allow_other_users
    assert not privileged.login_page.allow_other_users
    window.close()
