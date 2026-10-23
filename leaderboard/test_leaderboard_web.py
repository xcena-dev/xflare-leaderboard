#!/usr/bin/env python3
"""Leaderboard HTTP tests through the Flask test client, each on its own app (app.create_app) and temp data dir.
Run: python3 leaderboard/test_leaderboard_web.py

Covers the HTTP side: the service starting from config.yaml, "/" → board, the data dir env override, 8 MiB cap
(413, also for chunked bodies), receipts and 409 over HTTP, dry run writes nothing, withdraw, the raw route,
hostile strings staying inert on every page, storage down → 503 on the storage routes only, SF tabs + default
filter + hero from one run, two concurrent POSTs, the reproduce script, the display budgets (cut scalars,
capped lists and facets, diff budget) that keep any accepted upload's pages small, and the static hashes and the
board's script header that keep a board page from rendering an answer meant for another script.
The page-script checks run leaderboard.js under node with a minimal DOM stub, and the reproduce-script checks run
it in bash; without node or bash they fail, naming the missing runtime (XFLARE_LEADERBOARD_SKIP_NODE_TESTS=1 skips the
page-script checks instead, for the auto-updater's self-check).
"""
from __future__ import annotations

import errno
import hashlib
import html
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

import markupsafe
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(HERE, "fixtures"))

from werkzeug.test import EnvironBuilder, run_wsgi_app                # noqa: E402

import app as lb_app                                                  # noqa: E402
from leaderboard import aggregate, repro, store as lb_store           # noqa: E402
from leaderboard.display import facet_token                           # noqa: E402
from leaderboard.schema import MAX_BODY_BYTES                         # noqa: E402
from make_fixtures import build_envelope, build_manifest              # noqa: E402

LB = "/b/xflare_leaderboard"
SUBMIT = "/api/leaderboard/submissions"
HOSTILE = ['<img src=x onerror=alert(1)>', '</script><script>alert(1)</script>', '"onmouseover=alert(1) x="']
# An argv spark-tpch.sh accepts.
REAL_ARGV = ["--reservation", "4242", "--xflare-toml", "scripts/config/Resource.mu.d1.toml.in", "--scale-factor",
             "100", "--executors", "4", "--executor-cores", "6", "1", "6"]


def rid() -> str:
    return str(uuid.uuid4())


def flat_elapsed(xflare: int, velox: int = 3000):
    return lambda q, r, engine: xflare + q if engine == "xflare" else velox + q


def config(data_dir: str) -> dict:
    """config.yaml's settings with storage pointed at ``data_dir``."""
    cfg = lb_app.load_config(os.path.join(ROOT, "config.yaml"))
    cfg["leaderboard"]["storage"] = {"data_dir": data_dir}
    return cfg


class WebCase(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="lb-web-")
        # The env override would beat the config's data dir.
        self._env = os.environ.pop(lb_store.DATA_DIR_ENV, None)
        self.app = lb_app.create_app(config(self.data))
        self.c = self.app.test_client()

    def tearDown(self):
        if self._env is not None:
            os.environ[lb_store.DATA_DIR_ENV] = self._env
        os.chmod(self.data, 0o755)
        shutil.rmtree(self.data, ignore_errors=True)

    def post(self, envelope, query="", status=201):
        r = self.c.post(SUBMIT + query, json=envelope)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:500])
        return r.get_json()

    def submit(self, argv=None, **kw):
        kw.setdefault("run_id", rid())
        m = build_manifest(**kw)
        if argv is not None:
            m["run"]["argv"] = argv
        self.post(build_envelope(m))
        return m["run_id"]

    def pin_target(self, run_id, revision=0, goal=None, status=200, **headers):
        """POST the SF-100 tab's target (A8) and check the status; returns the JSON answer."""
        r = self.c.post("/api/leaderboard/targets/100/snappy",
                        json={"actor": "a@x", "expected_revision": revision, "run_id": run_id, "goal": goal},
                        headers=headers)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:300])
        return r.get_json()

    def page_data(self) -> dict:
        """The board page's real data block, for the page-script harnesses."""
        html = self.c.get(LB).get_data(as_text=True)
        return {"lb-page-data": re.search(r'id="lb-page-data">(.*?)</script>', html, re.S).group(1)}

    def board(self, query="", status=200):
        r = self.c.get("/api/leaderboard/board" + query)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:500])
        return r.get_json()


class TestService(WebCase):
    """The standalone service: config.yaml settings, the data dir env override, app.py serving on its port."""

    def test_config_yaml(self):
        cfg = lb_app.load_config()
        self.assertEqual(cfg["server"], {"port": 8894, "bind": "0.0.0.0"})
        lb = cfg["leaderboard"]
        self.assertEqual((lb["tabs"], lb["default_filter"]), (["100", "1000"], {"executors": 4, "executor_cores": 6}))
        self.assertEqual(lb_store.resolve_data_dir(lb), lb["storage"]["data_dir"])
        other = os.path.join(self.data, "other.yaml")
        with open(other, "w") as f:
            f.write("leaderboard: {title: x}\n")
        with mock.patch.dict(os.environ, {lb_app.CONFIG_ENV: other}):
            self.assertEqual(lb_app.load_config(), {"leaderboard": {"title": "x"}})

    def test_links_follow_the_route_not_the_config(self):
        """The board script builds run and compare links from the board route; no config key names it."""
        cfg = config(self.data)
        cfg["leaderboard"].pop("id", None)
        cfg["leaderboard"]["title"] = "Other title"
        c = lb_app.create_app(cfg).test_client()
        page = c.get(LB).get_data(as_text=True)
        self.assertIn("<h1>Other title</h1>", page)
        data = json.loads(re.search(r'id="lb-page-data">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(data["board"], LB)

    def test_data_dir_env_override(self):
        """XFLARE_LEADERBOARD_DATA beats the config's storage.data_dir."""
        override = os.path.join(self.data, "override")
        os.mkdir(override)
        with mock.patch.dict(os.environ, {lb_store.DATA_DIR_ENV: override}):
            c = lb_app.create_app(config(os.path.join(self.data, "configured"))).test_client()
            run_id = build_manifest(run_id=rid())["run_id"]
            self.assertEqual(c.post(SUBMIT, json=build_envelope(build_manifest(run_id=run_id))).status_code, 201)
            self.assertEqual([r["run_id"] for r in c.get("/api/leaderboard/runs").get_json()], [run_id])
        self.assertEqual(sorted(os.listdir(self.data)), ["override"])
        self.assertIn(lb_store.DB_NAME, os.listdir(override))

    @staticmethod
    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def serve(self, args: list[str], port: int, **env_vars) -> str:
        """Starts `python3 app.py ARGS` with ``env_vars`` set (None: unset) and returns the page "/" redirects
        to, once it answers on ``port``."""
        env = dict(os.environ)
        for k, v in env_vars.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        # A log file, not a pipe: a server that logs more than a pipe holds would block mid-request.
        log = open(os.path.join(self.data, "app.log"), "w+")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py"), *args], cwd=self.data, env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:   # follows "/" → the board
                    return r.read().decode()
            except urllib.error.HTTPError as e:
                raise AssertionError(f"app.py answered {e.code}") from None
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    raise AssertionError("app.py did not serve:\n" + log.read()[-2000:]) from None
                time.sleep(0.1)

    def test_app_py_serves_from_its_config_file(self):
        """`python3 app.py --config FILE` with no flags: bind, port and data dir all come from the file, as the
        systemd unit runs it (there with config.yaml)."""
        port = self.free_port()
        cfg = config(self.data)
        cfg["server"] = {"port": port, "bind": "127.0.0.1"}
        path = os.path.join(self.data, "service.yaml")
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f)
        page = self.serve(["--config", path], port, XFLARE_LEADERBOARD_DATA=None)
        self.assertIn('data-page="board"', page)
        self.assertIn("<h1>XFLARE TPC-H Leaderboard</h1>", page)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/leaderboard/board", timeout=10) as r:
            self.assertEqual([t["label"] for t in json.load(r)["tabs"]], ["100 · snappy", "1000 · snappy"])
        self.assertIn(lb_store.DB_NAME, os.listdir(self.data))          # the file's data dir

    def test_app_py_flags_override_the_server_block(self):
        """With no --config it reads config.yaml; --host/--port beat its server block, the env its data dir."""
        port = self.free_port()
        page = self.serve(["--host", "127.0.0.1", "--port", str(port)], port,
                          XFLARE_LEADERBOARD_DATA=self.data, XFLARE_LEADERBOARD_CONFIG=None)
        self.assertIn("<h1>XFLARE TPC-H Leaderboard</h1>", page)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/leaderboard/board", timeout=10) as r:
            self.assertEqual(r.status, 200)
        self.assertIn(lb_store.DB_NAME, os.listdir(self.data))


class TestBodyCap(WebCase):
    """Over 8 MiB → 413, by Content-Length and by a bounded read when there is none."""

    def test_content_length_over_cap(self):
        r = self.c.post(SUBMIT, data=b" " * (MAX_BODY_BYTES + 1), content_type="application/json")
        self.assertEqual(r.status_code, 413)
        self.assertEqual(os.listdir(self.data), [])

    def test_chunked_body_over_cap(self):
        """No Content-Length and wsgi.input_terminated, as the dev server passes a chunked body: Werkzeug then
        hands over the raw stream (no global MAX_CONTENT_LENGTH), so only the bounded read stops it."""
        class Endless(io.RawIOBase):
            served = 0

            def readable(self):
                return True

            def readinto(self, buf):
                Endless.served += len(buf)
                buf[:] = b" " * len(buf)
                return len(buf)

        env = EnvironBuilder(path=SUBMIT, method="POST", content_type="application/json").get_environ()
        env.pop("CONTENT_LENGTH", None)
        env.update({"wsgi.input": io.BufferedReader(Endless()), "wsgi.input_terminated": True,
                    "HTTP_TRANSFER_ENCODING": "chunked"})
        app_iter, status, _headers = run_wsgi_app(self.app, env, buffered=True)
        self.assertTrue(status.startswith("413"), status)
        self.assertLess(Endless.served, MAX_BODY_BYTES + (1 << 20))
        self.assertEqual(os.listdir(self.data), [])

    def test_at_cap_is_parsed(self):
        body = json.dumps(build_envelope(build_manifest(run_id=rid()))).encode()
        body += b" " * (MAX_BODY_BYTES - len(body))
        r = self.c.post(SUBMIT, data=body, content_type="application/json")
        self.assertEqual(r.status_code, 201)


class TestIdentity(WebCase):
    """Receipts over HTTP: 201 created, 200 identical replay, 409 changed content, 400 rejects; dry run stores nothing."""

    def test_created_existing_conflict(self):
        m = build_manifest(run_id=rid())
        first = self.post(build_envelope(m))
        self.assertEqual(set(first), {"run_id", "content_id", "url", "ranked_total", "warnings"})
        self.assertTrue(first["ranked_total"])
        self.assertEqual(first["url"], f"{LB}/run/{m['run_id']}")
        self.assertEqual(self.post(build_envelope(m), status=200), first)

        # Envelope metadata is mutable and outside the hash.
        again = self.post(build_envelope(m, note="second note"), status=200)
        self.assertEqual(again["content_id"], first["content_id"])
        self.assertEqual(self.c.get(f"/api/leaderboard/runs/{m['run_id']}").get_json()["meta"]["note"],
                         "second note")

        changed = build_manifest(run_id=m["run_id"], elapsed=flat_elapsed(500))
        r = self.c.post(SUBMIT, json=build_envelope(changed))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["content_id"], first["content_id"])

    def test_rejects(self):
        m = build_manifest(run_id=rid())
        for body in (b'{"schema": 1, "schema": 2}', b"[1, 2]", b"{not json", b'{"x": NaN}'):
            r = self.c.post(SUBMIT, data=body, content_type="application/json")
            self.assertEqual(r.status_code, 400, body)
            self.assertTrue(r.get_json()["errors"])
        r = self.c.post(SUBMIT, json=m)            # a bare manifest only on the dry-run path
        self.assertEqual(r.status_code, 400)
        self.assertEqual(os.listdir(self.data), [])

    def test_dry_run_writes_nothing(self):
        m = build_manifest(run_id=rid())
        for body in (m, build_envelope(m)):
            out = self.post(body, "?dry_run=1", status=200)
            self.assertTrue(out["dry_run"])
            self.assertTrue(out["ranked_total"])
        self.assertEqual(os.listdir(self.data), [])       # no DB file, no raw/, no row
        # It never opens storage, so it answers even when there is none.
        shutil.rmtree(self.data)
        self.post(m, "?dry_run=1", status=200)
        os.mkdir(self.data)


