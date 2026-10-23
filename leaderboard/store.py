"""SQLite (WAL) rows + raw manifest files under one data dir. Opened lazily; every I/O failure is StorageUnavailable.

Layout: <data>/leaderboard.sqlite3, <data>/raw/<content_id>.json whose bytes are exactly the canonical manifest, so
sha256(file) == content_id. put() makes the raw file durable (tmp + fsync + rename + dir fsync) before the row: a crash
in between leaves a harmless orphan that the retry reuses. Mutable envelope metadata lives only in the row.

Every SQLite connection of this process (reads, one-time init, writes and the checkpoint on close) runs under one
lock, so in-process connections never overlap; the read-only backup process relies on busy_timeout.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import stat
import tempfile
import threading
from datetime import datetime, timezone

from . import aggregate

DEFAULT_DATA_DIR = "/home/sw-devops/data/xflare_leaderboard"
DATA_DIR_ENV = "XFLARE_LEADERBOARD_DATA"
DB_NAME = "leaderboard.sqlite3"
USER_VERSION = 2
_RAW_NAME = re.compile(r"[0-9a-f]{64}\.json")
_LOCK = threading.RLock()
_stores: dict[str, "Store"] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL,
    sf TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    submitter_email TEXT,
    total_eligible INTEGER NOT NULL,
    meta_json TEXT NOT NULL,
    derived_json TEXT NOT NULL,
    derive_version INTEGER NOT NULL,
    withdrawn INTEGER NOT NULL DEFAULT 0,
    withdrawn_at TEXT,
    withdrawn_by TEXT,
    withdrawn_reason TEXT,
    receipt_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_sf ON runs (sf);
CREATE TABLE IF NOT EXISTS targets (
    sf TEXT NOT NULL,
    codec TEXT NOT NULL,
    run_id TEXT,
    goal REAL,
    revision INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (sf, codec)
);
CREATE TABLE IF NOT EXISTS target_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sf TEXT NOT NULL,
    codec TEXT NOT NULL,
    revision INTEGER NOT NULL,
    before_run_id TEXT,
    after_run_id TEXT,
    before_goal REAL,
    after_goal REAL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS target_events_tab ON target_events (sf, codec, event_id);
"""


class StorageUnavailable(Exception):
    pass


def resolve_data_dir(cfg: dict | None = None) -> str:
    """env XFLARE_LEADERBOARD_DATA > config.yaml leaderboard.storage.data_dir > DEFAULT_DATA_DIR."""
    return (os.environ.get(DATA_DIR_ENV) or ((cfg or {}).get("storage") or {}).get("data_dir")
            or DEFAULT_DATA_DIR)


def store_for(cfg: dict | None) -> "Store":
    """One Store per resolved data dir, shared by every request of the service."""
    path = resolve_data_dir(cfg)
    with _LOCK:
        if path not in _stores:
            _stores[path] = Store(path)
        return _stores[path]


