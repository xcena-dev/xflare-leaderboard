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

import datetime
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

    def submit(self, argv=None, basis=None, **kw):
        kw.setdefault("run_id", rid())
        m = build_manifest(**kw)
        if argv is not None:
            m["run"]["argv"] = argv
        self.post(build_envelope(m, **({} if basis is None else {"basis": basis})))
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
        self.assertEqual(len(files), 6)
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
  querySelectorAll(sel) {
    // cfg.selectors: elements a page lists by class (the A9 restore buttons), each {id, dataset}.
    if ((cfg.selectors || {})[sel]) {
      return cfg.selectors[sel].map(x => { const e = byId[x.id] = new Element('button', x.id); Object.assign(e.dataset, x.dataset); return e; });
    }
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
  return { ok: (cfg.status || 200) < 300, status: cfg.status || 200, json: async () => JSON.parse(cfg.fetch),
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


def json_blocks(html: str) -> dict:
    """Every JSON data block of a page, {id: text}, as the page script reads them."""
    return dict(re.findall(r'<script type="application/json" id="([^"]+)">(.*?)</script>', html, re.S))


def run_page_js(page: str, ids=None, data=None, fetch=None, slots=None, click=None, click_class=None,
                secure=False, check=0, headers=None, prompts=(), datasets=None, reject_post=False,
                selectors=None, status=200) -> dict:
    node = page_script_node()
    cfg = {"js": os.path.join(ROOT, "static", "leaderboard.js"), "page": page, "ids": ids,
           "data": data or {}, "fetch": fetch, "slots": slots, "click": click,
           "clickClass": click_class, "secure": secure, "check": check, "prompts": list(prompts),
           "datasets": datasets or {}, "rejectPost": reject_post, "selectors": selectors or {}, "status": status}
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
        out = run_page_js("run", ids=re.findall(r'id="([^"]+)"', html), data=json_blocks(html),
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
        out = run_page_js("run", ids=re.findall(r'id="([^"]+)"', html), data=json_blocks(html))
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
# <details>, {"close": id} closes it; {"toggle": id} flips a checkbox. cfg.search is the page's query string,
# cfg.prompts answers prompt() in turn. cfg.labelDraws ({width, height} each) runs the newest hero-history chart's
# label plugin over a chart area that size, its hero points spread evenly across at mid-height, text 6 px a
# character. Prints what the page ended up showing.
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
  setAttribute(k, v) { (this.attrs = this.attrs || {})[k] = v; }
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
    this.options = conf.options; this.datasets = conf.data.datasets; this.plugins = conf.plugins || [];
    this.destroyed = false; charts.push(this);
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
    } else if (step.close) {
      byId[step.close].open = false;
      byId[step.close].fire('toggle');
    } else if (step.toggle) {
      const e = byId[step.toggle];
      e.checked = !e.checked;
      e.fire('change');
    } else if (step.check) {
      // A9: tick the checkbox of that PR set in the PR-set panel.
      const box = all.find(e => e.tagName === 'input' && e.attrs && e.attrs['aria-label'] === `compare ${step.check}`);
      box.checked = true;
      box.fire('change');
    } else if (step.tab) {
      byId[step.tab[0]].children.find(b => b.textContent === step.tab[1]).fire('click');
    } else if (step.select) {
      byId[step.select[0]].value = step.select[1];
      byId[step.select[0]].fire('change');
    }
  }
  await tick(); await tick();
  const flat = e => [e.textContent, ...e.children.flatMap(flat)].filter(t => t !== '' && t !== undefined);
  const hrefs = e => [e.href, ...e.children.flatMap(hrefs)].filter(Boolean);
  const titles = e => [e.title, ...e.children.flatMap(titles)].filter(Boolean);
  const inputs = e => [...(e.tagName === 'input' ? [{ checked: e.checked, disabled: !!e.disabled }] : []),
                       ...e.children.flatMap(inputs)];
  const rows = sel => (byId[sel] ? byId[sel].children : []).map(tr => ({ text: tr.children.map(td => flat(td).join(' ')),
                                                                         hrefs: hrefs(tr), titles: titles(tr),
                                                                         cls: tr.children.map(td => td.className),
                                                                         inputs: inputs(tr) }));
  const labelDraws = (cfg.labelDraws || []).map(({ width, height }) => {
    const c = [...charts].reverse().find(c => c.plugins.some(p => p.id === 'heroLabels'));
    const drawn = [], n = c.datasets[1].data.length;
    const ctx = { save() {}, restore() {}, measureText: t => ({ width: 6 * t.length }),
                  fillText: (text, x, y) => drawn.push({ text, x, y, w: 6 * text.length }) };
    const pts = c.datasets[1].data.map((_, i) => ({ x: n > 1 ? width * i / (n - 1) : width / 2, y: height / 2 }));
    c.plugins.find(p => p.id === 'heroLabels').afterDatasetsDraw(
      { ctx, chartArea: { left: 0, right: width, top: 0, bottom: height }, getDatasetMeta: () => ({ data: pts }) });
    return drawn;
  });
  const compareBtn = byId['lb-prsets-compare'] || {};
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
    target: byId['hero-target'].textContent, targetHidden: byId['hero-target'].hidden,
    // A9: each table row's cell texts and links, the history panel's active metric, where a click navigated.
    boardRows: rows('#lb-board tbody'), historyRows: rows('#lb-history-table tbody'),
    prsetRows: rows('#lb-prsets-table tbody'), location: location.href || null,
    historyNote: (byId['lb-history-note'] || {}).textContent,
    buttons: all.filter(e => e.tagName === 'button').map(b => ({ text: String(b.textContent), title: String(b.title || '') })), prsetsNote: (byId['lb-prsets-note'] || {}).textContent,
    heroMeta: byId['hero-meta'] ? { text: flat(byId['hero-meta']), hrefs: hrefs(byId['hero-meta']),
                                    titles: titles(byId['hero-meta']) } : null,
    setCompare: { disabled: !!compareBtn.disabled, text: compareBtn.textContent }, labelDraws,
    historyMetric: ((byId['lb-history-metrics'] || { children: [] }).children.find(b => b.className.includes('active')) || {}).textContent,
    charts_all: charts.map(c => c.datasets.map(d => ({ label: d.label, data: d.data }))),
    chartInfo: charts.map(c => ({ destroyed: c.destroyed, x: c.options.scales.x.type, xMin: c.options.scales.x.min,
                                  stepped: c.datasets.map(d => d.stepped ?? null),
                                  plugins: c.plugins.map(p => p.id) })) }));
})();
"""


class TestBoardScript(WebCase):
    """The board page script under controlled timing: stale answers never win, an answer for another script never
    renders, one log toggle drives the chart."""

    def drive(self, steps, responses, meta=None, search="", prompts=(), label_draws=()):
        """Answers name the page's own script unless meta[key] gives other headers (names in lower case)."""
        node = page_script_node()
        data = self.page_data()
        cfg = {"js": os.path.join(ROOT, "static", "leaderboard.js"), "data": data, "steps": steps,
               "responses": responses, "meta": meta or {}, "search": search, "prompts": list(prompts),
               "headers": {"x-leaderboard-script": json.loads(data["lb-page-data"])["script"]},
               "labelDraws": list(label_draws)}
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
        """A8: the history lists the latest changes; "다시 지정" posts that earlier target and goal at the revision the
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
        unknown = self.board(f"?target={rid()}")
        self.assertEqual((unknown["target"]["problem"], unknown["hero"]["vs_target"]), ("unknown run", None))
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
                "data": json_blocks(page)}

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
        a = self.submit(started_at=t0, elapsed=lambda q, r, e: 1, basis=prs(P))
        b = self.submit(started_at=t0 + datetime.timedelta(days=1),
                        elapsed=lambda q, r, e: 5 * 10 ** 306 if e == "xflare" else 1, basis=prs(Q))
        for u in ("/api/leaderboard/board", "/api/leaderboard/runs", f"/api/leaderboard/runs/{a}",
                  f"/api/leaderboard/runs/{b}", "/api/leaderboard/prsets", "/api/leaderboard/history",
                  f"/api/leaderboard/prset?prset={quote(PQ)}", f"/api/leaderboard/runs/{a}/history"):
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
        data = {**json_blocks(page), "repro-cmd": script}
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


# --- amendment A9: basis, links, PR sets, hero history -------------------------------------------------------------

P = {"repo": "xcena-dev/xflare-analytics", "number": 1561}
Q = {"repo": "apache/gluten", "number": 88}
R = {"repo": "facebookincubator/velox", "number": 12}
MAIN = {"kind": "main", "repo": "xcena-dev/xflare-analytics", "latest_pr": 1564}
PQ = "apache/gluten#88+xcena-dev/xflare-analytics#1561"


def prs(*members) -> dict:
    return {"kind": "pr", "prs": [dict(m) for m in members]}


def at(minutes: int):
    """A start time ``minutes`` after the fixtures' T0 (the run finishes about an hour later)."""
    return make_fixtures_T0 + timedelta(minutes=minutes)


from datetime import timedelta                                        # noqa: E402
from make_fixtures import T0 as make_fixtures_T0                      # noqa: E402


