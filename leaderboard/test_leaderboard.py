#!/usr/bin/env python3
"""Leaderboard tests (stdlib unittest). Run: python3 leaderboard/test_leaderboard.py

The single runner: it also loads test_leaderboard_web.py, test_leaderboard_backup.py and
test_leaderboard_auto_update.py (all required).
Covers validator accept/reject, eligibility, medians/ordering/hero, and Store identity, withdraw and crash safety.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shlex
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from datetime import timedelta
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE / "fixtures"))

import make_fixtures as mf                                          # noqa: E402
from leaderboard import aggregate as agg                            # noqa: E402
from leaderboard import repro                                       # noqa: E402
from leaderboard import schema                                      # noqa: E402
from leaderboard import store as store_mod                           # noqa: E402
from leaderboard.store import StorageUnavailable, Store, store_for  # noqa: E402

FIX = HERE / "fixtures"
NOT_ROOT = unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
BIG = 10 ** 400  # a valid JSON integer (the parser caps at 4300 digits) far beyond float range


def load(name):
    return json.loads((FIX / name).read_bytes())


def row(env, withdrawn=False):
    d = agg.derive(env["manifest"], env)
    return {"run_id": d["run_id"], "content_id": d["content_id"], "derived": d, "withdrawn": withdrawn}


def const_elapsed(xflare, velox=None):
    """Every query/repeat takes xflare (and velox) ms; values may be lists indexed by repeat."""
    def f(q, r, engine):
        v = xflare if engine == "xflare" else (velox if velox is not None else 2 * xflare if isinstance(xflare, int)
                                               else [2 * x for x in xflare])
        return v[r - 1] if isinstance(v, list) else v
    return f


class Fixtures(unittest.TestCase):
    def test_committed_fixtures_match_generator(self):
        for name, data in mf.render_all().items():
            self.assertEqual((FIX / name).read_bytes(), data, f"{name} stale: rerun make_fixtures.py")


    def test_runner_requires_every_suite(self):
        """A missing web/backup/auto-update suite fails the single runner instead of being skipped."""
        with mock.patch.dict(globals(), {"SUITES": ("test_leaderboard_missing",)}):
            with self.assertRaises(FileNotFoundError):
                load_tests(unittest.TestLoader(), unittest.TestSuite(), None)


class Validator(unittest.TestCase):
    """Validator accept/reject (the HTTP 8 MiB cap is in test_leaderboard_web.py)."""

    def test_accept_contract_fixtures(self):
        for name in ("valid-envelope.json", "valid-shared-envelope.json", "velox-mismatch.json",
                     "partial.json", "recording-mode.json"):
            obj = schema.parse_json_strict((FIX / name).read_bytes())
            self.assertEqual(schema.validate_envelope(obj), ([], []), name)
        self.assertEqual(schema.validate_manifest(load("valid-manifest.json")), ([], []))

    def test_reject_fixture_files(self):
        for name in ("reject-duplicate-key.json", "reject-nan.json"):
            with self.assertRaises(schema.BadJSON, msg=name):
                schema.parse_json_strict((FIX / name).read_bytes())
        errs, _ = schema.validate_envelope(load("reject-negative-elapsed.json"))
        self.assertEqual(errs, ["$.manifest.repeats[0].queries[0].engines.xflare.elapsed_ms: -5 outside 0.."])
        errs, _ = schema.validate_envelope(load("reject-unknown-enum.json"))
        self.assertEqual(errs, ["$.manifest.run.codec: 'lz4' not one of snappy|zstd|uncompressed"])

    def test_strict_json(self):
        bad = [b'{"a":1,"a":2}', b'{"a":{"b":1,"b":1}}', b'[{"x":1,"x":1}]', b'{"a":NaN}', b'{"a":Infinity}',
               b'{"a":-Infinity}', b'{"a":1e400}', b'\xff\xfe', b'{"a":"\\ud800"}', b"[" * 100000, b"",
               b'\xef\xbb\xbf{}', b'{"a":1} x']
        for body in bad:
            with self.assertRaises(schema.BadJSON, msg=body[:30]):
                schema.parse_json_strict(body)
        self.assertEqual(schema.parse_json_strict('{"n":"é","f":1.5}'.encode()), {"n": "é", "f": 1.5})

    def rejects(self, mutate, expect):
        env = copy.deepcopy(load("valid-envelope.json"))
        mutate(env, env["manifest"])
        errs, _ = schema.validate_envelope(env)
        self.assertTrue(any(expect in e for e in errs), f"{expect!r} not in {errs}")

    def test_reject_cases(self):
        r0q0 = lambda m: m["repeats"][0]["queries"][0]                          # noqa: E731
        cases = [
            (lambda e, m: e.update(schema="xflare-bench-submission/2"), "$.schema: expected"),
            (lambda e, m: m.update(schema="xflare-bench-manifest/0"), "$.manifest.schema: expected"),
            (lambda e, m: r0q0(m)["engines"]["xflare"].update(elapsed_ms=1.5), "elapsed_ms: expected integer"),
            (lambda e, m: r0q0(m)["engines"]["xflare"].update(elapsed_ms=float("nan")), "elapsed_ms: expected int"),
            (lambda e, m: r0q0(m)["engines"]["velox"].update(elapsed_ms="12"), "elapsed_ms: expected integer"),
            (lambda e, m: m["run"]["resources"].update(executors=True), "executors: expected integer, got boolean"),
            (lambda e, m: m["run"]["resources"].update(executors=0), "executors: 0 outside 1.."),
            (lambda e, m: m["run"].update(repeat_requested=False), "repeat_requested: expected integer"),
            (lambda e, m: m["run"].update(record_answers=0), "record_answers: expected boolean"),
            (lambda e, m: r0q0(m)["engines"]["xflare"].update(status="fast"), "status: 'fast' not one of"),
            (lambda e, m: r0q0(m)["engines"]["xflare"]["verify"].update(status="meh"), "verify.status: 'meh'"),
            (lambda e, m: m["repeats"][0].update(status="done"), "repeats[0].status"),
            (lambda e, m: m["repeats"][0].update(cleanup="maybe"), "repeats[0].cleanup"),
            (lambda e, m: m["build"]["bundle"].update(kind="beta"), "bundle.kind"),
            (lambda e, m: m["build"].update(provenance_source="git"), "provenance_source"),
            (lambda e, m: m["run"].update(exit_status="crashed"), "exit_status"),
            (lambda e, m: e.update(isolation_claim="mostly"), "$.isolation_claim"),
            (lambda e, m: e["environment"].update(booking_overlap="maybe"), "booking_overlap"),
            (lambda e, m: e["environment"].update(source="guess"), "environment.source"),
            (lambda e, m: e["environment"].update(cpu_contention="low"), "cpu_contention"),
            (lambda e, m: m["run"]["xflare_overrides"][0].update(source="env"), "xflare_overrides[0].source"),
            (lambda e, m: m.update(created_at="2026-10-19 01:40:55"), "created_at: '2026-10-19 01:40:55' is not"),
            (lambda e, m: m.update(created_at="2026-10-19T01:40:55+09:00"), "created_at"),
            (lambda e, m: m.update(created_at="2026-13-19T01:40:55Z"), "not a valid date/time"),
            (lambda e, m: m["run"].update(finished_at=None), "run.finished_at: expected string"),
            (lambda e, m: m["client"].update(revision="ABC" + "0" * 37), "client.revision"),
            (lambda e, m: m["client"].update(revision="0" * 39), "client.revision"),
            (lambda e, m: m["run"]["xflare_toml"].update(input_sha256="x" * 64), "input_sha256"),
            (lambda e, m: m["run"]["answers"].update(sha256="0" * 63), "answers.sha256"),
            (lambda e, m: m.update(run_id="not-a-uuid"), "run_id"),
            (lambda e, m: m.update(run_id="00000000-0000-1000-8000-000000000001"), "run_id"),
            (lambda e, m: m["run"].update(queries=[0, 1]), "run.queries[0]: 0 outside 1..22"),
            (lambda e, m: m["run"].update(queries=[1, 23]), "run.queries[1]: 23 outside 1..22"),
            (lambda e, m: m["run"].update(queries=[1, 1]), "duplicate query 1"),
            (lambda e, m: r0q0(m).update(id=23), "queries[0].id: 23 outside"),
            (lambda e, m: m["run"].update(engines=["velox", "spark"]), "engines[1]: 'spark' not one of"),
            (lambda e, m: m["run"].update(engines=["xflare", "xflare"]), "duplicate engine xflare"),
            (lambda e, m: m["run"].update(engines=[]), "run.engines: empty"),
            (lambda e, m: r0q0(m)["engines"].update(spark=r0q0(m)["engines"]["velox"]), "engine 'spark'"),
            (lambda e, m: m["repeats"][0]["queries"].append(copy.deepcopy(r0q0(m))), "duplicate query 1 in one"),
            (lambda e, m: m["repeats"][0].update(index=0), "repeats[0].index: 0 outside 1..3"),
            (lambda e, m: m["repeats"][0].update(index=4), "repeats[0].index: 4 outside 1..3"),
            (lambda e, m: m["repeats"][1].update(index=1), "duplicate repeat index 1"),
            (lambda e, m: m["run"].pop("codec"), "$.manifest.run.codec: missing"),
            (lambda e, m: e.pop("environment"), "$.environment: missing"),
            (lambda e, m: e.update(manifest=[]), "$.manifest: expected object, got array"),
            (lambda e, m: m["run"].update(scale_factor="100; rm"), "scale_factor"),
            (lambda e, m: e["environment"]["reservation"].update(end="tomorrow"), "reservation.end"),
            (lambda e, m: m["repeats"][0]["applications"][0].update(spark_conf_effective=[]),
             "applications[0].spark_conf_effective: expected object, got array"),
            (lambda e, m: m["repeats"][0]["applications"][0].update(spark_conf_effective={"a": 1}),
             "applications[0].spark_conf_effective['a']: expected string, got integer"),
            (lambda e, m: m["repeats"][0]["applications"][1].update(xflare_parameters_effective="x"),
             "applications[1].xflare_parameters_effective: expected object, got string"),
        ]
        for mutate, expect in cases:
            with self.subTest(expect=expect):
                self.rejects(mutate, expect)

    def test_accept_effective_settings_without_warnings(self):
        env = copy.deepcopy(load("valid-envelope.json"))
        apps = env["manifest"]["repeats"][0]["applications"]
        apps[0]["spark_conf_effective"] = {"spark.sql.shuffle.partitions": "200"}
        apps[1]["xflare_parameters_effective"] = {"driver.merge_launch_groups": "true", "driver.launch_parallelism": ""}
        self.assertEqual(schema.validate_envelope(env), ([], []))

    def test_many_duplicates_capped_and_fast(self):
        """Duplicate checks are set-based (linear) and diagnostics are capped."""
        env = load("valid-shared-envelope.json")
        env["manifest"]["run"]["engines"] = ["velox"] * 80000
        t0 = time.monotonic()
        errs, _ = schema.validate_envelope(env)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(len(errs), schema.MAX_MESSAGES + 1)
        self.assertEqual(errs[0], "$.manifest.run.engines[1]: duplicate engine velox")
        self.assertEqual(errs[-1], f"... {79999 - schema.MAX_MESSAGES} more errors")
        env = load("valid-envelope.json")
        env.update({f"k{i}": i for i in range(1000)})
        errs, warns = schema.validate_envelope(env)
        self.assertEqual((errs, len(warns), warns[-1]), ([], schema.MAX_MESSAGES + 1, "... 950 more warnings"))

    def test_uuid4_re(self):
        self.assertTrue(schema.UUID4_RE.fullmatch(mf.fixture_uuid(1)))
        for bad in ("00000000-0000-1000-8000-000000000001", "ABCDEF00-0000-4000-8000-000000000001",
                    mf.fixture_uuid(1) + "\n",
                    "../" + mf.fixture_uuid(1)):
            self.assertIsNone(schema.UUID4_RE.fullmatch(bad), bad)

    def test_time_digits_are_ascii(self):
        """RFC 3339 digits are ASCII only, including the fraction; anything accepted can be ordered."""
        for bad in ("2026-10-01T00:21:00.\u0661Z", "\u0662\u0660\u0662\u0666-10-01T00:21:00Z",
                    "2026-10-01T00:21:00.\uff11Z", "2026-10-01T00:21:0\u0661Z"):
            m = mf.build_manifest()
            m["run"]["finished_at"] = bad
            errs, _ = schema.validate_manifest(m)
            self.assertEqual(len(errs), 1, bad)
            self.assertTrue(errs[0].startswith("$.run.finished_at: "), errs)
        m = mf.build_manifest()
        m["run"]["finished_at"] = "2026-10-01T00:21:00.123456789Z"
        self.assertEqual(schema.validate_manifest(m)[0], [])
        agg.utc_key(m["run"]["finished_at"])

    def test_nesting_limit(self):
        """At most 64 levels of objects/arrays anywhere in a submission: 64 passes end to end, 65 is refused by the
        parser (before any json.dumps) and by both validators."""
        def nest(levels, kind):
            v = 0
            for _ in range(levels):
                v = [v] if kind == "array" else {"a": v}
            return v

        # (where the extension goes, how many containers enclose it there)
        positions = [("envelope", lambda e: e, 1), ("manifest", lambda e: e["manifest"], 2),
                     ("run.resources", lambda e: e["manifest"]["run"]["resources"], 4)]
        for name, parent, base in positions:
            for kind in ("array", "object"):
                for depth in (64, 65):
                    with self.subTest(position=name, kind=kind, depth=depth):
                        env = mf.build_envelope(mf.build_manifest())
                        parent(env)["future"] = nest(depth - base, kind)
                        body = json.dumps(env).encode()
                        # the same total depth for a bare manifest, which counts from its own root
                        bare = copy.deepcopy(env["manifest"])
                        if name != "envelope":
                            parent({"manifest": bare})["future"] = nest(depth - base + 1, kind)
                        if depth == 64:
                            self.assertEqual(schema.parse_json_strict(body), env)
                            self.assertEqual(schema.validate_envelope(env)[0], [])
                            if name != "envelope":
                                self.assertEqual(schema.validate_manifest(bare)[0], [])
                            tmp = tempfile.mkdtemp(prefix="lb-depth-")
                            try:
                                status, r = Store(tmp).put(env)
                                self.assertEqual(status, "created")
                                self.assertEqual(Store(tmp).list_runs()[0]["meta"], r["meta"])
                            finally:
                                shutil.rmtree(tmp)
                            continue
                        with mock.patch.object(schema.json, "dumps", side_effect=AssertionError("dumps called")):
                            with self.assertRaises(schema.BadJSON) as cm:
                                schema.parse_json_strict(body)
                        self.assertEqual(str(cm.exception), "invalid JSON: JSON nesting deeper than 64 levels")
                        self.assertEqual(schema.validate_envelope(env), (["$: JSON nesting deeper than 64 levels"], []))
                        if name != "envelope":
                            self.assertEqual(schema.validate_manifest(bare),
                                             (["$: JSON nesting deeper than 64 levels"], []))
        # the bare manifest counts from its own root: 64 levels below it is fine there, but not inside an envelope
        m = mf.build_manifest()
        m["future"] = nest(63, "array")
        self.assertEqual(schema.validate_manifest(m)[0], [])
        self.assertEqual(schema.validate_envelope(mf.build_envelope(m))[0], ["$: JSON nesting deeper than 64 levels"])
        # far deeper than Python's own recursion limit: same message, from the parser
        with self.assertRaises(schema.BadJSON) as cm:
            schema.parse_json_strict(b"[" * 100000 + b"]" * 100000)
        self.assertEqual(str(cm.exception), "invalid JSON: JSON nesting deeper than 64 levels")
        self.assertTrue(schema.too_deep(nest(100000, "array")))         # iterative: no RecursionError

    def test_parse_errors_have_one_prefix(self):
        for body in (b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff', b'{', b"[" * 70 + b"]" * 70):
            with self.assertRaises(schema.BadJSON) as cm:
                schema.parse_json_strict(body)
            msg = str(cm.exception)
            self.assertTrue(msg.startswith("invalid JSON: "), msg)
            self.assertEqual(msg.count("invalid JSON"), 1, msg)

    def test_huge_integers_are_snipped_in_messages(self):
        huge = 10 ** 4299                                                # 4300 digits: the largest the parser takes
        env = load("valid-envelope.json")
        m = env["manifest"]
        m["run"]["repeat_requested"] = huge
        m["repeats"][0]["index"] = 0                                     # "0 outside 1..<huge>"
        m["repeats"][1]["index"] = huge
        m["repeats"][2]["index"] = huge                                  # duplicate huge index
        m["run"]["resources"]["executors"] = -huge
        m["repeats"][0]["queries"][0]["engines"]["xflare"]["elapsed_ms"] = -huge
        self.assertEqual(schema.parse_json_strict(json.dumps(env).encode()), env)
        errs, _ = schema.validate_envelope(env)
        self.assertEqual(len(errs), 4, errs)
        for msg in errs:
            self.assertLess(len(msg), 120, msg)
        self.assertIn("$.manifest.repeats[0].index: 0 outside 1..<integer of ~4300 digits>", errs)
        self.assertIn("$.manifest.repeats[2].index: duplicate repeat index <integer of ~4300 digits>", errs)
        self.assertIn("$.manifest.run.resources.executors: <-integer of ~4300 digits> outside 1..", errs)
        # beyond the parser's limit (direct callers only), repr would raise; the snippet does not
        self.assertEqual(schema._snip(10 ** 5000), "<integer of ~5001 digits>")

    def test_empty_submitter_email(self):
        env = mf.build_envelope(mf.build_manifest())
        env["submitter"]["email"] = ""
        self.assertEqual(schema.validate_envelope(env), ([], []))
        email = load("../schema/xflare-bench-submission-1.schema.json")["properties"]["submitter"]["properties"]["email"]
        self.assertNotIn("minLength", email)

    def test_depth_walk_memory_independent_of_array_width(self):
        wide = [0] * 200000
        tracemalloc.start()
        try:
            self.assertFalse(schema.too_deep(wide))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 128 * 1024)

        # The deep branch must still be visited when it follows many shallow siblings.
        deep = []
        for _ in range(schema.MAX_DEPTH):
            deep = [deep]
        self.assertTrue(schema.too_deep([wide, deep]))

    def test_not_an_object(self):
        self.assertEqual(schema.validate_envelope([]), (["$: expected object, got array"], []))
        self.assertEqual(schema.validate_manifest("x"), (["$: expected object, got string"], []))

    def test_unknown_property_is_warning_and_hashed(self):
        env = load("valid-envelope.json")
        before = agg.content_id(env["manifest"])
        env["manifest"]["run"]["future_field"] = 1
        env["extra"] = True
        errs, warns = schema.validate_envelope(env)
        self.assertEqual(errs, [])
        self.assertEqual(warns, ["$.'extra': unknown property (kept, not interpreted)",
                                 "$.manifest.run.'future_field': unknown property (kept, not interpreted)"])
        self.assertNotEqual(agg.content_id(env["manifest"]), before)

    def test_zero_elapsed_accepted_but_ineligible(self):
        env = load("valid-envelope.json")
        env["manifest"]["repeats"][2]["queries"][4]["engines"]["xflare"]["elapsed_ms"] = 0
        self.assertEqual(schema.validate_envelope(env)[0], [])
        d = agg.derive(env["manifest"], env)
        self.assertFalse(d["per_query"]["5"]["eligible"])
        self.assertEqual(d["ineligible_reasons"]["q5"], ["xflare elapsed_ms not positive in repeat 3"])

    def test_errors_do_not_echo_long_values(self):
        hostile = "<img src=x onerror=alert(1)>" * 400
        env = load("valid-envelope.json")
        env["manifest"]["run"]["codec"] = hostile
        env[hostile] = 1
        errs, warns = schema.validate_envelope(env)
        for msg in errs + warns:
            self.assertLess(len(msg), 120, msg)

    def test_nullable_shapes(self):
        env = load("valid-envelope.json")
        m = env["manifest"]
        m["repeats"][0]["queries"][0]["engines"]["velox"]["verify"] = None
        m["repeats"][0]["finished_at"] = None
        m["repeats"][0]["applications"][-1]["finished_at"] = None
        env["environment"].update(reservation=None, observed_at=None, host=None, source="none",
                                  booking_overlap="unknown")
        m["build"]["bundle"]["kind"] = "dev"
        m["build"]["gluten"] = {"commit": None, "dirty": None, "build_command": None}
        self.assertEqual(schema.validate_envelope(env), ([], []))
        m["build"]["bundle"]["kind"] = "admin"
        self.assertEqual(schema.validate_envelope(env)[1], ["$.manifest.build.gluten: expected null unless "
                                                            "bundle.kind=dev"])


class Eligibility(unittest.TestCase):
    """Per-engine Q(run, q) over every completed repeat; total needs all 22, every repeat completed, exit ok."""

    def test_valid(self):
        d = agg.derive(load("valid-envelope.json")["manifest"], load("valid-envelope.json"))
        self.assertTrue(d["total_eligible"])
        self.assertEqual(d["eligible_queries"], list(range(1, 23)))
        self.assertEqual(d["ineligible_reasons"], {})
        self.assertEqual(d["badges"], ["x3", "reproducible", "offload:offloaded"])

    def test_velox_mismatch(self):
        env = load("velox-mismatch.json")
        d = agg.derive(env["manifest"], env)
        self.assertTrue(d["total_eligible"], "A7: a wrong velox answer only blanks that query's velox time")
        self.assertEqual(d["eligible_queries"], list(range(1, 23)))
        self.assertEqual(d["ineligible_reasons"], {})
        q7 = d["per_query"]["7"]
        self.assertIsNotNone(q7["xflare_ms"])
        self.assertEqual((q7["velox_ms"], q7["speedup"], q7["velox_problems"]),
                         (None, None, ["velox verify mismatch in repeat 2"]))
        self.assertEqual(d["per_query"]["8"]["velox_problems"], [])
        self.assertIsNotNone(d["totals"]["xflare_ms"])
        self.assertEqual((d["totals"]["velox_ms"], d["totals"]["speedup"]), (None, None))
        self.assertNotIn("partial", d["badges"])

    def test_partial(self):
        env = load("partial.json")
        d = agg.derive(env["manifest"], env)
        self.assertEqual(d["eligible_queries"], list(range(1, 21)))
        self.assertEqual(d["ineligible_reasons"]["total"], ["queries not eligible: 21, 22", "exit_status failed"])
        self.assertEqual(d["ineligible_reasons"]["q21"], ["missing in repeat 2"])
        self.assertEqual(d["totals"], {"xflare_ms": None, "velox_ms": None, "speedup": None})
        self.assertIn("partial", d["badges"])

    def test_recording_mode_nothing_eligible(self):
        env = load("recording-mode.json")
        d = agg.derive(env["manifest"], env)
        self.assertEqual(d["eligible_queries"], [])
        self.assertIn("recording mode (record_answers=true)", d["ineligible_reasons"]["common"])
        self.assertNotIn("partial", d["badges"])
        # record_answers alone (even with answers-verified samples) disqualifies
        m = mf.build_manifest()
        m["run"]["record_answers"] = True
        self.assertEqual(agg.derive(m, None)["eligible_queries"], [])

    def derive_m(self, mutate, **kw):
        m = mf.build_manifest(**kw)
        mutate(m)
        return agg.derive(m, None)

    def test_predicate_cases(self):
        def set_identity(rep, key, value):
            rep[key] = value
            for application in rep["applications"]:
                application[key] = value

        d = self.derive_m(lambda m: m["repeats"][2].update(status="failed"))
        self.assertEqual(d["eligible_queries"], list(range(1, 23)), "medians come from completed repeats only")
        self.assertEqual(d["ineligible_reasons"], {"total": ["repeat 3 failed"]})
        self.assertEqual(d["per_query"]["1"]["n"], 2)
        self.assertEqual(d["badges"][0], "x2")

        d = self.derive_m(lambda m: m["repeats"].pop(1))
        self.assertEqual(d["eligible_queries"], [], "a missing repeat index disqualifies every query")
        self.assertIn("repeat list incomplete", d["ineligible_reasons"]["common"])

        d = self.derive_m(lambda m: set_identity(m["repeats"][1], "artifact_id", "other"))
        self.assertEqual(d["eligible_queries"], [])
        self.assertIn("artifact differs across repeats", d["ineligible_reasons"]["common"])
        d = self.derive_m(lambda m: set_identity(m["repeats"][2], "bundle", "other"))
        self.assertIn("bundle differs across repeats", d["ineligible_reasons"]["common"])
        # differing artifact on a failed repeat does not matter; null counts as a value
        d = self.derive_m(lambda m: (m["repeats"][2].update(status="aborted"),
                                     set_identity(m["repeats"][2], "artifact_id", None)))
        self.assertEqual(len(d["eligible_queries"]), 22)
        d = self.derive_m(lambda m: [set_identity(r, "artifact_id", None) for r in m["repeats"]])
        self.assertTrue(d["total_eligible"])

        d = self.derive_m(lambda m: m["run"].update(exit_status="failed"))
        self.assertEqual(len(d["eligible_queries"]), 22)
        self.assertEqual(d["ineligible_reasons"], {"total": ["exit_status failed"]})

        d = self.derive_m(lambda m: None, engines=("velox",))
        self.assertEqual(d["eligible_queries"], [], "xflare is required")
        self.assertIn("xflare missing in repeat 1", d["ineligible_reasons"]["common"])

        d = self.derive_m(lambda m: m["repeats"][0]["queries"][2]["engines"]["xflare"]["verify"].update(
            against="previous"))
        self.assertEqual(d["ineligible_reasons"]["q3"], ["xflare not verified against answers in repeat 1"])

        d = self.derive_m(lambda m: m["repeats"][0]["queries"][2]["engines"]["velox"].update(verify=None))
        self.assertEqual(d["per_query"]["3"]["velox_problems"], ["velox not verified against answers in repeat 1"])
        self.assertEqual(d["ineligible_reasons"], {})

        d = self.derive_m(lambda m: m["repeats"][1]["queries"][3]["engines"]["velox"].update(status="timeout"))
        self.assertEqual(d["per_query"]["4"]["velox_problems"], ["velox timeout in repeat 2"])
        self.assertEqual((d["per_query"]["4"]["velox_ms"], d["per_query"]["4"]["speedup"]), (None, None))
        self.assertTrue(d["per_query"]["4"]["eligible"])

        d = self.derive_m(lambda m: [r.update(status="aborted") for r in m["repeats"]])
        self.assertEqual(d["eligible_queries"], [])
        self.assertIn("no completed repeat", d["ineligible_reasons"]["common"])

        d = self.derive_m(lambda m: None, queries=list(range(1, 22)))
        self.assertEqual(d["eligible_queries"], list(range(1, 22)))
        self.assertEqual(d["ineligible_reasons"]["total"], ["queries not eligible: 22"])

    def test_huge_repeat_requested(self):
        """Completeness is checked by length first; no allocation proportional to repeat_requested."""
        for n in (10 ** 8, 10 ** 30):
            env = mf.build_envelope(mf.build_manifest())
            env["manifest"]["run"]["repeat_requested"] = n
            self.assertEqual(schema.validate_envelope(env)[0], [])
            tracemalloc.start()
            t0 = time.monotonic()
            try:
                d = agg.derive(env["manifest"], env)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertLess(peak, 16 * 2 ** 20)
            self.assertEqual(d["eligible_queries"], [])
            self.assertEqual(d["ineligible_reasons"]["common"], ["repeat list incomplete"])
            self.assertEqual(d["repeats_requested"], n)

    def test_wrong_answers_in_unfinished_repeats(self):
        """A failed or aborted repeat that recorded a non-ok/skipped engine status or a non-ok/null verify status for q
        disqualifies q; the reason names the engine, the value and the repeat."""
        def unfinished(repeat_status, engine, change):
            def mutate(m):
                m["repeats"][1]["status"] = repeat_status
                m["repeats"][1]["queries"][4]["engines"][engine].update(change)      # query 5, repeat 2
            return self.derive_m(mutate)

        cases = [
            (("failed", "xflare", {"status": "error", "elapsed_ms": None}), "xflare error in failed repeat 2"),
            (("aborted", "velox", {"status": "timeout"}), "velox timeout in aborted repeat 2"),
        ]
        for vs in ("mismatch", "unavailable", "changed", "new"):
            verify = {"against": "answers", "status": vs, "detail": None}
            cases.append((("failed", "velox", {"verify": verify}), f"velox verify {vs} in failed repeat 2"))
            cases.append((("aborted", "xflare", {"verify": verify}), f"xflare verify {vs} in aborted repeat 2"))
        for (status, engine, change), reason in cases:
            with self.subTest(reason=reason):
                d = unfinished(status, engine, change)
                if engine == "velox":        # A7: velox only blanks its own time
                    self.assertTrue(d["per_query"]["5"]["eligible"])
                    self.assertEqual(d["per_query"]["5"]["velox_problems"], [reason])
                    self.assertEqual((d["per_query"]["5"]["velox_ms"], d["per_query"]["5"]["speedup"]), (None, None))
                    self.assertEqual(d["ineligible_reasons"], {"total": [f"repeat 2 {status}"]})
                    continue
                self.assertFalse(d["per_query"]["5"]["eligible"])
                self.assertEqual(d["ineligible_reasons"]["q5"], [reason])
                self.assertEqual(d["eligible_queries"], [q for q in range(1, 23) if q != 5])
                self.assertIsNone(d["per_query"]["5"]["xflare_ms"])

        # skipped / null verify in an unfinished repeat is not a wrong answer by itself
        not_wrong = [("aborted", "xflare", {"status": "skipped", "elapsed_ms": None,
                                            "verify": {"against": None, "status": None, "detail": None}}),
                     ("failed", "velox", {"verify": {"against": None, "status": None, "detail": None}}),
                     ("failed", "velox", {"status": "skipped", "elapsed_ms": None})]
        for status, engine, change in not_wrong:
            with self.subTest(status=status, engine=engine, change=change):
                d = unfinished(status, engine, change)
                self.assertTrue(d["per_query"]["5"]["eligible"])
                self.assertEqual(d["ineligible_reasons"], {"total": [f"repeat 2 {status}"]})
        self.assertTrue(unfinished("failed", "velox", {"verify": None})["per_query"]["5"]["eligible"])  # null object
        # a completed repeat is still held to the full rule (ok + verified against answers)
        d = self.derive_m(lambda m: m["repeats"][1]["queries"][4]["engines"]["xflare"].update(status="skipped"))
        self.assertEqual(d["ineligible_reasons"]["q5"], ["xflare skipped in repeat 2"])
        self.assertGreaterEqual(agg.DERIVE_VERSION, 6)  # stored rows pick up A4 mode, eligibility and reproduction

    def test_badges(self):
        env = mf.build_envelope(mf.build_manifest(bundle_kind="dev"))
        env["environment"]["booking_overlap"] = "present"
        self.assertEqual(agg.derive(env["manifest"], env)["badges"],
                         ["x3", "reproducible", "dev bundle", "booking overlap", "offload:offloaded"])
        env["manifest"]["build"]["gluten"]["dirty"] = True
        self.assertNotIn("reproducible", agg.derive(env["manifest"], env)["badges"])
        m = mf.build_manifest()
        m["client"]["dirty"] = True
        self.assertEqual(agg.derive(m, None)["badges"], ["x3", "overlap unknown", "offload:offloaded"])
        m = mf.build_manifest()
        m["build"]["provenance_source"] = "local-checkout"
        m["repeats"][0]["queries"][0]["evidence"]["status"] = "fallback"
        self.assertEqual(agg.derive(m, None)["badges"], ["x3", "overlap unknown", "offload:mixed"])
        m = mf.build_manifest()
        m["repeats"][0]["queries"][0]["evidence"]["status"] = "partial"
        m["repeats"][1]["queries"][1]["evidence"]["status"] = "offloaded_except_skipped"
        self.assertEqual(agg.derive(m, None)["badges"], ["x3", "reproducible", "overlap unknown", "offload:offloaded"])
        m["repeats"][2]["queries"][2]["evidence"]["status"] = "declined"
        self.assertIn("offload:mixed", agg.derive(m, None)["badges"])
        self.assertGreater(agg.DERIVE_VERSION, 8)  # stored rows pick up the offload badge rule


class OptionalVelox(unittest.TestCase):
    """A7: xflare gates every query, velox only fills its own column; the velox total needs all 22 medians."""

    def test_xflare_only_run_ranks_without_velox(self):
        d = agg.derive(mf.build_manifest(engines=("xflare",)), None)
        self.assertTrue(d["total_eligible"])
        self.assertEqual(d["ineligible_reasons"], {})
        self.assertEqual((d["totals"]["xflare_ms"], d["totals"]["velox_ms"], d["totals"]["speedup"]),
                         (sum(1000 + 37 * q + 4 for q in range(1, 23)), None, None))
        self.assertEqual(d["per_query"]["3"]["velox_problems"], [])
        self.assertIsNone(d["per_query"]["3"]["velox_ms"])
        self.assertEqual((d["config"]["engines"], d["config"]["codec"]), ("xflare", "snappy"))

    def test_a_velox_problem_blanks_only_that_query(self):
        def blank(change):
            return lambda m: m["repeats"][1]["queries"][2]["engines"]["velox"].update(change)
        cases = [
            (blank({"status": "error", "elapsed_ms": None}), "velox error in repeat 2"),
            (blank({"elapsed_ms": 0}), "velox elapsed_ms not positive in repeat 2"),
            (blank({"verify": {"against": "answers", "status": "mismatch", "detail": None}}),
             "velox verify mismatch in repeat 2"),
            (lambda m: m["repeats"][1]["queries"][2]["engines"].pop("velox"), "velox missing in repeat 2"),
        ]
        for mutate, problem in cases:
            with self.subTest(problem=problem):
                m = mf.build_manifest()
                mutate(m)
                d = agg.derive(m, None)
                q3 = d["per_query"]["3"]
                self.assertEqual((q3["eligible"], q3["velox_ms"], q3["speedup"], q3["velox_problems"]),
                                 (True, None, None, [problem]))
                self.assertIsNotNone(q3["xflare_ms"])
                self.assertEqual((d["total_eligible"], d["ineligible_reasons"]), (True, {}))
                self.assertEqual((d["totals"]["velox_ms"], d["totals"]["speedup"]), (None, None))
                self.assertIsNotNone(d["totals"]["xflare_ms"])
                self.assertIsNotNone(d["per_query"]["4"]["speedup"])

    def test_xflare_problem_still_disqualifies(self):
        m = mf.build_manifest()
        m["repeats"][1]["queries"][2]["engines"]["xflare"]["verify"]["status"] = "mismatch"
        d = agg.derive(m, None)
        self.assertEqual(d["ineligible_reasons"]["q3"], ["xflare verify mismatch in repeat 2"])
        self.assertFalse(d["per_query"]["3"]["eligible"])

    def test_single_engine_shared_run_counts_as_per_engine(self):
        for mode in (None, "shared"):
            with self.subTest(mode=mode):
                d = agg.derive(mf.build_manifest(engines=("xflare",), application_mode=mode), None)
                self.assertTrue(d["total_eligible"])
                self.assertEqual(d["config"]["application_mode"], "per-engine")
                self.assertNotIn(agg.SHARED_BADGE, d["badges"])
                self.assertNotIn("common", d["ineligible_reasons"])
        d = agg.derive(mf.build_manifest(application_mode="shared"), None)
        self.assertFalse(d["total_eligible"], "two engines in one application still never rank (A6)")

    def test_engines_and_codec_are_config_keys(self):
        d = agg.derive(mf.build_manifest(codec="zstd"), None)
        self.assertTrue(set(("engines", "codec")) <= set(agg.CONFIG_KEYS))
        self.assertTrue(d["config_label"].endswith(" · per-engine · velox,xflare · zstd"))
        for flt, want in (({"engines": "velox,xflare"}, True), ({"engines": "xflare"}, False),
                          ({"codec": "zstd"}, True), ({"codec": "snappy"}, False)):
            self.assertEqual(agg.matches_filter(d, flt), want, flt)
        self.assertEqual(agg.tab_of(d), ("100", "zstd"))


class SettingsDifferAcrossApplications(unittest.TestCase):
    """A7: the same engine's gated Spark settings must not change between applications of a run."""

    def derive_with(self, maps):
        """maps: {(repeat index, engine): spark_conf_effective, or None for the field absent}."""
        m = mf.build_manifest()
        for rep in m["repeats"]:
            for app in rep["applications"]:
                value = maps.get((rep["index"], app["engine"]), {"spark.sql.shuffle.partitions": "200"})
                if value is not None:
                    app["spark_conf_effective"] = value
        return agg.derive(m, None)

    def test_cases(self):
        base = {"spark.sql.shuffle.partitions": "200", "spark.executor.cores": "6"}
        cases = [
            ("identical", {}, False),
            ("gated key changed", {(2, "xflare"): {**base, "spark.sql.shuffle.partitions": "400"}}, True),
            ("gated key only in one", {(2, "velox"): {**base, "spark.gluten.new": "1"}}, True),
            ("ungated key changed", {(2, "xflare"): {**base, "spark.executor.cores": "8"},
                                     (1, "xflare"): base}, False),
            ("engines differ from each other", {(r, "velox"): {"spark.sql.shuffle.partitions": "1"}
                                                for r in (1, 2, 3)}, False),
            ("one application recorded none", {(2, "xflare"): None}, False),
        ]
        for name, maps, differs in cases:
            with self.subTest(name):
                d = self.derive_with(maps)
                reasons = d["ineligible_reasons"].get("common", [])
                self.assertEqual(agg.SPARK_SETTINGS_DIFFER in reasons, differs)
                self.assertEqual(d["total_eligible"], not differs)

    def test_a_malformed_map_is_an_application_problem(self):
        d = self.derive_with({(2, "xflare"): ["x"]})
        self.assertEqual(d["ineligible_reasons"]["common"], ["applications missing or malformed in repeat 2"])


