from __future__ import annotations

import logging

from PySide6.QtCore import QMetaObject, QThread, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QComboBox,
    QSizePolicy,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .auth import (
    Account,
    AuthenticationOutcome,
    PamWorker,
    SessionOpenOutcome,
)
from .session import SessionController, invoking_account
from .xsessions import XSession, discover_xsessions, preferred_xsession_index

LOG = logging.getLogger(__name__)
OTHER_USERS_DISABLED = (
    "Log in as another user is disabled. Start Userdesk with sudo to enable it."
)
VIEWPORT_PADDING = 12


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
    authenticate_requested = Signal(str, str, str)
    pam_open_requested = Signal(str, str, str, str)
    pam_close_requested = Signal()

    def __init__(self, pam_service: str, allow_other_users: bool):
        super().__init__()
        self.pam_service = pam_service
        self.allow_other_users = allow_other_users
        self.current_account = invoking_account()
        self.account: Account | None = None
        self.selected_session: XSession | None = None
        self.pending_session: XSession | None = None
        self.pam_session_required = False
        self.pending_message = ""
        self.closing = False

        self.setWindowTitle("Userdesk")
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
        self.logout_button = QPushButton("End Session")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(self.user_label)
        self.toolbar.addWidget(spacer)
        self.toolbar.addWidget(self.logout_button)
        self.addToolBar(self.toolbar)
        self.toolbar.hide()
        self.logout_button.setEnabled(False)

        self.session_controller = SessionController()
        self.desktop_page.host.resized.connect(self.session_controller.resize_xephyr)
        self.session_controller.xephyr_ready.connect(self._on_xephyr_ready)
        self.session_controller.session_ready.connect(self._on_session_ready)
        self.session_controller.finished.connect(self._on_session_finished)

        self.pam_thread = QThread(self)
        self.pam_worker = PamWorker()
        self.pam_worker.moveToThread(self.pam_thread)
        self.authenticate_requested.connect(self.pam_worker.authenticate)
        self.pam_open_requested.connect(self.pam_worker.open_session)
        self.pam_close_requested.connect(self.pam_worker.close_session)
        self.pam_worker.authentication_finished.connect(
            self._on_authentication_finished
        )
        self.pam_worker.session_open_finished.connect(self._on_pam_open_finished)
        self.pam_worker.session_closed.connect(self._on_pam_closed)
        self.pam_thread.start()

        self.login_page.submitted.connect(self._authenticate)
        self.login_page.current_user_requested.connect(self._use_current_user)
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
        self.authenticate_requested.emit(username, password, self.pam_service)
        password = ""  # drop this reference as soon as Qt has queued the call

    def _on_authentication_finished(self, outcome: AuthenticationOutcome) -> None:
        if self.closing:
            return
        if not outcome.ok or outcome.account is None:
            self.pending_session = None
            self.login_page.set_busy(False, outcome.message or "Authentication failed")
            self.login_page.password.setFocus()
            return
        if self.pending_session is None:
            self.login_page.set_busy(False, "No X session is selected")
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
                self.pam_close_requested.emit()
            else:
                self._reset_login(self.pending_message)

    def _on_xephyr_ready(self, display: str) -> None:
        if self.closing or self.account is None or self.selected_session is None:
            return
        if not self.pam_session_required:
            self.session_controller.start_user_session(
                self.account, {}, self.selected_session
            )
            return
        try:
            invoking_user = invoking_account().username
        except Exception:
            invoking_user = ""
        self.pam_open_requested.emit(
            display,
            invoking_user,
            self.selected_session.session_id,
            self.selected_session.current_desktop,
        )

    def _on_pam_open_finished(self, outcome: SessionOpenOutcome) -> None:
        if self.closing:
            return
        if not outcome.ok or self.account is None or self.selected_session is None:
            self.session_controller.stop(
                outcome.message or "Could not open PAM session"
            )
            return
        self.session_controller.start_user_session(
            self.account,
            outcome.environment or {},
            self.selected_session,
        )

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
        self.session_controller.request_end_session()

    def _on_session_finished(self, message: str) -> None:
        if self.closing:
            return
        self.pending_message = message
        if self.pam_session_required:
            self.pam_close_requested.emit()
        else:
            self._reset_login(message)

    def _on_pam_closed(self) -> None:
        if self.closing:
            return
        message, self.pending_message = self.pending_message, ""
        self._reset_login(message)

    def _reset_login(self, message: str) -> None:
        if not message and not self.allow_other_users:
            message = OTHER_USERS_DISABLED
        self.account = None
        self.selected_session = None
        self.pending_session = None
        self.pam_session_required = False
        self.pending_message = ""
        self.logout_button.setEnabled(False)
        self.toolbar.hide()
        self.pages.setCurrentWidget(self.login_page)
        self.login_page.clear_form()
        self.login_page.set_busy(False, message)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.closing = True
        self.session_controller.shutdown_blocking()
        QMetaObject.invokeMethod(
            self.pam_worker,
            "close_session",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        self.pam_thread.quit()
        self.pam_thread.wait(3000)
        event.accept()