class A9Case(WebCase):
    def edit(self, run_id, field, body, status=200, **headers):
        r = self.c.post(f"/api/leaderboard/runs/{run_id}/{field}", json=body, headers=headers)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:500])
        return r.get_json()

    def history(self, run_id, query=""):
        r = self.c.get(f"/api/leaderboard/runs/{run_id}/history{query}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        return r.get_json()

    def basis_of(self, run_id):
        return self.c.get(f"/api/leaderboard/runs/{run_id}").get_json()["basis"]


class TestBasisContract(A9Case):
    """A9: the envelope's basis is the run's first claim; later changes go through the versioned edits only."""

    def test_envelope_basis_is_validated_and_normalized(self):
        m = build_manifest(run_id=rid())
        mixed = prs({"repo": "XCENA-dev/xflare-analytics", "number": 1561}, {"repo": "apache/Gluten", "number": 88})
        dry = self.post(build_envelope(m, basis=mixed), "?dry_run=1", status=200)
        self.assertEqual(dry["basis"]["prset"], PQ)
        self.assertEqual(dry["basis"]["label"], "gluten#88 + #1561")
        self.assertEqual(self.c.get("/api/leaderboard/runs").get_json(), [])          # a dry run stores nothing
        self.post(build_envelope(m, basis=mixed))
        got = self.basis_of(m["run_id"])
        self.assertEqual([(p["repo"], p["number"], p["url"]) for p in got["prs"]],
                         [("apache/gluten", 88, "https://github.com/apache/gluten/pull/88"),
                          ("xcena-dev/xflare-analytics", 1561, "https://github.com/xcena-dev/xflare-analytics/pull/1561")])
        main = self.submit(basis=MAIN)
        self.assertEqual(self.basis_of(main)["label"], "main · latest #1564")
        plain = self.submit()
        self.assertIsNone(self.basis_of(plain))                                         # absent = unknown
        self.assertIsNone(self.post(build_envelope(build_manifest(run_id=rid())), "?dry_run=1", status=200)["basis"])

    def test_bad_bases_are_refused(self):
        many = prs(*({"repo": "a/b", "number": n} for n in range(1, 18)))
        cases = {
            "kind": ({"kind": "branch"}, "$.basis.kind"),
            "null": (None, "$.basis: expected object"),
            "empty": (prs(), "$.basis.prs: 1 to 16 PRs, got 0"),
            "17": (many, "$.basis.prs: 1 to 16 PRs, got 17"),
            "dup": (prs(P, {"repo": "XCENA-DEV/xflare-analytics", "number": 1561}), "duplicate PR"),
            "dotdot": (prs({"repo": "xcena-dev/..", "number": 1}), "is not a GitHub owner/name"),
            "dot": (prs({"repo": "xcena-dev/.", "number": 1}), "is not a GitHub owner/name"),
            "parent": (prs({"repo": "../xflare-analytics", "number": 1}), "is not a GitHub owner/name"),
            "hyphen": (prs({"repo": "-x/y", "number": 1}), "is not a GitHub owner/name"),
            "space": (prs({"repo": "a b/c", "number": 1}), "is not a GitHub owner/name"),
            "deep": (prs({"repo": "a/b/c", "number": 1}), "is not a GitHub owner/name"),
            "zero": (prs({"repo": "a/b", "number": 0}), "is not a PR number"),
            "bool": (prs({"repo": "a/b", "number": True}), "is not a PR number"),
            "float": (prs({"repo": "a/b", "number": 1.0}), "is not a PR number"),
            "big": (prs({"repo": "a/b", "number": 2 ** 31}), "is not a PR number"),
            "main": ({"kind": "main", "repo": "a/b"}, "$.basis.latest_pr: missing"),
        }
        for name, (basis, error) in cases.items():
            with self.subTest(name):
                out = self.post(build_envelope(build_manifest(run_id=rid()), basis=basis), "?dry_run=1", status=400)
                self.assertTrue(any(error in e for e in out["errors"]), out["errors"])

    def test_an_identical_retry_never_changes_the_stored_basis(self):
        """pr2-01: submit {P,Q}, add R through the edit, replay the original envelope: still {P,Q,R}, same receipt."""
        m = build_manifest(run_id=rid())
        env = build_envelope(m, basis=prs(P, Q))
        first = self.post(env)
        state = self.edit(m["run_id"], "basis", {"actor": "web@x", "expected_revision": 1, "basis": prs(P, Q, R)})
        self.assertEqual(state["revision"], 2)
        again = self.post(env, status=200)
        self.assertEqual(again, first)
        self.assertEqual(len(self.basis_of(m["run_id"])["prs"]), 3)
        self.assertEqual(self.history(m["run_id"])["revision"], 2)
        # A run stored without a basis keeps none when retried with one: the claim is made through the edit.
        n = build_manifest(run_id=rid())
        self.post(build_envelope(n))
        self.post(build_envelope(n, basis=prs(P)), status=200)
        self.assertIsNone(self.basis_of(n["run_id"]))

    def test_revisions_conflicts_unlink_and_restore(self):
        run = self.submit(basis=prs(P))
        hist = self.history(run)
        self.assertEqual((hist["revision"], hist["updated_by"], len(hist["history"])), (1, "bench@example.com", 1))
        claim = hist["history"][0]
        self.assertEqual((claim["before"], claim["after"]["prset"]), (None, "xcena-dev/xflare-analytics#1561"))
        a = self.edit(run, "basis", {"actor": "a@x", "expected_revision": 1, "basis": prs(P, Q)})
        stale = self.edit(run, "basis", {"actor": "b@x", "expected_revision": 1, "basis": prs(R)}, status=409)
        self.assertEqual((stale["current"]["revision"], stale["current"]["basis"]["prset"]), (2, a["basis"]["prset"]))
        same = self.edit(run, "basis", {"actor": "c@x", "expected_revision": 2, "basis": prs(Q, P)})
        self.assertEqual(same["revision"], 2)                                       # an exact no-op adds no event
        gone = self.edit(run, "basis", {"actor": "d@x", "expected_revision": 2, "basis": None})
        self.assertIsNone(gone["basis"])
        page = self.c.get(f"{LB}/run/{run}").get_data(as_text=True)               # pr2-02: restorable from the run
        events = self.history(run)["history"]
        self.assertEqual([e["revision"] for e in events], [3, 2, 1])
        self.assertEqual(page.count('class="lb-btn lb-restore"'), 2)                # every value but the current one
        back = self.edit(run, "basis", {"actor": "e@x", "expected_revision": 3, "restore": events[1]["event_id"]})
        self.assertEqual((back["basis"]["prset"], back["revision"], back["updated_by"]), (PQ, 4, "e@x"))
        newest = self.history(run)["history"][0]
        self.assertEqual((newest["restored_from"], newest["before"], newest["actor"]), (events[1]["event_id"], None, "e@x"))
        link = self.edit(run, "report_url", {"actor": "f@x", "expected_revision": 4, "report_url": "https://claude.ai/artifact/abc"})
        self.assertEqual((link["report_url"], link["revision"]), ("https://claude.ai/artifact/abc", 5))
        self.assertEqual(self.c.get(f"/api/leaderboard/runs/{run}").get_json()["report_url"], "https://claude.ai/artifact/abc")
        # Content, receipt, timings and ranking are untouched by any of it.
        self.assertEqual(self.board()["rows"][0]["run_id"], run)

    def test_history_pages_by_event_id(self):
        run = self.submit()
        for revision in range(55):
            self.edit(run, "report_url", {"actor": "a", "expected_revision": revision, "report_url": f"https://x.example/{revision}"})
        first = self.history(run)
        self.assertEqual((len(first["history"]), first["more"], first["history"][0]["revision"]), (50, True, 55))
        older = self.history(run, f"?before={first['history'][-1]['event_id']}")
        self.assertEqual(([e["revision"] for e in older["history"]], older["more"]), ([5, 4, 3, 2, 1], False))
        self.assertEqual(self.c.get(f"/api/leaderboard/runs/{run}/history?before=x").status_code, 400)
        page = self.c.get(f"{LB}/run/{run}").get_data(as_text=True)
        self.assertIn(f"before={first['history'][-1]['event_id']}", page)

    def test_edit_refusals(self):
        run, other = self.submit(), self.submit(basis=prs(P))
        ok = {"actor": "a", "expected_revision": 0}
        r = self.c.post(f"/api/leaderboard/runs/{run}/report_url", data=json.dumps({**ok, "report_url": None}),
                        content_type="text/plain")
        self.assertEqual(r.status_code, 415)
        self.edit(run, "report_url", {**ok, "report_url": None}, status=403, Origin="http://evil.example")
        self.edit("not-a-run", "basis", {**ok, "basis": None}, status=404)
        self.edit(rid(), "basis", {**ok, "basis": None}, status=404)
        foreign = self.history(other)["history"][0]["event_id"]
        for body, error in (({**ok}, "exactly one"), ({**ok, "report_url": None, "restore": 1}, "exactly one"),
                            ({**ok, "restore": True}, "restore: an event id"),
                            ({**ok, "restore": 2 ** 63}, "restore: an event id"),
                            ({"actor": "x" * 257, "expected_revision": 0, "report_url": None}, "actor:"),
                            ({"actor": " ", "expected_revision": 0, "report_url": None}, "actor:"),
                            ({"actor": "a", "expected_revision": -1, "report_url": None}, "expected_revision"),
                            ({"actor": "a", "expected_revision": True, "report_url": None}, "expected_revision")):
            with self.subTest(body=str(body)[:60]):
                self.assertIn(error, " ".join(self.edit(run, "report_url", body, status=400)["errors"]))
        self.assertIn("not a change of this report_url",
                      self.edit(run, "report_url", {**ok, "restore": foreign}, status=400)["errors"][0])
        self.assertIn("not a change of this report_url",
                      self.edit(other, "report_url", {"actor": "a", "expected_revision": 1, "restore": foreign},
                                status=400)["errors"][0])                           # a basis event is not a report
        for link in ("javascript:alert(1)", "ftp://x.example/a", "https://user:pw@x.example/", "https://x.example/a b",
                     "https://x.example/<script>", "HTTPS://x.example/", "https://", "//x.example/a",
                     "https://x.example/" + "a" * 2048, "https://x.example\\@evil.example/", "https://ex ample.com"):
            with self.subTest(link=link[:40]):
                self.assertIn("report_url: an http(s) URL", self.edit(run, "report_url", {**ok, "report_url": link}, status=400)["errors"][0])
        self.assertEqual(self.edit(run, "basis", {**ok, "basis": {"kind": "pr", "prs": []}}, status=400)["errors"],
                         ["basis.prs: 1 to 16 PRs, got 0"])
        for link in ("http://confluence.example:8090/pages/viewpage.action?pageId=12&x=%ED%95%9C#s",
                     "https://claude.ai/code/artifact/0b5f", "https://docs.example.com/d/1?a=(b)"):
            with self.subTest(ok=link):
                revision = self.history(run)["revision"]
                self.edit(run, "report_url", {"actor": "a", "expected_revision": revision, "report_url": link})

    def test_withdrawn_runs_stay_editable(self):
        run = self.submit()
        self.c.post(f"/api/leaderboard/runs/{run}/withdraw", json={"actor": "a", "reason": "b"})
        self.edit(run, "basis", {"actor": "a", "expected_revision": 0, "basis": MAIN})
        page = self.c.get(f"{LB}/run/{run}").get_data(as_text=True)
        self.assertIn('id="lb-basis-edit"', page)
        self.assertNotIn('id="lb-withdraw"', page)

    def test_hostile_values_stay_inert(self):
        run = self.submit(basis=prs(P))
        html_actor = HOSTILE[0]
        self.edit(run, "report_url", {"actor": html_actor, "expected_revision": 1,
                                "report_url": "https://x.example/a?q='onmouseover=alert(1)"})
        self.edit(run, "basis", {"actor": HOSTILE[1], "expected_revision": 2, "basis": None})
        page = self.c.get(f"{LB}/run/{run}").get_data(as_text=True)
        for text in HOSTILE[:2]:
            self.assertNotIn(text, page)
            self.assertIn(html.escape(text, quote=False).replace("'", "&#39;")[:20], page)
        self.assertIn('href="https://x.example/a?q=&#39;onmouseover=alert(1)"', page)
        self.assertIn('rel="noopener noreferrer"', page)


class TestPrUrls(A9Case):
    """A9: every PR URL is https://github.com/<repo>/pull/<n>, built by the server from the validated, lower-case key;
    a hostile repository never gets stored, so it never reaches an href."""

    GITHUB = re.compile(r"https://github\.com/[a-z0-9-]+/[a-z0-9._-]+/pull/[1-9][0-9]*")

    def test_urls_come_from_validated_parts_only(self):
        mixed = prs({"repo": "Apache/Gluten", "number": 88}, {"repo": "a-b/c.d_e", "number": 2147483647})
        run = self.submit(basis=mixed)
        main = self.submit(basis={"kind": "main", "repo": "Facebookincubator/Velox", "latest_pr": 12})
        urls = [p["url"] for p in self.basis_of(run)["prs"]] + [self.basis_of(main)["url"]]
        self.assertEqual(urls, ["https://github.com/a-b/c.d_e/pull/2147483647", "https://github.com/apache/gluten/pull/88",
                                "https://github.com/facebookincubator/velox/pull/12"])
        pages = [self.c.get(f"{LB}/run/{run}").get_data(as_text=True), self.c.get(f"{LB}/run/{main}").get_data(as_text=True),
                 self.c.get(f"{LB}/prset?prset={quote(self.basis_of(run)['prset'])}").get_data(as_text=True)]
        hrefs = [h for page in pages for h in re.findall(r'href="(https://github[^"]*)"', page)]
        self.assertTrue(hrefs)
        for h in hrefs:
            self.assertRegex(h, f"^{self.GITHUB.pattern}$")
        self.assertIn('href="https://github.com/facebookincubator/velox/pull/12"', pages[1])   # main's latest #N
        for repo in ('a"onmouseover=x/b', "javascript:alert(1)/x", "a/b?x=1", "a/b#frag", "evil.example/../a",
                     "a/b/../../c", "%2e%2e/x", "a/<b>", "ａ/b"):
            with self.subTest(repo=repo):
                env = build_envelope(build_manifest(run_id=rid()), basis=prs({"repo": repo, "number": 1}))
                self.post(env, status=400)
                self.edit(run, "basis", {"actor": "a", "expected_revision": 1, "basis": prs({"repo": repo, "number": 1})},
                          status=400)
        for key in ('a"x/b#1', "javascript:x/b#1", "a/b#1+c/d#x"):
            with self.subTest(key=key):
                self.assertEqual(self.c.get(f"{LB}/prset?prset={quote(key)}").status_code, 404)


class TestBasisFilters(A9Case):
    """A9: `pr` (membership), `prset` (exact set) and `basis` filters, literal and token, on /board and /runs."""

    def setUp(self):
        super().setUp()
        self.ids = {"P": self.submit(basis=prs(P), elapsed=flat_elapsed(500)),
                    "PQ": self.submit(basis=prs(Q, P), elapsed=flat_elapsed(400)),
                    "QR": self.submit(basis=prs(Q, R), elapsed=flat_elapsed(450)),
                    "main": self.submit(basis=MAIN, elapsed=flat_elapsed(480)),
                    "none": self.submit(elapsed=flat_elapsed(470))}

    def names(self, query):
        found = {r["run_id"] for r in self.board(query)["rows"]}
        runs = {r["run_id"] for r in self.c.get("/api/leaderboard/runs" + query).get_json()}
        self.assertEqual(found, runs, query)
        return {k for k, v in self.ids.items() if v in found}

    def test_literals_and_tokens_agree(self):
        key_p = "xcena-dev/xflare-analytics#1561"
        pq, qr = "prset:" + PQ, "prset:apache/gluten#88+facebookincubator/velox#12"
        cases = {f"pr={quote(key_p)}": {"P", "PQ"}, f"basis={quote(pq)}": {"PQ"}, "basis=main": {"main"},
                 "basis=-": {"none"}, "basis=pr": {"P", "PQ", "QR"}, "pr=-": {"main", "none"},
                 f"pr={quote('apache/gluten#88')}": {"PQ", "QR"},
                 f"pr={quote(key_p)}&basis={quote(qr)}": set(),                      # both apply
                 f"pr={quote('apache/gluten#88')}&basis={quote(qr)}": {"QR"},
                 f"basis={quote('prset:XCENA-dev/xflare-analytics#1561+Apache/gluten#88')}": {"PQ"},  # any case/order
                 "pr=XCENA-dev/xflare-analytics%231561": {"P", "PQ"},
                 f"basis={quote('prset:' + PQ + '+')}": set()}                       # not a key: compared as sent
        for query, want in cases.items():
            with self.subTest(query):
                self.assertEqual(self.names("?" + query), want)
                literal = dict(urllib.parse.parse_qsl(query))
                # The select sends basis_t, a token of the canonical value.
                if "basis" in literal and "XCENA" not in query and not literal["basis"].endswith("+"):
                    tokens = "&".join(f"{k}_t={quote(facet_token(v))}" if k == "basis" else f"{k}={quote(v)}"
                                      for k, v in literal.items())
                    self.assertEqual(self.names("?" + tokens), want)
        board = self.board(f"?basis={quote('prset:' + PQ)}")
        self.assertEqual(board["filter_tokens"]["basis"], facet_token("prset:" + PQ))
        self.assertNotIn("pr", board["facets"])                                      # one PR is a chip, not a select
        self.assertEqual([o["label"] for o in board["facets"]["basis"]],
                         ["main", "PR-based (all)", "PR set gluten#88 + velox#12", "PR set gluten#88 + #1561",
                          "PR set #1561", "unknown (no basis)"])                    # sets in key order
        self.assertEqual(board["rows"][0]["basis"]["prset"], PQ)
        self.assertEqual(self.board(f"?pr={quote(key_p)}")["filter"]["pr"], key_p)

    def test_a_basis_edit_moves_the_run_between_filters(self):
        self.edit(self.ids["none"], "basis", {"actor": "a", "expected_revision": 0, "basis": prs(P, Q)})
        self.assertEqual(self.names(f"?basis={quote('prset:' + PQ)}"), {"PQ", "none"})


def quote(text: str) -> str:
    return urllib.parse.quote(text, safe="")


import urllib.parse                                                   # noqa: E402


class TestPrSets(A9Case):
    """A9: the PR-set list (representative = best total-eligible run under the tab and filter), its page and compare."""

    def setUp(self):
        super().setUp()
        self.pq_slow = self.submit(basis=prs(P, Q), elapsed=flat_elapsed(500), started_at=at(0))
        self.pq_fast = self.submit(basis=prs(Q, P), elapsed=flat_elapsed(400), started_at=at(100))
        self.pq_other = self.submit(basis=prs(P, Q), elapsed=flat_elapsed(300), executors=8, started_at=at(200))
        self.r_only = self.submit(basis=prs(R), elapsed=flat_elapsed(450), started_at=at(300))
        m = build_manifest(run_id=rid(), elapsed=flat_elapsed(350), started_at=at(400))
        m["run"]["exit_status"] = "failed"                                           # stored, never total-eligible
        self.post(build_envelope(m, basis=prs({"repo": "a/b", "number": 9})))
        self.partial = m["run_id"]
        self.main = self.submit(basis=MAIN, elapsed=flat_elapsed(420), started_at=at(500))

    def prsets(self, query="", status=200):
        r = self.c.get("/api/leaderboard/prsets" + query)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:300])
        return r.get_json()

    def test_list_representatives_and_sorts(self):
        out = self.prsets()
        self.assertEqual([x["prset"] for x in out["prsets"]],
                         [PQ, "facebookincubator/velox#12", "a/b#9"])                # best first, none last
        pq = out["prsets"][0]
        self.assertEqual((pq["best"]["run_id"], pq["run_count"], pq["eligible_count"], pq["is_hero"]),
                         (self.pq_fast, 2, 2, True))                                  # 8×6 is off the default filter
        self.assertIsNone(out["prsets"][2]["best"])
        r_vs = out["prsets"][1]["vs_hero_pct"]
        self.assertAlmostEqual(r_vs, 22 * 50 * 100 / (22 * 400 + 253))               # totals: 22 × base + Σq
        anyshape = self.prsets("?executors=&executor_cores=")["prsets"][0]
        self.assertEqual(anyshape["best"]["run_id"], self.pq_other)                 # the same rank() as the board
        self.assertEqual([x["prset"] for x in self.prsets("?sort=number")["prsets"]],
                         [PQ, "facebookincubator/velox#12", "a/b#9"])                # highest PR number first
        self.assertEqual([x["prset"] for x in self.prsets("?sort=recent")["prsets"]],
                         ["a/b#9", "facebookincubator/velox#12", PQ])
        self.prsets("?sort=size", status=400)
        self.c.post(f"/api/leaderboard/runs/{self.pq_fast}/withdraw", json={"actor": "a", "reason": "b"})
        self.assertEqual(self.prsets()["prsets"][1]["best"]["run_id"], self.pq_slow)

    def test_compare_by_prset(self):
        r = self.c.get(f"{LB}/compare?prsets={quote(PQ)},{quote('facebookincubator/velox#12')}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        page = r.get_data(as_text=True)
        self.assertLess(page.index(self.pq_fast[:8]), page.index(self.r_only[:8]))
        self.assertIn("gluten#88 + #1561", page)
        self.assertIn("velox#12", page)
        wide = self.c.get(f"{LB}/compare?prsets={quote(PQ)},{quote('facebookincubator/velox#12')}"
                          "&executors=&executor_cores=").get_data(as_text=True)
        self.assertIn(self.pq_other[:8], wide)
        self.assertEqual(self.c.get(f"{LB}/compare?prsets={quote(PQ)},a%2Fb%239").status_code, 404)
        self.assertEqual(self.c.get(f"{LB}/compare?prsets={quote(PQ)},{quote(PQ)}").status_code, 400)
        self.assertEqual(self.c.get(f"{LB}/compare?prsets={quote(PQ)},nope").status_code, 400)
        self.assertEqual(self.c.get(f"{LB}/compare?prsets={quote(PQ)},a%2Fb%239&runs={self.main}").status_code, 400)
        for bad in ("sf=999", "sf=abc", "target=nope"):                              # page errors are HTML
            r = self.c.get(f"{LB}/compare?prsets={quote(PQ)},{quote('facebookincubator/velox#12')}&{bad}")
            self.assertEqual((r.status_code, r.mimetype), (400, "text/html"), bad)

    def test_prset_page_and_api(self):
        reordered = quote("XCENA-dev/xflare-analytics#1561+apache/gluten#88")
        api = self.c.get(f"/api/leaderboard/prset?prset={reordered}").get_json()
        self.assertEqual((api["prset"], api["revision"], api["report_url"]), (PQ, 0, None))
        # Every configuration of the set, in board order: the 8×6 run leads here (the list's filter is not applied).
        self.assertEqual([r["run_id"] for r in api["runs"]], [self.pq_other, self.pq_fast, self.pq_slow])
        self.assertEqual([r["place"] for r in api["runs"]], [1, 2, 3])
        self.assertEqual([p["check"] for p in api["prs"]], [None, None])
        page = self.c.get(f"{LB}/prset?prset={reordered}")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("https://github.com/apache/gluten/pull/88", text)
        self.assertIn('id="lb-report-edit"', text)
        self.assertEqual(self.c.get(f"{LB}/prset?prset=a%2F..%231").status_code, 404)
        self.assertEqual(self.c.get("/api/leaderboard/prset?prset=").status_code, 400)
        empty = self.c.get(f"/api/leaderboard/prset?prset={quote('z/z#1')}").get_json()
        self.assertEqual(empty["runs"], [])
        # The set's report URL: versioned, restorable, kept when no run claims the set any more.
        post = lambda body, status=200: self.c.post(f"/api/leaderboard/prset?prset={reordered}", json=body)
        r = post({"actor": "a", "expected_revision": 0, "report_url": "https://claude.ai/artifact/x1"})
        self.assertEqual((r.status_code, r.get_json()["revision"]), (200, 1))
        self.assertEqual(post({"actor": "b", "expected_revision": 0, "report_url": None}).status_code, 409)
        post({"actor": "b", "expected_revision": 1, "report_url": None})
        event = self.c.get(f"/api/leaderboard/prset?prset={reordered}").get_json()["history"][1]["event_id"]
        r = post({"actor": "c", "expected_revision": 2, "restore": event})
        self.assertEqual(r.get_json()["report_url"], "https://claude.ai/artifact/x1")
        self.assertEqual(self.prsets()["prsets"][0]["report_url"], "https://claude.ai/artifact/x1")
        self.assertEqual(self.c.post(f"/api/leaderboard/prset?prset={reordered}", data="{}",
                                     content_type="text/plain").status_code, 415)

    def test_pr_checks_and_completion(self):
        check = lambda repo, n, ids, status=200, **kw: self.c.post(
            f"/api/leaderboard/prs/{repo}/{n}/check", json={"actor": "skill@x", "run_ids": ids, **kw})
        completion = lambda run: self.c.get(f"/api/leaderboard/prset?prset={quote(PQ)}").get_json()["runs"][
            [r["run_id"] for r in self.c.get(f"/api/leaderboard/prset?prset={quote(PQ)}").get_json()["runs"]].index(run)
        ]["completion"]
        self.assertEqual(completion(self.pq_fast)["complete"], False)
        r = check("xcena-dev/xflare-analytics", 1561, [self.pq_fast, self.pq_slow], body_sha256="a" * 64, source="file")
        self.assertEqual((r.status_code, r.get_json()["listed_count"]), (200, 2))
        self.assertEqual([m["status"] for m in completion(self.pq_fast)["members"]], ["not checked", "listed"])
        check("Apache/Gluten", 88, [self.pq_fast])                                  # names any case of the repo
        self.assertTrue(completion(self.pq_fast)["complete"])
        self.assertEqual([m["status"] for m in completion(self.pq_slow)["members"]], ["not listed", "listed"])
        run_page = self.c.get(f"{LB}/run/{self.pq_fast}").get_data(as_text=True)
        self.assertIn(">complete<", run_page)
        # Adding a member re-opens it: R's body was never checked.
        self.edit(self.pq_fast, "basis", {"actor": "a", "expected_revision": 1, "basis": prs(P, Q, R)})
        detail = self.c.get(f"/api/leaderboard/runs/{self.pq_fast}").get_json()
        self.assertFalse(self.c.get(f"/api/leaderboard/prset?prset={quote(PQ + '+facebookincubator/velox#12')}")
                         .get_json()["runs"][0]["completion"]["complete"])
        self.assertEqual(detail["basis"]["prs"][2]["key"], "xcena-dev/xflare-analytics#1561")
        for path, body, status in (("a/b/1", {"actor": "x", "run_ids": ["nope"]}, 400),
                                   ("a/b/1", {"actor": "x", "run_ids": [], "body_sha256": "A" * 64}, 400),
                                   ("a/b/1", {"actor": "", "run_ids": []}, 400),
                                   ("a/b/1", {"actor": "x", "run_ids": [rid()] * 1001}, 400),
                                   ("a/../1", {"actor": "x", "run_ids": []}, 404),
                                   ("a/b/0", {"actor": "x", "run_ids": []}, 404)):
            with self.subTest(path=path, body=str(body)[:40]):
                self.assertEqual(self.c.post(f"/api/leaderboard/prs/{path}/check", json=body).status_code, status)
        self.assertEqual(self.c.post("/api/leaderboard/prs/a/b/1/check", json={"actor": "x", "run_ids": []},
                                     headers={"Origin": "http://evil.example"}).status_code, 403)


class TestHeroHistory(A9Case):
    """A9: best so far among today's eligible runs, per metric, labelled with each hero's basis."""

    def hist(self, query="", status=200):
        r = self.c.get("/api/leaderboard/history" + query)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:300])
        return r.get_json()

    def test_total_record_breakers_ties_and_window(self):
        a = self.submit(basis=prs(P), elapsed=flat_elapsed(500), started_at=at(0))
        b = self.submit(elapsed=flat_elapsed(600), started_at=at(1440))                   # slower: a point only
        c = self.submit(basis=MAIN, elapsed=flat_elapsed(400), started_at=at(2880))
        tie = self.submit(elapsed=flat_elapsed(400), started_at=at(4320))                 # same value, same repeats
        more = self.submit(elapsed=flat_elapsed(400), repeats=5, started_at=at(5760))     # same value, more repeats
        gone = self.submit(elapsed=flat_elapsed(100), started_at=at(7200))
        self.c.post(f"/api/leaderboard/runs/{gone}/withdraw", json={"actor": "a", "reason": "b"})
        self.submit(elapsed=flat_elapsed(100), executors=8, started_at=at(8640))          # off the default filter
        out = self.hist()
        self.assertEqual([p["run_id"] for p in out["points"]], [a, b, c, tie, more])
        self.assertEqual([h["run_id"] for h in out["heroes"]], [a, c, more])
        self.assertEqual(self.board()["hero"]["run_id"], more)                          # the last hero is the board's
        self.assertEqual([h["basis"] and h["basis"]["label"] for h in out["heroes"]], ["#1561", "main · latest #1564", None])
        self.assertAlmostEqual(out["heroes"][1]["delta_pct"], (400 - 500) * 22 * 100 / (500 * 22 + 253))
        self.assertEqual([p["hero"] for p in out["points"]], [True, False, True, False, True])
        self.assertLess(out["points"][0]["t"], out["points"][1]["t"])
        window = self.hist(f"?from={self.day(tie)}")
        self.assertEqual([h["run_id"] for h in window["heroes"]], [c, more])
        self.assertEqual([h["entering"] for h in window["heroes"]], [True, False])

    def day(self, run_id):
        return self.c.get(f"/api/leaderboard/runs/{run_id}").get_json()["finished_at"][:10]

    def test_per_query_series(self):
        """q3's history: a run with q3 ineligible is no point there (but is on the Total of q5); ties as the board."""
        def elapsed(base, q3=None):
            return lambda q, r, e: (q3 if q == 3 and q3 is not None and e == "xflare" else base + q)
        a = self.submit(elapsed=elapsed(500), started_at=at(0))
        broken = self.submit(elapsed=elapsed(100, q3=0), started_at=at(100))              # q3 not eligible
        tie = self.submit(elapsed=elapsed(500), started_at=at(200))
        fast3 = self.submit(elapsed=elapsed(600, q3=50), started_at=at(300))
        q3 = self.hist("?metric=q3")
        self.assertEqual(q3["metric"], "q3")
        self.assertEqual([p["run_id"] for p in q3["points"]], [a, tie, fast3])
        self.assertEqual([h["run_id"] for h in q3["heroes"]], [a, fast3])
        self.assertEqual([h["xflare_ms"] for h in q3["heroes"]], [503, 50])
        q5 = self.hist("?metric=q5")
        self.assertEqual([h["run_id"] for h in q5["heroes"]], [a, broken])
        total = self.hist()
        self.assertNotIn(broken, [p["run_id"] for p in total["points"]])
        self.assertEqual(self.board("?metric=q3")["rows"][0]["run_id"], q3["heroes"][-1]["run_id"])
        for bad in ("q23", "Total", "q0"):
            self.hist(f"?metric={bad}", status=400)

    def test_vs_target_per_metric_and_goal_on_the_total_only(self):
        target = self.submit(executors=8, elapsed=flat_elapsed(500, velox=2000), started_at=at(0))
        hero = self.submit(elapsed=flat_elapsed(400, velox=3000), started_at=at(100))
        self.pin_target(target, goal=4.0)
        total = self.hist()
        self.assertEqual(total["target"]["goal"], 4.0)
        self.assertAlmostEqual(total["heroes"][0]["vs_target"], sum(2000 + q for q in range(1, 23))
                               / sum(400 + q for q in range(1, 23)))
        q7 = self.hist("?metric=q7")
        self.assertIsNone(q7["target"]["goal"])
        self.assertEqual(q7["target"]["velox_ms"], 2007)
        self.assertAlmostEqual(q7["heroes"][0]["vs_target"], 2007 / 407)
        self.assertEqual(q7["heroes"][0]["run_id"], hero)
        self.assertIsNone(self.hist("?target=none")["target"])

    def test_answers_name_the_script(self):
        script = json.loads(self.page_data()["lb-page-data"])["script"]
        for url in ("/api/leaderboard/history", "/api/leaderboard/prsets", "/api/leaderboard/history?metric=bad"):
            self.assertEqual(self.c.get(url).headers.get("X-Leaderboard-Script"), script, url)


