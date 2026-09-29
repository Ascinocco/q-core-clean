"""The API's own attachment guards, independent of the tool.

The tool's checks stop a bad path being READ. These are the only thing
standing between the attachment store and anything that did not arrive
through the tool -- which is what makes them defence in depth that is
actually reachable rather than the signature-omission kind.
"""

from api.documents import MAX_ATTACHMENT_BYTES

FAKE_CARD = "4532015112830366"


def _ticket(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    entity = client.post(
        "/entities", json={"type": "project", "name": "P"}, headers=headers
    ).json()
    board = client.post(
        "/boards", json={"entity_id": entity["id"], "title": "B"}, headers=headers
    ).json()
    created = client.post(
        "/tickets",
        json={"board_id": board["id"], "title": "T", "type": "task", "actor": "x"},
        headers=headers,
    ).json()
    return headers, created["id"]


def test_one_byte_over_the_cap_is_refused_by_the_api_independently(
    client, test_settings
):
    """The API's cap is not redundant: it is the only thing standing
    between the store and anything that did not arrive through the tool."""
    headers, ticket_id = _ticket(client, test_settings)

    response = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("big.bin", b"\x00" * (MAX_ATTACHMENT_BYTES + 1))},
        headers=headers,
    )

    assert response.status_code == 413, response.text
    assert str(MAX_ATTACHMENT_BYTES) in response.json()["error"]["message"]


def test_the_api_refuses_unredacted_content_independently(client, test_settings):
    headers, ticket_id = _ticket(client, test_settings)

    response = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("receipt.txt", f"Card {FAKE_CARD}".encode())},
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "attachment_not_redacted"
    assert FAKE_CARD not in response.text


def test_the_api_refuses_a_sensitive_filename_without_echoing_it(
    client, test_settings, caplog
):
    headers, ticket_id = _ticket(client, test_settings)

    response = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": (f"receipt-{FAKE_CARD}.txt", b"safe text")},
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "attachment_not_redacted"
    assert FAKE_CARD not in response.text
    assert FAKE_CARD not in caplog.text