class TestWithdraw(WebCase):
    """Withdraw over HTTP: idempotent, drops the run from boards and hero, not revived by a re-upload."""

    def test_withdraw_idempotent_and_not_revived(self):
        m = build_manifest(run_id=rid())
        first = self.post(build_envelope(m))
        url = f"/api/leaderboard/runs/{m['run_id']}/withdraw"
        r = self.c.post(url, json={"actor": "a@x", "reason": "bad run"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["withdrawn"])
        r = self.c.post(url, json={"actor": "b@x", "reason": "again"})
        self.assertEqual(r.get_json()["withdrawal"]["actor"], "a@x")          # first record kept

        b = self.board()
        self.assertIsNone(b["hero"])
        self.assertEqual(b["rows"], [])
        # A replay returns the receipt as first issued, even after the withdrawal.
        self.assertEqual(self.post(build_envelope(m), status=200), first)
        self.assertIsNone(self.board()["hero"])
        self.assertTrue(self.c.get(f"/api/leaderboard/runs/{m['run_id']}").get_json()["withdrawn"])
        page = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        self.assertIn("Withdrawn", page)
        self.assertNotIn('id="lb-withdraw"', page)

    def test_withdraw_errors(self):
        self.assertEqual(self.c.post(f"/api/leaderboard/runs/{rid()}/withdraw",
                                     json={"actor": "a", "reason": "r"}).status_code, 404)
        run_id = self.submit()
        url = f"/api/leaderboard/runs/{run_id}/withdraw"
        for body in ({}, {"actor": "", "reason": "r"}, {"actor": "a", "reason": 3}, {"actor": " ", "reason": "r"}):
            self.assertEqual(self.c.post(url, json=body).status_code, 400, body)
        # No length cap: any non-empty strings are accepted (summaries cut them for display).
        r = self.c.post(url, json={"actor": "a" * 5000, "reason": "r" * 50_000})
        self.assertEqual(r.status_code, 200)
        whole = self.c.get(f"/api/leaderboard/runs/{run_id}").get_json()["withdrawal"]
        self.assertEqual((len(whole["actor"]), len(whole["reason"])), (5000, 50_000))


class TestRawRoute(WebCase):
    """Raw download: found only through the DB, attachment + nosniff, traversal/encoded ids and symlinks refused."""

    def test_raw_attachment(self):
        m = build_manifest(run_id=rid())
        receipt = self.post(build_envelope(m))
        r = self.c.get(f"/api/leaderboard/runs/{m['run_id']}/raw")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/json")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(r.headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(hashlib.sha256(r.data).hexdigest(), receipt["content_id"])

    def test_rejects_non_ids_and_symlinks(self):
        m = build_manifest(run_id=rid())
        receipt = self.post(build_envelope(m))
        for bad in ("abc", "..", "%2e%2e", "..%2F..%2Fetc%2Fpasswd", "..%252F", m["run_id"].upper(),
                    m["run_id"] + "%00", rid()):
            r = self.c.get(f"/api/leaderboard/runs/{bad}/raw")
            self.assertIn(r.status_code, (400, 404), bad)
            self.assertNotEqual(r.mimetype, "application/octet-stream")
        raw = os.path.join(self.data, "raw", receipt["content_id"] + ".json")
        secret = os.path.join(self.data, "secret.json")
        with open(secret, "w") as f:
            f.write('{"secret": 1}')
        os.unlink(raw)
        os.symlink(secret, raw)
        r = self.c.get(f"/api/leaderboard/runs/{m['run_id']}/raw")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(b"secret", r.data)


class TestHostileStrings(WebCase):
    """Uploaded strings are inert on the board API, detail, compare and error responses."""

    def hostile_envelope(self):
        m = build_manifest(run_id=rid())
        # values of options spark-tpch.sh takes, so the run page also prints them in its reproduce script
        m["run"]["argv"] = REAL_ARGV + [x for h in HOSTILE for x in ("--xflare", h)]
        m["run"]["xflare_overrides"].append({"key": HOSTILE[0], "value": HOSTILE[1],
                                             "source": "cli", "destination": "session"})
        m["run"]["xflare_toml"]["name"] = HOSTILE[2]
        m["run"]["xflare_toml"]["rendered_text"] += HOSTILE[1] + "\n"
        m["build"]["bundle"]["resolved"] = HOSTILE[0]
        for rep in m["repeats"]:
            rep["bundle"] = HOSTILE[0]
            for app in rep["applications"]:                                 # per-engine: the repeat's is derived
                app["bundle"] = HOSTILE[0]
        env = build_envelope(m, note=" ".join(HOSTILE), submitter={"email": HOSTILE[0], "sdp_id": HOSTILE[2]})
        env["environment"]["host"] = HOSTILE[1]
        env["environment"]["devices"][0]["id"] = HOSTILE[2]
        return env

    def assert_inert(self, html: str, where: str):
        for s in HOSTILE:
            self.assertNotIn(s, html, where)

    def test_pages_escape(self):
        env = self.hostile_envelope()
        self.post(env)
        hostile_id = env["manifest"]["run_id"]
        other = self.submit()

        run_page = self.c.get(f"{LB}/run/{hostile_id}")
        self.assertEqual(run_page.status_code, 200)
        html = run_page.get_data(as_text=True)
        self.assert_inert(html, "run page")
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)            # shown, escaped
        self.assertIn("--xflare &#39;&lt;img src=x onerror=alert(1)&gt;&#39;", html)   # in the script too

        cmp_page = self.c.get(f"{LB}/compare?runs={hostile_id},{other}")
        self.assertEqual(cmp_page.status_code, 200)
        self.assert_inert(cmp_page.get_data(as_text=True), "compare page")

        self.assert_inert(self.c.get(LB).get_data(as_text=True), LB)

        # The board API returns them as JSON data; the page sets them with textContent.
        b = self.c.get("/api/leaderboard/board?executors=&executor_cores=")
        self.assertEqual(b.mimetype, "application/json")
        self.assertIn(HOSTILE[0], [r["submitter"] for r in b.get_json()["rows"]])

    def test_error_responses(self):
        r = self.c.post(SUBMIT, json={"schema": HOSTILE[0], "manifest": HOSTILE[1]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.mimetype, "application/json")
        for url in (f"{LB}/run/{HOSTILE[0]}", f"{LB}/compare?runs={HOSTILE[0]},{HOSTILE[1]}",
                    f"/api/leaderboard/runs/{HOSTILE[2]}"):
            r = self.c.get(url)
            self.assertIn(r.status_code, (400, 404), url)
            self.assert_inert(r.get_data(as_text=True), url)

    def test_no_html_sinks_in_templates_or_js(self):
        """The grep half: no path from data to HTML parsing in the leaderboard's own templates/JS."""
        sinks = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function")
        unsafe = re.compile(r"\|\s*safe\b|autoescape\s+false|Markup\(")
        files = [os.path.join(ROOT, "static", "leaderboard.js")] + [
            os.path.join(ROOT, "templates", n) for n in sorted(os.listdir(os.path.join(ROOT, "templates")))]
        self.assertEqual(len(files), 5)
        for path in files:
            with open(path) as f:
                text = f.read()
            self.assertIsNone(sinks.search(text), path)
            self.assertIsNone(unsafe.search(text), path)


class TestStorageDown(WebCase):
    """Storage missing or read-only: every route that reads or writes it 503s; the board page (which reads none)
    and the "/" redirect keep their status."""

    LB_ROUTES = ["/api/leaderboard/board", "/api/leaderboard/runs"]
    NO_STORAGE = {"/": 302, LB: 200}

    def statuses(self, routes):
        return {u: self.c.get(u).status_code for u in routes}

    def check_down(self, run_id):
        for u in self.LB_ROUTES + [f"{LB}/run/{run_id}", f"/api/leaderboard/runs/{run_id}",
                                   f"/api/leaderboard/runs/{run_id}/raw"]:
            r = self.c.get(u)
            self.assertEqual(r.status_code, 503, u)
            self.assertNotIn(self.data, r.get_data(as_text=True))
        self.assertEqual(self.c.post(SUBMIT, json=build_envelope(build_manifest(run_id=rid()))).status_code, 503)
        self.assertEqual(self.c.post(f"/api/leaderboard/runs/{run_id}/withdraw",
                                     json={"actor": "a", "reason": "r"}).status_code, 503)
        self.assertEqual(self.statuses(self.NO_STORAGE), self.NO_STORAGE)

    def test_missing_and_read_only(self):
        run_id = self.submit()
        moved = self.data + ".moved"
        os.rename(self.data, moved)
        try:
            self.check_down(run_id)
        finally:
            os.rename(moved, self.data)

        if os.geteuid() != 0:                         # root ignores the mode bits
            os.chmod(self.data, 0o555)
            self.check_down(run_id)


class TestBoard(WebCase):
    """SF tabs, default filter, hero from one run, top-N, Q tabs, filters, compare."""

    def test_tabs_default_filter_and_hero(self):
        hero = self.submit(elapsed=flat_elapsed(1000))
        slower = self.submit(elapsed=flat_elapsed(2000))
        off_filter = self.submit(elapsed=flat_elapsed(100), executors=1)
        sf1000 = self.submit(sf="1000", elapsed=flat_elapsed(9000))
        self.submit(elapsed=flat_elapsed(50), record_answers=True)       # never ranks

        b = self.board()
        self.assertEqual(b["tabs"], [{"sf": "100", "codec": "snappy", "label": "100 · snappy"},
                                     {"sf": "1000", "codec": "snappy", "label": "1000 · snappy"}])
        self.assertEqual((b["sf"], b["codec"]), ("100", "snappy"))
        self.assertEqual(b["filter"], {"executors": "4", "executor_cores": "6"})
        self.assertEqual([r["run_id"] for r in b["rows"]], [hero, slower])
        self.assertEqual(b["hero"]["run_id"], hero)
        detail = self.c.get(f"/api/leaderboard/runs/{hero}").get_json()["derived"]["per_query"]
        self.assertEqual([p["xflare_ms"] for p in b["hero"]["per_query"]],
                         [detail[str(q)]["xflare_ms"] for q in range(1, 23)])
        self.assertEqual(len(b["hero"]["per_query"]), 22)
        self.assertEqual(b["hero"]["totals"]["xflare_ms"], sum(1000 + q for q in range(1, 23)))

        lifted = self.board("?executors=&executor_cores=")
        self.assertEqual(lifted["filter"], {})
        self.assertEqual(lifted["hero"]["run_id"], off_filter)
        self.assertEqual(self.board("?executors=1&executor_cores=")["hero"]["run_id"], off_filter)
        self.assertEqual(self.board("?sf=1000")["hero"]["run_id"], sf1000)
        dev = self.board("?badge=dev%20bundle")          # nothing matches: the hero falls back, the rows do not
        self.assertEqual((dev["hero"]["run_id"], dev["hero_fallback"], dev["rows"]), (off_filter, True, []))
        self.assertIn({"token": facet_token("4"), "label": "4"}, b["facets"]["executors"])
        self.assertIn({"token": facet_token("1"), "label": "1"}, b["facets"]["executors"])

        self.board("?sf=10", status=400)
        self.board("?metric=q23", status=400)

    def test_top_n_and_query_tabs(self):
        ids = [self.submit(elapsed=flat_elapsed(1000 + 10 * i)) for i in range(6)]
        self.assertEqual(len(self.board("?top=5")["rows"]), 5)
        self.assertEqual(len(self.board("?top=25")["rows"]), 6)
        self.assertEqual(len(self.board("?top=7")["rows"]), 6)              # not a choice → default 10
        # Q3 ranks by that query's median; the slowest-total run is fastest on Q3.
        q3 = self.submit(elapsed=lambda q, r, e: (10 if q == 3 else 5000) if e == "xflare" else 6000)
        b = self.board("?metric=q3")
        self.assertEqual(b["rows"][0]["run_id"], q3)
        self.assertEqual(b["rows"][0]["value"]["xflare_ms"], 10)
        self.assertEqual(self.board()["rows"][0]["run_id"], ids[0])

    def test_pages_and_compare(self):
        a, b, c = (self.submit(argv=REAL_ARGV, elapsed=flat_elapsed(1000 + i)) for i in range(3))
        page = self.c.get(f"{LB}/run/{a}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertRegex(html, r'<pre id="repro-cmd">[^<]*scripts/spark-tpch.sh [^<]*--reservation &#34;\$RESERVATION&#34;')
        self.assertIn('class="d-add"', html)                               # input vs rendered toml diff
        self.assertEqual(self.c.get(f"{LB}/run/{rid()}").status_code, 404)

        r = self.c.get(f"{LB}/compare?runs={a},{b},{c}")
        self.assertEqual(r.status_code, 200)
        for x in (a, b, c):
            self.assertIn(x[:8], r.get_data(as_text=True))
        self.assertEqual(self.c.get(f"{LB}/compare?runs={a}").status_code, 400)
        self.assertEqual(self.c.get(f"{LB}/compare?runs={a},{a}").status_code, 400)
        self.assertEqual(self.c.get(f"{LB}/compare?runs={','.join([a, b, c, rid(), rid()])}").status_code, 400)
        self.assertEqual(self.c.get(f"{LB}/compare?runs={a},{rid()}").status_code, 404)

    def test_root_redirects_to_the_board(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], LB)
        page = self.c.get("/", follow_redirects=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-page="board"', page.get_data(as_text=True))


class TestRawStorage(WebCase):
    """The raw download is the whole stored file; any storage failure under it is a 503, never a 404 or 500."""

    def test_served_whole_past_body_cap(self):
        """A compact body whose canonical form is bigger than 8 MiB ("1E5" re-serialises as "100000.0")."""
        m = build_manifest(run_id=rid())
        m["x_probe"] = "__PROBE__"
        body = json.dumps(build_envelope(m)).replace('"__PROBE__"', "[" + ",".join(["1E5"] * 1_200_000) + "]")
        self.assertLess(len(body), MAX_BODY_BYTES)
        receipt = self.c.post(SUBMIT, data=body, content_type="application/json").get_json()
        r = self.c.get(f"/api/leaderboard/runs/{m['run_id']}/raw")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.data), MAX_BODY_BYTES)
        self.assertEqual(hashlib.sha256(r.data).hexdigest(), receipt["content_id"])

    def _raw_io_fails(self, target: str):
        """os.fstat or the read of the raw file raises EIO inside the store; every other call is untouched."""
        m = build_manifest(run_id=rid())
        raw = os.path.realpath(os.path.join(self.data, "raw", self.post(build_envelope(m))["content_id"] + ".json"))
        is_raw = lambda fd: os.readlink(f"/proc/self/fd/{fd}") == raw          # noqa: E731
        real_fstat, real_fdopen = os.fstat, os.fdopen

        def fstat(fd):
            if isinstance(fd, int) and is_raw(fd):
                raise OSError(errno.EIO, "injected")
            return real_fstat(fd)

        class FailingRead:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()

            def read(self, *a):
                raise OSError(errno.EIO, "injected")

        def fdopen(fd, *a, **kw):
            f = real_fdopen(fd, *a, **kw)
            return FailingRead(f) if is_raw(fd) and "r" in (a[0] if a else kw.get("mode", "r")) else f

        patch = {"fstat": mock.patch.object(lb_store.os, "fstat", fstat),
                 "read": mock.patch.object(lb_store.os, "fdopen", fdopen)}[target]
        with patch:
            r = self.c.get(f"/api/leaderboard/runs/{m['run_id']}/raw")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json(), {"errors": ["leaderboard storage unavailable"]})

    def test_fstat_failure_is_503(self):
        self._raw_io_fails("fstat")

    def test_read_failure_is_503(self):
        self._raw_io_fails("read")

    @unittest.skipIf(os.geteuid() == 0, "root reads mode-000 files")
    def test_unreadable_raw_is_503(self):
        m = build_manifest(run_id=rid())
        raw = os.path.join(self.data, "raw", self.post(build_envelope(m))["content_id"] + ".json")
        os.chmod(raw, 0)
        try:
            r = self.c.get(f"/api/leaderboard/runs/{m['run_id']}/raw")
        finally:
            os.chmod(raw, 0o644)
        self.assertEqual(r.status_code, 503)
        self.assertTrue(r.is_json)

    @unittest.skipIf(os.geteuid() == 0, "root traverses mode-000 dirs")
    def test_data_dir_under_untraversable_parent(self):
        """503 JSON without the path on the storage routes; the board page still loads."""
        run_id = self.submit()
        parent = tempfile.mkdtemp(prefix="lb-locked-")
        inner = os.path.join(parent, "data")
        os.rename(self.data, inner)
        c = lb_app.create_app(config(inner)).test_client()
        os.chmod(parent, 0)
        try:
            for u in ("/api/leaderboard/runs", "/api/leaderboard/board", f"/api/leaderboard/runs/{run_id}"):
                r = c.get(u)
                self.assertEqual(r.status_code, 503, u)
                self.assertEqual(r.get_json(), {"errors": ["leaderboard storage unavailable"]})
            r = c.get(f"{LB}/run/{run_id}")
            self.assertEqual(r.status_code, 503)
            self.assertNotIn(parent, r.get_data(as_text=True))
            self.assertEqual(c.get(LB).status_code, 200)
        finally:
            os.chmod(parent, 0o755)
            os.rename(inner, self.data)
            os.rmdir(parent)


class TestAcceptedExtremes(WebCase):
    """Whatever the validator accepts never turns into a 500 later."""

    def test_huge_integers(self):
        def huge_q1(q, r, engine):
            return 10 ** 400 + r if q == 1 and engine == "xflare" else 1000 + q
        cases = [build_manifest(run_id=rid(), elapsed=huge_q1),
                 build_manifest(run_id=rid(), elapsed=huge_q1, repeats=2),    # even-N median past float range
                 build_manifest(run_id=rid(), elapsed=lambda q, r, e: 10 ** 400 if e == "velox" else 1000)]
        for n in (100_000_000, 10 ** 20):
            m = build_manifest(run_id=rid())
            m["run"]["repeat_requested"] = n
            cases.append(m)
        for m in cases:
            self.post(m, "?dry_run=1", status=200)
            self.post(build_envelope(m), status=201)
            self.assertEqual(self.c.get(f"{LB}/run/{m['run_id']}").status_code, 200)
        other = self.submit()
        self.assertEqual(self.c.get(f"{LB}/compare?runs={cases[0]['run_id']},{cases[2]['run_id']},{other}")
                         .status_code, 200)
        for u in ("/api/leaderboard/board?executors=&executor_cores=", "/api/leaderboard/runs"):
            self.assertEqual(self.c.get(u).status_code, 200, u)

    def test_non_ascii_digit_timestamps_rejected(self):
        """Unicode digits (U+0661) are not RFC 3339: 400 at the door, so the board never meets them."""
        for ts in ("2026-10-01T01:03:00.١Z", "2026-10-01T01:03:0١Z", "٢026-10-01T01:03:00Z"):
            m = build_manifest(run_id=rid())
            m["run"]["finished_at"] = ts
            r = self.c.post(SUBMIT, json=build_envelope(m))
            self.assertEqual(r.status_code, 400, ts)
            self.assertTrue(r.get_json()["errors"])
        self.submit()
        for u in ("/api/leaderboard/board", "/api/leaderboard/runs"):
            self.assertEqual(self.c.get(u).status_code, 200, u)


# Runs leaderboard.js against a minimal DOM stub: ids the page has become elements (all ids when "ids" is
# null), "data" seeds their textContent, fetch answers with "fetch", "slots" are the board's filter slots. Prints
# the listeners it saw, the text and hidden flag of every element by id, and the text, value, title and href of every element it
# created. "check" ticks that many of the board's row checkboxes in order, then "click" names an element whose click
# handlers run ("clickClass": every element of that class); "secure" is window.isSecureContext. "prompts" answers
# prompt() in turn, "datasets" gives elements their data-* attributes, "rejectPost" makes a POST fail like a lost
# connection. The compare button's disabled state after each tick ("gate"), location.href, clipboard writes,
# execCommand calls, every fetch ("fetches": url, method, JSON body), alerts, reloads and each element's disabled
# flag by id ("disabled") are reported.
_JS_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const cfg = JSON.parse(fs.readFileSync(0, 'utf8'));
const listeners = {}, handlers = {}, byId = {}, all = [], clipboard = [], execs = [];
class Element {
  constructor(tag, id) {
    Object.assign(this, { tagName: tag, id, children: [], dataset: { ...((cfg.datasets || {})[id] || {}) },
                          hidden: false, value: '', attrs: {},
                          title: '', textContent: (cfg.data || {})[id] || '' });
    this.style = { setProperty() {} };
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    all.push(this);
  }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...c) { this.children = c; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, h) {
    if (t === 'click') (this.clicks = this.clicks || []).push(h);
    if (t === 'change') (this.changes = this.changes || []).push(h);
    if (!this.id) return;
    (listeners[this.id] = listeners[this.id] || []).push(t);
    if (t === 'click') (handlers[this.id] = handlers[this.id] || []).push(h);
  }
  select() {}
  remove() {}
  insertRow() { return this.appendChild(new Element('tr')); }
  insertCell() { return this.appendChild(new Element('td')); }
}
global.Node = Element;
global.document = {
  body: { dataset: { page: cfg.page }, appendChild(c) { return c; } },
  createRange() { return { selectNodeContents() {} }; },
  getElementById(id) {
    if (cfg.ids && !cfg.ids.includes(id)) return null;
    return byId[id] = byId[id] || new Element('div', id);
  },
  querySelector(sel) { return byId[sel] = byId[sel] || new Element('div'); },
  querySelectorAll() {
    return (cfg.slots || []).map(k => Object.assign(new Element('span'), { dataset: { key: k, label: k } }));
  },
  createElement(tag) { return new Element(tag); },
  execCommand(c) { execs.push(c); return true; },
};
global.window = { isSecureContext: !!cfg.secure };
// Node has a read-only global navigator of its own; replace it.
Object.defineProperty(globalThis, 'navigator',
                      { value: { clipboard: { writeText: async t => { clipboard.push(t); } } }, configurable: true });