class TestA9Storage(A9Case):
    def test_v2_database_gains_the_a9_tables(self):
        run = self.submit()
        db = os.path.join(self.data, "leaderboard.sqlite3")
        self.pin_target(run)
        import sqlite3
        c = sqlite3.connect(db)
        c.executescript("DROP TABLE run_attribution; DROP TABLE prset_attribution; DROP TABLE attribution_events;"
                        " DROP TABLE pr_checks; PRAGMA user_version = 2;")
        c.close()
        app = lb_app.create_app(config(self.data))
        lb_store._stores.clear()
        client = app.test_client()
        self.assertIsNone(client.get(f"/api/leaderboard/runs/{run}").get_json()["basis"])
        r = client.post(f"/api/leaderboard/runs/{run}/basis", json={"actor": "a", "expected_revision": 0, "basis": MAIN})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client.get("/api/leaderboard/targets/100/snappy").get_json()["run_id"], run)
        c = sqlite3.connect(db)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 3)
        # An older binary (going back a version) lowers the number; the tables and their data stay, and this one
        # raises it again without touching them.
        c.execute("PRAGMA user_version = 2")
        c.close()
        lb_store._stores.clear()
        again = lb_app.create_app(config(self.data)).test_client()
        self.assertEqual(again.get(f"/api/leaderboard/runs/{run}").get_json()["basis"]["label"], "main · latest #1564")
        c = sqlite3.connect(db)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 3)
        c.close()


