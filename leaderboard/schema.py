"""Stdlib validator for README §1 (manifest) and §2 (envelope).

Authoritative; schema/*.schema.json document the same rules for producers.

Errors and warnings are "<json path>: <problem>" strings. Submitted values only ever appear as short repr() snippets.
Unknown properties are warnings (forward compatible). Inside the manifest they are part of content_id;
elsewhere in the envelope they are stored metadata.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime

MAX_BODY_BYTES = 8 * 1024 * 1024
SUBMISSION_SCHEMA = "xflare-bench-submission/1"
MANIFEST_SCHEMA = "xflare-bench-manifest/1"
ENGINES = ("velox", "xflare")
CODECS = ("snappy", "zstd", "uncompressed")
# A7: optional per-application maps of what the session and the engine ran with (string values).
APPLICATION_SETTINGS = ("spark_conf_effective", "xflare_parameters_effective")
MAX_MESSAGES = 50  # per list; a hostile body must not turn into a multi-megabyte 400 response
# Contract amendment: any submitted JSON value is at most 64 levels deep (each object/array is one level). Every later
# step (hashing, storage, rendering) recurses, so deeper documents are refused before anything else touches them.
MAX_DEPTH = 64
DEPTH_ERROR = f"JSON nesting deeper than {MAX_DEPTH} levels"
_END = object()

# ASCII digits only: \d would also accept e.g. U+0661, which later time parsing rejects.
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
UUID4_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")  # use fullmatch
SCALE_FACTOR_RE = re.compile(r"[0-9]+(\.[0-9]+)?")


class BadJSON(ValueError):
    pass


def _no_dup_keys(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise BadJSON(f"duplicate key {_snip(k)}")
        obj[k] = v
    return obj


def _bad_constant(name):
    raise BadJSON(f"non-finite number {name}")


def _finite_float(s):
    f = float(s)
    if not math.isfinite(f):  # 1e400 parses to inf without ever spelling "Infinity"
        raise BadJSON("non-finite number")
    return f


def too_deep(value) -> bool:
    """True beyond MAX_DEPTH objects/arrays; the iterator stack uses memory bounded by depth."""
    stack = [iter((value,))]
    while stack:
        v = next(stack[-1], _END)
        if v is _END:
            stack.pop()
            continue
        if isinstance(v, (dict, list)):
            if len(stack) > MAX_DEPTH:
                return True
            stack.append(iter(v.values() if isinstance(v, dict) else v))
    return False


def parse_json_strict(body: bytes) -> object:
    """Every BadJSON message starts with "invalid JSON: " (callers print it as is)."""
    try:
        obj = json.loads(body.decode("utf-8"), object_pairs_hook=_no_dup_keys, parse_constant=_bad_constant,
                         parse_float=_finite_float)
        if too_deep(obj):
            raise BadJSON(DEPTH_ERROR)
        # A lone surrogate ("\ud800") parses but cannot be hashed as UTF-8 later.
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except BadJSON as e:
        raise BadJSON(f"invalid JSON: {e}") from None
    except UnicodeError:
        raise BadJSON("invalid JSON: body is not valid UTF-8 text") from None
    except RecursionError:  # json.loads recurses: far deeper than MAX_DEPTH fails inside the parser itself
        raise BadJSON(f"invalid JSON: {DEPTH_ERROR}") from None
    except ValueError as e:
        raise BadJSON(f"invalid JSON: {str(e)[:120]}") from None
    return obj


def _snip(v) -> str:
    if type(v) is int and v.bit_length() > 128:  # repr of a huge int is slow, long, or refused above 4300 digits
        return f"<{'-' if v < 0 else ''}integer of ~{int(v.bit_length() * 0.30103) + 1} digits>"
    r = repr(v)
    return r if len(r) <= 40 else r[:37] + "..."


def _type_name(v) -> str:
    if v is None:
        return "null"
    return {bool: "boolean", int: "integer", float: "number", str: "string", list: "array",
            dict: "object"}.get(type(v), type(v).__name__)


class _V:
    """Collects errors/warnings while walking one document. Field checkers skip keys that are absent
    (obj() already reported them as missing)."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.dropped = {"errors": 0, "warnings": 0}

    def _add(self, kind: str, line: str):
        out = getattr(self, kind)
        if len(out) < MAX_MESSAGES:
            out.append(line)
        else:
            self.dropped[kind] += 1

    def err(self, path, msg):
        self._add("errors", f"{path}: {msg}")

    def warn(self, path, msg):
        self._add("warnings", f"{path}: {msg}")

    def result(self) -> tuple[list[str], list[str]]:
        for kind, n in self.dropped.items():
            if n:
                getattr(self, kind).append(f"... {n} more {kind}")
        return self.errors, self.warnings

    def obj(self, v, path, required, nullable=False, optional=()) -> dict | None:
        """Object with the `required` keys and optionally the `optional` ones (any other key warns).
        Returns it only when it is a dict."""
        if v is None and nullable:
            return None
        if not isinstance(v, dict):
            self.err(path, f"expected object, got {_type_name(v)}")
            return None
        for k in required:
            if k not in v:
                self.err(f"{path}.{k}", "missing")
        for k in v:
            if k not in required and k not in optional:
                self.warn(f"{path}.{_snip(k)}", "unknown property (kept, not interpreted)")
        return v

    def sub(self, d, k, path, required, nullable=False, optional=()) -> dict | None:
        return self.obj(d[k], f"{path}.{k}", required, nullable, optional) if k in d else None

    def str_(self, d, k, path, nullable=False, pattern=None, enum=None, what=None):
        if k not in d:
            return None
        v, p = d[k], f"{path}.{k}"
        if v is None and nullable:
            return None
        if not isinstance(v, str):
            self.err(p, f"expected string{' or null' if nullable else ''}, got {_type_name(v)}")
            return None
        if enum is not None and v not in enum:
            self.err(p, f"{_snip(v)} not one of {'|'.join(enum)}")
        elif pattern is not None and not pattern.fullmatch(v):
            self.err(p, f"{_snip(v)} is not {what}")
        return v

    def hex64(self, d, k, path, nullable=False):
        self.str_(d, k, path, nullable, _HEX64, what="64 lowercase hex")

    def time(self, d, k, path, nullable=False):
        v = self.str_(d, k, path, nullable, _TIME, what="an RFC 3339 UTC time (...Z)")
        if v is not None and _TIME.fullmatch(v):  # the grammar covers the fraction; strptime checks the calendar
            try:
                datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                self.err(f"{path}.{k}", f"{_snip(v)} is not a valid date/time")

    def int_value(self, v, p, nullable=False, minimum=None, maximum=None):
        if v is None and nullable:
            return None
        if type(v) is not int:  # bool is an int subclass and 1.0 is a float: both rejected
            self.err(p, f"expected integer{' or null' if nullable else ''}, got {_type_name(v)}")
            return None
        if (minimum is not None and v < minimum) or (maximum is not None and v > maximum):
            lo = "" if minimum is None else _snip(minimum)
            hi = "" if maximum is None else _snip(maximum)
            self.err(p, f"{_snip(v)} outside {lo}..{hi}")
            return None
        return v

    def int_(self, d, k, path, **kw):
        return self.int_value(d[k], f"{path}.{k}", **kw) if k in d else None

    def bool_(self, d, k, path, nullable=False):
        if k in d and not (isinstance(d[k], bool) or (nullable and d[k] is None)):
            self.err(f"{path}.{k}", f"expected boolean{' or null' if nullable else ''}, got {_type_name(d[k])}")

    def list_(self, d, k, path) -> list:
        if k not in d:
            return []
        if not isinstance(d[k], list):
            self.err(f"{path}.{k}", f"expected array, got {_type_name(d[k])}")
            return []
        return d[k]