global.getSelection = () => ({ removeAllRanges() {}, addRange() {} });
global.location = { search: '', pathname: '/b/xflare_leaderboard', reload() { reloads.push(1); } };
global.history = { replaceState() {} };
const fetches = [], alerts = [], reloads = [], prompts = [...(cfg.prompts || [])];
global.prompt = () => (prompts.length ? prompts.shift() : null);
global.alert = m => { alerts.push(String(m)); };
global.fetch = async (url, init) => {
  fetches.push({ url, method: (init && init.method) || 'GET', body: init && init.body ? JSON.parse(init.body) : null });
  if (cfg.rejectPost && init && init.method === 'POST') throw new TypeError('Failed to fetch');
  return { ok: true, status: 200, json: async () => JSON.parse(cfg.fetch),
           headers: { get: k => (cfg.headers || {})[k.toLowerCase()] || null } };
};
vm.runInThisContext(fs.readFileSync(cfg.js, 'utf8'));
setTimeout(async () => {
  const gate = [];
  for (const box of all.filter(e => !e.id && e.type === 'checkbox').slice(0, cfg.check || 0)) {
    box.checked = true;
    for (const h of box.changes) h();
    gate.push(byId['lb-compare'].disabled);
  }
  for (const h of handlers[cfg.click] || []) await h();
  for (const e of all.filter(e => cfg.clickClass && e.className === cfg.clickClass)) for (const h of e.clicks || []) await h();
  const text = {};
  const hidden = {};
  const disabled = {};
  for (const [id, e] of Object.entries(byId)) if (e.id) { text[id] = e.textContent; hidden[id] = e.hidden; disabled[id] = !!e.disabled; }
  console.log(JSON.stringify({ listeners, text, hidden, texts: all.map(e => String(e.textContent)),
                               values: all.map(e => String(e.value)), titles: all.map(e => String(e.title)),
                               classes: all.map(e => String(e.className)), hrefs: all.map(e => String(e.href || '')), gate, location: location.href || null,
                               clipboard, execs, fetches, alerts, reloads: reloads.length, disabled }));
}, 50);
"""


SKIP_NODE_ENV = "XFLARE_LEADERBOARD_SKIP_NODE_TESTS"


def page_script_node() -> str:
    """node for the page-script tests; missing node fails them. The auto-updater's self-check sets SKIP_NODE_ENV=1 to
    skip them instead: the page script runs in browsers, so the server's node (12 on Ubuntu 22.04, too old for `??`)
    says nothing about it. They must pass on a machine with node >= 14 before a merge (README: Automatic updates).
    A test that also checks the server keeps its page-script part in a subTest, so those checks still run."""
    if os.environ.get(SKIP_NODE_ENV) == "1":
        raise unittest.SkipTest(f"{SKIP_NODE_ENV}=1")
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node not installed: the page-script tests need it (README: Run locally)")
    return node


def run_page_js(page: str, ids=None, data=None, fetch=None, slots=None, click=None, click_class=None,
                secure=False, check=0, headers=None, prompts=(), datasets=None, reject_post=False) -> dict:
    node = page_script_node()
    cfg = {"js": os.path.join(ROOT, "static", "leaderboard.js"), "page": page, "ids": ids,
           "data": data or {}, "fetch": fetch, "slots": slots, "click": click,
           "clickClass": click_class, "secure": secure, "check": check, "prompts": list(prompts),
           "datasets": datasets or {}, "rejectPost": reject_post}
    if headers is None:
        # The answer names the page's own script, as the service that rendered the page does.
        script = json.loads((data or {}).get("lb-page-data") or "{}").get("script")
        headers = {"X-Leaderboard-Script": script} if script else {}
    cfg["headers"] = {k.lower(): v for k, v in headers.items()}
    out = subprocess.run([node, "-e", _JS_HARNESS], input=json.dumps(cfg), capture_output=True, text=True,
                         timeout=30)
    if out.returncode:
        raise AssertionError("leaderboard.js failed:\n" + out.stderr[-2000:])
    return json.loads(out.stdout)


class TestPageBudgets(WebCase):
    """Pages and board/runs summaries show a bounded cut of whatever was accepted; the raw JSON keeps it all."""

    def test_toml_diff_budget(self):
        m = build_manifest(run_id=rid())
        lines = [str(i) for i in range(12000)]
        swapped = [lines[i ^ 1] for i in range(len(lines))]
        t = m["run"]["xflare_toml"]
        t["input_text"], t["rendered_text"] = "\n".join(lines), "\n".join(swapped)
        t["input_sha256"] = hashlib.sha256(t["input_text"].encode()).hexdigest()
        t["rendered_sha256"] = hashlib.sha256(t["rendered_text"].encode()).hexdigest()
        self.post(build_envelope(m))
        t0 = time.monotonic()
        r = self.c.get(f"{LB}/run/{m['run_id']}")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(r.status_code, 200)
        self.assertIn("too large to diff", r.get_data(as_text=True))

    def test_uploaded_lists_bounded(self):
        m = build_manifest(run_id=rid())
        m["run"]["argv"] = [""] * 1_000_000
        m["run"]["xflare_overrides"] = [{"key": f"k{i}", "value": "v", "source": "cli", "destination": "session"}
                                        for i in range(10_000)]
        env = build_envelope(m)
        env["environment"]["devices"] = [{"id": str(i), "serial": None, "type": None, "fw": None}
                                         for i in range(10_000)]
        env["environment"]["overlapping"] = [{"owner": "o", "start": "2026-10-01T00:00:00Z",
                                              "end": "2026-10-01T00:01:00Z"} for _ in range(10_000)]
        env["environment"]["booking_overlap"] = "present"
        self.post(env)
        other = self.submit()
        for u in (f"{LB}/run/{m['run_id']}", f"{LB}/compare?runs={m['run_id']},{other}"):
            r = self.c.get(u)
            self.assertEqual(r.status_code, 200, u)
            self.assertLess(len(r.data), 512 * 1024, u)
            self.assertIn("more — see raw JSON", r.get_data(as_text=True), u)

    def test_long_scalars_cut_everywhere(self):
        """Megabyte-long producer / sdp_id / bundle / submitter: every page and summary stays small."""
        big = "x" * 1_000_000
        m = build_manifest(run_id=rid())
        m["build"]["bundle"]["resolved"] = "b" + big
        env = build_envelope(m, producer="p" + big, submitter={"email": "s" + big, "sdp_id": "d" + big})
        self.post(env)
        other = self.submit()
        for u in (f"{LB}/run/{m['run_id']}", f"{LB}/compare?runs={m['run_id']},{other}",
                  "/api/leaderboard/board", "/api/leaderboard/runs"):
            r = self.c.get(u)
            self.assertEqual(r.status_code, 200, u)
            self.assertLess(len(r.data), 512 * 1024, u)
        rows = self.c.get("/api/leaderboard/runs").get_json()
        mine = next(x for x in rows if x["run_id"] == m["run_id"])
        self.assertEqual(mine["submitter"], ("s" + big)[:512] + "…")
        # Filters still compare the full stored value, not the cut one.
        self.assertEqual([x["run_id"] for x in self.c.get("/api/leaderboard/runs?submitter=s" + big).get_json()],
                         [m["run_id"]])
        self.assertEqual(self.c.get("/api/leaderboard/runs?submitter=" + mine["submitter"]).get_json(), [])
        self.assertEqual(len(self.c.get(f"/api/leaderboard/runs/{m['run_id']}").get_json()["meta"]["producer"]),
                         1_000_001)

    def test_long_invocations_cut_per_repeat(self):
        """repeats[].invocation is an identifier shown once per repeat row: cut like any scalar, so 100 repeats
        with 60,000-char invocations still give a small run page."""
        # shared: ranking is not the point, and per-engine applications would repeat each invocation past the cap
        m = build_manifest(run_id=rid(), repeats=100, application_mode=None)
        for rep in m["repeats"]:
            rep["invocation"] = "i" * 60_000
        self.post(build_envelope(m))
        r = self.c.get(f"{LB}/run/{m['run_id']}")
        self.assertEqual(r.status_code, 200)
        self.assertLess(len(r.data), 1024 * 1024)
        self.assertIn("i" * 512 + "…", r.get_data(as_text=True))

    def test_many_devices_bounded(self):
        m = build_manifest(run_id=rid())
        env = build_envelope(m)
        env["environment"]["devices"] = [{"id": f"dev-{i}", "serial": None, "type": None, "fw": None}
                                         for i in range(20_000)]
        self.post(env)
        r = self.c.get("/api/leaderboard/board")
        self.assertLess(len(r.data), 512 * 1024)
        b = r.get_json()
        self.assertEqual(len(b["facets"]["device"]), 200)
        self.assertTrue(b["facets_truncated"])
        self.assertEqual((len(b["hero"]["device_ids"]), b["hero"]["device_count"]), (100, 20_000))
        self.assertEqual(len(b["rows"][0]["device_ids"]), 100)
        self.assertIn('id="lb-facets-note"', self.c.get(LB).get_data(as_text=True))
        self.assertFalse(self.board("?sf=1000")["facets_truncated"])

    def test_omitted_command_says_why(self):
        too_many = build_manifest(run_id=rid())
        too_many["run"]["argv"] = ["--download-logs"] * (repro.MAX_ARGS + 1)
        too_long = build_manifest(run_id=rid())
        too_long["run"]["argv"] = ["--results-dir", "y" * repro.MAX_CHARS]
        unparsed = build_manifest(run_id=rid())
        unparsed["run"]["argv"] = ["--note", "x", "1"]                # spark-tpch.sh has no --note
        for m in (too_many, too_long, unparsed):
            self.post(build_envelope(m))
        html = self.c.get(f"{LB}/run/{too_many['run_id']}").get_data(as_text=True)
        self.assertIn(f"argv 항목이 너무 많습니다 ({repro.MAX_ARGS + 1} entries, 한도 {repro.MAX_ARGS})", html)
        html = self.c.get(f"{LB}/run/{too_long['run_id']}").get_data(as_text=True)
        self.assertIn("명령이 너무 깁니다 (", html)
        self.assertIn(f"characters, 한도 {repro.MAX_CHARS})", html)
        self.assertNotIn("항목이 너무 많습니다", html)
        html = self.c.get(f"{LB}/run/{unparsed['run_id']}").get_data(as_text=True)
        self.assertIn("argv 를 spark-tpch.sh 의 옵션으로 읽지 못했습니다", html)
        self.assertNotIn('id="repro-cmd"', html)

    def test_withdraw_on_a_lost_connection(self):
        """A lost connection while withdrawing re-enables the button and says the outcome is unknown."""
        run_id = self.submit()
        html = self.c.get(f"{LB}/run/{run_id}").get_data(as_text=True)
        data = re.search(r'id="lb-run-data">(.*?)</script>', html, re.S).group(1)
        out = run_page_js("run", ids=re.findall(r'id="([^"]+)"', html), data={"lb-run-data": data},
                          click="lb-withdraw", prompts=["me@x", "why"], fetch="{}", reject_post=True)
        self.assertEqual((len(out["fetches"]), out["reloads"], out["disabled"]["lb-withdraw"]), (1, 0, False))
        self.assertIn("저장됐는지 알 수 없으니", out["alerts"][0])

    def test_withdraw_attaches_without_copy_button(self):
        """With the command omitted there is no copy button; the page script must still wire withdraw."""
        m = build_manifest(run_id=rid())
        m["run"]["argv"] = ["--download-logs"] * (repro.MAX_ARGS + 1)
        self.post(build_envelope(m))
        html = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        self.assertIn('id="lb-withdraw"', html)
        self.assertNotIn('id="repro-copy"', html)
        data = re.search(r'id="lb-run-data">(.*?)</script>', html, re.S).group(1)
        out = run_page_js("run", ids=re.findall(r'id="([^"]+)"', html), data={"lb-run-data": data})
        self.assertEqual(out["listeners"].get("lb-withdraw"), ["click"])

    def test_board_script_says_out_of_range(self):
        """A per-query median past float range is an exact JSON integer that parses as Infinity; the board row
        prints "out of range" for it, as the server-rendered pages do."""
        self.submit(elapsed=lambda q, r, e: 10 ** 400 if (q, e) == (1, "xflare") else 1000)
        board = self.board("?metric=q1")
        self.assertGreater(board["rows"][0]["value"]["xflare_ms"], 10 ** 399)
        out = run_page_js("board", data=self.page_data(),
                          fetch=self.c.get("/api/leaderboard/board?metric=q1").get_data(as_text=True))
        self.assertIn("out of range", out["texts"])


def nested(levels: int) -> str:
    """JSON text of `levels` nested arrays: each array is one level."""
    return "[" * levels + "]" * levels


class TestNestingDepth(WebCase):
    """Any JSON value in a body is at most 64 levels deep (the envelope itself is level 1); deeper is a 400 before
    anything is stored, wherever the depth sits."""

    def body(self, where: str, levels: int):
        """An envelope (or, for "bare", a manifest) whose total depth is `levels`, reached under `where`."""
        m = build_manifest(run_id=rid())
        doc, base = build_envelope(m), {"envelope": 1, "manifest": 2, "resources": 4, "bare": 1}[where]
        target = {"envelope": doc, "manifest": m, "resources": m["run"]["resources"], "bare": m}[where]
        target["x_deep"] = "__DEEP__"
        text = json.dumps(m if where == "bare" else doc).replace('"__DEEP__"', nested(levels - base))
        return m["run_id"], text

    def post_raw(self, text, query=""):
        return self.c.post(SUBMIT + query, data=text, content_type="application/json")

    def test_depth_65_rejected_everywhere(self):
        for where in ("envelope", "manifest", "resources"):
            _, text = self.body(where, 65)
            for query in ("", "?dry_run=1"):
                r = self.post_raw(text, query)
                self.assertEqual(r.status_code, 400, (where, query))
                self.assertTrue(r.is_json)
                self.assertTrue(r.get_json()["errors"])
        _, bare = self.body("bare", 65)
        self.assertEqual(self.post_raw(bare, "?dry_run=1").status_code, 400)
        self.assertEqual(os.listdir(self.data), [])            # nothing stored, not even the DB file
        run_id = self.submit()
        r = self.c.post(f"/api/leaderboard/runs/{run_id}/withdraw",
                        data='{"actor": "a", "reason": "r", "x": ' + nested(64) + "}", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.c.get(f"/api/leaderboard/runs/{run_id}").get_json()["withdrawn"])

    def test_depth_64_accepted_end_to_end(self):
        ids = []
        for where in ("envelope", "manifest", "resources"):
            run_id, text = self.body(where, 64)
            self.assertEqual(self.post_raw(text, "?dry_run=1").status_code, 200, where)
            self.assertEqual(self.post_raw(text).status_code, 201, where)
            ids.append(run_id)
        _, bare = self.body("bare", 64)
        self.assertEqual(self.post_raw(bare, "?dry_run=1").status_code, 200)
        for run_id in ids:
            for u in (f"/api/leaderboard/runs/{run_id}", f"{LB}/run/{run_id}", f"/api/leaderboard/runs/{run_id}/raw"):
                self.assertEqual(self.c.get(u).status_code, 200, u)
        for u in ("/api/leaderboard/board", "/api/leaderboard/runs", f"{LB}/compare?runs={','.join(ids)}", LB):
            self.assertEqual(self.c.get(u).status_code, 200, u)
        r = self.c.post(f"/api/leaderboard/runs/{ids[0]}/withdraw",
                        data='{"actor": "a", "reason": "r", "x": ' + nested(63) + "}", content_type="application/json")
        self.assertEqual(r.status_code, 200)


class TestDisplayRules(WebCase):
    """What the pages and summaries show of accepted values: declared fields only, cut labels, finite numbers."""

    def test_unknown_resource_keys_not_rendered(self):
        m = build_manifest(run_id=rid())
        m["run"]["resources"]["x_ext"] = {"a": [1, {"b": "nested"}]}
        m["run"]["resources"]["x_long"] = "z" * 100_000
        self.post(build_envelope(m))
        html = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        self.assertIn("executors=4", html)
        self.assertNotIn("x_ext", html)
        self.assertNotIn("x_long", html)
        resources = self.c.get(f"/api/leaderboard/runs/{m['run_id']}").get_json()["manifest"]["run"]["resources"]
        self.assertEqual(resources["x_ext"], {"a": [1, {"b": "nested"}]})

    def test_filter_echo_whole_option_label_cut(self):
        """The board echoes the requested filter whole; the page labels the option cut and sends it whole."""
        self.submit()
        wanted = "s" * 2000
        board = self.c.get("/api/leaderboard/board?submitter=" + wanted)
        self.assertEqual(board.get_json()["filter"]["submitter"], wanted)
        out = run_page_js("board", data=self.page_data(),
                          fetch=board.get_data(as_text=True), slots=["submitter"])
        self.assertIn(wanted[:512] + "…", out["texts"])
        self.assertNotIn(wanted, out["texts"])
        self.assertIn(facet_token(wanted), out["values"])          # the option selects by token

    def test_compare_delta_never_inf(self):
        small = self.submit(elapsed=lambda q, r, e: 1)
        big = self.submit(elapsed=lambda q, r, e: 10 ** 307 if e == "xflare" else 1)
        html = self.c.get(f"{LB}/compare?runs={small},{big}").get_data(as_text=True)
        self.assertNotIn("inf%", html)
        self.assertNotIn("nan%", html)
        self.assertIn("out of range", html)

    def test_withdrawal_cut_in_summaries(self):
        run_id = self.submit()
        reason = "r" * 2000
        r = self.c.post(f"/api/leaderboard/runs/{run_id}/withdraw", json={"actor": "a@x", "reason": reason})
        self.assertEqual(r.get_json()["withdrawal"]["reason"], reason[:512] + "…")
        listed = self.c.get("/api/leaderboard/runs?include_withdrawn=1").get_json()[0]
        self.assertEqual(listed["withdrawal"]["reason"], reason[:512] + "…")
        self.assertEqual(self.c.get(f"/api/leaderboard/runs/{run_id}").get_json()["withdrawal"]["reason"], reason)

    def test_toml_diff_char_budget(self):
        """One line per side over 64 KiB: no diff, a small page."""
        m = build_manifest(run_id=rid())
        t = m["run"]["xflare_toml"]
        t["input_text"], t["rendered_text"] = "a" * 1_000_000, "a" * 999_999 + "b"
        t["input_sha256"] = hashlib.sha256(t["input_text"].encode()).hexdigest()
        t["rendered_sha256"] = hashlib.sha256(t["rendered_text"].encode()).hexdigest()
        self.post(build_envelope(m))
        r = self.c.get(f"{LB}/run/{m['run_id']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("too large to diff", r.get_data(as_text=True))
        self.assertLess(len(r.data), 512 * 1024)

    def test_bad_json_message_has_one_prefix(self):
        r = self.c.post(SUBMIT, data=b"{not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        msg = r.get_json()["errors"][0]
        self.assertTrue(msg.startswith("invalid JSON"), msg)
        self.assertEqual(msg.count("invalid JSON"), 1, msg)


class TestHeroFallback(WebCase):
    """No Total-eligible run under the filter: the hero is the tab's fastest run in any configuration, flagged and
    announced; the board rows stay filtered, and nothing crosses SF tabs."""

    def test_board_falls_back_and_rows_stay_filtered(self):
        slow_1x6 = self.submit(executors=1, elapsed=flat_elapsed(3000))
        fast_2x12 = self.submit(executors=2, executor_cores=12, elapsed=flat_elapsed(2000))
        self.submit(executors=4, record_answers=True)                     # 4×6 but never Total-eligible
        b = self.board()
        self.assertEqual(b["filter"], {"executors": "4", "executor_cores": "6"})
        self.assertTrue(b["hero_fallback"])
        self.assertEqual(b["hero"]["run_id"], fast_2x12)
        self.assertEqual(b["rows"], [])
        other = self.board("?executors=1&executor_cores=6")
        self.assertEqual((other["hero"]["run_id"], other["hero_fallback"]), (slow_1x6, False))
        self.assertTrue(self.board("?executors=8")["hero_fallback"])
        sf1000 = self.board("?sf=1000")                                   # the fallback never crosses tabs
        self.assertEqual((sf1000["hero"], sf1000["hero_fallback"]), (None, False))

        # A 4×6 run, even a slower one, takes the hero back from the fallback.
        four = self.submit(elapsed=flat_elapsed(5000))
        b = self.board()
        self.assertEqual((b["hero"]["run_id"], b["hero_fallback"]), (four, False))
        self.assertEqual([r["run_id"] for r in b["rows"]], [four])

    def test_no_eligible_run_anywhere(self):
        self.submit(record_answers=True)
        b = self.board()
        self.assertEqual((b["hero"], b["hero_fallback"]), (None, False))

    def test_hero_label_is_cut(self):
        m = build_manifest(run_id=rid(), executors=2)
        m["build"]["bundle"]["resolved"] = "b" * 5000
        self.post(build_envelope(m))
        label = self.board()["hero"]["config_label"]                   # the fallback hero
        self.assertTrue(label.endswith("…"))
        self.assertLessEqual(len(label), 513)

    def test_board_page_notice(self):
        self.submit(executors=2, executor_cores=12, elapsed=flat_elapsed(2000))
        page_data = self.page_data()
        board = self.c.get("/api/leaderboard/board").get_data(as_text=True)
        label = json.loads(board)["hero"]["config_label"]
        with self.subTest("page script"):
            out = run_page_js("board", data=page_data, fetch=board)
            self.assertEqual(out["text"]["hero-notice"], "no 4×6 result yet")
            self.assertEqual(out["text"]["hero-config"], label)
            other = self.c.get("/api/leaderboard/board?executors=8").get_data(as_text=True)
            self.assertEqual(run_page_js("board", data=page_data, fetch=other)["text"]["hero-notice"],
                             "no result for this filter yet")
        self.submit(elapsed=flat_elapsed(5000))
        normal = self.c.get("/api/leaderboard/board").get_data(as_text=True)
        with self.subTest("page script"):
            self.assertEqual(run_page_js("board", data=page_data, fetch=normal)["text"]["hero-notice"], "")
        html = self.c.get(LB).get_data(as_text=True)
        self.assertLess(html.index('id="hero-notice"'), html.index('id="hero-body"'))


class TestWrongAnswersShown(WebCase):
    """A wrong answer in a failed or aborted repeat disqualifies the query; the run page says why."""

    def test_failed_repeat_error_reason_on_run_page(self):
        m = build_manifest(run_id=rid())
        rep = m["repeats"][2]
        rep["status"] = "failed"
        q5 = next(q for q in rep["queries"] if q["id"] == 5)
        q5["engines"]["xflare"].update(status="error", elapsed_ms=None, error="boom")
        q7 = next(q for q in rep["queries"] if q["id"] == 7)
        q7["engines"]["xflare"]["verify"] = {"against": "answers", "status": "mismatch", "detail": "row 1"}
        self.post(build_envelope(m))
        reasons = self.c.get(f"/api/leaderboard/runs/{m['run_id']}").get_json()["derived"]["ineligible_reasons"]
        self.assertTrue(any("failed repeat 3" in r for r in reasons["q5"]), reasons)
        self.assertTrue(any("failed repeat 3" in r for r in reasons["q7"]), reasons)
        html = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        for r in reasons["q5"] + reasons["q7"]:
            self.assertIn(r, html)


# Drives the board script step by step against a DOM stub and a Chart stub. Every fetch stays pending until a step
# answers it ({"answer": i, "with": key} resolves the i-th request with responses[key], with meta[key]'s status and
# headers, else 200 and the default headers, or rejects it when meta[key] has "reject"); {"click": text} clicks the
# last button made with that text (a detached one too); {"fire": id} clicks that element; {"open": id} opens a
# <details>; {"toggle": id} flips a checkbox. cfg.search is the page's query string, cfg.prompts answers prompt() in
# turn. Prints what the page ended up showing.
_BOARD_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const cfg = JSON.parse(fs.readFileSync(0, 'utf8'));
const byId = {}, all = [], pending = [], charts = [], problems = [];
let lastUrl = '';
class Element {
  constructor(tag, id) {
    Object.assign(this, { tagName: tag, id, children: [], dataset: {}, hidden: false, value: '', className: '',
                          href: '', checked: false, on: {}, textContent: (cfg.data || {})[id] || '' });
    this.style = { setProperty() {} };
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    all.push(this);
  }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...c) { this.children = c; }
  setAttribute() {}
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }
  fire(t) { for (const f of this.on[t] || []) f({ target: this }); }
  insertRow() { return this.appendChild(new Element('tr')); }
  insertCell() { return this.appendChild(new Element('td')); }
}
global.Node = Element;
global.document = {
  body: { dataset: { page: 'board' } },
  getElementById(id) { return byId[id] = byId[id] || new Element('div', id); },
  querySelector(sel) { return byId[sel] = byId[sel] || new Element('div'); },
  querySelectorAll() { return []; },
  createElement(tag) { return new Element(tag); },
};
global.location = { search: cfg.search || '', pathname: '/b/xflare_leaderboard' };
global.history = { replaceState(a, b, u) { lastUrl = u; } };
global.fetch = (url, init) => new Promise((res, rej) => pending.push({ url, init, res, rej }));
const alerts = [], prompts = [...(cfg.prompts || [])];
global.prompt = () => (prompts.length ? prompts.shift() : null);
global.alert = m => { alerts.push(String(m)); };
global.Chart = class {
  constructor(canvas, conf) {
    this.options = conf.options; this.datasets = conf.data.datasets; this.destroyed = false; charts.push(this);
  }
  update() { if (this.destroyed) problems.push('update on a destroyed chart'); }
  destroy() { this.destroyed = true; }
};
vm.runInThisContext(fs.readFileSync(cfg.js, 'utf8'));
const tick = () => new Promise(r => setTimeout(r, 5));
(async () => {
  for (const step of cfg.steps) {
    await tick();
    if (step.answer !== undefined) {
      const text = cfg.responses[step.with];
      const meta = cfg.meta[step.with] || {};
      const status = meta.status || 200, headers = meta.headers || cfg.headers;
      if (meta.reject) pending[step.answer].rej(new TypeError('Failed to fetch'));
      else pending[step.answer].res({ ok: status < 300, status, json: async () => JSON.parse(text),
                                 headers: { get: k => headers[k.toLowerCase()] || null } });
    } else if (step.click) {
      all.filter(e => e.tagName === 'button' && e.textContent === step.click).pop().fire('click');
    } else if (step.fire) {
      byId[step.fire].fire('click');
    } else if (step.open) {
      byId[step.open].open = true;
      byId[step.open].fire('toggle');
    } else if (step.toggle) {
      const e = byId[step.toggle];
      e.checked = !e.checked;
      e.fire('change');
    }
  }
  await tick(); await tick();
  const tabs = byId['lb-sf-tabs'].children;
  const links = e => [e.href, ...e.children.flatMap(links)].filter(Boolean);
  console.log(JSON.stringify({
    lastUrl, activeTab: (tabs.find(b => b.className.includes('active')) || {}).textContent,
    heroLink: byId['hero-link'].href, heroEmptyHidden: byId['hero-empty'].hidden,
    status: byId['lb-status'].textContent, statusHidden: byId['lb-status'].hidden,
    boardLinks: links(byId['#lb-board tbody']),
    logListeners: (byId['hero-log'].on.change || []).length,
    charts: charts.map(c => ({ destroyed: c.destroyed, y: c.options.scales.y.type })), problems,
    datasets: charts.length ? charts[charts.length - 1].datasets.map(d => ({ label: d.label, data: d.data })) : [],
    requests: pending.map(p => p.url),
    posts: pending.filter(p => p.init && p.init.method === 'POST').map(p => ({ url: p.url, body: JSON.parse(p.init.body) })),
    alerts, events: byId['hero-target-events'].children.map(c => c.textContent),
    target: byId['hero-target'].textContent, targetHidden: byId['hero-target'].hidden }));
})();
"""


