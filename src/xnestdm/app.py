from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QResizeEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QComboBox,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .auth import (
    Account,
    AuthenticationOutcome,
    SessionStartOutcome,
)
from .clipboard import ClipboardBridge
from .helper_client import HelperClient
from .session import SessionController, invoking_account
from .xsessions import XSession, discover_xsessions, preferred_xsession_index

LOG = logging.getLogger(__name__)
OTHER_USERS_DISABLED = (
    "Log in as another user is disabled. Enable the NixOS module or start "
    "xnestdm with sudo to enable it."
)
VIEWPORT_PADDING = 12
CLIPBOARD_SHARING_SETTING = "clipboard/sharingEnabled"


class LoginPage(QWidget):
    submitted = Signal(str, str, object)
    current_user_requested = Signal(object)

    def __init__(
        self,
        current_username: str,
        allow_other_users: bool,
        sessions: list[XSession],
    ) -> None:
        super().__init__()
        self.allow_other_users = allow_other_users
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.session = QComboBox()
        for xsession in sessions:
            self.session.addItem(xsession.name, xsession)
        self.session.setCurrentIndex(preferred_xsession_index(sessions))
        self.login_button = QPushButton("Log In")
        self.current_user_button = QPushButton(f"Use Current User ({current_username})")
        self.other_user_label = QLabel("Log in as another user")
        self.status = QLabel()
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Session:", self.session)
        form.addRow("Username:", self.username)
        form.addRow("Password:", self.password)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.login_button)

        panel = QWidget()
        panel.setMaximumWidth(440)
        panel_layout = QVBoxLayout(panel)
        panel_layout.addWidget(QLabel("Start a nested X session"))
        panel_layout.addWidget(self.current_user_button)
        panel_layout.addWidget(self.other_user_label)
        panel_layout.addLayout(form)
        panel_layout.addWidget(self.status)
        panel_layout.addLayout(actions)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        centered = QHBoxLayout()
        centered.addStretch(1)
        centered.addWidget(panel)
        centered.addStretch(1)
        layout.addLayout(centered)
        layout.addStretch(1)

        self.login_button.clicked.connect(self._submit)
        self.current_user_button.clicked.connect(self._request_current_user)
        self.password.returnPressed.connect(self._submit)
        self.username.returnPressed.connect(self.password.setFocus)
        self.set_busy(False)

    def set_busy(self, busy: bool, message: str = "") -> None:
        authentication_enabled = not busy and self.allow_other_users
        self.username.setEnabled(authentication_enabled)
        self.password.setEnabled(authentication_enabled)
        self.login_button.setEnabled(authentication_enabled)
        self.current_user_button.setEnabled(not busy)
        self.session.setEnabled(not busy)
        self.status.setText(message)

    def set_allow_other_users(self, allowed: bool, message: str = "") -> None:
        self.allow_other_users = allowed
        self.set_busy(False, message)

    def clear_form(self) -> None:
        self.username.clear()
        self.password.clear()
        self.status.clear()
        if self.allow_other_users:
            self.username.setFocus()
        else:
            self.current_user_button.setFocus()

    def _submit(self) -> None:
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            self.status.setText("Enter a username and password")
            return
        self.submitted.emit(username, password, self.selected_session())

    def _request_current_user(self) -> None:
        self.current_user_requested.emit(self.selected_session())

    def selected_session(self) -> XSession:
        session = self.session.currentData()
        if not isinstance(session, XSession):
            raise RuntimeError("No X session is selected")
        return session


class ViewportHost(QWidget):
    resized = Signal(int, int)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        size = event.size()
        self.resized.emit(size.width(), size.height())


class DesktopPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.host = ViewportHost()
        self.host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.host.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            VIEWPORT_PADDING,
            VIEWPORT_PADDING,
            VIEWPORT_PADDING,
            VIEWPORT_PADDING,
        )
        layout.setSpacing(0)
        layout.addWidget(self.host)


