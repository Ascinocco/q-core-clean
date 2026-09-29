"""WAL-safe on-demand backups (split part 3: the intake skill from any machine)."""
import json
import re
import sqlite3
import stat
from datetime import datetime, timezone

import pytest

from api.backup import backup, main, rotate


def _wal_db(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")  # keep commits in the -wal sidecar
    connection.execute("CREATE TABLE t (v TEXT)")
    connection.executemany("INSERT INTO t VALUES (?)", [("row",)] * 50)
    connection.commit()
    return connection


def test_backup_includes_commits_still_in_the_wal(tmp_path):
    db = tmp_path / "q-core.db"
    writer = _wal_db(db)  # held open: nothing checkpointed into the main file
    try:
        assert (tmp_path / "q-core.db-wal").stat().st_size > 0
        result = backup(db, now=datetime(2026, 9, 25, 10, 30, 5, tzinfo=timezone.utc))
    finally:
        writer.close()
    copy = sqlite3.connect(result["path"])
    assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 50
    assert result["integrity"] == "ok"


def test_backup_is_private_and_its_name_has_no_long_digit_run(tmp_path):
    db = tmp_path / "q-core.db"
    _wal_db(db).close()
    result = backup(db, now=datetime(2026, 9, 25, 10, 30, 5, tzinfo=timezone.utc))
    assert result["path"].endswith("backups/q-core-2026-09-25T10-30-05Z.db")
    assert not re.search(r"\d{7,}", result["path"].rsplit("/", 1)[1])
    assert stat.S_IMODE((tmp_path / "backups").stat().st_mode) == 0o700
    assert stat.S_IMODE(stat.S_IMODE(__import__("os").stat(result["path"]).st_mode)) == 0o600


def test_a_second_backup_in_the_same_second_is_refused_not_overwritten(tmp_path):
    db = tmp_path / "q-core.db"
    _wal_db(db).close()
    moment = datetime(2026, 9, 25, 10, 30, 5, tzinfo=timezone.utc)
    backup(db, now=moment)
    with pytest.raises(FileExistsError):
        backup(db, now=moment)


def test_cli_prints_one_json_line(test_settings, capsys, monkeypatch):
    _wal_db(test_settings.db_path).close()
    monkeypatch.setattr("api.config.get_settings", lambda: test_settings)
    assert main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["integrity"] == "ok" and out["path"].startswith(str(test_settings.db_path).rsplit("/", 1)[0])


def test_cli_reports_a_missing_database_without_a_path(test_settings, capsys, monkeypatch):
    monkeypatch.setattr("api.config.get_settings", lambda: test_settings)
    assert main([]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "FileNotFoundError" and test_settings.db_path not in err["message"]


def test_a_failed_backup_leaves_no_partial_file(tmp_path, monkeypatch):
    db = tmp_path / "q-core.db"
    _wal_db(db).close()

    import api.backup as module
    real_connect = sqlite3.connect

    class FailingSource:
        """The read-only source connection; its backup dies part-way."""
        def __init__(self, *args, **kwargs):
            self.inner = real_connect(*args, **kwargs)
        def backup(self, target, *args, **kwargs):
            target.execute("CREATE TABLE partial (v)")  # something was written
            raise sqlite3.OperationalError("disk I/O error")
        def close(self):
            self.inner.close()

    def connect(database, *args, **kwargs):
        return FailingSource(database, *args, **kwargs) if "mode=ro" in str(database) else real_connect(database, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError):
        backup(db, now=datetime(2026, 9, 25, 10, 30, 5, tzinfo=timezone.utc))
    assert list((tmp_path / "backups").iterdir()) == []


def _touch_backups(directory, stamps, stem="q-core"):
    directory.mkdir(exist_ok=True)
    paths = {}
    for stamp in stamps:
        path = directory / f"{stem}-{stamp.strftime('%Y-%m-%dT%H-%M-%SZ')}.db"
        path.write_bytes(b"")
        paths[stamp] = path
    return paths


def test_rotation_keeps_a_week_of_copies_and_four_weekly_ones(tmp_path):
    # Sixty consecutive nightly copies ending Friday 2026-09-25, plus an
    # on-demand (pre-import) copy earlier that day.
    from datetime import timedelta

    last = datetime(2026, 9, 25, 2, 30)
    nightly = [last - timedelta(days=n) for n in range(60)]
    extra = datetime(2026, 9, 25, 1, 15)
    paths = _touch_backups(tmp_path / "backups", nightly + [extra])

    removed = rotate(tmp_path / "backups", "q-core")

    kept = {stamp for stamp, path in paths.items() if path.exists()}
    recent = set(nightly[:7]) | {extra}  # after 09-18 02:30; the pre-import copy stays
    # ISO weeks from Monday 09-21, 09-14, 09-07 and 08-31: the first two's
    # newest (09-25, 09-20) are already recent.
    weekly = {datetime(2026, 9, 13, 2, 30), datetime(2026, 9, 6, 2, 30)}
    assert kept == recent | weekly
    assert len(removed) == 61 - len(kept)


def test_rotation_measures_from_the_newest_copy_not_the_clock(tmp_path):
    # Copies on 2026-09-01..08 only, then the server was off. The week before
    # the newest copy stays; 09-01 (a Tuesday) is exactly seven days older and
    # goes, because 09-06 is newer in the same ISO week.
    stamps = [datetime(2026, 9, day, 2, 30) for day in range(1, 9)]
    paths = _touch_backups(tmp_path / "backups", stamps)
    assert rotate(tmp_path / "backups", "q-core") == [paths[stamps[0]]]


def test_rotation_of_an_empty_directory_removes_nothing(tmp_path):
    (tmp_path / "backups").mkdir()
    assert rotate(tmp_path / "backups", "q-core") == []


def test_rotation_leaves_other_files_and_other_databases_alone(tmp_path):
    directory = tmp_path / "backups"
    old = [datetime(2026, 1, day, 2, 30) for day in range(1, 29)]
    _touch_backups(directory, old, stem="other")
    (directory / "notes.txt").write_text("keep")
    (directory / "q-core-latest.db").write_bytes(b"")
    assert rotate(directory, "q-core") == []
    assert len(list(directory.iterdir())) == 30


def test_cli_rotate_reports_how_many_it_removed(test_settings, capsys, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr("api.config.get_settings", lambda: test_settings)
    _wal_db(test_settings.db_path).close()
    # Nightly copies 2025-01-01..19 plus the one main() takes now. Kept: the
    # new copy (the only one within a week of itself, and its own ISO week)
    # and the newest of the next three weeks: 01-19, 01-12 and 01-05.
    _touch_backups(Path(test_settings.db_path).parent / "backups",
                   [datetime(2025, 1, day, 2, 30) for day in range(1, 20)])
    assert main(["--rotate"]) == 0
    line = json.loads(capsys.readouterr().out)
    assert line["integrity"] == "ok" and line["removed"] == 16


def test_rotation_leaves_an_impossible_date_alone(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    odd = directory / "q-core-2026-13-45T02-30-00Z.db"  # matches the pattern, isn't a date
    odd.write_bytes(b"")
    assert rotate(directory, "q-core") == [] and odd.exists()


def test_a_failed_rotation_still_reports_the_new_copy_and_fails(test_settings, capsys, monkeypatch):
    monkeypatch.setattr("api.config.get_settings", lambda: test_settings)
    _wal_db(test_settings.db_path).close()

    def broken(*args, **kwargs):
        raise PermissionError("invented")

    monkeypatch.setattr("api.backup.rotate", broken)
    assert main(["--rotate"]) == 1
    line = json.loads(capsys.readouterr().out)
    assert line["integrity"] == "ok" and line["rotation_error"] == "PermissionError"
    assert __import__("pathlib").Path(line["path"]).is_file()