class TestA9BoardScript(WebCase):
    """A9 on the board page: the Basis column, the hero-history panel (its metric follows the board's tab and has
    its own selector; only the newest answer draws) and the PR-set panel (compare by set)."""

    drive = TestBoardScript.drive

    def setUp(self):
        super().setUp()
        self.a = self.submit(basis=prs(P, Q), elapsed=flat_elapsed(500), started_at=at(0))
        self.b = self.submit(basis=MAIN, elapsed=flat_elapsed(400), started_at=at(1440))
        m = build_manifest(run_id=rid(), elapsed=flat_elapsed(450), started_at=at(2880))
        self.post(build_envelope(m, basis=prs(*({"repo": "a/b", "number": n} for n in range(1, 6)))))
        self.c.post(f"/api/leaderboard/runs/{m['run_id']}/report_url",
                    json={"actor": "a", "expected_revision": 1, "report_url": "https://claude.ai/artifact/x"})
        get = lambda u: self.c.get(u).get_data(as_text=True)
        self.responses = {"board": get("/api/leaderboard/board"), "q3": get("/api/leaderboard/board?metric=q3"),
                          "hist": get("/api/leaderboard/history"), "hist3": get("/api/leaderboard/history?metric=q3"),
                          "hist5": get("/api/leaderboard/history?metric=q5"), "sets": get("/api/leaderboard/prsets")}

    def test_basis_and_report_columns(self):
        out = self.drive([{"answer": 0, "with": "board"}], self.responses)
        texts = [r["text"][2] for r in out["boardRows"]]
        self.assertIn("main · latest #1564", texts)
        five = next(r for r in out["boardRows"] if "+2" in r["text"][2])
        self.assertEqual(five["text"][2:4], ["b#1 ↗ b#2 ↗ b#3 ↗ +2", "↗ Report"])   # three chips, the rest counted
        self.assertIn("https://claude.ai/artifact/x", five["hrefs"])
        self.assertEqual([h for h in five["hrefs"] if "github" in h],
                         [f"https://github.com/a/b/pull/{n}" for n in (1, 2, 3)])    # each chip's ↗
        self.assertEqual([r["text"][3] for r in out["boardRows"]].count("—"), 2)

    def test_only_github_pr_urls_become_hrefs(self):
        """A9: the page makes an href of a PR only from a https://github.com/<repo>/pull/<n> URL; a tampered answer's
        other URL (another origin, a script URL, a path that escapes) is shown as text, never linked."""
        board = json.loads(self.responses["board"])
        row = next(r for r in board["rows"] if r["basis"] and r["basis"]["kind"] == "pr" and len(r["basis"]["prs"]) == 2)
        bad = ["javascript:alert(1)", "https://evil.example/apache/gluten/pull/88",
               "https://github.com/apache/gluten/pull/88/../../../evil", "https://github.com.evil.example/a/b/pull/1"]
        row["basis"]["prs"][0]["url"], row["basis"]["prs"][1]["url"] = bad[0], bad[1]
        main = next(r for r in board["rows"] if r["basis"] and r["basis"]["kind"] == "main")
        main["basis"]["url"] = bad[2]
        board["hero"]["basis"] = {**row["basis"], "prs": [{**row["basis"]["prs"][0], "url": bad[3]}]}
        out = self.drive([{"answer": 0, "with": "tampered"}], {**self.responses, "tampered": json.dumps(board)})
        links = [h for r in out["boardRows"] for h in r["hrefs"]]
        for url in bad:
            self.assertNotIn(url, links)
        self.assertIn("gluten#88", [r["text"][2] for r in out["boardRows"] if "gluten" in r["text"][2]][0])

    def test_a_pr_chip_filters_and_its_chip_removes_it(self):
        """A9: clicking a PR chip asks the board for that PR (pr=<key>) and shows it as a removable chip."""
        picked = self.c.get(f"/api/leaderboard/board?pr={quote('apache/gluten#88')}").get_data(as_text=True)
        out = self.drive([{"answer": 0, "with": "board"}, {"click": "gluten#88"}, {"answer": 1, "with": "picked"},
                          {"click": "gluten#88 ×"}], {**self.responses, "picked": picked})
        self.assertIn("pr=apache%2Fgluten%2388", out["requests"][1])
        self.assertEqual(len(out["boardRows"]), 1)
        self.assertNotIn("pr=", out["requests"][2])                                  # the chip's × lifts it again

    def test_a_long_pr_literal_is_cut_on_its_chip(self):
        board = json.loads(self.responses["board"])
        board["filter"]["pr"] = "x" * 3000
        out = self.drive([{"answer": 0, "with": "long"}], {**self.responses, "long": json.dumps(board)})
        chip = [b for b in out["buttons"] if b["text"].endswith(" ×")]
        self.assertEqual(len(chip), 1)
        self.assertLessEqual(len(chip[0]["text"]), 512 + 3)
        self.assertLessEqual(len(chip[0]["title"]), 512 + 10)

    def test_a_panel_answer_for_another_script_after_a_new_board_empties_the_panel(self):
        """Round 2: the board moved on (a PR chip), the history's answer comes from another script: nothing of the
        old context stays in the panel."""
        picked = self.c.get(f"/api/leaderboard/board?pr={quote('apache/gluten#88')}").get_data(as_text=True)
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "hist"},
                          {"click": "gluten#88"}, {"answer": 2, "with": "picked"}, {"answer": 3, "with": "other"}],
                         {**self.responses, "picked": picked, "other": self.responses["hist"]},
                         meta={"other": {"headers": {"x-leaderboard-script": "0" * 12}}})
        self.assertEqual(out["historyRows"], [])
        self.assertEqual(out["status"], "서버가 다른 버전으로 바뀌었습니다. 페이지를 새로고침하세요.")

    def test_history_follows_the_board_metric_and_its_own_selector(self):
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "hist"},
                          {"tab": ["lb-metric-tabs", "Q3"]}, {"answer": 2, "with": "q3"}, {"answer": 3, "with": "hist3"}],
                         self.responses)
        self.assertIn("metric=total", out["requests"][1])
        self.assertIn("/api/leaderboard/history?", out["requests"][3])
        self.assertIn("metric=q3", out["requests"][3])                               # follows the board's Q3 tab
        self.assertEqual(out["historyMetric"], "Q3")
        self.assertTrue(out["historyNote"].startswith("Q3:"), out["historyNote"])
        # Its own selector: Q5 there; a late Q3 answer (asked first) never draws over it.
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"click": "Q3"},
                          {"click": "Q5"}, {"answer": 3, "with": "hist5"}, {"answer": 2, "with": "hist3"}],
                         self.responses)
        self.assertEqual([u for u in out["requests"] if "history" in u][-2:][0].count("metric=q3"), 1)
        self.assertTrue(out["historyNote"].startswith("Q5:"), out["historyNote"])
        self.assertIn("metric=total", out["requests"][1])

    def test_history_table_and_chart(self):
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "hist"}],
                         self.responses)
        self.assertEqual(len(out["historyRows"]), 2)                                 # newest hero first
        self.assertIn("main · latest #1564", out["historyRows"][0]["text"][4])
        self.assertIn("gluten#88 ↗ #1561 ↗", out["historyRows"][1]["text"][4])
        hist = out["charts_all"][-1]
        self.assertEqual([d["label"] for d in hist], ["eligible runs", "best so far (hero)"])
        self.assertEqual(len(hist[0]["data"]), 3)
        self.assertEqual([p["y"] for p in hist[1]["data"]], [500 * 22 + 253, 400 * 22 + 253, 400 * 22 + 253])

    def test_history_draws_the_target_and_its_goal(self):
        self.pin_target(self.a, goal=3.0)
        responses = {**self.responses, "board": self.c.get("/api/leaderboard/board").get_data(as_text=True),
                     "hist": self.c.get("/api/leaderboard/history").get_data(as_text=True),
                     "hist3": self.c.get("/api/leaderboard/history?metric=q3").get_data(as_text=True)}
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "hist"}],
                         responses)
        self.assertEqual([d["label"] for d in out["charts_all"][-1]],
                         ["eligible runs", "best so far (hero)", "speedup vs target", "goal 3.00×"])
        q3 = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"click": "Q3"},
                         {"answer": 2, "with": "hist3"}], responses)
        self.assertEqual([d["label"] for d in q3["charts_all"][-1]],
                         ["eligible runs", "best so far (hero)", "speedup vs target"])  # no goal for a query

    def test_prset_panel_compares_two_sets(self):
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-prsets"}, {"answer": 1, "with": "sets"},
                          {"check": PQ}, {"check": "a/b#1+a/b#2+a/b#3+a/b#4+a/b#5"}, {"fire": "lb-prsets-compare"}],
                         self.responses)
        self.assertEqual(len(out["prsetRows"]), 2)
        self.assertEqual([r["text"][4] for r in out["prsetRows"]], ["+12.2%", "+24.3%"])      # vs the main hero
        url = urllib.parse.urlsplit(out["location"])
        self.assertEqual(url.path, f"{LB}/compare")
        self.assertEqual(urllib.parse.parse_qs(url.query)["prsets"], [f"{PQ},a/b#1+a/b#2+a/b#3+a/b#4+a/b#5"])
        self.assertEqual(self.c.get(out["location"]).status_code, 200)

    def test_a_panel_answer_for_another_script_asks_for_a_reload(self):
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "hist"}],
                         self.responses, meta={"hist": {"headers": {"x-leaderboard-script": "0" * 12}}})
        self.assertEqual(out["status"], "서버가 다른 버전으로 바뀌었습니다. 페이지를 새로고침하세요.")
        self.assertEqual(out["historyRows"], [])

    FIVE = "a/b#1+a/b#2+a/b#3+a/b#4+a/b#5"
    HISTORY_OPEN = [{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "hist"}]
    SETS_PICKED = [{"answer": 0, "with": "board"}, {"open": "lb-prsets"}, {"answer": 1, "with": "sets"},
                   {"check": PQ}, {"check": FIVE}]

    @staticmethod
    def history_chart(out):
        return [c for c in out["chartInfo"] if "heroLabels" in c["plugins"]]

    def test_a_panel_answer_of_an_older_board_never_draws(self):
        """A9: a panel answer asked under the board's previous tab or filter never draws, also when it arrives before
        the new board answer, and a closed panel's answer never draws; the new board asks the open panel again."""
        # History open and pending, the board's Q3 tab clicked, the Total history answers first.
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"tab": ["lb-metric-tabs", "Q3"]},
                          {"answer": 1, "with": "hist"}, {"answer": 2, "with": "q3"}], self.responses)
        self.assertEqual((out["historyRows"], self.history_chart(out)), ([], []), out.get("historyNote"))
        self.assertIn("metric=q3", out["requests"][3])
        # PR sets open and pending, a PR chip filters the board, the unfiltered answer comes first.
        picked = self.c.get(f"/api/leaderboard/board?pr={quote('apache/gluten#88')}").get_data(as_text=True)
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-prsets"}, {"click": "gluten#88"},
                          {"answer": 1, "with": "sets"}, {"answer": 2, "with": "picked"}],
                         {**self.responses, "picked": picked})
        self.assertEqual(out["prsetRows"], [])
        self.assertIn("pr=apache%2Fgluten%2388", out["requests"][3])
        # Closed while pending: after a tab change (its answer after the new board), and without one.
        for steps in ([{"tab": ["lb-metric-tabs", "Q3"]}, {"answer": 2, "with": "q3"}, {"answer": 1, "with": "hist"}],
                      [{"answer": 1, "with": "hist"}]):
            with self.subTest(steps=steps):
                out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"close": "lb-history"}]
                                 + steps, self.responses)
                self.assertEqual((out["historyRows"], self.history_chart(out)), ([], []))
        # Drawn, then closed: the next board empties it, so opening it never shows the old context first.
        out = self.drive(self.HISTORY_OPEN + [{"close": "lb-history"}, {"tab": ["lb-metric-tabs", "Q3"]},
                                              {"answer": 2, "with": "q3"}], self.responses)
        self.assertEqual(out["historyRows"], [])
        self.assertEqual([c["destroyed"] for c in self.history_chart(out)], [True])

    def test_a_failed_board_load_empties_the_panels(self):
        """A9: a board load that draws no new board (an error answer, a lost connection, another script's answer)
        leaves no table, chart or compare selection of the previous context in the panels."""
        responses = {**self.responses, "400": '{"errors": ["metric: total or q1..q22"]}', "down": ""}
        for key, meta in (("400", {"status": 400}), ("down", {"reject": True}),
                          ("q3", {"headers": {"x-leaderboard-script": "0" * 12}})):
            with self.subTest(answer=key):
                out = self.drive(self.HISTORY_OPEN + [{"open": "lb-prsets"}, {"answer": 2, "with": "sets"},
                                                      {"check": PQ}, {"check": self.FIVE},
                                                      {"tab": ["lb-metric-tabs", "Q3"]}, {"answer": 3, "with": key}],
                                 responses, meta={key: meta})
                self.assertEqual((out["historyRows"], out["prsetRows"]), ([], []))
                self.assertEqual(out["historyNote"], "board 를 불러오지 못했습니다.")
                self.assertTrue(all(c["destroyed"] for c in self.history_chart(out)), out["chartInfo"])
                self.assertEqual(out["setCompare"], {"disabled": True, "text": "선택한 PR set 비교 (0/2–4)"})

    def test_a_panel_error_leaves_nothing_of_the_last_answer(self):
        """A9: an error answer for the history's other metric, or for the PR sets in another order, shows the error
        alone: the previous metric's table and chart, or the previous table and its selection, are gone."""
        responses = {**self.responses, "bad": '{"errors": ["leaderboard storage unavailable"]}'}
        meta = {"bad": {"status": 503}}
        out = self.drive(self.HISTORY_OPEN + [{"click": "Q3"}, {"answer": 2, "with": "bad"}], responses, meta)
        self.assertEqual(out["historyRows"], [])
        self.assertEqual(out["historyNote"], "leaderboard storage unavailable")
        self.assertEqual([c["destroyed"] for c in self.history_chart(out)], [True])
        out = self.drive(self.SETS_PICKED + [{"select": ["lb-prsets-sort", "number"]}, {"answer": 2, "with": "bad"}],
                         responses, meta)
        self.assertEqual(out["prsetRows"], [])
        self.assertEqual(out["prsetsNote"], "leaderboard storage unavailable")
        self.assertEqual(out["setCompare"], {"disabled": True, "text": "선택한 PR set 비교 (0/2–4)"})

    def test_an_entering_hero_is_drawn_where_the_window_starts(self):
        """Round 6: with `from`, the x axis starts at the window, not at the entering hero's old finish; without a
        window it starts at the first point as before."""
        old = self.submit(basis=prs(P), elapsed=flat_elapsed(100), started_at=at(-1440 * 300))
        newer = self.submit(elapsed=flat_elapsed(900), started_at=at(1440 * 30))
        day = self.c.get(f"/api/leaderboard/runs/{newer}").get_json()["finished_at"][:10]
        hist = self.c.get(f"/api/leaderboard/history?from={day}").get_data(as_text=True)
        self.assertEqual([h["run_id"] for h in json.loads(hist)["heroes"]], [old])
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "win"}],
                         {**self.responses, "win": hist})
        [chart] = [c for c in out["chartInfo"] if "heroLabels" in c["plugins"]]
        start = int(datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()) * 1000
        self.assertGreaterEqual(chart["xMin"], start)
        steps = [d for d in out["charts_all"][-1] if d["label"] == "best so far (hero)"][0]["data"]
        self.assertEqual(steps[0]["x"], start)
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "hist"}],
                         self.responses)
        [chart] = [c for c in out["chartInfo"] if "heroLabels" in c["plugins"]]
        self.assertEqual(chart["xMin"], min(p["t"] for p in json.loads(self.responses["hist"])["points"]))

    def test_the_chart_start_survives_a_bad_from_and_a_cut(self):
        """Round 7: an impossible `from` (2026-10-00) never reaches the chart as NaN, and after a budget cut the chart
        starts at the first shown point even with `from` set."""
        hist = json.loads(self.responses["hist"])
        first = min(p["t"] for p in hist["points"])
        for case, extra in (("bad from", {"filter": {**hist["filter"], "from": "2026-10-00"}}),
                            ("cut", {"filter": {**hist["filter"], "from": "2000-01-01"}, "truncated": True})):
            with self.subTest(case):
                # The entering hero finished before the shown series, as the server sends it after a cut.
                entering = {**hist["heroes"][0], "entering": True, "t": first - 10 * 86400000}
                answer = {**hist, **extra, "heroes": [entering] + hist["heroes"][1:]}
                out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "x"}],
                                 {**self.responses, "x": json.dumps(answer)})
                [chart] = [c for c in out["chartInfo"] if "heroLabels" in c["plugins"]]
                self.assertEqual(chart["xMin"], first)

    def test_a_board_reload_drops_a_set_selection_at_once(self):
        out = self.drive(self.SETS_PICKED + [{"tab": ["lb-metric-tabs", "Q3"]}], self.responses)
        self.assertEqual(out["setCompare"], {"disabled": True, "text": "선택한 PR set 비교 (0/2–4)"})
        # Round 3: nothing of the old list can be ticked while the new board is pending, selected or not.
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-prsets"}, {"answer": 1, "with": "sets"},
                          {"tab": ["lb-metric-tabs", "Q3"]}], self.responses)
        self.assertEqual(out["prsetRows"], [])

    def test_a_panel_reopened_during_a_failed_load_draws_nothing(self):
        responses = {**self.responses, "503": '{"errors": ["leaderboard storage unavailable"]}'}
        out = self.drive([{"answer": 0, "with": "board"}, {"open": "lb-history"}, {"answer": 1, "with": "hist"},
                          {"tab": ["lb-metric-tabs", "Q3"]}, {"close": "lb-history"}, {"open": "lb-history"},
                          {"answer": 2, "with": "503"}, {"answer": 3, "with": "hist"}],
                         responses, meta={"503": {"status": 503}})
        self.assertEqual(out["historyRows"], [])

    def test_a_compare_selection_keeps_only_sets_with_a_best_run(self):
        """A9: when the list comes back (here re-sorted), a selected set without a best run is unchecked (never
        checked and disabled) and the Compare button counts what is left."""
        sets = json.loads(self.responses["sets"])
        for x in sets["prsets"]:
            if x["prset"] == self.FIVE:
                x["best"] = None
        out = self.drive(self.SETS_PICKED + [{"select": ["lb-prsets-sort", "number"]}, {"answer": 2, "with": "none"}],
                         {**self.responses, "none": json.dumps(sets)})
        boxes = {r["text"][1]: r["inputs"] for r in out["prsetRows"]}
        self.assertEqual(boxes["b#1 + b#2 + b#3 +2"], [{"checked": False, "disabled": True}])
        self.assertEqual([v for k, v in boxes.items() if "gluten" in k], [[{"checked": True, "disabled": False}]])
        self.assertEqual(out["setCompare"], {"disabled": True, "text": "선택한 PR set 비교 (1/2–4)"})

    def test_history_chart_axis_and_steps(self):
        """A9: x is a linear axis of epoch ms (each point at its run's t); the best line and the speedup line hold each
        hero's value until the next hero (Chart.js stepped 'before': 'after' would draw the next value over the
        interval)."""
        self.pin_target(self.a, goal=3.0)
        hist = self.c.get("/api/leaderboard/history").get_data(as_text=True)
        out = self.drive(self.HISTORY_OPEN, {**self.responses, "hist": hist})
        info = self.history_chart(out)[-1]
        self.assertEqual(info["x"], "linear")
        self.assertEqual(info["stepped"], [None, "before", "before", None])
        data = json.loads(hist)
        series = out["charts_all"][-1]
        self.assertEqual([p["x"] for p in series[0]["data"]], [p["t"] for p in data["points"]])
        self.assertEqual([p["x"] for p in series[1]["data"]][:-1], [h["t"] for h in data["heroes"]])
        for d in series:
            for p in d["data"]:
                self.assertIsInstance(p["x"], int)
                self.assertGreater(p["x"], 10 ** 12)                                 # ms, not seconds

    def test_history_labels_one_per_hero_inside_the_chart(self):
        """A9: the canvas names each hero's basis (BASIS_CHIPS PRs, as the table) when there is room; on a narrow chart
        a label that would leave the chart area or overlap one already drawn is left out."""
        hist = json.loads(self.responses["hist"])
        board = json.loads(self.responses["board"])
        hist["heroes"][0]["basis"] = next(r["basis"] for r in board["rows"]
                                          if r["basis"] and r["basis"].get("prset") == self.FIVE)
        out = self.drive(self.HISTORY_OPEN, {**self.responses, "hist": json.dumps(hist)},
                         label_draws=[{"width": 2000, "height": 300}, {"width": 200, "height": 40}])
        self.assertEqual(json.loads(self.page_data()["lb-page-data"])["basis_chips"], 3)
        wide, narrow = out["labelDraws"]
        self.assertEqual([d["text"] for d in wide], ["b#1 + b#2 + b#3 +2", "main · latest #1564"])
        self.assertEqual(len(narrow), 1)
        for draws, width, height in ((wide, 2000, 300), (narrow, 200, 40)):
            boxes = [(d["x"] - d["w"] / 2, d["x"] + d["w"] / 2, d["y"] - 11, d["y"] + 2) for d in draws]
            for i, (l, r, t, b) in enumerate(boxes):
                self.assertTrue(0 <= l and r <= width and 0 <= t and b <= height, (draws, width, height))
                for l2, r2, t2, b2 in boxes[:i]:
                    self.assertFalse(l < r2 and l2 < r and t < b2 and t2 < b, draws)

    def test_report_falls_back_to_the_pr_set_report(self):
        """A9: the Report cell (board, hero, history) links the run's own Report, else its PR set's (said so in the
        tooltip), else "—"; only an http(s) URL becomes a link."""
        board = json.loads(self.responses["board"])
        for r in board["rows"]:
            kind = (r["basis"] or {}).get("prset")
            r["prset_report_url"] = {self.FIVE: "https://example.com/five", PQ: "https://example.com/pq"}.get(kind)
            if not kind:
                r["prset_report_url"] = "javascript:alert(1)"
        board["hero"]["prset_report_url"] = "https://example.com/hero-set"
        hist = json.loads(self.responses["hist"])
        hist["heroes"][0]["prset_report_url"] = "https://example.com/pq"
        out = self.drive(self.HISTORY_OPEN, {**self.responses, "board": json.dumps(board), "hist": json.dumps(hist)})
        rows = {r["text"][2]: r for r in out["boardRows"]}
        five = next(r for k, r in rows.items() if "+2" in k)
        pq = next(r for k, r in rows.items() if "gluten" in k)
        main = rows["main · latest #1564"]
        self.assertEqual((five["text"][3], pq["text"][3], main["text"][3]), ("↗ Report", "↗ Report", "—"))
        self.assertIn("https://claude.ai/artifact/x", five["hrefs"])                  # its own wins
        self.assertNotIn("https://example.com/five", five["hrefs"])
        self.assertIn("https://example.com/pq", pq["hrefs"])
        self.assertIn("PR set Report: https://example.com/pq", pq["titles"])
        self.assertNotIn("javascript:alert(1)", main["hrefs"])
        self.assertIn("https://example.com/hero-set", out["heroMeta"]["hrefs"])
        self.assertIn("PR set Report: https://example.com/hero-set", out["heroMeta"]["titles"])
        self.assertEqual(out["historyRows"][1]["text"][5], "↗ Report")                 # newest hero first
        self.assertIn("PR set Report: https://example.com/pq", out["historyRows"][1]["titles"])
        self.assertEqual(out["historyRows"][0]["text"][5], "—")

    def test_long_labels_wrap_in_their_cells(self):
        """A9: the history's Basis cell and the PR-set cell wrap (a long repository name stays in its column)."""
        out = self.drive(self.HISTORY_OPEN + [{"open": "lb-prsets"}, {"answer": 2, "with": "sets"}], self.responses)
        self.assertEqual({r["cls"][4] for r in out["historyRows"]}, {"lb-wrap-cell"})
        self.assertEqual({r["cls"][1] for r in out["prsetRows"]}, {"lb-wrap-cell"})