class TestBoardScript(WebCase):
    """The board page script under controlled timing: stale answers never win, an answer for another script never
    renders, one log toggle drives the chart."""

    def drive(self, steps, responses, meta=None, search="", prompts=()):
        """Answers name the page's own script unless meta[key] gives other headers (names in lower case)."""
        node = page_script_node()
        data = self.page_data()
        cfg = {"js": os.path.join(ROOT, "static", "leaderboard.js"), "data": data, "steps": steps,
               "responses": responses, "meta": meta or {}, "search": search, "prompts": list(prompts),
               "headers": {"x-leaderboard-script": json.loads(data["lb-page-data"])["script"]}}
        out = subprocess.run([node, "-e", _BOARD_HARNESS], input=json.dumps(cfg), capture_output=True, text=True,
                             timeout=30)
        if out.returncode:
            raise AssertionError("leaderboard.js failed:\n" + out.stderr[-2000:])
        return json.loads(out.stdout)

    def two_tabs(self):
        hundred = self.submit()
        thousand = self.submit(sf="1000")
        return hundred, thousand, {"100": self.c.get("/api/leaderboard/board?sf=100").get_data(as_text=True),
                                   "1000": self.c.get("/api/leaderboard/board?sf=1000").get_data(as_text=True)}

    def test_stale_answer_does_not_win(self):
        """SF-1000 then SF-100 clicked; SF-100's answer arrives first, SF-1000's last: the page stays on SF-100."""
        hundred, _, responses = self.two_tabs()
        out = self.drive([{"answer": 0, "with": "100"}, {"click": "SF-1000 · snappy"}, {"click": "SF-100 · snappy"},
                          {"answer": 2, "with": "100"}, {"answer": 1, "with": "1000"}], responses)
        self.assertIn("sf=1000", out["requests"][1])
        self.assertEqual(out["activeTab"], "SF-100 · snappy")
        self.assertTrue(out["heroLink"].endswith(hundred), out)
        self.assertIn("sf=100", out["lastUrl"])

    def test_one_log_toggle_listener(self):
        _, _, responses = self.two_tabs()
        out = self.drive([{"answer": 0, "with": "100"}, {"click": "SF-100 · snappy"}, {"answer": 1, "with": "100"},
                          {"click": "SF-100 · snappy"}, {"answer": 2, "with": "100"}, {"toggle": "hero-log"}], responses)
        self.assertEqual(out["logListeners"], 1)
        self.assertEqual(out["problems"], [])
        self.assertEqual(out["charts"][-1], {"destroyed": False, "y": "logarithmic"})

    def test_answer_for_another_script_keeps_the_board(self):
        """SF-100 shown, then SF-1000 clicked (or refresh) and answered by a service with another script, whatever the
        status or body, or by a 2xx from a service older than the check: the SF-100 hero stays and the page asks for
        a reload. The next answer that names this script renders as usual."""
        hundred, thousand, responses = self.two_tabs()
        responses["html"] = "<!doctype html><title>500 Internal Server Error</title>"
        other = {"x-leaderboard-script": "0" * 12}
        meta = {"200": {"headers": other}, "400": {"status": 400, "headers": other},
                "503": {"status": 503, "headers": other}, "html": {"status": 500, "headers": other},
                "no header": {"headers": {}}}
        responses.update({"200": responses["1000"], "400": '{"errors": ["metric: total or q1..q22"]}',
                          "503": '{"errors": ["leaderboard storage unavailable"]}', "no header": responses["1000"]})
        reload = "서버가 다른 버전으로 바뀌었습니다. 페이지를 새로고침하세요."
        shown = self.drive([{"answer": 0, "with": "100"}], responses)["boardLinks"]
        self.assertIn(f"{LB}/run/{hundred}", shown)
        for key in ("200", "400", "503", "html", "no header"):
            for action in ({"click": "SF-1000 · snappy"}, {"fire": "lb-refresh"}):
                with self.subTest(answer=key, action=action):
                    out = self.drive([{"answer": 0, "with": "100"}, action, {"answer": 1, "with": key}],
                                     responses, meta)
                    self.assertTrue(out["heroLink"].endswith(hundred), out)
                    self.assertTrue(out["heroEmptyHidden"], out)
                    self.assertEqual(out["boardLinks"], shown)
                    self.assertEqual((out["status"], out["statusHidden"]), (reload, False))
                    self.assertEqual(out["charts"][-1]["destroyed"], False)
        out = self.drive([{"answer": 0, "with": "100"}, {"click": "SF-1000 · snappy"}, {"answer": 1, "with": "200"},
                          {"click": "SF-1000 · snappy"}, {"answer": 2, "with": "1000"}], responses, meta)
        self.assertTrue(out["heroLink"].endswith(thousand), out)
        self.assertTrue(out["statusHidden"], out)

    def target_tabs(self):
        """SF-100 with two runs pinned in turn (the second is the target now) and SF-1000 with one run."""
        first, second = self.submit(elapsed=flat_elapsed(500, velox=2500)), self.submit(elapsed=flat_elapsed(400))
        self.submit(sf="1000")
        self.pin_target(first, 0, goal=2.0)
        self.pin_target(second, 1)
        get = lambda u: self.c.get(u).get_data(as_text=True)
        return first, second, {"100": get("/api/leaderboard/board?sf=100"), "1000": get("/api/leaderboard/board?sf=1000"),
                               "hist": get("/api/leaderboard/targets/100/snappy"), "pin": '{"revision": 3}'}

    def test_target_history_pins_an_earlier_target_again(self):
        """A8: the history lists every change; "다시 지정" posts that earlier target and goal at the revision the
        history showed, then reloads the board."""
        first, second, responses = self.target_tabs()
        out = self.drive([{"answer": 0, "with": "100"}, {"open": "hero-target-history"}, {"answer": 1, "with": "hist"},
                          {"click": "다시 지정"}, {"answer": 2, "with": "pin"}], responses, prompts=["me@x"])
        self.assertEqual(len(out["events"]), 2, out)
        self.assertEqual(out["posts"], [{"url": "/api/leaderboard/targets/100/snappy",
                                         "body": {"actor": "me@x", "expected_revision": 2, "run_id": first,
                                                  "goal": 2.0}}])
        self.assertIn("sf=100", out["requests"][3])                         # the board reloads after the change
        self.assertEqual(out["alerts"], [])
        refused = self.drive([{"answer": 0, "with": "100"}, {"open": "hero-target-history"}, {"answer": 1, "with": "hist"},
                              {"click": "다시 지정"}, {"answer": 2, "with": "stale"}],
                             {**responses, "stale": '{"errors": ["the target changed meanwhile: reload and try again"]}'},
                             meta={"stale": {"status": 409}}, prompts=["me@x"])
        self.assertEqual(len(refused["alerts"]), 1)
        self.assertIn("HTTP 409", refused["alerts"][0])
        self.assertEqual(len(refused["requests"]), 3)                         # nothing reloads after a refusal

    def test_open_history_refetches_only_on_a_new_revision(self):
        """A8: with the history open, a board answer at a new revision fetches it again; one at the same revision
        (another metric) does not. The target line names the pinned run and who set it."""
        _, second, responses = self.target_tabs()
        newer = json.loads(responses["100"])
        newer["target_pin"]["revision"] += 1
        responses["newer"] = json.dumps(newer)
        out = self.drive([{"answer": 0, "with": "100"}, {"open": "hero-target-history"}, {"answer": 1, "with": "hist"},
                          {"click": "Q1"}, {"answer": 2, "with": "100"}, {"click": "Q2"}, {"answer": 3, "with": "newer"}],
                         responses)
        targets = [u for u in out["requests"] if "/api/leaderboard/targets/" in u]
        self.assertEqual(len(targets), 2, out["requests"])                    # the opening, then the new revision
        self.assertFalse(out["targetHidden"])

    def test_target_line_wording(self):
        """A8: what the target line says without a target, for a link's own target and for ?target=none."""
        first, second, responses = self.target_tabs()
        never = json.loads(responses["1000"])
        self.assertEqual(never["target_pin"]["revision"], 0)
        out = run_page_js("board", data=self.page_data(), fetch=responses["1000"])
        self.assertTrue(out["text"]["hero-target"].startswith("이 탭에 target 이 없습니다"))
        none = run_page_js("board", data=self.page_data(),
                           fetch=self.c.get("/api/leaderboard/board?sf=100&target=none").get_data(as_text=True))
        self.assertEqual(none["text"]["hero-target"], "이 보기는 target 없이 같은 run 의 velox 와 비교합니다.")
        link = run_page_js("board", data=self.page_data(),
                           fetch=self.c.get(f"/api/leaderboard/board?sf=100&target={first}").get_data(as_text=True))
        self.assertIn(" · 이 링크에서만 (저장된 target 아님)", link["texts"])
        pinned = run_page_js("board", data=self.page_data(), fetch=responses["100"])
        self.assertTrue(any(t.endswith(" 에 지정") and "a@x 가 " in t for t in pinned["texts"]), pinned["texts"])
        self.assertIn(f"{LB}/run/{second}", pinned["hrefs"])

    def test_hero_chart_draws_the_target_velox(self):
        """A8: the hero chart's bars are the hero's xflare, the target's velox and (faint) the hero's own velox."""
        first, second, responses = self.target_tabs()
        self.pin_target(first, 2)                                             # a target that is not the hero
        responses["100"] = self.c.get("/api/leaderboard/board?sf=100").get_data(as_text=True)
        board = json.loads(responses["100"])
        self.assertEqual((board["hero"]["run_id"], board["target"]["run_id"]), (second, first))
        self.assertNotEqual(board["target"]["per_query"], [p["velox_ms"] for p in board["hero"]["per_query"]])
        out = self.drive([{"answer": 0, "with": "100"}], responses)
        self.assertEqual([d["label"] for d in out["datasets"]], ["xflare", "target velox", "velox (same run)"])
        self.assertEqual(out["datasets"][1]["data"], board["target"]["per_query"])
        self.assertEqual(out["datasets"][2]["data"], [p["velox_ms"] for p in board["hero"]["per_query"]])

    def test_target_history_of_another_tab_never_acts(self):
        """A8: after a tab change, a late history answer (or failure) of the old tab draws nothing, and a button
        the old tab drew posts nothing."""
        _, _, responses = self.target_tabs()
        late = self.drive([{"answer": 0, "with": "100"}, {"open": "hero-target-history"},
                           {"click": "SF-1000 · snappy"}, {"answer": 2, "with": "1000"}, {"answer": 1, "with": "hist"}],
                          responses)
        self.assertEqual(late["events"], [])
        failed = self.drive([{"answer": 0, "with": "100"}, {"open": "hero-target-history"},
                             {"click": "SF-1000 · snappy"}, {"answer": 2, "with": "1000"}, {"answer": 1, "with": "fail"}],
                            {**responses, "fail": '{"errors": ["x"]}'}, meta={"fail": {"status": 503}})
        self.assertEqual(failed["events"], [])
        stale = self.drive([{"answer": 0, "with": "100"}, {"open": "hero-target-history"}, {"answer": 1, "with": "hist"},
                            {"click": "SF-1000 · snappy"}, {"click": "다시 지정"}, {"answer": 2, "with": "1000"}],
                           responses, prompts=["me@x"])
        self.assertEqual(stale["posts"], [])

    def test_target_link_is_passed_on_and_dropped_on_a_tab_change(self):
        """A8: ?target= reaches the board API; another tab, or a malformed value, loads without it."""
        first, _, responses = self.target_tabs()
        out = self.drive([{"answer": 0, "with": "100"}, {"click": "SF-1000 · snappy"}],
                         responses, search=f"?target={first}")
        self.assertIn(f"target={first}", out["requests"][0])
        self.assertNotIn("target=", out["requests"][1])
        bad = self.drive([{"answer": 0, "with": "bad"}, {"answer": 1, "with": "100"}],
                         {**responses, "bad": '{"errors": ["target: a run id or none"]}'},
                         meta={"bad": {"status": 400}}, search="?sf=100&codec=snappy&target=nope")
        self.assertIn("target=nope", bad["requests"][0])
        self.assertNotIn("target=", bad["requests"][1])
        self.assertTrue(bad["statusHidden"], bad)

    def test_answers_from_this_script_keep_their_own_paths(self):
        """The check stands only in front of answers for another script: a failed fetch keeps the board and says so,
        and an error that names this script, or a non-2xx without the header, clears the hero and board and shows its
        errors."""
        hundred, _, responses = self.two_tabs()
        responses.update({"down": "", "400": '{"errors": ["metric: total or q1..q22"]}',
                          "503": '{"errors": ["leaderboard storage unavailable"]}'})
        meta = {"down": {"reject": True}, "400": {"status": 400}, "503": {"status": 503, "headers": {}}}
        shown = self.drive([{"answer": 0, "with": "100"}], responses)["boardLinks"]
        out = self.drive([{"answer": 0, "with": "100"}, {"fire": "lb-refresh"}, {"answer": 1, "with": "down"}],
                         responses, meta)
        self.assertEqual((out["status"], out["boardLinks"]), ("board 를 불러오지 못했습니다.", shown))
        self.assertTrue(out["heroLink"].endswith(hundred), out)
        for key, error in (("400", "metric: total or q1..q22"), ("503", "leaderboard storage unavailable")):
            with self.subTest(answer=key):
                out = self.drive([{"answer": 0, "with": "100"}, {"click": "SF-1000 · snappy"},
                                  {"answer": 1, "with": key}], responses, meta)
                self.assertEqual(out["status"], error)
                self.assertFalse(out["heroEmptyHidden"], out)
                self.assertNotIn(f"{LB}/run/{hundred}", out["boardLinks"])


