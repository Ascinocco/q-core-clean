"""The extract reader may not open files outside the pytest temp root.

`POST /documents/extract` reads from three configurable roots:
`intake_dir`, `documents_dir` and `inbox_dir`. `intake_dir` defaults to
`REPO_ROOT/intake`, which holds real bank statements — so a test holding
real settings reads them, and the failure is silent: extraction succeeds
and the test passes.

Each test asserts the refusal names *its own* root. With one root that
was redundant; with three it is the whole point, because a test can now
be refused by a root it is not testing and pass on the strength of
someone else's guard. The label is the only thing that distinguishes
them, which also makes a mis-paired label a failure here rather than a
cosmetic wrong word in a log line.

Through the endpoint rather than against `_resolve_under_roots` directly,
because the question is whether the guard is reachable on the real call
path, not whether the predicate works.

The refusal surfaces as a 500 rather than an exception reaching the test:
the app has a catch-all handler, so it is caught and logged like any other
unexpected error. A 500 alone would not distinguish the guard from an
ordinary bug, so each test asserts the refusal message in the log *and*
that the file was never opened — the second is the property that matters,
since a refusal after reading would be no protection at all.
"""


def _auth(settings):
    return {"Authorization": f"Bearer {settings.api_token}"}


def _post(client, settings, path):
    return client.post(
        "/documents/extract", json={"path": str(path)}, headers=_auth(settings)
    )


def _refusal_names(caplog, what: str):
    """The refusal fired for THIS root, not merely for some root."""
    return any(
        f"Refusing to touch {what}" in record.getMessage()
        or f"Refusing to touch {what}" in str(getattr(record, "exc_text", "") or "")
        for record in caplog.records
    )


def _refusal_logged(caplog):
    return any(
        "Refusing to touch" in record.getMessage()
        or "Refusing to touch" in str(getattr(record, "exc_text", "") or "")
        for record in caplog.records
    )

import logging
from pathlib import Path

from api.config import Settings


def _settings_with_roots(tmp_path, template, intake, documents, inbox):
    """Every root stated explicitly — `inbox` has no default on purpose.

    It used to be omitted here, so it fell back to `REPO_ROOT/inbox`: a
    real directory, reachable through the endpoint, in every test built by
    this helper. A default would have hidden that again, and the value a
    caller wants depends on which root it is testing.
    """
    return Settings(
        _env_file=None,
        api_token=template.api_token,
        db_path=template.db_path,
        documents_dir=str(documents),
        intake_dir=str(intake),
        inbox_dir=str(inbox),
        jyra_dir=template.jyra_dir,
        logs_dir=template.logs_dir,
        schema_path=template.schema_path,
        seed_categories_path=template.seed_categories_path,
        migrations_dir=template.migrations_dir,
    )


def test_extract_refuses_an_intake_root_outside_the_temp_root(
    client, test_settings, tmp_path, monkeypatch, caplog
):
    """The case that matters: intake_dir pointing at the real statements."""
    from api.config import get_settings
    from api.main import app

    allow_root = tmp_path / "pytest-root"
    allow_root.mkdir()
    outside = tmp_path / "workspace" / "real-repo" / "intake"
    outside.mkdir(parents=True)
    document = outside / "statement.pdf"
    document.write_bytes(b"%PDF-1.4 not really a pdf")
    monkeypatch.setenv("Q_CORE_TEST_TMP_ROOT", str(allow_root))

    dangerous = _settings_with_roots(
        tmp_path, test_settings, outside, allow_root / "documents", allow_root / "inbox"
    )
    opened: list[str] = []
    real_open = Path.open
    monkeypatch.setattr(
        Path, "open", lambda self, *a, **k: (opened.append(str(self)), real_open(self, *a, **k))[1]
    )

    app.dependency_overrides[get_settings] = lambda: dangerous
    try:
        with caplog.at_level(logging.ERROR):
            response = _post(client, dangerous, document)
    finally:
        app.dependency_overrides[get_settings] = lambda: test_settings

    assert response.status_code == 500
    assert _refusal_logged(caplog), "a 500 alone does not say the guard fired"
    assert _refusal_names(caplog, "the intake directory"), (
        "refused, but not for intake_dir — another root tripped first"
    )
    assert str(document) not in opened, "the file was read before the refusal"