def css_rules(name):
    """static/<name> as {selector: {property: value}}: comments dropped, each selector of a list on its own, a later
    rule adding to an earlier one (the file has no at-rules)."""
    with open(os.path.join(ROOT, "static", name), encoding="utf-8") as f:
        text = re.sub(r"/\*.*?\*/", "", f.read(), flags=re.S)
    rules = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        decls = {k.strip(): v.strip() for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d)}
        for sel in selectors.split(","):
            rules.setdefault(" ".join(sel.split()), {}).update(decls)
    return rules


class TestA9Styles(unittest.TestCase):
    """A9 layout rules of leaderboard.css that a 390 px page needs."""

    def setUp(self):
        self.rules = css_rules("leaderboard.css")

    def test_long_chips_wrap(self):
        """A chip label (a valid repository name runs to 100 characters) wraps in the hero, a table cell and the PR
        filter instead of running past the page."""
        chip = self.rules[".lb-chip"]
        # break-word, not anywhere: a table cell keeps whole short labels on one line (the demo's PR-set members).
        self.assertEqual((chip["white-space"], chip["overflow-wrap"], chip["max-width"]), ("normal", "break-word", "100%"))
        self.assertEqual(self.rules[".lb-chip-pair"]["display"], "inline-flex")      # a chip and its ↗ wrap as one
        # Round 4: in an inline-flex chip the label is a flex item that break-word never breaks (a 100-character name
        # ran past a 390 px page); the chip must be a block box for break-word to apply.
        self.assertEqual(chip["display"], "inline-block")
        # Round 5: an edit history line (a grid item) may hold one long actor or chip: it may shrink and break.
        events = self.rules[".lb-target-events > li"]
        self.assertEqual((events["min-width"], events["overflow-wrap"]), ("0", "anywhere"))
        self.assertEqual(self.rules[".lb-chips"]["max-width"], "100%")
        pr = self.rules[".lb-pr-filter"]
        self.assertEqual((pr["flex-wrap"], pr["max-width"]), ("wrap", "100%"))
        self.assertEqual(self.rules['body[data-page="prset"] h1']["overflow-wrap"], "anywhere")   # "PR set <label>"

    def test_board_report_column_fits_its_label(self):
        """"↗ Report" needs about 81 px with the cell's padding: its fixed column is wider, and Submitter keeps the
        100 px the table's min-width leaves it."""
        widths = {}
        for sel, decls in self.rules.items():
            m = re.fullmatch(r"\.lb-board th:nth-child\((\d+)\)", sel)
            if m and "width" in decls:
                widths[int(m.group(1))] = int(decls["width"].removesuffix("px"))
        self.assertGreaterEqual(widths[4], 96)
        self.assertNotIn(13, widths)
        self.assertEqual(int(self.rules[".lb-board"]["min-width"].removesuffix("px")) - sum(widths.values()), 100)

    def test_wrap_cells_keep_a_width_in_auto_tables(self):
        """A wrapping cell of an auto-layout table keeps a usable width (the PR-set page's Config was squeezed to
        ~78 px at 390 px); the board's fixed columns are left alone."""
        self.assertGreaterEqual(int(self.rules[".lb-table td.lb-wrap-cell"]["min-width"].removesuffix("px")), 160)
        self.assertNotIn("min-width", self.rules[".lb-board td.lb-wrap-cell"])