def _manifest(v: _V, m, path):
    m = v.obj(m, path, ("schema", "run_id", "created_at", "client", "build", "run", "repeats"))
    if m is None:
        return
    if "schema" in m and m["schema"] != MANIFEST_SCHEMA:
        v.err(f"{path}.schema", f"expected {MANIFEST_SCHEMA!r}, got {_snip(m['schema'])}")
    v.str_(m, "run_id", path, pattern=UUID4_RE, what="a lowercase uuid4")
    v.time(m, "created_at", path)

    c = v.sub(m, "client", path, ("revision", "branch", "dirty", "dirty_digest"))
    if c is not None:
        p = f"{path}.client"
        v.str_(c, "revision", p, pattern=_HEX40, what="40 lowercase hex")
        v.str_(c, "branch", p, nullable=True)
        v.bool_(c, "dirty", p)
        v.str_(c, "dirty_digest", p, nullable=True)

    b = v.sub(m, "build", path, ("artifact_id", "provenance_source", "revision", "dirty", "dirty_digest", "bundle",
                                 "gluten"))
    if b is not None:
        p = f"{path}.build"
        v.str_(b, "artifact_id", p, nullable=True)
        v.str_(b, "provenance_source", p, enum=("artifact-manifest", "local-checkout", "unknown"))
        v.str_(b, "revision", p, nullable=True)
        v.bool_(b, "dirty", p, nullable=True)
        v.str_(b, "dirty_digest", p, nullable=True)
        kind = None
        bun = v.sub(b, "bundle", p, ("requested", "resolved", "kind"))
        if bun is not None:
            v.str_(bun, "requested", f"{p}.bundle", nullable=True)
            v.str_(bun, "resolved", f"{p}.bundle", nullable=True)
            kind = v.str_(bun, "kind", f"{p}.bundle", enum=("admin", "dev", "unknown"))
        g = v.sub(b, "gluten", p, ("commit", "dirty", "build_command"), nullable=True)
        if g is not None:
            v.str_(g, "commit", f"{p}.gluten", nullable=True)
            v.bool_(g, "dirty", f"{p}.gluten", nullable=True)
            v.str_(g, "build_command", f"{p}.gluten", nullable=True)
            if kind in ("admin", "unknown"):
                v.warn(f"{p}.gluten", "expected null unless bundle.kind=dev")

    n_req = None
    requested = set()
    r = v.sub(m, "run", path, ("argv", "scale_factor", "codec", "engines", "queries", "repeat_requested",
                               "record_answers", "answers", "resources", "xflare_toml", "xflare_overrides", "dataset",
                               "started_at", "finished_at", "exit_status"),
              optional=("application_mode", "spark_conf", "spark_conf_readback"))
    if r is not None:
        p = f"{path}.run"
        v.str_(r, "application_mode", p, enum=("shared", "per-engine"))
        for field in ("spark_conf", "spark_conf_readback"):
            _string_map(v, r, field, p)
        for i, s in enumerate(v.list_(r, "argv", p)):
            if not isinstance(s, str):
                v.err(f"{p}.argv[{i}]", f"expected string, got {_type_name(s)}")
        v.str_(r, "scale_factor", p, pattern=SCALE_FACTOR_RE, what="a decimal scale factor")
        v.str_(r, "codec", p, enum=CODECS)
        engines = v.list_(r, "engines", p)
        if isinstance(r.get("engines"), list) and not engines:
            v.err(f"{p}.engines", "empty")
        seen_engines = set()
        for i, e in enumerate(engines):
            if e not in ENGINES:
                v.err(f"{p}.engines[{i}]", f"{_snip(e)} not one of {'|'.join(ENGINES)}")
            elif e in seen_engines:
                v.err(f"{p}.engines[{i}]", f"duplicate engine {e}")
            else:
                seen_engines.add(e)
        for i, q in enumerate(v.list_(r, "queries", p)):
            q = v.int_value(q, f"{p}.queries[{i}]", minimum=1, maximum=22)
            if q in requested:
                v.err(f"{p}.queries[{i}]", f"duplicate query {q}")
            elif q is not None:
                requested.add(q)
        n_req = v.int_(r, "repeat_requested", p, minimum=1)
        v.bool_(r, "record_answers", p)
        a = v.sub(r, "answers", p, ("path", "sha256"))
        if a is not None:
            v.str_(a, "path", f"{p}.answers", nullable=True)
            v.hex64(a, "sha256", f"{p}.answers", nullable=True)
        res = v.sub(r, "resources", p, ("executors", "executor_cores", "offheap", "shuffle_partitions", "devices"))
        if res is not None:
            pr = f"{p}.resources"
            v.int_(res, "executors", pr, minimum=1)
            v.int_(res, "executor_cores", pr, minimum=1)
            v.str_(res, "offheap", pr, nullable=True)
            v.int_(res, "shuffle_partitions", pr, nullable=True, minimum=1)
            v.int_(res, "devices", pr, nullable=True, minimum=1)
        t = v.sub(r, "xflare_toml", p, ("name", "input_text", "input_sha256", "sent_sha256", "rendered_text",
                                        "rendered_sha256"))
        if t is not None:
            pt = f"{p}.xflare_toml"
            v.str_(t, "name", pt)
            v.str_(t, "input_text", pt)
            v.hex64(t, "input_sha256", pt)
            v.hex64(t, "sent_sha256", pt)
            v.str_(t, "rendered_text", pt, nullable=True)
            v.hex64(t, "rendered_sha256", pt, nullable=True)
        for i, o in enumerate(v.list_(r, "xflare_overrides", p)):
            po = f"{p}.xflare_overrides[{i}]"
            o = v.obj(o, po, ("key", "value", "source", "destination"))
            if o is not None:
                v.str_(o, "key", po)
                v.str_(o, "value", po)
                v.str_(o, "source", po, enum=("cli", "default"))
                v.str_(o, "destination", po, enum=("session", "template"))
        ds = v.sub(r, "dataset", p, ("uri", "marker"))
        if ds is not None:
            v.str_(ds, "uri", f"{p}.dataset", nullable=True)
            v.str_(ds, "marker", f"{p}.dataset", nullable=True)
        v.time(r, "started_at", p)
        v.time(r, "finished_at", p)
        v.str_(r, "exit_status", p, enum=("ok", "failed"))

    seen_idx = set()
    for i, rep in enumerate(v.list_(m, "repeats", path)):
        pr = f"{path}.repeats[{i}]"
        rep = v.obj(rep, pr, ("index", "invocation", "app_id", "artifact_id", "bundle", "started_at", "finished_at",
                              "status", "cleanup", "queries"), optional=("applications",))
        if rep is None:
            continue
        idx = v.int_(rep, "index", pr, minimum=1, maximum=n_req)
        if idx in seen_idx:
            v.err(f"{pr}.index", f"duplicate repeat index {_snip(idx)}")
        elif idx is not None:
            seen_idx.add(idx)
        v.str_(rep, "invocation", pr)
        v.str_(rep, "app_id", pr, nullable=True)
        v.str_(rep, "artifact_id", pr, nullable=True)
        v.str_(rep, "bundle", pr, nullable=True)
        v.time(rep, "started_at", pr)
        v.time(rep, "finished_at", pr, nullable=True)
        v.str_(rep, "status", pr, enum=("completed", "failed", "aborted"))
        v.str_(rep, "cleanup", pr, enum=("ok", "failed", "unknown"))
        if r is not None and r.get("application_mode", "shared") == "per-engine":
            _applications(v, rep, pr, r)
        elif r is not None and r.get("application_mode", "shared") == "shared" and "applications" in rep:
            v.err(f"{pr}.applications", "must be absent in shared application mode")
        seen_q = set()
        for j, q in enumerate(v.list_(rep, "queries", pr)):
            pq = f"{pr}.queries[{j}]"
            q = v.obj(q, pq, ("id", "engines", "evidence"))
            if q is None:
                continue
            qid = v.int_(q, "id", pq, minimum=1, maximum=22)
            if qid in seen_q:
                v.err(f"{pq}.id", f"duplicate query {qid} in one repeat")
            elif qid is not None:
                seen_q.add(qid)
                if requested and qid not in requested:
                    v.warn(f"{pq}.id", f"query {qid} was not in run.queries")
            if "engines" in q:
                if not isinstance(q["engines"], dict):
                    v.err(f"{pq}.engines", f"expected object, got {_type_name(q['engines'])}")
                else:
                    for name, res in q["engines"].items():
                        if name in ENGINES:
                            _engine_result(v, res, f"{pq}.engines.{name}")
                        else:
                            v.err(f"{pq}.engines", f"engine {_snip(name)} not one of {'|'.join(ENGINES)}")
            ev = v.sub(q, "evidence", pq, ("status", "detail"))
            if ev is not None:
                v.str_(ev, "status", f"{pq}.evidence", nullable=True)
                if "detail" in ev and ev["detail"] is not None and not isinstance(ev["detail"], dict):
                    v.err(f"{pq}.evidence.detail", f"expected object or null, got {_type_name(ev['detail'])}")


