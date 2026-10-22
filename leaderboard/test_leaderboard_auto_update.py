#!/usr/bin/env python3
"""Auto-updater tests: systemd/xflare-leaderboard-auto-update.sh run against a local bare origin, with sudo, curl and
the interpreter stubbed. Run: python3 leaderboard/test_leaderboard_auto_update.py

The updater's self-check runs this suite on every new commit, so a commit that breaks the updater fails its own
deployment instead of being merged by it. These tests need bash and git; without them they fail, naming the missing
runtime.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "systemd" / "xflare-leaderboard-auto-update.sh"
UNITS = ROOT / "systemd"
DEPLOYED = "refs/auto-update/deployed"
PORT = 18894

# Stubs. sudo and curl log their arguments; curl answers FAKE_HTTP_CODE. The interpreter hands `-c` (the port read)
# to the real one and plays the self-check otherwise: it logs the skip, config and data variables it got, the suite it
# was given and the last line of the export's notes.txt (which commit it is), then runs FAKE_SELFCHECK_HOOK.
STUBS = {
    "sudo": '#!/bin/bash\necho "$*" >>"$STATE/sudo"\nexit "${FAKE_SUDO_RC:-0}"\n',
    "curl": '#!/bin/bash\necho "$*" >>"$STATE/curl"\nprintf %s "${FAKE_HTTP_CODE:-200}"\n'
            '[[ ${FAKE_HTTP_CODE:-200} != 000 ]] || exit 7\n',
    "python": '#!/bin/bash\n[[ $1 != -c ]] || exec "$REAL_PYTHON" "$@"\n'
              'echo "$XFLARE_LEADERBOARD_SKIP_NODE_TESTS ${XFLARE_LEADERBOARD_CONFIG-unset} $XFLARE_LEADERBOARD_DATA '
              '$1 $(tail -1 "${1%/leaderboard/*}/notes.txt")" >>"$STATE/selfcheck"\n'
              'eval "${FAKE_SELFCHECK_HOOK:-}"\nexit "${FAKE_SELFCHECK_RC:-0}"\n',
}


class AutoUpdate(unittest.TestCase):
    def setUp(self):
        for tool in ("bash", "git"):
            if shutil.which(tool) is None:
                raise AssertionError(f"{tool} not installed: the auto-updater tests need it (README: Run locally)")
        self.tmp = Path(tempfile.mkdtemp(prefix="lb-auto-update-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        bin_dir = self.tmp / "bin"
        self.state.mkdir()
        bin_dir.mkdir()
        for name, body in STUBS.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        # No global or system git config: a user's commit signing or hooks must not decide these tests.
        ident = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("XFLARE_LEADERBOARD_", "FAKE_", "GIT_"))}
        self.env.update(ident, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", STATE=str(self.state),
                        REAL_PYTHON=sys.executable, XFLARE_LEADERBOARD_CHECKOUT=str(self.tmp / "checkout"),
                        XFLARE_LEADERBOARD_PYTHON=str(bin_dir / "python"), XFLARE_LEADERBOARD_HEALTH_WAIT="2",
                        XFLARE_LEADERBOARD_CONFIG="/nonexistent", XFLARE_LEADERBOARD_DATA="/nonexistent")
        self.git("init", "-q", "--bare", "-b", "main", str(self.tmp / "origin.git"))
        self.git("clone", "-q", str(self.tmp / "origin.git"), str(self.tmp / "dev"))
        (self.tmp / "dev" / "config.yaml").write_text(f"server:\n  port: {PORT}\n")
        self.push("first")
        self.git("clone", "-q", str(self.tmp / "origin.git"), str(self.tmp / "checkout"))

    def git(self, *args, cwd=None) -> str:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def push(self, message: str) -> str:
        dev = self.tmp / "dev"
        with open(dev / "notes.txt", "a") as f:
            f.write(message + "\n")
        self.git("add", "-A", cwd=dev)
        self.git("commit", "-q", "-m", message, cwd=dev)
        self.git("push", "-q", "origin", "HEAD:main", cwd=dev)
        return self.git("rev-parse", "HEAD", cwd=dev)

    def tick(self, **fake) -> subprocess.CompletedProcess:
        # Run as systemd does: by path, so a lost exec bit or shebang fails here too.
        return subprocess.run([str(SCRIPT)], env={**self.env, **fake}, capture_output=True, text=True, timeout=60)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD", cwd=self.tmp / "checkout")

    def deployed(self) -> str | None:
        out = subprocess.run(["git", "rev-parse", "-q", "--verify", DEPLOYED], cwd=self.tmp / "checkout",
                             env=self.env, capture_output=True, text=True)
        return out.stdout.strip() or None

    def calls(self, name: str) -> list[str]:
        path = self.state / name
        return path.read_text().splitlines() if path.exists() else []

    def deploy_first(self):
        r = self.tick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deployed(), self.head())

    def test_script_parses(self):
        out = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_defaults_match_the_units(self):
        """The script's default checkout and interpreter are the ones the units run."""
        text = SCRIPT.read_text()
        checkout = re.search(r"^CHECKOUT=\$\{XFLARE_LEADERBOARD_CHECKOUT:-(.+)\}$", text, re.M).group(1)
        python = re.search(r"^PYTHON=\$\{XFLARE_LEADERBOARD_PYTHON:-(.+)\}$", text, re.M).group(1)
        updater = (UNITS / "xflare-leaderboard-auto-update.service").read_text()
        service = (UNITS / "xflare-leaderboard.service").read_text()
        self.assertIn(f"\nExecStart={checkout}/systemd/{SCRIPT.name}\n", updater)
        self.assertIn(f"\nExecStart={python} {checkout}/app.py\n", service)

    def test_first_tick_deploys_then_nothing_to_do(self):
        """No record yet (a fresh install): the current main is checked, restarted and recorded; then a no-op."""
        r = self.tick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"restarted on {self.head()[:8]}", r.stdout)
        self.assertEqual(self.deployed(), self.head())
        self.assertEqual(self.calls("sudo"), ["-n /usr/bin/systemctl restart xflare-leaderboard"])
        self.assertEqual([call.split()[-1] for call in self.calls("curl")],
                         [f"http://127.0.0.1:{PORT}/api/leaderboard/board"])
        # The self-check skips the node tests, ignores the config override and gets a scratch data dir.
        skip, config, data, suite, commit = self.calls("selfcheck")[0].split(" ")
        self.assertEqual((skip, config, commit), ("1", "unset", "first"))
        self.assertNotEqual(data, "/nonexistent")
        self.assertFalse(suite.startswith(str(self.tmp / "checkout")))
        self.assertFalse(os.path.exists(suite))                          # the export is gone after the tick
        r = self.tick()
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"already on {self.head()[:8]}"))
        self.assertEqual((len(self.calls("sudo")), len(self.calls("selfcheck"))), (1, 1))

    def test_new_commit_deploys(self):
        """The self-check runs on an export of the new commit, not on the checkout's old one."""
        self.deploy_first()
        new = self.push("second")
        r = self.tick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.head(), self.deployed()), (new, new))
        self.assertEqual(len(self.calls("sudo")), 2)
        suite, commit = self.calls("selfcheck")[1].split(" ")[3:]
        self.assertEqual(commit, "second")
        self.assertFalse(suite.startswith(str(self.tmp / "checkout")))

    def test_follows_the_remote_branch_not_a_tag(self):
        """A tag named origin/main (fetched with the repo's tags, say) must not stand in for the remote branch."""
        self.deploy_first()
        self.git("tag", "origin/main", "HEAD", cwd=self.tmp / "checkout")
        new = self.push("second")
        r = self.tick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deployed(), new)

    def test_failing_self_check_changes_nothing(self):
        self.deploy_first()
        old = self.head()
        self.push("broken")
        r = self.tick(FAKE_SELFCHECK_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("the self-check failed", r.stderr)
        self.assertEqual((self.head(), self.deployed(), len(self.calls("sudo"))), (old, old, 1))

    def test_failed_restart_is_retried(self):
        """The checkout moved but the restart failed: not recorded, so the next tick restarts again."""
        self.deploy_first()
        new = self.push("second")
        r = self.tick(FAKE_SUDO_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.head(), new)
        self.assertNotEqual(self.deployed(), new)
        r = self.tick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("restarted on", r.stdout)
        self.assertEqual(self.deployed(), new)

    def test_board_must_answer_2xx(self):
        self.deploy_first()
        new = self.push("second")
        for code in ("302", "000"):
            r = self.tick(FAKE_HTTP_CODE=code)
            self.assertEqual(r.returncode, 1)
            self.assertIn("did not answer 2xx", r.stderr)
            self.assertNotEqual(self.deployed(), new)
        self.assertEqual(self.tick().returncode, 0)
        self.assertEqual(self.deployed(), new)

    def test_pin_holds_and_unpin_restarts_on_main(self):
        """A pin is left alone and drops the record; back on main with main unchanged, the tick still restarts."""
        self.deploy_first()
        pinned = self.head()
        self.git("checkout", "-q", "--detach", "HEAD", cwd=self.tmp / "checkout")
        r = self.tick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pinned", r.stdout)
        self.assertIsNone(self.deployed())
        self.git("checkout", "-q", "main", cwd=self.tmp / "checkout")
        r = self.tick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("restarted on", r.stdout)
        self.assertEqual((self.head(), self.deployed(), len(self.calls("sudo"))), (pinned, pinned, 2))

    def test_documented_pin_restarts_on_unpin_without_a_tick(self):
        """README's pin drops the record itself, so an unpin no tick saw still restarts on main."""
        self.deploy_first()
        checkout = self.tmp / "checkout"
        self.git("checkout", "-q", "--detach", "HEAD", cwd=checkout)
        self.git("update-ref", "-d", DEPLOYED, cwd=checkout)
        self.git("checkout", "-q", "main", cwd=checkout)
        r = self.tick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("restarted on", r.stdout)
        self.assertEqual(len(self.calls("sudo")), 2)

    def test_pin_during_self_check_holds(self):
        self.deploy_first()
        old = self.head()
        self.push("second")
        r = self.tick(FAKE_SELFCHECK_HOOK='git -C "$XFLARE_LEADERBOARD_CHECKOUT" checkout -q --detach HEAD')
        self.assertEqual(r.returncode, 1)
        self.assertIn("changed during the self-check", r.stderr)
        self.assertEqual((self.head(), len(self.calls("sudo"))), (old, 1))
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.tmp / "checkout"), "HEAD")

    def test_local_changes_stop_the_update(self):
        self.deploy_first()
        old = self.head()
        self.push("second")
        checkout = self.tmp / "checkout"
        def hide_untracked():
            (checkout / "flask.py").write_text("")
            self.git("config", "status.showUntrackedFiles", "no", cwd=checkout)

        def show_untracked():
            (checkout / "flask.py").unlink()
            self.git("config", "--unset", "status.showUntrackedFiles", cwd=checkout)

        index = (checkout / ".git" / "index").read_bytes()
        for make, undo, why in ((lambda: (checkout / "config.yaml").write_text("edited\n"),
                                 lambda: self.git("checkout", "-q", "config.yaml", cwd=checkout),
                                 "local edits or untracked files"),
                                (lambda: (checkout / "flask.py").write_text(""),
                                 lambda: (checkout / "flask.py").unlink(), "local edits or untracked files"),
                                (hide_untracked, show_untracked, "local edits or untracked files"),
                                (lambda: (checkout / ".git" / "index").write_bytes(b"junk"),
                                 lambda: (checkout / ".git" / "index").write_bytes(index), "git status failed")):
            make()
            r = self.tick()
            self.assertEqual(r.returncode, 1)
            self.assertIn(why, r.stderr)
            self.assertEqual(self.head(), old)
            undo()
        self.git("commit", "-q", "--allow-empty", "-m", "local", cwd=checkout)
        r = self.tick()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not an ancestor", r.stderr)
        self.assertEqual(len(self.calls("sudo")), 1)


if __name__ == "__main__":
    unittest.main()