class TestA9EditScript(A9Case):
    """A9 edit buttons on the run and PR-set pages: actor + value prompts, the page's revision, restore by event."""

    def page(self, url):
        html = self.c.get(url).get_data(as_text=True)
        restores = [{"id": f"restore-{e}", "dataset": {"event": e, "field": f}}
                    for e, f in re.findall(r'data-event="(\d+)" data-field="(\w+)"', html)]
        return {"ids": re.findall(r'id="([^"]+)"', html) + [r["id"] for r in restores], "data": json_blocks(html),
                "selectors": {".lb-restore": restores}}

    def test_basis_prompt_reads_every_form(self):
        run = self.submit(basis=prs(P, Q))
        page = self.page(f"{LB}/run/{run}")
        out = run_page_js("run", click="lb-basis-edit", fetch="{}", prompts=["me@x"], **page)
        self.assertEqual(out["fetches"], [])                                         # cancelled at the value prompt
        for text, basis in (
                ("1561 apache/gluten#88", prs(P, Q)),
                ("#1561, https://github.com/apache/gluten/pull/88", prs(P, Q)),
                ("main 1564", MAIN),
                ("main facebookincubator/velox#12", {"kind": "main", "repo": "facebookincubator/velox", "latest_pr": 12}),
                ("", None)):
            with self.subTest(text):
                out = run_page_js("run", click="lb-basis-edit", fetch="{}", prompts=["me@x", text], **page)
                self.assertEqual(out["fetches"], [{"url": f"/api/leaderboard/runs/{run}/basis", "method": "POST",
                                                   "body": {"actor": "me@x", "expected_revision": 1, "basis": basis}}])
                self.assertEqual(out["reloads"], 1)
        for text in ("main", "main 1 2", "x/y/z#1", "#0", "abc", "apache/gluten88", "main a/b12"):
            with self.subTest(bad=text):
                out = run_page_js("run", click="lb-basis-edit", fetch="{}", prompts=["me@x", text], **page)
                self.assertEqual((out["fetches"], len(out["alerts"])), ([], 1))

    def test_prompt_starts_from_the_full_stored_value(self):
        run = self.submit(basis=prs(*({"repo": "a/b", "number": n} for n in range(1, 17))))
        page = self.page(f"{LB}/run/{run}")
        data = json.loads(page["data"]["lb-edit-data"])
        self.assertEqual(len(data["basis"]["prs"]), 16)
        out = run_page_js("run", click="lb-basis-edit", fetch="{}",
                          prompts=["me@x", " ".join(f"a/b#{n}" for n in range(1, 17))], **page)
        self.assertEqual(len(out["fetches"][0]["body"]["basis"]["prs"]), 16)

    def test_link_restore_and_conflict(self):
        run = self.submit()
        for revision, link in enumerate(("https://x.example/1", "https://x.example/2")):
            self.edit(run, "report_url", {"actor": "a", "expected_revision": revision, "report_url": link})
        page = self.page(f"{LB}/run/{run}")
        self.assertEqual(len(page["selectors"][".lb-restore"]), 1)                   # only the earlier value
        restore = page["selectors"][".lb-restore"][0]
        out = run_page_js("run", click=restore["id"], fetch="{}", prompts=["me@x"], **page)
        self.assertEqual(out["fetches"][0]["body"], {"actor": "me@x", "expected_revision": 2,
                                                     "restore": int(restore["dataset"]["event"])})
        out = run_page_js("run", click="lb-report-edit", fetch='{"errors": ["changed meanwhile: reload and try again"]}',
                          prompts=["me@x", ""], status=409, **page)
        self.assertEqual(out["fetches"][0]["body"], {"actor": "me@x", "expected_revision": 2, "report_url": None})
        self.assertIn("HTTP 409", out["alerts"][0])
        self.assertEqual((out["reloads"], out["disabled"]["lb-report-edit"]), (0, False))

    def test_prset_page_link_edit(self):
        self.submit(basis=prs(P, Q))
        page = self.page(f"{LB}/prset?prset={quote(PQ)}")
        out = run_page_js("prset", click="lb-report-edit", fetch="{}", prompts=["me@x", " https://x.example/e "], **page)
        self.assertEqual(out["fetches"], [{"url": f"/api/leaderboard/prset?prset={quote(PQ)}", "method": "POST",
                                           "body": {"actor": "me@x", "expected_revision": 0,
                                                    "report_url": "https://x.example/e"}}])