def _string_map(v: _V, obj: dict, field: str, path: str):
    """`obj[field]`, when present, must be an object of string values (A5, A7)."""
    if field not in obj:
        return
    settings = obj[field]
    if not isinstance(settings, dict):
        v.err(f"{path}.{field}", f"expected object, got {_type_name(settings)}")
        return
    for key, value in settings.items():
        if not isinstance(value, str):
            v.err(f"{path}.{field}[{_snip(key)}]", f"expected string, got {_type_name(value)}")


def _applications(v: _V, rep: dict, path: str, run: dict):
    p = f"{path}.applications"
    if "applications" not in rep:
        v.err(p, "required in per-engine application mode")
        return
    applications = v.list_(rep, "applications", path)
    if not applications:
        if isinstance(rep["applications"], list) and rep.get("status") != "aborted":
            v.err(p, "may be empty only for an aborted repeat")
        return
    before = len(v.errors) + v.dropped["errors"]
    engines = run.get("engines")
    positions = {e: i for i, e in enumerate(engines) if isinstance(e, str)} if isinstance(engines, list) else {}
    seen, last = set(), -1
    fields = ("engine", "invocation", "app_id", "artifact_id", "bundle", "started_at", "finished_at", "cleanup")
    for i, application in enumerate(applications):
        pa = f"{p}[{i}]"
        app = v.obj(application, pa, fields, optional=APPLICATION_SETTINGS)
        if app is None:
            continue
        for field in APPLICATION_SETTINGS:
            _string_map(v, app, field, pa)
        engine = v.str_(app, "engine", pa, enum=ENGINES)
        if engine in ENGINES:
            if engine not in positions:
                v.err(f"{pa}.engine", f"{engine} not in run.engines")
            elif engine in seen:
                v.err(f"{pa}.engine", f"duplicate engine {engine} in one repeat")
            else:
                pos = positions[engine]
                if pos < last:
                    v.err(f"{pa}.engine", "applications must follow run.engines order")
                last = pos
                seen.add(engine)
        v.str_(app, "invocation", pa)
        for key in ("app_id", "artifact_id", "bundle"):
            v.str_(app, key, pa, nullable=True)
        v.time(app, "started_at", pa)
        v.time(app, "finished_at", pa, nullable=True)
        v.str_(app, "cleanup", pa, enum=("ok", "failed", "unknown"))
    if len(v.errors) + v.dropped["errors"] != before:
        return
    expected = {key: applications[0][key] for key in ("invocation", "app_id", "started_at")}
    expected["finished_at"] = applications[-1]["finished_at"]
    cleanups = [app["cleanup"] for app in applications]
    expected["cleanup"] = "ok" if all(c == "ok" for c in cleanups) else "failed" if "failed" in cleanups else "unknown"
    for key in ("artifact_id", "bundle"):
        values = [app[key] for app in applications]
        expected[key] = values[0] if all(value == values[0] for value in values) else None
    for key, value in expected.items():
        if key in rep and rep[key] != value:
            v.err(f"{path}.{key}", "must equal the value derived from applications")


