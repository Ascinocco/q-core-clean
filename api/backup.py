"""A WAL-safe copy of the database, taken before a statement import.

    python -m api.backup

Uses SQLite's online backup API, never a file copy: in WAL mode recent
commits live in the `-wal` sidecar, and a plain copy can silently miss them.
The copy lands in `<db directory>/backups/` (0700) as a 0600 file, is
integrity-checked, and one JSON line reports its path.

The file name's timestamp is written with separators
(`q-core-2026-09-25T10-30-05Z.db`) so the path can be recorded in a note or
ticket: the stored-text privacy guard refuses runs of seven or more digits.

    python -m api.backup --rotate

is the scheduled form (the server's nightly timer, ticket T-05): after
the copy it keeps every backup from the last 7 days and the newest of each of
the last 4 ISO weeks, and deletes the rest. On-demand copies share the
directory and age out the same way. Files that don't carry this naming are
never touched.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api.db import BUSY_TIMEOUT_SECONDS


def backup(db_path: str | Path, *, now: datetime | None = None) -> dict:
    source_path = Path(db_path)
    if not source_path.is_file():
        raise FileNotFoundError("The database file does not exist")
    directory = source_path.parent / "backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H-%M-%SZ")
    target_path = directory / f"{source_path.stem}-{stamp}.db"
    if target_path.exists():
        raise FileExistsError("A backup with this timestamp already exists")

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    integrity = None
    try:
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
        if integrity != "ok":
            # A partial or corrupt copy must not look like a backup.
            target_path.unlink(missing_ok=True)
    if integrity != "ok":
        raise RuntimeError("The backup failed its integrity check")
    return {"path": str(target_path), "bytes": target_path.stat().st_size, "integrity": integrity}


_NAME = re.compile(r"^(?P<stem>.+)-(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)\.db$")


def rotate(directory: str | Path, stem: str, *, days: int = 7, weeks: int = 4) -> list[Path]:
    """Delete this database's backups outside the retention window; return them.

    Kept: every copy made within `days` days of the newest one, and the
    newest copy of each of the `weeks` most recent ISO weeks that have one.
    Every recent copy stays, not one per day, because an on-demand copy taken
    before an import is that import's rollback point, and the same night's
    copy already includes the import. Measuring from the newest copy rather
    than the clock means a server that was off for a while still keeps its
    last week of history.
    """
    dated = []
    for path in Path(directory).iterdir():
        match = _NAME.match(path.name)
        if not match or match["stem"] != stem or not path.is_file():
            continue
        try:
            stamp = datetime.strptime(match["stamp"], "%Y-%m-%dT%H-%M-%SZ")
        except ValueError:
            continue  # well-formed but impossible (month 13): not ours, left alone
        dated.append((stamp, path))
    if not dated:
        return []
    dated.sort(reverse=True)
    horizon = dated[0][0] - timedelta(days=days)
    seen_weeks, keep = set(), set()
    for stamp, path in dated:
        if stamp > horizon:
            keep.add(path)
        week = stamp.isocalendar()[:2]
        if week not in seen_weeks and len(seen_weeks) < weeks:
            seen_weeks.add(week)
            keep.add(path)
    removed = [path for _, path in dated if path not in keep]
    for path in removed:
        path.unlink()
    return removed


def main(argv=None) -> int:
    from api.config import get_settings

    parser = argparse.ArgumentParser(prog="python -m api.backup")
    parser.add_argument("--rotate", action="store_true", help="then apply the 7-day / 4-week retention")
    args = parser.parse_args(argv)
    try:
        result = backup(get_settings().db_path)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
    if args.rotate:
        target = Path(result["path"])
        try:
            result["removed"] = len(rotate(target.parent, Path(get_settings().db_path).stem))
        except OSError as exc:
            # The copy exists; say where, and fail so the timer's onFailure fires.
            result["rotation_error"] = type(exc).__name__
            print(json.dumps(result))
            return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