def _fsync_dir(path: pathlib.Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, data_dir: str | os.PathLike, busy_timeout_ms: int = 5000):
        self.data_dir = pathlib.Path(data_dir)
        self.db_path = self.data_dir / DB_NAME
        self.raw_dir = self.data_dir / "raw"
        self.busy_timeout_ms = busy_timeout_ms
        self._ready = False

    @contextlib.contextmanager
    def _guard(self, what: str):
        try:
            yield
        except (sqlite3.Error, OSError) as e:
            raise StorageUnavailable(f"leaderboard storage {what} failed ({type(e).__name__}) at {self.data_dir}") \
                from e

    def _connect(self) -> sqlite3.Connection:
        with self._guard("open"):
            # The data dir is provisioned by the operator; creating it here would hide a wrong mount/config.
            # Inside the guard: is_dir() raises PermissionError under a non-traversable parent.
            if not self.data_dir.is_dir() or not os.access(self.data_dir, os.W_OK | os.X_OK):
                raise StorageUnavailable(f"leaderboard data dir missing or not writable: {self.data_dir}")
            conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
                if conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                    raise StorageUnavailable(f"leaderboard database refuses WAL mode: {self.db_path}")
                # FULL: a 201 receipt must survive power loss (the raw file is already fsynced); uploads are rare.
                conn.execute("PRAGMA synchronous = FULL")
                if not self._ready:
                    conn.executescript(_SCHEMA)
                    conn.execute(f"PRAGMA user_version = {USER_VERSION}")
                    self._ready = True
            except BaseException:
                conn.close()
                raise
        return conn

    @contextlib.contextmanager
    def _db(self, what: str):
        with _LOCK:
            conn = self._connect()
            try:
                with self._guard(what):
                    yield conn
            finally:
                with self._guard("close"):
                    conn.close()

    def _open_raw(self, cid: str):
        """Read-only fd of raw/<cid>.json, never through a symlink; None when absent, a symlink or not a regular file.
        O_NONBLOCK keeps a FIFO planted there from blocking the open; S_ISREG is checked before any read.
        Any other OSError propagates (the caller's guard turns it into StorageUnavailable)."""
        if not isinstance(cid, str) or not _RAW_NAME.fullmatch(f"{cid}.json"):
            return None
        try:
            fd = os.open(self.raw_dir / f"{cid}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ELOOP):
                return None
            raise
        try:
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
        except BaseException:
            os.close(fd)
            raise
        if not regular:
            os.close(fd)
            return None
        return fd

    def _read_raw_file(self, cid: str) -> bytes | None:
        with self._guard("raw read"):
            fd = self._open_raw(cid)
            if fd is None:
                return None
            with os.fdopen(fd, "rb") as f:
                data = f.read()
        if hashlib.sha256(data).hexdigest() != cid:
            raise StorageUnavailable(f"leaderboard raw file does not match its content_id in {self.raw_dir}")
        return data

    def _write_raw(self, cid: str, data: bytes) -> pathlib.Path:
        path = self.raw_dir / f"{cid}.json"
        with self._guard("raw write"):
            self.raw_dir.mkdir(exist_ok=True)
            fd = self._open_raw(cid)
            if fd is not None:
                with os.fdopen(fd, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() == cid:
                        # Orphan of a crashed or concurrent attempt, possibly interrupted before its fsyncs:
                        # finish both barriers before the row may point at it.
                        os.fsync(f.fileno())
                        _fsync_dir(self.raw_dir)
                        return path
            fd, tmp = tempfile.mkstemp(prefix=f".{cid}.", suffix=".tmp", dir=self.raw_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            _fsync_dir(self.raw_dir)
        return path

    def _commit_row(self, envelope: dict, cid: str, derived: dict, receipt: dict) -> str:
        manifest = envelope["manifest"]
        meta = json.dumps({k: v for k, v in envelope.items() if k != "manifest"}, ensure_ascii=False)
        now = _now()
        mutable = (meta, envelope["submitter"]["email"], json.dumps(derived, ensure_ascii=False),
                   aggregate.DERIVE_VERSION, int(derived["total_eligible"]), now)
        with self._db("write") as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute("SELECT content_id FROM runs WHERE run_id = ?", (manifest["run_id"],)).fetchone()
                if old is None:
                    conn.execute(
                        "INSERT INTO runs (meta_json, submitter_email, derived_json, derive_version, total_eligible,"
                        " updated_at, run_id, content_id, sf, finished_at, submitted_at, receipt_json)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        mutable + (manifest["run_id"], cid, derived["sf"], derived["finished_at"], now,
                                   json.dumps(receipt, ensure_ascii=False)))
                    status = "created"
                elif old["content_id"] != cid:
                    status = "conflict"
                else:
                    # Same content: refresh metadata only; identity, receipt and withdrawal stay as first written.
                    conn.execute(
                        "UPDATE runs SET meta_json = ?, submitter_email = ?, derived_json = ?, derive_version = ?,"
                        " total_eligible = ?, updated_at = ? WHERE run_id = ?",
                        mutable + (manifest["run_id"],))
                    status = "existing"
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        return status

    def put(self, envelope: dict, warnings=()) -> tuple[str, dict]:
        """Validated envelope → ("created" | "existing" | "conflict", stored row). A conflict changes nothing.
        The row's "receipt" ({ranked_total, warnings}) is the one recorded at creation; "existing" replays it."""
        manifest = envelope["manifest"]
        raw = aggregate.canonical_manifest_bytes(manifest)
        cid = hashlib.sha256(raw).hexdigest()
        derived = aggregate.derive(manifest, envelope, manifest_content_id=cid)
        receipt = {"ranked_total": derived["total_eligible"], "warnings": list(warnings)}
        self._write_raw(cid, raw)
        status = self._commit_row(envelope, cid, derived, receipt)
        return status, self.get(manifest["run_id"])

    def _row(self, conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
        if r["derive_version"] != aggregate.DERIVE_VERSION:
            try:
                meta = json.loads(r["meta_json"])
            except ValueError:
                raise StorageUnavailable(f"leaderboard database row is not valid JSON in {self.db_path}") from None
            derived = aggregate.derive(self._read_manifest(r["content_id"]), meta,
                                       manifest_content_id=r["content_id"])
            conn.execute("UPDATE runs SET derived_json = ?, derive_version = ?, total_eligible = ?"
                         " WHERE run_id = ?",
                         (json.dumps(derived, ensure_ascii=False), aggregate.DERIVE_VERSION,
                          int(derived["total_eligible"]), r["run_id"]))
            r = conn.execute("SELECT * FROM runs WHERE run_id = ?", (r["run_id"],)).fetchone()
        try:
            derived, meta, receipt = (json.loads(r[k]) for k in ("derived_json", "meta_json", "receipt_json"))
        except ValueError:
            raise StorageUnavailable(f"leaderboard database row is not valid JSON in {self.db_path}") from None
        return {
            "run_id": r["run_id"], "content_id": r["content_id"], "sf": r["sf"], "finished_at": r["finished_at"],
            "submitted_at": r["submitted_at"], "updated_at": r["updated_at"], "submitter_email": r["submitter_email"],
            "total_eligible": derived["total_eligible"], "meta": meta, "derived": derived,
            "withdrawn": bool(r["withdrawn"]), "withdrawn_at": r["withdrawn_at"], "withdrawn_by": r["withdrawn_by"],
            "withdrawn_reason": r["withdrawn_reason"], "receipt": receipt,
        }

    def _rows(self, conn: sqlite3.Connection, sql: str, args) -> list[dict]:
        records = conn.execute(sql, args).fetchall()
        if not any(r["derive_version"] != aggregate.DERIVE_VERSION for r in records):
            return [self._row(conn, r) for r in records]
        del records
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read after acquiring the write lock: another process may have refreshed rows or metadata.
            rows = [self._row(conn, r) for r in conn.execute(sql, args).fetchall()]
            conn.execute("COMMIT")
            return rows
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def get(self, run_id: str) -> dict | None:
        with self._db("read") as conn:
            rows = self._rows(conn, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
            return rows[0] if rows else None

    def list_runs(self, sf: str | None = None, include_withdrawn: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM runs WHERE 1 = 1", []
        if sf is not None:
            sql += " AND sf = ?"
            args.append(sf)
        if not include_withdrawn:
            sql += " AND withdrawn = 0"
        with self._db("read") as conn:
            rows = self._rows(conn, sql, args)
        return sorted(rows, key=lambda r: (aggregate.utc_key(r["finished_at"]), r["run_id"]))

    def _read_manifest(self, cid: str) -> dict:
        data = self._read_raw_file(cid)
        if data is None:
            raise StorageUnavailable(f"leaderboard raw file missing for a stored run in {self.raw_dir}")
        return json.loads(data)

    def _content_id(self, run_id: str) -> str | None:
        with self._db("read") as conn:
            r = conn.execute("SELECT content_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return r["content_id"] if r else None

    def read_raw(self, run_id: str) -> bytes | None:
        """The run's whole stored canonical file (hash-checked). None: no row, file missing, a symlink or not a
        regular file. Any other I/O failure is StorageUnavailable."""
        cid = self._content_id(run_id)
        return None if cid is None else self._read_raw_file(cid)

    def load_manifest(self, run_id: str) -> dict | None:
        cid = self._content_id(run_id)
        return None if cid is None else self._read_manifest(cid)

    def withdraw(self, run_id: str, actor: str, reason: str) -> dict | None:
        """Soft flag, idempotent: the first withdrawal's time/actor/reason are kept. None for an unknown run."""
        with self._db("withdraw") as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                cur = conn.execute(
                    "UPDATE runs SET withdrawn = 1, withdrawn_at = ?, withdrawn_by = ?, withdrawn_reason = ?,"
                    " updated_at = ? WHERE run_id = ? AND withdrawn = 0", (now, actor, reason, now, run_id))
                found = cur.rowcount or conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        return self.get(run_id) if found else None

    def get_target(self, sf: str, codec: str) -> dict | None:
        """The tab's pinned target {sf, codec, run_id, goal, revision, updated_by, updated_at}; None when never
        set. ``goal`` is the target speedup (velox / xflare) the tab aims for, or None."""
        with self._db("read") as conn:
            r = conn.execute("SELECT * FROM targets WHERE sf = ? AND codec = ?", (sf, codec)).fetchone()
        return dict(r) if r else None

    def target_history(self, sf: str, codec: str, limit: int = 50) -> list[dict]:
        """The tab's target changes, newest first, at most ``limit``."""
        with self._db("read") as conn:
            rows = conn.execute("SELECT * FROM target_events WHERE sf = ? AND codec = ? ORDER BY event_id DESC"
                                " LIMIT ?", (sf, codec, limit)).fetchall()
        return [dict(r) for r in rows]

    def set_target(self, sf: str, codec: str, run_id: str | None, goal: float | None, actor: str,
                   expected_revision: int) -> tuple[str, dict | None]:
        """Pin ``run_id`` (None clears) and the goal speedup as the tab's target when its revision is still
        ``expected_revision`` (0 for a tab never set): "set" (one event appended, revision + 1), "unchanged" (already
        that run and goal: no event) or "conflict" (someone changed it meanwhile: nothing written). Returns the
        status and the state after."""
        with self._db("target") as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute("SELECT * FROM targets WHERE sf = ? AND codec = ?", (sf, codec)).fetchone()
                revision = cur["revision"] if cur else 0
                before = (cur["run_id"], cur["goal"]) if cur else (None, None)
                if revision != expected_revision or before == (run_id, goal):
                    conn.execute("ROLLBACK")
                    return ("conflict" if revision != expected_revision else "unchanged"), (dict(cur) if cur else None)
                now = _now()
                conn.execute("INSERT OR REPLACE INTO targets (sf, codec, run_id, goal, revision, updated_by,"
                             " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (sf, codec, run_id, goal, revision + 1, actor, now))
                conn.execute("INSERT INTO target_events (sf, codec, revision, before_run_id, after_run_id,"
                             " before_goal, after_goal, actor, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (sf, codec, revision + 1, before[0], run_id, before[1], goal, actor, now))
                state = dict(conn.execute("SELECT * FROM targets WHERE sf = ? AND codec = ?", (sf, codec)).fetchone())
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        return "set", state

    def health(self) -> dict:
        out = {"ok": False, "db_path": str(self.db_path), "row_count": None, "last_timestamp": None, "error": None}
        try:
            with self._db("health") as conn:
                r = conn.execute("SELECT COUNT(*) AS n, MAX(updated_at) AS t FROM runs").fetchone()
            out.update(ok=True, row_count=r["n"], last_timestamp=r["t"])
        except StorageUnavailable as e:
            out["error"] = str(e)
        return out
