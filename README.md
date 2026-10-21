# xflare TPC-H leaderboard

A small Flask service that ranks hand-run `spark-tpch.sh` batches people upload. The board is at
`/b/xflare_leaderboard` (`/` redirects there); runs come in through `POST /api/leaderboard/submissions`.

| path | what |
|---|---|
| `app.py` | the service: `create_app(config)` and the `python3 app.py` entry point |
| `config.yaml` | service settings: `server` (port, bind) and `leaderboard` (title, description, color, storage, SF tabs, default filter) |
| `leaderboard/schema.py` | strict JSON parse (duplicate keys, NaN → reject) and the stdlib validator (authoritative) |
| `leaderboard/schema/*.schema.json` | JSON Schema for producers (documentation; `schema.py` decides) |
| `leaderboard/aggregate.py` | `content_id`, eligibility, medians, totals, ordering, hero — pure functions |
| `leaderboard/repro.py` | the paste-ready reproduce script stored with each run |
| `leaderboard/store.py` | SQLite (WAL) + raw manifest files |
| `leaderboard/web.py` | Blueprint: API and pages |
| `leaderboard/display.py` | display budgets (cut lengths, list and facet sizes) and the helpers that apply them |
| `leaderboard/backup.py`, `systemd/` | the service unit; an optional daily NAS backup and its restore (not installed on server 60) |
| `leaderboard/fixtures/` | contract fixtures; `make_fixtures.py` regenerates them |
| `templates/`, `static/` | the board, run and compare pages |

## Run locally

Python ≥ 3.10 with Flask and PyYAML (`requirements.txt`).

```
mkdir -p /tmp/lb-data
XFLARE_LEADERBOARD_DATA=/tmp/lb-data python3 app.py --host 127.0.0.1 --port 8894
```

- Settings come from `config.yaml`, or the file named by `XFLARE_LEADERBOARD_CONFIG` or `--config`.
  `--host` / `--port` override `server.bind` / `server.port` (default `0.0.0.0:8894`).
- Data dir: `XFLARE_LEADERBOARD_DATA` > `leaderboard.storage.data_dir`. The service never creates it (see Storage).
- Tests: `python3 leaderboard/test_leaderboard.py` (runs `test_leaderboard_web.py` and `test_leaderboard_backup.py`
  too; each also runs on its own). The page-script tests need `node`, the reproduce-script tests `bash`; without
  them those tests fail, naming the missing runtime.

## Payload contract

This section restates data contract v1 (frozen 2026-10-19, shared with the producer `spark-tpch.sh --submission`
and the submit skill) with amendments A1–A6; this README is the contract's home, and code comments cite it as
README §1–§4. The schema files give each field's type; the meanings are stated here.

### §1 Manifest

`"schema": "xflare-bench-manifest/1"`, written by `spark-tpch.sh --submission FILE`: one `run_id` (uuid4) per
batch, all repeats included; `client` (the checkout that ran it), `build` (artifact, bundle, gluten), `run` (argv
verbatim and in order, scale factor, codec, engines, queries, resources, the resource toml as read and as rendered,
overrides, dataset, start/finish, exit status) and `repeats[]` (every attempted repeat, with per-query, per-engine
status, elapsed ms, rows, verify result and offload evidence). The producer pins repeats 2..N to the artifact and
bundle repeat 1 resolved and records a mismatch rather than hiding it; it stops the batch when a repeat's cleanup is
not `ok` (failed or unknown) and lists the remaining repeats as `aborted`. It rejects `--repeat` together with
`--request`.

Field meanings the types do not carry:
- `run.codec`: an empty codec is normalised to `snappy`.
- `run.xflare_toml`: `input_text` / `input_sha256` are the template as read from disk, `sent_sha256` is it after the
  engine/logging transformation, `rendered_text` / `rendered_sha256` what the run read (`null` when not recorded).
- `run.xflare_overrides`: the requested parameters, in the order they take effect.
- `repeats[].artifact_id` / `bundle`: as resolved for that repeat's own application.

A4 (additive; a manifest without these fields is unchanged and means `shared`):
- `run.application_mode`: `shared` (absent = shared): each repeat is one Spark application that runs the engines
  one after another; `per-engine` (`spark-tpch.sh --application-per-engine`): each repeat starts a fresh
  application per engine, in `run.engines` order, so no engine inherits the other's warmed JVM/executors.
- `repeats[].applications[]`: only in `per-engine` mode, where it is required (empty only for an `aborted`
  repeat); a `shared` manifest that has it is a 400. Each element is `{engine (velox|xflare), invocation, app_id,
  artifact_id, bundle, started_at, finished_at, cleanup (ok|failed|unknown)}`; engines are a subset of
  `run.engines`, in its order, without duplicates in one repeat.