class TestA9ReviewFixes(A9Case):
    """Round-1 review findings on the server side, each pinned."""

    def test_only_ascii_digits_name_an_event_or_a_pr(self):
        """F1: '²' passes str.isdigit() but not int(); '٣' passes both as 3. Neither is an event id or a PR number."""
        run = self.submit(basis=prs(P))
        for value in ("%C2%B2", "%D9%A3", "1%C2%B2"):
            with self.subTest(before=value):
                self.assertEqual(self.c.get(f"/api/leaderboard/runs/{run}/history?before={value}").status_code, 400)
                self.assertEqual(self.c.get(f"/api/leaderboard/prset?prset={quote(PQ)}&before={value}").status_code, 400)
                self.assertEqual(self.c.get(f"{LB}/run/{run}?before={value}").status_code, 400)
            with self.subTest(number=value):
                r = self.c.post(f"/api/leaderboard/prs/a/b/{value}/check", json={"actor": "x", "run_ids": []})
                self.assertEqual(r.status_code, 404)

    def test_run_page_shows_and_edits_one_snapshot(self):
        """F2: an edit that lands while the page is built never pairs the new revision with the old value."""
        run = self.submit(basis=prs(P))
        real_get = lb_store.Store.get

        def get_then_edit(store, run_id):
            row = real_get(store, run_id)
            store.edit("run", run_id, "basis", prs(P, R), "other@x", 1)               # lands between the reads
            lb_store.Store.get = real_get
            return row

        with mock.patch.object(lb_store.Store, "get", get_then_edit):
            page = self.c.get(f"{LB}/run/{run}").get_data(as_text=True)
        data = json.loads(json_blocks(page)["lb-edit-data"])
        self.assertEqual(data["revision"], 2)
        self.assertEqual(len(data["basis"]["prs"]), 2)                                # the value at revision 2
        basis_section = page[page.index('id="basis"'):page.index("Per-query medians")]
        self.assertIn("velox#12", basis_section)

    def test_meta_never_holds_a_basis(self):
        """F5: the run's basis is kept apart; a retry carrying another basis shows it nowhere."""
        m = build_manifest(run_id=rid())
        self.post(build_envelope(m, basis=prs(P)))
        self.post(build_envelope(m, basis=prs(R)), status=200)
        detail = self.c.get(f"/api/leaderboard/runs/{m['run_id']}").get_json()
        self.assertNotIn("basis", detail["meta"])
        self.assertEqual(detail["basis"]["prset"], "xcena-dev/xflare-analytics#1561")
        warned = self.post(build_envelope(build_manifest(run_id=rid()), basis={**prs(P), "x": 1}), "?dry_run=1",
                           status=200)["warnings"]
        self.assertIn("$.basis.'x': unknown property (ignored, not stored)", warned)

    def test_basis_option_labels_are_bounded(self):
        """F6: a PR set option names at most BASIS_CHIPS PRs plus +N and is cut like any label."""
        long = [{"repo": f"o{n}/" + "r" * 98, "number": n} for n in range(1, 17)]
        self.submit(basis=prs(*long))
        label = next(o["label"] for o in self.board()["facets"]["basis"] if o["label"].startswith("PR set"))
        self.assertTrue(label.endswith(" +13"), label)
        self.assertLessEqual(len(label), 512)

    def test_filter_literals_echo_the_canonical_key(self):
        """F7: the board echoes a pr / basis=prset: literal as its canonical key, so its option and chip select."""
        self.submit(basis=prs(P, Q))
        board = self.board(f"?pr={quote('XCENA-dev/xflare-analytics#1561')}&basis={quote('prset:XCENA-dev/xflare-analytics#1561+apache/gluten#88')}")
        self.assertEqual(board["filter"], {**board["filter"], "pr": "xcena-dev/xflare-analytics#1561", "basis": "prset:" + PQ})
        self.assertEqual(board["filter_tokens"]["basis"], facet_token("prset:" + PQ))
        self.assertEqual(len(board["rows"]), 1)

    def test_a_check_counts_each_run_once_and_replaces_the_last(self):
        """F12 (and F8 over HTTP): duplicates count once; a later check of the PR replaces the earlier one."""
        run = self.submit(basis=prs(P))
        post = lambda ids: self.c.post("/api/leaderboard/prs/xcena-dev/xflare-analytics/1561/check",
                                       json={"actor": "s", "run_ids": ids}).get_json()
        self.assertEqual(post([run, run])["listed_count"], 1)
        post([])
        page = self.c.get(f"{LB}/run/{run}").get_data(as_text=True)
        self.assertIn("not listed", page)
        self.assertEqual(self.c.post("/api/leaderboard/prs/a/b/1/check",
                                     json={"actor": "s", "run_ids": [], "source": ""}).status_code, 400)

    def test_a_sets_report_reaches_its_runs(self):
        """F14: a run without a Report of its own shows its PR set's on the board, the hero, the history and its page."""
        run = self.submit(basis=prs(P, Q), elapsed=flat_elapsed(100), started_at=at(0))
        other = self.submit(basis=prs(R), elapsed=flat_elapsed(900), started_at=at(1440))
        r = self.c.post(f"/api/leaderboard/prset?prset={quote(PQ)}",
                        json={"actor": "a", "expected_revision": 0, "report_url": "https://x.example/set"})
        self.assertEqual(r.status_code, 200)
        board = self.board()
        rows = {x["run_id"]: x for x in board["rows"]}
        self.assertEqual((rows[run]["report_url"], rows[run]["prset_report_url"]), (None, "https://x.example/set"))
        self.assertIsNone(rows[other]["prset_report_url"])
        self.assertEqual(board["hero"]["prset_report_url"], "https://x.example/set")
        self.assertEqual(self.c.get("/api/leaderboard/history").get_json()["heroes"][0]["prset_report_url"],
                         "https://x.example/set")
        page = self.c.get(f"{LB}/run/{run}").get_data(as_text=True)
        self.assertIn("PR set Report", page)
        self.assertIn('href="https://x.example/set"', page)
        both = self.c.get(f"/api/leaderboard/prset?prset={quote('facebookincubator/velox#12')}").get_json()
        self.assertEqual(both["runs"][0]["run_id"], other)
        for url in (f"{LB}/compare?runs={run},{other}",
                    f"{LB}/compare?prsets={quote(PQ)},{quote('facebookincubator/velox#12')}&executors=&executor_cores="):
            with self.subTest(url):
                compare = self.c.get(url).get_data(as_text=True)
                self.assertIn('href="https://x.example/set"', compare)            # the set's, for a run without one
        runs_table = self.c.get(f"{LB}/prset?prset={quote(PQ)}").get_data(as_text=True).split('id="prset-runs"')[1]
        self.assertIn('href="https://x.example/set"', runs_table)                # the runs table falls back too

    def test_run_page_shows_the_report_of_the_set_it_shows(self):
        """Round 2: a basis change while the page is built shows the new set's Report, never the old set's."""
        run = self.submit(basis=prs(P))
        for key, url in (("xcena-dev/xflare-analytics#1561", "https://x.example/OLD"),
                         ("facebookincubator/velox#12", "https://x.example/NEW")):
            self.c.post(f"/api/leaderboard/prset?prset={quote(key)}",
                        json={"actor": "a", "expected_revision": 0, "report_url": url})
        real_get = lb_store.Store.get

        def get_then_edit(store, run_id):
            row = real_get(store, run_id)
            store.edit("run", run_id, "basis", prs(R), "other@x", 1)
            lb_store.Store.get = real_get
            return row

        with mock.patch.object(lb_store.Store, "get", get_then_edit):
            page = self.c.get(f"{LB}/run/{run}").get_data(as_text=True)
        self.assertIn('href="https://x.example/NEW"', page)
        self.assertNotIn("https://x.example/OLD", page)

    def test_the_prset_list_stops_at_its_budget(self):
        """Round 2: the PR-set list keeps its first FACET_VALUES sets in its order and says it was cut."""
        from leaderboard import web as lb_web
        for n in (1, 2, 3):
            self.submit(basis=prs({"repo": "a/b", "number": n}), elapsed=flat_elapsed(100 * n))
        with mock.patch.object(lb_web, "FACET_VALUES", 2):
            out = self.c.get("/api/leaderboard/prsets").get_json()
        self.assertEqual(([x["prset"] for x in out["prsets"]], out["truncated"]), (["a/b#1", "a/b#2"], True))
        self.assertFalse(self.c.get("/api/leaderboard/prsets").get_json()["truncated"])

    def test_a_cut_history_starts_once_with_its_entering_hero(self):
        """F15: past the hero budget the series starts at the oldest kept hero, which enters it; no point is older."""
        from leaderboard import web as lb_web
        ids = [self.submit(elapsed=flat_elapsed(900 - 100 * n), started_at=at(1440 * n)) for n in range(5)]
        with mock.patch.object(lb_web, "HISTORY_HEROES", 3):
            out = self.c.get("/api/leaderboard/history").get_json()
        self.assertEqual([h["run_id"] for h in out["heroes"]], ids[2:])
        self.assertEqual([h["entering"] for h in out["heroes"]], [True, False, False])
        self.assertEqual([p["run_id"] for p in out["points"]], ids[3:])                # the entering hero's own is older
        self.assertTrue(out["truncated"])
        with mock.patch.object(lb_web, "HISTORY_POINTS", 2):
            out = self.c.get("/api/leaderboard/history").get_json()
        self.assertEqual([p["run_id"] for p in out["points"]], ids[3:])
        self.assertEqual([h["run_id"] for h in out["heroes"]], ids[2:])            # the hero in force at the start
        self.assertEqual(out["heroes"][0]["entering"], True)

    def test_report_url_ports_are_real_ports(self):
        """F16: a port outside 1..65535 is no URL."""
        run = self.submit()
        for revision, (url, status) in enumerate((("https://x.example:65535/r", 200), ("https://x.example:65536/r", 400),
                                                  ("https://x.example:0/r", 400), ("https://x.example:99999/r", 400))):
            with self.subTest(url):
                self.edit(run, "report_url", {"actor": "a", "expected_revision": min(revision, 1), "report_url": url},
                          status=status)


class TestA9Round2(A9Case):
    """Round-2 review findings, each pinned."""

    drive = TestBoardScript.drive

    def test_panels_and_compare_follow_the_board_filter_tokens(self):
        """doc2-2: the history, the PR-set list and compare by set take the board's <key>_t tokens as /board does."""
        four = self.submit(basis=prs(P), elapsed=flat_elapsed(500), started_at=at(0))
        eight = self.submit(basis=prs(Q), elapsed=flat_elapsed(400), executors=8, started_at=at(1440))
        token = f"executors=&executor_cores=&executors_t={quote(facet_token('8'))}"
        hist = self.c.get(f"/api/leaderboard/history?{token}").get_json()
        self.assertEqual([p["run_id"] for p in hist["points"]], [eight])
        sets = self.c.get(f"/api/leaderboard/prsets?{token}").get_json()
        self.assertEqual([x["prset"] for x in sets["prsets"]], ["apache/gluten#88"])
        only = self.c.get(f"/api/leaderboard/prsets?executors=&executor_cores=&basis_t="
                          f"{quote(facet_token('prset:xcena-dev/xflare-analytics#1561'))}").get_json()
        self.assertEqual([x["best"]["run_id"] for x in only["prsets"]], [four])
        page = self.c.get(f"{LB}/compare?prsets={quote('apache/gluten#88')},{quote('xcena-dev/xflare-analytics#1561')}"
                          f"&{token}")
        self.assertEqual(page.status_code, 404)                                     # #1561 has no 8×6 run

    def test_a_withdrawn_eligible_run_has_no_place(self):
        """doc2-9: places count the ranked runs only (total-eligible, not withdrawn)."""
        fast = self.submit(basis=prs(P), elapsed=flat_elapsed(100))
        slow = self.submit(basis=prs(P), elapsed=flat_elapsed(200))
        self.c.post(f"/api/leaderboard/runs/{fast}/withdraw", json={"actor": "a", "reason": "b"})
        runs = self.c.get(f"/api/leaderboard/prset?prset={quote('xcena-dev/xflare-analytics#1561')}").get_json()["runs"]
        self.assertEqual([(r["run_id"], r["place"]) for r in runs], [(slow, 1), (fast, None)])

    def test_check_and_key_limits(self):
        """doc2-10: the PR check's actor, number and hash limits, and the 16-member key limit."""
        ok = {"actor": "s", "run_ids": []}
        post = lambda path, body: self.c.post(f"/api/leaderboard/prs/{path}/check", json=body).status_code
        self.assertEqual(post("a/b/2147483647", ok), 200)
        self.assertEqual(post("a/b/2147483648", ok), 404)
        self.assertEqual(post("a/b/1", {**ok, "actor": "x" * 257}), 400)
        self.assertEqual(post("a/b/1", {**ok, "actor": "x" * 256}), 200)
        self.assertEqual(post("a/b/1", {**ok, "body_sha256": "a" * 63}), 400)
        self.assertEqual(post("a/b/1", {**ok, "body_sha256": "a" * 64}), 200)
        sixteen = "+".join(f"a/b#{n}" for n in range(1, 17))
        self.assertEqual(self.c.get(f"/api/leaderboard/prset?prset={quote(sixteen)}").status_code, 200)
        self.assertEqual(self.c.get(f"/api/leaderboard/prset?prset={quote(sixteen + '+a/b#17')}").status_code, 400)
        self.assertEqual(self.c.get(f"{LB}/prset?prset={quote(sixteen + '+a/b#17')}").status_code, 404)

    def test_restore_takes_only_this_runs_events(self):
        """doc2-10: an event of another run's same field is not a change of this run's."""
        a, b = self.submit(), self.submit()
        self.edit(b, "report_url", {"actor": "x", "expected_revision": 0, "report_url": "https://x.example/b"})
        event = self.history(b)["history"][0]["event_id"]
        out = self.edit(a, "report_url", {"actor": "x", "expected_revision": 0, "restore": event}, status=400)
        self.assertEqual(out["errors"], ["restore: not a change of this report_url"])

    def test_a_hero_on_the_from_day_is_shown_not_entering(self):
        """doc2-10: the window starts at `from` inclusive; the entering hero finished before it."""
        before = self.submit(elapsed=flat_elapsed(500), started_at=at(0))
        on = self.submit(elapsed=flat_elapsed(400), started_at=at(1440))
        day = self.c.get(f"/api/leaderboard/runs/{on}").get_json()["finished_at"][:10]
        heroes = self.c.get(f"/api/leaderboard/history?from={day}").get_json()["heroes"]
        self.assertEqual([(h["run_id"], h["entering"]) for h in heroes], [(before, True), (on, False)])

    def test_run_page_workflow_follows_the_shown_basis(self):
        """doc2-10: the PR workflow line uses the same snapshot as the shown basis."""
        run = self.submit(basis=prs(P))
        self.c.post("/api/leaderboard/prs/facebookincubator/velox/12/check", json={"actor": "s", "run_ids": [run]})
        real_get = lb_store.Store.get

        def get_then_edit(store, run_id):
            row = real_get(store, run_id)
            store.edit("run", run_id, "basis", prs(R), "other@x", 1)
            lb_store.Store.get = real_get
            return row

        with mock.patch.object(lb_store.Store, "get", get_then_edit):
            page = self.c.get(f"{LB}/run/{run}").get_data(as_text=True)
        self.assertIn(">complete<", page)                                           # R's body lists it; P's never did

    def test_submission_actor_is_trimmed(self):
        """doc2-6: a blank submitter email names the claim "submission"."""
        m = build_manifest(run_id=rid())
        env = build_envelope(m, basis=prs(P))
        env["submitter"]["email"] = "   "
        self.post(env)
        self.assertEqual(self.history(m["run_id"])["history"][0]["actor"], "submission")

    def test_meta_from_an_older_binary_shows_no_basis(self):
        """doc2-7: a basis an older binary kept in the stored metadata is not shown as metadata."""
        import sqlite3
        run = self.submit()
        with sqlite3.connect(os.path.join(self.data, "leaderboard.sqlite3")) as c:
            meta = json.loads(c.execute("SELECT meta_json FROM runs").fetchone()[0])
            c.execute("UPDATE runs SET meta_json = ?", (json.dumps({**meta, "basis": prs(P)}),))
        detail = self.c.get(f"/api/leaderboard/runs/{run}").get_json()
        self.assertNotIn("basis", detail["meta"])
        self.assertIsNone(detail["basis"])

    def test_a_bad_before_on_a_page_is_an_html_400(self):
        """doc2-8: pages answer HTML."""
        run = self.submit(basis=prs(P))
        for url in (f"{LB}/run/{run}?before=x", f"{LB}/prset?prset={quote('xcena-dev/xflare-analytics#1561')}&before=x"):
            with self.subTest(url):
                r = self.c.get(url)
                self.assertEqual((r.status_code, r.mimetype), (400, "text/html"))


