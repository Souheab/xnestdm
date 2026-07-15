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
        self.allow_other_users = helper_client is not None
        self.current_account = invoking_account()
        self.account: Account | None = None
        self.selected_session: XSession | None = None
        self.pending_session: XSession | None = None
        self.pam_session_required = False
        self.helper_transaction_pending = False
        self.remote_session_active = False
        self.pending_message = ""
        self.closing = False
        self.nested_display = ""

        self.setWindowTitle("xnestdm")
        self.resize(1200, 800)

        self.login_page = LoginPage(
            self.current_account.username,
            self.allow_other_users,
            discover_xsessions(home=self.current_account.home),
        )
        if not self.allow_other_users:
            self.login_page.status.setText(OTHER_USERS_DISABLED)
        self.desktop_page = DesktopPage()
        self.pages = QStackedWidget()
        self.pages.addWidget(self.login_page)
        self.pages.addWidget(self.desktop_page)
        self.setCentralWidget(self.pages)

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

        self.session_controller = SessionController()
        self.desktop_page.host.resized.connect(self.session_controller.resize_xephyr)
        self.session_controller.xephyr_ready.connect(self._on_xephyr_ready)
        self.session_controller.session_ready.connect(self._on_session_ready)
        self.session_controller.finished.connect(self._on_session_finished)

        if self.helper_client is not None:
            self.helper_client.authentication_finished.connect(
                self._on_authentication_finished
            )
            self.helper_client.session_start_finished.connect(
                self._on_session_start_finished
            )
            self.helper_client.session_finished.connect(
                self._on_helper_session_finished
            )
            self.helper_client.failed.connect(self._on_helper_failed)

        self.login_page.submitted.connect(self._authenticate)
        self.login_page.current_user_requested.connect(self._use_current_user)
        self.clipboard_action.toggled.connect(self._set_clipboard_sharing)
        self.logout_button.clicked.connect(self._confirm_end_session)

    def _authenticate(
        self, username: str, password: str, selected_session: XSession
    ) -> None:
        if self.account is not None or self.session_controller.active:
            return
        if not self.allow_other_users:
            self.login_page.status.setText(OTHER_USERS_DISABLED)
            return
        self.login_page.set_busy(True, "Authenticating…")
        self.login_page.password.clear()
        self.pending_session = selected_session
        if self.helper_client is None:
            self.login_page.set_busy(False, OTHER_USERS_DISABLED)
            return
        self.helper_client.authenticate(username, password)
        password = ""  # drop this reference as soon as Qt has queued the call

    def _on_authentication_finished(self, outcome: AuthenticationOutcome) -> None:
        if self.closing:
            return
        if not outcome.ok or outcome.account is None:
            self.helper_transaction_pending = False
            self.pending_session = None
            self.login_page.set_busy(False, outcome.message or "Authentication failed")
            self.login_page.password.setFocus()
            return
        self.helper_transaction_pending = True
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
        self.clipboard_bridge.stop()
        self.nested_display = ""
        self.account = account
        self.selected_session = selected_session
        self.pending_session = None
        self.pam_session_required = pam_session_required
        self.user_label.setText(
            f"{self.account.username} — {self.selected_session.name}"
        )
        self.logout_button.setEnabled(False)
        self.toolbar.show()
        self.pages.setCurrentWidget(self.desktop_page)
        self.desktop_page.host.show()
        self.desktop_page.host.winId()
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
        if self.clipboard_action.isChecked():
            self._start_clipboard_bridge()
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
            display,
            self.selected_session,
        )

    def _on_session_start_finished(self, outcome: SessionStartOutcome) -> None:
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
            self.logout_button.setEnabled(True)

    def _confirm_end_session(self) -> None:
        answer = QMessageBox.question(
            self,
            "End Session",
            "End the nested X session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.logout_button.setEnabled(False)
        self.clipboard_bridge.stop()
        self.nested_display = ""
        if self.pam_session_required:
            self._request_helper_stop()
        else:
            self.session_controller.request_end_session()

    def _on_session_finished(self, message: str) -> None:
        if self.closing:
            return
        self.clipboard_bridge.stop()
        self.nested_display = ""
        self.pending_message = message
        if self.pam_session_required and self.helper_transaction_pending:
            self._request_helper_stop()
        else:
            self._reset_login(message)

    def _on_helper_session_finished(self, message: str) -> None:
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

    def _on_helper_failed(self, message: str) -> None:
        if self.closing:
            return
        self.helper_transaction_pending = False
        self.remote_session_active = False
        self.helper_client = None
        self.allow_other_users = False
        self.login_page.set_allow_other_users(
            False, "" if self.session_controller.active else message
        )
        if self.session_controller.active:
            self.session_controller.stop(message)

    def _request_helper_stop(self) -> None:
        if self.helper_client is None:
            self.helper_transaction_pending = False
            self.remote_session_active = False
            if self.session_controller.active:
                self.session_controller.stop("Privileged helper is unavailable")
            return
        self.helper_client.stop_session()

    def _reset_login(self, message: str) -> None:
        if not message and not self.allow_other_users:
            message = OTHER_USERS_DISABLED
        self.clipboard_bridge.stop()
        self.nested_display = ""
        self.account = None
        self.selected_session = None
        self.pending_session = None
        self.pam_session_required = False
        self.helper_transaction_pending = False
        self.remote_session_active = False
        self.pending_message = ""
        self.logout_button.setEnabled(False)
        self.toolbar.hide()
        self.pages.setCurrentWidget(self.login_page)
        self.login_page.clear_form()
        self.login_page.set_busy(False, message)

    def _set_clipboard_sharing(self, enabled: bool) -> None:
        self.settings.setValue(CLIPBOARD_SHARING_SETTING, enabled)
        self.settings.sync()
        if not enabled:
            self.clipboard_bridge.stop()
        elif self.nested_display:
            self._start_clipboard_bridge()

    def _start_clipboard_bridge(self) -> None:
        try:
            self.clipboard_bridge.start(self.nested_display)
        except Exception as exc:
            LOG.exception("Could not start clipboard sharing")
            self._on_clipboard_bridge_failed(str(exc))

    def _on_clipboard_bridge_failed(self, message: str) -> None:
        if self.closing:
            return
        self.clipboard_bridge.stop()
        self.clipboard_action.setChecked(False)
        QMessageBox.warning(
            self,
            "Clipboard Sharing Unavailable",
            message or "Clipboard sharing stopped unexpectedly.",
        )

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.closing = True
        self.clipboard_bridge.stop()
        if self.helper_client is not None:
            self.helper_client.shutdown()
        self.session_controller.shutdown_blocking()
        event.accept()