class SessionTab(QWidget):
    title_changed = Signal(str)
    state_changed = Signal()

    def __init__(
        self,
        tab_id: int,
        current_account: Account,
        sessions: list[XSession],
        helper_client: HelperClient | None,
    ) -> None:
        super().__init__()
        self.tab_id = tab_id
        self.current_account = current_account
        self.helper_client = helper_client
        self.allow_other_users = helper_client is not None
        self.account: Account | None = None
        self.selected_session: XSession | None = None
        self.pending_session: XSession | None = None
        self.pam_session_required = False
        self.helper_transaction_pending = False
        self.remote_session_active = False
        self.pending_message = ""
        self.closing = False
        self.nested_display = ""
        self.session_is_ready = False

        self.login_page = LoginPage(
            current_account.username,
            self.allow_other_users,
            sessions,
        )
        if not self.allow_other_users:
            self.login_page.status.setText(OTHER_USERS_DISABLED)
        self.desktop_page = DesktopPage()
        self.pages = QStackedWidget()
        self.pages.addWidget(self.login_page)
        self.pages.addWidget(self.desktop_page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.pages)

        self.session_controller = SessionController()
        self.desktop_page.host.resized.connect(self.session_controller.resize_xephyr)
        self.session_controller.xephyr_ready.connect(self._on_xephyr_ready)
        self.session_controller.session_ready.connect(self._on_session_ready)
        self.session_controller.finished.connect(self._on_session_finished)
        self.login_page.submitted.connect(self._authenticate)
        self.login_page.current_user_requested.connect(self._use_current_user)

    @property
    def has_session_activity(self) -> bool:
        return (
            self.account is not None
            or self.pending_session is not None
            or self.helper_transaction_pending
            or self.session_controller.active
        )

    @property
    def toolbar_text(self) -> str:
        if self.account is None or self.selected_session is None:
            return ""
        return f"{self.account.username} logged in to: {self.selected_session.name}"

    def _authenticate(
        self, username: str, password: str, selected_session: XSession
    ) -> None:
        if self.account is not None or self.session_controller.active:
            return
        if not self.allow_other_users or self.helper_client is None:
            self.login_page.status.setText(OTHER_USERS_DISABLED)
            return
        self.login_page.set_busy(True, "Authenticating…")
        self.login_page.password.clear()
        self.pending_session = selected_session
        self.helper_transaction_pending = True
        self.state_changed.emit()
        self.helper_client.authenticate(self.tab_id, username, password)
        password = ""  # drop this reference as soon as Qt has queued the call

    def authentication_finished(self, outcome: AuthenticationOutcome) -> None:
        if self.closing:
            return
        if not outcome.ok or outcome.account is None:
            self.helper_transaction_pending = False
            self.pending_session = None
            self.login_page.set_busy(False, outcome.message or "Authentication failed")
            self.login_page.password.setFocus()
            self.state_changed.emit()
            return
        if self.pending_session is None:
            self.login_page.set_busy(False, "No X session is selected")
            self._request_helper_stop()
            return
        self._start_account(
            outcome.account,
            self.pending_session,
            pam_session_required=True,
        )

    def _use_current_user(self, selected_session: XSession) -> None:
        if self.account is not None or self.session_controller.active:
            return
        self.login_page.set_busy(True, f"Starting {selected_session.name}…")
        self._start_account(
            self.current_account,
            selected_session,
            pam_session_required=False,
        )

    def _start_account(
        self,
        account: Account,
        selected_session: XSession,
        pam_session_required: bool,
    ) -> None:
        self.nested_display = ""
        self.account = account
        self.selected_session = selected_session
        self.pending_session = None
        self.pam_session_required = pam_session_required
        self.session_is_ready = False
        self.title_changed.emit(self.toolbar_text)
        self.pages.setCurrentWidget(self.desktop_page)
        self.desktop_page.host.show()
        self.desktop_page.host.winId()
        self.state_changed.emit()
        try:
            self.session_controller.start_xephyr(self.desktop_page.host, self.account)
        except Exception as exc:
            LOG.exception("Could not begin Xephyr startup")
            self.pending_message = f"Could not start Xephyr: {exc}"
            if self.pam_session_required:
                self._request_helper_stop()
            else:
                self._reset_login(self.pending_message)

    def _on_xephyr_ready(self, display: str) -> None:
        if self.closing or self.account is None or self.selected_session is None:
            return
        self.nested_display = display
        self.state_changed.emit()
        if not self.pam_session_required:
            self.session_controller.start_user_session(
                self.account, {}, self.selected_session
            )
            return
        if self.helper_client is None:
            self.helper_transaction_pending = False
            self.session_controller.stop("Privileged helper is unavailable")
            return
        self.helper_client.start_session(
            self.tab_id,
            display,
            self.selected_session,
        )

    def session_start_finished(self, outcome: SessionStartOutcome) -> None:
        if self.closing:
            return
        if not outcome.ok:
            self.helper_transaction_pending = False
            self.session_controller.stop(
                outcome.message or "Could not start the nested session"
            )
            return
        self.remote_session_active = True
        try:
            self.session_controller.mark_remote_session_started()
        except Exception as exc:
            LOG.exception("Could not complete privileged session startup")
            self.pending_message = f"Could not start the nested session: {exc}"
            self._request_helper_stop()

    def _on_session_ready(self) -> None:
        if not self.closing:
            self.session_is_ready = True
            self.state_changed.emit()

    def request_end_session(self) -> None:
        self.session_is_ready = False
        self.nested_display = ""
        self.state_changed.emit()
        if self.pam_session_required:
            self._request_helper_stop()
        else:
            self.session_controller.request_end_session()

    def _on_session_finished(self, message: str) -> None:
        if self.closing:
            return
        self.nested_display = ""
        self.session_is_ready = False
        self.pending_message = message
        self.state_changed.emit()
        if self.pam_session_required and self.helper_transaction_pending:
            self._request_helper_stop()
        else:
            self._reset_login(message)

    def helper_session_finished(self, message: str) -> None:
        if self.closing:
            return
        self.helper_transaction_pending = False
        self.remote_session_active = False
        final_message = message or self.pending_message
        self.pending_message = final_message
        if self.session_controller.active:
            self.session_controller.stop(final_message)
        else:
            self._reset_login(final_message)

    def helper_failed(self, message: str) -> None:
        if self.closing:
            return
        privileged_activity = (
            self.pam_session_required or self.helper_transaction_pending
        )
        self.helper_client = None
        self.allow_other_users = False
        self.login_page.set_allow_other_users(
            False, "" if self.session_controller.active else message
        )
        if not privileged_activity:
            self.state_changed.emit()
            return
        self.helper_transaction_pending = False
        self.remote_session_active = False
        self.nested_display = ""
        self.session_is_ready = False
        self.state_changed.emit()
        if self.session_controller.active:
            self.session_controller.stop(message)
        else:
            self._reset_login(message)

    def _request_helper_stop(self) -> None:
        if self.helper_client is None:
            self.helper_transaction_pending = False
            self.remote_session_active = False
            if self.session_controller.active:
                self.session_controller.stop("Privileged helper is unavailable")
            else:
                self._reset_login("Privileged helper is unavailable")
            return
        self.helper_client.stop_session(self.tab_id)

    def _reset_login(self, message: str) -> None:
        if not message and not self.allow_other_users:
            message = OTHER_USERS_DISABLED
        self.nested_display = ""
        self.account = None
        self.selected_session = None
        self.pending_session = None
        self.pam_session_required = False
        self.helper_transaction_pending = False
        self.remote_session_active = False
        self.pending_message = ""
        self.session_is_ready = False
        self.pages.setCurrentWidget(self.login_page)
        self.login_page.clear_form()
        self.login_page.set_busy(False, message)
        self.title_changed.emit("New Session")
        self.state_changed.emit()

    def shutdown(self) -> None:
        if self.closing:
            return
        self.closing = True
        if (
            (self.pam_session_required or self.helper_transaction_pending)
            and self.helper_client is not None
        ):
            self.helper_client.stop_session(self.tab_id)
        self.session_controller.shutdown_blocking()