class TestTarget(WebCase):
    """A8: a tab's target run (its velox side, any configuration) and goal speedup: set through the API at the
    revision the caller saw, every change kept, compared with on the board, overridable for one link."""

    T = "/api/leaderboard/targets/100/snappy"

    def set(self, run_id, revision=0, goal=None, status=200, **headers):
        return self.pin_target(run_id, revision, goal, status, **headers)

    def two_runs(self):
        """A 8×6 run to compare with (off the default filter) and a 4×6 run that is the hero."""
        return (self.submit(executors=8, elapsed=flat_elapsed(500, velox=2000)),
                self.submit(elapsed=flat_elapsed(400, velox=3000)))

    def test_set_change_clear_and_history(self):
        a, b = self.two_runs()
        pin = self.set(a, goal=2.5)
        self.assertEqual((pin["run_id"], pin["goal"], pin["revision"], pin["updated_by"]), (a, 2.5, 1, "a@x"))
        self.assertEqual(self.set(a, revision=1, goal=2.5)["revision"], 1)          # the same: no new revision
        stale = self.set(b, revision=0, status=409)
        self.assertEqual((stale["current"]["run_id"], stale["current"]["revision"]), (a, 1))
        self.set(b, revision=1)
        self.set(None, revision=2)
        state = self.c.get(self.T).get_json()
        self.assertEqual((state["run_id"], state["revision"]), (None, 3))
        self.assertEqual([(e["before_run_id"], e["after_run_id"], e["after_goal"]) for e in state["history"]],
                         [(b, None, None), (a, b, None), (None, a, 2.5)])

    def test_refusals(self):
        a = self.submit()
        self.set(self.submit(engines=("xflare",)), status=400)                    # no velox total
        self.set(self.submit(sf="1000"), status=400)                              # another tab
        self.set(rid(), status=404)
        for body, field in (({"actor": " ", "expected_revision": 0, "run_id": a}, "actor"),
                            ({"actor": "x", "expected_revision": -1, "run_id": a}, "expected_revision"),
                            ({"actor": "x", "expected_revision": True, "run_id": a}, "expected_revision"),
                            ({"actor": "x", "expected_revision": 0}, "run_id"),
                            ({"actor": "x", "expected_revision": 0, "run_id": "nope"}, "run_id"),
                            ({"actor": "x", "expected_revision": 0, "run_id": a, "goal": 0}, "goal"),
                            ({"actor": "x", "expected_revision": 0, "run_id": a, "goal": "2"}, "goal"),
                            ({"actor": "x", "expected_revision": 0, "run_id": a, "goal": 1000.5}, "goal"),
                            ({"actor": "x", "expected_revision": 0, "run_id": None, "goal": 2}, "goal")):
            r = self.c.post(self.T, json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertTrue(r.get_json()["errors"][0].startswith(field + ":"), r.get_json())
        plain = self.c.post(self.T, data='{"actor": "x", "expected_revision": 0, "run_id": null}',
                            content_type="text/plain")
        self.assertEqual(plain.status_code, 415)
        self.set(a, status=403, Origin="http://evil.example")
        self.set(a, Origin="http://localhost")                                    # this site (the test client's)
        self.assertEqual(self.set(a, revision=1, goal=1000)["goal"], 1000)        # the bound itself is allowed
        self.assertEqual(self.c.post("/api/leaderboard/targets/x1/snappy", json={}).status_code, 404)
        self.assertEqual(self.c.get("/api/leaderboard/targets/100/lz4").status_code, 404)
        self.c.post(f"/api/leaderboard/runs/{a}/withdraw", json={"actor": "x", "reason": "y"})
        self.assertTrue(self.set(a, revision=2, goal=3, status=400)["errors"][0].endswith("withdrawn"))

    def test_board_compares_with_the_target(self):
        target, hero = self.two_runs()
        board = self.board()
        self.assertEqual((board["target"], board["hero"]["vs_target"], board["target_pin"]["revision"]),
                         (None, None, 0))
        self.set(target, goal=1.5)
        board = self.board()
        t = board["target"]
        self.assertEqual((t["run_id"], t["source"], t["goal"], t["problem"]), (target, "pinned", 1.5, None))
        velox, xflare = sum(2000 + q for q in range(1, 23)), sum(400 + q for q in range(1, 23))
        self.assertEqual((t["velox_ms"], t["per_query"][0]), (velox, 2001))
        self.assertEqual(board["hero"]["run_id"], hero)
        self.assertAlmostEqual(board["hero"]["vs_target"], velox / xflare)
        self.assertAlmostEqual(board["rows"][0]["value"]["vs_target"], velox / xflare)
        q1 = self.board("?metric=q1")
        self.assertAlmostEqual(q1["rows"][0]["value"]["vs_target"], 2001 / 401)
        self.assertEqual(q1["hero"]["vs_target"], board["hero"]["vs_target"])        # the hero keeps the totals
        # Pinning a ranked run (here the hero itself) changes no rank, count or hero, on any metric or view.
        plain = {m: self.board(f"?metric={m}&target=none") for m in ("total", "q1")}
        self.set(hero, revision=1)
        for m, before in plain.items():
            for query in (f"?metric={m}", f"?metric={m}&target={target}"):
                after = self.board(query)
                self.assertEqual([(r["rank"], r["run_id"], r["value"]["speedup"]) for r in after["rows"]],
                                 [(r["rank"], r["run_id"], r["value"]["speedup"]) for r in before["rows"]], query)
                self.assertEqual((after["eligible_count"], after["hero"]["run_id"], after["hero_fallback"]),
                                 (before["eligible_count"], before["hero"]["run_id"], before["hero_fallback"]), query)
        self.assertEqual(self.board()["target"]["run_id"], hero)
        self.set(target, revision=2, goal=1.5)
        link = self.board(f"?target={hero}")["target"]
        self.assertEqual((link["run_id"], link["source"], link["goal"]), (hero, "link", None))
        self.assertIsNone(self.board("?target=none")["target"])
        self.assertTrue(self.board("?target=nope", status=400)["errors"][0].startswith("target:"))
        self.c.post(f"/api/leaderboard/runs/{target}/withdraw", json={"actor": "x", "reason": "gone"})
        gone = self.board()
        self.assertEqual((gone["target"]["problem"], gone["hero"]["vs_target"]), ("withdrawn", None))
        self.assertIsNone(gone["rows"][0]["value"]["vs_target"])

    def test_board_page_shows_the_target(self):
        target, _ = self.two_runs()
        before = run_page_js("board", data=self.page_data(),
                             fetch=self.c.get("/api/leaderboard/board").get_data(as_text=True))
        self.assertEqual((before["text"]["hero-velox-label"], before["text"]["lb-col-speedup"]),
                         ("Velox total (same run)", "Speedup"))
        self.assertTrue(before["hidden"]["hero-goal"])
        self.set(target, goal=1.5)
        out = run_page_js("board", data=self.page_data(),
                          fetch=self.c.get("/api/leaderboard/board").get_data(as_text=True))
        self.assertEqual((out["text"]["hero-velox-label"], out["text"]["hero-speedup-label"],
                          out["text"]["lb-col-speedup"]), ("Target velox total", "Speedup vs target", "vs target"))
        self.assertEqual((out["text"]["hero-goal"], out["hidden"]["hero-goal"]), ("목표 1.50× · 달성", False))
        self.assertIn(f"{LB}/run/{target}", out["hrefs"])
        self.assertIn("lb-goal-met", " ".join(out["classes"]))
        board = self.board()
        fmt_x = lambda v: f"{v:.2f}×"
        self.assertEqual((out["text"]["hero-velox"], out["text"]["hero-speedup"]),
                         (f"{board['target']['velox_ms'] / 1000:.2f} s", fmt_x(board["hero"]["vs_target"])))
        # The speedup cells are the elements titled with the same-run speedup; their text is the vs-target one.
        cells = [(t, x) for t, x in zip(out["titles"], out["texts"]) if t.startswith("같은 run 의 velox 기준 ")]
        self.assertEqual(cells, [(f"같은 run 의 velox 기준 {fmt_x(r['value']['speedup'])}", fmt_x(r["value"]["vs_target"]))
                                 for r in board["rows"]])
        self.assertNotEqual(*{r["run_id"]: (r["value"]["speedup"], r["value"]["vs_target"])
                              for r in board["rows"]}[board["hero"]["run_id"]])
        self.set(target, revision=1, goal=10)
        short = run_page_js("board", data=self.page_data(),
                            fetch=self.c.get("/api/leaderboard/board").get_data(as_text=True))
        reached = round(100 * self.board()["hero"]["vs_target"] / 10)
        self.assertEqual(short["text"]["hero-goal"], f"목표 10.00× · {reached}%")
        self.c.post(f"/api/leaderboard/runs/{target}/withdraw", json={"actor": "x", "reason": "gone"})
        gone = run_page_js("board", data=self.page_data(),
                           fetch=self.c.get("/api/leaderboard/board").get_data(as_text=True))
        board = self.board()
        self.assertEqual((gone["text"]["hero-velox-label"], gone["text"]["lb-col-speedup"]),
                         ("Velox total (same run)", "Speedup"))
        self.assertEqual(gone["text"]["hero-velox"], f"{board['hero']['totals']['velox_ms'] / 1000:.2f} s")
        self.assertIn(fmt_x(board["rows"][0]["value"]["speedup"]), gone["texts"])

    def test_target_problem_shows_without_a_hero(self):
        """A8: a pinned target that no longer qualifies says why, also on a tab whose only Total-eligible run it was."""
        a = self.submit()
        self.set(a)
        self.c.post(f"/api/leaderboard/runs/{a}/withdraw", json={"actor": "x", "reason": "gone"})
        board = self.c.get("/api/leaderboard/board").get_data(as_text=True)
        self.assertIsNone(json.loads(board)["hero"])
        out = run_page_js("board", data=self.page_data(), fetch=board)
        self.assertFalse(out["hidden"]["hero-target"])
        self.assertIn("(withdrawn)", out["text"]["hero-target"])
        self.assertFalse(out["hidden"]["hero-target-history"])

    @staticmethod
    def run_page(page):
        """run_page_js arguments for a run page: its ids, data block and the target buttons' data-* attributes."""
        datasets = {}
        for tag in re.findall(r"<button[^>]*>", page):
            m = re.search(r'id="(lb-target-[a-z]+)"', tag)
            if m:
                datasets[m.group(1)] = {k: html.unescape(v) for k, v in re.findall(r'data-([a-z]+)="([^"]*)"', tag)}
        return {"ids": re.findall(r'id="([^"]+)"', page), "datasets": datasets,
                "data": {"lb-run-data": re.search(r'id="lb-run-data">(.*?)</script>', page, re.S).group(1)}}

    def test_run_page_goal_prompt(self):
        """A8: the goal must be a finite number above 0 and at most 1000 before anything is sent."""
        a = self.submit()
        page = self.run_page(self.c.get(f"{LB}/run/{a}").get_data(as_text=True))
        for goal in ("Infinity", "1e309", "0", "-2", "1001", "two"):
            out = run_page_js("run", click="lb-target-set", prompts=["me@x", goal], fetch="{}", **page)
            self.assertEqual(out["fetches"], [], goal)
            self.assertEqual(len(out["alerts"]), 1, goal)
        out = run_page_js("run", click="lb-target-set", prompts=["me@x", "2.5"], fetch="{}", **page)
        self.assertEqual(out["fetches"], [{"url": "/api/leaderboard/targets/100/snappy", "method": "POST",
                                           "body": {"actor": "me@x", "expected_revision": 0, "run_id": a,
                                                    "goal": 2.5}}])
        self.assertEqual(out["reloads"], 1)
        lost = run_page_js("run", click="lb-target-set", prompts=["me@x", ""], fetch="{}", reject_post=True, **page)
        self.assertEqual((len(lost["fetches"]), lost["reloads"], lost["disabled"]["lb-target-set"]), (1, 0, False))
        self.assertIn("저장됐는지 알 수 없으니", lost["alerts"][0])
        self.set(a)
        out = run_page_js("run", click="lb-target-clear", prompts=["me@x"], fetch="{}",
                          **self.run_page(self.c.get(f"{LB}/run/{a}").get_data(as_text=True)))
        self.assertEqual(out["fetches"][0]["body"], {"actor": "me@x", "expected_revision": 1, "run_id": None,
                                                     "goal": None})

    def test_withdrawn_meanwhile_is_refused(self):
        """A8: a run withdrawn between the request's checks and the write is not pinned."""
        a = self.submit()
        real_get = lb_store.Store.get

        done = []

        def get_then_withdraw(store, run_id):
            row = real_get(store, run_id)
            if not done:                                         # withdraw() reads the row back through get()
                done.append(1)
                store.withdraw(run_id, "x", "meanwhile")
            return row
        with mock.patch.object(lb_store.Store, "get", get_then_withdraw):
            self.assertEqual(self.set(a, status=400)["errors"], ["run_id: withdrawn"])
        self.assertEqual(self.c.get(self.T).get_json()["revision"], 0)

    def test_run_page_badge_and_who_can_be_a_target(self):
        """A8: the target run's page names its tab and goal; a run without a velox total offers no "set" button."""
        a = self.submit()
        self.set(a, goal=2)
        self.assertIn("target · SF-100 · snappy · 목표 2.00×", self.c.get(f"{LB}/run/{a}").get_data(as_text=True))
        page = self.c.get(f"{LB}/run/{self.submit(engines=('xflare',))}").get_data(as_text=True)
        self.assertNotIn('id="lb-target-set"', page)

class TestOneDeploy(WebCase):
    """Static URLs carry the hash taken at start, and every board answer names the board script by it: a board page
    with another script asks for a reload (TestBoardScript drives the open-page paths)."""

    def test_static_urls_carry_the_file_hash(self):
        m = self.submit()
        other = self.submit()
        for url in (LB, f"{LB}/run/{m}", f"{LB}/compare?runs={m},{other}"):
            page = self.c.get(url).get_data(as_text=True)
            refs = re.findall(r'(?:href|src)="(/static/[^"]*)"', page)
            self.assertEqual(sorted(r.split("?")[0] for r in refs),
                             ["/static/leaderboard.css", "/static/leaderboard.js", "/static/style.css"], url)
            for ref in refs:
                path, _, query = ref.partition("?")
                with open(os.path.join(ROOT, path.lstrip("/")), "rb") as f:
                    self.assertEqual(query, "v=" + hashlib.sha256(f.read()).hexdigest()[:12], ref)
                with self.c.get(html.unescape(ref)) as served:
                    self.assertEqual(served.status_code, 200, ref)

    def test_board_names_its_script(self):
        script = json.loads(self.page_data()["lb-page-data"])["script"]
        with open(os.path.join(ROOT, "static", "leaderboard.js"), "rb") as f:
            self.assertEqual(script, hashlib.sha256(f.read()).hexdigest()[:12])
        for query, status in (("", 200), ("?metric=q23", 400)):
            r = self.c.get("/api/leaderboard/board" + query)
            self.assertEqual(r.status_code, status)
            self.assertEqual(r.headers["X-Leaderboard-Script"], script, query)
        self.assertNotIn("X-Leaderboard-Script", self.c.get("/api/leaderboard/runs").headers)
        moved = self.data + ".moved"
        os.rename(self.data, moved)
        try:
            r = self.c.get("/api/leaderboard/board")
        finally:
            os.rename(moved, self.data)
        self.assertEqual((r.status_code, r.headers["X-Leaderboard-Script"]), (503, script))

    def test_hashed_once_at_start(self):
        """A static file that goes missing after the start (a checkout being rewritten) changes no answer; one
        missing at the start stops the start."""
        script = json.loads(self.page_data()["lb-page-data"])["script"]
        empty = tempfile.mkdtemp(prefix="lb-static-")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        self.app.static_folder = empty
        r = self.c.get("/api/leaderboard/board")
        self.assertEqual((r.status_code, r.headers["X-Leaderboard-Script"]), (200, script))
        self.assertIn(f"/static/leaderboard.js?v={script}", self.c.get(LB).get_data(as_text=True))
        with mock.patch.object(lb_app, "HERE", lb_app.Path(empty)), self.assertRaises(FileNotFoundError):
            lb_app.create_app(config(self.data))

    def test_stale_page_asks_for_reload(self):
        hero = self.submit()
        r = self.c.get("/api/leaderboard/board")
        board, header = r.get_data(as_text=True), {"X-Leaderboard-Script": r.headers["X-Leaderboard-Script"]}
        fresh = run_page_js("board", data=self.page_data(), fetch=board, headers=header)
        self.assertTrue(fresh["hidden"]["lb-status"])
        self.assertIn("SF-100 · snappy", fresh["texts"])
        self.assertIn(f"{LB}/run/{hero}", fresh["hrefs"])
        stale = json.loads(self.page_data()["lb-page-data"])
        stale["script"] = "0" * 12
        out = run_page_js("board", data={"lb-page-data": json.dumps(stale)}, fetch=board, headers=header)
        self.assertEqual(out["text"]["lb-status"], "서버가 다른 버전으로 바뀌었습니다. 페이지를 새로고침하세요.")
        self.assertFalse(out["hidden"]["lb-status"])
        self.assertNotIn("SF-100 · snappy", out["texts"])
        self.assertNotIn(f"{LB}/run/{hero}", out["hrefs"])


class TestDisplayRound(WebCase):
    """Facet tokens for long values, server-made notices, numbers that stay JSON, budgets in one place."""

    def test_facet_tokens_and_literals_never_collide(self):
        """One run's bundle is 513 b's, another's is literally the token of that value: every offered option and
        every exact full value selects the right run, on /board and /runs."""
        long_bundle = "b" * 513
        ids = {}
        for bundle in (long_bundle, facet_token(long_bundle)):
            m = build_manifest(run_id=rid())
            m["build"]["bundle"]["resolved"] = bundle
            self.post(build_envelope(m))
            ids[bundle] = m["run_id"]
        b = self.board()
        options = {o["token"]: o["label"] for o in b["facets"]["bundle"]}
        self.assertEqual(set(options), {facet_token(v) for v in ids})
        self.assertEqual(options[facet_token(long_bundle)], "b" * 512 + "…")

        def runs(query):
            return ([r["run_id"] for r in self.board(query)["rows"]],
                    [r["run_id"] for r in self.c.get("/api/leaderboard/runs" + query).get_json()])

        for value, run_id in ids.items():
            self.assertEqual(runs("?bundle_t=" + facet_token(value)), ([run_id], [run_id]), value[:20])
            self.assertEqual(runs("?bundle=" + value), ([run_id], [run_id]), value[:20])
            both = f"?bundle={value}&bundle_t={facet_token(value)}"
            self.assertEqual(runs(both), ([run_id], [run_id]))
        other = facet_token(long_bundle)
        self.assertEqual(runs(f"?bundle={long_bundle}&bundle_t={facet_token(other)}"), ([], []))  # must both match
        self.assertEqual(runs("?bundle_t=h:0000000000000000"), ([], []))
        echo = self.board("?bundle=" + long_bundle)
        self.assertEqual(echo["filter"]["bundle"], long_bundle)
        self.assertEqual(echo["filter_tokens"]["bundle"], facet_token(long_bundle))

    def test_shared_literal_url_selects_the_offered_option(self):
        """A URL with the full literal value selects the existing option instead of adding a second one."""
        long_bundle = "b" * 513
        m = build_manifest(run_id=rid())
        m["build"]["bundle"]["resolved"] = long_bundle
        self.post(build_envelope(m))
        board = self.c.get("/api/leaderboard/board?bundle=" + long_bundle).get_data(as_text=True)
        out = run_page_js("board", data=self.page_data(), fetch=board, slots=["bundle"])
        self.assertEqual(out["texts"].count("b" * 512 + "…"), 1)
        self.assertIn(facet_token(long_bundle), out["values"])

    def test_api_is_strict_json(self):
        """Two Total-eligible runs whose totals are finite but far apart: no Infinity/NaN anywhere in the API."""
        def strict(text):
            return json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        import datetime
        t0 = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
        a = self.submit(started_at=t0, elapsed=lambda q, r, e: 1)
        b = self.submit(started_at=t0 + datetime.timedelta(days=1),
                        elapsed=lambda q, r, e: 5 * 10 ** 306 if e == "xflare" else 1)
        for u in ("/api/leaderboard/board", "/api/leaderboard/runs", f"/api/leaderboard/runs/{a}",
                  f"/api/leaderboard/runs/{b}"):
            r = self.c.get(u)
            self.assertEqual(r.status_code, 200, u)
            strict(r.get_data(as_text=True))

    def test_run_chart_numbers_stay_finite(self):
        m = build_manifest(run_id=rid(), elapsed=lambda q, r, e: 10 ** 400 if (q, e) == (1, "xflare") else 1000)
        self.post(build_envelope(m))
        html = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        chart = json.loads(re.search(r'id="lb-run-data">(.*?)</script>', html, re.S).group(1))
        self.assertIsNone(chart["xflare"][0])
        self.assertEqual(chart["xflare"][1], 1000.0)

    def test_notice_made_by_the_server(self):
        self.submit(executors=2, executor_cores=12)
        self.assertEqual(self.board()["hero_notice"], "no 4×6 result yet")
        self.assertEqual(self.board("?executors=8")["hero_notice"], "no result for this filter yet")
        self.assertEqual(self.board("?executors=4&executor_cores=6&bundle=x")["hero_notice"],
                         "no result for this filter yet")
        self.submit()
        self.assertIsNone(self.board()["hero_notice"])

    def test_board_page_texts_and_budgets(self):
        html = self.c.get(LB).get_data(as_text=True)
        self.assertIn('<h3 id="hero-title">Hero</h3>', html)
        self.assertIn("이 SF 탭에 Total-eligible run 이 아직 없습니다", html)
        self.assertIn("shared application run 은 목록에만 남고 순위와 hero 에는 오르지 않습니다 (A6)", html)
        self.assertIn("항목당 200개", html)
        self.assertEqual(json.loads(self.page_data()["lb-page-data"])["scalar_chars"], 512)

    def test_common_reasons_shown_once(self):
        m = build_manifest(run_id=rid(), record_answers=True)
        self.post(build_envelope(m))
        reasons = self.c.get(f"/api/leaderboard/runs/{m['run_id']}").get_json()["derived"]["ineligible_reasons"]
        self.assertIn("recording mode (record_answers=true)", reasons["common"])
        html = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        self.assertIn('id="common-reasons"', html)
        self.assertEqual(html.count("recording mode (record_answers=true)"), 1)

    def test_dry_run_reason_lists_capped(self):
        m = build_manifest(run_id=rid(), repeats=60)
        for rep in m["repeats"][1:]:
            rep["status"] = "aborted"
        out = self.post(m, "?dry_run=1", status=200)
        total = out["ineligible_reasons"]["total"]
        self.assertEqual(len(total), 51)
        self.assertRegex(total[-1], r"^\.\.\. \d+ more$")
        for v in out["ineligible_reasons"].values():
            self.assertLessEqual(len(v), 51)

    def test_runs_listed_by_instant(self):
        ids = {}
        for ts in ("2026-10-01T01:03:00.5Z", "2026-10-01T01:03:00Z"):
            m = build_manifest(run_id=rid())
            m["run"]["finished_at"] = ts
            self.post(build_envelope(m))
            ids[ts] = m["run_id"]
        self.assertEqual([r["run_id"] for r in self.c.get("/api/leaderboard/runs").get_json()],
                         [ids["2026-10-01T01:03:00Z"], ids["2026-10-01T01:03:00.5Z"]])


class TestApiContract(WebCase):
    def test_replay_returns_original_warnings(self):
        m = build_manifest(run_id=rid())
        env = build_envelope(m)
        env["x_extra"] = 1                                  # unknown property: a warning, not an error
        first = self.post(env)
        self.assertTrue(first["warnings"])
        self.assertEqual(self.post(build_envelope(m), status=200), first)

    def test_malformed_run_id_json_404(self):
        for method, u in (("get", "/api/leaderboard/runs/abc"), ("get", "/api/leaderboard/runs/abc/raw"),
                          ("post", "/api/leaderboard/runs/abc/withdraw")):
            r = getattr(self.c, method)(u, json={"actor": "a", "reason": "r"} if method == "post" else None)
            self.assertEqual(r.status_code, 404, u)
            self.assertEqual(r.get_json(), {"errors": ["unknown run_id"]}, u)

    def test_runs_takes_filters_without_default(self):
        four = self.submit()
        one = self.submit(executors=1)
        got = lambda q: {r["run_id"] for r in self.c.get("/api/leaderboard/runs" + q).get_json()}  # noqa: E731
        self.assertEqual(got(""), {four, one})
        self.assertEqual(got("?executors=1"), {one})
        self.assertEqual(got("?executors=4&executor_cores=6"), {four})
        self.assertEqual(got("?from=2099-01-01"), set())
        self.assertEqual(self.c.get("/api/leaderboard/runs?sf=7").get_json(), [], "an SF with no runs lists nothing")
        for bad in ("abc", "1e3", "-1"):
            self.assertEqual(self.c.get(f"/api/leaderboard/runs?sf={bad}").status_code, 400, bad)


class TestCompare(WebCase):
    """Compare: the board's selection leads there, Δ% is against the first run, the diff marks what differs."""

    def test_board_selection_navigates_to_compare(self):
        ids = [self.submit(elapsed=flat_elapsed(1000 + 10 * i)) for i in range(5)]       # board order = ids
        fetch = self.c.get("/api/leaderboard/board").get_data(as_text=True)
        out = run_page_js("board", data=self.page_data(), fetch=fetch, check=3, click="lb-compare")
        self.assertEqual(out["gate"], [True, False, False])                  # 2–4 selected enable the button
        self.assertEqual(out["location"], f"{LB}/compare?runs={','.join(ids[:3])}")
        self.assertEqual(self.c.get(out["location"]).status_code, 200)
        self.assertEqual(run_page_js("board", data=self.page_data(), fetch=fetch, check=5)["gate"],
                         [True, False, False, False, True])                 # five is one too many

    def test_compare_script_wires_its_toggles(self):
        """The compare page's script builds its chart series and attaches the log-scale and only-differences toggles."""
        runs = [self.submit(elapsed=flat_elapsed(1000 + 10 * i)) for i in range(3)]
        html = self.c.get(f"{LB}/compare?runs={','.join(runs)}").get_data(as_text=True)
        data = re.search(r'id="lb-compare-data">(.*?)</script>', html, re.S).group(1)
        out = run_page_js("compare", ids=re.findall(r'id="([^"]+)"', html), data={"lb-compare-data": data})
        self.assertEqual(out["listeners"].get("compare-log"), ["change"])
        self.assertEqual(out["listeners"].get("params-only-diff"), ["change"])

    def test_run_and_compare_pages_link_back_to_the_board(self):
        a, b = self.submit(), self.submit()
        for url in (f"{LB}/run/{a}", f"{LB}/compare?runs={a},{b}"):
            page = self.c.get(url).get_data(as_text=True)
            self.assertIn(f'class="lb-brand" href="{LB}"', page, url)
            self.assertIn(f'<nav class="lb-crumbs" aria-label="Breadcrumb">', page, url)
            crumbs = page[page.index('<nav class="lb-crumbs"'):page.index("</nav>", page.index('<nav class="lb-crumbs"'))]
            self.assertIn(f'href="{LB}"', crumbs, url)

    def compare_deltas(self, *runs):
        """The Δ% texts of the Xflare total row and the Q1 row, left to right."""
        html = self.c.get(f"{LB}/compare?runs={','.join(runs)}").get_data(as_text=True)
        pct = re.compile(r"[+-]\d+\.\d%")
        total = re.search(r'<tr class="lb-total-row">(.*?)</tr>', html, re.S).group(1)
        q1 = re.search(r"<td>Q1</td>(.*?)</tr>", html, re.S).group(1)
        return pct.findall(total), pct.findall(q1)

    def test_delta_is_against_the_first_run(self):
        ms100, ms50, ms200 = (self.submit(elapsed=lambda q, r, e, x=x: x if e == "xflare" else 3000)
                              for x in (100, 50, 200))
        self.assertEqual(self.compare_deltas(ms100, ms50, ms200), (["-50.0%", "+100.0%"], ["-50.0%", "+100.0%"]))
        self.assertEqual(self.compare_deltas(ms50, ms100, ms200), (["+100.0%", "+300.0%"], ["+100.0%", "+300.0%"]))

    def test_parameter_diff_marks_what_differs(self):
        a = build_manifest(run_id=rid())
        b = build_manifest(run_id=rid(), executors=8)
        b["run"]["xflare_overrides"][0]["value"] = "4096"
        for m in (a, b):
            self.post(build_envelope(m))
        html = self.c.get(f"{LB}/compare?runs={a['run_id']},{b['run_id']}").get_data(as_text=True)
        rows = {key: (cls, cells) for cls, key, cells in
                re.findall(r'<tr class="(lb-differs|lb-same)">\s*<td><code>([^<]*)</code></td>(.*?)</tr>', html, re.S)}
        self.assertEqual(rows["resources.executors"][0], "lb-differs")
        self.assertIn(">4<", rows["resources.executors"][1])
        self.assertIn(">8<", rows["resources.executors"][1])
        self.assertEqual(rows["override.join.output_batch_rows"][0], "lb-differs")
        self.assertIn("4096", rows["override.join.output_batch_rows"][1])
        self.assertEqual(rows["resources.executor_cores"][0], "lb-same")


class TestConcurrency(WebCase):
    def test_two_concurrent_posts(self):
        envs = [build_envelope(build_manifest(run_id=rid(), elapsed=flat_elapsed(1000 + i))) for i in range(2)]
        barrier = threading.Barrier(2)
        codes = []

        def go(env):
            client = self.app.test_client()
            barrier.wait()
            codes.append(client.post(SUBMIT, json=env).status_code)

        threads = [threading.Thread(target=go, args=(e,)) for e in envs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(codes, [201, 201])
        got = {r["run_id"] for r in self.c.get("/api/leaderboard/runs").get_json()}
        self.assertEqual(got, {e["manifest"]["run_id"] for e in envs})


class TestReproAndBoardLabels(WebCase):
    """The board names a run by when it finished and spells its config out on hover; the run page's reproduce
    script pastes into a shell as is and copies over plain http."""

    # Every option is one spark-tpch.sh accepts; the hostile value rides in --results-dir.
    ARGV = ["--reservation", "4242", "--xflare-toml", "scripts/config/Resource.mu.d1.toml.in", "--scale-factor",
            "100", "--xflare", "driver.skip_tiling=false", "--results-dir", "$(touch pwned) 'q\"", "1", "6"]

    def run_with_argv(self, argv, **kw):
        m = build_manifest(run_id=rid(), **kw)
        m["run"]["argv"] = argv
        self.post(build_envelope(m))
        return m, self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)

    @staticmethod
    def script_of(page):
        return html.unescape(re.search(r'<pre id="repro-cmd">(.*?)</pre>', page, re.S).group(1))

    def paste(self, script, reservation):
        """Runs the script in bash from a fake checkout whose git and spark-tpch.sh only record their argv
        (NUL-separated; git also ends each call with a newline)."""
        bash = shutil.which("bash")
        if bash is None:
            raise AssertionError("bash not installed: the reproduce-script tests need it (README: Run locally)")
        root = tempfile.mkdtemp(prefix="lb-repro-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "bin"))
        os.makedirs(os.path.join(root, "scripts"))
        for path, name, tail in ((os.path.join(root, "bin", "git"), "git", 'printf "\\n" >> "$REC_DIR/git"\n'),
                                 (os.path.join(root, "scripts", "spark-tpch.sh"), "spark", "")):
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\0" "$a"; done >> "$REC_DIR/{name}"\n{tail}')
            os.chmod(path, 0o755)
        env = {"PATH": os.path.join(root, "bin") + os.pathsep + os.environ.get("PATH", ""), "REC_DIR": root}
        if reservation is not None:
            env["RESERVATION"] = reservation
        out = subprocess.run([bash], input=script, cwd=root, env=env, capture_output=True, text=True, timeout=30)

        def recorded(name):
            p = os.path.join(root, name)
            if not os.path.exists(p):
                return ""
            with open(p) as f:
                return f.read()
        spark = recorded("spark")
        return out, recorded("git"), spark.split("\0")[:-1] if spark else [], root

    def test_script_runs_the_run_again(self):
        m, page = self.run_with_argv(self.ARGV)
        out, git, spark, root = self.paste(self.script_of(page), "777")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(git, f"fetch\0origin\0\ncheckout\0{m['client']['revision']}\0\n")
        # artifact and bundle are pinned to what the run resolved; its own booking becomes the reader's, in place
        self.assertEqual(spark, ["--artifact", m["build"]["artifact_id"], "--bundle", m["build"]["bundle"]["resolved"],
                                 "--reservation", "777", *self.ARGV[2:]])
        self.assertFalse(os.path.exists(os.path.join(root, "pwned")))

    def test_script_waits_for_a_reservation(self):
        _, page = self.run_with_argv(self.ARGV)
        out, git, spark, _ = self.paste(self.script_of(page), None)
        self.assertEqual((git, spark), ("", []))
        self.assertIn("set RESERVATION", out.stderr)

    def test_script_keeps_the_runs_own_pins_and_flags_a_dirty_checkout(self):
        m = build_manifest(run_id=rid(), bundle_kind="dev")
        m["run"]["argv"] = ["--reservation", "1", "--bundle", "dev:abc", "--artifact", "art-9", "3"]
        m["client"]["dirty"] = True
        self.post(build_envelope(m))
        script = self.script_of(self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True))
        self.assertIn("uncommitted changes", script)
        self.assertIn("Dev bundle: only its owner", script)
        _, _, spark, _ = self.paste(script, "5")
        self.assertEqual(spark, ["--reservation", "5", "--bundle", "dev:abc", "--artifact", "art-9", "3"])

    def test_run_page_shows_the_stored_script(self):
        """The run page prints the script derive() stored with the run -- the board's copy -- and never rebuilds it."""
        m, page = self.run_with_argv(self.ARGV)
        stored = self.c.get(f"/api/leaderboard/runs/{m['run_id']}").get_json()["derived"]["repro"]
        with mock.patch.object(repro, "script", side_effect=AssertionError("rebuilt")):
            again = self.c.get(f"{LB}/run/{m['run_id']}")
            row = self.board()["rows"][0]
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.script_of(again.get_data(as_text=True)), stored["command"])
        self.assertEqual(row["repro"], stored["command"])

    def test_copy_without_a_secure_context(self):
        _, page = self.run_with_argv(self.ARGV)
        script = self.script_of(page)
        data = {"lb-run-data": re.search(r'id="lb-run-data">(.*?)</script>', page, re.S).group(1),
                "repro-cmd": script}
        ids = re.findall(r'id="([^"]+)"', page)
        plain = run_page_js("run", ids=ids, data=data, click="repro-copy")
        self.assertEqual((plain["execs"], plain["clipboard"]), (["copy"], []))
        self.assertEqual(plain["text"]["repro-copy"], "copied")
        secure = run_page_js("run", ids=ids, data=data, click="repro-copy", secure=True)
        self.assertEqual((secure["execs"], secure["clipboard"]), ([], [script]))

    def test_board_run_label_and_config_tooltip(self):
        run_id = self.submit()
        row = self.board()["rows"][0]
        out = run_page_js("board", data=self.page_data(),
                          fetch=self.c.get("/api/leaderboard/board").get_data(as_text=True))
        self.assertIn(row["finished_at"].replace("T", " ")[:16], out["texts"])
        self.assertNotIn(run_id[:8], out["texts"])
        self.assertIn(f"{run_id}\nfinished {row['finished_at']}", out["titles"])
        self.assertIn(f"{LB}/run/{run_id}", out["hrefs"])
        # the board cell says only the resources (the hero keeps the full label); the tooltip spells every field out
        self.assertIn("4×6 64g", out["texts"])
        self.assertIn("\n".join(f"{k}: {'—' if v is None else v}" for k, v in row["config"].items()), out["titles"])

    def test_board_row_copies_its_reproduce_script(self):
        m, page = self.run_with_argv(self.ARGV)
        row = self.board()["rows"][0]
        self.assertEqual(row["repro"], self.script_of(page))          # the board offers the run page's script
        with self.subTest("page script"):
            out = run_page_js("board", data=self.page_data(), click_class="lb-copy",
                              fetch=self.c.get("/api/leaderboard/board").get_data(as_text=True))
            self.assertEqual((out["execs"], out["clipboard"]), (["copy"], []))
            self.assertIn("copied", out["texts"])
            self.assertIn(row["repro"], out["titles"])
        # a script past the budget is not offered; the cell points at the run page
        self.run_with_argv(["--download-logs"] * (repro.MAX_ARGS + 1), elapsed=flat_elapsed(500))
        long_row = self.board()["rows"][0]
        self.assertIsNone(long_row["repro"])
        with self.subTest("page script"):
            out = run_page_js("board", data=self.page_data(),
                              fetch=self.c.get("/api/leaderboard/board").get_data(as_text=True))
            self.assertIn("run page", out["texts"])


