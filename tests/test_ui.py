from __future__ import annotations

from PySide6.QtCore import Qt

from userdesk.app import DesktopPage, LoginPage, MainWindow


def test_login_form_uses_password_echo_and_emits_credentials(qapp) -> None:
    page = LoginPage()
    submitted: list[tuple[str, str]] = []
    page.submitted.connect(
        lambda username, password: submitted.append((username, password))
    )
    page.username.setText("alice")
    page.password.setText("secret")

    page.login_button.click()

    assert page.password.echoMode() == page.password.EchoMode.Password
    assert submitted == [("alice", "secret")]


def test_busy_state_and_clear_form(qapp) -> None:
    page = LoginPage()
    page.username.setText("alice")
    page.password.setText("secret")
    page.set_busy(True, "Authenticating…")
    assert not page.login_button.isEnabled()
    assert page.status.text() == "Authenticating…"

    page.clear_form()
    page.set_busy(False)
    assert page.username.text() == ""
    assert page.password.text() == ""
    assert page.login_button.isEnabled()


def test_desktop_host_is_native_and_focusable(qapp) -> None:
    page = DesktopPage()
    assert page.host.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    assert page.host.focusPolicy() == Qt.FocusPolicy.StrongFocus


def test_main_window_clears_password_when_auth_is_dispatched(qapp) -> None:
    window = MainWindow("userdesk")
    window.authenticate_requested.disconnect(window.pam_worker.authenticate)
    requests: list[tuple[str, str, str]] = []
    window.authenticate_requested.connect(
        lambda username, password, service: requests.append(
            (username, password, service)
        )
    )
    window.login_page.password.setText("secret")

    window._authenticate("alice", "secret")

    assert window.login_page.password.text() == ""
    assert not window.login_page.login_button.isEnabled()
    assert requests == [("alice", "secret", "userdesk")]
    assert window.toolbar.isHidden()
    window.close()
