from __future__ import annotations

import logging

from PySide6.QtCore import QMetaObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
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

LOG = logging.getLogger(__name__)


class LoginPage(QWidget):
    submitted = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_button = QPushButton("Log In")
        self.status = QLabel()
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Username:", self.username)
        form.addRow("Password:", self.password)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.login_button)

        panel = QWidget()
        panel.setMaximumWidth(440)
        panel_layout = QVBoxLayout(panel)
        panel_layout.addWidget(QLabel("Log in to an XFCE session"))
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
        self.password.returnPressed.connect(self._submit)
        self.username.returnPressed.connect(self.password.setFocus)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.username.setEnabled(not busy)
        self.password.setEnabled(not busy)
        self.login_button.setEnabled(not busy)
        self.status.setText(message)

    def clear_form(self) -> None:
        self.username.clear()
        self.password.clear()
        self.status.clear()
        self.username.setFocus()

    def _submit(self) -> None:
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            self.status.setText("Enter a username and password")
            return
        self.submitted.emit(username, password)


class DesktopPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.host = QWidget()
        self.host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.host.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.host)


class MainWindow(QMainWindow):
    authenticate_requested = Signal(str, str, str)
    pam_open_requested = Signal(str, str)
    pam_close_requested = Signal()

    def __init__(self, pam_service: str):
        super().__init__()
        self.pam_service = pam_service
        self.account: Account | None = None
        self.pending_message = ""
        self.closing = False

        self.setWindowTitle("Userdesk")
        self.resize(1200, 800)

        self.login_page = LoginPage()
        self.desktop_page = DesktopPage()
        self.pages = QStackedWidget()
        self.pages.addWidget(self.login_page)
        self.pages.addWidget(self.desktop_page)
        self.setCentralWidget(self.pages)

        self.toolbar = QToolBar("Session", self)
        self.toolbar.setMovable(False)
        self.user_label = QLabel()
        self.logout_button = QPushButton("Log Out")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(self.user_label)
        self.toolbar.addWidget(spacer)
        self.toolbar.addWidget(self.logout_button)
        self.addToolBar(self.toolbar)
        self.toolbar.hide()
        self.logout_button.setEnabled(False)

        self.session_controller = SessionController()
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
        self.logout_button.clicked.connect(self._confirm_logout)

    def _authenticate(self, username: str, password: str) -> None:
        if self.account is not None or self.session_controller.active:
            return
        self.login_page.set_busy(True, "Authenticating…")
        self.login_page.password.clear()
        self.authenticate_requested.emit(username, password, self.pam_service)
        password = ""  # drop this reference as soon as Qt has queued the call

    def _on_authentication_finished(self, outcome: AuthenticationOutcome) -> None:
        if self.closing:
            return
        if not outcome.ok or outcome.account is None:
            self.login_page.set_busy(False, outcome.message or "Authentication failed")
            self.login_page.password.setFocus()
            return
        self.account = outcome.account
        self.user_label.setText(f"Logged in as {self.account.username}")
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
            self.pam_close_requested.emit()

    def _on_xephyr_ready(self, display: str) -> None:
        if self.closing or self.account is None:
            return
        try:
            invoking_user = invoking_account().username
        except Exception:
            invoking_user = ""
        self.pam_open_requested.emit(display, invoking_user)

    def _on_pam_open_finished(self, outcome: SessionOpenOutcome) -> None:
        if self.closing:
            return
        if not outcome.ok or self.account is None:
            self.session_controller.stop(
                outcome.message or "Could not open PAM session"
            )
            return
        self.session_controller.start_user_session(
            self.account, outcome.environment or {}
        )

    def _on_session_ready(self) -> None:
        if not self.closing:
            self.logout_button.setEnabled(True)

    def _confirm_logout(self) -> None:
        answer = QMessageBox.question(
            self,
            "Log Out",
            "Log out of the nested XFCE session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.logout_button.setEnabled(False)
        self.session_controller.request_logout()

    def _on_session_finished(self, message: str) -> None:
        if self.closing:
            return
        self.pending_message = message
        self.pam_close_requested.emit()

    def _on_pam_closed(self) -> None:
        if self.closing:
            return
        message, self.pending_message = self.pending_message, ""
        self.account = None
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
