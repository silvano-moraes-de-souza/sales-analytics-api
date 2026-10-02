"""Command line.

sales-api seed --scale 0.1 --lake lake      build a lakehouse to serve
sales-api serve --lake lake --port 8000     run the API (docs at /docs)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from .app import create_app
from .seed import build


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="sales-api", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--scale", type=float, default=0.1)
    s.add_argument("--lake", type=Path, required=True)
    r = sub.add_parser("serve")
    r.add_argument("--lake", type=Path, required=True)
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8000)
    a = p.parse_args(argv)
    if a.cmd == "seed":
        print(json.dumps(build(a.lake, a.scale), indent=2))
    else:
        uvicorn.run(create_app(a.lake), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
