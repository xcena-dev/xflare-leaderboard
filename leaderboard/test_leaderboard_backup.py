#!/usr/bin/env python3
"""Backup / restore tests in temp dirs. Run: python3 leaderboard/test_leaderboard_backup.py

The "NAS" is a temp dir and the expected mount is whatever findmnt says that dir sits on,
so the mount check runs for real; a wrong --mount stands in for an unmounted share.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from leaderboard import aggregate, backup                           # noqa: E402
from leaderboard.fixtures import make_fixtures                      # noqa: E402
from leaderboard.store import Store                                 # noqa: E402


def _mount_of(path: Path) -> str:
    return subprocess.run(["findmnt", "-n", "-o", "TARGET", "-T", str(path)],
                          check=True, capture_output=True, text=True).stdout.strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(argv: list[str]) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc = backup.main(argv)
    return rc, err.getvalue()


class BackupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lb-backup-test-")
        root = Path(self._tmp.name)
        self.data, self.nas = root / "data", root / "nas"
        self.data.mkdir()
        self.nas.mkdir()
        self.mount = _mount_of(self.nas)
        self.store = Store(self.data)
        # Distinct timings per run (run 3 is fastest on q1, run 0 on q22) so the
        # rankings compared after restore are real orderings, not ties.
        for i in range(4):
            def elapsed(q, r, e, i=i):
                return make_fixtures.default_elapsed(q, r, e) + (i * 11 if q < 22 else -i * 13)
            m = make_fixtures.build_manifest(run_id=make_fixtures.fixture_uuid(10 + i), elapsed=elapsed)
            # A creation warning per run, so the stored receipt (receipt_json) is not empty.
            status, _ = self.store.put(make_fixtures.build_envelope(m), warnings=[f"$.w{i}: test warning"])
            self.assertEqual(status, "created")
        self.store.withdraw(make_fixtures.fixture_uuid(11), "tester", "bad booking")

    def tearDown(self):
        self._tmp.cleanup()

    def backup_argv(self, *extra: str) -> list[str]:
        return ["backup", "--data", str(self.data), "--dest", str(self.nas),
                "--mount", self.mount, *extra]

    def generations(self) -> list[Path]:
        return backup.complete_generations(self.nas)

    def test_round_trip(self):
        rc, err = _run(self.backup_argv())
        self.assertEqual((rc, err), (0, ""))
        [gen] = self.generations()
        target = Path(self._tmp.name) / "restored"
        rc, err = _run(["restore", "--from", str(gen), "--data", str(target)])
        self.assertEqual((rc, err), (0, ""))

        with sqlite3.connect(target / backup.DB_NAME) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        restored = Store(target)
        before = self.store.list_runs(include_withdrawn=True)
        after = restored.list_runs(include_withdrawn=True)
        self.assertEqual(len(after), 4)
        self.assertEqual(sorted((r["run_id"], r["content_id"], bool(r["withdrawn"])) for r in before),
                         sorted((r["run_id"], r["content_id"], bool(r["withdrawn"])) for r in after))
        for r in before:   # whole rows: meta, derived, withdrawal record and the creation receipt
            self.assertEqual(restored.get(r["run_id"]), self.store.get(r["run_id"]))
        self.assertEqual(restored.get(make_fixtures.fixture_uuid(12))["receipt"]["warnings"], ["$.w2: test warning"])
        self.assertTrue(restored.get(make_fixtures.fixture_uuid(11))["withdrawn"])
        ranks = {}
        for metric in ("total", "q1", "q22"):
            ranks[metric] = [r["run_id"] for r in aggregate.rank(restored.list_runs(), metric)]
            self.assertEqual([r["run_id"] for r in aggregate.rank(self.store.list_runs(), metric)], ranks[metric])
        self.assertEqual(len(ranks["q1"]), 3)
        self.assertEqual(ranks["q22"], ranks["q1"][::-1])
        for r in after:
            raw = target / "raw" / f"{r['content_id']}.json"
            self.assertEqual(_sha(raw), r["content_id"])
            self.assertEqual(raw.read_bytes(), (self.data / "raw" / raw.name).read_bytes())

    def test_backup_never_writes_the_source(self):
        """The source is opened read-only: it can neither write nor checkpoint. A writer that died with
        frames still in the WAL (nothing checkpointed them) is the case where a read-write connection
        closing last would checkpoint into the main file; after the backup both files are byte-identical,
        and the copy still holds the WAL-only change."""
        db = self.data / backup.DB_NAME
        writer = ("import os, sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
                  "c.execute('PRAGMA wal_autocheckpoint = 0'); "
                  "c.execute(\"UPDATE runs SET withdrawn_reason = 'wal only'\"); c.commit(); os._exit(0)")
        subprocess.run([sys.executable, "-c", writer, str(db)], check=True)
        wal = Path(f"{db}-wal")
        self.assertGreater(wal.stat().st_size, 0)
        before = (_sha(db), _sha(wal))
        gen = backup.backup(self.data, self.nas, self.mount, keep=3, stamp="20261019T000000Z")
        self.assertTrue(wal.exists(), "the backup checkpointed and removed the source WAL")
        self.assertEqual((_sha(db), _sha(wal)), before)
        with sqlite3.connect(gen / backup.DB_NAME) as c:
            self.assertEqual(c.execute("SELECT DISTINCT withdrawn_reason FROM runs").fetchall(), [("wal only",)])

    def test_failed_mount_check_is_nonzero_and_writes_nothing(self):
        wrong = "/definitely/not/a/mount"
        proc = subprocess.run([sys.executable, str(HERE / "backup.py"), "backup", "--data", str(self.data),
                               "--dest", str(self.nas), "--mount", wrong], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("expected '/definitely/not/a/mount'", proc.stderr)
        self.assertEqual(list(self.nas.iterdir()), [])

    def test_missing_destination_is_nonzero(self):
        rc, err = _run(["backup", "--data", str(self.data), "--dest", str(self.nas / "absent"),
                        "--mount", self.mount])
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", err)

    def test_corrupted_raw_is_nonzero_and_leaves_no_generation(self):
        victim = next((self.data / "raw").glob("*.json"))
        victim.write_bytes(victim.read_bytes() + b" ")
        rc, err = _run(self.backup_argv())
        self.assertEqual(rc, 1)
        self.assertIn("hash mismatch", err)
        self.assertEqual(list(self.nas.iterdir()), [])

    def test_missing_raw_is_nonzero(self):
        next((self.data / "raw").glob("*.json")).unlink()
        rc, err = _run(self.backup_argv())
        self.assertEqual(rc, 1)
        self.assertIn("raw file missing", err)
        self.assertEqual(list(self.nas.iterdir()), [])

    def test_retention_keeps_newest_and_foreign_partials(self):
        foreign = self.nas / "20200101T000000Z.partial"
        foreign.mkdir()
        unrelated = self.nas / "notes"
        unrelated.mkdir()
        stamps = [f"2026101{d}T000000Z" for d in range(5)]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for s in stamps:
                backup.backup(self.data, self.nas, self.mount, keep=3, stamp=s)
        self.assertEqual([p.name for p in self.generations()], stamps[-3:])
        self.assertTrue(foreign.is_dir())
        self.assertTrue(unrelated.is_dir())
        self.assertIn(str(foreign), err.getvalue())

    def test_existing_generation_is_not_overwritten(self):
        backup.backup(self.data, self.nas, self.mount, keep=3, stamp="20261019T000000Z")
        with self.assertRaisesRegex(backup.BackupError, "already exists"):
            backup.backup(self.data, self.nas, self.mount, keep=3, stamp="20261019T000000Z")

    def test_restore_refuses_non_empty_target(self):
        backup.backup(self.data, self.nas, self.mount, keep=3, stamp="20261019T000000Z")
        rc, err = _run(["restore", "--from", str(self.nas / "20261019T000000Z"), "--data", str(self.data)])
        self.assertEqual(rc, 1)
        self.assertIn("must be an empty directory", err)

    def test_restore_verifies_before_writing(self):
        gen = backup.backup(self.data, self.nas, self.mount, keep=3, stamp="20261019T000000Z")
        raw = next((gen / "raw").glob("*.json"))
        raw.write_bytes(b"{}")
        target = Path(self._tmp.name) / "restored"
        rc, err = _run(["restore", "--from", str(gen), "--data", str(target)])
        self.assertEqual(rc, 1)
        self.assertIn("hash mismatch", err)
        self.assertFalse(target.exists())

    def test_restore_refuses_partial_generation(self):
        gen = backup.backup(self.data, self.nas, self.mount, keep=3, stamp="20261019T000000Z")
        os.rename(gen, self.nas / "20261019T000000Z.partial")
        rc, err = _run(["restore", "--from", str(self.nas / "20261019T000000Z.partial"),
                        "--data", str(Path(self._tmp.name) / "restored")])
        self.assertEqual(rc, 1)
        self.assertIn("not a complete generation", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
