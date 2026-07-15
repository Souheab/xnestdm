from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run host X11 sessions inside an embedded Xephyr server"
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

    if not os.environ.get("DISPLAY"):
        print(
            "xnestdm requires an outer X11 display or XWayland (DISPLAY is unset).",
            file=sys.stderr,
        )
        return 2

    if os.getuid() != os.geteuid() and args.pam_service:
        print(
            "--pam-service cannot be used through the privileged NixOS launcher.",
            file=sys.stderr,
        )
        return 2

    os.environ["QT_QPA_PLATFORM"] = "xcb"

    from PySide6.QtWidgets import QApplication

    from .app import MainWindow
    from .auth import select_pam_service

    application = QApplication(sys.argv[:1])
    window = MainWindow(
        select_pam_service(args.pam_service), allow_other_users=os.geteuid() == 0
    )
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