- In `per-engine` mode the repeat-level fields are derived from a non-empty `applications[]` and must agree with it
  (else a 400): `invocation` / `app_id` are the first application's, `started_at` the first start, `finished_at`
  the last finish (`null` when the last has none), `cleanup` is `ok` only if every application's is, else `failed`
  if any failed, else `unknown`; `artifact_id` / `bundle` the common value when all applications agree, else
  `null`. `status` keeps its meaning.
- A6: only `per-engine` runs rank (§4), so `spark-tpch.sh --submission` runs in per-engine mode and its manifest
  always says `application_mode: "per-engine"`. A `shared` manifest is still valid and accepted.

A5 (additive; absent = unknown, a manifest from before A5):
- `run.spark_conf`: object `{"<key>": "<value>"}` (string values) of the `spark-tpch.sh --spark-conf KEY=VALUE`
  settings in effect, the last value of a key winning as the script applies them; `{}` when none.
- `run.spark_conf_readback`: same shape, what the Spark session reported back for those keys (the script's
  `--results-dir` JSON); `{}` when none.
- Either one that is not an object of string values is a 400. Accepted values are bounded only by the body cap;
  pages cut them like any uploaded string. Producers refuse a `--submission` whose `spark_conf`,
  `xflare_overrides` or argv carry a secret-looking key (`secret|password|token|credential|access.?key|\.key$`,
  case-insensitive) rather than publish it; the server does not repeat that check.

### §2 Envelope

`"schema": "xflare-bench-submission/1"`: `manifest` (the §1 document as `spark-tpch.sh` wrote it),
`submitter {email, sdp_id}` (`email` any string, empty allowed), `note`, `isolation_claim`
(`exclusive|shared|unknown`), `producer` (the submit skill sends `xflare-bench-submit@<xflare revision>`),
`environment {source, observed_at, host, devices[], sdk, pxl, reservation, booking_overlap, overlapping[],
cpu_contention}`; `booking_overlap` is judged from booking history over [`run.started_at`, `run.finished_at`].
Times are RFC 3339 UTC (`...Z`), durations integer ms, unknown is `null` (never a placeholder string such as
"config", "derived" or "current"). Every string is plain text and is rendered escaped.

### §3 Identity and API

The routes are in API below.
- `content_id` = sha256 of `json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` as UTF-8.
  Only the manifest is hashed; every other envelope field (`submitter`, `note`, `isolation_claim`, `producer`,
  `environment`) is mutable metadata.
- `run_id` is unique. Same `run_id` + same `content_id` → 200 with the original receipt: the `ranked_total` and
  `warnings` stored when the run was created, even after a withdrawal. Metadata is still updated.
  Same `run_id` + different `content_id` → 409, nothing changes.
- Withdraw is a soft flag anyone can set: ownership is a trust convention. A withdrawn run never ranks on any board
  or as hero, and an identical re-upload (200) does not revive it.
- Unknown properties are warnings, not errors (inside `manifest` they are still part of the hash). Request bodies
  are capped at 8 MiB. A1: any JSON value in a submission is at most 64 levels deep (each object or array is one
  level); deeper bodies (submission, dry run, withdraw) are a 400
  `{"errors": ["invalid JSON: JSON nesting deeper than 64 levels"]}` and nothing is stored. A validation 400 lists
  at most 50 errors and 50 warnings, then one line saying how many more.

### §4 Eligibility and ranking

Computed on the server, never taken from the client.
- Query `q` of a run counts when, in every completed repeat, both velox and xflare have `status=ok`,
  `elapsed_ms > 0`, `verify.against=answers`, `verify.status=ok`; the run has `record_answers=false`; and all
  completed repeats share one `artifact_id` and one `bundle`. A2: a failed or aborted repeat does not count toward
  the medians, but a wrong answer it recorded for `q` (an engine `status` other than ok/skipped, or a
  `verify.status` other than ok/null) still disqualifies `q` for that run, with a reason such as
  `xflare error in aborted repeat 3` on the run page. A failed/aborted repeat that never reached `q` adds nothing.
- A4, in `per-engine` mode: every completed repeat must have an application for every engine in `run.engines`
  (else no query counts: `applications missing <engine> in completed repeat N`), and `artifact_id` and `bundle`
  must each be one value across all applications of all completed repeats (`null` counts as a value), on top of the
  repeat-level check above (`artifact differs across applications of completed repeats`, likewise `bundle`).
