// xflare TPC-H leaderboard pages (board, run detail, compare).
//
// Every string that came from a submission is hostile: the DOM is built with
// createElement + textContent only, and ids in URLs go through
// encodeURIComponent. Nothing here assigns HTML from data.
'use strict';

(function () {
  const QUERIES = Array.from({ length: 22 }, (_, i) => i + 1);
  const XFLARE_COLOR = '#14B8A6';
  const VELOX_COLOR = '#94A3B8';
  const SERIES_COLORS = ['#14B8A6', '#F59E0B', '#818CF8', '#F472B6'];
  // Badge text is uploaded-derived; the class comes from this fixed map.
  const BADGE_CLASS = {
    reproducible: 'b-good', partial: 'b-warn', 'dev bundle': 'b-warn',
    'booking overlap': 'b-bad', 'overlap unknown': 'b-dim',
  };

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
  // application mode in its own column).
  function compactConfig(c) {
    const parts = [`${c.executors}×${c.executor_cores}`];
    if (c.offheap != null) parts.push(c.offheap);
    if (c.shuffle_partitions != null) parts.push(`sp${c.shuffle_partitions}`);
    if (c.devices != null) parts.push(`d${c.devices}`);
    return cutLabel(parts.join(' '), 24);
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

  function badgeList(badges) {
    const wrap = el('span', 'lb-badges');
    for (const b of badges || []) {
      const cls = BADGE_CLASS[b] || (String(b).startsWith('offload:') ? 'b-dim' : '');
      wrap.appendChild(el('span', `lb-badge ${cls}`.trim(), b));
    }
    return wrap;
  }

  // Grouped bars per query, linear or log as the page's toggle stands now.
  function perQueryChart(canvas, datasets, logBox) {
    if (typeof Chart === 'undefined') return null;
    const chart = new Chart(canvas, {
      type: 'bar',
      data: { labels: QUERIES.map(q => `Q${q}`), datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        scales: {
          x: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(148,163,184,0.08)' } },
          y: { type: logBox && logBox.checked ? 'logarithmic' : 'linear',
               title: { display: true, text: 'ms', color: '#94a3b8' },
               ticks: { color: '#94a3b8' }, grid: { color: 'rgba(148,163,184,0.12)' } },
        },
        plugins: { legend: { labels: { color: '#e2e8f0' } } },
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
    return { label, data, backgroundColor: color, borderColor: color, borderWidth: 1 };
  }

  async function postJSON(url, body) {
    const resp = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    let data = null;
    try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
    return { ok: resp.ok, status: resp.status, data };
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
      // sends the tab list (config.yaml's leaderboard.tabs, one source for page and API).
      sf: params.get('sf') || '',
      tabs: [],
      metric: params.get('metric') || 'total',
      top: params.get('top') || '10',
      // Only keys the user (or the shared URL) set. An absent key takes the
      // server default; an empty one means "any" and lifts that default.
      filter: {},
    };
    for (const k of FILTER_KEYS) if (params.has(k)) state.filter[k] = params.get(k);
    const selected = new Set();
    let chart = null;
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
      for (const sf of state.tabs) {
        tabButton(sfHost, `SF-${sf}`, sf === state.sf, () => { state.sf = sf; load(); });
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
      document.getElementById('hero-velox').textContent = fmtMs(hero.totals.velox_ms);
      document.getElementById('hero-speedup').textContent = fmtX(hero.totals.speedup);
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
      kv(meta, 'Badges', badgeList(hero.badges));
      chart = perQueryChart(document.getElementById('hero-chart'), [
        bar('xflare', hero.per_query.map(p => p.xflare_ms), XFLARE_COLOR),
        bar('velox', hero.per_query.map(p => p.velox_ms), VELOX_COLOR),
      ], heroLog);
    }

    function renderBoard(data) {
      const tbody = document.querySelector('#lb-board tbody');
      tbody.replaceChildren();
      const metricName = data.metric === 'total' ? 'Total' : data.metric.toUpperCase();
      document.getElementById('lb-board-note').textContent =
        `${metricName}: ${data.eligible_count} eligible run(s) under the filter · top ${data.top} · ` +
        (data.metric === 'total' ? 'xflare median 합' : 'xflare median') +
        ' 오름차순, 동률은 repeats 많은 순 → 먼저 끝난 순';
      if (!data.rows.length) {
        const tr = tbody.insertRow();
        const td = tr.insertCell();
        td.colSpan = 13;
        td.className = 'lb-empty';
        td.textContent = '이 필터에 순위에 오를 run 이 없습니다.';
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
                                          [fmtX(r.value.speedup), 'num'], [`x${r.repeats}`, 'num'],
                                          [compactConfig(r.config), null, configText(r.config)],
                                          [r.config.application_mode ?? '—', null],
                                          [fmtDevices(r), null],
                                          [r.submitter ?? '—', null]]) {
          const td = tr.insertCell();
          td.textContent = text;
          if (cls) td.className = cls;
          if (title) td.title = title;
        }
        tr.insertCell().appendChild(badgeList(r.badges));
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
      renderTabs();
      const q = query();
      history.replaceState(null, '', `${location.pathname}?${q}`);
      let resp, data;
      try {
        resp = await fetch(`/api/leaderboard/board?${q}`);
        data = await resp.json();
      } catch (e) {
        if (mine === generation) showStatus('board 를 불러오지 못했습니다.');
        return;
      }
      if (mine !== generation) return;
      if (!resp.ok) {
        // An unknown ?sf= from a shared link: forget it so the tabs come back.
        if (resp.status === 400 && state.sf && !state.tabs.length) { state.sf = ''; load(); return; }
        showStatus((data.errors || []).join('; ') || `HTTP ${resp.status}`);
        renderHero(null);
        renderBoard({ metric: state.metric, eligible_count: 0, top: state.top, rows: [] });
        return;
      }
      showStatus('');
      state.sf = data.sf;
      state.tabs = data.tabs;
      renderTabs();
      renderFilters(data);
      renderHero(data.hero, data.hero_fallback ? data.hero_notice : null);
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
    bindLogToggle(heroLog, () => chart);
    updateCompare();
    load();
  }

  // ---- run detail page --------------------------------------------------------
  function initRun() {
    const run = JSON.parse(document.getElementById('lb-run-data').textContent);
    const logBox = document.getElementById('run-log');
    const chart = perQueryChart(document.getElementById('run-chart'), [
      bar('xflare', run.xflare, XFLARE_COLOR),
      bar('velox', run.velox, VELOX_COLOR),
    ], logBox);
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
      alert(`withdraw 실패 (HTTP ${r.status}): ${((r.data && r.data.errors) || []).join('; ')}`);
    });
  }

  // ---- compare page -----------------------------------------------------------
  function initCompare() {
    const data = JSON.parse(document.getElementById('lb-compare-data').textContent);
    const logBox = document.getElementById('compare-log');
    const chart = perQueryChart(document.getElementById('compare-chart'),
                                data.series.map((s, i) => bar(data.names[i], s, SERIES_COLORS[i % SERIES_COLORS.length])),
                                logBox);
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
