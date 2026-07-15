from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QResizeEvent

from userdesk.app import DesktopPage, LoginPage, MainWindow
from userdesk.xsessions import XSession


SESSION = XSession(
    "test-session",
    "Test Session",
    ("/bin/true",),
    ("Test",),
    Path("/host/test-session.desktop"),
)


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


def test_main_window_clears_password_when_auth_is_dispatched(qapp) -> None:
    window = MainWindow("userdesk", allow_other_users=True)
    window.authenticate_requested.disconnect(window.pam_worker.authenticate)
    requests: list[tuple[str, str, str]] = []
    window.authenticate_requested.connect(
        lambda username, password, service: requests.append(
            (username, password, service)
        )
    )
    window.login_page.password.setText("secret")

    window._authenticate("alice", "secret", SESSION)

    assert window.login_page.password.text() == ""
    assert not window.login_page.login_button.isEnabled()
    assert requests == [("alice", "secret", "userdesk")]
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


def test_current_user_path_skips_pam(qapp, monkeypatch) -> None:
    window = MainWindow("userdesk", allow_other_users=False)
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
    pam_requests = []
    monkeypatch.setattr(
        window.session_controller,
        "start_user_session",
        lambda account, environment, session: sessions.append(
            (account, environment, session)
        ),
    )
    window.pam_open_requested.connect(
        lambda display, username, session_id, desktop: pam_requests.append(
            (display, username, session_id, desktop)
        )
    )
    window._on_xephyr_ready(":7")

    assert sessions == [(window.current_account, {}, SESSION)]
    assert pam_requests == []
    window.close()