- A6 (supersedes A4's "both modes ranked together"): `Q(run, q)` and total-eligible additionally require the
  interpreted `run.application_mode` to be `per-engine`. A `shared` run (field absent or `"shared"`) — ABBA
  measurement 2026-10-20: the engine that runs first in a shared application is ~2% slower overall and up to 25% on
  short queries, so its ranking would depend on its `--engines` order — is accepted (dry run and a new POST answer as
  usual, `ranked_total: false`; an identical retry of a run stored before A6 returns its original §3 receipt, whose
  `ranked_total` may read true although the run no longer ranks), stored and listed, but is on no board (Total or any query), is never the hero and
  is never an A3 fallback candidate. It gets the common reason `shared application: engine order affects timings`
  (first in `common`) and the `shared application` badge. Its per-query medians and totals are still computed and
  shown (run page, detail API, compare) as measurements; every `per_query[q].eligible` and `total_eligible` is
  false. A run whose stored mode is unreadable (`-`, below) keeps its own reason and gets no shared badge.
- Reasons (`derived.ineligible_reasons`): causes shared by every query (A6's shared application, recording mode,
  incomplete repeat list, no completed repeat, artifact or bundle differing across repeats, and the A4 application
  checks) are listed once under `common`; `qN` lists only
  what is specific to that query, and `total` the Total-specific causes. A reason never embeds an unbounded value
  (a huge repeat index is shortened). The dry-run response lists at most 50 reasons per key, then `... N more`; the
  stored run keeps them all.
- Total-eligible: all 22 queries count, every repeat completed, `run.exit_status=ok`. Only these rank on the Total
  board and can be the hero. A run that is not total-eligible still ranks on the per-query boards where `q` counts
  (badge `partial`).
- Per query: median over repeats (`statistics.median`, even N = mean of the two middle values), no rounding.
  Total = sum of the 22 medians (ranked by the xflare total). Speedup = velox / xflare, `null` when xflare is 0.
- Numeric domain: durations have no upper bound. Medians and totals are exact integers where they can be (odd N,
  an even pair with an even sum, sums); an even-N median of `x.5` that does not fit a float makes that query
  ineligible, and a total that does not fit makes the total ineligible, both with reason
  `value beyond numeric range`. A speedup that does not fit a float is `null` and pages print "—"; a duration past
  float range prints "out of range".
- Order on every board: metric asc, completed repeats desc, `run.finished_at` asc, `content_id` asc.
- Hero per SF tab = first total-eligible, non-withdrawn run under the active filter (default `executors=4`,
  `executor_cores=6`). A3: when none matches, the hero falls back to the tab's fastest total-eligible run in any
  configuration, under a notice ("no 4×6 result yet" for the default filter, "no result for this filter yet"
  otherwise) and with its config label shown at the top; the board itself stays filtered. The fallback never
  crosses tabs, and (A6) its candidates are total-eligible, hence `per-engine`, runs only; with none on the tab
  there is no hero. Tabs: SF-100, then SF-1000.
- Badges never gate: `xN`, `reproducible` (`build.provenance_source=artifact-manifest`, `build.dirty=false`,
  `client.dirty=false`, and a bundle that is not `dev` or one with `gluten.commit` set and `gluten.dirty=false`),
  `dev bundle`, `booking overlap` / `overlap unknown`, `partial`,
  `offload:<status>` (the evidence status of the completed repeats, whole; `offload:mixed` when they disagree),
  `spark conf` (A5: the Spark settings state is `custom`), `shared application` (A6: informational; the gate is the
  rule above). Bundle and resources are columns and filters, never separate boards.
- A4: the application mode is part of the config label, a board column (Mode) and a filter, never a board split,
  and the default filter does not narrow it. (A4's "both modes rank together" is superseded by A6: only
  `per-engine` runs rank, so the Mode column shows `per-engine`; filtering to `shared` leaves the board empty and
  the page lists those runs below it.)
- A5: every run has a Spark settings state, `custom` when `run.spark_conf` is a non-empty object and `default` when
  it is `{}`. It never gates (§4 eligibility, order and hero are unchanged) and never splits a board: a `custom` run
  gets the `spark conf` badge and a ` +conf` marker at the end of its config label, and the filter `spark_conf`
  narrows to one state (`default` = "default Spark settings only"); the default filter does not narrow it.

Interpretations the contract leaves open (decided here, not contract changes):
- The `offload:<status>` badge reads the evidence statuses `partial` and `offloaded_except_skipped` as `offloaded`:
  it tells runs apart by executor core, not by the operators or tasks a query left on velox.
- The ok / elapsed / verify checks of `Q(run, q)` use the repeats with `status=completed`; failed and aborted repeats
  only contribute wrong answers (above). Q needs at least one completed repeat and the listed repeat indices to be
  exactly `1..repeat_requested`; `q` must appear in every completed repeat.
- "All repeats share one artifact_id and one bundle" is checked over completed repeats (an aborted repeat never
  resolved one); `null` counts as a value.
- `finished_at` ordering compares parsed UTC instants exact to the nanosecond (all 9 accepted fraction digits),
  not strings.
- A4 on runs stored before it: a mode outside the enum shows as `-` (`application_mode not understood: …`), and a
  per-engine run whose applications break the A4 rules gets `applications missing or malformed in repeat N`; both
  stay listed but never rank. A shared run's stray `applications` stay uninterpreted (since A6 it never ranks, as a
  shared run). An identical re-upload of such a run still gets 200 with its original receipt (§3), although today's validator would
  reject the manifest as a new submission; nothing is stored or updated by it.
