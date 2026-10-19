#!/usr/bin/env python3
"""xflare TPC-H leaderboard: a standalone Flask service.

Engineers submit their own spark-tpch.sh runs to /api/leaderboard/submissions; the board at
/b/xflare_leaderboard ranks them (see README.md). Settings come from config.yaml.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml
from flask import Flask

from leaderboard import web

HERE = Path(__file__).parent
CONFIG_ENV = "XFLARE_LEADERBOARD_CONFIG"


def load_config(path: str | os.PathLike | None = None) -> dict:
    with open(path or os.environ.get(CONFIG_ENV) or HERE / "config.yaml") as f:
        return yaml.safe_load(f) or {}


def create_app(config: dict | None = None) -> Flask:
    config = config if config is not None else load_config()
    app = Flask(__name__, static_folder=str(HERE / "static"), template_folder=str(HERE / "templates"))
    web.init_app(app, config["leaderboard"])
    return app


def main():
    p = argparse.ArgumentParser(description="xflare TPC-H leaderboard")
    p.add_argument("--config", help=f"settings file (default: ${CONFIG_ENV} or config.yaml)")
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    args = p.parse_args()
    config = load_config(args.config)
    srv = config.get("server") or {}
    host, port = args.host or srv.get("bind", "0.0.0.0"), args.port or srv.get("port", 8894)
    print(f"xflare leaderboard on http://{host}:{port}")
    create_app(config).run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
