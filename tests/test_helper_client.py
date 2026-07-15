from __future__ import annotations

import socket

from xnestdm.auth import Account
from xnestdm.helper_client import HelperClient
from xnestdm.helper_protocol import PROTOCOL_VERSION, decode_message
from xnestdm.helper_transport import HelperBootstrap


class FakeProcess:
    pass


def client() -> tuple[HelperClient, socket.socket, Account]:
    gui_socket, helper_socket = socket.socketpair()
    account = Account("caller", 1000, 1000, "/home/caller", "/bin/sh", (1000,))
    instance = HelperClient(
        HelperBootstrap(gui_socket, FakeProcess(), account)  # type: ignore[arg-type]
    )
    return instance, helper_socket, account


def read_request(connection: socket.socket) -> dict[str, object]:
    return decode_message(connection.recv(65536).rstrip(b"\n"))


def test_helper_client_tags_requests_and_routes_concurrent_responses(qapp) -> None:
    helper_client, connection, account = client()
    outcomes = []
    helper_client.authentication_finished.connect(
        lambda tab_id, outcome: outcomes.append((tab_id, outcome))
    )

    helper_client.authenticate(4, "alice", "first")
    first_request = read_request(connection)
    helper_client.authenticate(8, "bob", "second")
    second_request = read_request(connection)

    assert first_request["tab_id"] == 4
    assert second_request["tab_id"] == 8
    helper_client._handle(
        {
            "protocol": PROTOCOL_VERSION,
            "id": second_request["id"],
            "ok": True,
            "account": account.to_mapping(),
        }
    )
    helper_client._handle(
        {
            "protocol": PROTOCOL_VERSION,
            "id": first_request["id"],
            "ok": False,
            "message": "Authentication failed",
        }
    )

    assert [tab_id for tab_id, _outcome in outcomes] == [8, 4]
    assert outcomes[0][1].ok
    assert not outcomes[1][1].ok
    helper_client._close()
    connection.close()


def test_helper_client_routes_async_completion_by_tab(qapp) -> None:
    helper_client, connection, _account = client()
    completions = []
    helper_client.session_finished.connect(
        lambda tab_id, message: completions.append((tab_id, message))
    )

    helper_client._handle(
        {
            "protocol": PROTOCOL_VERSION,
            "event": "session_finished",
            "tab_id": 12,
            "status": 1,
            "message": "Session failed",
            "diagnostics": "details",
        }
    )

    assert completions == [(12, "Session failed")]
    helper_client._close()
    connection.close()
