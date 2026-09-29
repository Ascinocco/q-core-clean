def test_health_requires_a_token_like_every_route(client):
    """split part 2: no route answers without a credential (api/serve_gate.py)."""
    assert client.get("/health").status_code == 401
    response = client.get("/health", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_requires_auth(client):
    response = client.get("/")
    assert response.status_code == 401


def test_root_rejects_wrong_token(client):
    response = client.get("/", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Missing or invalid bearer token"}
    }


def test_root_accepts_correct_token(client, test_settings):
    response = client.get(
        "/", headers={"Authorization": f"Bearer {test_settings.api_token}"}
    )
    assert response.status_code == 200
    assert response.json() == {"name": "q-core API"}


def test_unknown_route_returns_flat_envelope(client):
    # Unauthenticated, even an unknown path is refused before routing.
    assert client.get("/nonexistent").status_code == 401
    response = client.get("/nonexistent", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 404
    assert "code" in response.json()["error"]


def test_missing_config_returns_clean_500(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from api.config import Settings, get_settings
    from api.main import app

    monkeypatch.delenv("Q_CORE_API_TOKEN", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", str(tmp_path / "nonexistent.env"))
    get_settings.cache_clear()
    app.dependency_overrides.pop(get_settings, None)

    unconfigured_client = TestClient(app)
    # Past the gate (which reads the autouse fixture's settings) to the
    # route's own Depends(get_settings), which is the path under test.
    response = unconfigured_client.get("/", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "configuration_error",
            "message": "Server misconfigured — check .env / Q_CORE_* environment variables.",
        }
    }

    get_settings.cache_clear()


# --- 422 error envelope -------------------------------------------------
#
# Every 422 in this API goes through main.py's RequestValidationError
# handler, so these live here rather than in a per-resource test file.
# They assert the body shape, not just the status code — the gap that let
# the raw FastAPI envelope survive all of Phase 2 (project-management.json
# a todo).


def _auth(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


def test_missing_required_field_uses_error_envelope(client, test_settings):
    response = client.post(
        "/entities", json={"type": "pet"}, headers=_auth(test_settings)
    )

    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "Request validation failed"
    assert body["error"]["details"] == [
        {"field": "name", "message": "Field required"}
    ]


def test_wrong_field_type_uses_error_envelope(client, test_settings):
    response = client.post(
        "/entities",
        json={"type": "pet", "name": 123},
        headers=_auth(test_settings),
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"] == [
        {"field": "name", "message": "Input should be a valid string"}
    ]


def test_bad_attribute_value_reports_dotted_field_path(client, test_settings):
    # That case: a nested attributes error must read as
    # "attributes.purchase_date", not a bare "purchase_date" that looks
    # like a top-level field.
    response = client.post(
        "/entities",
        json={
            "type": "property",
            "name": "Lake House",
            "attributes": {"purchase_date": "not-a-date"},
        },
        headers=_auth(test_settings),
    )

    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert [detail["field"] for detail in details] == ["attributes.purchase_date"]


def test_deeply_nested_attribute_reports_full_path(client, test_settings):
    response = client.post(
        "/entities",
        json={
            "type": "property",
            "name": "Lake House",
            "attributes": {"mortgage": {"term_years": "soon"}},
        },
        headers=_auth(test_settings),
    )

    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert [detail["field"] for detail in details] == ["attributes.mortgage.term_years"]


def test_bad_query_param_uses_error_envelope(client, test_settings):
    response = client.get("/entities?limit=0", headers=_auth(test_settings))

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert [detail["field"] for detail in body["error"]["details"]] == ["query.limit"]


def test_multiple_errors_all_appear_in_details(client, test_settings):
    response = client.post(
        "/entities", json={"type": "spaceship"}, headers=_auth(test_settings)
    )

    assert response.status_code == 422
    body = response.json()
    # Static summary — the per-field text lives in details, not concatenated
    # into the message.
    assert body["error"]["message"] == "Request validation failed"
    assert sorted(detail["field"] for detail in body["error"]["details"]) == [
        "name",
        "type",
    ]


def test_validation_errors_do_not_echo_submitted_values(client, test_settings):
    # FastAPI's default 422 body echoes the rejected value back in an
    # "input" field (and links pydantic's docs in "url"). Per CLAUDE.md,
    # submitted values can be sensitive — a mistyped account number must
    # not be reflected into API responses, logs or MCP transcripts.
    secret = "123-45-6789"
    response = client.post(
        "/entities",
        json={
            "type": "person",
            "name": "Alex",
            "attributes": {"date_of_birth": secret},
        },
        headers=_auth(test_settings),
    )

    assert response.status_code == 422
    assert secret not in response.text
    assert "url" not in response.text
    for detail in response.json()["error"]["details"]:
        assert set(detail) == {"field", "message"}


def test_app_raised_errors_keep_their_flat_envelope(client, test_settings):
    # The new handler must not disturb the non-422 paths: NotFoundError and
    # friends carry no details key.
    response = client.get("/entities/does-not-exist", headers=_auth(test_settings))

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "No entity with id 'does-not-exist'",
        }
    }


def test_malformed_json_body_reports_body_not_a_character_offset(client, test_settings):
    # Pydantic reports a json_invalid error with loc ("body", <char index>),
    # so the generic path renderer would emit a field of "1" — a character
    # position masquerading as a field name.
    response = client.post(
        "/entities",
        content=b"{not json",
        headers={**_auth(test_settings), "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert [detail["field"] for detail in details] == ["body"]
    assert "JSON" in details[0]["message"]


def test_rejected_account_number_never_reaches_the_http_response(client, test_settings):
    # Composition test over two protections that were built separately and
    # collided on merge: models.py redacts a sensitive field's `input`, and
    # this handler drops `input` from the response entirely.
    #
    # What this does NOT do is catch either layer failing — mutation testing
    # showed each is already pinned on its own (removing the handler's drop
    # fails three tests here; removing the redaction fails
    # test_rejected_sensitive_value_is_not_echoed_in_the_validation_error in
    # test_models.py). This test fails only when *both* are gone, which
    # makes it strictly weaker as a detector than that pair.
    #
    # Its value is instead the end-to-end contract: it is the only test
    # asserting the user-visible guarantee at the HTTP boundary, so it
    # survives either layer's internals being refactored or relocated,
    # where the layer-specific tests would not.
    card_number = "4111111111111111"
    response = client.post(
        "/entities",
        json={
            "type": "account",
            "name": "Chase Checking",
            "attributes": {"last4": card_number},
        },
        headers=_auth(test_settings),
    )

    assert response.status_code == 422
    assert card_number not in response.text
    details = response.json()["error"]["details"]
    assert [detail["field"] for detail in details] == ["attributes.last4"]


def test_the_autouse_settings_fixture_is_in_effect(test_settings):
    """Proves the conftest autouse fixture fires, rather than trusting it.

    An autouse fixture that silently stopped applying — a renamed module,
    a changed import — leaves every test looking exactly the same and the
    hazard wide open, which is the failure mode the fixture exists to
    remove in the first place.

    It lives here rather than in conftest.py because pytest does not
    collect tests from conftest.py: written there it would have passed by
    never running.
    """
    import api.main

    resolved = api.main.get_settings()

    assert resolved.db_path == test_settings.db_path
    assert "/q-core/data/" not in resolved.db_path, "that would be the real database"


def test_the_autouse_fixture_leaves_the_depends_path_alone(client, test_settings):
    """The fixture patches api.main's module-level name, not the function
    object that `Depends(get_settings)` resolves — which is why
    test_missing_config_returns_clean_500 still exercises the real path."""
    response = client.get("/health", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
