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

from . import aggregate, schema

DEFAULT_DATA_DIR = "/home/sw-devops/data/xflare_leaderboard"
DATA_DIR_ENV = "XFLARE_LEADERBOARD_DATA"
DB_NAME = "leaderboard.sqlite3"
USER_VERSION = 3
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
CREATE TABLE IF NOT EXISTS run_attribution (
    run_id TEXT PRIMARY KEY,
    basis_json TEXT,
    report_url_json TEXT,
    revision INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prset_attribution (
    prset TEXT PRIMARY KEY,
    report_url_json TEXT,
    revision INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attribution_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    field TEXT NOT NULL,
    revision INTEGER NOT NULL,
    before_json TEXT,
    after_json TEXT,
    restored_from INTEGER,
    actor TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attribution_events_subject ON attribution_events (subject_kind, subject, event_id);
CREATE TABLE IF NOT EXISTS pr_checks (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    run_ids_json TEXT NOT NULL,
    body_sha256 TEXT,
    source TEXT,
    checked_by TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (repo, number)
);
"""
# A9: what can be edited after a submission, per subject kind: (table, key column, fields). Each field is a JSON
# value in column <field>_json (NULL = none); every change is one attribution_events row.
_SUBJECTS = {"run": ("run_attribution", "run_id", ("basis", "report_url")),
             "prset": ("prset_attribution", "prset", ("report_url",))}


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


_SQL_INT_MAX = 2 ** 63 - 1


def _enc(value) -> str | None:
    """An A9 field value as stored: canonical JSON text (so equal values compare equal), NULL for none."""
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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
                    # Every statement is idempotent (IF NOT EXISTS), so an upgrade interrupted anywhere redoes it on
                    # the next start; the version follows the tables and is never lowered: a database an older
                    # binary opened meanwhile (going back a version) keeps the tables it did not know.
                    conn.executescript(_SCHEMA)
                    if conn.execute("PRAGMA user_version").fetchone()[0] < USER_VERSION:
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

    def _commit_row(self, envelope: dict, cid: str, derived: dict, receipt: dict, basis: dict | None = None) -> str:
        """``basis`` (A9, normalized) is the run's initial claim: stored with a new run, never by a retry."""
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
                    if basis is not None:
                        # The claim's actor is the submitter as the envelope names them.
                        self._write_field(conn, "run", manifest["run_id"], "basis", None, _enc(basis),
                                          envelope["submitter"]["email"] or "submission", 0, now)
                    status = "created"
                elif old["content_id"] != cid:
                    status = "conflict"
                else:
                    # Same content: refresh metadata only; identity, receipt, withdrawal and the A9 basis stay as
                    # first written (a later basis is an edit: Store.edit).
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
        basis = schema.normalize_basis(envelope["basis"]) if envelope.get("basis") is not None else None
        status = self._commit_row(envelope, cid, derived, receipt, basis)
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
            self._attach(conn, rows, "SELECT * FROM run_attribution WHERE run_id = ?", (run_id,))
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
            self._attach(conn, rows, "SELECT * FROM run_attribution", ())
        return sorted(rows, key=lambda r: (aggregate.utc_key(r["finished_at"]), r["run_id"]))

    def _attach(self, conn: sqlite3.Connection, rows: list[dict], sql: str, args) -> None:
        """Every row gains "attribution" (A9): its basis, report URL and their revision, as ``_state`` reads them."""
        found = {r["run_id"]: r for r in conn.execute(sql, args).fetchall()}
        for row in rows:
            row["attribution"] = self._state("run", found.get(row["run_id"]))

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

    @staticmethod
    def _target_row(conn: sqlite3.Connection, sf: str, codec: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM targets WHERE sf = ? AND codec = ?", (sf, codec)).fetchone()

    def get_target(self, sf: str, codec: str) -> dict | None:
        """The tab's pinned target {sf, codec, run_id, goal, revision, updated_by, updated_at}; None when never
        set. ``goal`` is the target speedup (velox / xflare) the tab aims for, or None."""
        with self._db("read") as conn:
            r = self._target_row(conn, sf, codec)
        return dict(r) if r else None

    def target_history(self, sf: str, codec: str, limit: int = 50) -> tuple[dict | None, list[dict]]:
        """The tab's target (as ``get_target``) and its changes, newest first, at most ``limit``, read in one
        transaction: the revision always belongs to the newest change listed."""
        with self._db("read") as conn:
            conn.execute("BEGIN")
            try:
                pin = self._target_row(conn, sf, codec)
                rows = conn.execute("SELECT * FROM target_events WHERE sf = ? AND codec = ? ORDER BY event_id DESC"
                                    " LIMIT ?", (sf, codec, limit)).fetchall()
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        return (dict(pin) if pin else None), [dict(r) for r in rows]

    def set_target(self, sf: str, codec: str, run_id: str | None, goal: float | None, actor: str,
                   expected_revision: int) -> tuple[str, dict | None]:
        """Pin ``run_id`` (None clears) and the goal speedup as the tab's target when its revision is still
        ``expected_revision`` (0 for a tab never set): "set" (one event appended, revision + 1), "unchanged" (already
        that run and goal: no event), "conflict" (someone changed it meanwhile) or "withdrawn" (the run was
        withdrawn, or is gone, by the time of the write); the last two write nothing. Returns the status and the
        state after. The caller checks the rest of the run's fitness, which a stored run cannot lose."""
        with self._db("target") as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._target_row(conn, sf, codec)
                revision = cur["revision"] if cur else 0
                before = (cur["run_id"], cur["goal"]) if cur else (None, None)
                live = run_id is None or conn.execute("SELECT 1 FROM runs WHERE run_id = ? AND withdrawn = 0",
                                                      (run_id,)).fetchone()
                if revision != expected_revision or not live or before == (run_id, goal):
                    conn.execute("ROLLBACK")
                    status = "conflict" if revision != expected_revision else "withdrawn" if not live else "unchanged"
                    return status, (dict(cur) if cur else None)
                now = _now()
                conn.execute("INSERT OR REPLACE INTO targets (sf, codec, run_id, goal, revision, updated_by,"
                             " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (sf, codec, run_id, goal, revision + 1, actor, now))
                conn.execute("INSERT INTO target_events (sf, codec, revision, before_run_id, after_run_id,"
                             " before_goal, after_goal, actor, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (sf, codec, revision + 1, before[0], run_id, before[1], goal, actor, now))
                state = dict(self._target_row(conn, sf, codec))
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        return "set", state

    # --- A9: basis and report URLs, versioned like the target (A8) -----------------------------------------------------

    def _state(self, kind: str, r: sqlite3.Row | None) -> dict:
        """A subject's editable fields (None when never set) with revision (0 for a subject never edited),
        updated_by and updated_at."""
        fields = _SUBJECTS[kind][2]
        if r is None:
            return {**{f: None for f in fields}, "revision": 0, "updated_by": None, "updated_at": None}
        return {**{f: self._dec(r[f + "_json"]) for f in fields}, "revision": r["revision"],
                "updated_by": r["updated_by"], "updated_at": r["updated_at"]}

    def _dec(self, text: str | None):
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise StorageUnavailable(f"leaderboard database row is not valid JSON in {self.db_path}") from None

    @staticmethod
    def _write_field(conn: sqlite3.Connection, kind: str, subject: str, field: str, before: str | None,
                     after: str | None, actor: str, revision: int, now: str, restored_from: int | None = None):
        """Set one field at ``revision`` + 1 and append its event, inside the caller's transaction."""
        table, key, fields = _SUBJECTS[kind]
        if revision:
            conn.execute(f"UPDATE {table} SET {field}_json = ?, revision = ?, updated_by = ?, updated_at = ?"
                         f" WHERE {key} = ?", (after, revision + 1, actor, now, subject))
        else:
            conn.execute(f"INSERT INTO {table} ({key}, {', '.join(f + '_json' for f in fields)}, revision, updated_by,"
                         f" updated_at) VALUES (?, {', '.join('?' for _ in fields)}, ?, ?, ?)",
                         (subject, *(after if f == field else None for f in fields), revision + 1, actor, now))
        conn.execute("INSERT INTO attribution_events (subject_kind, subject, field, revision, before_json, after_json,"
                     " restored_from, actor, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (kind, subject, field, revision + 1, before, after, restored_from, actor, now))

    def edit(self, kind: str, subject: str, field: str, value, actor: str, expected_revision: int,
             restore: int | None = None) -> tuple[str, dict | None]:
        """Set ``field`` of a run ("basis", "report_url") or a PR set ("report_url") to ``value`` (None clears) -- or, with
        ``restore``, to the value that event set -- when the subject's revision is still ``expected_revision`` (0 for
        one never edited). "set": one event appended, revision + 1; "unchanged": already that value, no event;
        "conflict": the revision moved; "unknown": no such run; "bad restore": the event is not one of this
        subject's changes to this field. Returns the status and the state after (None for "unknown"). Withdrawn runs
        stay editable."""
        table, key, _ = _SUBJECTS[kind]
        with self._db("edit") as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if kind == "run" and not conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (subject,)).fetchone():
                    conn.execute("ROLLBACK")
                    return "unknown", None
                cur = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (subject,)).fetchone()
                revision = cur["revision"] if cur else 0
                before = cur[field + "_json"] if cur else None
                after = _enc(value)
                if restore is not None:
                    event = conn.execute("SELECT after_json FROM attribution_events WHERE event_id = ?"
                                         " AND subject_kind = ? AND subject = ? AND field = ?",
                                         (restore, kind, subject, field)).fetchone()
                    after = event["after_json"] if event else None
                status = ("conflict" if revision != expected_revision else
                          "bad restore" if restore is not None and event is None else
                          "unchanged" if before == after else None)
                if status:
                    conn.execute("ROLLBACK")
                    return status, self._state(kind, cur)
                self._write_field(conn, kind, subject, field, before, after, actor, revision, _now(), restore)
                state = self._state(kind, conn.execute(f"SELECT * FROM {table} WHERE {key} = ?",
                                                       (subject,)).fetchone())
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        return "set", state

    def attribution_history(self, kind: str, subject: str, before: int | None = None,
                            limit: int = 50) -> tuple[dict, list[dict], bool]:
        """A subject's state, its changes newest first (at most ``limit``, older than event ``before`` when given),
        and whether older ones remain; read in one transaction."""
        table, key, _ = _SUBJECTS[kind]
        with self._db("read") as conn:
            conn.execute("BEGIN")
            try:
                cur = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (subject,)).fetchone()
                rows = conn.execute("SELECT * FROM attribution_events WHERE subject_kind = ? AND subject = ?"
                                    " AND event_id < ? ORDER BY event_id DESC LIMIT ?",
                                    (kind, subject, before or _SQL_INT_MAX, limit + 1)).fetchall()
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        events = [{"event_id": r["event_id"], "field": r["field"], "revision": r["revision"],
                   "before": self._dec(r["before_json"]), "after": self._dec(r["after_json"]),
                   "restored_from": r["restored_from"], "actor": r["actor"], "at": r["at"]} for r in rows[:limit]]
        return self._state(kind, cur), events, len(rows) > limit

    def prset_states(self) -> dict[str, dict]:
        """Every PR set that was ever edited, {key: state}."""
        with self._db("read") as conn:
            return {r["prset"]: self._state("prset", r) for r in conn.execute("SELECT * FROM prset_attribution")}

    def record_check(self, repo: str, number: int, run_ids: list[str], body_sha256: str | None, source: str | None,
                     actor: str) -> dict:
        """A9: the latest check of a PR's body (which run ids it lists), replacing the one before."""
        now = _now()
        with self._db("check") as conn:
            conn.execute("INSERT OR REPLACE INTO pr_checks (repo, number, run_ids_json, body_sha256, source,"
                         " checked_by, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (repo, number, json.dumps(run_ids), body_sha256, source, actor, now))
        return {"repo": repo, "number": number, "run_ids": run_ids, "body_sha256": body_sha256, "source": source,
                "checked_by": actor, "checked_at": now}

    def checks(self, keys) -> dict[tuple[str, int], dict]:
        """The latest body check of each (repo, number) in ``keys`` that has one."""
        out = {}
        with self._db("read") as conn:
            for repo, number in set(keys):
                r = conn.execute("SELECT * FROM pr_checks WHERE repo = ? AND number = ?", (repo, number)).fetchone()
                if r is not None:
                    out[(repo, number)] = {"repo": repo, "number": number, "run_ids": self._dec(r["run_ids_json"]),
                                           "body_sha256": r["body_sha256"], "source": r["source"],
                                           "checked_by": r["checked_by"], "checked_at": r["checked_at"]}
        return out

    def health(self) -> dict:
        out = {"ok": False, "db_path": str(self.db_path), "row_count": None, "last_timestamp": None, "error": None}
        try:
            with self._db("health") as conn:
                r = conn.execute("SELECT COUNT(*) AS n, MAX(updated_at) AS t FROM runs").fetchone()
            out.update(ok=True, row_count=r["n"], last_timestamp=r["t"])
        except StorageUnavailable as e:
            out["error"] = str(e)
        return out