- A5 on manifests without `run.spark_conf` (contract: unknown): the state is read from argv, `custom` when argv
  has an actual `--spark-conf` option and `default` otherwise. argv is read with `spark-tpch.sh`'s option arity
  (as the reproduce script reads it), so a `--spark-conf` that is another option's value does not count, and
  reading stops at the first entry it cannot place (unknown option, missing value). The run page and compare say
  whether the state came from `run.spark_conf` or from argv. A run stored before A5 whose `run.spark_conf` is not an
  object of string values (the older validator kept unknown fields) gets the state `unknown`: listed,
  matched by neither `spark_conf=default` nor `custom`.

## API

No authentication: trust is the submit skill and the network. Errors are `{"errors": [...]}` with fixed-format
messages; only a 400 from envelope/manifest validation also carries `warnings`. A body that is not a JSON object
(invalid JSON, duplicate key, NaN) is a 400 with `errors` only. Any storage failure is a 503
`{"errors": ["leaderboard storage unavailable"]}` (plain text on the run and compare pages); the board page itself
reads no storage and still loads.

| method, path | params / body | responses |
|---|---|---|
| `POST /api/leaderboard/submissions` | body = envelope; `?dry_run=1` also accepts a bare manifest | 201 created: receipt `{run_id, content_id, url, ranked_total, warnings}`; 200 existing: the receipt as first issued; dry run: 200 `{dry_run: true, run_id, content_id, url, ranked_total, warnings, badges, ineligible_reasons}` and nothing written; 400 `{errors, warnings}` (validation) or `{errors}` (unparseable body); 409 `{errors, run_id, content_id}`; 413 over 8 MiB; 503 |
| `POST /api/leaderboard/runs/<run_id>/withdraw` | `{"actor": str, "reason": str}`, both non-empty (no length cap) | 200 run summary (idempotent; the first withdrawal's actor/reason/time are kept); 400; 404 `unknown run_id`; 503 |
| `GET /api/leaderboard/runs` | `sf=100\|1000`, `include_withdrawn=1`, and the board's filters with NO default (absent = any) | 200 `[run summary]`, oldest `finished_at` instant first (then `run_id`); 400 bad sf |
| `GET /api/leaderboard/runs/<run_id>` | | 200 run summary + `derived`, `meta`, `manifest`; 404 `unknown run_id` |
| `GET /api/leaderboard/runs/<run_id>/raw` | | 200 the whole stored manifest file (no size cap), `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`; 404 `raw manifest not found` (no row, file missing, symlink or not a regular file); 503 when the file cannot be read |
| `GET /api/leaderboard/board` | `sf` (default first tab), `metric=total\|q1..q22`, `top=5\|10\|25` (default 10), filters `executors`, `executor_cores`, `offheap`, `shuffle_partitions`, `devices`, `toml`, `bundle`, `application_mode` (`shared\|per-engine`), `spark_conf` (`default\|custom`, or `unknown`; §4 A5), `device`, `submitter`, `badge` (each also as `<key>_t` = facet token), `from`, `to` (YYYY-MM-DD) | 200 `{sf, tabs, metric, top, filter, filter_tokens, default_filter, facets, facets_truncated, hero, hero_fallback, hero_notice, eligible_count, shared_count, shared_runs, rows}`; `shared_count` is the number of non-withdrawn `shared` runs under the filter (A6: stored and listed, never ranked) and `shared_runs` the first `top` of them, oldest `finished_at` first, as `{run_id, url, finished_at, config_label}`; `hero_fallback` is true when `hero` is the tab's fallback run (rows stay filtered, possibly empty) and `hero_notice` is then the line the page shows above it, else null; `hero` adds `per_query` (22 entries from that one run); each row is a run summary plus `rank`, `value {xflare_ms, velox_ms, speedup}` and `repro` (the reproduce script, or null when none is offered); 400 bad sf or metric |
| `GET /` | | 302 to `/b/xflare_leaderboard` |
| `GET /b/xflare_leaderboard` | | page: tabs, hero chart, board, and under the board the `shared_runs` (A6) linked to their pages; a tab with no total-eligible run shows the empty hero, and a board with no rows says why |
| `GET /b/xflare_leaderboard/run/<run_id>` | | page: for a `shared` run (A6) a notice at the top ("shared application: engine order affects timings — this run is listed but never ranked (amendment A6)…") and its measured totals beside the reasons it is not ranked; argv, overrides, the contract's resource fields, toml input vs rendered diff, environment, per-repeat table (plus, for a `per-engine` run, each repeat's applications: engine, cleanup, app_id, artifact, bundle, started/finished, invocation), application mode, Spark settings (the state and where it came from, then `run.spark_conf` and `run.spark_conf_readback` as key → value tables, each "not recorded" when absent, "none ({})" when empty and "not a key/value object" when stored before A5 validated it), raw download, how to reproduce, withdraw |
| `GET /b/xflare_leaderboard/compare?runs=a,b[,c,d]` | 2–4 distinct run ids | page: per-query deltas vs the first run, parameter diff (including `application_mode` and four Spark settings rows: `spark_conf (state)`, `spark_conf.source`, and `spark_conf (settings)` / `spark_conf_readback (settings)`, each map on one line as "not recorded" (absent), "{}" (empty), "not an object" (stored before A5 validated it) or its sorted `k=v` pairs joined by "; ", cut like any scalar; a settings row differs when the full maps differ); 400 / 404 |

