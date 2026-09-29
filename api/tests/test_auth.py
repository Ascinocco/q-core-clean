import pytest
from fastapi import HTTPException


def test_require_token_rejects_missing_header(test_settings):
    from api.auth import require_token

    with pytest.raises(HTTPException) as exc_info:
        require_token(authorization=None, settings=test_settings)
    assert exc_info.value.status_code == 401


def test_require_token_rejects_wrong_token(test_settings):
    from api.auth import require_token

    with pytest.raises(HTTPException) as exc_info:
        require_token(authorization="Bearer wrong-token", settings=test_settings)
    assert exc_info.value.status_code == 401


def test_require_token_accepts_correct_token(test_settings):
    from api.auth import require_token

    require_token(
        authorization=f"Bearer {test_settings.api_token}", settings=test_settings
    )