class TestA9Round3(A9Case):
    """Round-3 review: checks the suite did not pin yet."""

    def test_history_of_an_unknown_run_is_404(self):
        self.assertEqual(self.c.get(f"/api/leaderboard/runs/{rid()}/history").status_code, 404)

    def test_more_means_older_changes_remain(self):
        run = self.submit()
        for revision in range(50):
            self.edit(run, "report_url", {"actor": "a", "expected_revision": revision,
                                          "report_url": f"https://x.example/{revision}"})
        self.assertFalse(self.history(run)["more"])                                  # exactly 50: nothing older
        self.edit(run, "report_url", {"actor": "a", "expected_revision": 50, "report_url": None})
        self.assertTrue(self.history(run)["more"])

    def test_the_to_day_is_inside_the_window(self):
        first = self.submit(elapsed=flat_elapsed(500), started_at=at(0))
        last = self.submit(elapsed=flat_elapsed(400), started_at=at(1440))
        day = self.c.get(f"/api/leaderboard/runs/{last}").get_json()["finished_at"][:10]
        out = self.c.get(f"/api/leaderboard/history?to={day}").get_json()
        self.assertEqual([h["run_id"] for h in out["heroes"]], [first, last])

    def test_before_must_be_an_event_id(self):
        run = self.submit()
        for value in ("0", "9999999999999999999", "-1"):
            with self.subTest(value):
                self.assertEqual(self.c.get(f"/api/leaderboard/runs/{run}/history?before={value}").status_code, 400)

    def test_source_and_report_url_lengths(self):
        run = self.submit()
        post = lambda source: self.c.post("/api/leaderboard/prs/a/b/1/check",
                                          json={"actor": "s", "run_ids": [], "source": source}).status_code
        self.assertEqual((post("x" * 64), post("x" * 65)), (200, 400))
        base = "https://x.example/"
        self.edit(run, "report_url", {"actor": "a", "expected_revision": 0, "report_url": base + "a" * (2048 - len(base))})
        self.edit(run, "report_url", {"actor": "a", "expected_revision": 1,
                                      "report_url": base + "a" * (2049 - len(base))}, status=400)

    def test_a_key_naming_a_pr_twice_is_no_key(self):
        self.assertEqual(self.c.get(f"/api/leaderboard/prset?prset={quote('a/b#1+a/b#1')}").status_code, 400)
        self.assertEqual(self.c.get(f"/api/leaderboard/prset?prset={quote('a/b#1+A/B#1')}").status_code, 400)

    def test_the_prset_pages_board_link_lifts_the_default_filter(self):
        """doc3-2: "only this set" shows the set's runs in any resources."""
        self.submit(basis=prs(Q), executors=8)
        page = self.c.get(f"{LB}/prset?prset={quote('apache/gluten#88')}").get_data(as_text=True)
        href = html.unescape(re.search(r'href="([^"]*)">board 에서 이 set 만 보기', page).group(1))
        query = href.split("?", 1)[1]
        self.assertEqual(len(self.board("?" + query)["rows"]), 1)
        # Round 4: on the set's own tab, wherever the first tab is.
        self.submit(basis=prs(R), sf="1000")
        self.submit()                                                                   # SF-100 has other runs
        page = self.c.get(f"{LB}/prset?prset={quote('facebookincubator/velox#12')}").get_data(as_text=True)
        query = html.unescape(re.search(r'href="([^"]*)">board 에서 이 set 만 보기', page).group(1)).split("?", 1)[1]
        board = self.board("?" + query)
        self.assertEqual((board["sf"], len(board["rows"])), ("1000", 1))
        # Round 5: a withdrawn run on an earlier tab does not take the link there.
        gone = self.submit(basis=prs({"repo": "a/b", "number": 7}))
        kept = self.submit(basis=prs({"repo": "a/b", "number": 7}), sf="1000")
        self.c.post(f"/api/leaderboard/runs/{gone}/withdraw", json={"actor": "a", "reason": "b"})
        page = self.c.get(f"{LB}/prset?prset={quote('a/b#7')}").get_data(as_text=True)
        query = html.unescape(re.search(r'href="([^"]*)">board 에서 이 set 만 보기', page).group(1)).split("?", 1)[1]
        self.assertEqual([r["run_id"] for r in self.board("?" + query)["rows"]], [kept])
        # Round 6: a ranked run wins over an earlier tab's run that is listed but not ranked (shared application).
        self.submit(basis=prs({"repo": "a/b", "number": 8}), application_mode="shared")
        ranked = self.submit(basis=prs({"repo": "a/b", "number": 8}), sf="1000")
        page = self.c.get(f"{LB}/prset?prset={quote('a/b#8')}").get_data(as_text=True)
        query = html.unescape(re.search(r'href="([^"]*)">board 에서 이 set 만 보기', page).group(1)).split("?", 1)[1]
        self.assertEqual([r["run_id"] for r in self.board("?" + query)["rows"]], [ranked])

    def test_a_default_filter_on_basis_keeps_the_prset_page(self):
        """Round 4: a configured default filter naming `basis` does not break the PR-set page's board link."""
        self.app.extensions["leaderboard_cfg"]["default_filter"] = {"executors": 4, "executor_cores": 6, "basis": "main"}
        self.submit(basis=prs(P))
        r = self.c.get(f"{LB}/prset?prset={quote('xcena-dev/xflare-analytics#1561')}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("basis=prset:xcena-dev/xflare-analytics%231561", r.get_data(as_text=True))


class TestA9Round7(A9Case):
    def test_a_date_no_calendar_has_narrows_nothing(self):
        """Round 7: 2026-02-30 or 2026-10-00 is not a date: like a half-typed one it narrows nothing and is not echoed."""
        self.submit()
        for bad in ("2026-02-30", "2026-10-00", "2026-13-01"):
            with self.subTest(bad):
                for url in (f"/api/leaderboard/board?from={bad}", f"/api/leaderboard/history?from={bad}"):
                    out = self.c.get(url).get_json()
                    self.assertNotIn("from", out["filter"], url)
        self.assertEqual(self.board("?from=2026-02-28")["filter"]["from"], "2026-02-28")


class TestA9Round4(A9Case):
    """Round-4 review: compare by set from one read, and PR-set tab boundaries, orphaned Reports, 2-4 sets."""

    def test_compare_by_set_shows_the_rows_it_chose(self):
        a = self.submit(basis=prs({"repo": "a/b", "number": 1}), elapsed=flat_elapsed(100))
        self.submit(basis=prs({"repo": "a/b", "number": 2}), elapsed=flat_elapsed(200))
        real = lb_store.Store.list_runs

        def list_then_edit(store, *args, **kw):
            rows = real(store, *args, **kw)
            store.edit("run", a, "basis", MAIN, "other@x", 1)                         # lands after the choice
            lb_store.Store.list_runs = real
            return rows

        with mock.patch.object(lb_store.Store, "list_runs", list_then_edit):
            page = self.c.get(f"{LB}/compare?prsets=a%2Fb%231,a%2Fb%232").get_data(as_text=True)
        self.assertNotIn("main · latest #1564", page)                               # the basis it was chosen by
        self.assertIn("b#1", page)

    def test_prset_tabs_are_separate(self):
        on100 = self.submit(basis=prs(P), elapsed=flat_elapsed(300))
        on1000 = self.submit(basis=prs(P), elapsed=flat_elapsed(900), sf="1000")
        other = self.submit(basis=prs(Q), elapsed=flat_elapsed(400), sf="1000")
        key = "xcena-dev/xflare-analytics#1561"
        lst = lambda q: {x["prset"]: x["best"]["run_id"] for x in self.c.get(f"/api/leaderboard/prsets?{q}").get_json()["prsets"]}
        self.assertEqual(lst("sf=100"), {key: on100})
        self.assertEqual(lst("sf=1000"), {key: on1000, "apache/gluten#88": other})
        runs = self.c.get(f"/api/leaderboard/prset?prset={quote(key)}").get_json()["runs"]
        self.assertEqual([(r["run_id"], r["tab"]["sf"], r["place"]) for r in runs], [(on100, "100", 1), (on1000, "1000", 1)])
        page = self.c.get(f"{LB}/compare?prsets={quote(key)},{quote('apache/gluten#88')}&sf=1000").get_data(as_text=True)
        self.assertIn(on1000[:8], page)
        self.assertNotIn(on100[:8], page)

    def test_an_orphaned_sets_report_stays_restorable(self):
        run = self.submit(basis=prs(P))
        key = quote("xcena-dev/xflare-analytics#1561")
        post = lambda body: self.c.post(f"/api/leaderboard/prset?prset={key}", json={"actor": "a", **body})
        self.assertEqual(post({"expected_revision": 0, "report_url": "https://x.example/set"}).status_code, 200)
        self.edit(run, "basis", {"actor": "a", "expected_revision": 1, "basis": None})  # no run claims it now
        self.assertEqual(post({"expected_revision": 1, "report_url": None}).status_code, 200)
        api = self.c.get(f"/api/leaderboard/prset?prset={key}").get_json()
        self.assertEqual((api["runs"], api["report_url"], len(api["history"])), ([], None, 2))
        r = post({"expected_revision": 2, "restore": api["history"][1]["event_id"]})
        self.assertEqual((r.get_json()["report_url"], r.get_json()["revision"]), ("https://x.example/set", 3))
        self.assertIn('href="https://x.example/set"', self.c.get(f"{LB}/prset?prset={key}").get_data(as_text=True))

    def test_compare_takes_two_to_four_sets(self):
        keys = []
        for n in range(1, 6):
            self.submit(basis=prs({"repo": "a/b", "number": n}), elapsed=flat_elapsed(100 * n))
            keys.append(quote(f"a/b#{n}"))
        status = lambda k: self.c.get(f"{LB}/compare?prsets={','.join(keys[:k])}").status_code
        self.assertEqual([status(k) for k in range(1, 6)], [400, 200, 200, 200, 400])


if __name__ == "__main__":
    unittest.main(verbosity=2)
