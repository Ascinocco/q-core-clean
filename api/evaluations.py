"""Loopback-only GET summaries, no eval execution or private artifact downloads."""
import json
import os
import re
import stat
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.auth import require_reader
from api.config import REPO_ROOT, Settings, get_settings
from api.eval_summary_schema import validate_summary

router = APIRouter()


@router.get('/ui/evals', include_in_schema=False, dependencies=[Depends(require_reader)])
def evaluations_page():
    from api.ui import static_page
    return static_page('evals.html', '/ui/evals')


@router.get('/evals/runs', dependencies=[Depends(require_reader)])
def evaluation_runs(settings: Settings = Depends(get_settings)):
    root = Path(settings.eval_summaries_dir)
    runs, skipped = [], 0
    # No recursive scan, request-supplied path, raw report reads or symlink follows.
    if root.is_symlink():
        return JSONResponse({'runs': [], 'skipped': 1}, headers={'Cache-Control': 'no-store'})
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not re.fullmatch(r'[a-f0-9]{64}\.json', path.name):
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 32768:
                        raise ValueError('Not a small regular summary')
                    summary = validate_summary(json.loads(stream.read(32769)))
                if summary['report_hash'] != path.stem:
                    raise ValueError('Summary ID mismatch')
                runs.append(summary)
            except (OSError, ValueError, TypeError):
                skipped += 1
    runs.sort(key=lambda r: (r['created_at'], r['report_hash']), reverse=True)
    return JSONResponse({'runs': runs, 'skipped': skipped}, headers={'Cache-Control': 'no-store'})
