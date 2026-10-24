"""How the leaderboard shows what it accepted: the view settings from
config.yaml, the display budgets, and the helpers that apply them. Used by the
Blueprint (``web.py``) and, through the ``lb_budget`` template global, the
pages and their script.

What is shown is bounded, never what is accepted: an uploaded scalar is cut to
SCALAR_CHARS, a run-level long text (note, toml texts, build command) to
PREVIEW_CHARS, a list to PREVIEW_ITEMS. Filters, the run detail API and the
raw download always use the full stored values.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal

from . import aggregate
from .repro import MAX_ARGS, MAX_CHARS
from .schema import CODECS, DEFAULT_REPO, parse_prset_key, pr_key, prset_key

BENCH_ID = "xflare_leaderboard"
SCALAR_CHARS = 512
PREVIEW_CHARS = MAX_CHARS
PREVIEW_ITEMS = MAX_ARGS
FACET_VALUES = 200
MATRIX_REPEATS = 10
# A7: a recorded settings map (hundreds of keys) is read key by key, so its list budget is wider than PREVIEW_ITEMS.
EFFECTIVE_ITEMS = 500
DEFAULT_CODEC = "snappy"
# A9: a basis on the board, a chart label or a list shows this many PR chips plus "+N"; pages with room show all.
BASIS_CHIPS = 3
# A9: edit histories are read 50 changes at a time (older ones stay stored); the hero history keeps the newest points
# and heroes of its window; the PR-set list stops at FACET_VALUES sets.
HISTORY_EVENTS = 50
HISTORY_POINTS = 1000
HISTORY_HEROES = 200
BUDGET = {"scalar_chars": SCALAR_CHARS, "preview_chars": PREVIEW_CHARS, "preview_items": PREVIEW_ITEMS,
          "facet_values": FACET_VALUES, "effective_items": EFFECTIVE_ITEMS, "basis_chips": BASIS_CHIPS,
          "history_events": HISTORY_EVENTS, "history_points": HISTORY_POINTS, "history_heroes": HISTORY_HEROES}
# Badge text is uploaded-derived; its colour class comes from this fixed map, for the server-rendered pages
# (``badge_class``) and, through the board page's data block, the board script. ``offload:*`` badges are dim.
BADGE_CLASS = {"reproducible": "b-good", "partial": "b-warn", "dev bundle": "b-warn", "booking overlap": "b-bad",
               "overlap unknown": "b-dim", "spark conf": "b-warn", aggregate.SHARED_BADGE: "b-warn"}
# Facet options whose bare value would not say what choosing it does (README §4, A5).
FACET_LABELS = {"spark_conf": {"default": "default Spark settings only", "custom": "custom (--spark-conf)",
                               "unknown": "unknown (spark_conf unreadable)"},
                "basis": {"pr": "PR-based (all)", "main": "main", "-": "unknown (no basis)"}}


_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def norm_date(v: str | None) -> str | None:
    """An inclusive YYYY-MM-DD bound, or None: a half-typed date from a form
    field must narrow nothing rather than cut the window somewhere unintended."""
    if v is None:
        return None
    v = str(v).strip()
    return v if _DATE_RE.match(v) else None


def _pinned_tabs(cfg: dict) -> list[tuple[str, str]]:
    """The configured tabs, always shown: each configured scale factor with the default codec."""
    return [(str(t), DEFAULT_CODEC) for t in cfg.get("tabs") or ("100", "1000")]


def tabs(cfg: dict, deriveds: list[dict]) -> list[dict]:
    """The board tabs, {sf, codec, label}: the pinned ones in configured order, then every other (scale factor,
    codec) of the stored runs, scale factors in numeric order (the text breaks ties: "1", "1.0" and "01" stay
    distinct tabs) and codecs in CODECS order. The label cuts the scale factor like any uploaded scalar."""
    pinned = _pinned_tabs(cfg)
    found = {aggregate.tab_of(d) for d in deriveds} - set(pinned)
    rest = sorted(found, key=lambda t: (Decimal(t[0]), t[0], CODECS.index(t[1]) if t[1] in CODECS else len(CODECS)))
    return [{"sf": sf, "codec": codec, "label": tab_label(sf, codec)} for sf, codec in [*pinned, *rest]]


def tab_label(sf: str, codec: str) -> str:
    """A tab as pages name it: the scale factor cut like any uploaded scalar, then the codec."""
    return f"{cut(sf)} · {codec}"


def tab_note(cfg: dict, sf: str, codec: str) -> str | None:
    """What a dry run says about the tab its run would land on: nothing for a pinned tab."""
    if (sf, codec) in _pinned_tabs(cfg):
        return None
    return f"SF {cut(sf)} · {codec} is not a pinned tab: the first run stored at it creates its tab"


def default_filter(cfg: dict) -> dict:
    return cfg.get("default_filter") or aggregate.DEFAULT_FILTER


def as_float(x) -> float | None:
    """x as a float, or None when there is none or it is past float range: the
    contract puts no upper bound on durations and medians/totals stay exact
    ints, so one can be too big for float()."""
    try:
        return None if x is None else float(x)
    except OverflowError:
        return None


def cut(x, limit: int = SCALAR_CHARS):
    """An uploaded scalar as displayed: at most ``limit`` characters plus "…".
    Short values keep their type. Only scalars are ever passed here; a
    structure is not stringified (its repr could be arbitrarily large)."""
    if x is None or isinstance(x, (bool, float)):
        return x
    if not isinstance(x, (str, int)):
        return "[…]"
    text = str(x)
    if len(text) <= limit:
        return x
    return text[:limit] + "…"


def badge_class(badge) -> str:
    """The colour class of one badge: BADGE_CLASS, "b-dim" for an ``offload:*`` badge, else none."""
    if isinstance(badge, str) and badge.startswith("offload:"):
        return "b-dim"
    return BADGE_CLASS.get(badge, "") if isinstance(badge, str) else ""


def facet_token(value: str) -> str:
    """A bounded stand-in for a facet value of any length. Tokens travel in
    their own ``<key>_t`` parameter, so they never collide with a literal."""
    return "h:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def facet_option(value: str, key: str | None = None) -> dict:
    """A selectable facet value of filter ``key``: the token selects it, the
    label (FACET_LABELS, else the value cut for display) names it."""
    label = FACET_LABELS.get(key, {}).get(value)
    members = parse_prset_key(value[len("prset:"):]) if key == "basis" and value.startswith("prset:") else None
    if members:
        label = "PR set " + " + ".join(pr_label(m["repo"], m["number"]) for m in members)
    return {"token": facet_token(value), "label": label or cut(value)}


def pr_label(repo: str, number: int) -> str:
    """A PR as chips name it: ``#N`` in the default repository, else ``<name>#N`` (the key is in the tooltip)."""
    return f"#{number}" if repo == DEFAULT_REPO else f"{repo.split('/', 1)[1]}#{number}"


