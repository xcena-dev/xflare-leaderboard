// xflare TPC-H leaderboard pages (board, run detail, compare).
//
// Every string that came from a submission is hostile: the DOM is built with
// createElement + textContent only, and ids in URLs go through
// encodeURIComponent. Nothing here assigns HTML from data.
'use strict';

(function () {
  const QUERIES = Array.from({ length: 22 }, (_, i) => i + 1);

  // Chart colours come from style.css's tokens, read once per chart, so a chart follows the page's light or dark
  // theme as drawn; the fallbacks (the light theme's) cover a page whose stylesheet did not load.
  function chartColors() {
    const css = typeof getComputedStyle === 'function' ? getComputedStyle(document.documentElement) : null;
    const token = (name, fallback) => (css && css.getPropertyValue(name).trim()) || fallback;
    const xflare = token('--chart-xflare', '#3b5bdb');
    return {
      xflare, velox: token('--chart-velox', '#aab3c0'),
      series: [xflare, token('--chart-2', '#7048e8'), token('--chart-3', '#b7791f'), token('--chart-4', '#1f8a4c')],
      tick: token('--text-3', '#768194'), grid: token('--border', '#e1e5eb'), legend: token('--text-2', '#4a5566'),
      font: token('--font', 'sans-serif'),
    };
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  // The server keeps durations as exact integers; one past float range parses
  // as Infinity here, which the pages call "out of range" (as the server does).
  function fmtMs(ms) {
    if (ms === null || ms === undefined) return '—';
    const n = Number(ms);
    if (!isFinite(n)) return 'out of range';
    return n >= 1000 ? `${(n / 1000).toFixed(2)} s` : `${n.toFixed(0)} ms`;
  }
  function fmtX(v) {
    if (v === null || v === undefined) return '—';
    const n = Number(v);
    return isFinite(n) ? `${n.toFixed(2)}×` : 'out of range';
  }
  // A label cut like the server cuts uploaded scalars (the page's data block
  // carries the limit).
  function cutLabel(s, limit) {
    const t = String(s);
    return t.length > limit ? `${t.slice(0, limit)}…` : t;
  }
  // Summaries carry at most 100 device ids plus the full count.
  function fmtDevices(r) {
    const ids = r.device_ids || [];
    const rest = (r.device_count || 0) - ids.length;
    return (ids.join(', ') || '—') + (rest > 0 ? ` (+${rest} more)` : '');
  }
  function fmtTs(ts) { return ts ? String(ts).replace('T', ' ').replace('Z', '').slice(0, 16) : '—'; }
  // The config label abbreviates; its tooltip spells every field out.
  function configText(c) {
    return c ? Object.entries(c).map(([k, v]) => `${k}: ${v ?? '—'}`).join('\n') : '';
  }
  // The board's config cell: resources only (toml and bundle live in the tooltip and the filters, the
  // application mode in its own column), plus the config label's `+conf` marker for custom Spark settings.
  function compactConfig(c) {
    const parts = [`${c.executors}×${c.executor_cores}`];
    if (c.offheap != null) parts.push(c.offheap);
    if (c.shuffle_partitions != null) parts.push(`sp${c.shuffle_partitions}`);
    if (c.devices != null) parts.push(`d${c.devices}`);
    return cutLabel(parts.join(' '), 24) + (c.spark_conf === 'custom' ? ' +conf' : '');
  }

  // The service is plain http, where navigator.clipboard does not exist; a
  // selected textarea + execCommand still copies there. Throws when refused.
  async function copyText(text) {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const ta = el('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    if (!ok) throw new Error('copy refused');
  }
  function runHref(board, runId) { return `${board}/run/${encodeURIComponent(runId)}`; }

  // Badge text is uploaded-derived; the class comes from the server's fixed map (display.BADGE_CLASS, in the
  // board page's data block), looked up as an own property only.
  function badgeList(badges, classes) {
    const wrap = el('span', 'lb-badges');
    for (const b of badges || []) {
      const own = Object.prototype.hasOwnProperty.call(classes || {}, b);
      const cls = own ? classes[b] : (String(b).startsWith('offload:') ? 'b-dim' : '');
      wrap.appendChild(el('span', `lb-badge ${cls}`.trim(), b));
    }
    return wrap;
  }

  // Grouped bars per query, linear or log as the page's toggle stands now.
  function perQueryChart(canvas, datasets, logBox, colors) {
    if (typeof Chart === 'undefined') return null;
    const font = { family: colors.font, size: 12 };
    const { tick, grid } = colors;
    const chart = new Chart(canvas, {
      type: 'bar',
      data: { labels: QUERIES.map(q => `Q${q}`), datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        scales: {
          x: { ticks: { color: tick, font }, grid: { display: false }, border: { color: grid } },
          y: { type: logBox && logBox.checked ? 'logarithmic' : 'linear',
               title: { display: true, text: 'ms', color: tick, font },
               ticks: { color: tick, font }, grid: { color: grid }, border: { display: false } },
        },
        plugins: {
          legend: { align: 'end',
                    labels: { color: colors.legend, font, boxWidth: 10, boxHeight: 10,
                              useBorderRadius: true, borderRadius: 2 } },
        },
      },
    });
    return chart;
  }

  // The log toggle (Q1-style scans dwarf the rest), wired once per page; it
  // switches whichever chart is current, so a redrawn hero needs no new listener.
  function bindLogToggle(logBox, current) {
    logBox.addEventListener('change', () => {
      const chart = current();
      if (!chart) return;
      chart.options.scales.y.type = logBox.checked ? 'logarithmic' : 'linear';
      chart.update();
    });
  }

  function bar(label, data, color) {
    return { label, data, backgroundColor: color, borderWidth: 0, borderRadius: 3, maxBarThickness: 18 };
  }

  // Resolves to {ok, status, data}; a lost connection is status 0, since whether the edit was stored is unknown.
  async function postJSON(url, body) {
    let resp;
    try {
      resp = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
    } catch (e) {
      return { ok: false, status: 0, data: { errors: ['연결이 끊겼습니다 — 저장됐는지 알 수 없으니 새로고침해서 확인하세요'] } };
    }
    let data = null;
    try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
    return { ok: resp.ok, status: resp.status, data };
  }

  const GOAL_MAX = 1000;   // the server's bound (web.GOAL_MAX)

  function targetUrl(sf, codec) {
    return `/api/leaderboard/targets/${encodeURIComponent(sf)}/${encodeURIComponent(codec)}`;
  }

  // A8: pins (or clears, runId null) a tab's target and goal speedup. The server refuses a stale revision (409),
  // so a change made meanwhile is never overwritten. Resolves to the response, or null when the user cancels or
  // `stillValid` says the page moved on while the prompts were open.
  async function setTarget(sf, codec, revision, runId, askGoal, goal, stillValid) {
    const actor = prompt('Target 을 바꾸는 사람 (email):');
    if (!actor) return null;
    if (askGoal) {
      const text = prompt(`목표 speedup (target 의 velox / xflare, 0 초과 ${GOAL_MAX} 이하, 예: 2.0). 비워 두면 목표 없음:`,
                          goal ?? '');
      if (text === null) return null;
      goal = text.trim() === '' ? null : Number(text);
      if (goal !== null && !(Number.isFinite(goal) && goal > 0 && goal <= GOAL_MAX)) {
        alert(`목표 speedup 은 0 보다 크고 ${GOAL_MAX} 이하인 숫자여야 합니다.`);
        return null;
      }
    }
    if (stillValid && !stillValid()) return null;
    return postJSON(targetUrl(sf, codec), { actor, expected_revision: revision, run_id: runId, goal: runId ? goal : null });
  }

  function failed(what, r) {
    alert(`${what} 실패 (HTTP ${r.status}): ${((r.data && r.data.errors) || []).join('; ')}`);
  }

  // ---- board page ------------------------------------------------------------
  function initBoard() {
    const cfg = JSON.parse(document.getElementById('lb-page-data').textContent);
    const params = new URLSearchParams(location.search);
    // Facet filters travel as a literal `<key>` (a shared URL) or a token
    // `<key>_t` (what the options send); from/to are literal dates.
    const FACET_KEYS = [...document.querySelectorAll('.lb-filter-slot')].map(s => s.dataset.key);
    const FILTER_KEYS = FACET_KEYS.flatMap(k => [k, `${k}_t`]).concat(['from', 'to']);
    const state = {
      // Empty until the first answer: the server picks the first tab and
      // sends the tab list ({sf, codec, label}: the pinned config.yaml tabs, then the stored runs' own).
      sf: params.get('sf') || '',
      codec: params.get('codec') || '',
      tabs: [],
      metric: params.get('metric') || 'total',
      // A8: a shared link may name its own target (a run id, or none); it is never stored.
      target: params.get('target') || '',
      top: params.get('top') || '10',
      // Only keys the user (or the shared URL) set. An absent key takes the
      // server default; an empty one means "any" and lifts that default.
      filter: {},
    };
    for (const k of FILTER_KEYS) if (params.has(k)) state.filter[k] = params.get(k);
    const selected = new Set();
    let chart = null;
    let target = null;      // the target the last answer compared with (null: the same-run velox)
    let targetPin = null;   // the tab's stored target, for its history and changes
    // A8: each tab change and each history fetch bumps historyGen, so an answer or a button from an older one never
    // acts on the current tab; historyRevision is the revision the shown list was fetched at.
    const targetHistory = document.getElementById('hero-target-history');
    let historyGen = 0;
    let historyRevision = null;
    const heroLog = document.getElementById('hero-log');

    const status = document.getElementById('lb-status');
    function showStatus(text) { status.textContent = text || ''; status.hidden = !text; }

    function tabButton(host, label, active, onClick) {
      const b = el('button', active ? 'lb-tab active' : 'lb-tab', label);
      b.type = 'button';
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
      b.addEventListener('click', onClick);
      host.appendChild(b);
    }

    function renderTabs() {
      const sfHost = document.getElementById('lb-sf-tabs');
      sfHost.replaceChildren();
      for (const t of state.tabs) {
        tabButton(sfHost, `SF-${t.label}`, t.sf === state.sf && t.codec === state.codec,
                  () => { state.sf = t.sf; state.codec = t.codec; state.target = ''; load(); });
      }
      const mHost = document.getElementById('lb-metric-tabs');
      mHost.replaceChildren();
      for (const m of ['total', ...QUERIES.map(q => `q${q}`)]) {
        tabButton(mHost, m === 'total' ? 'Total' : m.toUpperCase(), m === state.metric,
                  () => { state.metric = m; load(); });
      }
    }

    function query() {
      const q = new URLSearchParams({ metric: state.metric, top: state.top });
      if (state.sf) q.set('sf', state.sf);
      if (state.codec) q.set('codec', state.codec);
      if (state.target) q.set('target', state.target);
      for (const [k, v] of Object.entries(state.filter)) q.set(k, v);
      return q;
    }

    function renderFilters(data) {
      for (const slot of document.querySelectorAll('.lb-filter-slot')) {
        const key = slot.dataset.key;
        // Options are {token, label}. The applied filter, literal or token,
        // comes back as a token too, so the matching option is selected; only
        // a value the tab does not offer gets an option of its own.
        const current = (data.filter_tokens || {})[key] || '';
        const options = (data.facets[key] || []).slice();
        if (current && !options.some(o => o.token === current)) {
          options.unshift({ token: current, label: cutLabel(data.filter[key] ?? current, cfg.scalar_chars) });
        }
        const label = el('label', null, `${slot.dataset.label}: `);
        const sel = el('select');
        sel.appendChild(el('option', null, '(any)')).value = '';
        for (const o of options) sel.appendChild(el('option', null, o.label)).value = o.token;
        sel.value = current;
        sel.addEventListener('change', () => {
          // A choice replaces the literal (sent empty, so a default lifts)
          // with the token; "(any)" clears both.
          state.filter[key] = '';
          if (sel.value) state.filter[`${key}_t`] = sel.value; else delete state.filter[`${key}_t`];
          load();
        });
        label.appendChild(sel);
        slot.replaceChildren(label);
      }
      document.getElementById('lb-facets-note').hidden = !data.facets_truncated;
      document.getElementById('lb-from').value = data.filter.from || '';
      document.getElementById('lb-to').value = data.filter.to || '';
      document.getElementById('lb-top').value = String(data.top);
    }

    function kv(host, key, value) {
      host.appendChild(el('dt', null, key));
      const dd = el('dd');
      if (value instanceof Node) dd.appendChild(value); else dd.textContent = value ?? '—';
      host.appendChild(dd);
      return dd;
    }

    function id8(runId) { return runId ? runId.slice(0, 8) : '없음'; }

    // A8: the target usable for comparison (null: compare with the same-run velox).
    function liveTarget() { return target && !target.problem ? target : null; }

    // A8: the target line and its history, shown whatever the hero (a tab may have none yet).
    function renderTarget() {
      const line = document.getElementById('hero-target');
      line.replaceChildren();
      line.className = 'lb-hero-target';
      line.hidden = targetHistory.hidden = !targetPin;
      if (!targetPin) return;
      if (!target) {
        line.textContent = targetPin.run_id
          ? '이 보기는 target 없이 같은 run 의 velox 와 비교합니다.'
          : '이 탭에 target 이 없습니다 — 같은 run 의 velox 와 비교합니다. run 페이지에서 target 으로 지정할 수 있습니다.';
        return;
      }
      if (target.problem) {
        line.className = 'lb-hero-target lb-banner lb-banner-warn';
        line.textContent = `target ${id8(target.run_id)} 을 쓸 수 없습니다 (${target.problem}) — 같은 run 의 velox 와 비교합니다.`;
        return;
      }
      line.appendChild(el('span', null, 'Target: '));
      const a = el('a', 'lb-run', `${id8(target.run_id)} · ${fmtTs(target.finished_at)}`);
      a.href = runHref(cfg.board, target.run_id);
      line.appendChild(a);
      line.appendChild(el('span', null, ` · ${target.config_label}`));
      line.appendChild(el('span', 'lb-dim', target.source === 'link' ? ' · 이 링크에서만 (저장된 target 아님)'
        : ` · ${targetPin.updated_by ?? '—'} 가 ${fmtTs(targetPin.updated_at)} 에 지정`));
    }

    // notice: the server's line for a fallback hero (the filter matched no
    // Total-eligible run, so this is the tab's fastest in any configuration).
    function renderHero(hero, notice) {
      const noticeEl = document.getElementById('hero-notice');
      noticeEl.textContent = notice || '';
      noticeEl.hidden = !(hero && notice);
      const configEl = document.getElementById('hero-config');
      configEl.textContent = hero && notice ? hero.config_label : '';
      configEl.hidden = !(hero && notice);
      document.getElementById('hero-empty').hidden = !!hero;
      document.getElementById('hero-body').hidden = !hero;
      const link = document.getElementById('hero-link');
      link.hidden = !hero;
      if (chart) { chart.destroy(); chart = null; }
      if (!hero) return;
      link.href = runHref(cfg.board, hero.run_id);
      document.getElementById('hero-xflare').textContent = fmtMs(hero.totals.xflare_ms);
      const t = liveTarget();
      document.getElementById('hero-velox-label').textContent = t ? 'Target velox total' : 'Velox total (same run)';
      document.getElementById('hero-velox').textContent = fmtMs(t ? t.velox_ms : hero.totals.velox_ms);
      document.getElementById('hero-speedup-label').textContent = t ? 'Speedup vs target' : 'Speedup (velox / xflare)';
      document.getElementById('hero-speedup').textContent = fmtX(t ? hero.vs_target : hero.totals.speedup);
      const goalEl = document.getElementById('hero-goal');
      goalEl.hidden = !(t && t.goal);
      if (t && t.goal) {
        const reached = hero.vs_target != null && hero.vs_target >= t.goal;
        goalEl.className = reached ? 'lb-stat-goal lb-goal-met' : 'lb-stat-goal';
        goalEl.textContent = hero.vs_target == null ? `목표 ${fmtX(t.goal)}`
          : `목표 ${fmtX(t.goal)} · ${reached ? '달성' : `${Math.round(100 * hero.vs_target / t.goal)}%`}`;
      }
      document.getElementById('hero-chart-note').textContent = t
        ? 'per-query median (ms): hero 의 xflare vs target 의 velox (흐린 막대: hero 와 같은 run 의 velox)'
        : 'per-query median (ms), 같은 run 의 xflare vs velox';
      const meta = document.getElementById('hero-meta');
      meta.replaceChildren();
      kv(meta, 'Config', hero.config_label).title = configText(hero.config);
      kv(meta, 'Bundle', `${hero.config.bundle ?? '—'} (${hero.build.bundle_kind ?? '—'})`);
      kv(meta, 'Revision', hero.build.revision);
      kv(meta, 'Client', hero.build.client_revision);
      kv(meta, 'Artifact', hero.build.artifact_id);
      kv(meta, 'Submitter', hero.submitter);
      kv(meta, 'Finished', fmtTs(hero.finished_at));
      kv(meta, 'Devices', fmtDevices(hero));
      kv(meta, 'Badges', badgeList(hero.badges, cfg.badge_class));
      const colors = chartColors();
      const sameRun = hero.per_query.map(p => p.velox_ms);
      chart = perQueryChart(document.getElementById('hero-chart'), t
        ? [bar('xflare', hero.per_query.map(p => p.xflare_ms), colors.xflare),
           bar('target velox', t.per_query, colors.velox),
           bar('velox (same run)', sameRun, colors.grid)]
        : [bar('xflare', hero.per_query.map(p => p.xflare_ms), colors.xflare),
           bar('velox', sameRun, colors.velox)], heroLog, colors);
    }

    // A6: shared-application runs under the filter, stored and listed but never ranked, named under the board
    // (at most `top` of them, linked to their pages) so an empty board does not hide them.
    function renderShared(data) {
      const runs = data.shared_runs || [];
      const count = data.shared_count || 0;
      const box = document.getElementById('lb-shared');
      const list = document.getElementById('lb-shared-list');
      list.replaceChildren();
      box.hidden = !count;
      if (!count) return;
      document.getElementById('lb-shared-note').textContent =
        `shared application run ${count}개는 이 필터에 해당하지만 순위에 오르지 않습니다 ` +
        '(engine order affects timings, A6) — run 페이지와 /api/leaderboard/runs 에는 남습니다' +
        (count > runs.length ? ` · 처음 ${runs.length}개:` : ':');
      for (const r of runs) {
        const li = el('li');
        const a = el('a', null, fmtTs(r.finished_at));
        a.href = runHref(cfg.board, r.run_id);
        a.title = `${r.run_id}\nfinished ${r.finished_at}`;
        li.appendChild(a);
        li.appendChild(el('span', 'lb-dim', ` · ${r.config_label ?? '—'}`));
        list.appendChild(li);
      }
    }

    function renderBoard(data) {
      const tbody = document.querySelector('#lb-board tbody');
      tbody.replaceChildren();
      renderShared(data);
      const metricName = data.metric === 'total' ? 'Total' : data.metric.toUpperCase();
      document.getElementById('lb-board-note').textContent =
        `${metricName}: ${data.eligible_count} eligible run(s) under the filter · top ${data.top} · ` +
        (data.metric === 'total' ? 'xflare median 합' : 'xflare median') +
        ' 오름차순, 동률은 repeats 많은 순 → 먼저 끝난 순';
      const t = liveTarget();
      document.getElementById('lb-col-speedup').textContent = t ? 'vs target' : 'Speedup';
      if (!data.rows.length) {
        const tr = tbody.insertRow();
        const td = tr.insertCell();
        td.colSpan = 14;
        td.className = 'lb-empty';
        td.textContent = '이 필터에 순위에 오를 run 이 없습니다.' +
          (data.shared_count ? ' shared application run 은 순위에 오르지 않습니다 — 아래 목록을 보세요.' : '');
        return;
      }
      for (const r of data.rows) {
        const tr = tbody.insertRow();
        const rank = tr.insertCell();
        rank.className = 'num';
        rank.textContent = r.rank;
        // A run is told apart by when it finished; the id stays in the link and tooltip.
        const a = el('a', 'lb-run', fmtTs(r.finished_at));
        a.href = runHref(cfg.board, r.run_id);
        a.title = `${r.run_id}\nfinished ${r.finished_at}`;
        tr.insertCell().appendChild(a);
        for (const [text, cls, title] of [[fmtMs(r.value.xflare_ms), 'num'], [fmtMs(r.value.velox_ms), 'num'],
                                          t ? [fmtX(r.value.vs_target),
                                               t.goal && r.value.vs_target >= t.goal ? 'num lb-goal-met' : 'num',
                                               `같은 run 의 velox 기준 ${fmtX(r.value.speedup)}`]
                                            : [fmtX(r.value.speedup), 'num'], [`x${r.repeats}`, 'num'],
                                          [compactConfig(r.config), null, configText(r.config)],
                                          [r.config.application_mode ?? '—', null],
                                          [(r.config.engines ?? '—').replace(',', ', '), null],
                                          [fmtDevices(r), null],
                                          [r.submitter ?? '—', null]]) {
          const td = tr.insertCell();
          td.textContent = text;
          if (cls) td.className = cls;
          if (title) td.title = title;
        }
        tr.insertCell().appendChild(badgeList(r.badges, cfg.badge_class));
        reproCell(tr.insertCell(), r);
        const box = el('input');
        box.type = 'checkbox';
        box.checked = selected.has(r.run_id);
        box.setAttribute('aria-label', `compare ${r.run_id}`);
        box.addEventListener('change', () => {
          if (box.checked) selected.add(r.run_id); else selected.delete(r.run_id);
          updateCompare();
        });
        tr.insertCell().appendChild(box);
      }
    }

    // Clicking anywhere in the cell copies the run's reproduce script; a run
    // without one points at its page instead.
    function reproCell(td, r) {
      if (!r.repro) {
        const a = el('a', 'lb-dim', 'run page');
        a.href = runHref(cfg.board, r.run_id);
        td.appendChild(a);
        return;
      }
      const btn = el('button', 'lb-btn', 'copy');
      btn.type = 'button';
      td.className = 'lb-copy';
      td.title = r.repro;
      td.appendChild(btn);
      td.addEventListener('click', async () => {
        try {
          await copyText(r.repro);
          btn.textContent = 'copied';
        } catch (e) {
          btn.textContent = 'copy failed — use the run page';
        }
      });
    }

    function updateCompare() {
      const btn = document.getElementById('lb-compare');
      btn.disabled = selected.size < 2 || selected.size > 4;
      btn.textContent = `선택한 run 비교 (${selected.size}/2–4)`;
    }

    // Loads overlap (a tab clicked while the last one is in flight); only the
    // newest one may touch the page, whichever answer arrives last.
    let generation = 0;
    async function load() {
      const mine = ++generation;
      // Another tab: the shown history (and its buttons) belongs to the old one.
      if (targetPin && (state.sf !== targetPin.sf || state.codec !== targetPin.codec)) forgetHistory();
      renderTabs();
      const q = query();
      history.replaceState(null, '', `${location.pathname}?${q}`);
      let resp, data;
      try {
        resp = await fetch(`/api/leaderboard/board?${q}`);
      } catch (e) {
        if (mine === generation) showStatus('board 를 불러오지 못했습니다.');
        return;
      }
      if (mine !== generation) return;
      // An answer from a service whose script is not this one (a page left open across a deploy, or after going back
      // a version) may not fit this script: keep the hero and board as they are and ask for a reload. Read before the
      // body, which may not be JSON. Every answer since this check names its script; a 2xx without one is from an
      // older service.
      const served = resp.headers.get('X-Leaderboard-Script');
      if (served ? served !== cfg.script : resp.ok) {
        showStatus('서버가 다른 버전으로 바뀌었습니다. 페이지를 새로고침하세요.');
        return;
      }
      try {
        data = await resp.json();
      } catch (e) {
        if (mine === generation) showStatus('board 를 불러오지 못했습니다.');
        return;
      }
      if (mine !== generation) return;
      if (!resp.ok) {
        // A malformed ?target= from a shared link: drop it and show the tab's stored target.
        if (resp.status === 400 && state.target && /^target:/.test((data.errors || [])[0] || '')) {
          state.target = '';
          load();
          return;
        }
        // An unknown ?sf= / ?codec= from a shared link: forget them so the tabs come back.
        if (resp.status === 400 && state.sf && !state.tabs.length) { state.sf = ''; state.codec = ''; load(); return; }
        showStatus((data.errors || []).join('; ') || `HTTP ${resp.status}`);
        target = targetPin = null;
        forgetHistory();
        renderTarget();
        renderHero(null);
        renderBoard({ metric: state.metric, eligible_count: 0, top: state.top, rows: [] });
        return;
      }
      showStatus('');
      state.sf = data.sf;
      state.codec = data.codec;
      state.tabs = data.tabs;
      renderTabs();
      renderFilters(data);
      if (!targetPin || targetPin.sf !== data.sf || targetPin.codec !== data.codec) forgetHistory();
      target = data.target || null;
      targetPin = data.target_pin || null;
      renderTarget();
      renderHero(data.hero, data.hero_fallback ? data.hero_notice : null);
      if (targetHistory.open && historyRevision !== targetPin.revision) loadTargetHistory();
      renderBoard(data);
      document.getElementById('lb-updated').textContent = 'updated ' + new Date().toLocaleTimeString();
    }

    document.getElementById('lb-from').addEventListener('change', e => { state.filter.from = e.target.value; load(); });
    document.getElementById('lb-to').addEventListener('change', e => { state.filter.to = e.target.value; load(); });
    document.getElementById('lb-top').addEventListener('change', e => { state.top = e.target.value; load(); });
    document.getElementById('lb-reset').addEventListener('click', () => { state.filter = {}; load(); });
    document.getElementById('lb-refresh').addEventListener('click', load);
    document.getElementById('lb-compare').addEventListener('click', () => {
      const ids = [...selected].map(encodeURIComponent).join(',');
      location.href = `${cfg.board}/compare?runs=${ids}`;
    });
    // A8: the tab's latest target changes (at most 50), newest first; a listed earlier target can be pinned again.
    function forgetHistory() {
      historyGen++;
      historyRevision = null;
      document.getElementById('hero-target-events').replaceChildren();
    }
    async function loadTargetHistory() {
      const list = document.getElementById('hero-target-events');
      const pin = targetPin;
      if (!pin) return;
      const mine = ++historyGen;
      let data;
      try {
        const resp = await fetch(targetUrl(pin.sf, pin.codec));
        data = await resp.json();
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      } catch (e) {
        if (mine === historyGen) list.replaceChildren(el('li', 'lb-dim', '이력을 불러오지 못했습니다.'));
        return;
      }
      if (mine !== historyGen) return;
      historyRevision = data.revision;
      list.replaceChildren();
      if (!data.history.length) list.appendChild(el('li', 'lb-dim', '아직 지정된 적이 없습니다.'));
      for (const e of data.history) {
        const goal = e.after_goal ? ` · 목표 ${fmtX(e.after_goal)}` : '';
        const li = el('li', null, `${fmtTs(e.at)} · ${e.actor} · ${id8(e.before_run_id)} → ${id8(e.after_run_id)}${goal}`);
        if (e.after_run_id && (e.after_run_id !== data.run_id || e.after_goal !== data.goal)) {
          const b = el('button', 'lb-btn', '다시 지정');
          b.type = 'button';
          b.addEventListener('click', async () => {
            const r = await setTarget(data.sf, data.codec, data.revision, e.after_run_id, false, e.after_goal,
                                      () => mine === historyGen && state.sf === data.sf && state.codec === data.codec);
            if (!r) return;
            if (!r.ok) { failed('target 지정', r); return; }
            load();
          });
          li.appendChild(b);
        }
        list.appendChild(li);
      }
    }
    targetHistory.addEventListener('toggle', () => { if (targetHistory.open) loadTargetHistory(); });
    bindLogToggle(heroLog, () => chart);
    updateCompare();
    load();
  }

  // ---- run detail page --------------------------------------------------------
  function initRun() {
    const run = JSON.parse(document.getElementById('lb-run-data').textContent);
    const logBox = document.getElementById('run-log');
    const colors = chartColors();
    const chart = perQueryChart(document.getElementById('run-chart'), [
      bar('xflare', run.xflare, colors.xflare),
      bar('velox', run.velox, colors.velox),
    ], logBox, colors);
    bindLogToggle(logBox, () => chart);

    // No copy button when no command is offered; withdraw below
    // must still attach.
    const copy = document.getElementById('repro-copy');
    if (copy) copy.addEventListener('click', async () => {
      try {
        await copyText(document.getElementById('repro-cmd').textContent);
        copy.textContent = 'copied';
      } catch (e) {
        const range = document.createRange();
        range.selectNodeContents(document.getElementById('repro-cmd'));
        const sel = getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        copy.textContent = 'selected — Ctrl+C';
      }
    });

    for (const [id, runId, what] of [['lb-target-set', run.run_id, 'target 지정'], ['lb-target-clear', null, 'target 해제']]) {
      const b = document.getElementById(id);
      if (!b) continue;
      b.addEventListener('click', async () => {
        b.disabled = true;
        const r = await setTarget(b.dataset.sf, b.dataset.codec, Number(b.dataset.revision), runId, !!runId,
                                  b.dataset.goal ? Number(b.dataset.goal) : null);
        if (r && r.ok) { location.reload(); return; }
        b.disabled = false;
        if (r) failed(what, r);
      });
    }

    const withdraw = document.getElementById('lb-withdraw');
    if (!withdraw) return;
    withdraw.addEventListener('click', async () => {
      const actor = prompt('Withdraw 하는 사람 (email):');
      if (!actor) return;
      const reason = prompt('Withdraw 사유:');
      if (!reason) return;
      withdraw.disabled = true;
      const r = await postJSON(`/api/leaderboard/runs/${encodeURIComponent(run.run_id)}/withdraw`,
                               { actor, reason });
      if (r.ok) { location.reload(); return; }
      withdraw.disabled = false;
      failed('withdraw', r);
    });
  }

  // ---- compare page -----------------------------------------------------------
  function initCompare() {
    const data = JSON.parse(document.getElementById('lb-compare-data').textContent);
    const logBox = document.getElementById('compare-log');
    const colors = chartColors();
    const chart = perQueryChart(document.getElementById('compare-chart'),
                                data.series.map((s, i) => bar(data.names[i], s, colors.series[i % colors.series.length])),
                                logBox, colors);
    bindLogToggle(logBox, () => chart);
    const only = document.getElementById('params-only-diff');
    only.addEventListener('change', () => {
      document.getElementById('params-table').classList.toggle('only-diff', only.checked);
    });
  }

  const page = document.body.dataset.page;
  if (page === 'board') initBoard();
  else if (page === 'run') initRun();
  else if (page === 'compare') initCompare();
})();