A run summary is `{run_id, content_id, url, sf, finished_at, submitter, total_eligible, repeats, totals, config,
config_label, build, device_ids, device_count, host, badges, ineligible_reasons, withdrawn, withdrawal}`. It is a
display view: every uploaded string in it (`withdrawal.actor`/`reason` included) is cut to 512 characters plus "…",
and `device_ids` holds at most 100 ids (`device_count` is the full count). `ineligible_reasons` is
`{"common": [...]}`: the run's common reasons (§4), why no query of it can rank — for a shared run (A6) it starts
with `shared application: engine order affects timings`; empty for a run whose queries can rank. Each reason is cut
like any scalar and the list stops at 50 plus `... N more`; the per-query and Total reasons are in the detail API's
`derived.ineligible_reasons`, uncut. The detail API's `withdrawal`, `derived`, `meta`,
`manifest` and the raw download are never cut. A `run_id` that is not a lowercase uuid4 is refused before any
lookup: JSON 404 `unknown run_id` on the API, HTML 404 on the run page, 400 on compare.

Filters: `facets` lists at most 200 options per filter as `{token, label}`, with `facets_truncated` set when any list
was capped; the label is the value cut like any scalar, the token is `h:<16 hex>` (the first 16 hex digits of the
value's sha256); the `spark_conf` options are labelled by what they select ("default Spark settings only",
"custom (--spark-conf)", "unknown (spark_conf unreadable)"). Each facet filter takes two parameters on `/board` and
`/runs`: `<key>` is a literal compared exactly with the full stored value, and `<key>_t` a token, matching a run whose
value hashes to it. They never stand in for each other; given both, a run must match both, and an unknown token
matches nothing. The page's options send `<key>_t`. The board echoes the applied literals in `filter` (as sent) and,
in `filter_tokens`, the token of every applied facet filter, so a shared URL with a full value selects the offered
option. On `/board` an absent filter takes the configured default (`executors`, `executor_cores`); sent empty
(`executors=`) it means "any", which is how the page lifts the default. Config filters compare as text with a `null`
value spelled `-`; `from`/`to` that are not YYYY-MM-DD narrow nothing.

Pages show previews of uploaded data; the raw JSON always has everything. Every uploaded string is cut to 512
characters plus "…", except the run-level long texts (note, toml input and rendered text, gluten build command), which
stop at 65,536 characters. Lists (argv, overrides, devices, overlapping bookings, samples, repeats, each repeat's
applications, ineligible reasons, the run page's Spark settings maps, compare override rows) stop at 100 items with
"N more — see raw JSON", and the repeat × query matrix shows the first 10 repeats. A duration, or a compare Δ%, past
float range prints "out of range", and the charts get `null` for it. The toml diff is skipped above 1000 lines or
65,536 characters per side.

**Reproduce script.** It pastes into a shell from an xflare-analytics checkout: it checks out the run's client
revision and runs `scripts/spark-tpch.sh` with the run's argv, read with `spark-tpch.sh`'s own option arity. Each
actual `--reservation` value becomes `"$RESERVATION"` in place, and one is added when argv has none (the reader's
own booking; nothing starts until it is set). The artifact and bundle the run resolved are pinned with `--artifact`
/ `--bundle` unless argv's own options chose them. Comments flag a dirty client checkout and a dev bundle (usable
only by its owner). No script is offered, and the run page says why, when argv has more than 100 entries, the script
passes 65,536 characters (a cut command would run something else), or argv cannot be read as `spark-tpch.sh` options
(unknown option, missing value, an argument that is not a query id, or `--`). The script is built once per run
(`repro.py`) and stored with its derived data; the run page shows that stored copy and the board's Repro column
copies it on click (a run without one links to its page). Copying also works on a plain-http origin.

