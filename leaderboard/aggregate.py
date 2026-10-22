"""content_id, eligibility, medians, ordering and hero selection (README §3-§4). Pure: no I/O.

Inputs are validated manifests/envelopes (schema.py), including stored rows accepted by older validators.
The A4 mode and per-engine applications are checked again (schema.application_mode_state) because older
validators kept them uninterpreted; a shared run's applications stay uninterpreted. derive() never raises for them:
integers are unbounded, so arithmetic is exact where Python can be (odd-N medians, integer sums), a speedup that is
not representable as a float is null, and an even-N median or a total beyond float range makes that query/the total
ineligible ("value beyond numeric range").
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from fractions import Fraction

from . import repro
from .schema import _snip, application_mode_state

DERIVE_VERSION = 10  # A7: velox optional, engines/codec config keys, Spark settings reason
QUERY_IDS = range(1, 23)
METRICS = ("total",) + tuple(f"q{q}" for q in QUERY_IDS)
DEFAULT_FILTER = {"executors": 4, "executor_cores": 6}
CONFIG_KEYS = ("executors", "executor_cores", "offheap", "shuffle_partitions", "devices", "toml", "bundle",
               "application_mode", "spark_conf", "engines", "codec")
# A7: the slice of a recorded Spark settings map that must not change between applications of one engine.
_GATED_SETTING_PREFIXES = ("spark.sql.", "spark.gluten.")
SPARK_SETTINGS_DIFFER = "Spark settings differ across applications"
OUT_OF_RANGE = "value beyond numeric range"
SHARED_REASON = "shared application: engine order affects timings"
ROLLUP_PREFIX = "queries not eligible: "
SHARED_BADGE = "shared application"
# Evidence statuses the offload badge reads as offloaded. partial and offloaded_except_skipped left some operators or
# tasks on velox, but the badge tells runs apart by executor core (README §4).
XFLARE_CORE_STATUSES = frozenset({"offloaded", "offloaded_except_skipped", "partial"})


def canonical_manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_id(manifest: dict) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def _ratio(num, den):
    """num/den as a float, exact up to the final rounding; None for a zero denominator or a non-float result."""
    if not den:
        return None
    try:
        return float(Fraction(num) / Fraction(den))
    except OverflowError:
        return None


def _median(values: list):
    """statistics.median without the float detour: odd N → the middle int; even N → exact int when the middle
    pair sums to an even number, else x.5 as a float (OverflowError when that is beyond float range)."""
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    pair = s[n // 2 - 1] + s[n // 2]
    return pair // 2 if pair % 2 == 0 else pair / 2


def _exact_sum(values: list):
    """Exact sum of ints and x.5 floats; an int when integral, else a float (OverflowError beyond float range)."""
    total = sum(Fraction(v) for v in values)
    if total.denominator == 1:
        float(total.numerator)  # the board, JSON consumers and JS cannot use a total beyond float range
        return total.numerator
    return float(total)


def _engine_problems(res, engine: str, idx: int) -> list[str]:
    idx = _snip(idx)
    if res is None:
        return [f"{engine} missing in repeat {idx}"]
    if res["status"] != "ok":
        return [f"{engine} {res['status']} in repeat {idx}"]
    out = []
    if not res["elapsed_ms"]:  # null or 0; negatives never pass the validator
        out.append(f"{engine} elapsed_ms not positive in repeat {idx}")
    verify = res["verify"] or {}
    if verify.get("against") != "answers":
        out.append(f"{engine} not verified against answers in repeat {idx}")
    elif verify.get("status") != "ok":
        out.append(f"{engine} verify {verify.get('status')} in repeat {idx}")
    return out


def _unfinished_problems(entry: dict, repeat: dict, engine: str) -> list[str]:
    """A failed or aborted repeat still recorded a wrong answer for this query: an engine status other than
    ok/skipped, or a verify status other than ok/null."""
    res = entry["engines"].get(engine)
    if res is None:
        return []
    out = []
    where = f"{repeat['status']} repeat {_snip(repeat['index'])}"
    if res["status"] not in ("ok", "skipped"):
        out.append(f"{engine} {res['status']} in {where}")
    verify = (res["verify"] or {}).get("status")
    if verify not in ("ok", None):
        out.append(f"{engine} verify {verify} in {where}")
    return out


def _entry(repeat: dict, q: int) -> dict | None:
    return next((x for x in repeat["queries"] if x["id"] == q), None)


def _query_problems(completed: list[dict], unfinished: list[dict], q: int, engine: str) -> list[str]:
    """Why `engine`'s timings of query q cannot be used: a completed repeat without it or with a bad result, or a
    failed/aborted repeat that recorded a wrong answer for it (A2)."""
    reasons = []
    for rep in completed:
        entry = _entry(rep, q)
        if entry is None:
            reasons.append(f"missing in repeat {_snip(rep['index'])}")
        else:
            reasons += _engine_problems(entry["engines"].get(engine), engine, rep["index"])
    for rep in unfinished:
        entry = _entry(rep, q)
        if entry is not None:
            reasons += _unfinished_problems(entry, rep, engine)
    return reasons


def _settings_differ(completed: list[dict], engines: list[str]) -> bool:
    """A7: two applications of one engine recorded different gated Spark settings. An application that recorded none
    is ignored. Only validated applications get here (application_mode_state checks the maps' shape)."""
    for engine in engines:
        seen = set()
        for rep in completed:
            for app in rep["applications"]:
                settings = app.get("spark_conf_effective")
                if app["engine"] == engine and settings is not None:
                    seen.add(json.dumps({k: v for k, v in settings.items() if k.startswith(_GATED_SETTING_PREFIXES)},
                                        sort_keys=True))
        if len(seen) > 1:
            return True
    return False


def _query_eligibility(manifest: dict, completed: list[dict], application_mode: str,
                       application_problems: list[str]) -> tuple[list[str], dict[int, list[str]], dict[int, list[str]]]:
    """(common reasons, xflare problems per query, velox problems per query), before A6's per-engine ranking gate.
    xflare gates: completed repeats must be fully ok, failed/aborted ones must not have recorded a wrong answer for q.
    A7: velox never gates, a velox problem only blanks that query's velox time."""
    run = manifest["run"]
    common = list(application_problems)
    if run["record_answers"]:
        common.append("recording mode (record_answers=true)")
    # Length first: repeat_requested is unbounded, so never materialise a range of it.
    indices = sorted(r["index"] for r in manifest["repeats"])
    if len(indices) != run["repeat_requested"] or any(i != k for k, i in enumerate(indices, 1)):
        common.append("repeat list incomplete")
    if not completed:
        common.append("no completed repeat")
    if len({r["artifact_id"] for r in completed}) > 1:
        common.append("artifact differs across repeats")
    if len({r["bundle"] for r in completed}) > 1:
        common.append("bundle differs across repeats")
    if application_mode == "per-engine" and not application_problems:
        for rep in completed:
            present = {app["engine"] for app in rep["applications"]}
            missing = [engine for engine in run["engines"] if engine not in present]
            if missing:
                common.append(f"applications missing {', '.join(missing)} in completed repeat {_snip(rep['index'])}")
        applications = [app for rep in completed for app in rep["applications"]]
        for key, label in (("artifact_id", "artifact"), ("bundle", "bundle")):
            if len({app[key] for app in applications}) > 1:
                common.append(f"{label} differs across applications of completed repeats")
        if _settings_differ(completed, run["engines"]):
            common.append(SPARK_SETTINGS_DIFFER)

    unfinished = sorted((r for r in manifest["repeats"] if r["status"] != "completed"), key=lambda r: r["index"])
    out = {q: _query_problems(completed, unfinished, q, "xflare") for q in QUERY_IDS}
    velox = {q: _query_problems(completed, unfinished, q, "velox") if "velox" in run["engines"] else []
             for q in QUERY_IDS}
    shared = set.intersection(*(set(why) for why in out.values()))
    common.extend(dict.fromkeys(reason for reason in out[1] if reason in shared))
    if shared:
        out = {q: [reason for reason in why if reason not in shared] for q, why in out.items()}
    return common, out, velox


def _samples(completed: list[dict], q: int, engine: str) -> list:
    out = []
    for rep in completed:
        entry = next((x for x in rep["queries"] if x["id"] == q), None)
        res = entry["engines"].get(engine) if entry else None
        out.append(res["elapsed_ms"] if res else None)
    return out


def _reproducible(manifest: dict) -> bool:
    b = manifest["build"]
    if b["provenance_source"] != "artifact-manifest" or b["dirty"] is not False or manifest["client"]["dirty"]:
        return False
    if b["bundle"]["kind"] != "dev":
        return True
    g = b["gluten"] or {}
    return bool(g.get("commit")) and g.get("dirty") is False


def _dash(v) -> str:
    return "-" if v is None else str(v)


def derive(manifest: dict, envelope: dict | None, *, manifest_content_id: str | None = None) -> dict:
    run = manifest["run"]
    repeats = sorted(manifest["repeats"], key=lambda r: r["index"])
    completed = [r for r in repeats if r["status"] == "completed"]
    application_mode, application_problems = application_mode_state(manifest)
    # A7: one engine has no engine order for A6 to protect, so it needs no application per engine to rank.
    mode = "per-engine" if application_mode == "shared" and len(run["engines"]) == 1 else application_mode
    if "spark_conf" in run:
        settings = run["spark_conf"]
        spark_conf_source = "field"
        # Pre-A5 validators accepted this as an uninterpreted field. Never let
        # malformed historical settings break lazy re-derivation or gate ranking.
        spark_conf = ("custom" if settings else "default") if (
            isinstance(settings, dict) and all(isinstance(v, str) for v in settings.values())) else "unknown"
    else:
        spark_conf_source = "argv"
        spark_conf = "custom" if repro.has_option(run["argv"], "--spark-conf") else "default"
    common, q_reasons, velox_problems = _query_eligibility(manifest, completed, application_mode, application_problems)

    # Shared timings stay visible on stored runs; only per-engine measurements may rank (A6).
    rankable_mode = mode == "per-engine"
    per_query = {}
    measured_queries = []
    for q in QUERY_IDS:
        xs, vs = _samples(completed, q, "xflare"), _samples(completed, q, "velox")
        x = v = None
        if not common and not q_reasons[q]:
            try:
                x = _median(xs)
            except OverflowError:
                q_reasons[q].append(OUT_OF_RANGE)
        ok = not common and not q_reasons[q]
        if ok:
            measured_queries.append(q)
            if "velox" in run["engines"] and not velox_problems[q]:
                try:
                    v = _median(vs)
                except OverflowError:
                    pass
        per_query[str(q)] = {"eligible": ok and rankable_mode, "xflare_ms": x, "velox_ms": v,
                             "speedup": _ratio(v, x) if v is not None else None, "velox_problems": velox_problems[q],
                             "n": len(completed), "xflare_samples": xs, "velox_samples": vs}
    eligible = [q for q in QUERY_IDS if per_query[str(q)]["eligible"]]

    total_reasons = []
    if len(eligible) != len(QUERY_IDS):
        total_reasons.append(ROLLUP_PREFIX + ", ".join(str(q) for q in QUERY_IDS if q not in eligible))
    total_reasons += [f"repeat {_snip(r['index'])} {r['status']}" for r in repeats if r["status"] != "completed"]
    if run["exit_status"] != "ok":
        total_reasons.append(f"exit_status {run['exit_status']}")
    totals = {"xflare_ms": None, "velox_ms": None, "speedup": None}
    if (len(measured_queries) == len(QUERY_IDS) and len(completed) == len(repeats)
            and run["exit_status"] == "ok"):
        try:
            tx = _exact_sum([per_query[str(q)]["xflare_ms"] for q in QUERY_IDS])
            totals = {"xflare_ms": tx, "velox_ms": None, "speedup": None}
        except OverflowError:
            total_reasons.append(OUT_OF_RANGE)
        else:
            velox = [per_query[str(q)]["velox_ms"] for q in QUERY_IDS]
            if None not in velox:
                try:
                    tv = _exact_sum(velox)
                    totals.update(velox_ms=tv, speedup=_ratio(tv, tx))
                except OverflowError:
                    pass
    total_eligible = not total_reasons
    if mode == "shared":
        common.insert(0, SHARED_REASON)

    res = run["resources"]
    bundle = manifest["build"]["bundle"]
    config = {"executors": res["executors"], "executor_cores": res["executor_cores"], "offheap": res["offheap"],
              "shuffle_partitions": res["shuffle_partitions"], "devices": res["devices"],
              "toml": run["xflare_toml"]["name"], "bundle": bundle["resolved"],
              "application_mode": mode, "spark_conf": spark_conf, "engines": ",".join(sorted(run["engines"])),
              "codec": run["codec"]}

    env = (envelope or {}).get("environment") or {}
    badges = [f"x{len(completed)}"]
    if mode == "shared":
        badges.append(SHARED_BADGE)
    if _reproducible(manifest):
        badges.append("reproducible")
    if bundle["kind"] == "dev":
        badges.append("dev bundle")
    overlap = env.get("booking_overlap", "unknown")
    if overlap == "present":
        badges.append("booking overlap")
    elif overlap == "unknown":
        badges.append("overlap unknown")
    if not total_eligible and eligible:
        badges.append("partial")
    offload = sorted({"offloaded" if status in XFLARE_CORE_STATUSES else status
                      for status in (x["evidence"]["status"] for r in completed for x in r["queries"])
                      if status is not None})
    if offload:
        badges.append("offload:" + (offload[0] if len(offload) == 1 else "mixed"))
    if spark_conf == "custom":
        badges.append("spark conf")

    reasons = {f"q{q}": q_reasons[q] for q in QUERY_IDS if q_reasons[q]}
    if common:
        reasons["common"] = common
    if total_reasons:
        reasons["total"] = total_reasons
    return {
        "derive_version": DERIVE_VERSION,
        "run_id": manifest["run_id"],
        "content_id": content_id(manifest) if manifest_content_id is None else manifest_content_id,
        "sf": run["scale_factor"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "repeats_requested": run["repeat_requested"],
        "repeats_completed": len(completed),
        "total_eligible": total_eligible,
        "eligible_queries": eligible,
        "per_query": per_query,
        "totals": totals,
        "config": config,
        "spark_conf_source": spark_conf_source,
        "config_label": (f"{config['executors']}×{config['executor_cores']} · {_dash(config['offheap'])}"
                         f" · sp={_dash(config['shuffle_partitions'])} · dev={_dash(config['devices'])}"
                         f" · {config['toml']} · {_dash(config['bundle'])} · {config['application_mode']}"
                         f" · {config['engines']} · {config['codec']}" + (" +conf" if spark_conf == "custom" else "")),
        "build": {"revision": manifest["build"]["revision"], "artifact_id": manifest["build"]["artifact_id"],
                  "bundle_kind": bundle["kind"], "provenance_source": manifest["build"]["provenance_source"],
                  "client_revision": manifest["client"]["revision"]},
        "submitter": ((envelope or {}).get("submitter") or {}).get("email"),
        "device_ids": [d["id"] for d in env.get("devices") or []],
        "host": env.get("host"),
        "badges": badges,
        "ineligible_reasons": reasons,
        "repro": repro.script(manifest),
    }


def tab_of(derived: dict) -> tuple[str, str]:
    """The board tab a run belongs to: (scale factor, codec)."""
    return derived["sf"], derived["config"]["codec"]


def metric_values(derived: dict, metric: str) -> dict:
    """{"xflare_ms", "velox_ms", "speedup"} of `metric` ("total" | "q1".."q22").

    Shared runs retain valid measurements for display, although they never rank.
    """
    src = derived["totals"] if metric == "total" else derived["per_query"][metric[1:]]
    return {k: src[k] for k in ("xflare_ms", "velox_ms", "speedup")}


def _eligible(derived: dict, metric: str) -> bool:
    if metric == "total":
        return derived["total_eligible"]
    return derived["per_query"][metric[1:]]["eligible"]


def utc_key(ts: str) -> tuple[datetime, int]:
    """RFC 3339 UTC time (...Z, validated) → (aware datetime to the second, fraction in nanoseconds). Exact for every
    accepted fraction (1-9 digits), so equal keys mean equal instants."""
    base, _, frac = ts[:-1].partition(".")
    return datetime.strptime(base + "+0000", "%Y-%m-%dT%H:%M:%S%z"), int(frac.ljust(9, "0"))


def order_key(derived: dict, metric: str) -> tuple:
    """metric "total" | "q1".."q22": value asc, repeat count desc, finished_at asc, content_id asc."""
    value = metric_values(derived, metric)["xflare_ms"]
    return (value is None, value or 0, -derived["repeats_completed"], utc_key(derived["finished_at"]),
            derived["content_id"])


def rank(rows: list[dict], metric: str) -> list[dict]:
    """Rows (each with "derived"; "withdrawn" optional) eligible for `metric`, non-withdrawn, in board order."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {str(metric)[:20]!r}")
    keep = [r for r in rows if not r.get("withdrawn") and _eligible(r["derived"], metric)]
    return sorted(keep, key=lambda r: order_key(r["derived"], metric))


def matches_filter(derived: dict, flt: dict) -> bool:
    """Keys: CONFIG_KEYS (compared as text, null spelled "-"), submitter, device (one of device_ids), badge (str or
    list: all present), date_from/date_to (finished_at's UTC date, YYYY-MM-DD, inclusive). A None value means no
    filter on that key; unknown keys raise ValueError."""
    for k, want in flt.items():
        if want is None:
            continue
        if k in CONFIG_KEYS:
            ok = _dash(derived["config"][k]) == _dash(want)
        elif k == "submitter":
            ok = derived["submitter"] == want
        elif k == "device":
            ok = want in derived["device_ids"]
        elif k == "badge":
            ok = all(b in derived["badges"] for b in ([want] if isinstance(want, str) else want))
        elif k == "date_from":
            ok = derived["finished_at"][:10] >= want
        elif k == "date_to":
            ok = derived["finished_at"][:10] <= want
        else:
            raise ValueError(f"unknown filter key {str(k)[:20]!r}")
        if not ok:
            return False
    return True


def hero(rows: list[dict], flt: dict) -> dict | None:
    """First total-eligible, non-withdrawn row under `flt`. Pass one SF tab's rows."""
    ranked = rank([r for r in rows if matches_filter(r["derived"], flt)], "total")
    return ranked[0] if ranked else None


def hero_or_fallback(rows: list[dict], flt: dict) -> tuple[dict | None, bool]:
    """(hero under `flt`, False); when none matches, (fastest total-eligible row of any configuration, True); (None,
    False) when no row qualifies at all. Pass one SF tab's rows, so the fallback never crosses tabs."""
    found = hero(rows, flt)
    if found is not None:
        return found, False
    found = hero(rows, {})
    return found, found is not None
