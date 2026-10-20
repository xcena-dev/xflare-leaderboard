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
import math
from fractions import Fraction

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, url_for

from . import aggregate, repro
from .display import (BENCH_ID, BUDGET, FACET_VALUES, MATRIX_REPEATS, PREVIEW_CHARS, PREVIEW_ITEMS, SCALAR_CHARS,
                      as_float, cut, default_filter, facet_option, facet_token, fallback_notice, norm_date, tabs)
from .schema import (MANIFEST_SCHEMA, MAX_BODY_BYTES, UUID4_RE, BadJSON, parse_json_strict,
                     validate_envelope, validate_manifest)
from .store import StorageUnavailable, store_for

TOP_CHOICES = (5, 10, 25)
# query parameter -> aggregate.matches_filter key
_FILTER_PARAMS = {**{k: k for k in aggregate.CONFIG_KEYS},
                  "device": "device", "submitter": "submitter", "badge": "badge",
                  "from": "date_from", "to": "date_to"}
# Facet filters; each also takes a token in `<key>_t` (display.facet_token).
_FACET_PARAMS = aggregate.CONFIG_KEYS + ("device", "submitter", "badge")
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
    app.register_blueprint(bp)


# The display budgets, for the leaderboard templates and (through the board
# page's data block) its script.
bp.record_once(lambda state: state.app.add_template_global(BUDGET, "lb_budget"))


def _cfg() -> dict:
    return current_app.extensions["leaderboard_cfg"]


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
            "warnings": warnings,
            "badges": derived["badges"],
            "ineligible_reasons": {k: _capped(v) for k, v in derived["ineligible_reasons"].items()},
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
        "withdrawn": bool(row["withdrawn"]),
        "withdrawal": _withdrawal(row),
    }


def _withdrawal(row: dict, limit: int | None = SCALAR_CHARS) -> dict | None:
    """Who withdrew the run and why; cut for summaries, whole (limit None)
    for the run detail API."""
    if not row["withdrawn"]:
        return None
    show = (lambda x: x) if limit is None else (lambda x: cut(x, limit))
    return {"at": row["withdrawn_at"], "actor": show(row["withdrawn_by"]), "reason": show(row["withdrawn_reason"])}


def _sf(cfg: dict, fallback: str | None):
    sf = request.args.get("sf") or fallback
    if sf is not None and sf not in tabs(cfg):
        raise _BadBody("sf: one of " + ", ".join(tabs(cfg)))
    return sf


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
        if v:
            flt[k] = v
    return flt


def _tokens() -> dict:
    """Facet tokens the request names, {key: token} from `<key>_t`."""
    return {k: request.args[k + "_t"] for k in _FACET_PARAMS if request.args.get(k + "_t")}


def _values_of(d: dict, key: str) -> set:
    """The full values one run takes for a facet filter. A null config value
    is spelled "-", as matches_filter compares it."""
    if key in aggregate.CONFIG_KEYS:
        v = d["config"][key]
        return {"-" if v is None else str(v)}
    if key == "device":
        return set(d["device_ids"] or [])
    if key == "submitter":
        return {d["submitter"]} if d["submitter"] else set()
    return set(d["badges"])


def _facet_values(rows: list[dict]) -> dict[str, set]:
    """Every full value each facet filter takes over ``rows``."""
    return {k: set().union(*(_values_of(r["derived"], k) for r in rows)) for k in _FACET_PARAMS}


def _filtered(rows: list[dict], flt: dict, tokens: dict) -> list[dict]:
    """Rows matching the literal filter (exact full values) and every token
    (a run matches when one of its values hashes to it); both apply when a
    request gives a literal and a token for the same key."""
    agg = {_FILTER_PARAMS[k]: v for k, v in flt.items()}
    return [r for r in rows if aggregate.matches_filter(r["derived"], agg)
            and all(any(facet_token(v) == t for v in _values_of(r["derived"], k)) for k, t in tokens.items())]