def test_extract_refuses_a_documents_root_outside_the_temp_root(
    client, test_settings, tmp_path, monkeypatch, caplog
):
    """The second root, which the function is not named for.

    `_resolve_under_roots` opens files under either root, so guarding only
    `intake_dir` would leave this call site able to read a real
    `documents_dir` — covered where init_db creates it, uncovered here.
    Different call sites, different reachability.
    """
    from api.config import get_settings
    from api.main import app

    allow_root = tmp_path / "pytest-root"
    allow_root.mkdir()
    (allow_root / "intake").mkdir()
    outside = tmp_path / "workspace" / "real-repo" / "documents"
    outside.mkdir(parents=True)
    document = outside / "statement.pdf"
    document.write_bytes(b"%PDF-1.4 not really a pdf")
    monkeypatch.setenv("Q_CORE_TEST_TMP_ROOT", str(allow_root))

    dangerous = _settings_with_roots(
        tmp_path, test_settings, allow_root / "intake", outside, allow_root / "inbox"
    )
    opened: list[str] = []
    real_open = Path.open
    monkeypatch.setattr(
        Path, "open", lambda self, *a, **k: (opened.append(str(self)), real_open(self, *a, **k))[1]
    )

    app.dependency_overrides[get_settings] = lambda: dangerous
    try:
        with caplog.at_level(logging.ERROR):
            response = _post(client, dangerous, document)
    finally:
        app.dependency_overrides[get_settings] = lambda: test_settings

    assert response.status_code == 500
    assert _refusal_logged(caplog), "a 500 alone does not say the guard fired"
    assert _refusal_names(caplog, "the documents directory"), (
        "refused, but not for documents_dir — another root tripped first"
    )
    assert str(document) not in opened, "the file was read before the refusal"


def test_extract_refuses_an_inbox_root_outside_the_temp_root(
    client, test_settings, tmp_path, monkeypatch, caplog
):
    """The third root, which the guard loop never received.

    `inbox_dir` was added to the allowed roots without being added to the
    guard: the roots and their labels were two parallel sequences joined
    by `zip()`, and three roots against two labels truncates to two. Not
    an error, not a warning — `zip()` stops at the shorter input, so the
    root added last was dropped from the loop entirely.

    The endpoint is the right place to see it. `inbox_dir` defaults to a
    real directory this system *moves files out of*, and the symptom is
    not a crash: it is the extract reader reaching a real inbox during a
    test and succeeding.
    """
    from api.config import get_settings
    from api.main import app

    allow_root = tmp_path / "pytest-root"
    allow_root.mkdir()
    (allow_root / "intake").mkdir()
    outside = tmp_path / "workspace" / "real-repo" / "inbox"
    outside.mkdir(parents=True)
    document = outside / "receipt.pdf"
    document.write_bytes(b"%PDF-1.4 not really a pdf")
    monkeypatch.setenv("Q_CORE_TEST_TMP_ROOT", str(allow_root))

    dangerous = _settings_with_roots(
        tmp_path,
        test_settings,
        allow_root / "intake",
        allow_root / "documents",
        outside,
    )
    opened: list[str] = []
    real_open = Path.open
    monkeypatch.setattr(
        Path, "open", lambda self, *a, **k: (opened.append(str(self)), real_open(self, *a, **k))[1]
    )

    app.dependency_overrides[get_settings] = lambda: dangerous
    try:
        with caplog.at_level(logging.ERROR):
            response = _post(client, dangerous, document)
    finally:
        app.dependency_overrides[get_settings] = lambda: test_settings

    assert response.status_code == 500
    assert _refusal_logged(caplog), "a 500 alone does not say the guard fired"
    assert _refusal_names(caplog, "the inbox directory"), (
        "refused, but not for inbox_dir — another root tripped first"
    )
    assert str(document) not in opened, "the file was read before the refusal"


def test_the_refusal_happens_before_anything_is_read(
    client, test_settings, tmp_path, monkeypatch
):
    """Refusing after reading would be no protection at all.

    The point is that the bytes are never opened, not that the response is
    an error — by the time a real statement has been read, redaction or no
    redaction, it has been read.
    """
    from api.config import get_settings
    from api.main import app

    allow_root = tmp_path / "pytest-root"
    allow_root.mkdir()
    outside = tmp_path / "workspace" / "intake"
    outside.mkdir(parents=True)
    document = outside / "statement.pdf"
    document.write_bytes(b"%PDF-1.4 not really a pdf")
    monkeypatch.setenv("Q_CORE_TEST_TMP_ROOT", str(allow_root))

    opened: list[str] = []
    real_open = Path.open

    def recording_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)

    dangerous = _settings_with_roots(
        tmp_path, test_settings, outside, allow_root / "documents", allow_root / "inbox"
    )
    app.dependency_overrides[get_settings] = lambda: dangerous
    try:
        response = _post(client, dangerous, document)
    finally:
        app.dependency_overrides[get_settings] = lambda: test_settings

    assert response.status_code == 500
    assert str(document) not in opened, "the file was opened before the refusal"


def test_roots_under_the_temp_root_are_allowed(client, test_settings, tmp_path):
    """The guard must not fail closed on an ordinary test."""
    intake = tmp_path / "intake"
    intake.mkdir()
    dangerous = _settings_with_roots(
        tmp_path, test_settings, intake, tmp_path / "documents", tmp_path / "inbox"
    )

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: dangerous
    try:
        # Missing file, so the endpoint answers on its own terms — the
        # point is that the guard did not intervene.
        response = _post(client, dangerous, intake / "nope.pdf")
    finally:
        app.dependency_overrides[get_settings] = lambda: test_settings

    assert response.status_code in (400, 404), response.status_code
