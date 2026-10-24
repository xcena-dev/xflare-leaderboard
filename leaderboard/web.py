"""HTTP side of the xflare TPC-H leaderboard (README §3).

Every route here reaches the leaderboard storage only through ``Store``, which
raises StorageUnavailable for every storage failure; that turns into a fixed
503 answer, so a missing or read-only data dir never surfaces as a 500 or
leaks a path.

Everything a submitter sends is hostile text: pages render it through Jinja
autoescape or ``textContent`` only, and error bodies here carry fixed text.
What a page or summary shows is bounded by the budgets in ``display``.
"""

from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import math
import os
import re
from fractions import Fraction

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, url_for

from . import aggregate, repro
from .display import (BADGE_CLASS, BENCH_ID, BUDGET, DEFAULT_CODEC, EFFECTIVE_ITEMS, FACET_VALUES, HISTORY_EVENTS,
                      HISTORY_HEROES, HISTORY_POINTS, MATRIX_REPEATS, PREVIEW_CHARS, PREVIEW_ITEMS, SCALAR_CHARS,
                      as_float, badge_class, basis_view, cut, default_filter, facet_option, facet_token,
                      fallback_notice, norm_date, pr_label, tab_label, tab_note, tabs)
from .schema import (APPLICATION_SETTINGS, CODECS, DEFAULT_REPO, MANIFEST_SCHEMA, MAX_BODY_BYTES,
                     HEX64_RE, MAX_REPORT_URL_CHARS, PR_NUMBER_MAX, SCALE_FACTOR_RE, UUID4_RE, BadJSON, normalize_basis, parse_json_strict, parse_prset_key,
                     parse_repo, parse_report_url, pr_key, prset_key, validate_basis, validate_envelope,
                     validate_manifest)
from .store import StorageUnavailable, store_for

TOP_CHOICES = (5, 10, 25)
# A9: one Basis filter, `basis` = main | pr (every PR-based run) | prset:<canonical key> (exactly that PR set) | "-"
# (no basis), and `pr=<owner/name#N>` (a run whose PR set contains that PR; the board's PR chips set it, a literal with
# no facet). Both read the run's basis, which lives beside the run (Store "attribution"), not in its derived data.
_BASIS_PARAMS = ("basis", "pr")
# query parameter -> aggregate.matches_filter key (the A9 ones are matched here, by _values_of)
_FILTER_PARAMS = {**{k: k for k in aggregate.CONFIG_KEYS},
                  "device": "device", "submitter": "submitter", "badge": "badge",
                  "from": "date_from", "to": "date_to", **{k: k for k in _BASIS_PARAMS}}
# Facet filters; each also takes a token in `<key>_t` (display.facet_token).
_FACET_PARAMS = aggregate.CONFIG_KEYS + ("device", "submitter", "badge", "basis")
# difflib is quadratic on adversarial text and the diff runs on every view.
_DIFF_MAX_LINES = 1000

bp = Blueprint("leaderboard", __name__)


class _TooLarge(Exception):
    pass


class _BadBody(Exception):
    pass


class _UnknownRun(Exception):
    pass


def init_app(app, cfg: dict):
    """Register the blueprint with the leaderboard's settings (config.yaml's ``leaderboard`` block)."""
    app.extensions["leaderboard_cfg"] = cfg
    # Hashed once, at start: a missing asset fails here, not in every later answer.
    app.extensions["leaderboard_assets"] = {name: _digest(os.path.join(app.static_folder, name)) for name in _ASSETS}
    app.register_blueprint(bp)


# The display budgets and the badge colours, for the leaderboard templates and
# (through the board page's data block) its script.
bp.record_once(lambda state: state.app.add_template_global(BUDGET, "lb_budget"))
bp.record_once(lambda state: state.app.add_template_global(BADGE_CLASS, "lb_badge_classes"))
bp.record_once(lambda state: state.app.add_template_global(DEFAULT_REPO, "lb_default_repo"))


_ASSETS = ("style.css", "leaderboard.css", "leaderboard.js")
_BOARD_SCRIPT = "leaderboard.js"
_SCRIPT_HEADER = "X-Leaderboard-Script"