class TestLegacyApplicationMode(WebCase):
    """Version 5 accepted A4 fields as uninterpreted extensions; read migration re-checks the mode and per-engine
    applications, and an identical retry keeps its original receipt."""

    def assert_legacy_reads(self, manifest, old, good, reason, mode):
        import sqlite3
        store = lb_store.Store(self.data)
        raw = aggregate.canonical_manifest_bytes(manifest)
        cid = aggregate.content_id(manifest)
        old["content_id"] = cid
        old["derive_version"] = 5
        old["config"].pop("application_mode")
        receipt = {"ranked_total": True, "warnings": ["legacy extension"]}
        store._write_raw(cid, raw)
        # Construct the persisted row directly, without passing its historical extensions through A4 derive().
        with mock.patch.object(aggregate, "DERIVE_VERSION", 5):
            self.assertEqual(store._commit_row(build_envelope(manifest), cid, old, receipt), "created")
        run_id = manifest["run_id"]
        for path in ("/api/leaderboard/board", "/api/leaderboard/runs",
                     f"/api/leaderboard/runs/{run_id}", f"{LB}/run/{run_id}"):
            with sqlite3.connect(store.db_path) as conn:
                conn.execute("UPDATE runs SET derive_version=5 WHERE run_id=?", (run_id,))
            with mock.patch.object(aggregate, "derive", wraps=aggregate.derive) as derive:
                response = self.c.get(path)
                self.assertEqual(response.status_code, 200, (path, response.get_data(as_text=True)[:500]))
                current = store.get(run_id)
                self.assertEqual(derive.call_count, 1, path)
            d = current["derived"]
            self.assertFalse(d["total_eligible"])
            self.assertEqual(d["eligible_queries"], [])
            self.assertTrue(all(not q["eligible"] for q in d["per_query"].values()))
            self.assertIn(reason, " ".join(d["ineligible_reasons"]["common"]))
            self.assertEqual(d["config"]["application_mode"], mode)
            # A6: an unreadable mode keeps its own reason; it is not called a shared application
            self.assertNotIn(aggregate.SHARED_BADGE, d["badges"])
            self.assertNotIn(aggregate.SHARED_REASON, json.dumps(d["ineligible_reasons"]))
            self.assertEqual((current["content_id"], current["receipt"]), (cid, receipt))
            self.assertEqual(store.read_raw(run_id), raw)
            if path == f"{LB}/run/{run_id}":
                self.assertIn(reason, response.get_data(as_text=True))
                self.assertNotIn('id="shared-notice"', response.get_data(as_text=True))
        listed = self.c.get("/api/leaderboard/runs").get_json()
        self.assertIn(run_id, [r["run_id"] for r in listed])
        # §3: an identical retry keeps its original receipt although today's validator rejects the manifest
        retry = self.c.post(SUBMIT, json=build_envelope(manifest))
        self.assertEqual(retry.status_code, 200, retry.get_data(as_text=True)[:300])
        self.assertEqual(retry.get_json(),
                         {"run_id": run_id, "content_id": cid, "url": f"{LB}/run/{run_id}", **receipt})
        changed = json.loads(json.dumps(manifest))
        changed["run"]["codec"] = "zstd" if changed["run"]["codec"] != "zstd" else "snappy"
        self.assertEqual(self.c.post(SUBMIT, json=build_envelope(changed)).status_code, 400)  # not identical: validated
        detail = self.c.get(f"/api/leaderboard/runs/{run_id}").get_json()
        self.assertEqual(detail["manifest"], manifest)
        self.assertIn(reason, json.dumps(detail))
        for query in ("", "?metric=q1"):
            board = self.board(query)
            self.assertEqual([r["run_id"] for r in board["rows"]], [good])
            self.assertEqual(board["hero"]["run_id"], good)
        # the compare view shows the interpreted mode, never the stored raw value
        page = self.c.get(f"{LB}/compare?runs={good},{run_id}").get_data(as_text=True)
        row = re.search(r'<td><code>application_mode</code></td>(.*?)</tr>', page, re.S).group(1)
        self.assertEqual(re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)[-1].strip(), html.escape(mode))

    def test_non_enum_historical_modes_are_ineligible_and_display_a_dash(self):
        good = self.submit(repeats=1)
        for mode in ("per_engine", "", None, 1, True, [], {"x": 1}):
            with self.subTest(mode=mode):
                m = build_manifest(run_id=rid(), repeats=1, application_mode=None)
                old = aggregate.derive(m, None)
                m["run"]["application_mode"] = mode
                self.assert_legacy_reads(m, old, good, "application_mode not understood:", "-")

    def test_missing_and_malformed_historical_applications_are_ineligible(self):
        good = self.submit(repeats=1)
        cases = [("missing", None)]
        cases += [("applications", value) for value in (None, {}, {"engine": "velox"}, "apps", 1, True, [])]
        cases += [("element", value) for value in (None, [], "app", 1, True, {}, {"engine": "velox"})]
        fields = ("engine", "invocation", "app_id", "artifact_id", "bundle", "started_at", "finished_at", "cleanup")
        cases += [("missing-field", key) for key in fields]
        cases += [("bad-field", key) for key in fields]
        for kind, value in cases:
            with self.subTest(kind=kind, value=value):
                m = build_manifest(run_id=rid(), repeats=1, application_mode=None)
                old = aggregate.derive(m, None)
                m["run"]["application_mode"] = "per-engine"
                if kind == "applications":
                    m["repeats"][0]["applications"] = value
                elif kind == "element":
                    m["repeats"][0]["applications"] = [value]
                elif kind in ("missing-field", "bad-field"):
                    apps = build_manifest(application_mode="per-engine", repeats=1)["repeats"][0]["applications"]
                    m["repeats"][0]["applications"] = apps
                    if kind == "missing-field":
                        del apps[0][value]
                    else:
                        apps[0][value] = {}
                self.assert_legacy_reads(m, old, good, "applications missing or malformed in repeat 1", "per-engine")


class TestApplicationMode(WebCase):
    """A4: the application mode is a board column, a filter with its facet and a compare parameter, never a board
    split; a per-engine run's page lists every repeat's applications, a shared run's page has no such list. A6: only
    per-engine runs rank (TestSharedUnranked has the rest)."""

    def per_engine(self, **kw):
        kw.setdefault("run_id", rid())
        return build_manifest(application_mode="per-engine", **kw)

    def test_mode_column_filter_and_facet(self):
        shared = self.submit(elapsed=flat_elapsed(1000), application_mode=None)
        per_engine = self.per_engine(elapsed=flat_elapsed(2000))
        self.post(build_envelope(per_engine))
        b = self.board()
        # A6: the faster shared run is not a row; the filter still offers both modes
        self.assertEqual([r["config"]["application_mode"] for r in b["rows"]], ["per-engine"])
        self.assertEqual(b["facets"]["application_mode"],
                         [{"token": facet_token(x), "label": x} for x in ("per-engine", "shared")])
        for value, run_id, rows in (("shared", shared, []), ("per-engine", per_engine["run_id"],
                                                               [per_engine["run_id"]])):
            for query in (f"?application_mode={value}", f"?application_mode_t={facet_token(value)}"):
                self.assertEqual([r["run_id"] for r in self.board(query)["rows"]], rows, query)
                self.assertEqual([r["run_id"] for r in self.c.get("/api/leaderboard/runs" + query).get_json()],
                                 [run_id], query)
        page = self.c.get(LB).get_data(as_text=True)
        self.assertIn('data-key="application_mode"', page)
        self.assertIn("<th>Mode</th>", page)
        fetch = self.c.get("/api/leaderboard/board").get_data(as_text=True)
        out = run_page_js("board", data=self.page_data(), fetch=fetch, slots=["application_mode"])
        # one Mode cell for the one row, plus one filter option per mode
        self.assertEqual((out["texts"].count("shared"), out["texts"].count("per-engine")), (1, 2))
        self.assertIn(facet_token("per-engine"), out["values"])
        self.assertIn(facet_token("shared"), out["values"])

    def test_mixed_modes_only_per_engine_ranks(self):
        """A6 (supersedes A4's "ranked together"): with both modes stored, only the per-engine runs rank, in the
        usual order, on Total and on every query; the hero is the fastest per-engine run even when a shared run is
        faster, and filtering to shared leaves no rows and a per-engine fallback hero."""
        slow = self.submit(elapsed=flat_elapsed(3000), application_mode=None)
        fast = self.per_engine(elapsed=flat_elapsed(1000))
        mid = self.per_engine(elapsed=flat_elapsed(2000))
        for m in (fast, mid):
            self.post(build_envelope(m))
        quickest_shared = self.submit(elapsed=flat_elapsed(500), application_mode="shared")
        b = self.board()
        self.assertEqual([(r["run_id"], r["config"]["application_mode"]) for r in b["rows"]],
                         [(fast["run_id"], "per-engine"), (mid["run_id"], "per-engine")])
        self.assertEqual((b["hero"]["run_id"], b["hero_fallback"], b["eligible_count"]), (fast["run_id"], False, 2))
        for q in aggregate.QUERY_IDS:
            self.assertEqual([r["run_id"] for r in self.board(f"?metric=q{q}")["rows"]],
                             [fast["run_id"], mid["run_id"]], q)
        only_shared = self.board("?application_mode=shared")
        self.assertEqual((only_shared["hero"]["run_id"], only_shared["hero_fallback"],
                          [r["run_id"] for r in only_shared["rows"]], only_shared["eligible_count"]),
                         (fast["run_id"], True, [], 0))
        self.assertCountEqual([r["run_id"] for r in only_shared["shared_runs"]], [slow, quickest_shared])

    def test_per_engine_run_detail_lists_applications(self):
        m = self.per_engine(repeats=2)
        m["repeats"][0]["applications"][1]["invocation"] = "x" * 600         # the cut applies here too
        self.post(build_envelope(m))
        page = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        self.assertRegex(page, r"<dt>Application mode</dt><dd>per-engine — ")
        self.assertIn(" · per-engine · velox,xflare · snappy</h3>", page)     # the config label in the header
        table = re.search(r'<table class="cmp-table lb-table" id="applications">(.*?)</table>', page, re.S).group(1)
        rows = re.findall(r"<tr>\s*<td>(\d+)</td><td>(\w+)</td><td>(\w+)</td><td>([^<]*)</td>", table)
        self.assertEqual(rows, [(str(rep["index"]), a["engine"], a["cleanup"], a["app_id"])
                                for rep in m["repeats"] for a in rep["applications"]])
        self.assertEqual([r[1] for r in rows], ["velox", "xflare", "velox", "xflare"])
        for rep in m["repeats"]:
            for a in rep["applications"]:
                for key in ("started_at", "finished_at", "artifact_id", "bundle"):
                    self.assertIn(f"<td>{a[key]}</td>", table)
        self.assertIn(m["repeats"][1]["applications"][1]["invocation"], table)
        self.assertIn("x" * 512 + "…", table)
        self.assertNotIn("x" * 513, page)

    def test_aborted_per_engine_repeat_without_applications(self):
        m = self.per_engine(repeats=2)
        rep = m["repeats"][1]
        rep.update(status="aborted", applications=[], finished_at=None, queries=[])
        self.post(build_envelope(m))
        page = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        table = re.search(r'id="applications">(.*?)</table>', page, re.S).group(1)
        self.assertRegex(table, r'<tr><td>2</td><td class="lb-dim" colspan="8">no application</td></tr>')

    def test_shared_run_detail_unchanged(self):
        run_id = self.submit(repeats=2, application_mode=None)
        page = self.c.get(f"{LB}/run/{run_id}").get_data(as_text=True)
        self.assertRegex(page, r"<dt>Application mode</dt><dd>shared — ")
        self.assertNotIn('id="applications"', page)
        self.assertNotIn("Applications, per repeat", page)
        manifest = self.c.get(f"/api/leaderboard/runs/{run_id}").get_json()["manifest"]
        self.assertNotIn("application_mode", manifest["run"])                 # absent means shared
        for rep in manifest["repeats"]:
            self.assertIn(f"<td>{rep['app_id']}</td>", page)                  # the repeat table as before

    def test_hostile_application_fields_stay_escaped(self):
        m = self.per_engine()
        for rep in m["repeats"]:
            first, second = rep["applications"]
            first["app_id"] = rep["app_id"] = HOSTILE[0]
            second["app_id"], second["invocation"] = HOSTILE[1], HOSTILE[2]
            for a in (first, second):
                a["artifact_id"], a["bundle"] = HOSTILE[2], HOSTILE[0]
            rep["artifact_id"], rep["bundle"] = HOSTILE[2], HOSTILE[0]
        m["build"]["artifact_id"] = HOSTILE[2]
        self.post(build_envelope(m))
        page = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        table = re.search(r'id="applications">(.*?)</table>', page, re.S).group(1)
        for s in HOSTILE:
            self.assertNotIn(s, page)
            self.assertIn(str(markupsafe.escape(s)), table)
        other = self.submit()
        cmp_page = self.c.get(f"{LB}/compare?runs={m['run_id']},{other}").get_data(as_text=True)
        for s in HOSTILE:
            self.assertNotIn(s, cmp_page)

    def test_compare_shows_the_mode(self):
        shared = self.submit(application_mode=None)
        m = self.per_engine()
        self.post(build_envelope(m))
        html = self.c.get(f"{LB}/compare?runs={shared},{m['run_id']}").get_data(as_text=True)
        row = re.search(r'<tr class="(lb-differs|lb-same)">\s*<td><code>application_mode</code></td>(.*?)</tr>',
                        html, re.S)
        self.assertEqual(row.group(1), "lb-differs")
        self.assertEqual(re.findall(r"<td[^>]*>([^<]*)</td>", row.group(2)), ["shared", "per-engine"])


