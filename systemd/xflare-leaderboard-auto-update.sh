#!/bin/bash
#
# Follow main on the deployed checkout: test origin/main, fast-forward to it, restart the service on it, and record it
# as deployed once the board answers. xflare-leaderboard-auto-update.timer runs this (README: Automatic updates).
#
# Rules that keep an unattended update from taking the board down:
#   1. Nothing happens when origin/main is checked out AND recorded as deployed. The record (DEPLOYED, a ref in the
#      checkout's .git) is written only after a healthy restart, so a restart that failed or never ran is retried.
#   2. Only a clean checkout (no tracked edit, no untracked file outside .gitignore) on branch main moves. A checkout
#      off main is a version pinned by hand and is left alone. The documented pin drops the record (README: Upgrade),
#      and so does a tick that sees the pin, so going back to main restarts on main.
#   3. The test suite runs on an export of origin/main BEFORE the checkout moves, so a failing commit never reaches
#      the checkout and the running service is untouched. The page-script tests are skipped: the page script runs
#      in browsers, and the server's node is too old to run it.
#   4. Right before the merge the checkout is checked again, so a pin made during the self-check holds.
#
# config.yaml is tracked, so a settings change is a commit like any other and arrives the same way.
#
# Environment (all optional; the tests set them):
#   XFLARE_LEADERBOARD_CHECKOUT     the deployed checkout (default /home/sw-devops/xflare-leaderboard)
#   XFLARE_LEADERBOARD_PYTHON       the interpreter the service runs (default /usr/bin/python3)
#   XFLARE_LEADERBOARD_HEALTH_WAIT  seconds the board gets to answer after the restart (default 30)
set -euo pipefail

CHECKOUT=${XFLARE_LEADERBOARD_CHECKOUT:-/home/sw-devops/xflare-leaderboard}
PYTHON=${XFLARE_LEADERBOARD_PYTHON:-/usr/bin/python3}
HEALTH_WAIT=${XFLARE_LEADERBOARD_HEALTH_WAIT:-30}
UNIT=xflare-leaderboard
DEPLOYED=refs/auto-update/deployed

# Prints why the checkout may not move, or nothing when it may.
blocked() {
	local branch status
	if ! branch=$(git symbolic-ref -q --short HEAD) || [[ $branch != main ]]; then
		echo "pinned"
	# --untracked-files: status.showUntrackedFiles=no would otherwise hide a stray module that shadows an import.
	elif ! status=$(git status --porcelain --untracked-files=normal); then
		echo "git status failed"
	elif [[ -n $status ]]; then
		echo "the checkout has local edits or untracked files"
	fi
}

cd "$CHECKOUT"

why=$(blocked)
if [[ $why == pinned ]]; then
	git update-ref -d "$DEPLOYED"
	echo "the checkout is pinned at $(git rev-parse --short HEAD) (not on main); leaving it there"
	exit 0
elif [[ -n $why ]]; then
	echo "$why, so this update stops here" >&2
	exit 1
fi

git fetch -q origin main
old=$(git rev-parse HEAD)
new=$(git rev-parse refs/remotes/origin/main)   # in full: a tag named origin/main would win
deployed=$(git rev-parse -q --verify "$DEPLOYED" || true)
if [[ $old == "$new" && $deployed == "$new" ]]; then
	echo "already on ${new:0:8}"
	exit 0
fi
if ! git merge-base --is-ancestor "$old" "$new"; then
	echo "${old:0:8} is not an ancestor of origin/main ${new:0:8}, so this update stops here" >&2
	exit 1
fi

shown=${deployed:-none}
echo "deploying ${new:0:8} (checkout ${old:0:8}, deployed ${shown:0:8})"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/src" "$tmp/data"
git archive "$new" | tar -x -C "$tmp/src"
# The suite makes its own data dirs. A scratch default catches a non-web test that forgets (the web tests clear the
# variable); the unit's InaccessiblePaths= is what denies the live dir to all of them.
if ! env -u XFLARE_LEADERBOARD_CONFIG XFLARE_LEADERBOARD_DATA="$tmp/data" XFLARE_LEADERBOARD_SKIP_NODE_TESTS=1 \
	"$PYTHON" "$tmp/src/leaderboard/test_leaderboard.py" >"$tmp/selfcheck.log" 2>&1; then
	echo "the self-check failed on ${new:0:8}; staying on ${old:0:8}" >&2
	tail -40 "$tmp/selfcheck.log" >&2
	exit 1
fi

why=$(blocked)
if [[ -n $why || $(git rev-parse HEAD) != "$old" ]]; then
	echo "the checkout changed during the self-check (${why:-moved}); leaving it there" >&2
	exit 1
fi
git merge -q --ff-only "$new"
port=$("$PYTHON" -c 'import yaml; print(((yaml.safe_load(open("config.yaml")) or {}).get("server") or {}).get("port", 8894))')
sudo -n /usr/bin/systemctl restart "$UNIT"
deadline=$((SECONDS + HEALTH_WAIT))
while ((SECONDS < deadline)); do
	left=$((deadline - SECONDS))
	((left > 0)) || break   # curl --max-time 0 means no limit
	code=$(curl -s -o /dev/null -w '%{http_code}' --max-time $((left < 5 ? left : 5)) \
		"http://127.0.0.1:$port/api/leaderboard/board" || true)
	if [[ $code == 2?? ]]; then
		git update-ref "$DEPLOYED" "$new"
		echo "restarted on ${new:0:8}"
		exit 0
	fi
	sleep 1
done
echo "restarted on ${new:0:8}, but the board did not answer 2xx on :$port within ${HEALTH_WAIT} s" \
	"(journalctl -u $UNIT); the next tick restarts again" >&2
exit 1
