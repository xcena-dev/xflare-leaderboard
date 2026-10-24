# xflare TPC-H leaderboard

A small Flask service that ranks hand-run `spark-tpch.sh` batches people upload. The board is at
`/b/xflare_leaderboard` (`/` redirects there); runs come in through `POST /api/leaderboard/submissions`.

| path | what |
|---|---|
| `app.py` | the service: `create_app(config)` and the `python3 app.py` entry point |
| `config.yaml` | service settings: `server` (port, bind) and `leaderboard` (title, description, color, storage, pinned SF tabs, default filter) |
| `leaderboard/schema.py` | strict JSON parse (duplicate keys, NaN → reject) and the stdlib validator (authoritative) |
| `leaderboard/schema/*.schema.json` | JSON Schema for producers (documentation; `schema.py` decides) |
| `leaderboard/aggregate.py` | `content_id`, eligibility, medians, totals, ordering, hero — pure functions |
| `leaderboard/repro.py` | the paste-ready reproduce script stored with each run |
| `leaderboard/store.py` | SQLite (WAL) + raw manifest files |
| `leaderboard/web.py` | Blueprint: API and pages |
| `leaderboard/display.py` | display budgets (cut lengths, list and facet sizes) and the helpers that apply them |
| `leaderboard/backup.py`, `systemd/` | the service unit; the auto-updater (Deploy runbook); an optional daily NAS backup and its restore (the backup is not installed on server 60) |
| `leaderboard/fixtures/` | contract fixtures; `make_fixtures.py` regenerates them |
| `templates/`, `static/` | the board, run, PR-set and compare pages |

## Run locally

Python ≥ 3.10 with Flask and PyYAML (`requirements.txt`).

```
mkdir -p /tmp/lb-data
XFLARE_LEADERBOARD_DATA=/tmp/lb-data python3 app.py --host 127.0.0.1 --port 8894
```

- Settings come from `config.yaml`, or the file named by `XFLARE_LEADERBOARD_CONFIG` or `--config`.
  `--host` / `--port` override `server.bind` / `server.port` (default `0.0.0.0:8894`).
- Data dir: `XFLARE_LEADERBOARD_DATA` > `leaderboard.storage.data_dir`. The service never creates it (see Storage).
- Tests: `python3 leaderboard/test_leaderboard.py` (runs `test_leaderboard_web.py`, `test_leaderboard_backup.py` and
  `test_leaderboard_auto_update.py` too; each also runs on its own). The page-script tests need `node` ≥ 14, the
  reproduce-script tests `bash`, the auto-updater tests `bash` and `git`; without them those tests fail, naming the
  missing runtime. `XFLARE_LEADERBOARD_SKIP_NODE_TESTS=1` skips the page-script tests instead; only the auto-updater's
  self-check sets it.

**Before every merge to main, run the page-script tests by hand.** Nothing else runs them: there is no CI, and the
server's auto-updater skips them (server 60's node is 12) and deploys what passes the rest. A page-script regression
merged without this run goes live unattended. As a non-root user, on a machine with node ≥ 14:
```
node --version                                                              # v14 or later
env -u XFLARE_LEADERBOARD_SKIP_NODE_TESTS -u XFLARE_LEADERBOARD_CONFIG python3 leaderboard/test_leaderboard.py
```
It must end in a plain `OK`. `skipped=` in that line means some tests did not run: never set the skip variable here.

## Payload contract

This section restates data contract v1 (frozen 2026-10-19, shared with the producer `spark-tpch.sh --submission`
and the submit skill) with amendments A1–A9; this README is the contract's home, and code comments cite it as
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

A7 (additive; absent = not recorded):
- `repeats[].applications[].spark_conf_effective`: object `{"<key>": "<value>"}` (string values), the session's
  effective `spark.sql.*`, `spark.gluten.*`, `spark.executor.*` and `spark.memory.*` settings, read once after the
  runner has set the application's first engine switch.
- `repeats[].applications[].xflare_parameters_effective`: same shape, the engine's effective parameters as the first
  query admitted by that application's executors printed them (an unset optional parameter is `""`); only xflare
  applications have it.