def pr_url(repo: str, number: int) -> str:
    """The PR on GitHub, built from parts the parser accepted (never from submitted URL text)."""
    return f"https://github.com/{repo}/pull/{number}"


def basis_view(basis: dict | None) -> dict | None:
    """A stored basis as the API and pages show it (A9); every member is there (at most MAX_PRS)."""
    if basis is None:
        return None
    if basis["kind"] == "main":
        repo, n = basis["repo"], basis["latest_pr"]
        return {"kind": "main", "repo": repo, "latest_pr": n, "url": pr_url(repo, n),
                "label": f"main · latest {pr_label(repo, n)}"}
    prs = [{"repo": m["repo"], "number": m["number"], "key": pr_key(m["repo"], m["number"]),
            "url": pr_url(m["repo"], m["number"]), "label": pr_label(m["repo"], m["number"])} for m in basis["prs"]]
    return {"kind": "pr", "prset": prset_key(basis["prs"]), "prs": prs, "label": " + ".join(p["label"] for p in prs)}


def short_basis(view: dict | None) -> str:
    """A basis in at most BASIS_CHIPS PR labels plus "+N" (chart labels, lists)."""
    if view is None:
        return "—"
    if view["kind"] == "main":
        return view["label"]
    labels = [p["label"] for p in view["prs"]]
    return " + ".join(labels[:BASIS_CHIPS]) + (f" +{len(labels) - BASIS_CHIPS}" if len(labels) > BASIS_CHIPS else "")


def fallback_notice(flt: dict, default: dict) -> str:
    """What the hero says when it fell back: "no E×C result yet" when the
    applied filter is exactly the default executors × executor_cores filter,
    else a generic line."""
    keys = ("executors", "executor_cores")
    applied = {k: str(v) for k, v in flt.items()}
    if set(default) == set(keys) and applied == {k: str(default[k]) for k in keys}:
        return f"no {applied['executors']}×{applied['executor_cores']} result yet"
    return "no result for this filter yet"