def _digest(path: str) -> str:
    """The first 12 hex digits of the file's sha256."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def _asset_version(name: str) -> str:
    return current_app.extensions["leaderboard_assets"][name]


def _static_url(name: str) -> str:
    return url_for("static", filename=name, v=_asset_version(name))


bp.record_once(lambda state: state.app.add_template_global(_static_url, "lb_static"))
bp.record_once(lambda state: state.app.add_template_global(lambda: _asset_version(_BOARD_SCRIPT),
                                                           "lb_script_version"))


def _cfg() -> dict:
    return current_app.extensions["leaderboard_cfg"]


@bp.app_template_test("lb_rollup")
def _is_rollup(reason) -> bool:
    """The Total reason that only restates per-query ineligibility (aggregate's "queries not eligible: …")."""
    return isinstance(reason, str) and reason.startswith(aggregate.ROLLUP_PREFIX)


@bp.app_template_filter("lb_ms")
def _fmt_ms(x) -> str:
    f = as_float(x)
    if f is None:
        return "—" if x is None else "out of range"
    return f"{f / 1000:.2f} s" if f >= 1000 else f"{f:.0f} ms"


@bp.app_template_filter("lb_x")
def _fmt_x(x) -> str:
    f = as_float(x)
    return "—" if f is None else f"{f:.2f}×"


@bp.app_template_filter("lb_pct")
def _fmt_pct(x) -> str:
    if x is None:
        return ""
    return f"{x:+.1f}%" if math.isfinite(x) else "out of range"


@bp.app_template_filter("lb_cut")
def _cut_filter(x):
    return cut(x)


@bp.app_template_filter("lb_badge_class")
def _badge_class_filter(x):
    return badge_class(x)


@bp.app_template_filter("lb_text")
def _text_filter(x):
    return cut(x, PREVIEW_CHARS)


def _errors(status: int, *messages: str, **extra):
    return jsonify({"errors": list(messages), **extra}), status


@bp.errorhandler(StorageUnavailable)
def _storage_down(_e):
    # Fixed text: the exception names the data dir, and the page shows it.
    if request.path.startswith("/api/"):
        return _errors(503, "leaderboard storage unavailable")
    return Response("leaderboard storage unavailable\n", status=503, mimetype="text/plain")


@bp.errorhandler(_TooLarge)
def _too_large(_e):
    return _errors(413, f"request body exceeds {MAX_BODY_BYTES} bytes")


@bp.errorhandler(_BadBody)
def _bad_body(e):
    return _errors(400, str(e))


@bp.errorhandler(_UnknownRun)
def _unknown_run(_e):
    return _errors(404, "unknown run_id")


def _read_body() -> bytes:
    """The body, at most MAX_BODY_BYTES. Content-Length is checked first; the
    bounded read covers chunked bodies, which carry no length to check."""
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise _TooLarge
    chunks, size = [], 0
    while True:
        chunk = request.stream.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _TooLarge
        chunks.append(chunk)


def _json_object() -> dict:
    try:
        doc = parse_json_strict(_read_body())
    except BadJSON as e:
        raise _BadBody(str(e)) from None
    if not isinstance(doc, dict):
        raise _BadBody("body must be a JSON object")
    return doc


def _capped(items: list, limit: int = 50) -> list:
    """A response list cut to ``limit`` entries plus one "... N more" line."""
    return items if len(items) <= limit else [*items[:limit], f"... {len(items) - limit} more"]


def _api_run_id(run_id: str) -> None:
    # README §1: one uuid4 per batch. Anything else never reached the DB, so
    # it is refused before a lookup -- that also keeps '..' and friends out.
    if not UUID4_RE.fullmatch(run_id):
        raise _UnknownRun


def _url(run_id: str) -> str:
    return f"/b/{BENCH_ID}/run/{run_id}"


def _receipt(row: dict) -> dict:
    """The receipt as first issued (README §3): a retry gets the same answer
    even after a withdrawal or a metadata update."""
    return {"run_id": row["run_id"], "content_id": row["content_id"], "url": _url(row["run_id"]),
            **row["receipt"]}


# --- write API ------------------------------------------------------------------

def _identical_stored(manifest, cfg: dict) -> dict | None:
    """The stored row whose run_id and content_id this manifest matches, if any."""
    run_id = manifest.get("run_id") if isinstance(manifest, dict) else None
    if not isinstance(run_id, str) or not UUID4_RE.fullmatch(run_id):
        return None
    row = store_for(cfg).get(run_id)
    return row if row is not None and row["content_id"] == aggregate.content_id(manifest) else None


@bp.post("/api/leaderboard/submissions")
def api_submit():
    cfg = _cfg()
    dry_run = request.args.get("dry_run") in ("1", "true")
    doc = _json_object()
    if dry_run and doc.get("schema") == MANIFEST_SCHEMA:
        manifest, envelope = doc, None
        errors, warnings = validate_manifest(doc)
    else:
        manifest, envelope = doc.get("manifest"), doc
        errors, warnings = validate_envelope(doc)
    if errors:
        # A run stored before a later amendment tightened validation keeps its §3 identity: an identical retry still
        # gets the original receipt, and nothing is stored or updated.
        stored = None if dry_run else _identical_stored(manifest, cfg)
        if stored is not None:
            return jsonify(_receipt(stored)), 200
        return _errors(400, *errors, warnings=warnings)
    if dry_run:
        # Nothing below may touch the store: a dry run must not even create
        # the DB file.
        derived = aggregate.derive(manifest, envelope)
        return jsonify({
            "dry_run": True,
            "run_id": manifest["run_id"],
            "content_id": derived["content_id"],
            "url": _url(manifest["run_id"]),
            "ranked_total": bool(derived["total_eligible"]),
            "tab_note": tab_note(cfg, *aggregate.tab_of(derived)),
            "warnings": warnings,
            "badges": derived["badges"],
            "ineligible_reasons": {k: _capped(v) for k, v in derived["ineligible_reasons"].items()},
            # A9: the basis as it would be stored, so a client can tell this service takes one.
            "basis": basis_view(normalize_basis(envelope["basis"])) if envelope and "basis" in envelope else None,
        })
    outcome, row = store_for(cfg).put(envelope, warnings)
    if outcome == "conflict":
        return _errors(409, "run_id already stored with different manifest content",
                       run_id=row["run_id"], content_id=row["content_id"])
    return jsonify(_receipt(row)), (201 if outcome == "created" else 200)


@bp.post("/api/leaderboard/runs/<run_id>/withdraw")
def api_withdraw(run_id):
    _api_run_id(run_id)
    doc = _json_object()
    actor, reason = doc.get("actor"), doc.get("reason")
    errors = [f"{k}: non-empty string" for k, v in (("actor", actor), ("reason", reason))
              if not isinstance(v, str) or not v.strip()]
    if errors:
        return _errors(400, *errors)
    row = store_for(_cfg()).withdraw(run_id, actor.strip(), reason.strip())
    if row is None:
        raise _UnknownRun
    return jsonify(_run_summary(row))


# --- read API -------------------------------------------------------------------

def _run_summary(row: dict) -> dict:
    """A run as boards and lists show it: uploaded fields cut for display
    (``cut``), device ids capped with their full count."""
    d = row["derived"]
    devices = d["device_ids"] or []
    return {
        "run_id": row["run_id"],
        "content_id": row["content_id"],
        "url": _url(row["run_id"]),
        "sf": cut(d["sf"]),
        "finished_at": d["finished_at"],
        "submitter": cut(d["submitter"]),
        "total_eligible": d["total_eligible"],
        "repeats": d["repeats_completed"],
        "totals": d["totals"],
        "config": {k: cut(v) for k, v in d["config"].items()},
        "config_label": cut(d["config_label"]),
        "build": {k: cut(v) for k, v in d["build"].items()},
        "device_ids": [cut(x) for x in devices[:PREVIEW_ITEMS]],
        "device_count": len(devices),
        "host": cut(d["host"]),
        "badges": [cut(b) for b in d["badges"]],
        # Why no query of the run can rank (A6: a shared run's reason), cut and capped like the dry run's lists;
        # the per-query and Total reasons are in the run detail API's "derived".
        "ineligible_reasons": {"common": _capped([cut(x) for x in d["ineligible_reasons"].get("common", [])])},
        "withdrawn": bool(row["withdrawn"]),
        "withdrawal": _withdrawal(row),
        # A9: bounded by the validator (MAX_PRS members, MAX_REPORT_URL_CHARS), so never cut: a cut URL would lead elsewhere.
        "basis": basis_view(row["attribution"]["basis"]),
        "report_url": row["attribution"]["report_url"],
        # The Report of the run's PR set (A9), shown where the run has none of its own.
        "prset_report_url": row["attribution"]["prset_report_url"],
    }


def _withdrawal(row: dict, limit: int | None = SCALAR_CHARS) -> dict | None:
    """Who withdrew the run and why; cut for summaries, whole (limit None)
    for the run detail API."""
    if not row["withdrawn"]:
        return None
    show = (lambda x: x) if limit is None else (lambda x: cut(x, limit))
    return {"at": row["withdrawn_at"], "actor": show(row["withdrawn_by"]), "reason": show(row["withdrawn_reason"])}


def _sf_param() -> str | None:
    """The ``sf`` filter of the run list: any decimal scale factor (one with no runs lists nothing)."""
    sf = request.args.get("sf")
    if sf and not SCALE_FACTOR_RE.fullmatch(sf):
        raise _BadBody("sf: a decimal scale factor")
    return sf or None


def _select_tab(tab_list: list[dict]) -> dict:
    """The tab the board request names: ``sf`` (default the first tab) and ``codec`` (default snappy)."""
    sf = request.args.get("sf")
    codec = request.args.get("codec") or (DEFAULT_CODEC if sf else tab_list[0]["codec"])
    sf = sf or tab_list[0]["sf"]
    for tab in tab_list:
        if (tab["sf"], tab["codec"]) == (sf, codec):
            return tab
    raise _BadBody("sf, codec: one of " + ", ".join(_capped([t["label"] for t in tab_list], PREVIEW_ITEMS)))


def _filter(defaults: dict) -> dict:
    """Active filter as {query parameter: value}. A parameter that is absent
    takes ``defaults``; one sent empty means "any", which is how the board
    lifts its executors x executor_cores default. A date that is not
    YYYY-MM-DD narrows nothing."""
    flt = {}
    for k in _FILTER_PARAMS:
        v = request.args.get(k)
        if v is None and k in defaults:
            v = str(defaults[k])
        if k in ("from", "to"):
            v = norm_date(v)
        elif k in _BASIS_PARAMS and v:
            v = _canonical_basis_literal(k, v)
        if v:
            flt[k] = v
    return flt


def _canonical_basis_literal(key: str, value: str) -> str:
    """A9: a `pr` key or a `basis=prset:<key>` in any case or member order names the canonical key (GitHub matches
    names in any case); anything else stays as sent and is compared exactly."""
    if key == "pr":
        members = parse_prset_key(value) if "+" not in value else None
        return prset_key(members) if members else value
    if value.startswith(PRSET_PREFIX):
        members = parse_prset_key(value[len(PRSET_PREFIX):])
        return PRSET_PREFIX + prset_key(members) if members else value
    return value


def _tokens() -> dict:
    """Facet tokens the request names, {key: token} from `<key>_t`."""
    return {k: request.args[k + "_t"] for k in _FACET_PARAMS if request.args.get(k + "_t")}


def _values_of(row: dict, key: str) -> set:
    """The full values one run takes for a facet filter. A null config value
    is spelled "-", as matches_filter compares it; so is a run without a basis
    (A9) and, for `pr`, a run without a PR set."""
    d = row["derived"]
    if key in _BASIS_PARAMS:
        basis = row["attribution"]["basis"]
        if basis is None or basis["kind"] == "main" and key == "pr":
            return {"-"}
        if key == "pr":
            return {pr_key(m["repo"], m["number"]) for m in basis["prs"]}
        return {"main"} if basis["kind"] == "main" else {"pr", PRSET_PREFIX + prset_key(basis["prs"])}
    if key in aggregate.CONFIG_KEYS:
        v = d["config"][key]
        return {"-" if v is None else str(v)}
    if key == "device":
        return set(d["device_ids"] or [])
    if key == "submitter":
        return {d["submitter"]} if d["submitter"] else set()
    return set(d["badges"])


PRSET_PREFIX = "prset:"


def _prset_of(row: dict) -> str | None:
    """The canonical key of the run's PR set, or None (no basis, or main)."""
    basis = row["attribution"]["basis"]
    return prset_key(basis["prs"]) if basis and basis["kind"] == "pr" else None


def _facet_values(rows: list[dict]) -> dict[str, set]:
    """Every full value each facet filter takes over ``rows``."""
    return {k: set().union(*(_values_of(r, k) for r in rows)) for k in _FACET_PARAMS}


def _filtered(rows: list[dict], flt: dict, tokens: dict) -> list[dict]:
    """Rows matching the literal filter (exact full values) and every token
    (a run matches when one of its values hashes to it); both apply when a
    request gives a literal and a token for the same key."""
    agg = {_FILTER_PARAMS[k]: v for k, v in flt.items() if k not in _BASIS_PARAMS}
    basis = {k: v for k, v in flt.items() if k in _BASIS_PARAMS}
    return [r for r in rows if aggregate.matches_filter(r["derived"], agg)
            and all(v in _values_of(r, k) for k, v in basis.items())
            and all(any(facet_token(v) == t for v in _values_of(r, k)) for k, t in tokens.items())]


@bp.get("/api/leaderboard/runs")
def api_runs():
    """Every run of a tab (or all tabs), filtered like the board but with no
    default filter: an absent parameter means any."""
    cfg = _cfg()
    rows = store_for(cfg).list_runs(sf=_sf_param(),
                                    include_withdrawn=request.args.get("include_withdrawn") == "1")
    return jsonify([_run_summary(r) for r in _filtered(rows, _filter({}), _tokens())])


@bp.get("/api/leaderboard/runs/<run_id>")
def api_run(run_id):
    _api_run_id(run_id)
    store = store_for(_cfg())
    row = store.get(run_id)
    if row is None:
        raise _UnknownRun
    return jsonify({**_run_summary(row), "withdrawal": _withdrawal(row, None), "derived": row["derived"],
                    "meta": row["meta"], "manifest": store.load_manifest(run_id)})


@bp.get("/api/leaderboard/runs/<run_id>/raw")
def api_raw(run_id):
    _api_run_id(run_id)
    data = store_for(_cfg()).read_raw(run_id)
    if data is None:
        return _errors(404, "raw manifest not found")
    resp = Response(data, mimetype="application/json")
    resp.headers["Content-Disposition"] = f'attachment; filename="{run_id}.json"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _facets(values: dict[str, set]) -> tuple[dict, bool]:
    """The options each filter offers on this tab, at most FACET_VALUES per
    filter, as {token, label} (``display.facet_option``); the flag says some
    were left out."""
    truncated = any(len(v) > FACET_VALUES for v in values.values())
    return {k: [facet_option(x, k) for x in sorted(v, key=lambda s: _facet_order(k, s))[:FACET_VALUES]]
            for k, v in values.items()}, truncated


def _facet_order(key: str, value: str) -> tuple:
    """Numbers in numeric order, the rest by text; the Basis options (A9) read main, every PR set, each set, none."""
    if key == "basis":
        return {"main": 0, "pr": 1, "-": 3}.get(value, 2), value
    return (len(value), value) if value.isdigit() else (1 << 30, value)


def _hero(row: dict | None, target_velox) -> dict | None:
    if row is None:
        return None
    d = row["derived"]
    return {
        **_run_summary(row),
        # Always the totals, whatever the board's metric: the hero is the Total board's.
        "vs_target": _with_vs_target(aggregate.metric_values(d, "total"), target_velox)["vs_target"],
        "per_query": [{"q": q, **aggregate.metric_values(d, m)}
                      for q, m in zip(aggregate.QUERY_IDS, aggregate.METRICS[1:])],
    }


# --- target (amendment A8) -------------------------------------------------------------------------------------
# A tab's target is a stored run whose velox side the board compares the hero and every row with, whatever its
# configuration, plus an optional goal speedup. Anyone may change it (no auth); every change is kept with its actor.

GOAL_MAX = 1000.0


def _target_problem(row: dict | None, sf: str, codec: str) -> str | None:
    """Why a run cannot be the tab's target, or None."""
    if row is None:
        return "unknown run"
    if row["withdrawn"]:
        return "withdrawn"
    d = row["derived"]
    if aggregate.tab_of(d) != (sf, codec):
        return f"on another tab ({tab_label(*aggregate.tab_of(d))})"
    if d["totals"]["velox_ms"] is None:
        return "no velox total"
    return None


def _pin(state: dict | None, sf: str, codec: str) -> dict:
    """The stored target as the API shows it: revision 0 and no run for a tab never set."""
    state = state or {"run_id": None, "goal": None, "revision": 0, "updated_by": None, "updated_at": None}
    return {"sf": sf, "codec": codec, "run_id": state["run_id"], "goal": state["goal"],
            "revision": state["revision"], "updated_by": state["updated_by"] and cut(state["updated_by"]),
            "updated_at": state["updated_at"]}


def _effective_target(store, sf: str, codec: str, pin: dict, rows: dict) -> tuple[dict | None, dict | None]:
    """What the board compares with, and that run's derived data when it can serve: ``?target=<run id>`` for this
    view only (no goal), ``?target=none`` for the same-run velox, else the pinned target. ``rows`` holds the runs the
    board already read (every non-withdrawn one), so only a withdrawn or unknown target costs a lookup. A target that
    cannot serve says why in ``problem``."""
    asked = request.args.get("target")
    if asked == "none":
        return None, None
    if asked:
        if not UUID4_RE.fullmatch(asked):
            raise _BadBody("target: a run id or none")
        run_id, source = asked, "link"
    elif pin["run_id"]:
        run_id, source = pin["run_id"], "pinned"
    else:
        return None, None
    row = rows.get(run_id) or store.get(run_id)
    problem = _target_problem(row, sf, codec)
    out = {"run_id": run_id, "url": _url(run_id), "source": source,
           "goal": pin["goal"] if source == "pinned" else None, "problem": problem}
    if problem is not None:
        return out, None
    d = row["derived"]
    out.update(config_label=cut(d["config_label"]), finished_at=d["finished_at"], velox_ms=d["totals"]["velox_ms"],
               per_query=[d["per_query"][str(q)]["velox_ms"] for q in aggregate.QUERY_IDS])
    return out, d


def _with_vs_target(values: dict, target_velox) -> dict:
    """A run's metric values plus ``vs_target``: the target's velox over the run's xflare, None without a target."""
    return {**values, "vs_target": None if target_velox is None
            else aggregate.speedup(target_velox, values["xflare_ms"])}


def _known_tab(sf: str, codec: str) -> bool:
    return bool(SCALE_FACTOR_RE.fullmatch(sf)) and codec in CODECS


def _same_site_json():
    """Edits come from this site's pages or a script: a JSON body (a cross-site form cannot send one without a CORS
    preflight this service never answers) and, when a browser names its Origin, this one. None when acceptable."""
    if request.mimetype != "application/json":
        return _errors(415, "Content-Type: application/json")
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return _errors(403, "cross-site edit refused")
    return None


@bp.get("/api/leaderboard/targets/<sf>/<codec>")
def api_target(sf, codec):
    if not _known_tab(sf, codec):
        return _errors(404, "unknown tab")
    pin, events = store_for(_cfg()).target_history(sf, codec)
    history = [{"event_id": e["event_id"], "revision": e["revision"], "before_run_id": e["before_run_id"],
                "after_run_id": e["after_run_id"], "before_goal": e["before_goal"], "after_goal": e["after_goal"],
                "actor": cut(e["actor"]), "at": e["at"]} for e in events]
    return jsonify({**_pin(pin, sf, codec), "history": history})


@bp.post("/api/leaderboard/targets/<sf>/<codec>")
def api_set_target(sf, codec):
    refused = _same_site_json()
    if refused:
        return refused
    if not _known_tab(sf, codec):
        return _errors(404, "unknown tab")
    doc = _json_object()
    actor, revision, run_id, goal = (doc.get(k) for k in ("actor", "expected_revision", "run_id", "goal"))
    errors = []
    if not isinstance(actor, str) or not actor.strip():
        errors.append("actor: non-empty string")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        errors.append("expected_revision: integer >= 0")
    if "run_id" not in doc or run_id is not None and not (isinstance(run_id, str) and UUID4_RE.fullmatch(run_id)):
        errors.append("run_id: a run id or null")
    if goal is not None and (isinstance(goal, bool) or not isinstance(goal, (int, float))
                             or not (0 < goal <= GOAL_MAX)):
        errors.append(f"goal: a number above 0 and at most {GOAL_MAX:g}, or null")
    elif goal is not None and run_id is None:
        errors.append("goal: needs a run_id")
    if errors:
        return _errors(400, *errors)
    store = store_for(_cfg())
    if run_id is not None:
        row = store.get(run_id)
        if row is None:
            raise _UnknownRun
        problem = _target_problem(row, sf, codec)
        if problem:
            return _errors(400, f"run_id: {problem}")
    status, state = store.set_target(sf, codec, run_id, None if goal is None else float(goal), actor.strip(),
                                     revision)
    if status == "conflict":
        return _errors(409, "the target changed meanwhile: reload and try again", current=_pin(state, sf, codec))
    if status == "withdrawn":
        return _errors(400, "run_id: withdrawn")
    return jsonify(_pin(state, sf, codec))


# --- basis, reports and checks (amendment A9) ---------------------------------------------------------------------
# A run's basis (a PR set, or main with its latest PR) and report URL, and a PR set's report URL, change only through these
# versioned edits (an identical submission retry never does): each applies at the revision the caller saw (else 409
# with the current state), is kept as an event with its claimed actor, and any earlier value can be set again by
# naming its event (restore). Edits come from this site's pages or a script, like the target's.

ACTOR_CHARS = 256
CHECK_RUN_IDS = 1000
_EVENT_ID_MAX = 2 ** 63 - 1
# ASCII digits only: str.isdigit() also takes "²" (which int() refuses) and "٣" (which it reads as 3).
_DIGITS = re.compile(r"[0-9]{1,19}")


def _event_id(value) -> int | None:
    return value if type(value) is int and 1 <= value <= _EVENT_ID_MAX else None


def _edit_request(doc: dict, field: str, parse) -> tuple[list[str], dict]:
    """The common edit body: {actor, expected_revision} and exactly one of ``field`` (its value, or null to clear)
    and ``restore`` (an event id). ``parse`` turns a value into (stored value, errors)."""
    actor, revision = doc.get("actor"), doc.get("expected_revision")
    errors, out = [], {"value": None, "restore": None}
    if not isinstance(actor, str) or not actor.strip() or len(actor.strip()) > ACTOR_CHARS:
        errors.append(f"actor: non-empty string of at most {ACTOR_CHARS} characters")
    else:
        out["actor"] = actor.strip()
    if type(revision) is not int or revision < 0:
        errors.append("expected_revision: integer >= 0")
    out["revision"] = revision
    if (field in doc) == ("restore" in doc):
        errors.append(f"{field}, restore: exactly one of them")
    elif "restore" in doc:
        out["restore"] = _event_id(doc["restore"])
        if out["restore"] is None:
            errors.append("restore: an event id")
    elif doc[field] is not None:
        out["value"], more = parse(doc[field])
        errors += more
    return errors, out


def _parse_basis(value) -> tuple[dict | None, list[str]]:
    errors = validate_basis(value)
    return (None if errors else normalize_basis(value)), errors


def _parse_report_url(value) -> tuple[str | None, list[str]]:
    url = parse_report_url(value)
    return url, ([] if url else [f"report_url: an http(s) URL of at most {MAX_REPORT_URL_CHARS} characters"])


def _state_view(kind: str, subject: str, state: dict) -> dict:
    """An A9 subject's state as the API shows it (full values: the edit forms start from them)."""
    out = {"run_id" if kind == "run" else "prset": subject, "revision": state["revision"],
           "report_url": state["report_url"],
           "updated_by": state["updated_by"] and cut(state["updated_by"]), "updated_at": state["updated_at"]}
    if kind == "run":
        out["basis"] = basis_view(state["basis"])
    return out


def _event_view(e: dict) -> dict:
    show = basis_view if e["field"] == "basis" else (lambda x: x)
    return {"event_id": e["event_id"], "field": e["field"], "revision": e["revision"], "before": show(e["before"]),
            "after": show(e["after"]), "restored_from": e["restored_from"], "actor": cut(e["actor"]), "at": e["at"]}


def _edit(kind: str, subject: str, field: str, parse):
    """POST handler body shared by the A9 edits."""
    refused = _same_site_json()
    if refused:
        return refused
    errors, req = _edit_request(_json_object(), field, parse)
    if errors:
        return _errors(400, *errors)
    status, state = store_for(_cfg()).edit(kind, subject, field, req["value"], req["actor"], req["revision"],
                                           req["restore"])
    if status == "unknown":
        raise _UnknownRun
    if status == "conflict":
        return _errors(409, "changed meanwhile: reload and try again", current=_state_view(kind, subject, state))
    if status == "bad restore":
        return _errors(400, f"restore: not a change of this {field}")
    return jsonify(_state_view(kind, subject, state))


def _before_param() -> int | None:
    """``?before=<event id>``: the next older page of an edit history."""
    raw = request.args.get("before")
    if not raw:
        return None
    before = _event_id(int(raw)) if _DIGITS.fullmatch(raw) else None
    if before is None:
        raise _BadBody("before: an event id")
    return before


def _history_with_state(kind: str, subject: str) -> tuple[dict, dict]:
    """A subject's stored state and, from the same read, its API view with one page of history."""
    state, events, more = store_for(_cfg()).attribution_history(kind, subject, _before_param(), HISTORY_EVENTS)
    return state, {**_state_view(kind, subject, state), "history": [_event_view(e) for e in events], "more": more}


def _page_history(kind: str, subject: str) -> tuple[dict, dict]:
    """``_history_with_state`` for a page: a bad ``?before=`` is an HTML 400 there."""
    try:
        return _history_with_state(kind, subject)
    except _BadBody as e:
        abort(400, description=str(e))


def _history(kind: str, subject: str) -> dict:
    return _history_with_state(kind, subject)[1]


@bp.post("/api/leaderboard/runs/<run_id>/basis")
def api_set_basis(run_id):
    _api_run_id(run_id)
    return _edit("run", run_id, "basis", _parse_basis)


@bp.post("/api/leaderboard/runs/<run_id>/report_url")
def api_set_run_report(run_id):
    _api_run_id(run_id)
    return _edit("run", run_id, "report_url", _parse_report_url)


@bp.get("/api/leaderboard/runs/<run_id>/history")
def api_run_history(run_id):
    _api_run_id(run_id)
    if store_for(_cfg()).get(run_id) is None:
        raise _UnknownRun
    return jsonify(_history("run", run_id))


def _prset_param() -> str:
    """``?prset=<key>`` as its canonical key; any order or case of the members names the same set."""
    members = parse_prset_key(request.args.get("prset"))
    if members is None:
        raise _BadBody("prset: a PR set key (owner/name#N joined by '+', at most 16)")
    return prset_key(members)


@bp.get("/api/leaderboard/prset")
def api_prset():
    key = _prset_param()
    return jsonify({**_history("prset", key), **_prset_detail(key)})


@bp.post("/api/leaderboard/prset")
def api_set_prset_report():
    refused = _same_site_json()
    return refused or _edit("prset", _prset_param(), "report_url", _parse_report_url)


def _prset_detail(key: str) -> dict:
    """A PR set's members with their latest body checks, and every run that claims exactly this set (withdrawn ones
    flagged), tab by tab in the tabs' order, each tab's ranked runs (total-eligible, not withdrawn) first in board order
    with their place."""
    cfg, store = _cfg(), store_for(_cfg())
    members = parse_prset_key(key)
    rows = [r for r in store.list_runs(include_withdrawn=True) if _prset_of(r) == key]
    checks = store.checks((m["repo"], m["number"]) for m in members)
    view = basis_view({"kind": "pr", "prs": members})
    by_tab: dict[tuple, list[dict]] = {}
    for r in rows:
        by_tab.setdefault(aggregate.tab_of(r["derived"]), []).append(r)
    runs = []
    for tab in tabs(cfg, [r["derived"] for r in rows]):
        here = by_tab.get((tab["sf"], tab["codec"]), [])
        ranked = aggregate.rank(here, "total")
        ranked_ids = {x["run_id"] for x in ranked}
        rest = [r for r in here if r["run_id"] not in ranked_ids]
        for place, r in enumerate(ranked + rest, 1):
            runs.append({**_run_summary(r), "tab": tab, "place": place if place <= len(ranked) else None,
                         "completion": _completion(r, checks)})
    return {"label": view["label"], "prs": [{**p, "check": _check_view(checks.get((p["repo"], p["number"])))}
                                            for p in view["prs"]], "runs": runs}


def _delta_pct(v, b):
    """Exact Δ% of v against b; None without both, +inf past float range (lb_pct prints "out of range")."""
    if v is None or not b:
        return None
    try:
        return float((Fraction(v) - Fraction(b)) * 100 / Fraction(b))
    except OverflowError:
        return math.inf


def _json_pct(pct):
    """A Δ% for a JSON answer: None past float range (JSON has no Infinity; the pages print "—")."""
    return pct if pct is None or math.isfinite(pct) else None


def _board_view(cfg, store) -> dict:
    """What the board and its panels (A9 PR sets, hero history) read from one request: the tab list and the tab it
    names, the filter (default applied) and tokens, the tab's non-withdrawn runs, the stored target and the one in
    effect with its derived data (None when it cannot serve)."""
    all_rows = store.list_runs()
    tab_list = tabs(cfg, [r["derived"] for r in all_rows])
    tab = _select_tab(tab_list)
    flt, tokens = _filter(default_filter(cfg)), _tokens()
    # The tab consumes the codec: it is not a filter of the board.
    for params in (flt, tokens):
        params.pop("codec", None)
    pin = _pin(store.get_target(tab["sf"], tab["codec"]), tab["sf"], tab["codec"])
    target, target_derived = _effective_target(store, tab["sf"], tab["codec"], pin, {r["run_id"]: r for r in all_rows})
    return {"tabs": tab_list, "tab": tab, "filter": flt, "tokens": tokens, "pin": pin, "target": target,
            "target_derived": target_derived,
            "rows": [r for r in all_rows if aggregate.tab_of(r["derived"]) == (tab["sf"], tab["codec"])]}


def _total_velox(target_derived: dict | None):
    return None if target_derived is None else target_derived["totals"]["velox_ms"]


def _best_view(r: dict, target_velox) -> dict:
    d = r["derived"]
    return {"run_id": r["run_id"], "url": _url(r["run_id"]), "finished_at": d["finished_at"],
            "config_label": cut(d["config_label"]), "repeats": d["repeats_completed"],
            **_with_vs_target(aggregate.metric_values(d, "total"), target_velox)}


PRSET_SORTS = ("best", "number", "recent")


@bp.get("/api/leaderboard/prsets")
def api_prsets():
    """A9: the PR sets of the tab's runs under the board's filter, each with its representative: its best
    total-eligible run in board order (aggregate.rank), the same run compare resolves the set to."""
    cfg, store = _cfg(), store_for(_cfg())
    sort = request.args.get("sort") or "best"
    if sort not in PRSET_SORTS:
        return _errors(400, "sort: best, number or recent")
    view = _board_view(cfg, store)
    tab, flt, target_velox = view["tab"], view["filter"], _total_velox(view["target_derived"])
    chosen = _filtered(view["rows"], flt, view["tokens"])
    hero = aggregate.hero(chosen, {})
    groups: dict[str, list[dict]] = {}
    for r in chosen:
        key = _prset_of(r)
        if key is not None:
            groups.setdefault(key, []).append(r)
    reports = store.prset_reports(groups)
    sets = []
    for key, members in groups.items():
        ranked = aggregate.rank(members, "total")
        best = ranked[0] if ranked else None
        last = max(members, key=lambda r: aggregate.utc_key(r["derived"]["finished_at"]))
        sets.append({"prset": key, "basis": basis_view(members[0]["attribution"]["basis"]),
                     "report_url": reports.get(key), "run_count": len(members),
                     "eligible_count": len(ranked), "last_finished_at": last["derived"]["finished_at"],
                     "best": best and _best_view(best, target_velox), "is_hero": best is not None and best is hero,
                     "vs_hero_pct": best and hero and _json_pct(_delta_pct(best["derived"]["totals"]["xflare_ms"],
                                                                           hero["derived"]["totals"]["xflare_ms"])),
                     "_order": (best is None, best and aggregate.order_key(best["derived"], "total")),
                     "_recent": aggregate.utc_key(last["derived"]["finished_at"])})
    if sort == "best":
        sets.sort(key=lambda x: x["prset"])
        sets.sort(key=lambda x: (x["_order"][0], x["_order"][1] or ()))
    elif sort == "number":
        sets.sort(key=lambda x: (-max(p["number"] for p in x["basis"]["prs"]), x["prset"]))
    else:
        sets.sort(key=lambda x: x["prset"])
        sets.sort(key=lambda x: x["_recent"], reverse=True)
    for x in sets:
        del x["_order"], x["_recent"]
    return jsonify({"sf": tab["sf"], "codec": tab["codec"], "sort": sort, "filter": flt,
                    "hero_run_id": hero and hero["run_id"], "prsets": sets[:FACET_VALUES],
                    "truncated": len(sets) > FACET_VALUES})


def _epoch_ms(ts: str) -> int:
    dt, ns = aggregate.utc_key(ts)
    return int(dt.timestamp()) * 1000 + ns // 1_000_000


@bp.get("/api/leaderboard/history")
def api_history():
    """A9: the tab's hero over time for one metric (the Total or one query) under the board's filter (no fallback):
    every run ranked on that board as a point, and the runs that led it when they finished, recomputed from today's
    eligible runs. A from/to window keeps the hero it entered with; the newest points and heroes of the window are
    kept, from one start. vs_target compares with the target's velox for the same metric; the goal applies to the Total only."""
    cfg, store = _cfg(), store_for(_cfg())
    view = _board_view(cfg, store)
    metric = request.args.get("metric") or "total"
    if metric not in aggregate.METRICS:
        return _errors(400, "metric: total or q1..q22")
    tab, flt, target, target_derived = view["tab"], view["filter"], view["target"], view["target_derived"]
    target_velox = None if target_derived is None else aggregate.metric_values(target_derived, metric)["velox_ms"]
    window = {k: flt.pop(k) for k in ("from", "to") if k in flt}
    timeline, heroes = aggregate.best_so_far(_filtered(view["rows"], flt, view["tokens"]), metric)
    day = lambda r: r["derived"]["finished_at"][:10]
    inside = lambda r: window.get("from", "") <= day(r) and ("to" not in window or day(r) <= window["to"])
    points = [r for r in timeline if inside(r)]
    before = [h for h in heroes if "from" in window and day(h) < window["from"]]
    shown = [h for h in heroes if inside(h)]
    hero_ids = {h["run_id"] for h in heroes}
    # Budgets: the window's newest HISTORY_POINTS points and HISTORY_HEROES heroes. A cut moves where the shown series
    # starts, for points and heroes alike, and the hero in force there enters with it (so a cut never leaves points
    # without the best line they were measured against).
    order = lambda r: (aggregate.utc_key(r["derived"]["finished_at"]), r["run_id"])
    starts = ([order(points[-HISTORY_POINTS])] if len(points) > HISTORY_POINTS else []) + (
        [order(shown[-(HISTORY_HEROES - 1)])] if len(shown) + bool(before) > HISTORY_HEROES else [])
    if starts:
        start = max(starts)
        before += [h for h in shown if order(h) < start]
        shown = [h for h in shown if order(h) >= start]
        points = [r for r in points if order(r) >= start]
    entering = before[-1:]
    shown = entering + shown

    def values_of(r):
        return _with_vs_target(aggregate.metric_values(r["derived"], metric), target_velox)

    out_heroes, previous = [], None
    for h in shown:
        values = values_of(h)
        out_heroes.append({"run_id": h["run_id"], "url": _url(h["run_id"]), "finished_at": h["derived"]["finished_at"],
                           "t": _epoch_ms(h["derived"]["finished_at"]), "xflare_ms": values["xflare_ms"],
                           "y": as_float(values["xflare_ms"]), "vs_target": values["vs_target"],
                           "config_label": cut(h["derived"]["config_label"]),
                           "basis": basis_view(h["attribution"]["basis"]),
                           "report_url": h["attribution"]["report_url"],
                           "prset_report_url": h["attribution"]["prset_report_url"],
                           "entering": bool(entering) and h is entering[0],
                           "delta_pct": previous and _json_pct(_delta_pct(
                               values["xflare_ms"], aggregate.metric_values(previous["derived"], metric)["xflare_ms"]))})
        previous = h
    point_ms = lambda r: aggregate.metric_values(r["derived"], metric)["xflare_ms"]
    return jsonify({
        "sf": tab["sf"], "codec": tab["codec"], "metric": metric, "filter": {**flt, **window},
        # What vs_target compares with: the target's velox for this metric; its goal speaks of the Total only.
        "target": target and {"run_id": target["run_id"], "url": target["url"], "source": target["source"],
                              "problem": target["problem"], "velox_ms": target_velox,
                              "goal": target["goal"] if metric == "total" else None},
        "points": [{"run_id": r["run_id"], "url": _url(r["run_id"]), "finished_at": r["derived"]["finished_at"],
                    "t": _epoch_ms(r["derived"]["finished_at"]), "xflare_ms": point_ms(r),
                    "y": as_float(point_ms(r)), "hero": r["run_id"] in hero_ids}
                   for r in points],
        "heroes": out_heroes,
        "truncated": bool(starts),
    })


@bp.post("/api/leaderboard/prs/<owner>/<name>/<number>/check")
def api_pr_check(owner, name, number):
    """A9: the PR workflow's evidence, sent by the skill after it read the PR body: which run ids the body lists."""
    refused = _same_site_json()
    if refused:
        return refused
    repo = parse_repo(f"{owner}/{name}")
    n = int(number) if _DIGITS.fullmatch(number) else 0
    if repo is None or not 1 <= n <= PR_NUMBER_MAX:
        return _errors(404, "unknown PR")
    doc = _json_object()
    actor, run_ids, sha, source = (doc.get(k) for k in ("actor", "run_ids", "body_sha256", "source"))
    errors = []
    if not isinstance(actor, str) or not actor.strip() or len(actor.strip()) > ACTOR_CHARS:
        errors.append(f"actor: non-empty string of at most {ACTOR_CHARS} characters")
    if not (isinstance(run_ids, list) and len(run_ids) <= CHECK_RUN_IDS
            and all(isinstance(x, str) and UUID4_RE.fullmatch(x) for x in run_ids)):
        errors.append(f"run_ids: a list of at most {CHECK_RUN_IDS} run ids")
    if sha is not None and not (isinstance(sha, str) and HEX64_RE.fullmatch(sha)):
        errors.append("body_sha256: 64 lowercase hex or null")
    if source is not None and not (isinstance(source, str) and 0 < len(source) <= 64):
        errors.append("source: a string of 1 to 64 characters or null")
    if errors:
        return _errors(400, *errors)
    check = store_for(_cfg()).record_check(repo, n, sorted(set(run_ids)), sha, source, actor.strip())
    return jsonify(_check_view(check))


def _check_view(check: dict | None) -> dict | None:
    if check is None:
        return None
    return {"repo": check["repo"], "number": check["number"], "listed_count": len(check["run_ids"]),
            "body_sha256": check["body_sha256"], "source": check["source"] and cut(check["source"]),
            "checked_by": cut(check["checked_by"]), "checked_at": check["checked_at"]}


def _completion(row: dict, checks: dict) -> dict | None:
    """A9: the PR workflow of a run with a PR set is complete when every current member's latest body check lists
    the run; a member never checked or not listing it keeps it pending. Status only: it never gates."""
    basis = row["attribution"]["basis"]
    if basis is None or basis["kind"] != "pr":
        return None
    members = []
    for m in basis["prs"]:
        check = checks.get((m["repo"], m["number"]))
        status = "not checked" if check is None else "listed" if row["run_id"] in check["run_ids"] else "not listed"
        members.append({"key": pr_key(m["repo"], m["number"]), "label": pr_label(m["repo"], m["number"]),
                        "status": status, "checked_at": check and check["checked_at"]})
    return {"complete": all(m["status"] == "listed" for m in members), "members": members}


def _member_checks(store, rows: list[dict]) -> dict:
    return store.checks((m["repo"], m["number"]) for r in rows for m in
                        ((r["attribution"]["basis"] or {}).get("prs") or []))


@bp.get("/api/leaderboard/board")
def api_board():
    cfg = _cfg()
    store = store_for(cfg)
    view = _board_view(cfg, store)
    tab, tab_list, flt, tokens, pin = view["tab"], view["tabs"], view["filter"], view["tokens"], view["pin"]
    target, target_derived, rows = view["target"], view["target_derived"], view["rows"]
    metric = request.args.get("metric") or "total"
    if metric not in aggregate.METRICS:
        return _errors(400, "metric: total or q1..q22")
    try:
        top = int(request.args.get("top", 10))
    except ValueError:
        top = 10
    if top not in TOP_CHOICES:
        top = 10
    target_velox = None if target_derived is None else aggregate.metric_values(target_derived, metric)["velox_ms"]
    chosen = _filtered(rows, flt, tokens)
    ranked = aggregate.rank(chosen, metric)
    facets, facets_truncated = _facets(_facet_values(rows))
    # aggregate.hero_or_fallback over a filter that also holds tokens: when
    # nothing chosen is Total-eligible, the hero is the tab's fastest run in
    # any configuration; the rows stay filtered.
    hero, hero_fallback = aggregate.hero(chosen, {}), False
    if hero is None:
        hero = aggregate.hero(rows, {})
        hero_fallback = hero is not None
    # A6: shared-application runs under the filter are stored and listed but never rank; the board names them so
    # an empty board does not hide them. list_runs left out the withdrawn ones.
    shared = [r for r in chosen if r["derived"]["config"]["application_mode"] == "shared"]
    return jsonify({
        "sf": tab["sf"],
        "codec": tab["codec"],
        "tabs": tab_list,
        "metric": metric,
        "top": top,
        "filter": flt,
        # The token of every applied facet filter, literal or token, so the
        # page selects the matching option either way.
        "filter_tokens": {k: tokens.get(k) or facet_token(flt[k]) for k in _FACET_PARAMS if k in tokens or k in flt},
        "default_filter": default_filter(cfg),
        "facets": facets,
        "facets_truncated": facets_truncated,
        "hero": _hero(hero, _total_velox(target_derived)),
        "target": target,
        "target_pin": pin,
        "hero_fallback": hero_fallback,
        "hero_notice": (fallback_notice({**flt, **{k + "_t": t for k, t in tokens.items()}}, default_filter(cfg))
                        if hero_fallback else None),
        "eligible_count": len(ranked),
        "shared_count": len(shared),
        "shared_runs": [{"run_id": r["run_id"], "url": _url(r["run_id"]), "finished_at": r["derived"]["finished_at"],
                         "config_label": cut(r["derived"]["config_label"])} for r in shared[:top]],
        # A row carries its reproduce script so the board can copy it within the click.
        "rows": [{"rank": i + 1, **_run_summary(r),
                  "value": _with_vs_target(aggregate.metric_values(r["derived"], metric), target_velox),
                  "repro": r["derived"]["repro"]["command"]}
                 for i, r in enumerate(ranked[:top])],
    })


@bp.after_request
def _name_script(resp):
    # Every board answer, errors included: a stale page must not act on a 400 or 503 either.
    if request.endpoint in ("leaderboard.api_board", "leaderboard.api_history", "leaderboard.api_prsets"):
        resp.headers[_SCRIPT_HEADER] = _asset_version(_BOARD_SCRIPT)
    return resp


# --- pages ----------------------------------------------------------------------

@bp.get("/")
def home():
    return redirect(url_for("leaderboard.board_page"))


@bp.get(f"/b/{BENCH_ID}")
def board_page():
    # Reads no storage: the page fetches /api/leaderboard/board, which answers 503 when storage is down.
    return render_template("leaderboard.html", benchmark=_cfg())


def _page_run_id(run_id: str) -> None:
    if not UUID4_RE.fullmatch(run_id):
        abort(404)


def _toml_diff(toml: dict) -> list[tuple[str, str]] | str | None:
    """Unified diff lines, None when nothing was rendered, or "too large" when
    either side is past the budget -- difflib is quadratic on adversarial text
    and this runs on every page view."""
    rendered = toml.get("rendered_text")
    if rendered is None:
        return None
    text_in = toml.get("input_text") or ""
    if max(len(text_in), len(rendered)) > PREVIEW_CHARS:
        return "too large"
    a, b = text_in.splitlines(), rendered.splitlines()
    if max(len(a), len(b)) > _DIFF_MAX_LINES:
        return "too large"
    out = []
    for line in difflib.unified_diff(a, b, "input", "rendered", lineterm=""):
        kind = {"+": "add", "-": "del", "@": "hunk"}.get(line[:1], "ctx")
        if line.startswith(("+++", "---")):
            kind = "file"
        out.append((kind, line))
    return out


def _repro(manifest: dict, stored: dict) -> dict:
    """What it takes to run this again: the paste-ready script derive() stored
    with the run (``stored``, the one the board copies), its budgets, plus the
    things it cannot pin."""
    run, build, client = manifest["run"], manifest["build"], manifest["client"]
    bundle = build.get("bundle") or {}
    toml = run["xflare_toml"]
    notes = [f"xflare-analytics checkout at {cut(client['revision'])}"
             + (" (dirty: local changes are not reproducible from the revision alone)" if client.get("dirty") else "")]
    if build.get("artifact_id"):
        notes.append(f"build artifact {cut(build['artifact_id'])} ({cut(build.get('provenance_source'))})")
    if bundle.get("resolved") or bundle.get("requested"):
        notes.append(f"bundle {cut(bundle.get('resolved') or bundle.get('requested'))} (kind {cut(bundle.get('kind'))})")
    gluten = build.get("gluten") or {}
    if gluten.get("commit"):
        notes.append(f"dev bundle built from gluten {cut(gluten['commit'])}"
                     + (f": {cut(gluten['build_command'], PREVIEW_CHARS)}" if gluten.get("build_command") else ""))
    notes.append(f"resource toml {cut(toml.get('name'))} (input sha256 {cut(toml.get('input_sha256'))})"
                 + ("; the rendered copy below is what the run read" if toml.get("rendered_text") is not None else ""))
    if (run.get("dataset") or {}).get("uri"):
        notes.append(f"dataset {cut(run['dataset']['uri'])}")
    return {**stored, "argc": len(run["argv"]), "max_args": repro.MAX_ARGS, "max_chars": repro.MAX_CHARS,
            "notes": notes}


def _settings(run: dict, field: str, limit: int = PREVIEW_ITEMS) -> dict:
    """``run[field]`` (A5 ``spark_conf`` / ``spark_conf_readback``) as the run page lists it: ``recorded`` is False
    when the field is absent (a manifest from before A5), ``items`` holds the first ``limit`` (key, value) pairs
    and ``count`` all of them; ``items`` is None for a stored value that is not an object (accepted before A5
    validated it)."""
    if field not in run:
        return {"recorded": False, "items": [], "count": 0}
    value = run[field]
    if not isinstance(value, dict):
        return {"recorded": True, "items": None, "count": 0}
    return {"recorded": True, "items": list(itertools.islice(value.items(), limit)), "count": len(value)}


def _effective_maps(manifest: dict) -> dict[str, dict]:
    """A7: ``{"<field> (<engine>)": map}``, each engine's recorded ``spark_conf_effective`` / ``xflare_parameters_effective``
    from its first application that has one, completed repeats first. Values that are not objects are skipped."""
    repeats = sorted(manifest["repeats"], key=lambda r: (r["status"] != "completed", r["index"]))
    applications = [app for rep in repeats if isinstance(rep.get("applications"), list)
                    for app in rep["applications"] if isinstance(app, dict)]
    maps = {}
    for field in APPLICATION_SETTINGS:
        for engine in manifest["run"]["engines"]:
            found = next((app[field] for app in applications
                          if app.get("engine") == engine and isinstance(app.get(field), dict)), None)
            if found is not None:
                maps[f"{field} ({engine})"] = found
    return maps


def _map_diff_rows(maps: list[dict[str, dict]]) -> list[dict]:
    """Compare rows, one per setting whose value differs among the runs that recorded that map. A map is one long
    cell otherwise, and the cell cut would hide the key that differs."""
    rows = []
    for label in dict.fromkeys(label for run_maps in maps for label in run_maps):
        recorded = [run_maps[label] for run_maps in maps if label in run_maps]
        if len(recorded) < 2:
            continue
        keys = sorted({key for m in recorded for key in m})
        differing = [k for k in keys if len({_cmp_key(m.get(k, _ABSENT)) for m in recorded}) > 1]
        for key in differing[:PREVIEW_ITEMS]:
            values = [str(run_maps[label].get(key, "—")) if label in run_maps else "—" for run_maps in maps]
            rows.append({"key": f"{label} · {key}", "values": values, "differs": True})
        if len(differing) > PREVIEW_ITEMS:
            rows.append({"key": f"{label} · (more)", "differs": True,
                         "values": [f"{len(differing) - PREVIEW_ITEMS} more — see raw JSON"] * len(maps)})
    return rows


def _repeat_matrix(repeats: list[dict]) -> list[dict]:
    """Query x (repeat, engine) cells for the given (already cut) repeats."""
    by_rep = [{q["id"]: q for q in r.get("queries") or []} for r in repeats]
    rows = []
    for q in aggregate.QUERY_IDS:
        cells = []
        for rq in by_rep:
            entry = rq.get(q)
            for eng in ("xflare", "velox"):
                e = ((entry or {}).get("engines") or {}).get(eng)
                cells.append(None if e is None else {
                    "ms": e.get("elapsed_ms"), "status": e.get("status"),
                    "verify": (e.get("verify") or {}).get("status")})
        rows.append({"q": q, "cells": cells})
    return rows


@bp.get(f"/b/{BENCH_ID}/run/<run_id>")
def run_page(run_id):
    cfg = _cfg()
    _page_run_id(run_id)
    store = store_for(cfg)
    row = store.get(run_id)
    if row is None:
        abort(404)
    manifest = store.load_manifest(run_id)
    d = row["derived"]
    per_query = [{"q": q, **d["per_query"][str(q)]} for q in aggregate.QUERY_IDS]
    repeats = sorted(manifest["repeats"], key=lambda r: r["index"])
    sf, codec = aggregate.tab_of(d)
    pin = _pin(store.get_target(sf, codec), sf, codec)
    # One snapshot for what the page shows, the edit prompts' starting values and the revision they apply at.
    state, history = _page_history("run", run_id)
    shown_set = _prset_of({"attribution": state})
    row = {**row, "attribution": {**state, "prset_report_url": shown_set and (
        store.prset_reports([shown_set]).get(shown_set))}}
    return render_template(
        "leaderboard_run.html",
        benchmark=cfg, row=row, d=d, meta=row["meta"] or {},
        m=manifest, summary=_run_summary(row),
        # A9: basis, report URL, the PR workflow's status and the edit history (with restore) of this run.
        attribution=history, completion=_completion(row, _member_checks(store, [row])),
        edit_data={"run_id": run_id, "revision": state["revision"], "basis": state["basis"],
                   "report_url": state["report_url"], "default_repo": DEFAULT_REPO},
        # A6: the run page's notice for a shared-application run (stored and listed, never ranked).
        shared_reason=aggregate.SHARED_REASON if d["config"]["application_mode"] == "shared" else None,
        per_query=per_query,
        # A8: whether this run is, or can be, its tab's target.
        target={"sf": sf, "codec": codec, "label": tab_label(sf, codec), "revision": pin["revision"],
                "goal": pin["goal"], "is_target": pin["run_id"] == run_id,
                "problem": _target_problem(row, sf, codec)},
        # as_float: a median past float range is a JSON integer the chart would read as Infinity.
        chart={"run_id": run_id, "xflare": [as_float(p["xflare_ms"]) for p in per_query],
               "velox": [as_float(p["velox_ms"]) for p in per_query]},
        repeats=repeats, matrix_repeats=repeats[:MATRIX_REPEATS],
        matrix=_repeat_matrix(repeats[:MATRIX_REPEATS]),
        toml_diff=_toml_diff(manifest["run"]["xflare_toml"]),
        repro=_repro(manifest, d["repro"]),
        spark_conf=_settings(manifest["run"], "spark_conf"),
        spark_conf_readback=_settings(manifest["run"], "spark_conf_readback"),
        effective=[(label, _settings({label: m}, label, EFFECTIVE_ITEMS))
                   for label, m in _effective_maps(manifest).items()],
    )


# A compare cell for a key a run does not have (or an A5 map it did not record): equal only to itself.
_ABSENT = object()


def _cmp_key(full) -> object:
    """What a compare row's "differs" compares: the JSON text of the full value, so JSON types stay distinct
    (``true`` is not ``1``) and map key order does not matter; ``_ABSENT`` stays its own value."""
    return full if full is _ABSENT else json.dumps(full, sort_keys=True, ensure_ascii=False)


def _settings_cell(run: dict, field: str) -> tuple:
    """``run[field]`` (A5 ``spark_conf`` / ``spark_conf_readback``) as one compare cell: (text, full value). The
    text is "not recorded", "{}", "not an object", or the map as sorted ``k=v`` pairs joined by "; " (a structure,
    only in a row stored before A5 validated the map, is never stringified); the template cuts it like any scalar.
    The full value is what the row's "differs" compares."""
    if field not in run:
        return "not recorded", _ABSENT
    value = run[field]
    if not isinstance(value, dict):
        return "not an object", value
    if not value:
        return "{}", value
    return "; ".join(f"{k}={'[…]' if isinstance(x, (dict, list)) else x}" for k, x in sorted(value.items())), value


def _params(manifest: dict, meta: dict, derived: dict, maps: dict[str, dict]) -> dict:
    """Flat key -> (text, full value) view of what a run ran with, for the compare diff; the full value is the
    text except for the Spark settings maps (``_settings_cell``). The application mode and the Spark settings state
    are the interpreted ones from ``derived`` (a stored mode outside the enum shows as "-")."""
    run, build, client = manifest["run"], manifest["build"], manifest["client"]
    res, toml, env = run["resources"], run["xflare_toml"], meta.get("environment") or {}
    bundle, gluten = build.get("bundle") or {}, build.get("gluten") or {}
    devices = env.get("devices") or []
    p = {
        "scale_factor": run["scale_factor"], "codec": run["codec"],
        "engines": ",".join(run["engines"]), "application_mode": derived["config"]["application_mode"],
        "spark_conf (state)": derived["config"]["spark_conf"],
        "spark_conf.source": {"field": "run.spark_conf", "argv": "argv (no run.spark_conf)"}.get(derived["spark_conf_source"]),
        **{f"{field} (settings)": _settings_cell(run, field) for field in ("spark_conf", "spark_conf_readback")},
        **{f"{label} (keys)": f"{len(m)} keys" for label, m in maps.items()},
        "queries": ",".join(str(q) for q in run["queries"]),
        "repeat_requested": run["repeat_requested"], "record_answers": run["record_answers"],
        **{f"resources.{k}": res.get(k) for k in ("executors", "executor_cores", "offheap",
                                                   "shuffle_partitions", "devices")},
        "toml.name": toml.get("name"), "toml.input_sha256": toml.get("input_sha256"),
        "toml.sent_sha256": toml.get("sent_sha256"), "toml.rendered_sha256": toml.get("rendered_sha256"),
        "bundle.resolved": bundle.get("resolved"), "bundle.kind": bundle.get("kind"),
        "build.artifact_id": build.get("artifact_id"), "build.revision": build.get("revision"),
        "build.provenance_source": build.get("provenance_source"), "build.dirty": build.get("dirty"),
        "gluten.commit": gluten.get("commit"),
        "client.revision": client.get("revision"), "client.branch": client.get("branch"),
        "client.dirty": client.get("dirty"),
        "answers.sha256": (run.get("answers") or {}).get("sha256"),
        "dataset.uri": (run.get("dataset") or {}).get("uri"),
        "env.host": env.get("host"),
        "env.devices": ",".join(str(x.get("id")) for x in devices[:PREVIEW_ITEMS])
                       + (f" (+{len(devices) - PREVIEW_ITEMS} more)" if len(devices) > PREVIEW_ITEMS else ""),
        "env.sdk": env.get("sdk"), "env.pxl": env.get("pxl"),
        "isolation_claim": meta.get("isolation_claim"),
    }
    overrides = run.get("xflare_overrides") or []
    for o in overrides[:PREVIEW_ITEMS]:
        p[f"override.{o.get('key')}"] = f"{o.get('value')} ({o.get('source')} → {o.get('destination')})"
    if len(overrides) > PREVIEW_ITEMS:
        p["override.(more)"] = f"{len(overrides) - PREVIEW_ITEMS} more — see raw JSON"
    text = {k: "—" if v is None else str(v) for k, v in p.items() if not isinstance(v, tuple)}
    return {k: v if isinstance(v, tuple) else (text[k], text[k]) for k, v in p.items()}


@bp.get(f"/b/{BENCH_ID}/prset")
def prset_page():
    """A9: one PR set: its members (GitHub links, body checks), its report URL with edit and history, and its runs."""
    members = parse_prset_key(request.args.get("prset"))
    if members is None:
        abort(404)
    key = prset_key(members)
    detail, history = _prset_detail(key), _page_history("prset", key)[1]
    # "Only this set" on the board: on the tab of its first listed run, the default filter lifted (sent empty).
    tab = {"sf": detail["runs"][0]["tab"]["sf"], "codec": detail["runs"][0]["tab"]["codec"]} if detail["runs"] else {}
    board = url_for("leaderboard.board_page",
                    **{**{k: "" for k in default_filter(_cfg())}, **tab, "basis": PRSET_PREFIX + key})
    return render_template("leaderboard_prset.html", benchmark=_cfg(), key=key, detail=detail, attribution=history,
                           board_url=board,
                           edit_data={"prset": key, "revision": history["revision"],
                                      "report_url": history["report_url"]})


def _prset_runs(keys: list[str]) -> list[tuple[dict, str]]:
    """Compare by PR set (A9): each set's representative, its best total-eligible run under the board's tab and
    filter (as the PR-set list shows it), as (row, set label): the page shows these rows, so a column's heading, basis
    and numbers come from the read that chose it. 404 names a set without one."""
    cfg = _cfg()
    try:
        view = _board_view(cfg, store_for(cfg))
    except _BadBody as e:                                   # a page: an HTML 400 (a bad sf, codec or target)
        abort(400, description=str(e))
    chosen = _filtered(view["rows"], view["filter"], view["tokens"])
    out = []
    for key in keys:
        best = aggregate.rank([r for r in chosen if _prset_of(r) == key], "total")
        if not best:
            abort(404, description=f"no total-eligible run of PR set {cut(key)} on SF-{view['tab']['label']} "
                                   "under this filter")
        out.append((best[0], basis_view(best[0]["attribution"]["basis"])["label"]))
    return out


@bp.get(f"/b/{BENCH_ID}/compare")
def compare_page():
    cfg = _cfg()
    labels, chosen_rows = {}, {}
    if request.args.get("prsets") is not None:
        if request.args.get("runs") is not None:
            abort(400, description="compare takes ?runs= or ?prsets=, not both")
        sets = [parse_prset_key(x) for x in request.args["prsets"].split(",") if x]
        keys = list(dict.fromkeys(prset_key(m) for m in sets if m is not None))
        if None in sets or not 2 <= len(keys) <= 4:
            abort(400, description="compare takes 2 to 4 distinct PR set keys: ?prsets=k1,k2[,k3,k4]")
        resolved = _prset_runs(keys)
        ids = [row["run_id"] for row, _ in resolved]
        labels = {row["run_id"]: label for row, label in resolved}
        chosen_rows = {row["run_id"]: row for row, _ in resolved}
    else:
        ids = list(dict.fromkeys(x for x in (request.args.get("runs") or "").split(",") if x))
    if not 2 <= len(ids) <= 4 or not all(UUID4_RE.fullmatch(x) for x in ids):
        abort(400, description="compare takes 2 to 4 distinct run ids: ?runs=a,b[,c,d]")
    store = store_for(cfg)
    runs = []
    for run_id in ids:
        row = chosen_rows.get(run_id) or store.get(run_id)
        if row is None:
            abort(404)
        manifest = store.load_manifest(run_id)
        maps = _effective_maps(manifest)
        runs.append({"row": row, "d": row["derived"], "summary": _run_summary(row), "maps": maps,
                     "set_label": labels.get(run_id),
                     "params": _params(manifest, row["meta"] or {}, row["derived"], maps)})
    base = runs[0]["d"]

    delta = _delta_pct
    queries = []
    for q in aggregate.QUERY_IDS:
        cells = [r["d"]["per_query"][str(q)] for r in runs]
        b = cells[0]["xflare_ms"]
        queries.append({"q": q, "cells": [{**c, "delta_pct": delta(c["xflare_ms"], b)} for c in cells]})
    totals = [{**r["d"]["totals"], "delta_pct": delta(r["d"]["totals"]["xflare_ms"], base["totals"]["xflare_ms"])}
              for r in runs]
    # Bounded: _params gives each run at most PREVIEW_ITEMS override keys.
    keys = list(dict.fromkeys(k for r in runs for k in r["params"]))
    params = []
    for k in keys:
        cells = [r["params"].get(k, ("—", _ABSENT)) for r in runs]
        # Compared in full; the template's v() cuts the texts for display.
        params.append({"key": k, "values": [t for t, _ in cells],
                       "differs": len({_cmp_key(full) for _, full in cells}) > 1})
    params += _map_diff_rows([r["maps"] for r in runs])
    return render_template(
        "leaderboard_compare.html",
        benchmark=cfg, runs=runs, queries=queries, totals=totals,
        params=params,
        chart={"names": [r["row"]["run_id"][:8] for r in runs],
               "series": [[as_float(r["d"]["per_query"][str(q)]["xflare_ms"]) for q in aggregate.QUERY_IDS]
                          for r in runs]},
    )
