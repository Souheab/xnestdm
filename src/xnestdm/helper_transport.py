from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass

from .auth import Account
from .helper_protocol import MAX_MESSAGE_SIZE, ProtocolError, decode_message


@dataclass
class HelperBootstrap:
    socket: socket.socket
    process: subprocess.Popen[bytes]
    caller: Account


def start_helper(
    executable: str,
    *,
    pam_service: str | None = None,
    caller_uid: int | None = None,
    verbose: bool = False,
    timeout: float = 3.0,
) -> HelperBootstrap:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    argv = [executable, "--socket-fd", str(child.fileno())]
    if pam_service:
        argv.extend(["--pam-service", pam_service])
    if caller_uid is not None:
        argv.extend(["--caller-uid", str(caller_uid)])
    if verbose:
        argv.append("--verbose")

    try:
        process = subprocess.Popen(
            argv,
            pass_fds=(child.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception:
        parent.close()
        child.close()
        raise
    child.close()

    try:
        parent.settimeout(timeout)
        payload = _read_line(parent)
        ready = decode_message(payload)
        if ready.get("event") != "ready" or ready.get("privileged") is not True:
            raise ProtocolError("Privileged helper did not complete its handshake")
        account_data = ready.get("caller")
        if not isinstance(account_data, dict):
            raise ProtocolError("Privileged helper returned invalid caller data")
        caller = Account.from_mapping(account_data)
        if caller_uid is not None and caller.uid != caller_uid:
            raise ProtocolError("Privileged helper returned the wrong caller")
    except Exception:
        parent.close()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        if parent.fileno() >= 0:
            parent.settimeout(None)

    return HelperBootstrap(parent, process, caller)


def configured_helper() -> str | None:
    value = os.environ.get("XNESTDM_HELPER", "").strip()
    return value or None


def _read_line(connection: socket.socket) -> bytes:
    buffer = bytearray()
    while True:
        chunk = connection.recv(min(4096, MAX_MESSAGE_SIZE + 1 - len(buffer)))
        if not chunk:
            raise ConnectionError("Privileged helper exited during startup")
        buffer.extend(chunk)
        if b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            if remainder:
                raise ProtocolError("Unexpected data after helper handshake")
            return bytes(line)
        if len(buffer) > MAX_MESSAGE_SIZE:
            raise ProtocolError("Privileged-helper handshake is too large")
