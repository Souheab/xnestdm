from __future__ import annotations

import argparse
import json
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Start XFCE inside a D-Bus session")
    parser.add_argument("--notify-fd", required=True, type=int)
    parser.add_argument("--shell", required=True)
    parser.add_argument("--xinitrc", required=True)
    args = parser.parse_args()

    payload = {
        "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
    }
    with os.fdopen(args.notify_fd, "w", encoding="utf-8", closefd=True) as stream:
        json.dump(payload, stream)
        stream.write("\n")
        stream.flush()

    os.execv(args.shell, [args.shell, args.xinitrc])


if __name__ == "__main__":
    main()
