"""Every writable or data-bearing path the test fixture produces must live
under tmp_path.

review-1's finding on #68: `intake_dir` defaults to `REPO_ROOT/intake`,
which holds 14 real bank statements, and the only thing keeping a test out
of them was fixture discipline. That is the same shape as the `db_path`
default pointing at the live database — a repo-relative default that a
hand-built `Settings` wanders into silently.

Asserted by iterating the fields rather than naming the ones we happened to
think of, so a setting added later is covered without anyone remembering to
come back here. The mcp-side guard already does this for its own fixture;
this is the api-side half, and it is what caught `intake_dir` in the first
place.
"""

from pathlib import Path

import pytest

from api.config import REPO_ROOT, Settings

#: Inputs the API only ever reads, shipped in the repo. Pointing these at the
#: real files is correct — they are the schema and the migrations under test.
#: `attachment_roots` joins them: attach_file only READS from it, and
#: its default is the working tree by design. Nothing writes there, so
#: the redirect-into-tmp_path rule does not apply -- but the MCP test
#: fixture sets it anyway, because a test that attaches a file needs
#: its own tmp_path to be an allowed root. `data_dir` and
#: `environment_file` are refusal boundaries for attach_file: nothing
#: writes by those names.
READ_ONLY_REPO_PATHS = {"schema_path", "seed_categories_path", "migrations_dir", "attachment_roots", "data_dir", "environment_file"}

#: Executable inputs are invoked, never used as writable data destinations.
READ_ONLY_EXECUTABLE_PATHS = {"transcription_ffmpeg"}

#: Fields that are not paths at all.
NON_PATH_FIELDS = {"api_token", "port", "log_level", "google_oauth_client_id", "google_oauth_client_secret", "timezone", "transcription_enabled", "transcription_token", "whisper_port", "serve_socket", "serve_hostname", "ui_allowed_logins", "google_token_store"}


def _path_fields() -> set[str]:
    return set(Settings.model_fields) - READ_ONLY_REPO_PATHS - READ_ONLY_EXECUTABLE_PATHS - NON_PATH_FIELDS


def test_every_data_bearing_path_in_the_fixture_is_under_tmp_path(
    test_settings, tmp_path
):
    escaped = {
        field: value
        for field in _path_fields()
        if not Path(value := getattr(test_settings, field)).is_relative_to(tmp_path)
    }

    assert not escaped, (
        "these settings escape tmp_path, so a test using them reads or writes "
        f"real data: {escaped}"
    )


@pytest.mark.parametrize("dangerous", ["data", "intake"])
def test_no_fixture_path_lands_in_a_real_repo_data_directory(test_settings, dangerous):
    """Named separately from the tmp_path check because the failure it
    describes is worse and more specific: `data/` holds the live database
    that now carries the project backlog, and `intake/` holds real bank
    statements whose contents must never reach a model."""
    offenders = {
        field: value
        for field in _path_fields()
        if Path(value := getattr(test_settings, field)).is_relative_to(
            REPO_ROOT / dangerous
        )
    }

    assert not offenders, f"fixture points at the real {dangerous}/: {offenders}"


def test_the_exclusion_lists_still_describe_every_field():
    """Keeps the two lists above honest as Settings grows.

    Without this, a new field is silently excluded from both checks by not
    appearing in either list — the same hand-maintained-list problem
    EXPECTED_TABLES needed a test for.
    """
    covered = _path_fields() | READ_ONLY_REPO_PATHS | READ_ONLY_EXECUTABLE_PATHS | NON_PATH_FIELDS

    assert set(Settings.model_fields) == covered, (
        "new Settings field(s) "
        f"{sorted(set(Settings.model_fields) - covered)} — decide whether each "
        "is a data-bearing path (must be redirected in the fixture), a "
        "read-only repo input, or not a path"
    )
