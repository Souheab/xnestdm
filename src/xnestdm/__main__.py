from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:
    from .auth import Account
    from .helper_transport import HelperBootstrap


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

    try:
        bootstrap = _prepare_helper(args.pam_service, args.verbose)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    os.environ["QT_QPA_PLATFORM"] = "xcb"

    from PySide6.QtWidgets import QApplication

    from .app import MainWindow
    from .helper_client import HelperClient

    application = QApplication(sys.argv[:1])
    helper_client = HelperClient(bootstrap) if bootstrap is not None else None
    window = MainWindow(helper_client)
    window.show()
    return application.exec()


def _prepare_helper(pam_service: str | None, verbose: bool) -> HelperBootstrap | None:
    from .auth import Account, select_pam_service
    from .helper_transport import configured_helper, start_helper

    real_uid = os.getuid()
    effective_uid = os.geteuid()
    helper_path = configured_helper()

    if effective_uid == 0:
        if real_uid != 0:
            if pam_service:
                raise RuntimeError(
                    "--pam-service cannot be used through the privileged NixOS launcher."
                )
            caller = Account.from_uid(real_uid)
            executable = helper_path or shutil.which("xnestdm-helper")
            if not executable:
                raise RuntimeError("The xnestdm privileged helper is unavailable")
            bootstrap = start_helper(executable, verbose=verbose)
        else:
            sudo_uid = os.environ.get("SUDO_UID", "")
            if not sudo_uid.isdigit() or int(sudo_uid) == 0:
                raise RuntimeError(
                    "Do not run the xnestdm GUI directly as root; start it as a normal "
                    "user or through sudo from a non-root account."
                )
            caller = Account.from_uid(int(sudo_uid))
            executable = helper_path or shutil.which("xnestdm-helper")
            if not executable:
                raise RuntimeError("The xnestdm privileged helper is unavailable")
            bootstrap = start_helper(
                executable,
                pam_service=select_pam_service(pam_service),
                caller_uid=caller.uid,
                verbose=verbose,
            )
        _drop_privileges(caller)
        return bootstrap

    if real_uid != effective_uid:
        raise RuntimeError("xnestdm must not retain partial process privileges")
    if not helper_path:
        return None
    try:
        return start_helper(helper_path, verbose=verbose)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Privileged helper is unavailable; alternate-user login is disabled: %s",
            exc,
        )
        return None


def _drop_privileges(account: Account) -> None:
    os.initgroups(account.username, account.gid)
    os.setgid(account.gid)
    os.setuid(account.uid)
    if os.getuid() != account.uid or os.geteuid() != account.uid:
        raise RuntimeError("Could not drop xnestdm GUI privileges")
    os.environ.update(
        {
            "HOME": account.home,
            "USER": account.username,
            "LOGNAME": account.username,
            "SHELL": account.shell,
        }
    )
    runtime = f"/run/user/{account.uid}"
    try:
        stat = os.stat(runtime)
    except OSError:
        os.environ.pop("XDG_RUNTIME_DIR", None)
    else:
        if stat.st_uid == account.uid:
            os.environ["XDG_RUNTIME_DIR"] = runtime
    for key in ("SUDO_UID", "SUDO_GID", "SUDO_USER", "SUDO_COMMAND"):
        os.environ.pop(key, None)


if __name__ == "__main__":
    raise SystemExit(main())