class MainWindow(QMainWindow):
    def __init__(
        self,
        helper_client: HelperClient | None = None,
        *,
        settings: QSettings | None = None,
        clipboard_bridge: ClipboardBridge | None = None,
    ):
        super().__init__()
        self.helper_client = helper_client
        self.settings = (
            settings if settings is not None else QSettings("xnestdm", "xnestdm")
        )
        self.current_account = invoking_account()
        self.sessions = discover_xsessions(home=self.current_account.home)
        self.closing = False
        self._next_tab_id = 1
        self._tabs_by_id: dict[int, SessionTab] = {}
        self._clipboard_display = ""

        self.setWindowTitle("xnestdm")
        self.resize(1200, 800)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(False)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_current_tab_changed)
        self.add_tab_button = QToolButton()
        self.add_tab_button.setText("+")
        self.add_tab_button.setToolTip("New session tab")
        self.add_tab_button.setAccessibleName("New session tab")
        self.add_tab_button.clicked.connect(self._add_tab)
        self.tabs.setCornerWidget(
            self.add_tab_button, Qt.Corner.TopRightCorner
        )
        self.setCentralWidget(self.tabs)

        self.toolbar = QToolBar("Session", self)
        self.toolbar.setMovable(False)
        self.user_label = QLabel()
        self.settings_button = QToolButton()
        self.settings_button.setToolTip("Settings")
        self.settings_button.setAccessibleName("Settings")
        settings_icon = QIcon.fromTheme("preferences-system")
        if settings_icon.isNull():
            self.settings_button.setText("⚙")
        else:
            self.settings_button.setIcon(settings_icon)
        self.settings_menu = QMenu(self.settings_button)
        self.clipboard_action = QAction("Share clipboard with guest", self)
        self.clipboard_action.setCheckable(True)
        self.clipboard_action.setChecked(
            self.settings.value(CLIPBOARD_SHARING_SETTING, False, type=bool)
        )
        self.settings_menu.addAction(self.clipboard_action)
        self.settings_button.setMenu(self.settings_menu)
        self.settings_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.logout_button = QPushButton("End Session")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(self.user_label)
        self.toolbar.addWidget(spacer)
        self.toolbar.addWidget(self.settings_button)
        self.toolbar.addWidget(self.logout_button)
        self.addToolBar(self.toolbar)
        self.toolbar.hide()
        self.logout_button.setEnabled(False)

        self.clipboard_bridge = (
            clipboard_bridge if clipboard_bridge is not None else ClipboardBridge(self)
        )
        self.clipboard_bridge.failed.connect(self._on_clipboard_bridge_failed)

        if self.helper_client is not None:
            self.helper_client.authentication_finished.connect(
                self._on_helper_authentication_finished
            )
            self.helper_client.session_start_finished.connect(
                self._on_helper_session_start_finished
            )
            self.helper_client.session_finished.connect(
                self._on_helper_session_finished
            )
            self.helper_client.failed.connect(self._on_helper_failed)

        self.clipboard_action.toggled.connect(self._set_clipboard_sharing)
        self.logout_button.clicked.connect(self._confirm_end_session)
        self._add_tab()

    @property
    def active_tab(self) -> SessionTab | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, SessionTab) else None

    # Compatibility accessors expose the currently selected session tab.
    @property
    def login_page(self) -> LoginPage:
        tab = self.active_tab
        if tab is None:
            raise RuntimeError("No session tab is selected")
        return tab.login_page

    @property
    def desktop_page(self) -> DesktopPage:
        tab = self.active_tab
        if tab is None:
            raise RuntimeError("No session tab is selected")
        return tab.desktop_page

    @property
    def pages(self) -> QStackedWidget:
        tab = self.active_tab
        if tab is None:
            raise RuntimeError("No session tab is selected")
        return tab.pages

    @property
    def session_controller(self) -> SessionController:
        tab = self.active_tab
        if tab is None:
            raise RuntimeError("No session tab is selected")
        return tab.session_controller

    @property
    def account(self) -> Account | None:
        return self.active_tab.account if self.active_tab is not None else None

    @property
    def pam_session_required(self) -> bool:
        return bool(self.active_tab and self.active_tab.pam_session_required)

    @property
    def helper_transaction_pending(self) -> bool:
        return bool(self.active_tab and self.active_tab.helper_transaction_pending)

    @property
    def remote_session_active(self) -> bool:
        return bool(self.active_tab and self.active_tab.remote_session_active)

    @property
    def nested_display(self) -> str:
        return self.active_tab.nested_display if self.active_tab is not None else ""

    @nested_display.setter
    def nested_display(self, value: str) -> None:
        if self.active_tab is not None:
            self.active_tab.nested_display = value

    def _add_tab(self) -> SessionTab:
        tab_id = self._next_tab_id
        self._next_tab_id += 1
        tab = SessionTab(
            tab_id,
            self.current_account,
            self.sessions,
            self.helper_client,
        )
        self._tabs_by_id[tab_id] = tab
        tab.title_changed.connect(
            lambda title, selected=tab: self._set_tab_title(selected, title)
        )
        tab.state_changed.connect(
            lambda selected=tab: self._on_tab_state_changed(selected)
        )
        index = self.tabs.addTab(tab, "New Session")
        self.tabs.setCurrentIndex(index)
        return tab

    def _set_tab_title(self, tab: SessionTab, title: str) -> None:
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.setTabText(index, title)

    def _on_tab_state_changed(self, tab: SessionTab) -> None:
        if not self.closing and tab is self.active_tab:
            self._sync_active_tab()

    def _on_current_tab_changed(self, _index: int) -> None:
        if not self.closing:
            self._sync_active_tab()

    def _sync_active_tab(self) -> None:
        tab = self.active_tab
        if tab is not None and tab.account is not None:
            self.user_label.setText(tab.toolbar_text)
            self.logout_button.setEnabled(tab.session_is_ready)
            self.toolbar.show()
        else:
            self.user_label.clear()
            self.logout_button.setEnabled(False)
            self.toolbar.hide()
        target = (
            tab.nested_display
            if tab is not None and self.clipboard_action.isChecked()
            else ""
        )
        self._set_clipboard_target(target)

    def _close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if not isinstance(widget, SessionTab):
            return
        if widget.has_session_activity:
            answer = QMessageBox.question(
                self,
                "Close Session Tab",
                "End this nested X session and close the tab?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if widget is self.active_tab:
            self._set_clipboard_target("")
        widget.shutdown()
        self._tabs_by_id.pop(widget.tab_id, None)
        self.tabs.removeTab(index)
        widget.deleteLater()
        if self.tabs.count() == 0:
            self._add_tab()
        else:
            self._sync_active_tab()

    def _authenticate(
        self, username: str, password: str, selected_session: XSession
    ) -> None:
        if self.active_tab is not None:
            self.active_tab._authenticate(username, password, selected_session)

    def _use_current_user(self, selected_session: XSession) -> None:
        if self.active_tab is not None:
            self.active_tab._use_current_user(selected_session)

    def _on_xephyr_ready(self, display: str) -> None:
        if self.active_tab is not None:
            self.active_tab._on_xephyr_ready(display)

    def _on_session_start_finished(self, outcome: SessionStartOutcome) -> None:
        if self.active_tab is not None:
            self.active_tab.session_start_finished(outcome)

    def _on_helper_authentication_finished(
        self, tab_id: int, outcome: AuthenticationOutcome
    ) -> None:
        tab = self._tabs_by_id.get(tab_id)
        if tab is not None:
            tab.authentication_finished(outcome)
        elif outcome.ok and self.helper_client is not None:
            self.helper_client.stop_session(tab_id)

    def _on_helper_session_start_finished(
        self, tab_id: int, outcome: SessionStartOutcome
    ) -> None:
        tab = self._tabs_by_id.get(tab_id)
        if tab is not None:
            tab.session_start_finished(outcome)
        elif outcome.ok and self.helper_client is not None:
            self.helper_client.stop_session(tab_id)

    def _on_helper_session_finished(self, tab_id: int, message: str) -> None:
        tab = self._tabs_by_id.get(tab_id)
        if tab is not None:
            tab.helper_session_finished(message)

    def _on_helper_failed(self, message: str) -> None:
        if self.closing:
            return
        for tab in tuple(self._tabs_by_id.values()):
            tab.helper_failed(message)
        self.helper_client = None
        self._sync_active_tab()

    def _confirm_end_session(self) -> None:
        tab = self.active_tab
        if tab is None or not tab.has_session_activity:
            return
        answer = QMessageBox.question(
            self,
            "End Session",
            "End the nested X session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            tab.request_end_session()

    def _set_clipboard_sharing(self, enabled: bool) -> None:
        self.settings.setValue(CLIPBOARD_SHARING_SETTING, enabled)
        self.settings.sync()
        self._sync_active_tab()

    def _set_clipboard_target(self, display: str) -> None:
        if display == self._clipboard_display:
            return
        self.clipboard_bridge.stop()
        self._clipboard_display = ""
        if not display:
            return
        try:
            self.clipboard_bridge.start(display)
            self._clipboard_display = display
        except Exception as exc:
            LOG.exception("Could not start clipboard sharing")
            self._on_clipboard_bridge_failed(str(exc))

    def _on_clipboard_bridge_failed(self, message: str) -> None:
        if self.closing:
            return
        self.clipboard_bridge.stop()
        self._clipboard_display = ""
        self.clipboard_action.setChecked(False)
        QMessageBox.warning(
            self,
            "Clipboard Sharing Unavailable",
            message or "Clipboard sharing stopped unexpectedly.",
        )

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.closing = True
        self._set_clipboard_target("")
        for tab in tuple(self._tabs_by_id.values()):
            tab.shutdown()
        if self.helper_client is not None:
            self.helper_client.shutdown()
        event.accept()
