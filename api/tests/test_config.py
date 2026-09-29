import pytest
from pydantic import ValidationError


def test_settings_requires_api_token(monkeypatch):
    from api.config import Settings

    monkeypatch.delenv("Q_CORE_API_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_defaults_resolve_under_repo_root():
    from api.config import REPO_ROOT, Settings

    settings = Settings(_env_file=None, api_token="test-token")

    assert settings.db_path == str(REPO_ROOT / "data" / "q-core.db")
    assert settings.documents_dir == str(REPO_ROOT / "data" / "documents")
    assert settings.schema_path == str(REPO_ROOT / "db" / "schema.sql")
    assert settings.seed_categories_path == str(
        REPO_ROOT / "db" / "seed_categories.sql"
    )
    assert settings.port == 8420


def test_env_prefix_overrides_default(monkeypatch):
    from api.config import Settings

    monkeypatch.setenv("Q_CORE_API_TOKEN", "env-token")
    monkeypatch.setenv("Q_CORE_PORT", "9999")

    settings = Settings(_env_file=None)

    assert settings.api_token == "env-token"
    assert settings.port == 9999


def test_no_env_file_and_no_environment_means_no_token(monkeypatch, tmp_path):
    from api.config import Settings

    monkeypatch.delenv("Q_CORE_API_TOKEN", raising=False)
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None)