def application_mode_state(manifest: dict) -> tuple[str, list[str]]:
    """Interpret A4 on read as well as on ingestion: older validators kept these fields unchecked.

    Reuse the application rules before derivation indexes any application field. Never change the raw manifest.
    """
    run = manifest["run"]
    mode = run.get("application_mode", "shared")
    if mode not in ("shared", "per-engine"):
        return "-", [f"application_mode not understood: {_snip(mode)}"]
    if mode == "per-engine":
        for rep in manifest["repeats"]:
            v = _V()
            _applications(v, rep, "$.repeats", run)
            if v.errors:
                return mode, [f"applications missing or malformed in repeat {_snip(rep['index'])}"]
    return mode, []


def _engine_result(v: _V, res, path):
    res = v.obj(res, path, ("status", "elapsed_ms", "rows", "verify", "error"))
    if res is None:
        return
    v.str_(res, "status", path, enum=("ok", "error", "timeout", "skipped"))
    v.int_(res, "elapsed_ms", path, nullable=True, minimum=0)  # 0 is legal input; README §4 makes it ineligible
    v.int_(res, "rows", path, nullable=True, minimum=0)
    v.str_(res, "error", path, nullable=True)
    ver = v.sub(res, "verify", path, ("against", "status", "detail"), nullable=True)
    if ver is not None:
        p = f"{path}.verify"
        v.str_(ver, "against", p, nullable=True, enum=("answers", "previous"))
        v.str_(ver, "status", p, nullable=True, enum=("ok", "mismatch", "unavailable", "changed", "new"))
        v.str_(ver, "detail", p, nullable=True)


