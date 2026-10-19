#!/usr/bin/env python3
"""Backup and restore of the leaderboard data dir (stdlib only).

    backup.py backup  --data DIR --dest NAS_DIR [--mount /mnt/sw_nas_ext] [--keep 14]
    backup.py restore --from GENERATION_DIR --data EMPTY_DIR

A generation is `<dest>/<UTC stamp>/` holding `leaderboard.sqlite3`, `raw/<content_id>.json`
for every row and `BACKUP.json`. It is built as `<stamp>.partial/` and renamed only after
every check passed, so a directory with the final name and a BACKUP.json is complete.

Retention keeps the newest --keep complete generations and deletes older ones. Nothing
else under dest is ever deleted: a failed run removes its own `.partial`, while a
`.partial` left by another (crashed or concurrent) run is reported and kept for a human
to look at.

Exit status is non-zero with a one-line reason on any failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DB_NAME = "leaderboard.sqlite3"
MARKER = "BACKUP.json"
FORMAT = "xflare-leaderboard-backup/1"
STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
CONTENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class BackupError(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_durable(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)
    with open(dst, "rb+") as f:
        os.fsync(f.fileno())


def _open_ro(path: Path) -> sqlite3.Connection:
    # immutable=1: nobody writes a backup copy, and it may sit on a network
    # share whose locking SQLite should not depend on.
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _check_db(path: Path) -> list[str]:
    """integrity_check, then the content ids the DB references."""
    conn = _open_ro(path)
    try:
        result = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            raise BackupError(f"integrity_check failed on {path}: {result[:3]}")
        ids = sorted({r[0] for r in conn.execute("SELECT content_id FROM runs")})
    except sqlite3.Error as e:
        raise BackupError(f"cannot read {path}: {e}") from None
    finally:
        conn.close()
    for cid in ids:
        if not isinstance(cid, str) or not CONTENT_ID_RE.match(cid):
            raise BackupError(f"{path}: content_id {str(cid)[:80]!r} is not 64 lowercase hex")
    return ids


def _verify_raw(raw_dir: Path, ids: list[str]) -> None:
    for cid in ids:
        f = raw_dir / f"{cid}.json"
        if not f.is_file():
            raise BackupError(f"raw file missing: {f}")
        if _sha256(f) != cid:
            raise BackupError(f"raw file hash mismatch: {f}")


def check_mount(dest: Path, mount: str) -> None:
    """dest must exist and live on `mount`, so an unmounted NAS never fills the local disk."""
    if not dest.is_dir():
        raise BackupError(f"destination {dest} does not exist (create it once on the mounted share)")
    try:
        out = subprocess.run(["findmnt", "-n", "-o", "TARGET", "-T", str(dest)],
                             check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise BackupError(f"findmnt failed for {dest}: {e}") from None
    if os.path.realpath(out) != os.path.realpath(mount):
        raise BackupError(f"{dest} is on mount {out!r}, expected {mount!r}; is the share mounted?")


def complete_generations(dest: Path) -> list[Path]:
    return sorted(p for p in dest.iterdir()
                  if p.is_dir() and STAMP_RE.match(p.name) and (p / MARKER).is_file())


def backup(data_dir: Path, dest: Path, mount: str, keep: int, stamp: str | None = None) -> Path:
    if keep < 1:
        raise BackupError("--keep must be at least 1")
    db = data_dir / DB_NAME
    if not db.is_file():
        raise BackupError(f"no database at {db}")
    check_mount(dest, mount)

    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = dest / stamp
    partial = dest / f"{stamp}.partial"
    if final.exists():
        raise BackupError(f"generation {final} already exists")
    try:
        partial.mkdir()
    except OSError as e:
        raise BackupError(f"cannot create {partial}: {e}") from None
    try:
        with tempfile.TemporaryDirectory(prefix="lb-backup-") as tmp:
            # The online backup runs against local disk: the copy comes out in WAL
            # mode, and WAL needs shared memory a network share may not offer. The
            # NAS only ever receives a finished rollback-journal file.
            snap = Path(tmp) / DB_NAME
            # Read-only: this connection can neither write nor checkpoint the live
            # DB, even when it is the last one to close (the service's own connections
            # are serialised in store.py; this one is outside that lock).
            src = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
            dst = sqlite3.connect(snap)
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                user_version = dst.execute("PRAGMA user_version").fetchone()[0]
                rows = dst.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            except sqlite3.Error as e:
                raise BackupError(f"online backup of {db} failed: {e}") from None
            finally:
                dst.close()
                src.close()
            _copy_durable(snap, partial / DB_NAME)

        db_sha = _sha256(partial / DB_NAME)
        ids = _check_db(partial / DB_NAME)
        (partial / "raw").mkdir()
        for cid in ids:
            src_raw = data_dir / "raw" / f"{cid}.json"
            if not src_raw.is_file():
                raise BackupError(f"raw file missing: {src_raw}")
            _copy_durable(src_raw, partial / "raw" / src_raw.name)
        _verify_raw(partial / "raw", ids)
        _fsync_dir(partial / "raw")

        marker = {
            "format": FORMAT,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": socket.gethostname(),
            "source": str(data_dir),
            "sqlite_version": sqlite3.sqlite_version,
            "db": {"file": DB_NAME, "sha256": db_sha, "user_version": user_version, "rows": rows},
            "raw_files": len(ids),
        }
        with open(partial / MARKER, "w", encoding="utf-8") as f:
            json.dump(marker, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(partial)
        os.rename(partial, final)
        _fsync_dir(dest)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise

    for old in complete_generations(dest)[:-keep]:
        shutil.rmtree(old)
    for p in sorted(dest.glob("*.partial")):
        print(f"backup: warning: incomplete generation left by another run: {p}", file=sys.stderr)
    return final


def verify_generation(gen: Path) -> tuple[dict, list[str]]:
    marker_path = gen / MARKER
    if not STAMP_RE.match(gen.name) or not marker_path.is_file():
        raise BackupError(f"{gen} is not a complete generation (no {MARKER} or not a final name)")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        want_sha = marker["db"]["sha256"]
    except (ValueError, KeyError, TypeError) as e:
        raise BackupError(f"unreadable {marker_path}: {e}") from None
    if marker.get("format") != FORMAT:
        raise BackupError(f"{marker_path}: unknown format {str(marker.get('format'))[:40]!r}")
    if _sha256(gen / DB_NAME) != want_sha:
        raise BackupError(f"{gen / DB_NAME}: sha256 differs from {MARKER}")
    ids = _check_db(gen / DB_NAME)
    _verify_raw(gen / "raw", ids)
    return marker, ids


def restore(gen: Path, data_dir: Path) -> None:
    if data_dir.exists() and (not data_dir.is_dir() or any(data_dir.iterdir())):
        raise BackupError(f"restore target {data_dir} must be an empty directory")
    marker, ids = verify_generation(gen)
    data_dir.mkdir(exist_ok=True)
    (data_dir / "raw").mkdir()
    for cid in ids:
        _copy_durable(gen / "raw" / f"{cid}.json", data_dir / "raw" / f"{cid}.json")
    _verify_raw(data_dir / "raw", ids)
    _fsync_dir(data_dir / "raw")
    # The DB goes in last under its final name, so an interrupted restore never
    # leaves a dir the service would open as a (partial) leaderboard.
    tmp = data_dir / f".{DB_NAME}.restore"
    _copy_durable(gen / DB_NAME, tmp)
    if _sha256(tmp) != marker["db"]["sha256"]:
        raise BackupError(f"{tmp}: copy differs from the generation")
    os.rename(tmp, data_dir / DB_NAME)
    _fsync_dir(data_dir)
    _check_db(data_dir / DB_NAME)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backup / restore of the xflare leaderboard data dir")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="write one verified generation to the NAS")
    b.add_argument("--data", required=True, type=Path, help="leaderboard data dir")
    b.add_argument("--dest", required=True, type=Path, help="existing backup root on the share")
    b.add_argument("--mount", default="/mnt/sw_nas_ext", help="mount target dest must sit on")
    b.add_argument("--keep", type=int, default=14, help="complete generations to keep")
    r = sub.add_parser("restore", help="restore one generation into an empty data dir")
    r.add_argument("--from", dest="gen", required=True, type=Path, help="generation dir")
    r.add_argument("--data", required=True, type=Path, help="empty (or absent) target data dir")
    args = p.parse_args(argv)
    try:
        if args.cmd == "backup":
            gen = backup(args.data, args.dest, args.mount, args.keep)
            print(f"backup: ok {gen}")
        else:
            restore(args.gen, args.data)
            print(f"restore: ok {args.gen} -> {args.data}")
    except (BackupError, OSError) as e:
        print(f"{args.cmd}: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