class TestSharedUnranked(WebCase):
    """A6: a shared-application run (mode field absent or "shared") is accepted, stored and listed with the shared
    reason and badge, keeps its measured medians and totals, but is on no board, never the hero and never an A3
    fallback candidate; its page says so. A per-engine run ranks. A tab where nothing ranks renders cleanly."""

    MODES = (None, "shared")                      # absent means shared (A4)

    def test_total_box_keeps_total_causes_beside_a_common_cause(self):
        """With a common cause the Total box drops only the per-query roll-up, not the Total-specific causes."""
        m = build_manifest(run_id=rid(), record_answers=True)
        m["run"]["exit_status"] = "failed"
        m["repeats"][2]["status"] = "aborted"
        self.post(build_envelope(m))
        page = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        box = page[page.index("Total 순위 대상이 아닙니다"):]
        box = box[:box.index("</ul>")]
        self.assertIn("공통 원인(common reasons) 때문입니다", box)
        self.assertIn("exit_status failed", box)
        self.assertIn("repeat 3 aborted", box)
        self.assertNotIn("queries not eligible", box)

    def shared(self, mode=None, **kw):
        kw.setdefault("run_id", rid())
        m = build_manifest(application_mode=mode, **kw)
        self.assertEqual(m["run"].get("application_mode"), mode)
        return m

    def assert_unranked(self, run_id):
        """``run_id`` is on no board (Total and every query, any filter) and not the hero."""
        for metric in aggregate.METRICS:
            for query in (f"?metric={metric}", f"?metric={metric}&executors=&executor_cores="):
                b = self.board(query)
                self.assertNotIn(run_id, [r["run_id"] for r in b["rows"]], query)
                self.assertNotEqual((b["hero"] or {}).get("run_id"), run_id, query)

    def test_shared_stored_but_unranked_via_http(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                m = self.shared(mode)
                run_id = m["run_id"]
                dry = self.post(build_envelope(m), "?dry_run=1", status=200)
                self.assertEqual((dry["ranked_total"], dry["run_id"]), (False, run_id))
                self.assertIn(aggregate.SHARED_BADGE, dry["badges"])
                self.assertIn(aggregate.SHARED_REASON, dry["ineligible_reasons"]["common"])
                self.assertEqual(self.c.get(f"/api/leaderboard/runs/{run_id}").status_code, 404)  # dry run stored nothing
                bare = self.post(m, "?dry_run=1", status=200)
                self.assertEqual((bare["ranked_total"], bare["content_id"]), (False, aggregate.content_id(m)))
                receipt = self.post(build_envelope(m))
                self.assertEqual((receipt["run_id"], receipt["ranked_total"]), (run_id, False))
                self.assertEqual(self.post(build_envelope(m), status=200), receipt)           # §3 replay

                listed = {r["run_id"]: r for r in self.c.get("/api/leaderboard/runs?sf=100").get_json()}
                self.assertIn(run_id, listed)
                self.assertFalse(listed[run_id]["total_eligible"])
                self.assertIn(aggregate.SHARED_BADGE, listed[run_id]["badges"])
                self.assertEqual(listed[run_id]["config"]["application_mode"], "shared")
                self.assertEqual(listed[run_id]["ineligible_reasons"], {"common": [aggregate.SHARED_REASON]})
                detail = self.c.get(f"/api/leaderboard/runs/{run_id}").get_json()
                d = detail["derived"]
                self.assertEqual(detail["manifest"], m)
                self.assertEqual(d["ineligible_reasons"]["common"][0], aggregate.SHARED_REASON)
                self.assertEqual((d["total_eligible"], d["eligible_queries"]), (False, []))
                # the measurements stay shown: medians and totals as a per-engine twin would have them
                twin = aggregate.derive(build_manifest(run_id=run_id), None)
                self.assertEqual(d["totals"], twin["totals"])
                self.assertIsNotNone(d["totals"]["xflare_ms"])
                for q in aggregate.QUERY_IDS:
                    self.assertFalse(d["per_query"][str(q)]["eligible"])
                    self.assertEqual(d["per_query"][str(q)]["xflare_ms"], twin["per_query"][str(q)]["xflare_ms"])
                self.assert_unranked(run_id)

                page = self.c.get(f"{LB}/run/{run_id}").get_data(as_text=True)
                notice = re.search(r'<p class="lb-banner lb-banner-warn" id="shared-notice">(.*?)</p>', page, re.S)
                self.assertIsNotNone(notice)
                self.assertIn(aggregate.SHARED_REASON + " — this run is listed but never ranked (amendment A6)",
                              " ".join(notice.group(1).split()))
                self.assertLess(page.index('id="shared-notice"'), page.index("<h3>SF-"))      # before the content
                self.assertIn(f'<span class="lb-badge b-warn">{aggregate.SHARED_BADGE}</span>', page)
                self.assertIn(f"<span class=\"lb-stat-val\">{_ms(d['totals']['xflare_ms'])}</span>", page)
                self.assertIn("Total 순위 대상이 아닙니다", page)
                total_box = page[page.index("Total 순위 대상이 아닙니다"):]
                total_box = total_box[:total_box.index("</ul>")]
                self.assertIn("공통 원인(common reasons) 때문입니다", total_box)      # the real cause, not "q1..q22"
                self.assertNotIn("queries not eligible", total_box)
                self.assertIn(aggregate.SHARED_REASON, re.search(r'id="common-reasons">(.*?)</ul>', page, re.S)
                              .group(1))
                self.assertIn("막대는 순위에 쓰이지 않는 측정값입니다", page)

    def test_per_engine_ranked(self):
        m = build_manifest(run_id=rid())
        self.assertEqual(m["run"]["application_mode"], "per-engine")    # the fixture default since A6
        dry = self.post(build_envelope(m), "?dry_run=1", status=200)
        self.assertTrue(dry["ranked_total"])
        self.assertNotIn(aggregate.SHARED_BADGE, dry["badges"])
        self.assertTrue(self.post(build_envelope(m))["ranked_total"])
        for metric in aggregate.METRICS:
            b = self.board(f"?metric={metric}")
            self.assertEqual([r["run_id"] for r in b["rows"]], [m["run_id"]], metric)
            self.assertEqual((b["hero"]["run_id"], b["hero_fallback"], b["shared_count"], b["shared_runs"]),
                             (m["run_id"], False, 0, []), metric)
        for summary in (self.board()["rows"][0], self.c.get("/api/leaderboard/runs").get_json()[0]):
            self.assertEqual(summary["ineligible_reasons"], {"common": []})
        page = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        self.assertNotIn('id="shared-notice"', page)
        self.assertNotIn(aggregate.SHARED_BADGE, page)
        self.assertIn("eligible 이 아닌 쿼리는 막대가 없습니다", page)

    def test_hero_never_shared_including_the_fallback(self):
        """A shared run is never the hero, even when it is the fastest run and the only one under the filter: the
        A3 fallback looks at per-engine runs only, and with none the tab has no hero at all."""
        fast_shared = self.shared(elapsed=flat_elapsed(500))                     # 4×6, the default filter
        self.post(build_envelope(fast_shared))
        b = self.board()
        self.assertEqual((b["hero"], b["hero_fallback"], b["hero_notice"], b["rows"]), (None, False, None, []))
        self.assertEqual(self.board("?executors=&executor_cores=")["hero"], None)
        other = self.shared("shared", executors=2, executor_cores=12, elapsed=flat_elapsed(400))
        self.post(build_envelope(other))
        self.assertIsNone(self.board()["hero"])

        per_engine_2x12 = self.submit(executors=2, executor_cores=12, elapsed=flat_elapsed(3000))
        b = self.board()                                                       # no per-engine 4×6 run: A3 fallback
        self.assertEqual((b["hero"]["run_id"], b["hero_fallback"], b["hero_notice"], b["rows"]),
                         (per_engine_2x12, True, "no 4×6 result yet", []))
        self.assertEqual(b["shared_count"], 1)                                 # the 4×6 shared run, under the filter
        b = self.board("?application_mode=shared&executors=&executor_cores=")
        self.assertEqual((b["hero"]["run_id"], b["hero_fallback"], b["shared_count"]), (per_engine_2x12, True, 2))

        per_engine_4x6 = self.submit(elapsed=flat_elapsed(4000))                # slower than both shared runs
        b = self.board()
        self.assertEqual((b["hero"]["run_id"], b["hero_fallback"], [r["run_id"] for r in b["rows"]]),
                         (per_engine_4x6, False, [per_engine_4x6]))
        b = self.board("?executors=&executor_cores=")
        self.assertEqual([r["run_id"] for r in b["rows"]], [per_engine_2x12, per_engine_4x6])
        self.assertEqual(b["hero"]["run_id"], per_engine_2x12)
        for run_id in (fast_shared["run_id"], other["run_id"]):
            self.assert_unranked(run_id)

    def test_empty_tab_renders(self):
        """Both tabs as after the A6 deploy: SF-100 holds only shared runs, SF-1000 nothing. The board API, the
        board script, the run pages and a compare of two shared runs all render, and say why nothing ranks."""
        ids = []
        for mode, ms in ((None, 1000), ("shared", 2000)):
            m = self.shared(mode, elapsed=flat_elapsed(ms))
            self.post(build_envelope(m))
            ids.append(m["run_id"])
        b = self.board()
        self.assertEqual((b["hero"], b["hero_fallback"], b["rows"], b["eligible_count"]), (None, False, [], 0))
        self.assertEqual(b["shared_count"], 2)
        self.assertCountEqual([r["run_id"] for r in b["shared_runs"]], ids)
        for r in b["shared_runs"]:
            self.assertEqual(r["url"], f"{LB}/run/{r['run_id']}")
            self.assertTrue(r["config_label"].endswith("· shared · velox,xflare · snappy"))
        self.assertEqual(len(self.board("?top=5")["shared_runs"]), 2)
        data = self.page_data()
        self.assertEqual(json.loads(data["lb-page-data"])["badge_class"][aggregate.SHARED_BADGE], "b-warn")

        with self.subTest("page script"):
            out = run_page_js("board", data=data, fetch=json.dumps(b))
            self.assertFalse(out["hidden"]["hero-empty"])
            self.assertTrue(out["hidden"]["hero-body"])
            self.assertTrue(out["hidden"]["hero-link"])
            self.assertTrue(out["hidden"]["hero-notice"])
            self.assertFalse(out["hidden"]["lb-shared"])
            self.assertTrue(out["text"]["lb-board-note"].startswith("Total: 0 eligible run(s) under the filter"))
            self.assertIn("shared application run 2개는 이 필터에 해당하지만 순위에 오르지 않습니다",
                          out["text"]["lb-shared-note"])
            self.assertIn("이 필터에 순위에 오를 run 이 없습니다. shared application run 은 순위에 오르지 않습니다 — "
                          "아래 목록을 보세요.", out["texts"])
            for run_id in ids:
                self.assertIn(f"{LB}/run/{run_id}", out["hrefs"])

        empty = self.board("?sf=1000")
        self.assertEqual((empty["hero"], empty["rows"], empty["shared_count"], empty["shared_runs"]),
                         (None, [], 0, []))
        with self.subTest("page script"):
            out = run_page_js("board", data=data, fetch=json.dumps(empty))
            self.assertFalse(out["hidden"]["hero-empty"])
            self.assertTrue(out["hidden"]["lb-shared"])
            self.assertIn("이 필터에 순위에 오를 run 이 없습니다.", out["texts"])

        # a shared list past `top` names the first `top` and says how many there are
        for _ in range(5):
            self.post(build_envelope(self.shared()))
        b = self.board("?top=5")
        self.assertEqual((b["shared_count"], len(b["shared_runs"])), (7, 5))
        with self.subTest("page script"):
            out = run_page_js("board", data=data, fetch=json.dumps(b))
            self.assertIn("처음 5개:", out["text"]["lb-shared-note"])

        for path in (f"{LB}", f"{LB}/run/{ids[0]}", f"{LB}/compare?runs={ids[0]},{ids[1]}"):
            self.assertEqual(self.c.get(path).status_code, 200, path)
        cmp_page = self.c.get(f"{LB}/compare?runs={ids[0]},{ids[1]}").get_data(as_text=True)
        self.assertEqual(cmp_page.count(f'<span class="lb-badge b-warn">{aggregate.SHARED_BADGE}</span>'), 2)

    def test_withdrawn_shared_run_leaves_the_list(self):
        m = self.shared()
        self.post(build_envelope(m))
        self.assertEqual(self.board()["shared_count"], 1)
        r = self.c.post(f"/api/leaderboard/runs/{m['run_id']}/withdraw", json={"actor": "a@x", "reason": "r"})
        self.assertEqual(r.status_code, 200)
        b = self.board()
        self.assertEqual((b["shared_count"], b["shared_runs"]), (0, []))
        page = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        self.assertIn('class="lb-banner"', page)                                    # withdrawn, and still shared
        self.assertIn('id="shared-notice"', page)

    def test_summary_reasons_cut_and_capped(self):
        """The summary's common reasons follow the summary budgets: each cut to SCALAR_CHARS, at most 50 plus a
        "... N more" line; the stored derived values stay whole."""
        from leaderboard import web
        m = self.shared(repeats=1)
        self.post(build_envelope(m))
        row = lb_store.Store(self.data).get(m["run_id"])
        reasons = [aggregate.SHARED_REASON, "r" * 600] + [f"reason {i}" for i in range(60)]
        row["derived"]["ineligible_reasons"]["common"] = reasons
        with self.app.test_request_context():
            listed = web._run_summary(row)["ineligible_reasons"]["common"]
        self.assertEqual(len(listed), 51)
        self.assertEqual(listed[:2], [aggregate.SHARED_REASON, "r" * 512 + "…"])
        self.assertEqual(listed[-1], "... 12 more")
        self.assertEqual(row["derived"]["ineligible_reasons"]["common"], reasons)

    def test_shared_fixture(self):
        """The committed shared fixture is accepted and stored but unranked; the default fixture ranks."""
        for name, ranked in (("valid-shared-envelope.json", False), ("valid-envelope.json", True)):
            with open(os.path.join(HERE, "fixtures", name), "rb") as f:
                r = self.c.post(SUBMIT, data=f.read(), content_type="application/json")
            self.assertEqual(r.status_code, 201, name)
            self.assertEqual(r.get_json()["ranked_total"], ranked, name)


def _ms(x) -> str:
    """lb_ms as the pages print it."""
    return f"{x / 1000:.2f} s" if x >= 1000 else f"{x:.0f} ms"


class TestSparkConf(WebCase):
    """A5: the Spark settings state is a filter (with labelled options), a badge, a board config-cell marker and a
    compare parameter; the run page lists both maps, says where the state came from, and tells an absent map from
    an empty one."""

    CONF = {"spark.sql.adaptive.enabled": "false", "spark.sql.shuffle.partitions": "400"}
    READBACK = {"spark.sql.adaptive.enabled": "false", "spark.sql.shuffle.partitions": "200"}

    def a5(self, spark_conf, readback=None, **kw):
        """An A5 manifest: spark_conf as given (argv gets the matching --spark-conf options), readback likewise."""
        kw.setdefault("run_id", rid())
        m = build_manifest(spark_conf=spark_conf, spark_conf_readback=readback, **kw)
        self.post(build_envelope(m))
        return m

    def page(self, run_id) -> str:
        r = self.c.get(f"{LB}/run/{run_id}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:500])
        return r.get_data(as_text=True)

    @staticmethod
    def section(page) -> str:
        return re.search(r'<section class="lb-panel" id="spark-conf">(.*?)</section>', page, re.S).group(1)

    @staticmethod
    def state(section) -> str:
        return " ".join(re.search(r"<dt>State</dt><dd>(.*?)</dd>", section, re.S).group(1).split())

    def compare_row(self, *run_ids, key) -> tuple[str, list[str]]:
        """The compare page's parameter row ``key``: its class and its cell texts."""
        html_ = self.c.get(f"{LB}/compare?runs={','.join(run_ids)}").get_data(as_text=True)
        found = re.search(r'<tr class="(lb-differs|lb-same)">\s*<td><code>' + re.escape(key) + r"</code></td>(.*?)</tr>",
                          html_, re.S)
        return found.group(1), re.findall(r"<td[^>]*>([^<]*)</td>", found.group(2))

    @staticmethod
    def tables(section) -> list[list[tuple[str, str]]]:
        return [re.findall(r"<tr><td><code>(.*?)</code></td><td><code>(.*?)</code></td></tr>", t, re.S)
                for t in re.findall(r"<tbody>(.*?)</tbody>", section, re.S)]

    def test_filter_facet_and_labels(self):
        default = self.a5({}, {}, elapsed=flat_elapsed(1000))
        custom = self.a5(self.CONF, self.READBACK, elapsed=flat_elapsed(2000))
        legacy = self.submit(elapsed=flat_elapsed(3000))                      # no field, no --spark-conf: default
        b = self.board()
        self.assertEqual(b["facets"]["spark_conf"],
                         [{"token": facet_token("custom"), "label": "custom (--spark-conf)"},
                          {"token": facet_token("default"), "label": "default Spark settings only"}])
        for value, ids in (("default", [default["run_id"], legacy]), ("custom", [custom["run_id"]])):
            for query in (f"?spark_conf={value}", f"?spark_conf_t={facet_token(value)}"):
                self.assertEqual([r["run_id"] for r in self.board(query)["rows"]], ids, query)
                self.assertEqual(sorted(r["run_id"] for r in self.c.get("/api/leaderboard/runs" + query).get_json()),
                                 sorted(ids), query)
        # the default board filter stays executors x executor_cores only: every state ranks
        self.assertEqual(len(b["rows"]), 3)
        self.assertEqual(b["default_filter"], {"executors": 4, "executor_cores": 6})
        self.assertIn('data-key="spark_conf" data-label="spark conf"', self.c.get(LB).get_data(as_text=True))
        out = run_page_js("board", data=self.page_data(), fetch=self.c.get("/api/leaderboard/board").get_data(as_text=True),
                          slots=["spark_conf"])
        self.assertIn("default Spark settings only", out["texts"])
        self.assertIn(facet_token("default"), out["values"])

    def test_badge_and_config_cell_marker(self):
        custom = self.a5(self.CONF, elapsed=flat_elapsed(1000))
        plain = self.a5({}, elapsed=flat_elapsed(2000))
        rows = self.board()["rows"]
        self.assertEqual([r["run_id"] for r in rows], [custom["run_id"], plain["run_id"]])
        self.assertIn("spark conf", rows[0]["badges"])
        self.assertNotIn("spark conf", rows[1]["badges"])
        self.assertTrue(rows[0]["config_label"].endswith(" +conf"))
        self.assertNotIn("+conf", rows[1]["config_label"])
        with self.subTest("page script"):
            out = run_page_js("board", data=self.page_data(),
                              fetch=self.c.get("/api/leaderboard/board").get_data(as_text=True))
            badge = [c for t, c in zip(out["texts"], out["classes"]) if t == "spark conf"]
            self.assertEqual(badge[:1], ["lb-badge b-warn"])                 # the board row's badge (hero repeats it)
            self.assertEqual(set(badge), {"lb-badge b-warn"})
            self.assertIn("4×6 64g +conf", out["texts"])
            self.assertIn("4×6 64g", out["texts"])
        page = self.page(custom["run_id"])
        self.assertIn('<span class="lb-badge b-warn">spark conf</span>', page)   # the run page colours it alike
        self.assertIn(" +conf</h3>", page)

    def test_detail_lists_both_maps(self):
        m = self.a5(self.CONF, self.READBACK)
        section = self.section(self.page(m["run_id"]))
        self.assertEqual(self.state(section), "custom — from run.spark_conf: the settings in effect are listed below")
        self.assertEqual(self.tables(section), [list(self.CONF.items()), list(self.READBACK.items())])
        self.assertIn("Requested — run.spark_conf", section)
        self.assertIn("Read back by the session — run.spark_conf_readback", section)
        self.assertNotIn("not recorded", section)

    def test_absent_and_empty_are_told_apart(self):
        empty = self.a5({}, {})
        section = self.section(self.page(empty["run_id"]))
        self.assertEqual(self.state(section), "default — from run.spark_conf: it is empty, the run kept the default "
                                              "Spark settings")
        self.assertEqual(section.count("none ({})"), 2)
        self.assertNotIn("not recorded", section)
        self.assertNotIn("<table", section)
        # absent (not recorded, e.g. a pre-A5 manifest): the state comes from argv, and neither map is recorded
        for argv, state in ((REAL_ARGV, "default"), (REAL_ARGV[:2] + ["--spark-conf", "spark.a=1"] + REAL_ARGV[2:],
                                                     "custom")):
            with self.subTest(state=state):
                run_id = self.submit(argv=argv)
                section = self.section(self.page(run_id))
                self.assertEqual(self.state(section),
                                 f"{state} — read from argv: this manifest has no run.spark_conf, "
                                 f"and argv {'has a' if state == 'custom' else 'has no'} --spark-conf option it could read")
                self.assertEqual(section.count("not recorded"), 2)
                self.assertNotIn("none ({})", section)
        # only the readback absent
        half = self.a5(self.CONF)
        section = self.section(self.page(half["run_id"]))
        self.assertEqual(self.tables(section), [list(self.CONF.items())])
        self.assertEqual(section.count("not recorded"), 1)

    def test_maps_are_cut_and_bounded(self):
        conf = {f"spark.k{i:03d}": "v" for i in range(150)}
        conf["spark.k000"] = "x" * 600
        long_key = "spark." + "k" * 600
        readback = {long_key: "1"}
        m = self.a5(conf, readback)
        page = self.page(m["run_id"])
        requested, read = self.tables(self.section(page))
        self.assertEqual(len(requested), 100)
        self.assertEqual(requested[0], ("spark.k000", "x" * 512 + "…"))
        self.assertIn('<p class="lb-more">50 more — see raw JSON</p>', page)
        self.assertEqual(read, [(long_key[:512] + "…", "1")])
        self.assertNotIn("x" * 513, page)
        self.assertNotIn(long_key, page)
        # compare: one row per map, cut like any scalar
        other = self.a5({})
        cls, (line, empty) = self.compare_row(m["run_id"], other["run_id"], key="spark_conf (settings)")
        self.assertEqual((cls, line, empty), ("lb-differs", "spark.k000=" + "x" * 501 + "…", "{}"))
        cls, (line, empty) = self.compare_row(m["run_id"], other["run_id"], key="spark_conf_readback (settings)")
        self.assertEqual((cls, line, empty), ("lb-differs", f"{long_key}=1"[:512] + "…", "not recorded"))

    def test_hostile_keys_and_values_stay_escaped(self):
        conf = {HOSTILE[0]: HOSTILE[1], "spark.x": HOSTILE[2]}
        m = self.a5(conf, {HOSTILE[1]: HOSTILE[0]})
        page = self.page(m["run_id"])
        section = self.section(page)
        for s in HOSTILE:
            self.assertNotIn(s, page)
            self.assertIn(str(markupsafe.escape(s)), section)
        other = self.a5({})
        cmp_page = self.c.get(f"{LB}/compare?runs={m['run_id']},{other['run_id']}").get_data(as_text=True)
        for s in HOSTILE:
            self.assertNotIn(s, cmp_page)
        self.assertIn(str(markupsafe.escape(f"{HOSTILE[0]}={HOSTILE[1]}; spark.x={HOSTILE[2]}")), cmp_page)

    def test_compare_shows_the_state_and_the_maps(self):
        custom = self.a5(self.CONF, self.READBACK)
        empty = self.a5({}, {})
        legacy = self.submit(argv=REAL_ARGV)
        ids = (custom["run_id"], empty["run_id"], legacy)
        html_ = self.c.get(f"{LB}/compare?runs={','.join(ids)}").get_data(as_text=True)
        self.assertEqual(re.findall(r"<td><code>(spark_conf[^<]*)</code></td>", html_),
                         ["spark_conf (state)", "spark_conf.source", "spark_conf (settings)",
                          "spark_conf_readback (settings)"])
        self.assertEqual(self.compare_row(*ids, key="spark_conf (state)"),
                         ("lb-differs", ["custom", "default", "default"]))
        self.assertEqual(self.compare_row(*ids, key="spark_conf.source"),
                         ("lb-differs", ["run.spark_conf", "run.spark_conf", "argv (no run.spark_conf)"]))
        self.assertEqual(self.compare_row(*ids, key="spark_conf (settings)"),
                         ("lb-differs", ["spark.sql.adaptive.enabled=false; spark.sql.shuffle.partitions=400", "{}",
                                         "not recorded"]))
        self.assertEqual(self.compare_row(*ids, key="spark_conf_readback (settings)"),
                         ("lb-differs", ["spark.sql.adaptive.enabled=false; spark.sql.shuffle.partitions=200", "{}",
                                         "not recorded"]))
        # the same map in another key order is the same setting
        same = self.a5(dict(reversed(self.CONF.items())), self.READBACK)
        self.assertEqual(self.compare_row(custom["run_id"], same["run_id"], key="spark_conf (settings)")[0], "lb-same")

    def test_compare_differs_on_the_full_maps(self):
        """The settings rows compare the maps themselves, never their (cut) text: an uploaded "—" or "(more)" is one
        more setting, and a difference past the display cut still marks the row."""
        dash, plain = self.a5({"spark.a": "—", "spark.b": "v"}), self.a5({"spark.b": "v"})
        self.assertEqual(self.compare_row(dash["run_id"], plain["run_id"], key="spark_conf (settings)"),
                         ("lb-differs", ["spark.a=—; spark.b=v", "spark.b=v"]))
        more = self.a5({"(more)": "REAL_SETTING", **{f"spark.k{i:03}": "v" for i in range(100)}})
        cls, (line, empty) = self.compare_row(more["run_id"], self.a5({})["run_id"], key="spark_conf (settings)")
        self.assertEqual((cls, empty), ("lb-differs", "{}"))
        self.assertTrue(line.startswith("(more)=REAL_SETTING; spark.k000=v; "), line)
        long_a, long_b = self.a5({"spark.a": "x" * 600 + "1"}), self.a5({"spark.a": "x" * 600 + "2"})
        self.assertEqual(self.compare_row(long_a["run_id"], long_b["run_id"], key="spark_conf (settings)"),
                         ("lb-differs", ["spark.a=" + "x" * 504 + "…"] * 2))
        # equal maps, both empty, or both absent: the same
        self.assertEqual(self.compare_row(plain["run_id"], self.a5({"spark.b": "v"})["run_id"],
                                          key="spark_conf (settings)")[0], "lb-same")
        self.assertEqual(self.compare_row(self.a5({})["run_id"], self.a5({})["run_id"], key="spark_conf (settings)")[0],
                         "lb-same")
        self.assertEqual(self.compare_row(self.submit(), self.submit(), key="spark_conf_readback (settings)")[0],
                         "lb-same")

    def test_legacy_malformed_settings_render(self):
        """A row stored before A5 validated the maps (the A4 validator kept them as unknown fields) still renders:
        state unknown on the run page, compare and filter, never a 500."""
        store = lb_store.Store(self.data)
        good = self.a5({})
        cases = ["spark.a=1", ["spark.a=1"], {"spark.a": 1}, {"spark.a": {"nested": ["x" * 600]}}, None]
        ids = []
        for value in cases:
            m = build_manifest(run_id=rid())
            m["run"]["spark_conf"] = value
            m["run"]["spark_conf_readback"] = value
            cid = aggregate.content_id(m)
            store._write_raw(cid, aggregate.canonical_manifest_bytes(m))
            self.assertEqual(store._commit_row(build_envelope(m), cid, aggregate.derive(m, None),
                                               {"ranked_total": True, "warnings": []}), "created")
            ids.append(m["run_id"])
            with self.subTest(value=repr(value)[:40]):
                section = self.section(self.page(m["run_id"]))
                self.assertTrue(self.state(section).startswith("unknown — run.spark_conf is not a string → string "
                                                               "object"), self.state(section))
                if isinstance(value, dict):
                    self.assertEqual([len(t) for t in self.tables(section)], [1, 1])
                else:
                    self.assertEqual(section.count("not a key/value object — see raw JSON"), 2)
                self.assertNotIn("x" * 513, section)
                cmp_page = self.c.get(f"{LB}/compare?runs={good['run_id']},{m['run_id']}")
                self.assertEqual(cmp_page.status_code, 200)
                self.assertNotIn("nested", cmp_page.get_data(as_text=True))
        self.assertEqual([r["run_id"] for r in self.board("?spark_conf=default")["rows"]], [good["run_id"]])
        self.assertEqual(sorted(r["run_id"] for r in self.c.get("/api/leaderboard/runs?spark_conf=unknown").get_json()),
                         sorted(ids))
        # JSON types stay distinct in "differs": a stored true and a stored 1 are different settings
        typed = []
        for value in (True, 1):
            m = build_manifest(run_id=rid())
            m["run"]["spark_conf"] = m["run"]["spark_conf_readback"] = {"spark.k": value}
            cid = aggregate.content_id(m)
            store._write_raw(cid, aggregate.canonical_manifest_bytes(m))
            store._commit_row(build_envelope(m), cid, aggregate.derive(m, None), {"ranked_total": True, "warnings": []})
            typed.append(m["run_id"])
        page = self.c.get(f"{LB}/compare?runs={typed[0]},{typed[1]}").get_data(as_text=True)
        for key in ("spark_conf (settings)", "spark_conf_readback (settings)"):
            row = re.search(r'<tr class="([^"]+)">\s*<td><code>' + re.escape(key) + r'</code>', page)
            self.assertEqual(row.group(1), "lb-differs", key)
        self.assertIn({"token": facet_token("unknown"), "label": "unknown (spark_conf unreadable)"},
                      self.board()["facets"]["spark_conf"])


class TestTabs(WebCase):
    """A7: a tab is (scale factor, codec): the configured SFs with snappy are pinned, every other pair comes from the
    stored runs; hero, default filter and the fallback apply per tab."""

    def labels(self, query=""):
        return [t["label"] for t in self.board(query)["tabs"]]

    def test_tab_order_is_pinned_then_numeric(self):
        self.assertEqual(self.labels(), ["100 · snappy", "1000 · snappy"])
        for sf, codec in (("3000", "snappy"), ("1000", "zstd"), ("2.5", "snappy"), ("1000", "uncompressed"),
                          ("1", "snappy"), ("1.0", "snappy"), ("01", "snappy"), ("9" * 5000, "snappy")):
            self.submit(sf=sf, codec=codec)
        got = self.labels()
        self.assertEqual(got[:2], ["100 · snappy", "1000 · snappy"])
        self.assertEqual(got[2:-1], ["01 · snappy", "1 · snappy", "1.0 · snappy", "2.5 · snappy", "1000 · zstd",
                                     "1000 · uncompressed", "3000 · snappy"])
        self.assertEqual(len(got[-1]), 513 + len(" · snappy"))                       # cut like any scalar, then "…"
        self.assertEqual(self.board("?sf=3000")["hero"]["sf"], "3000")

    def test_board_selects_a_tab_by_sf_and_codec(self):
        snappy = self.submit(sf="1000", elapsed=flat_elapsed(9000))
        zstd = self.submit(sf="1000", codec="zstd", elapsed=flat_elapsed(7000))
        sf100 = self.submit(sf="100")
        default = self.board()
        self.assertEqual((default["sf"], default["codec"], default["hero"]["run_id"]), ("100", "snappy", sf100))
        for query, want in (("?sf=1000", snappy), ("?sf=1000&codec=snappy", snappy), ("?sf=1000&codec=zstd", zstd)):
            b = self.board(query)
            self.assertEqual([r["run_id"] for r in b["rows"]], [want], query)
            self.assertEqual((b["hero"]["run_id"], b["hero_fallback"]), (want, False), query)
            self.assertNotIn("codec", b["filter"])
            self.assertNotIn("codec", b["filter_tokens"])
        self.assertEqual([r["run_id"] for r in self.c.get("/api/leaderboard/runs?codec=zstd").get_json()], [zstd])
        for bad in ("?sf=1000&codec=uncompressed", "?sf=7", "?codec=zstd"):
            r = self.c.get("/api/leaderboard/board" + bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("one of 100 · snappy, 1000 · snappy, 1000 · zstd", r.get_json()["errors"][0])

    def test_a_tab_without_a_default_shape_run_falls_back_within_the_tab(self):
        other_shape = self.submit(sf="1000", codec="zstd", executors=1)
        self.submit(sf="1000", executors=2)                     # another tab's run is never a candidate
        b = self.board("?sf=1000&codec=zstd")
        self.assertEqual((b["hero"]["run_id"], b["hero_fallback"], b["rows"]), (other_shape, True, []))

    def test_engines_is_a_filter_and_a_column(self):
        both = self.submit(elapsed=flat_elapsed(1000))
        alone = self.submit(elapsed=flat_elapsed(2000), engines=("xflare",))
        b = self.board()
        self.assertEqual([(r["run_id"], r["config"]["engines"]) for r in b["rows"]],
                         [(both, "velox,xflare"), (alone, "xflare")])
        self.assertEqual([r["run_id"] for r in self.board("?engines=xflare")["rows"]], [alone])
        self.assertEqual([o["label"] for o in b["facets"]["engines"]], ["velox,xflare", "xflare"])
        self.assertIn("<th>Engines</th>", self.c.get(LB).get_data(as_text=True))
        with self.subTest("page script"):
            out = run_page_js("board", data=self.page_data(), fetch=json.dumps(b))
            self.assertIn("velox, xflare", out["texts"])
            self.assertIn("xflare", out["texts"])
        self.assertEqual(b["rows"][1]["value"]["velox_ms"], None)

    def test_dry_run_says_which_runs_create_their_tab(self):
        for sf, codec, note in (("100", "snappy", None), ("1000", "snappy", None),
                                ("3000", "snappy", "SF 3000 · snappy is not a pinned tab: the first run stored at "
                                                   "it creates its tab"),
                                ("100", "zstd", "SF 100 · zstd is not a pinned tab: the first run stored at it "
                                                "creates its tab")):
            out = self.post(build_manifest(run_id=rid(), sf=sf, codec=codec), "?dry_run=1", status=200)
            self.assertEqual(out["tab_note"], note, (sf, codec))
        self.assertEqual(os.listdir(self.data), [])


class TestEffectiveSettings(WebCase):
    """A7: the recorded maps are on the run page and in the compare diff, key by key."""

    @staticmethod
    def with_maps(spark, xflare=None, **kw):
        m = build_manifest(run_id=rid(), **kw)
        for rep in m["repeats"]:
            for app in rep["applications"]:
                app["spark_conf_effective"] = dict(spark)
                if app["engine"] == "xflare" and xflare is not None:
                    app["xflare_parameters_effective"] = dict(xflare)
        return m

    def test_run_page_lists_the_maps(self):
        many = {f"spark.sql.k{i:03d}": str(i) for i in range(300)}
        m = self.with_maps({"spark.gluten.sql.columnar.maxBatchSize": "20480", **many},
                           {"driver.merge_launch_groups": "true", "driver.launch_parallelism": ""})
        self.post(build_envelope(m))
        page = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        section = re.search(r'<section class="lb-panel" id="effective-settings">(.*?)</section>', page, re.S).group(1)
        for want in ("spark_conf_effective (velox)", "spark_conf_effective (xflare)",
                     "xflare_parameters_effective (xflare)", "spark.gluten.sql.columnar.maxBatchSize",
                     "<code>20480</code>", "driver.merge_launch_groups", "spark.sql.k299"):
            self.assertIn(want, section)
        self.assertNotIn("xflare_parameters_effective (velox)", section)
        legacy = self.submit()
        self.assertIn("not recorded", re.search(r'id="effective-settings">(.*?)</section>',
                                                self.c.get(f"{LB}/run/{legacy}").get_data(as_text=True), re.S).group(1))

    def test_compare_lists_only_the_keys_that_differ(self):
        base = {"spark.sql.shuffle.partitions": "200", "spark.sql.adaptive.enabled": "true",
                "spark.executor.cores": "6"}
        a = self.with_maps(base, {"driver.merge_launch_groups": "true"})
        b = self.with_maps({**base, "spark.sql.shuffle.partitions": "400", "spark.sql.cbo.enabled": "true"},
                           {"driver.merge_launch_groups": "false"})
        for m in (a, b):
            self.post(build_envelope(m))
        page = self.c.get(f"{LB}/compare?runs={a['run_id']},{b['run_id']}").get_data(as_text=True)
        rows = {key: (cls, re.findall(r"<td[^>]*>([^<]*)</td>", cells)) for cls, key, cells in re.findall(
            r'<tr class="(lb-differs|lb-same)">\s*<td><code>(.*?)</code></td>(.*?)</tr>', page, re.S)}
        self.assertEqual(rows["spark_conf_effective (xflare) · spark.sql.shuffle.partitions"],
                         ("lb-differs", ["200", "400"]))
        self.assertEqual(rows["spark_conf_effective (xflare) · spark.sql.cbo.enabled"], ("lb-differs", ["—", "true"]))
        self.assertEqual(rows["xflare_parameters_effective (xflare) · driver.merge_launch_groups"],
                         ("lb-differs", ["true", "false"]))
        self.assertNotIn("spark_conf_effective (xflare) · spark.sql.adaptive.enabled", rows)
        self.assertEqual(rows["spark_conf_effective (xflare) (keys)"][1], ["3 keys", "4 keys"])

    def test_compare_caps_a_long_difference_list(self):
        a = self.with_maps({f"spark.sql.k{i:03d}": "a" for i in range(150)})
        b = self.with_maps({f"spark.sql.k{i:03d}": "b" for i in range(150)})
        for m in (a, b):
            self.post(build_envelope(m))
        page = self.c.get(f"{LB}/compare?runs={a['run_id']},{b['run_id']}").get_data(as_text=True)
        self.assertEqual(page.count("spark_conf_effective (xflare) · spark.sql.k"), 100)
        self.assertIn("50 more — see raw JSON", page)

    def test_a_blank_velox_cell_says_why(self):
        m = build_manifest(run_id=rid())
        m["repeats"][1]["queries"][2]["engines"]["velox"]["status"] = "timeout"
        self.post(build_envelope(m))
        page = self.c.get(f"{LB}/run/{m['run_id']}").get_data(as_text=True)
        self.assertRegex(page, r'<td class="num" title="velox timeout in repeat 2">—</td>')

    def test_a_run_whose_settings_drifted_does_not_rank(self):
        m = self.with_maps({"spark.sql.shuffle.partitions": "200"})
        m["repeats"][1]["applications"][1]["spark_conf_effective"] = {"spark.sql.shuffle.partitions": "400"}
        out = self.post(build_envelope(m), "?dry_run=1", status=200)
        self.assertFalse(out["ranked_total"])
        self.assertEqual(out["ineligible_reasons"]["common"], [aggregate.SPARK_SETTINGS_DIFFER])


if __name__ == "__main__":
    unittest.main(verbosity=2)
