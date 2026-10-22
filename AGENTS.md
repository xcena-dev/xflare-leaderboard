# Agent notes

Before a change reaches main, run the full test suite with the page-script tests, as README "Run locally" says (as a
non-root user, node ≥ 14, `XFLARE_LEADERBOARD_SKIP_NODE_TESTS` unset, a plain `OK` at the end). Nothing else runs
those tests: there is no CI, and the server's auto-updater skips them and deploys what passes the rest.

Never set `XFLARE_LEADERBOARD_SKIP_NODE_TESTS` yourself; only `systemd/xflare-leaderboard-auto-update.sh` does. When
node is missing or older than 14, or you run as root (some tests skip then), report which tests did not run instead
of calling the change tested.
