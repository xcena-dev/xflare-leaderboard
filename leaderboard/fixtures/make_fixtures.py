#!/usr/bin/env python3
"""Deterministic contract-v1 fixtures. Run: python3 make_fixtures.py  (rewrites the *.json next to it)

The builders are also the shared test factory: `build_manifest(**kw)` gives an SF-100, 4x6, admin-bundle,
N=3, all-22-queries, all-ok manifest; tests override keywords or mutate the result.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

QUERIES = list(range(1, 23))
T0 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)


def fixture_uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hex(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:n]


def default_elapsed(q: int, r: int, engine: str) -> int:
    """Milliseconds for query q, repeat r. velox ~2.5x slower; repeats drift a little so medians are non-trivial."""
    base = 1000 + 37 * q if engine == "xflare" else 2500 + 91 * q
    return base + (0, 9, 4)[(r - 1) % 3]


def _engine_result(ms, record_answers: bool, r: int) -> dict:
    verify = ({"against": "previous", "status": "new" if r == 1 else "ok", "detail": None} if record_answers
              else {"against": "answers", "status": "ok", "detail": None})
    return {"status": "ok", "elapsed_ms": ms, "rows": 4, "verify": verify, "error": None}


def build_manifest(*, run_id: str = fixture_uuid(1), sf: str = "100", executors: int = 4, executor_cores: int = 6,
                   offheap: str = "64g", repeats: int = 3, bundle_kind: str = "admin",
                   bundle: str = "xflare-admin-2026.10.1", artifact_id: str = "xflare-build-0001",
                   record_answers: bool = False, elapsed=default_elapsed, queries=QUERIES,
                   engines=("velox", "xflare"), started_at: datetime = T0, seed: str = "",
                   application_mode: str | None = None) -> dict:
    """`elapsed(q, r, engine) -> int` sets every timing; `seed` varies the hashes/revisions between runs."""
    seed = seed or run_id
    toml_in = f"[resource]\nexecutors = {executors}\ncores = {executor_cores}\n"
    toml_out = toml_in + "[logging]\nlevel = \"info\"\n"
    reps = []
    t = started_at
    for r in range(1, repeats + 1):
        qs = []
        for q in queries:
            qs.append({
                "id": q,
                "engines": {e: _engine_result(elapsed(q, r, e), record_answers, r) for e in engines},
                "evidence": {"status": "offloaded", "detail": None},
            })
        end = t + timedelta(minutes=20)
        reps.append({
            "index": r, "invocation": f"spark-submit --app tpch-{sf}-r{r}", "app_id": f"app-{_hex(seed + str(r), 8)}",
            "artifact_id": artifact_id, "bundle": bundle, "started_at": ts(t), "finished_at": ts(end),
            "status": "completed", "cleanup": "ok", "queries": qs,
        })
        if application_mode == "per-engine":
            applications = []
            for pos, engine in enumerate(engines):
                app_start = t + (end - t) * pos / len(engines)
                app_end = t + (end - t) * (pos + 1) / len(engines)
                applications.append({
                    "engine": engine, "invocation": f"spark-submit --app tpch-{sf}-r{r}-{engine}",
                    "app_id": f"app-{_hex(seed + str(r) + engine, 8)}", "artifact_id": artifact_id,
                    "bundle": bundle, "started_at": ts(app_start), "finished_at": ts(app_end), "cleanup": "ok",
                })
            reps[-1]["applications"] = applications
            if applications:
                for key in ("invocation", "app_id", "started_at"):
                    reps[-1][key] = applications[0][key]
                reps[-1]["finished_at"] = applications[-1]["finished_at"]
        t = end + timedelta(minutes=1)
    argv = ["--reservation", "4242", "--xflare-toml", "scripts/config/Resource.mu.d1.toml.in",
            "--scale-factor", sf, "--engines", ",".join(engines), "--repeat", str(repeats),
            "--executors", str(executors), "--executor-cores", str(executor_cores),
            "--offheap", offheap, "--submission", "out.json"]
    if record_answers:
        argv.append("--record-answers")
    if application_mode == "per-engine":
        argv.append("--application-per-engine")
    argv += [str(q) for q in queries]
    manifest = {
        "schema": "xflare-bench-manifest/1",
        "run_id": run_id,
        "created_at": ts(started_at),
        "client": {"revision": _hex("client" + seed, 40), "branch": "main", "dirty": False, "dirty_digest": None},
        "build": {
            "artifact_id": artifact_id, "provenance_source": "artifact-manifest",
            "revision": _hex("build" + seed, 40), "dirty": False, "dirty_digest": None,
            "bundle": {"requested": bundle, "resolved": bundle, "kind": bundle_kind},
            "gluten": ({"commit": _hex("gluten" + seed, 40), "dirty": False, "build_command": "make gluten"}
                       if bundle_kind == "dev" else None),
        },
        "run": {
            "argv": argv,
            "scale_factor": sf,
            "codec": "snappy",
            "engines": list(engines),
            "queries": list(queries),
            "repeat_requested": repeats,
            "record_answers": record_answers,
            "answers": {"path": f"scripts/spark/expected/sf_{sf}.json.gz", "sha256": _hex("answers" + sf, 64)},
            "resources": {"executors": executors, "executor_cores": executor_cores, "offheap": offheap,
                          "shuffle_partitions": None, "devices": None},
            "xflare_toml": {
                "name": "Resource.mu.d1.toml.in",
                "input_text": toml_in, "input_sha256": hashlib.sha256(toml_in.encode()).hexdigest(),
                "sent_sha256": hashlib.sha256(toml_out.encode()).hexdigest(),
                "rendered_text": toml_out, "rendered_sha256": hashlib.sha256(toml_out.encode()).hexdigest(),
            },
            "xflare_overrides": [
                {"key": "join.output_batch_rows", "value": "7680", "source": "default", "destination": "session"},
            ],
            "dataset": {"uri": f"hdfs:///tpch/sf{sf}", "marker": f"tpch-sf{sf}-v1"},
            "started_at": ts(started_at),
            "finished_at": ts(t),
            "exit_status": "ok",
        },
        "repeats": reps,
    }
    if application_mode is not None:
        manifest["run"]["application_mode"] = application_mode
    return manifest


def build_envelope(manifest: dict, **overrides) -> dict:
    """Envelope around `manifest`; keyword overrides replace top-level envelope keys."""
    run = manifest["run"]
    env = {
        "schema": "xflare-bench-submission/1",
        "manifest": manifest,
        "submitter": {"email": "bench@example.com", "sdp_id": None},
        "note": "fixture",
        "isolation_claim": "exclusive",
        "producer": "xflare-bench-submit@" + manifest["client"]["revision"],
        "environment": {
            "source": "reservation-server", "observed_at": run["started_at"], "host": "GNR-3",
            "devices": [{"id": "0", "serial": "SN-0001", "type": "MX1P", "fw": "1.2.3"}],
            "sdk": "2026.10.0", "pxl": "1.8.0",
            "reservation": {"id": "rsv-1", "owner": "bench@example.com",
                            "start": run["started_at"], "end": run["finished_at"]},
            "booking_overlap": "none",
            "overlapping": [],
            "cpu_contention": "unmeasured",
        },
    }
    env.update(copy.deepcopy(overrides))
    return env


def velox_mismatch_envelope() -> dict:
    m = build_manifest(run_id=fixture_uuid(2))
    m["repeats"][1]["queries"][6]["engines"]["velox"]["verify"] = {
        "against": "answers", "status": "mismatch", "detail": "row 3 differs"}
    return build_envelope(m)


def partial_envelope() -> dict:
    """q21 missing from repeat 2, q22 xflare error in every repeat, exit failed."""
    m = build_manifest(run_id=fixture_uuid(3))
    del m["repeats"][1]["queries"][20]
    for rep in m["repeats"]:
        rep["queries"][-1]["engines"]["xflare"] = {
            "status": "error", "elapsed_ms": None, "rows": None,
            "verify": {"against": None, "status": None, "detail": None}, "error": "OffloadException: dummy"}
    m["run"]["exit_status"] = "failed"
    return build_envelope(m)


def render_all() -> dict[str, bytes]:
    """File name -> exact bytes. Reject fixtures are raw text: a dict cannot hold duplicate keys or NaN."""
    def dump(obj):
        return (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode("utf-8")

    valid = build_envelope(build_manifest())
    out = {
        "valid-envelope.json": dump(valid),
        "valid-per-engine-envelope.json": dump(build_envelope(build_manifest(
            run_id=fixture_uuid(6), application_mode="per-engine"))),
        "valid-manifest.json": dump(build_manifest(run_id=fixture_uuid(5))),
        "velox-mismatch.json": dump(velox_mismatch_envelope()),
        "partial.json": dump(partial_envelope()),
        "recording-mode.json": dump(build_envelope(build_manifest(run_id=fixture_uuid(4), record_answers=True))),
    }
    compact = json.dumps(valid, separators=(",", ":"))
    out["reject-duplicate-key.json"] = compact.replace('"note":"fixture"', '"note":"fixture","note":"twice"',
                                                       1).encode()
    out["reject-nan.json"] = compact.replace('"elapsed_ms":1037', '"elapsed_ms":NaN', 1).encode()
    neg = copy.deepcopy(valid)
    neg["manifest"]["repeats"][0]["queries"][0]["engines"]["xflare"]["elapsed_ms"] = -5
    out["reject-negative-elapsed.json"] = dump(neg)
    enum = copy.deepcopy(valid)
    enum["manifest"]["run"]["codec"] = "lz4"
    out["reject-unknown-enum.json"] = dump(enum)
    return out


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    for name, data in render_all().items():
        with open(os.path.join(here, name), "wb") as f:
            f.write(data)
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
