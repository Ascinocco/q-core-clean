"""Receiving a pushed intake folder on the server (split, part 3). Invented files only."""
import io
import json
import stat
import tarfile

import pytest

from api.intake_receive import MAX_FILES, Refused, main, receive


def _tar(entries):
    """entries: (name, bytes | None for a directory | ('symlink', target))."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif isinstance(content, tuple):
                info.type, info.linkname = tarfile.SYMTYPE, content[1]
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    buffer.seek(0)
    return buffer


def test_a_folder_lands_private_and_only_names_come_back(tmp_path):
    archive = _tar([("sept", None), ("sept/a.csv", b"invented,1\n"), ("sept/sub", None), ("sept/sub/b.pdf", b"%PDF-invented")])
    result = receive(archive, "sept", tmp_path / "intake")
    assert result == {"folder": "sept", "files": ["a.csv", "sub/b.pdf"], "count": 2, "bytes": 24}
    assert "invented" not in json.dumps(result)
    folder = tmp_path / "intake" / "sept"
    assert (folder / "a.csv").read_bytes() == b"invented,1\n"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700 and stat.S_IMODE((folder / "sub").stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "sub" / "b.pdf").stat().st_mode) == 0o600
    assert [p.name for p in (tmp_path / "intake").iterdir()] == ["sept"]  # no staging left


@pytest.mark.parametrize("entries", [
    [("sept/link", ("symlink", "/etc/passwd"))],
    [("/etc/cron.d/x", b"x")],
    [("sept/../escape", b"x")],
    [("other/a.csv", b"x")],
    [("sept", None)],  # no files
])
def test_unsafe_archives_are_refused_and_leave_nothing(tmp_path, entries):
    with pytest.raises(Refused):
        receive(_tar(entries), "sept", tmp_path / "intake")
    assert list((tmp_path / "intake").iterdir()) == []


def test_an_existing_folder_is_never_overwritten(tmp_path):
    (tmp_path / "intake" / "sept").mkdir(parents=True)
    (tmp_path / "intake" / "sept" / "keep.csv").write_text("mine")
    with pytest.raises(Refused, match="already exists"):
        receive(_tar([("sept/keep.csv", b"theirs")]), "sept", tmp_path / "intake")
    assert (tmp_path / "intake" / "sept" / "keep.csv").read_text() == "mine"


@pytest.mark.parametrize("name", ["..", ".hidden", "a/b", "", "x" * 101, "has space"])
def test_folder_names_must_be_one_safe_component(tmp_path, name):
    with pytest.raises(Refused):
        receive(_tar([("x/a", b"1")]), name, tmp_path / "intake")


def test_too_many_files_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("api.intake_receive.MAX_FILES", 2)
    with pytest.raises(Refused, match="larger"):
        receive(_tar([(f"sept/{i}.csv", b"1") for i in range(3)]), "sept", tmp_path / "intake")
    assert list((tmp_path / "intake").iterdir()) == []


def test_cli_reports_refusals_without_echoing_the_name(test_settings, capsys, monkeypatch):
    monkeypatch.setattr("api.config.get_settings", lambda: test_settings)
    monkeypatch.setattr("sys.stdin", type("S", (), {"buffer": _tar([("x/a", b"1")])})())
    assert main(["bad name;rm"]) == 3
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "refused" and "rm" not in err["message"]


def _special(name, kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        if kind == tarfile.LNKTYPE:
            info.linkname = "sept/a.csv"
        archive.addfile(info)
    buffer.seek(0)
    return buffer


@pytest.mark.parametrize("kind", [tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_hard_links_devices_and_fifos_are_refused(tmp_path, kind):
    with pytest.raises(Refused, match="only regular files"):
        receive(_special("sept/x", kind), "sept", tmp_path / "intake")
    assert list((tmp_path / "intake").iterdir()) == []


def test_a_bare_file_named_like_the_folder_is_refused(tmp_path):
    with pytest.raises(Refused, match="must be a folder"):
        receive(_tar([("sept", b"not a folder")]), "sept", tmp_path / "intake")
    assert list((tmp_path / "intake").iterdir()) == []


def test_directory_entries_count_toward_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("api.intake_receive.MAX_ENTRIES", 3)
    with pytest.raises(Refused, match="larger"):
        receive(_tar([("sept", None)] + [(f"sept/d{i}", None) for i in range(3)] + [("sept/a.csv", b"1")]),
                "sept", tmp_path / "intake")
    assert list((tmp_path / "intake").iterdir()) == []


def test_the_byte_cap_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr("api.intake_receive.MAX_BYTES", 5)
    with pytest.raises(Refused, match="larger"):
        receive(_tar([("sept/a.csv", b"123"), ("sept/b.csv", b"456")]), "sept", tmp_path / "intake")
    assert list((tmp_path / "intake").iterdir()) == []


@pytest.mark.parametrize("expect", [{"expect_files": 2}, {"expect_bytes": 99}])
def test_an_incomplete_arrival_is_refused_before_it_is_visible(tmp_path, expect):
    """Review R1-F1: tar skipped an unreadable file and the rest was committed."""
    with pytest.raises(Refused, match="incomplete"):
        receive(_tar([("sept/a.csv", b"1")]), "sept", tmp_path / "intake", **expect)
    assert list((tmp_path / "intake").iterdir()) == []


def test_a_matching_manifest_is_accepted(tmp_path):
    result = receive(_tar([("sept/a.csv", b"12")]), "sept", tmp_path / "intake", expect_files=1, expect_bytes=2)
    assert result["count"] == 1


def test_an_empty_folder_already_there_is_not_merged_into(tmp_path):
    (tmp_path / "intake" / "sept").mkdir(parents=True)
    with pytest.raises(Refused, match="already exists"):
        receive(_tar([("sept/a.csv", b"1")]), "sept", tmp_path / "intake")
    assert (tmp_path / "intake" / "sept").is_dir()  # the existing folder is left exactly as it was


def test_a_killed_push_leaves_a_claim_that_is_reclaimed_once_stale(tmp_path, monkeypatch):
    import os
    import time
    claim = tmp_path / "intake" / "sept"
    claim.mkdir(parents=True)
    with pytest.raises(Refused, match="already exists"):  # fresh: maybe a push in progress
        receive(_tar([("sept/a.csv", b"1")]), "sept", tmp_path / "intake")
    old = time.time() - 3600
    os.utime(claim, (old, old))
    assert receive(_tar([("sept/a.csv", b"1")]), "sept", tmp_path / "intake")["count"] == 1


def test_a_stale_folder_with_content_is_never_reclaimed(tmp_path):
    import os
    import time
    folder = tmp_path / "intake" / "sept"
    folder.mkdir(parents=True)
    (folder / "keep.csv").write_text("mine")
    old = time.time() - 3600
    os.utime(folder, (old, old))
    with pytest.raises(Refused, match="already exists"):
        receive(_tar([("sept/keep.csv", b"theirs")]), "sept", tmp_path / "intake")
    assert (folder / "keep.csv").read_text() == "mine"