class ReproArguments(unittest.TestCase):
    """Option/value boundaries follow spark-tpch.sh, including option-shaped values."""

    def test_complete_producer_option_arities(self):
        # Pinned from the full case "$1" table in xflare-analytics/scripts/spark-tpch.sh,
        # inspected at b9d1bdbb7215a815a9d09f48410855c81f774f60.
        # --application-per-engine is the A4 flag (added on the producer's per-engine branch); it stays
        # accepted so recorded per-engine manifests parse.
        values = {
            "--xflare", "--python", "--launcher", "--timeout", "--spark-conf",
            "--reservation", "--xflare-toml", "--scale-factor", "--codec", "--engines",
            "--results-dir", "--config", "--artifact", "--bundle", "--request",
            "--executors", "--executor-cores", "--offheap", "--shuffle-partitions",
            "--repeat", "--submission", "--xflare-backend",
        }
        flags = {"--download-logs", "--record-answers", "--application-per-engine", "--list", "-h", "--help"}
        self.assertEqual(repro._VALUE_OPTIONS, values)
        self.assertEqual(repro._FLAGS, flags)
        self.assertEqual(repro._QUERY_IDS, {str(q) for q in range(1, 23)})
        for option in sorted(values):
            with self.subTest(option=option):
                self.assertEqual(list(repro._options([option, "--spark-conf", "22"])),
                                 [(option, "--spark-conf"), ("22", None)])
                with self.assertRaises(ValueError):
                    list(repro._options([option]))
        for flag in sorted(flags):
            with self.subTest(flag=flag):
                self.assertEqual(list(repro._options([flag, "22"])), [(flag, None), ("22", None)])

    def manifest(self, argv):
        manifest = mf.build_manifest()
        manifest["run"]["argv"] = argv
        return manifest

    def command_args(self, manifest):
        result = repro.script(manifest)
        self.assertIsNone(result["omitted"])
        self.assertEqual(result["length"], len(result["command"]))
        return shlex.split(result["command"].splitlines()[-1])[1:]

    def pins(self, manifest):
        return ["--artifact", manifest["build"]["artifact_id"],
                "--bundle", manifest["build"]["bundle"]["resolved"]]

    def test_reservation_shaped_config_value_is_preserved(self):
        for option in ("--config", "--submission"):
            with self.subTest(option=option):
                m = self.manifest([option, "--reservation", "--reservation", "4242", "1"])
                self.assertEqual(self.command_args(m), self.pins(m) +
                                 [option, "--reservation", "--reservation", "$RESERVATION", "1"])

    def test_artifact_shaped_config_value_does_not_select_artifact(self):
        for option in ("--config", "--submission"):
            with self.subTest(option=option):
                m = self.manifest([option, "--artifact", "1"])
                self.assertEqual(self.command_args(m), ["--reservation", "$RESERVATION", *self.pins(m),
                                                       option, "--artifact", "1"])

    def test_bundle_shaped_config_value_does_not_select_bundle(self):
        for option in ("--config", "--submission"):
            with self.subTest(option=option):
                m = self.manifest([option, "--bundle", "1"])
                self.assertEqual(self.command_args(m), ["--reservation", "$RESERVATION", *self.pins(m),
                                                       option, "--bundle", "1"])

    def test_reservations_are_replaced_in_place(self):
        m = self.manifest(["1", "--reservation", "4242", "--config", "cfg.toml",
                           "--reservation", "4243", "22"])
        self.assertEqual(self.command_args(m), self.pins(m) +
                         ["1", "--reservation", "$RESERVATION", "--config", "cfg.toml",
                          "--reservation", "$RESERVATION", "22"])

    def test_missing_reservation_is_inserted(self):
        m = self.manifest(["--xflare-toml", "template.toml", "1", "6"])
        self.assertEqual(self.command_args(m), ["--reservation", "$RESERVATION", *self.pins(m),
                                               *m["run"]["argv"]])

    def test_explicit_artifact_and_bundle_are_preserved(self):
        for chosen in (["--artifact", "chosen-artifact"], ["--bundle", "chosen-bundle"],
                       ["--artifact", "first", "--bundle", "chosen-bundle", "--artifact", "last"]):
            with self.subTest(chosen=chosen):
                m = self.manifest([*chosen, "1"])
                pins = ([] if "--artifact" in chosen else self.pins(m)[:2])
                pins += ([] if "--bundle" in chosen else self.pins(m)[2:])
                self.assertEqual(self.command_args(m), ["--reservation", "$RESERVATION", *pins, *chosen, "1"])

    def test_flag_shaped_artifact_and_bundle_values_are_preserved(self):
        for argv, pins in ((["--artifact", "--bundle", "1"], ["--bundle", "resolved-bundle"]),
                           (["--bundle", "--artifact", "1"], ["--artifact", "resolved-artifact"])):
            with self.subTest(argv=argv):
                m = self.manifest(argv)
                m["build"]["artifact_id"] = "resolved-artifact"
                m["build"]["bundle"]["resolved"] = "resolved-bundle"
                self.assertEqual(self.command_args(m), ["--reservation", "$RESERVATION", *pins, *argv])

    def test_real_option_arities_and_interleaved_query_ids(self):
        # This table is independent of repro's implementation: it mirrors the real shell parser.
        argv = ["1", "--download-logs", "--record-answers", "--application-per-engine", "--list", "2"]
        for option, value in (("--xflare", "key=--reservation"), ("--python", "--reservation"),
                              ("--launcher", "--artifact"), ("--timeout", "10"),
                              ("--spark-conf", "key=--bundle"), ("--xflare-toml", "--bundle"),
                              ("--scale-factor", "100"), ("--codec", "snappy"),
                              ("--engines", "velox,xflare"), ("--results-dir", "--artifact"),
                              ("--config", "--reservation"), ("--request", "request-1"),
                              ("--executors", "4"), ("--executor-cores", "6"),
                              ("--offheap", "64g"), ("--shuffle-partitions", "24"),
                              ("--repeat", "2"), ("--submission", "--bundle"),
                              ("--xflare-backend", "on")):
            argv += [option, value]
        argv += ["22", "--reservation", "4242", "6"]
        for prefix in ([], ["scripts/spark-tpch.sh"], ["/checkout/scripts/spark-tpch.sh"]):
            with self.subTest(prefix=prefix):
                m = self.manifest(prefix + argv)
                self.assertEqual(self.command_args(m), self.pins(m) + argv[:-2] + ["$RESERVATION", "6"])
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag):
                m = self.manifest([flag])
                self.assertEqual(self.command_args(m), ["--reservation", "$RESERVATION", *self.pins(m), flag])

    def test_unrecognized_or_incomplete_argv_omits_script(self):
        for argv in (["--future", "--reservation", "4242"], ["--future=value"],
                     ["--reservation=4242"], ["--config"], ["--reservation"], ["--artifact"], ["--bundle"],
                     ["--", "1"], ["1", "--", "--artifact", "chosen"], ["0"], ["23"], ["01"],
                     ["not-a-query"], ["--sf", "100"], ["--repeat"], ["--submission"]):
            with self.subTest(argv=argv):
                m = self.manifest(argv)
                before = copy.deepcopy(m)
                self.assertEqual(repro.script(m), {"command": None, "omitted": "unparsed", "length": 0})
                self.assertEqual(m, before, "unrecognized tokens remain verbatim in the manifest")

    def test_producer_manifest_argv_yields_a_script(self):
        # argv copied from a manifest produced by xflare-analytics scripts/spark-tpch.sh.
        # The suite needs no external checkout.
        argv = ["--reservation", "4242", "--xflare-toml", "scripts/config/Resource.mu.d1.toml.in",
                "--scale-factor", "100", "--executors", "4", "--executor-cores", "6", "--offheap", "64g",
                "--engines", "velox,xflare", "--xflare", "driver.skip_tiling=false", "--repeat", "2",
                "--submission", "_results/", "1", "6"]
        m = self.manifest(argv)
        self.assertEqual(self.command_args(m), self.pins(m) + ["--reservation", "$RESERVATION", *argv[2:]])
        self.assertIn('--reservation "$RESERVATION"', repro.script(m)["command"])

    def test_generated_fixtures_yield_scripts_with_their_run_shape(self):
        for filename in ("valid-envelope.json", "valid-manifest.json", "velox-mismatch.json", "partial.json",
                         "recording-mode.json"):
            with self.subTest(filename=filename):
                obj = load(filename)
                m = obj.get("manifest", obj)
                argv = m["run"]["argv"]
                self.assertEqual(self.command_args(m), self.pins(m) +
                                 ["--reservation", "$RESERVATION", *argv[2:]])
                self.assertEqual(argv[argv.index("--scale-factor") + 1], m["run"]["scale_factor"])
                self.assertEqual(argv[argv.index("--repeat") + 1], str(m["run"]["repeat_requested"]))
                self.assertEqual("--record-answers" in argv, m["run"]["record_answers"])

    def test_script_budgets(self):
        m = self.manifest(["1"] * repro.MAX_ARGS)
        self.assertIsNotNone(repro.script(m)["command"])
        m["run"]["argv"].append("1")
        self.assertEqual(repro.script(m), {"command": None, "omitted": "entries", "length": 0})
        m = self.manifest(["--results-dir", "x"])
        overhead = repro.script(m)["length"] - 1
        m["run"]["argv"][1] = "x" * (repro.MAX_CHARS - overhead)
        self.assertEqual(repro.script(m)["length"], repro.MAX_CHARS)
        self.assertIsNotNone(repro.script(m)["command"])
        m["run"]["argv"][1] += "x"
        self.assertEqual(repro.script(m), {"command": None, "omitted": "length", "length": repro.MAX_CHARS + 1})