@bp.get("/api/leaderboard/runs")
def api_runs():
    """Every run of a tab (or all tabs), filtered like the board but with no
    default filter: an absent parameter means any."""
    cfg = _cfg()
    rows = store_for(cfg).list_runs(sf=_sf(cfg, None),
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
    return {k: [facet_option(x) for x in sorted(v, key=lambda s: (len(s), s) if s.isdigit() else (1 << 30, s))
                [:FACET_VALUES]]
            for k, v in values.items()}, truncated


def _hero(row: dict | None) -> dict | None:
    if row is None:
        return None
    d = row["derived"]
    return {
        **_run_summary(row),
        "per_query": [{"q": q, **aggregate.metric_values(d, m)}
                      for q, m in zip(aggregate.QUERY_IDS, aggregate.METRICS[1:])],
    }


@bp.get("/api/leaderboard/board")
def api_board():
    cfg = _cfg()
    sf = _sf(cfg, tabs(cfg)[0])
    metric = request.args.get("metric") or "total"
    if metric not in aggregate.METRICS:
        return _errors(400, "metric: total or q1..q22")
    try:
        top = int(request.args.get("top", 10))
    except ValueError:
        top = 10
    if top not in TOP_CHOICES:
        top = 10
    flt, tokens = _filter(default_filter(cfg)), _tokens()
    rows = store_for(cfg).list_runs(sf=sf)
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
    return jsonify({
        "sf": sf,
        "tabs": tabs(cfg),
        "metric": metric,
        "top": top,
        "filter": flt,
        # The token of every applied facet filter, literal or token, so the
        # page selects the matching option either way.
        "filter_tokens": {k: tokens.get(k) or facet_token(flt[k]) for k in _FACET_PARAMS if k in tokens or k in flt},
        "default_filter": default_filter(cfg),
        "facets": facets,
        "facets_truncated": facets_truncated,
        "hero": _hero(hero),
        "hero_fallback": hero_fallback,
        "hero_notice": (fallback_notice({**flt, **{k + "_t": t for k, t in tokens.items()}}, default_filter(cfg))
                        if hero_fallback else None),
        "eligible_count": len(ranked),
        # A row carries its reproduce script so the board can copy it within the click.
        "rows": [{"rank": i + 1, **_run_summary(r), "value": aggregate.metric_values(r["derived"], metric),
                  "repro": r["derived"]["repro"]["command"]}
                 for i, r in enumerate(ranked[:top])],
    })


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
    return render_template(
        "leaderboard_run.html",
        benchmark=cfg, row=row, d=d, meta=row["meta"] or {},
        m=manifest, summary=_run_summary(row),
        per_query=per_query,
        # as_float: a median past float range is a JSON integer the chart would read as Infinity.
        chart={"run_id": run_id, "xflare": [as_float(p["xflare_ms"]) for p in per_query],
               "velox": [as_float(p["velox_ms"]) for p in per_query]},
        repeats=repeats, matrix_repeats=repeats[:MATRIX_REPEATS],
        matrix=_repeat_matrix(repeats[:MATRIX_REPEATS]),
        toml_diff=_toml_diff(manifest["run"]["xflare_toml"]),
        repro=_repro(manifest, d["repro"]),
    )


def _params(manifest: dict, meta: dict, mode: str) -> dict:
    """Flat key -> text view of what a run ran with, for the compare diff. ``mode`` is the interpreted
    application mode from the derived config (a stored value outside the enum shows as "-")."""
    run, build, client = manifest["run"], manifest["build"], manifest["client"]
    res, toml, env = run["resources"], run["xflare_toml"], meta.get("environment") or {}
    bundle, gluten = build.get("bundle") or {}, build.get("gluten") or {}
    devices = env.get("devices") or []
    p = {
        "scale_factor": run["scale_factor"], "codec": run["codec"],
        "engines": ",".join(run["engines"]), "application_mode": mode,
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
    return {k: "—" if v is None else str(v) for k, v in p.items()}


@bp.get(f"/b/{BENCH_ID}/compare")
def compare_page():
    cfg = _cfg()
    ids = list(dict.fromkeys(x for x in (request.args.get("runs") or "").split(",") if x))
    if not 2 <= len(ids) <= 4 or not all(UUID4_RE.fullmatch(x) for x in ids):
        abort(400, description="compare takes 2 to 4 distinct run ids: ?runs=a,b[,c,d]")
    store = store_for(cfg)
    runs = []
    for run_id in ids:
        row = store.get(run_id)
        if row is None:
            abort(404)
        runs.append({"row": row, "d": row["derived"], "summary": _run_summary(row),
                     "params": _params(store.load_manifest(run_id), row["meta"] or {},
                                        row["derived"]["config"]["application_mode"])})
    base = runs[0]["d"]

    def delta(v, b):
        """Exact Δ% of v against b; +inf when it is past float range, which
        lb_pct prints as "out of range"."""
        if v is None or not b:
            return None
        try:
            return float((Fraction(v) - Fraction(b)) * 100 / Fraction(b))
        except OverflowError:
            return math.inf

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
        values = [r["params"].get(k, "—") for r in runs]
        # Compared in full; the template's v() cuts them for display.
        params.append({"key": k, "values": values, "differs": len(set(values)) > 1})
    return render_template(
        "leaderboard_compare.html",
        benchmark=cfg, runs=runs, queries=queries, totals=totals,
        params=params,
        chart={"names": [r["row"]["run_id"][:8] for r in runs],
               "series": [[as_float(r["d"]["per_query"][str(q)]["xflare_ms"]) for q in aggregate.QUERY_IDS]
                          for r in runs]},
    )
