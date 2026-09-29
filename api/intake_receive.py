"""Receive an intake folder pushed from another machine (split, part 3).

    <tar of NAME/ on stdin> | python -m api.intake_receive NAME --expect-files N --expect-bytes B

The statement-intake skill runs this on the server over OpenSSH
(plugin/scripts/q-core-server.sh push). The checks run here, on the server,
because the archive comes from elsewhere. Each member is checked BEFORE
anything is written:

- a regular file or directory under `NAME/`: no symlinks, hard links,
  devices, absolute paths or `..`;
- at most MAX_FILES files, MAX_ENTRIES entries and MAX_BYTES in total,
  counted as the stream goes;
- `NAME` is one safe path component. `intake/NAME` is claimed with an
  exclusive mkdir before anything is written, so a push never overwrites or
  merges into a folder already there, even one created meanwhile.

The sender states how many files and bytes it is sending; a mismatch (a file
tar skipped, a truncated stream) refuses the push before it becomes
visible. It unpacks into a private temporary directory and renames over the
claimed empty folder, so a refused or interrupted push leaves nothing behind.
Directories are 0700 and files 0600. One JSON line reports the file names,
the count and the total size. Contents are never printed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import time
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

MAX_FILES = 500
MAX_ENTRIES = 2000
STALE_CLAIM_SECONDS = 600
MAX_BYTES = 500 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class Refused(ValueError):
    """The push is refused; messages never repeat caller-supplied text."""


def _check(member: tarfile.TarInfo, name: str) -> PurePosixPath:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != name:
        raise Refused("An archive entry is outside the named folder")
    if not (member.isfile() or member.isdir()):
        raise Refused("The folder may contain only regular files and folders (no links or devices)")
    if len(path.parts) == 1 and not member.isdir():
        raise Refused("The named folder must be a folder")
    return path


def _stale_empty_claim(target: Path) -> bool:
    """An empty real directory untouched for STALE_CLAIM_SECONDS: a killed push's claim."""
    try:
        info = target.lstat()
        return (stat.S_ISDIR(info.st_mode) and not any(target.iterdir())
                and time.time() - info.st_mtime > STALE_CLAIM_SECONDS)
    except OSError:
        return False


def receive(stream, name: str, intake_dir: str | Path, *, expect_files: int | None = None,
            expect_bytes: int | None = None) -> dict:
    if not NAME.fullmatch(name):
        raise Refused("The folder name must be letters, digits, '.', '_' or '-'")
    intake = Path(intake_dir)
    intake.mkdir(parents=True, exist_ok=True)
    target = intake / name
    try:
        os.mkdir(target, 0o700)  # claim the name: fails if anything is there
    except FileExistsError:
        if not _stale_empty_claim(target):
            raise Refused("That intake folder already exists on the server; choose another name") from None
        # A push killed mid-way (SIGKILL) left its empty claim; reclaim it.
        target.rmdir()
        try:
            os.mkdir(target, 0o700)
        except FileExistsError:
            raise Refused("That intake folder already exists on the server; choose another name") from None
    staging = Path(tempfile.mkdtemp(prefix=".push-", dir=intake))  # 0700
    files: list[tuple[str, int]] = []
    total = entries = 0
    committed = False
    try:
        with tarfile.open(fileobj=stream, mode="r|*") as archive:
            for member in archive:
                entries += 1
                if entries > MAX_ENTRIES:
                    raise Refused("The folder is larger than a push accepts")
                path = _check(member, name)
                destination = staging.joinpath(*path.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                total += member.size
                if len(files) + 1 > MAX_FILES or total > MAX_BYTES:
                    raise Refused("The folder is larger than a push accepts")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(archive.extractfile(member), out)
                files.append((str(PurePosixPath(*path.parts[1:])), member.size))
        if not files:
            raise Refused("The folder has no files")
        if (expect_files is not None and expect_files != len(files)) or (expect_bytes is not None and expect_bytes != total):
            raise Refused("The folder arrived incomplete: fewer files or bytes than were sent")
        for directory, _, _ in os.walk(staging):
            os.chmod(directory, 0o700)
        os.rename(staging / name, target)  # over the claimed, still-empty folder
        committed = True
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if not committed:
            try:
                target.rmdir()  # only the empty claim; never anything written by someone else
            except OSError:
                pass
    names = sorted(file for file, _ in files)
    return {"folder": name, "files": names, "count": len(names), "bytes": total}


def main(argv=None) -> int:
    from api.config import get_settings

    import argparse

    parser = argparse.ArgumentParser(prog="python -m api.intake_receive", add_help=False)
    parser.add_argument("name")
    parser.add_argument("--expect-files", type=int)
    parser.add_argument("--expect-bytes", type=int)
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        print(json.dumps({"error": "usage", "message": "python -m api.intake_receive NAME [--expect-files N --expect-bytes B] < archive.tar"}), file=sys.stderr)
        return 2
    try:
        print(json.dumps(receive(sys.stdin.buffer, args.name, get_settings().intake_dir,
                                 expect_files=args.expect_files, expect_bytes=args.expect_bytes)))
        return 0
    except Refused as exc:
        print(json.dumps({"error": "refused", "message": str(exc)}), file=sys.stderr)
        return 3
    except (tarfile.TarError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": "The archive could not be received"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