class Aggregation(unittest.TestCase):
    """Medians, sums, speedups and ordering: exact numbers."""

    def test_reason_values_are_short_and_common_reasons_stored_once(self):
        m = mf.build_manifest(repeats=1, queries=[1])
        huge = 10 ** 4299
        m["run"]["repeat_requested"] = huge
        m["repeats"][0]["index"] = huge
        m["repeats"][0]["queries"][0]["engines"]["xflare"]["status"] = "error"
        self.assertEqual(schema.validate_manifest(m), ([], []))
        d = agg.derive(m, None)
        reasons = d["ineligible_reasons"]
        self.assertEqual(d["eligible_queries"], [])
        self.assertIn("repeat list incomplete", reasons["common"])
        self.assertIn("xflare error in repeat <integer of ~4300 digits>", reasons["q1"])
        self.assertTrue(all(len(s) < 150 for why in reasons.values() for s in why))
        self.assertEqual(json.dumps(reasons).count("repeat list incomplete"), 1)

        m["repeats"][0]["status"] = "aborted"
        reasons = agg.derive(m, None)["ineligible_reasons"]
        self.assertIn("xflare error in aborted repeat <integer of ~4300 digits>", reasons["q1"])
        self.assertIn("repeat <integer of ~4300 digits> aborted", reasons["total"])

    def test_identical_query_reasons_are_common(self):
        m = mf.build_manifest(repeats=1, queries=[])
        # A completed repeat may have no query records after an interrupted execution.
        m["run"]["queries"] = list(range(1, 23))
        self.assertEqual(schema.validate_manifest(m), ([], []))
        d = agg.derive(m, None)
        self.assertEqual(d["ineligible_reasons"], {
            "common": ["missing in repeat 1"],
            "total": ["queries not eligible: " + ", ".join(map(str, range(1, 23)))]})
        self.assertFalse(any(p["eligible"] for p in d["per_query"].values()))

    def test_large_repeat_indices_do_not_amplify_stored_reasons(self):
        m = mf.build_manifest(repeats=1)
        rep = m["repeats"][0]
        rep["queries"] = []
        huge = 10 ** 1000
        m["run"]["repeat_requested"] = huge + 200
        m["repeats"] = [dict(rep, index=huge + i) for i in range(200)]
        self.assertEqual(schema.validate_manifest(m), ([], []))
        before = agg.canonical_manifest_bytes(m)
        d = agg.derive(m, None)
        self.assertLess(len(json.dumps(d["ineligible_reasons"])), 1000)
        self.assertFalse(any(k.startswith("q") for k in d["ineligible_reasons"]))
        self.assertEqual(agg.canonical_manifest_bytes(m), before)

    def test_offload_badge_keeps_full_status(self):
        m = mf.build_manifest()
        status = "offloaded-" + "x" * 600
        for rep in m["repeats"]:
            for query in rep["queries"]:
                query["evidence"]["status"] = status
        d = agg.derive(m, None)
        self.assertIn("offload:" + status, d["badges"])
        self.assertTrue(agg.matches_filter(d, {"badge": "offload:" + status}))

    def test_medians_n1_n2_n3(self):
        d = agg.derive(mf.build_manifest(repeats=1, elapsed=const_elapsed([101], [303])), None)
        self.assertEqual(d["per_query"]["1"]["xflare_ms"], 101)
        self.assertEqual(d["per_query"]["1"]["velox_ms"], 303)
        self.assertEqual(d["per_query"]["1"]["speedup"], 3.0)
        self.assertEqual(d["totals"], {"xflare_ms": 22 * 101, "velox_ms": 22 * 303, "speedup": 3.0})

        d = agg.derive(mf.build_manifest(repeats=2, elapsed=const_elapsed([100, 103], [10, 11])), None)
        self.assertEqual(d["per_query"]["9"]["xflare_ms"], 101.5)          # even N: mean of the middle two
        self.assertEqual(d["per_query"]["9"]["velox_ms"], 10.5)
        self.assertEqual(d["per_query"]["9"]["speedup"], 10.5 / 101.5)
        self.assertEqual(d["totals"]["xflare_ms"], 22 * 101.5)
        self.assertEqual(d["totals"]["velox_ms"], 22 * 10.5)

        d = agg.derive(mf.build_manifest(repeats=3, elapsed=const_elapsed([130, 100, 101], [1, 900, 7])), None)
        self.assertEqual(d["per_query"]["22"]["xflare_ms"], 101)
        self.assertEqual(d["per_query"]["22"]["velox_ms"], 7)
        self.assertEqual(d["per_query"]["22"]["xflare_samples"], [130, 100, 101])
        self.assertEqual(d["per_query"]["22"]["n"], 3)

    def test_total_is_sum_of_medians(self):
        # default_elapsed: xflare 1000+37q + (0,9,4)[r] → median offset 4; velox 2500+91q + 4
        d = agg.derive(mf.build_manifest(), None)
        want_x = sum(1000 + 37 * q + 4 for q in range(1, 23))
        want_v = sum(2500 + 91 * q + 4 for q in range(1, 23))
        self.assertEqual((want_x, want_v), (31449, 78111))
        self.assertEqual(d["totals"], {"xflare_ms": want_x, "velox_ms": want_v, "speedup": want_v / want_x})
        self.assertEqual(d["per_query"]["3"]["speedup"], (2500 + 273 + 4) / (1000 + 111 + 4))

    def test_zero_denominator_null(self):
        self.assertIsNone(agg._ratio(5, 0))
        self.assertIsNone(agg._ratio(5, 0.0))
        self.assertIsNone(agg._ratio(None, None))
        self.assertEqual(agg._ratio(0, 4), 0.0)

    def test_numeric_domain(self):
        """derive never raises for validator-accepted integers: exact where possible, else null/ineligible."""
        def with_q1(engine, values, repeats=3):
            m = mf.build_manifest(repeats=repeats)
            for rep, value in zip(m["repeats"], values):
                if value is not None:
                    rep["queries"][0]["engines"][engine]["elapsed_ms"] = value
            self.assertEqual(schema.validate_manifest(m)[0], [])
            d = agg.derive(m, None)
            json.dumps(d, allow_nan=False)
            return d

        # all repeats huge: odd-N median stays an exact int; its speedup is not a float → null; total out of range
        d = with_q1("velox", [BIG, BIG, BIG])
        self.assertEqual(agg.metric_values(d, "q1"), {"xflare_ms": 1041, "velox_ms": BIG, "speedup": None})
        self.assertTrue(d["per_query"]["1"]["eligible"])
        self.assertEqual(d["ineligible_reasons"], {}, "a velox total beyond float range never unranks the run")
        self.assertEqual(agg.metric_values(d, "total"),
                         {"xflare_ms": sum(1000 + 37 * q + 4 for q in range(1, 23)), "velox_ms": None, "speedup": None})
        d = with_q1("xflare", [BIG, BIG, BIG])
        self.assertEqual(d["per_query"]["1"]["speedup"], 0.0)                 # underflows, does not raise

        # 1 of 2 repeats, odd pair sum: the exact median is x.5 beyond float range → q1 ineligible
        d = with_q1("xflare", [BIG + 1, None], repeats=2)
        self.assertEqual(d["ineligible_reasons"]["q1"], [agg.OUT_OF_RANGE])
        self.assertEqual(d["ineligible_reasons"]["total"], ["queries not eligible: 1"])
        self.assertEqual(d["eligible_queries"], list(range(2, 23)))
        # 1 of 2 repeats, even pair sum: exact int median, no float detour
        d = with_q1("xflare", [BIG, None], repeats=2)
        self.assertEqual(d["per_query"]["1"]["xflare_ms"], (BIG + 1046) // 2)

        # 1 of 3 repeats: the median ignores the outlier and nothing changes
        d = with_q1("xflare", [None, BIG, None])
        self.assertEqual(d["per_query"]["1"]["xflare_ms"], 1041)
        self.assertEqual(d["totals"]["xflare_ms"], 31449)

        self.assertIsNone(agg._ratio(BIG, 1))
        self.assertEqual(agg._ratio(1, BIG), 0.0)
        self.assertEqual(agg._ratio(BIG, BIG // 4), 4.0)

    def test_even_median_exact(self):
        self.assertEqual(agg._median([100, 102]), 101)
        self.assertIsInstance(agg._median([100, 102]), int)
        self.assertEqual(agg._median([100, 103]), 101.5)
        self.assertEqual(agg._median([2 ** 60 + 1, 2 ** 60 + 3]), 2 ** 60 + 2)   # exact beyond 2**53

    def test_metric_helpers(self):
        self.assertEqual(agg.METRICS, ("total",) + tuple(f"q{q}" for q in range(1, 23)))
        d = agg.derive(load("valid-envelope.json")["manifest"], None)
        self.assertEqual(agg.metric_values(d, "total"), d["totals"])
        self.assertEqual(agg.metric_values(d, "q3"), {"xflare_ms": 1115, "velox_ms": 2777, "speedup": 2777 / 1115})
        from datetime import datetime, timezone
        self.assertEqual(agg.utc_key("2026-10-01T00:00:00.1234567Z"),
                         (datetime(2026, 10, 1, tzinfo=timezone.utc), 123456700))
        self.assertEqual(agg.utc_key("2026-10-01T00:00:00.5Z"), agg.utc_key("2026-10-01T00:00:00.500000000Z"))
        self.assertLess(agg.utc_key("2026-10-01T00:00:00Z"), agg.utc_key("2026-10-01T00:00:00.1Z"))
        self.assertLess(agg.utc_key("2026-10-01T00:00:00.09Z"), agg.utc_key("2026-10-01T00:00:00.1Z"))

    def test_ordering_and_ties(self):
        T = mf.T0
        from datetime import timedelta
        mk = lambda n, x, reps=3, start=T: row(mf.build_envelope(mf.build_manifest(              # noqa: E731
            run_id=mf.fixture_uuid(n), repeats=reps, elapsed=const_elapsed(x), started_at=start)))
        fast = mk(10, 90)
        tie_n2 = mk(11, 100, reps=2)                 # same total, fewer repeats → after the x3 ties
        tie_late = mk(12, 100, start=T + timedelta(days=1))
        tie_early = mk(13, 100)
        tie_same_time = mk(14, 100)                  # same finished_at as tie_early → content_id decides
        slow = mk(15, 120)
        rows = [slow, tie_n2, tie_late, tie_same_time, fast, tie_early]
        ranked = agg.rank(rows, "total")
        a, b = sorted([tie_early, tie_same_time], key=lambda r: r["content_id"])
        self.assertEqual([r["run_id"] for r in ranked],
                         [r["run_id"] for r in (fast, a, b, tie_late, tie_n2, slow)])
        self.assertEqual(tie_early["derived"]["finished_at"], tie_same_time["derived"]["finished_at"])
        self.assertEqual(agg.rank(rows, "q5"), ranked)

    def test_finished_at_compares_as_time(self):
        d1 = {"totals": {"xflare_ms": 1, "velox_ms": 2, "speedup": 2.0}, "repeats_completed": 3,
              "finished_at": "2026-10-01T00:00:00.5Z",
              "content_id": "a"}
        d2 = dict(d1, finished_at="2026-10-01T00:00:00.10Z", content_id="b")
        d3 = dict(d1, finished_at="2026-10-01T00:00:00Z", content_id="c")
        keys = sorted([d1, d2, d3], key=lambda d: agg.order_key(d, "total"))
        self.assertEqual([d["content_id"] for d in keys], ["c", "b", "a"])

    def test_finished_at_ties_use_every_fraction_digit(self):
        """Equal metric and repeat count: the run that finished first ranks first even when the two times differ
        only after the sixth fractional digit (content_id must not decide)."""
        early, late = "2026-10-01T00:21:00.123456001Z", "2026-10-01T00:21:00.123456999Z"

        def mk(n, finished):
            m = mf.build_manifest(run_id=mf.fixture_uuid(n), elapsed=const_elapsed(100))
            m["run"]["finished_at"] = finished
            self.assertEqual(schema.validate_manifest(m)[0], [])
            return row(mf.build_envelope(m))

        # pick a pair whose content_id order is the opposite of finishing order, so only the time can rank it right
        for n in range(100, 200, 2):
            a, b = mk(n, early), mk(n + 1, late)
            if b["content_id"] < a["content_id"]:
                break
        else:
            self.fail("no fixture pair with reversed content_id order")
        self.assertEqual([r["run_id"] for r in agg.rank([b, a], "total")], [a["run_id"], b["run_id"]])
        self.assertIs(agg.hero([b, a], agg.DEFAULT_FILTER), a)
        self.assertLess(agg.utc_key(early), agg.utc_key(late))

    def test_rank_per_query_and_withdrawn(self):
        ok = row(load("valid-envelope.json"))
        wrong = mf.build_manifest(run_id=mf.fixture_uuid(7))
        wrong["repeats"][1]["queries"][6]["engines"]["xflare"]["verify"]["status"] = "mismatch"
        mis = row(mf.build_envelope(wrong))
        self.assertEqual([r["run_id"] for r in agg.rank([mis, ok], "q7")], [ok["run_id"]])
        self.assertEqual(len(agg.rank([mis, ok], "q8")), 2)
        self.assertEqual(agg.rank([mis, ok], "total"), [ok])
        self.assertEqual(agg.rank([dict(ok, withdrawn=True)], "total"), [])
        with self.assertRaises(ValueError):
            agg.rank([ok], "q23")

    def test_sf_tabs_default_filter_and_hero(self):
        def mk(n, x, sf="100", e=4, c=6, **kw):
            return row(mf.build_envelope(mf.build_manifest(run_id=mf.fixture_uuid(n), sf=sf, executors=e,
                                                           executor_cores=c, elapsed=const_elapsed(x), **kw)))

        hero_run = mk(20, 50)
        faster_wrong_cfg = mk(21, 10, e=2, c=12)
        faster_withdrawn = dict(mk(22, 20), withdrawn=True)
        faster_partial = mk(23, 30, queries=list(range(1, 22)))
        faster_sf1000 = mk(24, 5, sf="1000")
        slower = mk(25, 60)
        sf100 = [r for r in (hero_run, faster_wrong_cfg, faster_withdrawn, faster_partial, faster_sf1000, slower)
                 if r["derived"]["sf"] == "100"]

        h = agg.hero(sf100, agg.DEFAULT_FILTER)
        self.assertIs(h, hero_run)
        for q in range(1, 23):                                        # every bar comes from that one run
            self.assertEqual(h["derived"]["per_query"][str(q)]["xflare_ms"], 50)
            self.assertEqual(h["derived"]["per_query"][str(q)]["velox_ms"], 100)
        self.assertIs(agg.hero(sf100, {"executors": 2, "executor_cores": 12}), faster_wrong_cfg)
        self.assertIs(agg.hero(sf100, {}), faster_wrong_cfg)
        self.assertIs(agg.hero([faster_sf1000], agg.DEFAULT_FILTER), faster_sf1000)
        self.assertIsNone(agg.hero(sf100, {"executors": 8}))
        self.assertIsNone(agg.hero([faster_withdrawn], agg.DEFAULT_FILTER))

    def test_hero_or_fallback(self):
        """No total-eligible run under the filter: the hero falls back to the fastest total-eligible run of any
        configuration among the rows passed (one SF tab), flagged; the filtered board itself stays empty."""
        def mk(n, x, e=4, c=6, sf="100", **kw):
            return row(mf.build_envelope(mf.build_manifest(run_id=mf.fixture_uuid(n), sf=sf, executors=e,
                                                           executor_cores=c, elapsed=const_elapsed(x), **kw)))

        two = mk(30, 40, e=2, c=12)
        one = mk(31, 30, e=1, c=6)
        withdrawn_4x6 = dict(mk(32, 10), withdrawn=True)
        partial_4x6 = mk(33, 5, queries=list(range(1, 22)))
        faster_other_tab = mk(34, 1, e=1, c=6, sf="1000")
        sf100 = [two, one, withdrawn_4x6, partial_4x6]

        self.assertEqual(agg.hero_or_fallback(sf100, agg.DEFAULT_FILTER), (one, True))
        board = agg.rank([r for r in sf100 if agg.matches_filter(r["derived"], agg.DEFAULT_FILTER)], "total")
        self.assertEqual(board, [])                                            # the board stays filtered (empty)
        self.assertEqual(agg.hero_or_fallback(sf100, {"bundle": "nope"}), (one, True))
        self.assertEqual(agg.hero_or_fallback([faster_other_tab], agg.DEFAULT_FILTER), (faster_other_tab, True))
        fourx6 = mk(35, 50)                                                    # slower than both, but matches
        self.assertEqual(agg.hero_or_fallback(sf100 + [fourx6], agg.DEFAULT_FILTER), (fourx6, False))
        self.assertEqual(agg.hero_or_fallback([withdrawn_4x6, partial_4x6], agg.DEFAULT_FILTER), (None, False))
        self.assertEqual(agg.hero_or_fallback([], agg.DEFAULT_FILTER), (None, False))

    def test_matches_filter(self):
        env = mf.build_envelope(mf.build_manifest(bundle_kind="dev"))
        d = agg.derive(env["manifest"], env)
        yes = [{}, {"executors": "4", "executor_cores": 6}, {"shuffle_partitions": "-"}, {"devices": None},
               {"bundle": "xflare-admin-2026.10.1"}, {"toml": "Resource.mu.d1.toml.in"}, {"offheap": "64g"},
               {"submitter": "bench@example.com"},
               {"device": "0"}, {"badge": "dev bundle"}, {"badge": ["x3", "reproducible"]},
               {"date_from": "2026-10-01", "date_to": "2026-10-01"}]
        no = [{"executors": 1}, {"shuffle_partitions": 200}, {"device": "1"},
              {"badge": ["x3", "partial"]}, {"date_from": "2026-10-02"}, {"date_to": "2026-09-30"},
              {"submitter": "x@y"}]
        for f in yes:
            self.assertTrue(agg.matches_filter(d, f), f)
        for f in no:
            self.assertFalse(agg.matches_filter(d, f), f)
        for key in ("top", "sf", "bundle_kind", "host"):                    # no route filters on these
            with self.assertRaises(ValueError):
                agg.matches_filter(d, {key: "x"})

    def test_derived_shape(self):
        env = load("valid-envelope.json")
        d = agg.derive(env["manifest"], env)
        json.dumps(d, allow_nan=False)
        self.assertEqual(d["content_id"], agg.content_id(env["manifest"]))
        self.assertEqual(d["config_label"],
                         "4×6 · 64g · sp=- · dev=- · Resource.mu.d1.toml.in · xflare-admin-2026.10.1 · per-engine"
                         " · velox,xflare · snappy")
        self.assertEqual((d["sf"], d["repeats_requested"], d["repeats_completed"], d["submitter"], d["device_ids"]),
                         ("100", 3, 3, "bench@example.com", ["0"]))

    def test_canonical_bytes(self):
        m = {"b": [1, {"é": "<x>"}], "a": None}
        self.assertEqual(agg.canonical_manifest_bytes(m), '{"a":null,"b":[1,{"é":"<x>"}]}'.encode())
        self.assertEqual(agg.content_id(m), hashlib.sha256('{"a":null,"b":[1,{"é":"<x>"}]}'.encode()).hexdigest())


class ApplicationMode(unittest.TestCase):
    def manifest(self, **kw):
        return mf.build_manifest(application_mode="per-engine", **kw)

    def assert_valid(self, manifest):
        self.assertEqual(schema.validate_manifest(manifest), ([], []))

    def assert_rejects(self, manifest, message):
        errors, _ = schema.validate_manifest(manifest)
        self.assertTrue(any(message in e for e in errors), errors)

    def test_fixture_and_builder_preserve_engine_order_and_derived_fields(self):
        self.assertEqual(schema.validate_envelope(load("valid-per-engine-envelope.json")), ([], []))
        for engines in (("velox", "xflare"), ("xflare", "velox"), ("xflare",)):
            m = self.manifest(engines=engines)
            self.assert_valid(m)
            for rep in m["repeats"]:
                apps = rep["applications"]
                self.assertEqual([a["engine"] for a in apps], list(engines))
                for key in ("invocation", "app_id", "started_at"):
                    self.assertEqual(rep[key], apps[0][key])
                self.assertEqual(rep["finished_at"], apps[-1]["finished_at"])

    def test_optional_mode_and_shared_rejects_applications_even_when_empty(self):
        m = mf.build_manifest(application_mode=None)
        before = agg.content_id(m)
        self.assert_valid(m)
        self.assertEqual(agg.derive(m, None)["config"]["application_mode"], "shared")
        self.assertEqual(agg.content_id(m), before)
        m["run"]["application_mode"] = "shared"
        self.assert_valid(m)
        for mode in (None, "shared"):
            for apps in ([], [{}], None):
                with self.subTest(mode=mode, apps=apps):
                    m = mf.build_manifest(application_mode=mode)
                    m["repeats"][0]["applications"] = apps
                    self.assert_rejects(m, "must be absent in shared")

    def test_mode_enum_and_type(self):
        for mode in ("separate", "", None, 1, True, [], {}):
            with self.subTest(mode=mode):
                m = mf.build_manifest()
                m["run"]["application_mode"] = mode
                self.assert_rejects(m, "$.run.application_mode")

    def test_invalid_mode_does_not_report_shared_application_errors(self):
        for mode in ("per_engine", None, 1, True, [], {}):
            with self.subTest(mode=mode):
                m = self.manifest()
                m["run"]["application_mode"] = mode
                errors, _ = schema.validate_manifest(m)
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0].startswith("$.run.application_mode:"), errors)

    def test_historical_malformed_applications_in_unfinished_repeats_disqualify_the_run(self):
        for status in ("failed", "aborted"):
            with self.subTest(status=status):
                m = self.manifest()
                m["repeats"][1].update(status=status, applications=[None])
                d = agg.derive(m, None)
                self.assertEqual(d["eligible_queries"], [])
                self.assertIn("applications missing or malformed in repeat 2", d["ineligible_reasons"]["common"])

    def test_applications_required_array_and_empty_only_if_aborted(self):
        m = self.manifest()
        m["repeats"][0].pop("applications")
        self.assert_rejects(m, "applications: required")
        for bad in (None, {}, "applications", 1):
            m = self.manifest()
            m["repeats"][0]["applications"] = bad
            self.assert_rejects(m, "applications: expected array")
        for status in ("completed", "failed", "aborted"):
            m = self.manifest()
            m["repeats"][0].update(status=status, applications=[])
            if status == "aborted":
                self.assert_valid(m)
            else:
                self.assert_rejects(m, "may be empty only for an aborted repeat")

    def test_application_shape_required_fields_types_enums_and_times(self):
        for bad in (None, [], "app", 1):
            m = self.manifest()
            m["repeats"][0]["applications"][0] = bad
            self.assert_rejects(m, "applications[0]: expected object")
        fields = ("engine", "invocation", "app_id", "artifact_id", "bundle", "started_at", "finished_at", "cleanup")
        for key in fields:
            m = self.manifest()
            del m["repeats"][0]["applications"][0][key]
            self.assert_rejects(m, f"applications[0].{key}: missing")
            m = self.manifest()
            m["repeats"][0]["applications"][0][key] = {}
            self.assert_rejects(m, f"applications[0].{key}: expected string")
        for key, bad in (("engine", "spark"), ("cleanup", "maybe"), ("invocation", None),
                         ("started_at", None), ("started_at", "2026-02-30T00:00:00Z"),
                         ("finished_at", "2026-10-01T00:00:00+00:00")):
            m = self.manifest()
            m["repeats"][0]["applications"][0][key] = bad
            self.assert_rejects(m, f"applications[0].{key}:")

    def test_application_engine_subset_duplicates_and_order(self):
        m = self.manifest()
        m["run"]["engines"] = ["velox"]
        self.assert_rejects(m, "xflare not in run.engines")
        m = self.manifest()
        m["repeats"][0]["applications"][1]["engine"] = "velox"
        self.assert_rejects(m, "duplicate engine velox")
        m = self.manifest()
        m["repeats"][0]["applications"].reverse()
        self.assert_rejects(m, "applications must follow run.engines order")

    def test_derived_field_inconsistency_rejected_individually(self):
        for key, value in (("invocation", "wrong"), ("app_id", None), ("started_at", "2026-10-01T01:00:00Z"),
                           ("finished_at", None), ("cleanup", "failed"), ("artifact_id", None), ("bundle", None)):
            with self.subTest(key=key):
                m = self.manifest()
                m["repeats"][0][key] = value
                self.assert_rejects(m, f"repeats[0].{key}: must equal the value derived from applications")

    def test_derived_cleanup_precedence_and_nullable_last_finish(self):
        for cleanups, expected in ((("ok", "ok"), "ok"), (("ok", "unknown"), "unknown"),
                                   (("unknown", "ok"), "unknown"), (("failed", "unknown"), "failed"),
                                   (("unknown", "failed"), "failed")):
            m = self.manifest()
            rep = m["repeats"][0]
            for app, cleanup in zip(rep["applications"], cleanups):
                app["cleanup"] = cleanup
            rep["cleanup"] = expected
            rep["applications"][-1]["finished_at"] = rep["finished_at"] = None
            self.assert_valid(m)
            rep["cleanup"] = "unknown" if expected == "failed" else "failed"
            self.assert_rejects(m, "repeats[0].cleanup: must equal")
        m = self.manifest()
        rep = m["repeats"][0]
        rep["applications"][0]["finished_at"] = None
        self.assert_valid(m)  # first finish does not replace the last finish

    def test_nullable_identifiers_and_artifact_bundle_common_value(self):
        for key in ("app_id", "artifact_id", "bundle"):
            m = self.manifest()
            rep = m["repeats"][0]
            rep[key] = None
            for app in rep["applications"]:
                app[key] = None
            self.assert_valid(m)
        for key in ("artifact_id", "bundle"):
            for other in (None, "different"):
                m = self.manifest()
                rep = m["repeats"][0]
                rep["applications"][1][key] = other
                self.assert_rejects(m, f"repeats[0].{key}: must equal")
                rep[key] = None
                self.assert_valid(m)

    def test_unknown_application_properties_warn_and_diagnostics_are_capped(self):
        m = self.manifest()
        app = m["repeats"][0]["applications"][0]
        app["future"] = "kept"
        self.assertEqual(schema.validate_manifest(m),
                         ([], ["$.repeats[0].applications[0].'future': unknown property (kept, not interpreted)"]))
        app["engine"] = "x" * 10000
        errors, _ = schema.validate_manifest(m)
        self.assertLess(len(errors[0]), 200)
        m["repeats"][0]["applications"] = [dict(app)] * 200
        errors, warnings = schema.validate_manifest(m)
        self.assertEqual(len(errors), schema.MAX_MESSAGES + 1)
        self.assertEqual(len(warnings), schema.MAX_MESSAGES + 1)
        self.assertTrue(errors[-1].endswith("more errors"))
        self.assertTrue(warnings[-1].endswith("more warnings"))

    def test_missing_application_is_ineligible_rather_than_invalid(self):
        m = self.manifest()
        rep = m["repeats"][0]
        rep["applications"].pop()
        rep["finished_at"] = rep["applications"][0]["finished_at"]
        self.assert_valid(m)
        d = agg.derive(m, None)
        self.assertFalse(d["total_eligible"])
        self.assertEqual(d["eligible_queries"], [])
        self.assertIn("applications missing xflare in completed repeat 1", d["ineligible_reasons"]["common"])

    def test_application_artifact_and_bundle_equality_includes_null(self):
        for key, label in (("artifact_id", "artifact"), ("bundle", "bundle")):
            for value in (None, "different"):
                m = self.manifest()
                # Every repeat has the same derived null, so the existing repeat-level check cannot catch this.
                for rep in m["repeats"]:
                    rep["applications"][1][key] = value
                    rep[key] = None
                self.assert_valid(m)
                d = agg.derive(m, None)
                self.assertEqual(d["eligible_queries"], [])
                self.assertIn(f"{label} differs across applications of completed repeats",
                              d["ineligible_reasons"]["common"])
        m = self.manifest()
        for rep in m["repeats"]:
            for key in ("artifact_id", "bundle"):
                rep[key] = None
                for app in rep["applications"]:
                    app[key] = None
        self.assert_valid(m)
        self.assertTrue(agg.derive(m, None)["total_eligible"])

    def test_application_equality_spans_completed_repeats_only(self):
        for key, label in (("artifact_id", "artifact"), ("bundle", "bundle")):
            m = self.manifest()
            rep = m["repeats"][1]
            rep[key] = "different"
            for app in rep["applications"]:
                app[key] = "different"
            self.assert_valid(m)
            self.assertIn(f"{label} differs across applications of completed repeats",
                          agg.derive(m, None)["ineligible_reasons"]["common"])
            rep["status"] = "failed"
            rep["applications"].pop()
            rep["finished_at"] = rep["applications"][-1]["finished_at"]
            self.assert_valid(m)
            self.assertEqual(agg.derive(m, None)["eligible_queries"], list(range(1, 23)))

    def test_shared_modes_keep_measurements_but_never_rank(self):
        baseline = agg.derive(self.manifest(), None)
        for mode in (None, "shared"):
            with self.subTest(mode=mode):
                m = mf.build_manifest(application_mode=mode)
                before = agg.canonical_manifest_bytes(m)
                self.assert_valid(m)
                d = agg.derive(m, None)
                self.assertEqual(agg.canonical_manifest_bytes(m), before)
                self.assertEqual(d["content_id"], agg.content_id(m))
                self.assertFalse(d["total_eligible"])
                self.assertEqual(d["eligible_queries"], [])
                self.assertEqual(d["ineligible_reasons"]["common"],
                                 ["shared application: engine order affects timings"])
                self.assertEqual(d["ineligible_reasons"]["total"],
                                 ["queries not eligible: " + ", ".join(map(str, range(1, 23)))])
                self.assertEqual(d["badges"].count("shared application"), 1)
                self.assertNotIn("partial", d["badges"])
                self.assertEqual(d["totals"], baseline["totals"])
                for q in map(str, range(1, 23)):
                    self.assertFalse(d["per_query"][q]["eligible"])
                    for key in baseline["per_query"][q].keys() - {"eligible"}:
                        self.assertEqual(d["per_query"][q][key], baseline["per_query"][q][key], (q, key))
                shared = row(mf.build_envelope(m))
                for metric in agg.METRICS:
                    self.assertEqual(agg.rank([shared], metric), [], metric)
                self.assertIsNone(agg.hero([shared], {}))
                self.assertEqual(agg.hero_or_fallback([shared], {"executors": 8}), (None, False))

    def test_only_per_engine_ranks_and_is_a_fallback_candidate(self):
        shared = row(mf.build_envelope(mf.build_manifest(application_mode="shared", elapsed=const_elapsed(1))))
        per_engine = row(mf.build_envelope(self.manifest(run_id=mf.fixture_uuid(6), executors=1,
                                                       elapsed=const_elapsed(5))))
        for metric in agg.METRICS:
            self.assertEqual(agg.rank([shared, per_engine], metric), [per_engine], metric)
        self.assertEqual(agg.hero_or_fallback([shared, per_engine], agg.DEFAULT_FILTER), (per_engine, True))
        self.assertEqual(agg.hero_or_fallback([shared, per_engine], {"application_mode": "shared"}),
                         (per_engine, True))
        self.assertEqual(agg.DEFAULT_FILTER, {"executors": 4, "executor_cores": 6})
        self.assertIn("application_mode", agg.CONFIG_KEYS)
        self.assertNotIn("shared application", per_engine["derived"]["badges"])
        for item, mode in ((shared, "shared"), (per_engine, "per-engine")):
            d = item["derived"]
            self.assertEqual(d["config"]["application_mode"], mode)
            self.assertIn(" · " + mode + " · ", d["config_label"])
            self.assertTrue(agg.matches_filter(d, {"application_mode": mode}))
            self.assertFalse(agg.matches_filter(d, {"application_mode": "other"}))

    def test_unknown_historical_mode_keeps_its_reason_without_shared_badge(self):
        for mode in ("-", "separate", None, 1, [], {}):
            with self.subTest(mode=mode):
                m = mf.build_manifest(application_mode=None)
                m["run"]["application_mode"] = mode
                d = agg.derive(m, None)
                self.assertEqual(d["config"]["application_mode"], "-")
                self.assertEqual(d["eligible_queries"], [])
                self.assertFalse(d["total_eligible"])
                self.assertTrue(d["ineligible_reasons"]["common"][0].startswith("application_mode not understood:"))
                self.assertNotIn("shared application", d["badges"])
                self.assertNotIn("shared application: engine order affects timings", d["ineligible_reasons"]["common"])

    def test_shared_mode_does_not_restore_invalid_measurements(self):
        m = mf.build_manifest(application_mode="shared")
        m["repeats"][1]["queries"][6]["engines"]["xflare"]["verify"]["status"] = "mismatch"
        d = agg.derive(m, None)
        self.assertFalse(d["per_query"]["7"]["eligible"])
        self.assertIsNone(d["per_query"]["7"]["xflare_ms"])
        self.assertEqual(d["ineligible_reasons"]["q7"], ["xflare verify mismatch in repeat 2"])
        self.assertEqual(d["totals"], {"xflare_ms": None, "velox_ms": None, "speedup": None})
        self.assertEqual(d["per_query"]["1"]["xflare_ms"], 1041)
        self.assertIn("shared application: engine order affects timings", d["ineligible_reasons"]["common"])

    def test_version_seven_shared_row_rederives_once_without_changing_identity_or_receipt(self):
        import sqlite3
        self.assertGreater(agg.DERIVE_VERSION, 7)
        m = mf.build_manifest(application_mode=None)
        env = mf.build_envelope(m)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            _, before = store.put(env)
            old = copy.deepcopy(before["derived"])
            old.update(total_eligible=True, eligible_queries=list(range(1, 23)), ineligible_reasons={}, derive_version=7)
            old["badges"].remove("shared application")
            for q in old["per_query"].values():
                q["eligible"] = True
            receipt = {"ranked_total": True, "warnings": ["original warning"]}
            with sqlite3.connect(store.db_path) as conn:
                conn.execute("UPDATE runs SET derived_json=?, derive_version=7, total_eligible=1, receipt_json=?",
                             (json.dumps(old), json.dumps(receipt)))
                conn.row_factory = sqlite3.Row
                persisted_before = dict(conn.execute("SELECT * FROM runs").fetchone())
            with mock.patch.object(agg, "derive", wraps=agg.derive) as derive:
                after = store.get(m["run_id"])
                self.assertEqual(store.list_runs(), [after])
                self.assertEqual(derive.call_count, 1)
            self.assertFalse(after["derived"]["total_eligible"])
            self.assertEqual(after["derived"]["eligible_queries"], [])
            self.assertEqual(after["content_id"], agg.content_id(m))
            self.assertEqual(after["derived"]["totals"], old["totals"])
            self.assertEqual(after["receipt"], receipt)
            self.assertEqual(store.read_raw(m["run_id"]), agg.canonical_manifest_bytes(m))
            with sqlite3.connect(store.db_path) as conn:
                conn.row_factory = sqlite3.Row
                persisted_after = dict(conn.execute("SELECT * FROM runs").fetchone())
            for key in persisted_before.keys() - {"derived_json", "derive_version", "total_eligible"}:
                self.assertEqual(persisted_before[key], persisted_after[key], key)
            self.assertEqual((persisted_after["derive_version"], persisted_after["total_eligible"]),
                             (agg.DERIVE_VERSION, 0))

    def test_reproduce_preserves_zero_argument_flag_and_following_options(self):
        m = self.manifest()
        m["run"]["argv"] = ["--application-per-engine", "--engines", "xflare,velox", "1", "--repeat", "3"]
        result = repro.script(m)
        self.assertIsNone(result["omitted"])
        args = shlex.split(result["command"].splitlines()[-1])
        start = args.index("--application-per-engine")
        self.assertEqual(args[start:], m["run"]["argv"])

    def test_stored_version_five_rederives_mode_and_application_rules_once(self):
        import sqlite3
        m = self.manifest()
        for rep in m["repeats"]:
            rep["applications"][1]["artifact_id"] = "other"
            rep["artifact_id"] = None
        env = mf.build_envelope(m)
        self.assert_valid(m)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            _, before = store.put(env)
            old = copy.deepcopy(before["derived"])
            old["config"].pop("application_mode")
            old["total_eligible"] = True
            with sqlite3.connect(store.db_path) as conn:
                conn.execute("UPDATE runs SET derived_json=?, derive_version=5, total_eligible=1", (json.dumps(old),))
            with mock.patch.object(agg, "derive", wraps=agg.derive) as derive:
                after = store.get(m["run_id"])
                self.assertEqual(after, before)
                self.assertEqual(store.get(m["run_id"]), after)
                self.assertEqual(derive.call_count, 1)
            self.assertFalse(after["derived"]["total_eligible"])
            self.assertEqual(after["content_id"], agg.content_id(m))
            with sqlite3.connect(store.db_path) as conn:
                self.assertEqual(conn.execute("SELECT derive_version,total_eligible FROM runs").fetchone(),
                                 (agg.DERIVE_VERSION, 0))


class SparkConf(unittest.TestCase):
    def test_factory_maps_are_optional_and_copied(self):
        legacy = mf.build_manifest()
        self.assertNotIn("spark_conf", legacy["run"])
        self.assertNotIn("spark_conf_readback", legacy["run"])
        settings = {"spark.sql.adaptive.enabled": "false"}
        m = mf.build_manifest(spark_conf=settings, spark_conf_readback=settings)
        self.assertEqual(m["run"]["spark_conf"], settings)
        self.assertEqual(m["run"]["spark_conf_readback"], settings)
        self.assertIn("spark.sql.adaptive.enabled=false", m["run"]["argv"])
        settings["extra"] = "later"
        self.assertNotIn("extra", m["run"]["spark_conf"])
        self.assertNotIn("extra", m["run"]["spark_conf_readback"])

    def test_validate_both_optional_string_maps_without_new_length_limits(self):
        for settings in ({}, {"spark.sql.adaptive.enabled": "false"},
                         {"x" * 10000: "y" * 100000, "spark.password": "plain"}):
            m = mf.build_manifest(spark_conf=settings, spark_conf_readback=settings)
            self.assertEqual(schema.validate_manifest(m), ([], []))
            self.assertEqual(schema.validate_envelope(mf.build_envelope(m)), ([], []))
        self.assertEqual(schema.validate_manifest(mf.build_manifest()), ([], []))

    def test_reject_malformed_maps_and_bound_messages(self):
        for field in ("spark_conf", "spark_conf_readback"):
            for value in (None, [], "x", 1, False, {"key": None}, {"key": 3}, {"key": True},
                          {"key": []}, {"key": {}}):
                with self.subTest(field=field, value=value):
                    m = mf.build_manifest()
                    m["run"][field] = value
                    errors, warnings = schema.validate_manifest(m)
                    self.assertTrue(errors, (field, value))
                    self.assertTrue(errors[0].startswith("$.run." + field), errors)
                    self.assertEqual(warnings, [])
            m = mf.build_manifest()
            m["run"][field] = {("x" * 10000) + str(i): 0 for i in range(100)}
            errors, _ = schema.validate_manifest(m)
            self.assertEqual(len(errors), schema.MAX_MESSAGES + 1)
            self.assertLess(max(map(len, errors)), 160)

    def test_documented_maps_are_optional_string_objects(self):
        spec = json.loads((HERE / "schema/xflare-bench-manifest-1.schema.json").read_text())
        run = spec["properties"]["run"]
        for field in ("spark_conf", "spark_conf_readback"):
            self.assertNotIn(field, run["required"])
            self.assertEqual(run["properties"][field]["type"], "object")
            self.assertEqual(run["properties"][field]["additionalProperties"], {"type": "string"})

    def test_derived_state_source_badge_marker_and_eligibility(self):
        baseline = agg.derive(mf.build_manifest(), None)
        self.assertGreater(agg.DERIVE_VERSION, 6)
        self.assertIn("spark_conf", agg.CONFIG_KEYS)
        self.assertEqual(agg.DEFAULT_FILTER, {"executors": 4, "executor_cores": 6})
        for settings, expected in (({}, "default"), ({"spark.sql.shuffle.partitions": "64"}, "custom")):
            for mode in (None, "shared", "per-engine"):
                with self.subTest(settings=settings, mode=mode):
                    m = mf.build_manifest(spark_conf=settings, spark_conf_readback={}, application_mode=mode)
                    d = agg.derive(m, None)
                    self.assertEqual(d["config"]["spark_conf"], expected)
                    self.assertEqual(d["spark_conf_source"], "field")
                    self.assertEqual("spark conf" in d["badges"], expected == "custom")
                    self.assertEqual(d["config_label"].endswith(" +conf"), expected == "custom")
                    self.assertTrue(agg.matches_filter(d, {"spark_conf": expected}))
                    self.assertFalse(agg.matches_filter(d, {"spark_conf": "custom" if expected == "default" else "default"}))
                    mode_baseline = agg.derive(mf.build_manifest(application_mode=mode), None)
                    for key in ("total_eligible", "eligible_queries", "per_query", "totals", "ineligible_reasons"):
                        self.assertEqual(d[key], mode_baseline[key], key)
        self.assertEqual(baseline["config"]["spark_conf"], "default")
        self.assertEqual(baseline["spark_conf_source"], "argv")

    def test_field_takes_precedence_and_readback_does_not_set_state(self):
        m = mf.build_manifest(spark_conf={}, spark_conf_readback={"key": "value"})
        m["run"]["argv"] += ["--spark-conf", "key=value"]
        self.assertEqual(agg.derive(m, None)["config"]["spark_conf"], "default")
        m["run"].pop("spark_conf")
        m["run"]["argv"] = []
        self.assertEqual(agg.derive(m, None)["config"]["spark_conf"], "default")
        m["run"]["spark_conf"] = {"key": "value"}
        self.assertEqual(agg.derive(m, None)["config"]["spark_conf"], "custom")

    def test_legacy_argv_uses_option_boundaries(self):
        cases = [([], "default"), (["--spark-conf", "k=v"], "custom"),
                 (["1", "--spark-conf", "k=v", "2"], "custom"),
                 (["scripts/spark-tpch.sh", "--spark-conf", "k=v"], "custom"),
                 (["--application-per-engine", "--spark-conf", "k=v"], "custom"),
                 (["--xflare-backend", "on", "--spark-conf", "k=v"], "custom"),
                 (["--spark-conf", "k=v", "--xflare-backend", "on"], "custom"),
                 (["--xflare-backend", "--spark-conf", "1"], "default"),
                 (["--xflare-backend", "--spark-conf", "--spark-conf", "k=v"], "custom"),
                 (["--config", "--spark-conf", "1"], "default"),
                 (["--bundle", "--spark-conf", "--repeat", "3"], "default"),
                 (["--config", "--spark-conf", "--spark-conf", "k=v"], "custom"),
                 (["--config", "some --spark-conf k=v"], "default"),
                 (["--spark-conf", "k=v", "--unknown"], "custom"),
                 (["--spark-conf"], "default"), (["--unknown", "--spark-conf", "k=v"], "default")]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                m = mf.build_manifest()
                m["run"]["argv"] = argv
                d = agg.derive(m, None)
                self.assertEqual(d["config"]["spark_conf"], expected)
                self.assertEqual(d["spark_conf_source"], "argv")
                self.assertEqual("spark conf" in d["badges"], expected == "custom")
                self.assertEqual("+conf" in d["config_label"], expected == "custom")

    def test_legacy_spark_conf_before_and_after_every_producer_option(self):
        for option in sorted(repro._VALUE_OPTIONS - {"--spark-conf"}):
            cases = [([option, "on", "--spark-conf", "k=v"], "custom"),
                     (["--spark-conf", "k=v", option, "on"], "custom"),
                     ([option, "--spark-conf", "1"], "default"),
                     ([option, "--spark-conf", "--spark-conf", "k=v"], "custom"),
                     (["--spark-conf", "k=v", option, "--spark-conf"], "custom")]
            for argv, expected in cases:
                with self.subTest(argv=argv):
                    m = mf.build_manifest()
                    m["run"]["argv"] = argv
                    d = agg.derive(m, None)
                    self.assertEqual(d["config"]["spark_conf"], expected)
                    self.assertEqual("spark conf" in d["badges"], expected == "custom")
                    self.assertEqual("+conf" in d["config_label"], expected == "custom")
                    self.assertEqual(agg.matches_filter(d, {"spark_conf": "default"}), expected == "default")
        for flag in sorted(repro._FLAGS):
            with self.subTest(flag=flag):
                self.assertTrue(repro.has_option([flag, "--spark-conf", "k=v"], "--spark-conf"))

    def test_legacy_malformed_field_is_unknown_and_does_not_gate(self):
        for value in (None, [], "x", 1, False, {"key": 1}, {"key": None}, {"key": []}):
            with self.subTest(value=value):
                m = mf.build_manifest()
                m["run"]["spark_conf"] = value
                m["run"]["argv"] += ["--spark-conf", "key=value"]
                d = agg.derive(m, None)
                self.assertEqual(d["config"]["spark_conf"], "unknown")
                self.assertEqual(d["spark_conf_source"], "field")
                self.assertNotIn("spark conf", d["badges"])
                self.assertNotIn("+conf", d["config_label"])
                self.assertTrue(d["total_eligible"])
                for state in ("default", "custom"):
                    self.assertFalse(agg.matches_filter(d, {"spark_conf": state}))

    def test_custom_and_default_share_rank_and_hero(self):
        default = row(mf.build_envelope(mf.build_manifest(run_id=mf.fixture_uuid(80), spark_conf={})))
        custom = row(mf.build_envelope(mf.build_manifest(run_id=mf.fixture_uuid(81),
                     spark_conf={"k": "v"}, elapsed=const_elapsed(1))))
        rows = [default, custom]
        self.assertEqual(agg.rank(rows, "total"), [custom, default])
        self.assertIs(agg.hero(rows, agg.DEFAULT_FILTER), custom)
        self.assertIs(agg.hero(rows, {"spark_conf": "default"}), default)
        self.assertIs(agg.hero(rows, {"spark_conf": "custom"}), custom)

    def test_a4_row_with_malformed_settings_rederives_and_reads_over_http(self):
        import sqlite3
        import app
        for value in (None, [], "x", {"key": False}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                    os.environ, {store_mod.DATA_DIR_ENV: tmp}):
                m = mf.build_manifest()
                m["run"]["spark_conf"] = value
                # A4 accepted this unknown field. Store its exact immutable bytes and
                # mark the derived cache as the version actually written by A4.
                store = Store(tmp)
                _, before = store.put(mf.build_envelope(m))
                old = copy.deepcopy(before["derived"])
                old["config"].pop("spark_conf")
                old.pop("spark_conf_source")
                old["derive_version"] = 6
                with sqlite3.connect(store.db_path) as conn:
                    conn.execute("UPDATE runs SET derived_json=?, derive_version=6", (json.dumps(old),))
                client = app.create_app(app.load_config(HERE.parent / "config.yaml")).test_client()
                with mock.patch.object(agg, "derive", wraps=agg.derive) as derive:
                    response = client.get("/api/leaderboard/runs/" + m["run_id"])
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    self.assertEqual(response.get_json()["config"]["spark_conf"], "unknown")
                    self.assertEqual(client.get("/api/leaderboard/runs/" + m["run_id"]).status_code, 200)
                    self.assertEqual(derive.call_count, 1)
                after = store.get(m["run_id"])
                self.assertEqual(after["content_id"], before["content_id"])
                self.assertEqual(after["receipt"], before["receipt"])
                self.assertEqual(after["derived"]["totals"], before["derived"]["totals"])
                self.assertTrue(after["derived"]["total_eligible"])
                self.assertFalse(agg.matches_filter(after["derived"], {"spark_conf": "default"}))
                self.assertEqual(store.read_raw(m["run_id"]), agg.canonical_manifest_bytes(m))
                with sqlite3.connect(store.db_path) as conn:
                    self.assertEqual(conn.execute("SELECT derive_version FROM runs").fetchone()[0], agg.DERIVE_VERSION)


class StoreTests(unittest.TestCase):
    """Identity, receipts, withdrawal, storage failures, concurrency and crash safety at Store level."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lb-store-")
        self.store = Store(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, n=1, **kw):
        return mf.build_envelope(mf.build_manifest(run_id=mf.fixture_uuid(n), **kw))

    def test_lazy_init(self):
        Store(os.path.join(self.tmp, "nope"))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_created_existing_conflict(self):
        env = self.env()
        status, r = self.store.put(env)
        self.assertEqual(status, "created")
        cid = agg.content_id(env["manifest"])
        self.assertEqual(r["content_id"], cid)
        raw = pathlib.Path(self.tmp, "raw", f"{cid}.json")
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), cid)
        self.assertEqual(raw.read_bytes(), agg.canonical_manifest_bytes(env["manifest"]))
        self.assertNotIn("manifest", r["meta"])

        env2 = copy.deepcopy(env)
        env2["note"] = "updated note"
        env2["environment"]["booking_overlap"] = "present"
        env2["submitter"]["email"] = "other@example.com"
        status, r2 = self.store.put(env2)
        self.assertEqual(status, "existing")
        self.assertEqual((r2["content_id"], r2["submitted_at"], r2["run_id"]), (cid, r["submitted_at"], r["run_id"]))
        self.assertEqual(r2["meta"]["note"], "updated note")
        self.assertEqual(r2["submitter_email"], "other@example.com")
        self.assertIn("booking overlap", r2["derived"]["badges"])

        env3 = copy.deepcopy(env)
        env3["manifest"]["run"]["argv"].append("--changed")
        status, r3 = self.store.put(env3)
        self.assertEqual(status, "conflict")
        self.assertEqual(r3, self.store.get(env["manifest"]["run_id"]))
        self.assertEqual((r3["content_id"], r3["meta"]["note"]), (cid, "updated note"))
        self.assertEqual(self.store.load_manifest(r["run_id"]), env["manifest"])
        self.assertEqual(self.store.health()["row_count"], 1)

    def test_list_and_raw_lookup(self):
        self.store.put(self.env(1))
        self.store.put(self.env(2, sf="1000"))
        self.assertEqual([r["sf"] for r in self.store.list_runs()], ["100", "1000"])
        self.assertEqual([r["run_id"] for r in self.store.list_runs(sf="1000")], [mf.fixture_uuid(2)])
        self.assertIsNone(self.store.get(mf.fixture_uuid(9)))
        self.assertIsNone(self.store.read_raw(mf.fixture_uuid(9)))
        self.assertIsNone(self.store.read_raw("../../etc/passwd"))
        p = pathlib.Path(self.tmp, "raw", self.store.get(mf.fixture_uuid(1))["content_id"] + ".json")
        self.assertEqual(self.store.read_raw(mf.fixture_uuid(1)), p.read_bytes())
        # a symlink swapped in for the raw file is never followed
        target = pathlib.Path(self.tmp, "secret")
        target.write_text("secret")
        p.unlink()
        p.symlink_to(target)
        self.assertIsNone(self.store.read_raw(mf.fixture_uuid(1)))
        with self.assertRaises(StorageUnavailable):
            self.store.load_manifest(mf.fixture_uuid(1))
        # a tampered content_id in the DB never becomes a path
        import sqlite3
        with sqlite3.connect(os.path.join(self.tmp, "leaderboard.sqlite3")) as c:
            c.execute("UPDATE runs SET content_id = '../secret' WHERE run_id = ?", (mf.fixture_uuid(2),))
        self.assertIsNone(self.store.read_raw(mf.fixture_uuid(2)))

    def test_withdraw(self):
        env = self.env()
        rid = env["manifest"]["run_id"]
        fast = self.env(2, elapsed=const_elapsed(1))
        self.store.put(env)
        self.store.put(fast)
        self.assertIsNone(self.store.withdraw(mf.fixture_uuid(9), "a", "b"))
        first = self.store.withdraw(fast["manifest"]["run_id"], "alice", "bad device")
        self.assertTrue(first["withdrawn"])
        self.assertEqual((first["withdrawn_by"], first["withdrawn_reason"]), ("alice", "bad device"))
        again = self.store.withdraw(fast["manifest"]["run_id"], "bob", "other")
        self.assertEqual((again["withdrawn_at"], again["withdrawn_by"], again["withdrawn_reason"]),
                         (first["withdrawn_at"], "alice", "bad device"))
        self.assertEqual([r["run_id"] for r in self.store.list_runs()], [rid])
        self.assertEqual(len(self.store.list_runs(include_withdrawn=True)), 2)
        rows = self.store.list_runs(include_withdrawn=True)
        self.assertEqual(agg.hero(rows, agg.DEFAULT_FILTER)["run_id"], rid)
        self.assertEqual([r["run_id"] for r in agg.rank(rows, "total")], [rid])
        status, r = self.store.put(fast)                              # identical re-upload does not revive
        self.assertEqual(status, "existing")
        self.assertTrue(r["withdrawn"])
        self.assertEqual(r["withdrawn_by"], "alice")

    def test_missing_dir(self):
        missing = os.path.join(self.tmp, "absent")
        s = Store(missing)
        for call in (lambda: s.put(self.env()), lambda: s.get("x"), s.list_runs, lambda: s.read_raw("x"),
                     lambda: s.withdraw("x", "a", "b"), lambda: s.load_manifest("x")):
            with self.assertRaises(StorageUnavailable):
                call()
        h = s.health()
        self.assertFalse(h["ok"])
        self.assertIn("missing or not writable", h["error"])
        self.assertFalse(os.path.exists(missing), "Store never creates the data dir")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_read_only_dir(self):
        self.store.put(self.env())
        os.chmod(self.tmp, 0o555)
        try:
            with self.assertRaises(StorageUnavailable):
                Store(self.tmp).list_runs()
        finally:
            os.chmod(self.tmp, 0o755)

    def test_storage_error_is_fixed_text(self):
        with mock.patch("sqlite3.connect", side_effect=__import__("sqlite3").OperationalError("disk I/O <x>")):
            with self.assertRaises(StorageUnavailable) as cm:
                Store(self.tmp).get("x")
        self.assertNotIn("<x>", str(cm.exception))

    def test_concurrent_puts(self):
        envs = [self.env(n) for n in (1, 2)]
        envs += [copy.deepcopy(envs[0]), copy.deepcopy(envs[0])]
        barrier = threading.Barrier(len(envs))
        results, errors = [], []

        def worker(e):
            barrier.wait()
            try:
                results.append(self.store.put(e)[0])
            except Exception as exc:                                   # surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(e,)) for e in envs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["created", "created", "existing", "existing"])
        self.assertEqual(len(self.store.list_runs()), 2)
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "raw"))), 2, "no tmp files left behind")

    def test_crash_between_raw_and_row_then_retry(self):
        env = self.env()
        with mock.patch.object(Store, "_commit_row", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.store.put(env)
        cid = agg.content_id(env["manifest"])
        raw = pathlib.Path(self.tmp, "raw", f"{cid}.json")
        self.assertTrue(raw.is_file())
        self.assertEqual(self.store.list_runs(), [])
        before = os.stat(raw)
        status, r = Store(self.tmp).put(env)
        self.assertEqual((status, r["content_id"]), ("created", cid))
        after = os.stat(raw)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns), "raw file reused")
        self.assertEqual(sorted(os.listdir(raw.parent)), [f"{cid}.json"])

    def test_corrupt_orphan_is_rewritten(self):
        env = self.env()
        cid = agg.content_id(env["manifest"])
        os.makedirs(os.path.join(self.tmp, "raw"))
        pathlib.Path(self.tmp, "raw", f"{cid}.json").write_bytes(b"truncated")
        self.store.put(env)
        self.assertEqual(self.store.load_manifest(env["manifest"]["run_id"]), env["manifest"])

    def test_stale_derive_version_recomputed(self):
        env = self.env()
        self.store.put(env)
        import sqlite3
        with sqlite3.connect(os.path.join(self.tmp, "leaderboard.sqlite3")) as c:
            c.execute("UPDATE runs SET derived_json = '{}', derive_version = 0")
        r = self.store.get(env["manifest"]["run_id"])
        self.assertEqual(r["derived"], agg.derive(env["manifest"], env))
        self.assertEqual(r["derived"]["repro"], repro.script(env["manifest"]))

    def test_stale_derivation_is_persisted_once_without_changing_metadata(self):
        import sqlite3
        env = self.env()
        rid = env["manifest"]["run_id"]
        self.store.put(env, ["original warning"])
        original = self.store.withdraw(rid, "actor", "reason")
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("UPDATE runs SET derived_json = '{}', derive_version = 0, total_eligible = 0")
            conn.row_factory = sqlite3.Row
            before = dict(conn.execute("SELECT * FROM runs").fetchone())
        with mock.patch.object(agg, "derive", wraps=agg.derive) as derive:
            self.assertEqual(self.store.get(rid), original)
            self.assertEqual(Store(self.tmp).list_runs(include_withdrawn=True), [original])
            self.assertEqual(self.store.get(rid), original)
        self.assertEqual(derive.call_count, 1)
        with sqlite3.connect(self.store.db_path) as conn:
            conn.row_factory = sqlite3.Row
            after = dict(conn.execute("SELECT * FROM runs").fetchone())
        for key in before.keys() - {"derived_json", "derive_version", "total_eligible"}:
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after["derive_version"], agg.DERIVE_VERSION)
        self.assertEqual(json.loads(after["derived_json"]), original["derived"])
        self.assertEqual(after["total_eligible"], 1)

    def test_stale_derivation_failure_is_not_persisted(self):
        import sqlite3
        env = self.env()
        rid = env["manifest"]["run_id"]
        _, row = self.store.put(env)
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("UPDATE runs SET derive_version = 0")
        pathlib.Path(self.tmp, "raw", row["content_id"] + ".json").unlink()
        for read in (lambda: self.store.get(rid), self.store.list_runs):
            with self.assertRaises(StorageUnavailable):
                read()
        with sqlite3.connect(self.store.db_path) as conn:
            self.assertEqual(conn.execute("SELECT derive_version FROM runs").fetchone()[0], 0)

    def test_stale_list_derivation_commits_all_rows_once(self):
        import sqlite3
        original = [self.store.put(self.env(n, repeats=1))[1] for n in range(1, 201)]
        original[-1] = self.store.withdraw(original[-1]["run_id"], "actor", "reason")
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("UPDATE runs SET derived_json = '{}', derive_version = 0, total_eligible = 0")
        statements = []
        connect = self.store._connect

        def traced_connect():
            conn = connect()
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(self.store, "_connect", side_effect=traced_connect):
            with mock.patch.object(agg, "derive", wraps=agg.derive) as derive:
                self.assertEqual(self.store.list_runs(include_withdrawn=True), original)
                self.assertEqual(self.store.list_runs(include_withdrawn=True), original)
        self.assertEqual(derive.call_count, 200)
        self.assertEqual(statements.count("BEGIN IMMEDIATE"), 1)
        self.assertEqual(statements.count("COMMIT"), 1)
        with sqlite3.connect(self.store.db_path) as conn:
            persisted = conn.execute("SELECT derive_version, total_eligible, derived_json FROM runs").fetchall()
        self.assertEqual(len(persisted), 200)
        self.assertTrue(all(version == agg.DERIVE_VERSION and eligible == 1 and json.loads(derived)["total_eligible"]
                            for version, eligible, derived in persisted))

    def test_stale_list_derivation_failure_rolls_back_every_row(self):
        import sqlite3
        for n in (1, 2):
            self.store.put(self.env(n))
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("UPDATE runs SET derived_json = '{}', derive_version = 0, total_eligible = 0")
            before = conn.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        read = self.store._read_manifest
        seen = []

        def fail_second(cid):
            seen.append(cid)
            if len(seen) == 2:
                raise StorageUnavailable("injected raw read failure")
            return read(cid)

        with mock.patch.object(self.store, "_read_manifest", side_effect=fail_second):
            with self.assertRaises(StorageUnavailable):
                self.store.list_runs()
        self.assertEqual(len(seen), 2)
        with sqlite3.connect(self.store.db_path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM runs ORDER BY run_id").fetchall(), before)

    def test_list_runs_orders_parsed_instants(self):
        envs = []
        for idx, end in enumerate(("2026-10-01T00:21:00.000000001Z", "2026-10-01T00:21:00Z",
                                   "2026-10-01T00:21:00.5Z")):
            env = self.env(5000 + idx)
            env["manifest"]["run"]["finished_at"] = end
            self.store.put(env)
            envs.append(env)
        self.assertEqual([r["finished_at"] for r in self.store.list_runs()],
                         [envs[i]["manifest"]["run"]["finished_at"] for i in (1, 0, 2)])

    def test_put_canonicalizes_and_hashes_manifest_once(self):
        env = self.env()
        with mock.patch.object(agg, "canonical_manifest_bytes", wraps=agg.canonical_manifest_bytes) as canonical:
            with mock.patch.object(store_mod.hashlib, "sha256", wraps=hashlib.sha256) as sha:
                _, r = self.store.put(env)
        self.assertEqual(canonical.call_count, 1)
        self.assertEqual(sha.call_count, 1)
        self.assertEqual(self.store.read_raw(r["run_id"]), agg.canonical_manifest_bytes(env["manifest"]))
        self.assertEqual(r["content_id"], r["derived"]["content_id"])

    def test_rows_from_the_previous_derive_version_pick_up_new_rules(self):
        """A row stored by derive version 1 (before wrong answers in failed repeats counted) re-derives on read."""
        env = self.env()
        env["manifest"]["repeats"][1]["status"] = "failed"
        env["manifest"]["repeats"][1]["queries"][4]["engines"]["xflare"]["status"] = "error"
        rid = env["manifest"]["run_id"]
        self.store.put(env)
        old = agg.derive(env["manifest"], env)
        old["per_query"]["5"]["eligible"] = True
        old["ineligible_reasons"].pop("q5", None)
        import sqlite3
        with sqlite3.connect(os.path.join(self.tmp, "leaderboard.sqlite3")) as c:
            c.execute("UPDATE runs SET derived_json = ?, derive_version = 1", (json.dumps(old),))
        d = self.store.get(rid)["derived"]
        self.assertFalse(d["per_query"]["5"]["eligible"])
        self.assertEqual(d["ineligible_reasons"]["q5"], ["xflare error in failed repeat 2"])

    def test_wal_and_user_version(self):
        self.store.put(self.env())
        import sqlite3
        c = sqlite3.connect(os.path.join(self.tmp, "leaderboard.sqlite3"))
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 3)
        c.close()

    def test_v1_database_gains_the_target_tables(self):
        """A database written before A8 (user_version 1, runs only) opens with the target tables added and its runs
        intact: the live service's upgrade path."""
        run_id = self.store.put(self.env())[1]["run_id"]
        import sqlite3
        c = sqlite3.connect(os.path.join(self.tmp, "leaderboard.sqlite3"))
        c.executescript("DROP TABLE target_events; DROP TABLE targets; DROP TABLE run_attribution;"
                        " DROP TABLE prset_attribution; DROP TABLE attribution_events; DROP TABLE pr_checks;"
                        " PRAGMA user_version = 1;")
        c.close()
        s = Store(self.tmp)
        self.assertEqual(s.get(run_id)["run_id"], run_id)
        self.assertIsNone(s.get_target("100", "snappy"))
        self.assertEqual(s.set_target("100", "snappy", run_id, None, "a", 0)[0], "set")
        c = sqlite3.connect(os.path.join(self.tmp, "leaderboard.sqlite3"))
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 3)
        c.close()

    def test_basis_edit_and_its_event_are_one_transaction(self):
        """A9: a failed event write leaves the field and revision as they were (and a failed field write adds no
        event); the submission's initial claim is revision 1 with its event."""
        env = self.env()
        env["basis"] = {"kind": "pr", "prs": [{"repo": "B/c", "number": 2}, {"repo": "a/b", "number": 1}]}
        run = self.store.put(env)[1]["run_id"]
        state, events, more = self.store.attribution_history("run", run)
        self.assertEqual((state["basis"]["prs"][0]["repo"], state["revision"], len(events), more), ("a/b", 1, 1, False))
        import sqlite3
        db = os.path.join(self.tmp, "leaderboard.sqlite3")
        with sqlite3.connect(db) as c:
            c.execute("ALTER TABLE attribution_events RENAME TO gone")
        with self.assertRaises(StorageUnavailable):
            self.store.edit("run", run, "report_url", "https://x.example/", "a", 1)
        with sqlite3.connect(db) as c:
            c.execute("ALTER TABLE gone RENAME TO attribution_events")
            self.assertEqual(c.execute("SELECT revision, report_url_json FROM run_attribution").fetchall(), [(1, None)])
        self.assertEqual(self.store.edit("run", run, "report_url", "https://x.example/", "a", 1)[0], "set")
        self.assertEqual(self.store.edit("run", run, "report_url", "https://x.example/", "b", 2)[0], "unchanged")
        self.assertEqual(self.store.edit("run", mf.fixture_uuid(9), "report_url", None, "b", 0), ("unknown", None))
        self.assertEqual(len(self.store.attribution_history("run", run)[1]), 2)

    def test_a_stale_identical_edit_is_a_conflict(self):
        """A9: a lost-response retry of an edit that landed (same value, the old revision) is a conflict that shows
        the current state, not "unchanged"; a later body check of a PR replaces the earlier one."""
        run = self.store.put(self.env())[1]["run_id"]
        self.assertEqual(self.store.edit("run", run, "report_url", "https://x.example/", "a", 0)[0], "set")
        status, state = self.store.edit("run", run, "report_url", "https://x.example/", "a", 0)
        self.assertEqual((status, state["revision"], state["report_url"]), ("conflict", 1, "https://x.example/"))
        self.assertEqual(len(self.store.attribution_history("run", run)[1]), 1)
        self.store.record_check("a/b", 1, [], None, None, "x")
        self.store.record_check("a/b", 1, [run], None, "file", "y")
        self.assertEqual((self.store.checks([("a/b", 1)])[("a/b", 1)]["run_ids"],
                          self.store.checks([("a/b", 1)])[("a/b", 1)]["checked_by"]), ([run], "y"))

    def test_user_version_is_raised_never_lowered_and_follows_the_tables(self):
        """A9: a database another binary marked newer keeps its number; a failed setup leaves the old number and
        the next open finishes it."""
        import sqlite3
        db = os.path.join(self.tmp, "leaderboard.sqlite3")
        run = self.store.put(self.env())[1]["run_id"]
        with sqlite3.connect(db) as c:
            c.execute("PRAGMA user_version = 4")
        Store(self.tmp).list_runs()
        with sqlite3.connect(db) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 4)
            c.executescript("DROP TABLE pr_checks; DROP TABLE attribution_events; PRAGMA user_version = 2;")
        broken = store_mod._SCHEMA.replace("CREATE TABLE IF NOT EXISTS pr_checks", "CREATE TABLE IF NOT EXISTS pr_checks (x")
        with mock.patch.object(store_mod, "_SCHEMA", broken), self.assertRaises(StorageUnavailable):
            Store(self.tmp).list_runs()
        with sqlite3.connect(db) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 2)
        again = Store(self.tmp)
        self.assertEqual(again.edit("run", run, "basis", {"kind": "main", "repo": "a/b", "latest_pr": 1}, "a", 0)[0], "set")
        again.record_check("a/b", 1, [run], None, None, "a")
        with sqlite3.connect(db) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_one_run_reads_only_its_own_sets_report(self):
        """Reading one run looks up the Report of its own PR set only, however many other sets have one."""
        env = self.env()
        env["basis"] = {"kind": "pr", "prs": [{"repo": "a/b", "number": 1}]}
        run = self.store.put(env)[1]["run_id"]
        for n in range(2, 80):
            self.store.edit("prset", f"a/b#{n}", "report_url", f"https://x.example/{n}", "a", 0)
        self.store.edit("prset", "a/b#1", "report_url", "https://x.example/own", "a", 0)
        statements, connect = [], Store._connect

        def traced(store):
            conn = connect(store)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(Store, "_connect", traced):
            self.assertEqual(self.store.get(run)["attribution"]["prset_report_url"], "https://x.example/own")
        reads = [x for x in statements if "FROM prset_attribution" in x]
        self.assertEqual(len(reads), 1)
        self.assertIn("WHERE prset = 'a/b#1'", reads[0])             # its own set's row, not every Report
        self.assertEqual(self.store.prset_reports(["a/b#5", "z/z#1"]), {"a/b#5": "https://x.example/5"})
        many = [self.store.put(self.env(n))[1]["run_id"] for n in range(2, 4)]
        self.assertEqual({r["run_id"]: r["attribution"]["prset_report_url"] for r in self.store.list_runs()},
                         {run: "https://x.example/own", **{m: None for m in many}})

    def test_best_so_far(self):
        """A9: heroes are the record breakers in finish order under the board's order: an equal later run with the same
        repeats does not displace, one with more repeats does; withdrawn and ineligible runs are never points."""
        def r(n, ms, minutes, repeats=3, withdrawn=False, **kw):
            env = mf.build_envelope(mf.build_manifest(run_id=mf.fixture_uuid(n), elapsed=const_elapsed(ms),
                                                      repeats=repeats, started_at=mf.T0 + timedelta(minutes=minutes),
                                                      **kw))
            return row(env, withdrawn)
        rows = [r(1, 500, 0), r(2, 600, 100), r(3, 400, 200), r(4, 400, 300), r(5, 400, 400, repeats=5),
                r(6, 100, 500, withdrawn=True), r(7, 50, 600, record_answers=True)]
        timeline, heroes = agg.best_so_far(rows)
        self.assertEqual([x["run_id"] for x in timeline], [mf.fixture_uuid(n) for n in (1, 2, 3, 4, 5)])
        self.assertEqual([x["run_id"] for x in heroes], [mf.fixture_uuid(n) for n in (1, 3, 5)])
        self.assertEqual([x["run_id"] for x in agg.best_so_far(rows, "q4")[1]], [mf.fixture_uuid(n) for n in (1, 3, 5)])
        self.assertEqual(agg.best_so_far([], "q1"), ([], []))

    def test_target_history_keeps_the_latest_50(self):
        """The history read lists at most 50 changes, newest first; every change stays stored."""
        s = self.store
        ids = [s.put(self.env(n))[1]["run_id"] for n in (1, 2)]
        for revision in range(55):
            self.assertEqual(s.set_target("100", "snappy", ids[revision % 2], None, "a", revision)[0], "set")
        pin, events = s.target_history("100", "snappy")
        self.assertEqual((len(events), events[0]["revision"], events[-1]["revision"], pin["revision"]), (50, 55, 6, 55))

    def test_target_revisions_and_history(self):
        """A8: a target change applies only at the revision the caller saw and only to a stored, non-withdrawn run;
        a repeat of the stored state writes nothing; every change is one event, newest first."""
        s = self.store
        r1, r2 = (s.put(self.env(n))[1]["run_id"] for n in (1, 2))
        self.assertIsNone(s.get_target("100", "snappy"))
        self.assertEqual(s.set_target("100", "snappy", r1, 2.0, "a", 1), ("conflict", None))
        self.assertEqual(s.set_target("100", "snappy", mf.fixture_uuid(9), 2.0, "a", 0), ("withdrawn", None))
        status, state = s.set_target("100", "snappy", r1, 2.0, "a", 0)
        self.assertEqual((status, state["run_id"], state["goal"], state["revision"], state["updated_by"]),
                         ("set", r1, 2.0, 1, "a"))
        self.assertEqual(s.set_target("100", "snappy", r1, 2.0, "b", 1)[0], "unchanged")
        self.assertEqual(s.set_target("100", "snappy", r1, 3.0, "b", 1)[1]["revision"], 2)      # the goal alone
        s.withdraw(r2, "x", "y")
        self.assertEqual(s.set_target("100", "snappy", r2, None, "b", 2)[0], "withdrawn")
        self.assertEqual(s.set_target("100", "snappy", None, None, "c", 1)[0], "conflict")
        self.assertEqual(s.set_target("100", "snappy", None, None, "c", 2)[1]["run_id"], None)
        pin, events = s.target_history("100", "snappy")
        self.assertEqual([(e["revision"], e["before_run_id"], e["after_run_id"], e["before_goal"], e["after_goal"],
                           e["actor"]) for e in events],
                         [(3, r1, None, 3.0, None, "c"), (2, r1, r1, 2.0, 3.0, "b"), (1, None, r1, None, 2.0, "a")])
        self.assertEqual(pin["revision"], events[0]["revision"])        # read together: the newest change listed
        self.assertIsNone(s.get_target("1000", "snappy"))
        self.assertEqual(s.target_history("1000", "snappy"), (None, []))

    def test_reuse_finishes_durability_barriers(self):
        """Crash after os.replace but before the raw-dir fsync; the retry fsyncs file and dir before the row."""
        env = self.env()
        real_fsync, real_commit = os.fsync, Store._commit_row
        seen = []

        def fsync_dir_fails(fd):                     # call 1 = tmp file, call 2 = raw dir
            seen.append(fd)
            if len(seen) == 2:
                raise OSError(errno.EIO, "injected")
            real_fsync(fd)

        with mock.patch.object(store_mod.os, "fsync", side_effect=fsync_dir_fails):
            with self.assertRaises(StorageUnavailable):
                self.store.put(env)
        cid = agg.content_id(env["manifest"])
        self.assertTrue(pathlib.Path(self.tmp, "raw", f"{cid}.json").is_file())
        self.assertEqual(self.store.list_runs(), [])

        calls = []
        with mock.patch.object(store_mod.os, "fsync", side_effect=lambda fd: (calls.append("fsync"), real_fsync(fd))), \
                mock.patch.object(Store, "_commit_row", lambda *a: (calls.append("row"), real_commit(*a))[1]):
            status, _ = self.store.put(env)
        self.assertEqual(status, "created")
        self.assertEqual(calls, ["fsync", "fsync", "row"])  # reused file, raw dir, then the row

    @NOT_ROOT
    def test_non_traversable_parent(self):
        """A data dir whose parent cannot be traversed is StorageUnavailable, not a PermissionError."""
        parent = pathlib.Path(self.tmp, "locked")
        data = parent / "data"
        data.mkdir(parents=True)
        os.chmod(parent, 0)
        try:
            s = Store(data)
            for call in (s.list_runs, lambda: s.get("x"), lambda: s.read_raw("x"), lambda: s.put(self.env()),
                         lambda: s.withdraw("x", "a", "b")):
                with self.assertRaises(StorageUnavailable):
                    call()
            self.assertFalse(s.health()["ok"])
        finally:
            os.chmod(parent, 0o755)

    def test_receipt_replayed(self):
        """"existing" returns the creation receipt, even after a withdrawal; metadata still updates."""
        env = self.env()
        rid = env["manifest"]["run_id"]
        _, r = self.store.put(env, warnings=["$.'x': unknown property (kept, not interpreted)"])
        self.assertEqual(r["receipt"], {"ranked_total": True,
                                       "warnings": ["$.'x': unknown property (kept, not interpreted)"]})
        self.store.withdraw(rid, "alice", "bad")
        env2 = copy.deepcopy(env)
        env2["note"] = "later"
        status, r2 = self.store.put(env2, warnings=[])
        self.assertEqual((status, r2["receipt"], r2["meta"]["note"]), ("existing", r["receipt"], "later"))
        self.assertEqual(self.store.get(rid)["receipt"], r["receipt"])
        _, p = self.store.put(load("partial.json"))
        self.assertEqual(p["receipt"], {"ranked_total": False, "warnings": []})

    def test_every_connection_waits_for_the_process_lock(self):
        """Reads and the one-time init (journal_mode, DDL, user_version) run under the process-wide lock."""
        self.store.put(self.env())
        fresh = pathlib.Path(self.tmp, "fresh")
        fresh.mkdir()
        done = [threading.Event(), threading.Event()]
        threads = [threading.Thread(target=lambda: (Store(self.tmp).list_runs(), done[0].set())),
                   threading.Thread(target=lambda: (Store(fresh).health(), done[1].set()))]
        with store_mod._LOCK:
            for t in threads:
                t.start()
            self.assertFalse(done[0].wait(0.3), "a read ran while another thread held the lock")
            self.assertFalse(done[1].is_set())
            self.assertFalse((fresh / store_mod.DB_NAME).exists(), "init ran outside the lock")
        for t in threads:
            t.join(5)
        self.assertTrue(all(e.is_set() for e in done))
        self.assertTrue((fresh / store_mod.DB_NAME).exists())

    def test_read_raw(self):
        env = self.env()
        env["manifest"]["run"]["blob"] = "x" * (9 * 2 ** 20)          # canonical file larger than the 8 MiB body cap
        rid = env["manifest"]["run_id"]
        self.store.put(env)
        want = agg.canonical_manifest_bytes(env["manifest"])
        self.assertEqual(self.store.read_raw(rid), want)
        self.assertIsNone(self.store.read_raw(mf.fixture_uuid(9)))     # no row
        raw = pathlib.Path(self.tmp, "raw", f"{agg.content_id(env['manifest'])}.json")
        moved = pathlib.Path(self.tmp, "moved")
        raw.rename(moved)
        self.assertIsNone(self.store.read_raw(rid))                    # ENOENT
        raw.symlink_to(moved)
        self.assertIsNone(self.store.read_raw(rid))                    # ELOOP: never followed
        raw.unlink()
        self.assertEqual(self.store.put(env)[0], "existing")           # re-upload restores the file
        self.assertEqual(self.store.read_raw(rid), want)
        raw.write_bytes(want[:-1] + b" ")
        with self.assertRaises(StorageUnavailable):
            self.store.read_raw(rid)

    @NOT_ROOT
    def test_read_raw_permission_denied(self):
        env = self.env()
        rid = env["manifest"]["run_id"]
        self.store.put(env)
        raw = pathlib.Path(self.tmp, "raw", f"{agg.content_id(env['manifest'])}.json")
        os.chmod(raw, 0)
        try:
            with self.assertRaises(StorageUnavailable):                # EACCES is an outage, not "missing"
                self.store.read_raw(rid)
        finally:
            os.chmod(raw, 0o644)

    def test_read_raw_fifo_does_not_block(self):
        """A FIFO planted at the raw path is "not a regular file": read_raw returns None at once, with no writer."""
        env = self.env()
        rid = env["manifest"]["run_id"]
        self.store.put(env)
        raw = pathlib.Path(self.tmp, "raw", f"{agg.content_id(env['manifest'])}.json")
        raw.unlink()
        os.mkfifo(raw)
        result = []
        t = threading.Thread(target=lambda: result.append(self.store.read_raw(rid)), daemon=True)
        t.start()
        t.join(3)
        if t.is_alive():                      # unblock the stuck open so the test process can exit
            os.close(os.open(raw, os.O_WRONLY | os.O_NONBLOCK))
            t.join(3)
            self.fail("read_raw blocked on a FIFO")
        self.assertEqual(result, [None])

    def test_read_raw_io_errors_are_storage_unavailable(self):
        """EIO from fstat or from the read itself inside read_raw is an outage, never None or a raw OSError."""
        env = self.env()
        rid = env["manifest"]["run_id"]
        self.store.put(env)

        class EIOFile(io.FileIO):
            def read(self, *a):
                raise OSError(errno.EIO, "injected")

        with mock.patch.object(store_mod.os, "fstat", side_effect=OSError(errno.EIO, "injected")):
            with self.assertRaises(StorageUnavailable):
                self.store.read_raw(rid)
        with mock.patch.object(store_mod.os, "fdopen", side_effect=lambda fd, mode: EIOFile(fd, "rb")):
            with self.assertRaises(StorageUnavailable):
                self.store.read_raw(rid)
        self.assertEqual(self.store.read_raw(rid), agg.canonical_manifest_bytes(env["manifest"]))

    def test_store_for(self):
        other = os.path.join(self.tmp, "other")
        cfg = {"storage": {"data_dir": self.tmp}}
        with mock.patch.dict(os.environ, {store_mod.DATA_DIR_ENV: ""}):
            self.assertIs(store_for(cfg), store_for(copy.deepcopy(cfg)))
            self.assertEqual(store_for(cfg).data_dir, pathlib.Path(self.tmp))
            self.assertEqual(store_mod.resolve_data_dir({}), store_mod.DEFAULT_DATA_DIR)
        with mock.patch.dict(os.environ, {store_mod.DATA_DIR_ENV: other}):
            self.assertEqual(store_for(cfg).data_dir, pathlib.Path(other))

    def test_huge_values_round_trip(self):
        env = self.env()
        for rep in env["manifest"]["repeats"]:
            rep["queries"][0]["engines"]["velox"]["elapsed_ms"] = BIG
        status, r = self.store.put(env)
        self.assertEqual((status, r["derived"]["per_query"]["1"]["velox_ms"]), ("created", BIG))
        self.assertEqual(self.store.load_manifest(env["manifest"]["run_id"]), env["manifest"])

    def test_hostile_strings_stored_verbatim(self):
        hostile = "<img src=x onerror=alert(1)>"
        env = self.env()
        env["note"] = hostile
        env["submitter"]["email"] = hostile
        env["manifest"]["run"]["argv"].append(hostile)
        env["manifest"]["run"]["xflare_overrides"][0]["value"] = hostile
        self.assertEqual(schema.validate_envelope(env)[0], [])
        _, r = self.store.put(env)
        self.assertEqual((r["meta"]["note"], r["derived"]["submitter"]), (hostile, hostile))


SUITES = ("test_leaderboard_web", "test_leaderboard_backup", "test_leaderboard_auto_update")


def load_tests(loader, tests, pattern):
    """Single runner: the web, backup and auto-update suites are part of it; a missing one is an import error, not a
    skip."""
    for name in SUITES:
        spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        tests.addTests(loader.loadTestsFromModule(mod))
    return tests


if __name__ == "__main__":
    unittest.main()
