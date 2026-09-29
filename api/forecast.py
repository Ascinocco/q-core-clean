"""Private versioned plans; public loopback read-only scenario projections."""
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import Field

from api.auth import require_token, require_reader
from api.config import REPO_ROOT, Settings, get_settings
from api.errors import ConflictError, NotFoundError
from api.forecast_model import Model, Plan, forecast
from api.privacy import StoredText

router = APIRouter()


class PlanUpdate(Model):
    plan: Plan
    expected_revision: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    actor: StoredText = Field(min_length=1, max_length=100)
    note: StoredText = Field(min_length=1, max_length=700)


def read_plan(settings):
    root = Path(settings.forecast_dir)
    path = root / 'current.json'
    if not path.exists():
        raise NotFoundError('No forecast plan configured')
    try:
        if root.is_symlink():
            raise ValueError('Forecast directory cannot be a symlink')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
                raise ValueError('Forecast plan must be a bounded regular file')
            value = json.loads(stream.read(262145))
        Plan.model_validate(value['plan'])
        return value
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise ConflictError('Forecast plan invalid; restore a reviewed revision') from None


@router.get('/forecast/plan', dependencies=[Depends(require_token)])
def get_plan(settings: Settings = Depends(get_settings)):
    return read_plan(settings)


@router.put('/forecast/plan', dependencies=[Depends(require_token)])
def update_plan(body: PlanUpdate, settings: Settings = Depends(get_settings)):
    root = Path(settings.forecast_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(root/'write.lock',os.O_CREAT|os.O_RDWR,0o600),'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        existing = read_plan(settings) if (root/'current.json').exists() else None
        if body.expected_revision != (existing['revision'] if existing else None):
            raise ConflictError('Forecast changed; read the current plan before updating')
        value = {'plan':body.plan.model_dump(mode='json'),'actor':body.actor,'note':body.note,
                 'previous_revision':body.expected_revision,'saved_at':datetime.now(ZoneInfo('UTC')).isoformat()}
        value['revision'] = hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
        encoded = json.dumps(value,indent=2).encode()
        with os.fdopen(os.open(root/(value['revision']+'.json'),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as history:
            history.write(encoded)
            history.flush(); os.fsync(history.fileno())
        fd, temporary = tempfile.mkstemp(dir=root,prefix='.plan-')
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(encoded);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,root/'current.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return value


@router.get('/ui/forecast', include_in_schema=False, dependencies=[Depends(require_reader)])
def forecast_page():
    from api.ui import static_page
    return static_page('forecast.html', '/ui/forecast')


@router.get('/forecast/projection', dependencies=[Depends(require_reader)])
def projection(horizon_days: int = Query(default=90,ge=1,le=365),
               extra_payment_cents: int = Query(default=0,ge=0,le=100000000),
               extra_payment_date: date | None = None,
               snowball_monthly_cents: int | None = Query(default=None,ge=0,le=100000000),
               settings: Settings = Depends(get_settings)):
    stored = read_plan(settings)
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    try:
        result = forecast(Plan.model_validate(stored['plan']),today+timedelta(days=horizon_days),today,
                          extra_payment_cents,extra_payment_date,snowball_monthly_cents)
    except ValueError:
        raise ConflictError('Scenario dates or amounts invalid; refresh old snapshots or check the payment date') from None
    return JSONResponse({**result,'revision':stored['revision']},headers={'Cache-Control':'no-store'})
