from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an XFCE desktop inside an embedded Xephyr server"
    )
    parser.add_argument("--pam-service", help="override the PAM service name")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    os.environ["QT_QPA_PLATFORM"] = "xcb"

    if not os.environ.get("DISPLAY"):
        print(
            "userdesk requires an outer X11 display or XWayland (DISPLAY is unset).",
            file=sys.stderr,
        )
        return 2

    if os.geteuid() != 0:
        message = (
            "Userdesk must run as root to authenticate and switch users.\n\n"
            "Run:\n"
            "sudo --preserve-env=DISPLAY,XAUTHORITY nix run ."
        )
        print(message, file=sys.stderr)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            application = QApplication(sys.argv[:1])
            QMessageBox.critical(None, "Userdesk", message)
            application.quit()
        except Exception:
            logging.getLogger(__name__).exception("Could not show startup error")
        return 2

    from PySide6.QtWidgets import QApplication

    from .app import MainWindow
    from .auth import select_pam_service

    application = QApplication(sys.argv[:1])
    window = MainWindow(select_pam_service(args.pam_service))
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