- Either one that is not an object of string values is a 400 (and, on a stored run, makes the applications "missing or
  malformed" like any other broken A4 field). Like A5's maps, accepted values are bounded only by the body cap.

### §2 Envelope

`"schema": "xflare-bench-submission/1"`: `manifest` (the §1 document as `spark-tpch.sh` wrote it),
`submitter {email, sdp_id}` (`email` any string, empty allowed), `note`, `isolation_claim`
(`exclusive|shared|unknown`), `producer` (the submit skill sends `xflare-bench-submit@<xflare revision>`),
`environment {source, observed_at, host, devices[], sdk, pxl, reservation, booking_overlap, overlapping[],
cpu_contention}`; `booking_overlap` is judged from booking history over [`run.started_at`, `run.finished_at`].
Times are RFC 3339 UTC (`...Z`), durations integer ms, unknown is `null` (never a placeholder string such as
"config", "derived" or "current"). Every string is plain text and is rendered escaped.

A9 (additive; absent = unknown, as for every run from before A9):
- `basis` (optional): what the run measured. `{"kind": "pr", "prs": [{"repo": "owner/name", "number": N}, ...]}` is
  a **PR set** (1 to 16 PRs, none twice once repositories are lower-cased); `{"kind": "main", "repo": "owner/name",
  "latest_pr": N}` a run on main, `latest_pr` being the last PR merged into the client revision (the skill reads it
  from that revision's history). `N` is an integer 1..2147483647 (no float, no boolean); `repo` is a GitHub owner
  (letters, digits and inner hyphens, at most 39) `/` name (letters, digits, `.`, `_`, `-`, at most 100, not only
  dots). `null`, another `kind` or a bad member is a 400; unknown keys inside warn and are left out of the stored
  basis, which is normalized: repositories lower case, PRs sorted by (repo, number). It is the run's initial claim
  only (§4 A9).

### §3 Identity and API

The routes are in API below.
- `content_id` = sha256 of `json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` as UTF-8.
  Only the manifest is hashed; every other envelope field (`submitter`, `note`, `isolation_claim`, `producer`,
  `environment`, A9's `basis`) is mutable metadata.
- `run_id` is unique. Same `run_id` + same `content_id` → 200 with the original receipt: the `ranked_total` and
  `warnings` stored when the run was created, even after a withdrawal. Metadata is still updated, except A9's
  basis (§4).
  Same `run_id` + different `content_id` → 409, nothing changes.
- Withdraw is a soft flag anyone can set: ownership is a trust convention. A withdrawn run never ranks on any board
  or as hero, and an identical re-upload (200) does not revive it.
- Unknown properties are warnings, not errors (inside `manifest` they are still part of the hash). Request bodies
  are capped at 8 MiB. A1: any JSON value in a submission is at most 64 levels deep (each object or array is one
  level); deeper bodies (submission, dry run, withdraw, target change, A9 edit and PR check) are a 400
  `{"errors": ["invalid JSON: JSON nesting deeper than 64 levels"]}` and nothing is stored. A validation 400 lists
  at most 50 errors and 50 warnings, then one line saying how many more.

### §4 Eligibility and ranking

Computed on the server, never taken from the client.
- Query `q` of a run counts when, in every completed repeat, xflare (A7: velox does not gate, see below) has
  `status=ok`, `elapsed_ms > 0`, `verify.against=answers`, `verify.status=ok`; the run has `record_answers=false`; and
  all completed repeats share one `artifact_id` and one `bundle`. A2: a failed or aborted repeat does not count toward
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
- A7 (supersedes the "both velox and xflare" wording of the first bullet and A2's velox case):
  1. Velox never gates. `per_query[q].velox_ms` (and `speedup`) is the velox median when velox is in `run.engines` and,
     in every completed repeat, has `status=ok`, `elapsed_ms > 0`, `verify.against=answers`, `verify.status=ok`, and no
     failed or aborted repeat recorded a wrong velox answer for `q` (A2's test, applied to velox). Otherwise they are `null`
     (pages print "—"), `per_query[q].velox_problems` says why (empty when velox is simply not in `run.engines`), and `q`
     still counts for xflare. The xflare checks, the order (xflare total) and the Total's other conditions are unchanged.
  2. `totals.velox_ms` and `totals.speedup` are `null` unless all 22 velox medians exist (a velox total beyond float
     range is also `null`; it never makes the Total ineligible).
  3. A run with one engine in `run.engines` and no `application_mode` (or `shared`) counts as `per-engine` for A6: there is
     no engine order to protect. It ranks, gets no shared reason or badge, and its `config.application_mode` reads
     `per-engine`. A4's application checks still apply only to a manifest that says `per-engine`.
  4. `engines` (the run's engines sorted and joined by `,`: `velox,xflare` or `xflare`) and `codec` are config keys: part
     of the config label (`... · <mode> · <engines> · <codec>`), filters and facets. `engines` is a board filter, never a
     separate board.
  5. A tab is (scale factor, codec). The pinned tabs are `config.yaml` `leaderboard.tabs` with `snappy`, always listed in
     that order, empty or not; every other (scale factor, codec) of the stored non-withdrawn runs follows, scale factors
     in numeric order (`1`, `1.0` and `01` are three tabs), then `snappy`, `zstd`, `uncompressed`. The first run stored
     at a new pair creates its tab. The hero, the default filter and A3's fallback apply per tab. Existing runs are
     `snappy`; their ranks do not change.
  6. `spark_conf_effective` and `xflare_parameters_effective` are shown on the run page and in the compare diff and gate
     nothing, with one exception: when two completed applications of the same engine recorded different `spark.sql.*` /
     `spark.gluten.*` values (a key present in only one counts), the run gets the common reason
     `Spark settings differ across applications` (the artifact/bundle pattern, per engine because the velox and xflare
     applications differ on purpose in the engine switch; `spark.executor.*` and `spark.memory.*` are shown, not
     compared). An application that recorded none is ignored.
- A8 (additive; a tab never set has no target): each tab may pin a **target**, a stored run whose velox side the board
  compares with, whatever its configuration, and a **goal** speedup (a number above 0, at most 1000, or none). A
  target must be on that tab, not withdrawn, and have a velox total (all 22 velox medians); a pinned target that later
  stops meeting this keeps its place and the board says why (`target.problem`) and compares with the same-run velox. A
  row's `vs_target` = the target's velox / the row's xflare for the board's metric (the total, or that query's
  median); the hero's always compares the totals. Both are `null` without a usable target. It never gates: ranking,
  eligibility and the hero are unchanged. Anyone may change a target (no auth, as withdraw); a change names its
  `actor`, applies only at the revision the caller saw (else 409 with the current state) and is kept as an event, so
  an earlier target can be pinned again. `?target=<run id>` on the board compares one view with another run (no goal,
  nothing stored); `?target=none` with the same-run velox.
- A9 (additive; a run without a basis is unchanged): a run's **basis** (§2) and **Report**, a link to a page that
  explains an effect, never gate: ranking, eligibility, the hero, `content_id` and receipts are unchanged.
  1. The envelope's `basis` is the run's initial claim only: a new run stores it as revision 1 (actor
     `submitter.email`, or `submission` when that is empty); an identical retry (200) never changes it, whatever basis
     it carries (the detail API's `meta` keeps the envelope as last uploaded, its `basis` included; the run's basis is
     the summary's `basis`). After that the basis changes only through its edit (API), versioned like A8's target: a
     change names its `actor`, applies only at the revision the caller saw (else 409 with the current state) and is
     kept as an event; a restore names an earlier event and sets that value again as a new change. A run's basis and
     Report share one revision. Withdrawn runs stay editable.
  2. Report: one optional `report_url` per run (e.g. a main-based tuning run) and one per PR set (the set's effect
     page), any http/https URL (artifact, Claude Doc, Confluence…) of at most 2048 characters with a host name, no
     credentials and only RFC 3986 characters (anything else is refused, never repaired). It is stored as given,
     rendered as a plain link (`rel="noopener noreferrer"`) and set, cleared or restored only through its edit, never
     by a submission. A PR set's Report stays when no run claims the set any more.
  3. A PR set's key is its members `owner/name#N` in stored order joined by `+`
     (`apache/gluten#88+xcena-dev/xflare-analytics#1561`); a `prset` parameter takes the members in any order or
     case and means the canonical key. Pages name a PR `#N` in `xcena-dev/xflare-analytics` and `<name>#N` elsewhere
     (`gluten#88 + #1561`), a main basis `main · latest #N`, and link every PR to
     `https://github.com/<repo>/pull/<n>`, built from the parsed parts.
  4. PR workflow completion: the skill reads each member PR's body and records which run ids it lists (PR check, the
     latest per PR replacing the one before). A run with a PR set is `complete` when every current member's latest
     check lists it, else `pending` (a member not checked, or not listing it). Status only, on the run and PR-set
     pages.
  5. Filters: one Basis facet filter, `basis` = `main` | `pr` (every PR-based run) | `prset:<canonical key>` (exactly
     that set) | `-` (no basis), with `basis_t` tokens; and `pr=<owner/name#N>` (a run whose PR set has that PR; `-`
     = no PR set, i.e. no basis or main), a literal with no facet that a PR chip on the board sets and shows as a
     removable chip. The default filter narrows neither.
  6. Hero history, per tab, filter and metric (`total` or `q1..q22`): every run on that metric's board as a point at
     its `run.finished_at`, and the heroes, a run being one when it beats every run that finished before it in the
     board's order. It is recomputed from today's eligible runs (no A3 fallback), so a withdrawal or a re-derivation
     rewrites it. `vs_target` uses the target's velox for the same metric; the goal applies to the Total only. A
     `from`/`to` window keeps the hero it started with.
  7. PR-set list: the PR sets of the tab's runs under the board's filter, each represented by its best total-eligible
     run in Total board order, sorted `best` (by that run, sets without one last), `number` (highest member PR first)
     or `recent` (latest finished run first). The PR-set page lists every run of the set on every tab; compare by PR
     set compares the representatives.
  8. Budgets (`display.py`; the edit limits in `web.py`): a basis on the board shows at most 3 PR chips plus `+N` (the
     full set in its tooltip; detail pages show all), an edit history 50 changes per page, the hero history the newest
     1000 points and 200 heroes of its window, the PR-set list 200 sets; an A9 `actor` is at most 256 characters, a PR
     check at most 1000 run ids.
  9. Storage: `user_version` 3 adds four tables (Storage) and changes no existing row. The setup is idempotent, so an
     upgrade interrupted anywhere is redone on the next start, and the number is set only after the tables exist. An
     older binary (going back a version) lowers the number, the tables stay (it neither reads nor writes them, so a
     run it stores meanwhile gets no basis); this one raises it again.
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
  there is no hero. Tabs: A7 (above).
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
| `POST /api/leaderboard/submissions` | body = envelope; `?dry_run=1` also accepts a bare manifest | 201 created: receipt `{run_id, content_id, url, ranked_total, warnings}`; 200 existing: the receipt as first issued; dry run: 200 `{dry_run: true, run_id, content_id, url, ranked_total, tab_note, warnings, badges, ineligible_reasons, basis}` and nothing written (`basis` (A9) is the envelope's basis as it would be stored, in the run summary's form, `null` without one; `tab_note` is `null` for a pinned tab, else `SF X · codec is not a pinned tab: the first run stored at it creates its tab`; a dry run reads no store, so the note is advisory); 400 `{errors, warnings}` (validation) or `{errors}` (unparseable body); 409 `{errors, run_id, content_id}`; 413 over 8 MiB; 503 |
| `POST /api/leaderboard/runs/<run_id>/withdraw` | `{"actor": str, "reason": str}`, both non-empty (no length cap) | 200 run summary (idempotent; the first withdrawal's actor/reason/time are kept); 400; 404 `unknown run_id`; 503 |
| `GET /api/leaderboard/runs` | `sf` (any decimal scale factor; one with no runs lists nothing), `include_withdrawn=1`, and the board's filters with NO default (absent = any) | 200 `[run summary]`, oldest `finished_at` instant first (then `run_id`); 400 an `sf` that is not a decimal |
| `GET /api/leaderboard/runs/<run_id>` | | 200 run summary + `derived`, `meta`, `manifest`; 404 `unknown run_id` |
| `GET /api/leaderboard/runs/<run_id>/raw` | | 200 the whole stored manifest file (no size cap), `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`; 404 `raw manifest not found` (no row, file missing, symlink or not a regular file); 503 when the file cannot be read |
| `GET /api/leaderboard/board` | `sf` (default: the first tab) and `codec` (default `snappy`; with neither, the first tab) name the tab (A7), `metric=total\|q1..q22`, `top=5\|10\|25` (default 10), filters `executors`, `executor_cores`, `offheap`, `shuffle_partitions`, `devices`, `toml`, `bundle`, `application_mode` (`shared\|per-engine`), `engines` (`velox,xflare\|xflare`), `spark_conf` (`default\|custom`, or `unknown`; §4 A5), `device`, `submitter`, `badge` (each also as `<key>_t` = facet token), `from`, `to` (YYYY-MM-DD), `target` (a run id, or `none`; A8), `basis` (`main\|pr\|prset:<key>\|-`, also `basis_t`; A9), `pr` (`owner/name#N` or `-`, literal only; A9) | 200 `{sf, codec, tabs, metric, top, filter, filter_tokens, default_filter, facets, facets_truncated, hero, hero_fallback, hero_notice, eligible_count, shared_count, shared_runs, target, target_pin, rows}`; `target` is what the view compares with (A8): `null`, or `{run_id, url, source (pinned\|link), goal, problem}` plus, when `problem` is null, `{config_label, finished_at, velox_ms, per_query (22 velox medians)}`; `target_pin` is the tab's stored target `{sf, codec, run_id, goal, revision, updated_by, updated_at}` (revision 0, no run, when never set); `hero.vs_target` and each row's `value.vs_target` compare with it; `shared_count` is the number of non-withdrawn `shared` runs under the filter (A6: stored and listed, never ranked) and `shared_runs` the first `top` of them, oldest `finished_at` first, as `{run_id, url, finished_at, config_label}`; `hero_fallback` is true when `hero` is the tab's fallback run (rows stay filtered, possibly empty) and `hero_notice` is then the line the page shows above it, else null; `hero` adds `per_query` (22 entries from that one run); each row is a run summary plus `rank`, `value {xflare_ms, velox_ms, speedup, vs_target}` and `repro` (the reproduce script, or null when none is offered); 400 an `sf`/`codec` that is not a tab (the message lists the tabs), a bad metric, or a `target` that is neither a run id nor `none`; `tabs` is `[{sf, codec, label}]` (A7: it was a list of scale factors); the tab consumes `codec`, so it is not among the board's `filter`; every answer, errors included, carries the header `X-Leaderboard-Script` (below) |
| `GET /api/leaderboard/targets/<sf>/<codec>` | | 200 the tab's stored target (as `target_pin` above) plus `history`: its changes, newest first, at most 50, as `{event_id, revision, before_run_id, after_run_id, before_goal, after_goal, actor, at}`; 404 `unknown tab` (a scale factor that is not a decimal, or a codec outside `snappy\|zstd\|uncompressed`) |
| `POST /api/leaderboard/targets/<sf>/<codec>` | `Content-Type: application/json`, `{"actor": str, "expected_revision": int, "run_id": run id \| null, "goal": number \| null}` (`run_id` required; null clears; a goal needs a run) | 200 the stored target (unchanged when it already is that run and goal: no new revision); 400; 403 `cross-site edit refused` (a browser `Origin` other than this site); 404 `unknown run_id` / `unknown tab`; 409 `{errors, current}` when the revision moved; 415 another content type; 503 |
| `POST /api/leaderboard/runs/<run_id>/basis` | `Content-Type: application/json`, `{"actor": str, "expected_revision": int, "basis": basis \| null}` or, instead of `basis`, `"restore": <event id>` (exactly one of them; null clears; `actor` non-empty, at most 256 characters once trimmed; §4 A9) | 200 the run's state `{run_id, revision, basis, report_url, updated_by, updated_at}` (`basis` as in the run summary; unchanged when it already is that value: no new revision); 400 (`restore: not a change of this basis` for an event that is not one of this run's basis changes); 403 `cross-site edit refused`; 404 `unknown run_id`; 409 `{errors, current}` (`changed meanwhile: reload and try again`) when the revision moved; 415 another content type; 503 |
| `POST /api/leaderboard/runs/<run_id>/report_url` | as `/basis`, with `"report_url": URL \| null` (§4 A9; else `report_url: an http(s) URL of at most 2048 characters`) | as `/basis` (`restore: not a change of this report_url`) |
| `GET /api/leaderboard/runs/<run_id>/history` | `before` (an event id: the next older page) | 200 the run's state plus `history`, its basis and Report changes newest first, at most 50, as `{event_id, field (basis\|report_url), revision, before, after, restored_from, actor, at}` (a basis in the run summary's form), and `more` (older changes remain); 400 `before: an event id`; 404 `unknown run_id` |
| `GET /api/leaderboard/prset` | `prset` (a PR set key, members in any order or case), `before` | 200 `{prset (the canonical key), revision, report_url, updated_by, updated_at, history, more, label, prs, runs}` for any valid key, runs or not: `history` as the run's (field `report_url`); `prs` the members `{repo, number, key, url, label, check}`, `check` the latest body check `{repo, number, listed_count, body_sha256, source, checked_by, checked_at}` or null; `runs` every run whose basis is exactly this set, withdrawn ones included, tab by tab in the tabs' order, each tab's total-eligible runs first in board order, then the rest, each a run summary plus `tab`, `place` (null for the rest) and `completion` `{complete, members: [{key, label, status (listed\|not listed\|not checked), checked_at}]}`; 400 `prset: a PR set key (owner/name#N joined by '+', at most 16)` |
| `POST /api/leaderboard/prset` | `?prset=<key>`; body as the run's `/report_url` | 200 the set's state `{prset, revision, report_url, updated_by, updated_at}`; 400 (a bad `prset` too); 403; 409; 415; 503 |
| `POST /api/leaderboard/prs/<owner>/<name>/<number>/check` | `Content-Type: application/json`, `{"actor": str, "run_ids": [run id, ...], "body_sha256": 64 lowercase hex \| null, "source": str \| null}` (at most 1000 lowercase uuid4 run ids, not looked up; `source` at most 64 characters; the last two optional) | 200 the PR's check (as `check` above), which replaces the one before (§4 A9); 400; 403; 404 `unknown PR` (not a GitHub owner/name, or a number outside 1..2147483647); 415; 503 |
| `GET /api/leaderboard/prsets` | the board's `sf`, `codec` and filters (the default applies), `sort=best\|number\|recent` (default `best`) | 200 `{sf, codec, sort, filter, hero_run_id, prsets, truncated}`: each PR set of the tab's runs under the filter as `{prset, basis, report_url (the set's), run_count, eligible_count, last_finished_at, best, is_hero, vs_hero_pct}`, `best` its best total-eligible run `{run_id, url, finished_at, config_label, repeats, xflare_ms, velox_ms, speedup, vs_target}` or null, `vs_hero_pct` that run's total against the hero's (`hero_run_id`: the filtered board's hero, no A3 fallback); at most 200 sets (`truncated`); 400 as `/board` for the tab, or `sort: best, number or recent`; header `X-Leaderboard-Script` |
| `GET /api/leaderboard/history` | the board's `sf`, `codec`, filters (the default applies; `from`/`to` window the series) and `metric` | 200 `{sf, codec, metric, filter, target, points, heroes, truncated}` (§4 A9): `target` `{run_id, url, source, problem, velox_ms (the target's velox for this metric), goal (Total only)}` or null; `points` every run on that board in finish order as `{run_id, url, finished_at, t (epoch ms), xflare_ms, y, hero}`; `heroes` `{run_id, url, finished_at, t, xflare_ms, y, vs_target, config_label, basis, basis_short, report_url, entering, delta_pct}` (`entering`: the hero the window started with; `delta_pct` against the hero before it); the newest 1000 points and 200 heroes (`truncated`); 400 as `/board` (the tab, `metric: total or q1..q22`); header `X-Leaderboard-Script` |
| `GET /` | | 302 to `/b/xflare_leaderboard` |
| `GET /b/xflare_leaderboard` | | page: tabs, hero chart, board, and under the board the `shared_runs` (A6) linked to their pages; a tab with no total-eligible run shows the empty hero, and a board with no rows says why; with a target (A8) the hero shows the target's velox total, the speedup vs target and the goal, its chart the target's velox per query (the same-run velox faint), the board's speedup column compares with the target (the same-run speedup in its tooltip), and on every tab, with or without a hero, the hero card shows a target line (the target and its run page, why a pinned target cannot serve, or that none is set or this view uses none) and a disclosure with the tab's target history, from which an earlier target can be pinned again; A9: Basis and Report columns (a PR set as chips, at most 3 plus `+N` with every member in the tooltip, each chip filtering the board by its PR and a small `↗` beside it opening the PR on GitHub; a main basis one chip to its latest PR on GitHub; "↗ Report" with the URL in its tooltip), Basis and Report rows on the hero card, the Basis filter and the removable PR chip, and two folded panels that load when opened and reload with the board: "Hero history — best so far" (time axis in UTC, a point per run, the stepped best line with each hero's basis as its label, speedup vs target on a second axis with the goal line on the Total, its own Total / Q1..Q22 tabs that follow the board's metric tab, a table of the heroes) and "PR sets on this tab" (the sort, and 2–4 checked sets compared by set) |
| `GET /b/xflare_leaderboard/run/<run_id>` | | page: for a `shared` run (A6) a notice at the top ("shared application: engine order affects timings — this run is listed but never ranked (amendment A6)…") and its measured totals beside the reasons it is not ranked; argv, overrides, the contract's resource fields, toml input vs rendered diff, environment, per-repeat table (plus, for a `per-engine` run, each repeat's applications: engine, cleanup, app_id, artifact, bundle, started/finished, invocation), application mode, Spark settings (the state and where it came from, then `run.spark_conf` and `run.spark_conf_readback` as key → value tables, each "not recorded" when absent, "none ({})" when empty and "not a key/value object" when stored before A5 validated it), A7's settings in effect (each engine's `spark_conf_effective` and `xflare_parameters_effective` from its first application that recorded them, as key → value tables up to 500 keys), a velox cell that is "—" with its `velox_problems` as the tooltip, raw download, how to reproduce, withdraw, and (A8) "set as target" (asks for the actor and an optional goal) when the run can be its tab's target, or "clear target" when it is; (A9) the section "Basis & report (A9)": the basis chips (each to its PR on GitHub), for a PR set its PR-set page and the PR workflow (`complete`/`pending`, each member's check status), the Report, "basis 바꾸기…" and "Report 지정…" ("Report 바꾸기…" once set), and the edit history, newest 50 (`?before=` for older ones), with "이 값으로 되돌리기…" on every value other than the current one |
| `GET /b/xflare_leaderboard/prset?prset=<key>` | a PR set key (any order or case) | page (A9): the members (each to its PR on GitHub, with its last body check and how many runs it lists), the set's Report with "Report 지정…" / "Report 바꾸기…" and its edit history with restore, every run of the set by tab (place among the set's total-eligible runs, totals, Report, PR workflow, withdrawn flagged), and a link to the board filtered to the set; 404 for a key that is not a PR set key |
| `GET /b/xflare_leaderboard/compare?runs=a,b[,c,d]` | 2–4 distinct run ids; or (A9) `?prsets=k1,k2[,k3,k4]`, 2–4 distinct PR set keys, with the board's `sf`, `codec` and filters (the default applies) | page: for `prsets`, each set's best total-eligible run on that tab under that filter (the PR-set list's `best`), its set heading the column; Basis and Report rows; per-query deltas vs the first run, parameter diff (including `application_mode`, A7's per-map "N keys" rows and one row per setting that differs between the runs that recorded the map (`<map> (<engine>) · <key>`, at most 100 per map then "N more — see raw JSON"), and four Spark settings rows: `spark_conf (state)`, `spark_conf.source`, and `spark_conf (settings)` / `spark_conf_readback (settings)`, each map on one line as "not recorded" (absent), "{}" (empty), "not an object" (stored before A5 validated it) or its sorted `k=v` pairs joined by "; ", cut like any scalar; a settings row differs when the full maps differ); 400 (`compare takes ?runs= or ?prsets=, not both`, `compare takes 2 to 4 distinct PR set keys: ?prsets=k1,k2[,k3,k4]`) / 404 (`no total-eligible run of PR set <key> on SF-<tab> under this filter`) |

A run summary is `{run_id, content_id, url, sf, finished_at, submitter, total_eligible, repeats, totals, config,
config_label, build, device_ids, device_count, host, badges, ineligible_reasons, withdrawn, withdrawal, basis,
report_url}`. It is a display view: every uploaded string in it (`withdrawal.actor`/`reason` included) is cut to 512
characters plus "…", and `device_ids` holds at most 100 ids (`device_count` is the full count). A9: `basis` is
`{kind: "pr", prset, label, prs: [{repo, number, key, url, label}]}`, `{kind: "main", repo, latest_pr, url, label}`
or `null` (unknown), `report_url` the run's Report or `null`; neither is ever cut (the validator bounds them: 16 PRs,
2048 characters), as a cut URL would lead elsewhere. `ineligible_reasons` is
`{"common": [...]}`: the run's common reasons (§4), why no query of it can rank — for a shared run (A6) it starts
with `shared application: engine order affects timings`; empty for a run whose queries can rank. Each reason is cut
like any scalar and the list stops at 50 plus `... N more`; the per-query and Total reasons are in the detail API's
`derived.ineligible_reasons`, uncut. The detail API's `withdrawal`, `derived`, `meta`,
`manifest` and the raw download are never cut. A `run_id` that is not a lowercase uuid4 is refused before any
lookup: JSON 404 `unknown run_id` on the API, HTML 404 on the run page, 400 on compare.

Filters: `facets` lists at most 200 options per filter as `{token, label}`, with `facets_truncated` set when any list
was capped; the label is the value cut like any scalar, the token is `h:<16 hex>` (the first 16 hex digits of the
value's sha256); the `spark_conf` options are labelled by what they select ("default Spark settings only",
"custom (--spark-conf)", "unknown (spark_conf unreadable)"), and the `basis` options (A9) read "main", "PR-based
(all)", each PR set ("PR set gluten#88 + #1561"), then "unknown (no basis)". Each facet filter takes two parameters
on `/board` and `/runs`: `<key>` is a literal compared exactly with the full stored value, and `<key>_t` a token,
matching a run whose value hashes to it. They never stand in for each other; given both, a run must match both, and an
unknown token matches nothing. The page's options send `<key>_t`. The board echoes the applied literals in `filter`
(as sent) and, in `filter_tokens`, the token of every applied facet filter, so a shared URL with a full value selects
the offered option. On `/board` an absent filter takes the configured default (`executors`, `executor_cores`); sent
empty (`executors=`) it means "any", which is how the page lifts the default. Config filters compare as text with a
`null` value spelled `-`; `from`/`to` that are not YYYY-MM-DD narrow nothing.

Pages show previews of uploaded data; the raw JSON always has everything. Every uploaded string is cut to 512
characters plus "…", except the run-level long texts (note, toml input and rendered text, gluten build command), which
stop at 65,536 characters. Lists (argv, overrides, devices, overlapping bookings, samples, repeats, each repeat's
applications, ineligible reasons, the run page's A5 Spark settings maps, compare override rows) stop at 100 items with
"N more — see raw JSON" (A7's settings-in-effect maps on the run page stop at 500 keys), and the repeat × query matrix shows the first 10 repeats. A duration, or a compare Δ%, past
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
spells out every config field; the Mode column shows the application mode (always `per-engine` since A6). The Engines column shows the run's engines (A7: `velox, xflare` or `xflare`). Badge
colours come from one fixed map (`display.BADGE_CLASS`, sent to the board script in the page's data block):
`reproducible` green; `partial`, `dev bundle`, `spark conf` and `shared application` amber; `booking overlap` red;
`overlap unknown` and `offload:*` dim. The run and compare pages colour badges the same way.

The pages share the XFLARE Developer Platform's look: `static/style.css` carries its design tokens, light or dark as
the browser's colour scheme says, and the charts read their colours from the same tokens. `config.yaml`'s `color`
marks each page's heading.

Every `/static/` URL the pages write carries `?v=` and the first 12 hex digits of the file's sha256, taken when the
service starts (a missing file stops the start), so a reload never reuses a cached copy of an older file. The board
page's data block names its script by that hash (`script`), and every board API answer (A9: the hero history and the
PR-set list too) names the service's board script in `X-Leaderboard-Script`. When the answer names another script, or
is a 2xx without the header (a service from before this check, after going back a version), the board page leaves the
hero and board as they were and asks for a reload instead of rendering an answer its script may misread; the tabs and
the URL already follow the click, so the reload opens what was clicked. Static files are served from disk as they are,
so a page loaded between the auto-updater's fast-forward and its restart can get the new script from the old service;
the first answer after the restart then asks for a reload. A page from before this check cannot tell: reload it once.

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
- Besides `runs` and A8's `targets` / `target_events`, A9 (`user_version` 3; §4 A9 for the upgrade) keeps
  `run_attribution` (a run's basis and Report, one revision per run), `prset_attribution` (a PR set's Report),
  `attribution_events` (every change of either: field, revision, before, after, the event a restore copied, actor,
  time) and `pr_checks` (each PR's latest body check). They hold what was sent, nothing derived.
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
target must be empty or absent. To restore the live data: stop the auto-update timer and let a running tick end
(Deploy runbook: Automatic updates), stop the service, restore into a new dir, move the old dir aside, move the new
one into place, start the service, start the timer.

The service and backup units are in `systemd/`: `xflare-leaderboard.service` (the service) and, for the optional
backup, `xflare-leaderboard-backup.{service,timer}` (daily 03:30 host time + up to 10 min jitter, `Persistent=true`,
oneshot).
All run as `sw-devops` with `/usr/bin/python3` from `/home/sw-devops/xflare-leaderboard`.

## Deploy runbook (server 60, done by a person)

The service runs on its own port next to anything else on the host and shares nothing with other services
(Benchmark Hub on :8892 is unrelated and unchanged). Every git command runs as `sw-devops`, the checkout's owner
(git refuses a repository owned by someone else).

Server 60 as checked at the first deploy (2026-10-20): Ubuntu 22.04.5; `/usr/bin/python3` 3.10.12 with Flask 2.3.3,
Werkzeug 3.1.3, PyYAML 6.0.1 and SQLite 3.37.2; port 8894 free; ufw inactive and `INPUT ACCEPT` (no firewall
change needed); `/home` is local ext4. Re-run step 0 before a fresh install on another host; if port 8894 or the
checkout path must differ there, change `server.port` in `config.yaml` or the paths in `systemd/` before step 4, and
leave that host's automatic updates off (step 7): the updater stops on a checkout with local edits or commits, and a
change committed to main reaches every host that follows main (`test_config_yaml` also pins the port).

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
7. Turn on automatic updates (below).

**Automatic updates.** `xflare-leaderboard-auto-update.timer` runs
[`systemd/xflare-leaderboard-auto-update.sh`](systemd/xflare-leaderboard-auto-update.sh) as `sw-devops` 2 min after
boot and then every 10 min (+ up to 60 s jitter). A tick:
1. Leaves a checkout off branch `main` alone (exit 0): that is a version pinned by hand (see Upgrade). Stops (exit 1)
   on a tracked edit or an untracked file in the checkout (files `.gitignore` covers, such as `*.pyc`, are not
   checked).
2. Fetches `origin/main`. When the checkout is on it and it is recorded as deployed (the ref
   `refs/auto-update/deployed` in the checkout), prints `already on <sha>` and exits. Stops (exit 1) when the checkout
   holds a commit `origin/main` lacks.
3. Runs `leaderboard/test_leaderboard.py` with `/usr/bin/python3` on a `git archive` of `origin/main` in a temp dir,
   with `XFLARE_LEADERBOARD_DATA` pointed at a scratch dir and the page-script tests skipped: server 60's node is 12,
   and the page script runs in browsers, not on the server. They must be run by hand before every merge (Run
   locally). About 80 s on server 60. A failure prints the log's tail and leaves the checkout and the
   service as they are; the next tick tries again.
4. Checks the checkout again (a pin made meanwhile holds), fast-forwards it, runs
   `sudo -n /usr/bin/systemctl restart xflare-leaderboard`, and gives
   `http://127.0.0.1:<server.port>/api/leaderboard/board` 30 s to answer 2xx. Only then is the commit recorded as
   deployed. A failed restart or no answer fails the tick, and the next tick restarts again; nothing is rolled back.
   That probe stands in for step 5: the board API is the route that reads the live data, and the self-check ran the
   others.

The unit gives a tick 15 min, hides the data dir from it (`InaccessiblePaths=`) and gives it its own `/tmp`. That keeps
a test's accidental opens off the live data; it is no boundary against hostile code, which main is trusted not to hold:
a deployed commit runs as `sw-devops` anyway. A settings change is a commit to `config.yaml` like any other. A restart
drops requests in flight (the client sees a connection error and resends the same envelope). The updater does not
install systemd units, Python packages or node, and does not back up the data: a commit that changes a unit file or
`requirements.txt` still needs those steps by hand (a change to the script itself takes effect on the next tick).

Install, once. On an existing install, first pull the checkout (Upgrade) so it has these files. `sw-devops` must be able
to run that restart through `sudo -n`; on server 60 it already has passwordless sudo. On a host where it has not, add a
rule for that one command first:
```
t=$(mktemp) && printf '%s\n' 'sw-devops ALL=(root) NOPASSWD: /usr/bin/systemctl restart xflare-leaderboard' >"$t" &&
  sudo visudo -cf "$t" && sudo install -m 440 -o root -g root "$t" /etc/sudoers.d/xflare-leaderboard-auto-update; rm -f "$t"
```
Then:
```
sudo -u sw-devops sudo -n -l /usr/bin/systemctl restart xflare-leaderboard   # prints the command back = allowed
sudo cp /home/sw-devops/xflare-leaderboard/systemd/xflare-leaderboard-auto-update.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now xflare-leaderboard-auto-update.timer
journalctl -u xflare-leaderboard-auto-update -f     # first tick: "deploying …", then "restarted on <sha>"; Ctrl-C
systemctl list-timers xflare-leaderboard-auto-update.timer
```
Nothing is recorded as deployed yet, so the first tick, which starts right away, runs the whole path on the current
main, one restart included. Between ticks the updater's service is `inactive`, or `failed` after a failed tick until
a later one succeeds; the journal says why.

Before a change by hand (a pin, a restore, a rollback), stop the timer and let a running tick end, and start the timer
again afterwards:
```
sudo systemctl stop xflare-leaderboard-auto-update.timer
systemctl is-active xflare-leaderboard-auto-update.service     # wait while "activating" (about 2 min, 15 at most)
sudo systemctl start xflare-leaderboard-auto-update.timer      # when done
```

**Upgrade.** With the timer on, a merge to main reaches the server within about 15 min and this is not needed (an
upgrade by hand then gets checked and restarted once more by the next tick). Without it: record the running SHA, pull,
restart, and run step 5's status loop and dry run (its `/runs` and raw-file counts apply to a fresh install only):
```
sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard rev-parse HEAD      # keep it: the version to go back to
sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard pull --ff-only
sudo systemctl restart xflare-leaderboard
```
To go back a version: `sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard checkout <recorded SHA>`,
`sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard update-ref -d refs/auto-update/deployed` (the
auto-updater's deploy record; harmless without it) and `sudo systemctl restart xflare-leaderboard`. That leaves the
checkout on a detached HEAD, so the next upgrade runs
`sudo -u sw-devops git -C /home/sw-devops/xflare-leaderboard checkout main` after recording the SHA and before the pull
(record first: the SHA to keep is the one running, not main's). With the timer on, stop it first (above); the detached
checkout then pins the version, ticks leave it alone, and the timer can run again. To unpin, `checkout main`: with the
record gone, the next tick restarts the service on main.

**Rollback.** `sudo systemctl disable --now xflare-leaderboard-auto-update.timer`, wait until
`xflare-leaderboard-auto-update.service` is no longer `activating` (above), then
`sudo systemctl disable --now xflare-leaderboard` (and `xflare-leaderboard-backup.timer` if the backup was turned on).
Nothing else on the host depends on the service. Leave `/home/sw-devops/data/xflare_leaderboard` in place so a re-deploy
picks it up. To remove it entirely, also delete `/etc/systemd/system/xflare-leaderboard.service`,
`xflare-leaderboard-auto-update.{service,timer}` and any `xflare-leaderboard-backup.{service,timer}` there (then
`sudo systemctl daemon-reload`), `/etc/sudoers.d/xflare-leaderboard-auto-update` if it was added, the checkout, the data
dir, and the deploy key (GitHub Deploy keys, the `Host github-xflare-leaderboard` block, the key files).

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
