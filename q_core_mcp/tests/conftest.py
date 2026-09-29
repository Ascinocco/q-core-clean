import json
from pathlib import Path

import anyio
import pytest

from api.config import REPO_ROOT, Settings


@pytest.fixture()
def test_settings(tmp_path: Path) -> Settings:
    profile = tmp_path / 'privacy.json'
    profile.write_text(json.dumps({'version': 1, 'reviewed': True,
                                   'names': ['Alex Example'], 'addresses': ['123 Example Lane']}))
    profile.chmod(0o600)
    return Settings(
        _env_file=None,
        api_token="test-token",
        db_path=str(tmp_path / "q-core.db"),
        documents_dir=str(tmp_path / "documents"),
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        # jyra_dir is not optional here: it defaults to the real
        # data/jyra/, so a test that uploads an attachment would write into
        # the user's actual data directory. api/tests/conftest.py has always
        # set it; this fixture was never updated when Jyra landed.
        jyra_dir=str(tmp_path / "jyra"),
        eval_summaries_dir=str(tmp_path / "eval-summaries"),
        forecast_dir=str(tmp_path / "forecast"),
        # logs_dir for the same reason, and it is not merely theoretical:
        # every test in test_logging_config.py currently redirects it by
        # hand with model_copy, which works but is defence-by-remembering.
        # A new test that triggers logging without that line would append to
        # the real data/logs/ — which now holds the running launchd
        # server's own log.
        logs_dir=str(tmp_path / "logs"),
        intake_dir=str(tmp_path / "intake"),
        inbox_dir=str(tmp_path / "inbox"),
        secrets_dir=str(tmp_path / "secrets"),
        # Never this machine's Keychain: a test that fell back to it once
        # sent the real OAuth client to Google (pre-deploy follow-ups).
        google_token_store="file",
        privacy_profile_path=str(profile),
        # attach_file reads only from configured roots, and a test's files
        # live under tmp_path. Set here rather than per-test so a new
        # attachment test does not fail for a reason unrelated to what it
        # is checking -- and so the DEFAULT (the working tree) is never
        # silently what tests exercise.
        attachment_roots=(str(tmp_path),),
        migrations_dir=str(REPO_ROOT / "db" / "migrations"),
        port=8420,
    )


@pytest.fixture()
def call_tool():
    """Call a tool and return its payload.

    `call_tool` returns a `CallToolResult`, not the tool's own return
    value — the payload is either `structured_content` or the JSON in the
    text block. A fixture rather than an importable helper: both `api/`
    and `mcp/` have a `tests` package, so importing across them by module
    path is ambiguous, while fixtures are discovered without imports.
    """

    def _call(server, name: str, arguments: dict):
        result = anyio.run(lambda: server.call_tool(name, arguments))
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)

    return _call


@pytest.fixture()
def tools_by_name():
    def _tools(server) -> dict:
        return {tool.name: tool for tool in anyio.run(lambda: server.list_tools())}

    return _tools