On the board, the Run column shows when each run finished (UTC) and links to its page; the config cell shows only
the resources (e.g. `4×6 64g`, plus `sp…`/`d…` when set, and ` +conf` for custom Spark settings) and its tooltip
spells out every config field; the Mode column shows the application mode (always `per-engine` since A6). Badge
colours come from one fixed map (`display.BADGE_CLASS`, sent to the board script in the page's data block):
`reproducible` green; `partial`, `dev bundle`, `spark conf` and `shared application` amber; `booking overlap` red;
`overlap unknown` and `offload:*` dim. The run and compare pages colour badges the same way.

A6 on the live service: the four runs submitted in shared mode on 2026-10-20 (SF-100 and SF-1000, each at 1×6 and
4×6) stay stored and visible — `/api/leaderboard/runs`, their run pages, compare and the raw download, with the
shared reason and badge, and their content ids, medians and totals unchanged — but they leave the boards: until
per-engine runs arrive both SF tabs show no rows and no hero, and list the runs matching the filter under the board
(the 4×6 one by default; clear the filter for both). They are not withdrawn.

## How a CI job would submit (method only)

Nothing in CI does this today. A job that wants its runs on the board would:

1. Run `spark-tpch.sh ... --submission manifest.json` (it writes the `xflare-bench-manifest/1` document).
2. Wrap it in an envelope: `submitter.email` = the job's owner address, `note` = job name + build URL,
   `isolation_claim` = what the job's booking guarantees, `producer: <job>@<revision>`, and `environment` from the
   reservation server (`source: reservation-server`, `observed_at` = when it was read). Without one, every
   environment key must still be present; the minimal valid object is
   ```json
   {"source": "none", "observed_at": null, "host": null, "devices": [], "sdk": null, "pxl": null,
    "reservation": null, "booking_overlap": "unknown", "overlapping": [], "cpu_contention": "unmeasured"}
   ```
   Keep the envelope file: it is the retry unit.
3. `POST /api/leaderboard/submissions?dry_run=1` with it and stop on 400 (print `errors`; they are the fix list).
4. `POST /api/leaderboard/submissions` and act on the status:
   - 201 / 200 — stored; 200 means this exact run was already stored (a retry). Print `url`.
   - 409 — the `run_id` exists with different content: a bug in the job (it reused a manifest). Do not retry.
   - 400 — invalid envelope; do not retry. 413 — over 8 MiB; do not retry.
   - 503 / connection error / timeout — retry the SAME envelope file with backoff (it is idempotent: a lost
     response comes back as 200 with the original receipt).
5. Never rebuild the manifest for a retry: a changed byte is a different `content_id` and turns the retry into a 409.

## Storage

- Data dir: env `XFLARE_LEADERBOARD_DATA` > `config.yaml` `leaderboard.storage.data_dir`
  (`/home/sw-devops/data/xflare_leaderboard`). It must exist (the service never creates it) and sit on local disk:
  SQLite WAL needs real shared memory and locks. Missing or unwritable → 503 on the storage routes.
- `<data>/leaderboard.sqlite3` (WAL, busy timeout) and `<data>/raw/<content_id>.json` holding exactly the canonical
  manifest bytes, so `sha256(file) == content_id`.
- A submission writes the raw file first (tmp + fsync + rename), then the row in one short transaction. A crash
  in between leaves an orphan raw file, which is harmless and reused on retry.
- After an upgrade that changes the derivation, a stored run is re-derived from its raw file the first time it is
  read and the result is written back once (derived values, version, eligibility; receipt and metadata untouched).
  A stored run whose raw file is missing makes storage unavailable.
- Every SQLite connection the service opens, reads included, is opened, used and closed under one process-wide
  lock, and so is the one-time setup (WAL mode, tables, `user_version`). Run the service as a single process;
  nothing else should open the DB read-write (`backup.py` opens it read-only).

## Backup and restore (optional)

The server 60 deployment runs without a backup: the data lives only in the data dir on that host's local disk, and a
lost disk loses the submissions. `backup.py` and its units stay in the repo so it can be turned on later (see the end
of the deploy runbook).

```
python3 leaderboard/backup.py backup  --data /home/sw-devops/data/xflare_leaderboard \
    --dest /mnt/sw_nas_ext/public/xcena_xflare_leaderboard_backup --mount /mnt/sw_nas_ext --keep 14
python3 leaderboard/backup.py restore --from <dest>/<YYYYMMDDTHHMMSSZ> --data <empty or absent dir>
```

backup, in order; any failure exits 1 with one line on stderr and leaves no complete generation:
1. `--dest` must exist and `findmnt -T <dest>` must report `--mount` (an unmounted share never fills the local disk).
2. Online backup (`sqlite3.Connection.backup`) from a read-only connection to the live DB into a local temp file,
   switched to a rollback journal, then copied to `<dest>/<stamp>.partial/`. The service keeps serving and writing
   meanwhile.
3. `PRAGMA integrity_check` on the copy; copy `raw/<content_id>.json` for every `content_id` in the copy's `runs`;
   verify `sha256(copy) == content_id` for each.
4. Write `BACKUP.json` (format, time, host, source, sqlite version, DB sha256/rows/user_version), fsync, rename to
   `<dest>/<stamp>/`. A directory with that name and a `BACKUP.json` is a complete generation.
5. Keep the newest `--keep` complete generations, delete older complete ones. A failed run removes its own
   `.partial`. A `.partial` from another run (crash, power loss) is never deleted — it is reported on stderr for a
   human to look at. Nothing else under `--dest` is touched.

restore verifies the generation first (`BACKUP.json` present, DB sha256 matches, `integrity_check`, every raw hash)
and writes nothing if any check fails. It then copies `raw/` and, last, the DB, so an interrupted restore never
leaves a dir the service would open as a partial leaderboard (without the DB it would start an empty one). The
target must be empty or absent. To restore the live data: stop the service, restore into a new dir, move the old dir
aside, move the new one into place, start the service.

The units are in `systemd/`: `xflare-leaderboard.service` (the service) and, for the optional backup,
`xflare-leaderboard-backup.{service,timer}` (daily 03:30 host time + up to 10 min jitter, `Persistent=true`, oneshot).
All run as `sw-devops` with `/usr/bin/python3` from `/home/sw-devops/xflare-leaderboard`.

## Deploy runbook (server 60, done by a person)

The service runs on its own port next to anything else on the host and shares nothing with other services
(Benchmark Hub on :8892 is unrelated and unchanged). Every git command runs as `sw-devops`, the checkout's owner
(git refuses a repository owned by someone else).

Server 60 as checked at the first deploy (2026-10-20): Ubuntu 22.04.5; `/usr/bin/python3` 3.10.12 with Flask 2.3.3,
Werkzeug 3.1.3, PyYAML 6.0.1 and SQLite 3.37.2; port 8894 free; ufw inactive and `INPUT ACCEPT` (no firewall
change needed); `/home` is local ext4. Re-run step 0 before a fresh install on another host; if port 8894 or the
checkout path must differ there, change `server.port` in `config.yaml` or the paths in `systemd/` before step 4.

0. Pre-checks (read-only):
   ```
   sudo -u sw-devops /usr/bin/python3 -c 'import sys,sqlite3; from importlib.metadata import version as v; print(sys.version.split()[0], "flask", v("flask"), "werkzeug", v("werkzeug"), "pyyaml", v("pyyaml"), "sqlite", sqlite3.sqlite_version)'
   ss -ltnp 'sport = :8894'                       # must list no socket
   sudo ufw status; sudo iptables -S INPUT | head  # ufw active: sudo ufw allow 8894/tcp; another firewall: open 8894/tcp there
   findmnt -T /home/sw-devops; df -h /home/sw-devops
   ```
   `requirements.txt` sets the floor: Flask ≥ 2.2 (Werkzeug comes with it) and PyYAML ≥ 6.0; below that, stop. The
   SQLite WAL-reset bug is fixed in 3.51.3 and backported to 3.44.6 and 3.50.7, so a version is fixed when it is
   ≥ 3.51.3, or 3.50.7–3.50.x, or 3.44.6–3.44.x. Server 60's 3.37.2 is not. What this app does about it: the
   service opens, uses and closes every connection (close-time checkpoints and the one-time setup included) under
   one process-wide lock, and `backup.py`, when used, reads through a read-only connection that can neither write
   nor checkpoint. That keeps the concurrent write/checkpoint pattern the advisory describes out of the app's own
   paths. What remains: any other process that opens the DB read-write (a `sqlite3` shell, a second service
   instance) — do not; on an unpatched host such as server 60, do not open the DB file at all while the service runs.
   Upgrading SQLite is the only mitigation the advisory itself offers.
1. Give `sw-devops` a read-only deploy key for this repo (its default GitHub key belongs to a person):
   ```
   sudo -u sw-devops ssh-keygen -t ed25519 -N '' -C 'sw-devops@server60 xflare-leaderboard (read-only)' -f /home/sw-devops/.ssh/xflare_leaderboard_deploy
   sudo -u sw-devops cat /home/sw-devops/.ssh/xflare_leaderboard_deploy.pub
   ```
   Add the printed line under the repo's Settings → Deploy keys, with "Allow write access" unchecked. Then add an
   ssh alias that uses only that key (`ssh -T` naming a person instead of the repo means another key was used):
   ```
   printf '%s\n' '' 'Host github-xflare-leaderboard' '    HostName github.com' '    User git' \
     '    IdentityFile ~/.ssh/xflare_leaderboard_deploy' '    IdentitiesOnly yes' |
     sudo -u sw-devops tee -a /home/sw-devops/.ssh/config >/dev/null
   sudo -u sw-devops ssh -T github-xflare-leaderboard      # "Hi xcena-dev/xflare-leaderboard!" = the deploy key
   sudo -u sw-devops git ls-remote github-xflare-leaderboard:xcena-dev/xflare-leaderboard.git   # lists HEAD and main
   ```
2. Clone and record the SHA:
   ```
   sudo -u sw-devops git clone github-xflare-leaderboard:xcena-dev/xflare-leaderboard.git /home/sw-devops/xflare-leaderboard
   sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard rev-parse HEAD
   ```
3. Create the data dir as `sw-devops` on local disk:
   ```
   sudo -u sw-devops mkdir -p /home/sw-devops/data/xflare_leaderboard
   findmnt -T /home/sw-devops/data/xflare_leaderboard     # FSTYPE must be a local fs (ext4/xfs), not nfs/cifs
   ```
   The service checks the dir on every request, so creating it after the service started needs no restart.
4. Install and start the service:
   ```
   sudo cp /home/sw-devops/xflare-leaderboard/systemd/xflare-leaderboard.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now xflare-leaderboard
   systemctl status xflare-leaderboard --no-pager | head -5    # active (running)
   ss -ltn 'sport = :8894'                                      # LISTEN 0.0.0.0:8894, a second or two after start
   ```
   The journal's "This is a development server" warning is expected: the service is Flask's own threaded server.
5. Smoke:
   ```
   for p in / /b/xflare_leaderboard /api/leaderboard/board; do curl -s -o /dev/null -w "%{http_code} $p\n" "http://127.0.0.1:8894$p"; done
   curl -s -X POST -H 'Content-Type: application/json' --data-binary @/home/sw-devops/xflare-leaderboard/leaderboard/fixtures/valid-envelope.json \
     'http://127.0.0.1:8894/api/leaderboard/submissions?dry_run=1'          # 200, "dry_run": true
   curl -s 'http://127.0.0.1:8894/api/leaderboard/runs'                    # [] — the dry run stored nothing
   find /home/sw-devops/data/xflare_leaderboard -name '*.json' | wc -l     # 0 raw files
   ```
   Expect `302 /`, `200` for the other two. Then open `http://192.168.97.60:8894/` from another machine.
6. Jail egress, once per user, on that user's jail host (the submit skill and a jail's browser both need it):
   ```
   jail egress-policy allow 192.168.97.60:8894
   ```
   (Syntax as the jail tooling documents it; confirm with `jail egress-policy --help`.) Check from inside the jail:
   `curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.97.60:8894/` prints `302`; `000` means no HTTP answer
   — egress still closed, or the service down — and curl's error line says which.

