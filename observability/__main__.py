"""Entry point: ``python -m observability [--port N]``.

Read-only. Point it at the harness checkout and it discovers every run
database underneath, newest write first, and serves the page on loopback.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from observability import reader
from observability.server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m observability",
        description="Live, read-only observability over harness run databases: "
                    "context composition, cache behaviour, event flow and calls.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (loopback by default)")
    parser.add_argument("--port", type=int, default=8788, help="bind port (0 picks a free one)")
    parser.add_argument("--root", default=None,
                        help="checkout to scan for run databases (default: this repo)")
    parser.add_argument("--context-window", type=int, default=reader.DEFAULT_CONTEXT_WINDOW,
                        help="model context window in tokens, for the occupancy meter")
    args = parser.parse_args(argv)

    server = serve(host=args.host, port=args.port,
                   root=Path(args.root) if args.root else reader.repo_root(),
                   context_window=args.context_window)
    host, port = server.server_address[:2]
    print(f"serving {args.root or reader.repo_root()}", flush=True)
    print(f"open http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