def _span(v: _V, d, path, keys):
    d = v.obj(d, path, keys)
    if d is not None:
        for k in keys:
            if k in ("start", "end"):
                v.time(d, k, path)
            else:
                v.str_(d, k, path)


def _envelope(v: _V, e):
    e = v.obj(e, "$", ("schema", "manifest", "submitter", "note", "isolation_claim", "producer", "environment"))
    if e is None:
        return
    if "schema" in e and e["schema"] != SUBMISSION_SCHEMA:
        v.err("$.schema", f"expected {SUBMISSION_SCHEMA!r}, got {_snip(e['schema'])}")
    if "manifest" in e:
        _manifest(v, e["manifest"], "$.manifest")
    s = v.sub(e, "submitter", "$", ("email", "sdp_id"))
    if s is not None:
        v.str_(s, "email", "$.submitter")
        v.str_(s, "sdp_id", "$.submitter", nullable=True)
    v.str_(e, "note", "$")
    v.str_(e, "isolation_claim", "$", enum=("exclusive", "shared", "unknown"))
    v.str_(e, "producer", "$")
    env = v.sub(e, "environment", "$", ("source", "observed_at", "host", "devices", "sdk", "pxl", "reservation",
                                        "booking_overlap", "overlapping", "cpu_contention"))
    if env is None:
        return
    p = "$.environment"
    v.str_(env, "source", p, enum=("reservation-server", "manual", "none"))
    v.time(env, "observed_at", p, nullable=True)
    v.str_(env, "host", p, nullable=True)
    for i, d in enumerate(v.list_(env, "devices", p)):
        pd = f"{p}.devices[{i}]"
        d = v.obj(d, pd, ("id", "serial", "type", "fw"))
        if d is not None:
            v.str_(d, "id", pd)
            for k in ("serial", "type", "fw"):
                v.str_(d, k, pd, nullable=True)
    v.str_(env, "sdk", p, nullable=True)
    v.str_(env, "pxl", p, nullable=True)
    if env.get("reservation") is not None:
        _span(v, env["reservation"], f"{p}.reservation", ("id", "owner", "start", "end"))
    v.str_(env, "booking_overlap", p, enum=("none", "present", "unknown"))
    for i, o in enumerate(v.list_(env, "overlapping", p)):
        _span(v, o, f"{p}.overlapping[{i}]", ("owner", "start", "end"))
    v.str_(env, "cpu_contention", p, enum=("unmeasured",))


def validate_manifest(m) -> tuple[list[str], list[str]]:
    if too_deep(m):
        return [f"$: {DEPTH_ERROR}"], []
    v = _V()
    _manifest(v, m, "$")
    return v.result()


def validate_envelope(e) -> tuple[list[str], list[str]]:
    """Paths inside the manifest start with "$.manifest."."""
    if too_deep(e):
        return [f"$: {DEPTH_ERROR}"], []
    v = _V()
    _envelope(v, e)
    return v.result()