**Upgrade.** Record the running SHA, pull, restart, and run step 5's status loop and dry run (its `/runs` and
raw-file counts apply to a fresh install only):
```
sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard rev-parse HEAD      # keep it: the version to go back to
sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard pull --ff-only
sudo systemctl restart xflare-leaderboard
```
To go back a version: `sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard checkout <recorded SHA>` and
`sudo systemctl restart xflare-leaderboard`. That leaves the checkout on a detached HEAD, so the next upgrade runs
`sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard checkout main` after recording the SHA and before the
pull (record first: the SHA to keep is the one running, not main's).

**Rollback.** `sudo systemctl disable --now xflare-leaderboard` (and `xflare-leaderboard-backup.timer` if the
backup was turned on). Nothing else on the host depends on the service. Leave `/home/sw-devops/data/xflare_leaderboard`
in place so a re-deploy picks it up. To remove it entirely, also delete `/etc/systemd/system/xflare-leaderboard.service`
and any `xflare-leaderboard-backup.{service,timer}` there (then `sudo systemctl daemon-reload`), the checkout, the
data dir, and the deploy key (GitHub Deploy keys, the `Host github-xflare-leaderboard` block, the key files).

**Turning the backup on later** (create the NAS destination once, as `sw-devops`, while the share is mounted). If
`findmnt` reports a different TARGET than `/mnt/sw_nas_ext`, edit `--mount` in the installed unit before enabling it:
```
findmnt -T /mnt/sw_nas_ext/public                                        # the TARGET must equal --mount in the unit
sudo -u sw-devops test -w /mnt/sw_nas_ext/public && sudo -u sw-devops mkdir -p /mnt/sw_nas_ext/public/xcena_xflare_leaderboard_backup
sudo cp /home/sw-devops/xflare-leaderboard/systemd/xflare-leaderboard-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now xflare-leaderboard-backup.timer
sudo systemctl start xflare-leaderboard-backup.service; journalctl -u xflare-leaderboard-backup -n 20 --no-pager
```
Check one restore by hand once the board holds at least one run (an empty board exercises no raw files):
```
G=$(ls -d /mnt/sw_nas_ext/public/xcena_xflare_leaderboard_backup/2*Z | tail -1)
sudo -u sw-devops /usr/bin/python3 /home/sw-devops/xflare-leaderboard/leaderboard/backup.py restore --from "$G" --data /tmp/lb-restore-check
sudo -u sw-devops /usr/bin/python3 -c 'import sqlite3; c=sqlite3.connect("/tmp/lb-restore-check/leaderboard.sqlite3"); print(c.execute("PRAGMA integrity_check").fetchone(), c.execute("SELECT COUNT(*) FROM runs").fetchone())'
sudo rm -rf /tmp/lb-restore-check
```
To turn it off again: `sudo systemctl disable --now xflare-leaderboard-backup.timer`.
