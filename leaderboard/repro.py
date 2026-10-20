"""The paste-ready shell script that reruns a submitted run (README §1, manifest argv). Pure: no I/O.

derive() stores it with the run so a board row can copy it on click, and the
run page shows the same text.
"""

from __future__ import annotations

import shlex

# A script past either budget is not offered at all: a cut command would run something else.
MAX_ARGS = 100
MAX_CHARS = 64 * 1024

# Option arities of the manifest producer, xflare-analytics scripts/spark-tpch.sh.
# Values may themselves look like options;
# query IDs can occur between options, and the script has no "--" terminator.
_VALUE_OPTIONS = frozenset({
    "--xflare", "--python", "--launcher", "--timeout", "--spark-conf",
    "--reservation", "--xflare-toml", "--scale-factor", "--codec", "--engines",
    "--results-dir", "--config", "--artifact", "--bundle", "--request",
    "--executors", "--executor-cores", "--offheap", "--shuffle-partitions",
    "--repeat", "--submission",
})
_FLAGS = frozenset({"--download-logs", "--record-answers", "--application-per-engine", "--list", "-h", "--help"})
_QUERY_IDS = frozenset(str(q) for q in range(1, 23))


def _script(run: dict, build: dict, client: dict) -> str | None:
    """The client revision checked out, then spark-tpch.sh with the run's argv.

    The booking is the reader's own, so the run's --reservation value becomes
    "$RESERVATION" and nothing starts until it is set. The artifact and bundle
    this run resolved are pinned unless argv already chose them -- left to
    "the current one" they would silently move to a newer build."""
    argv = list(run["argv"])
    if argv and argv[0].rsplit("/", 1)[-1] == "spark-tpch.sh":
        argv = argv[1:]
    rest, chosen, i = [], set(), 0
    while i < len(argv):
        option = argv[i]
        if option in _VALUE_OPTIONS:
            if i + 1 == len(argv):
                return None
            chosen.add(option)
            rest += [shlex.quote(option), ('"$RESERVATION"' if option == "--reservation"
                                           else shlex.quote(argv[i + 1]))]
            i += 2
            continue
        if option not in _FLAGS and option not in _QUERY_IDS:
            # Unknown arity could change every boundary that follows. Keep the
            # stored argv intact, but offer no script rather than guess.
            return None
        rest.append(shlex.quote(option))
        i += 1
    pins = []
    if "--artifact" not in chosen and build.get("artifact_id"):
        pins += ["--artifact", build["artifact_id"]]
    resolved = (build.get("bundle") or {}).get("resolved")
    if "--bundle" not in chosen and resolved:
        pins += ["--bundle", resolved]
    reservation = [] if "--reservation" in chosen else ['--reservation "$RESERVATION"']
    command = " ".join(["scripts/spark-tpch.sh", *reservation, *(shlex.quote(a) for a in pins), *rest])
    lines = ["# From an xflare-analytics checkout; set RESERVATION=<your PC booking id> first."]
    if client.get("dirty"):
        lines.append("# The run had uncommitted changes: this revision alone does not include them.")
    if (build.get("bundle") or {}).get("kind") == "dev":
        lines.append("# Dev bundle: only its owner can use it; others rebuild it from the gluten commit.")
    lines += ["[ -n \"$RESERVATION\" ] || echo 'set RESERVATION=<your PC booking id> first' >&2",
              f"[ -n \"$RESERVATION\" ] && git fetch origin && git checkout {shlex.quote(client['revision'])} &&",
              f"  {command}"]
    return "\n".join(lines)


def script(manifest: dict) -> dict:
    """{"command": script or None, "omitted": None | "entries" | "length" | "unparsed", "length": chars}."""
    run = manifest["run"]
    if len(run["argv"]) > MAX_ARGS:
        return {"command": None, "omitted": "entries", "length": 0}
    command = _script(run, manifest["build"], manifest["client"])
    if command is None:
        return {"command": None, "omitted": "unparsed", "length": 0}
    if len(command) > MAX_CHARS:
        return {"command": None, "omitted": "length", "length": len(command)}
    return {"command": command, "omitted": None, "length": len(command)}
