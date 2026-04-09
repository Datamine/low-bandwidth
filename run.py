from __future__ import annotations

import argparse
import json

from src.actions import ActionController
from src.collector import BandwidthCollector, DEFAULT_SAMPLE_SECONDS
from src.server import serve
from src.tui import run_tui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bandwidth conservation dashboard for macOS")
    parser.add_argument("--ui", choices=("web", "tui"), default="web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8421)
    parser.add_argument("--sample-seconds", type=int, default=DEFAULT_SAMPLE_SECONDS)
    parser.add_argument("--dump-snapshot", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    collector = BandwidthCollector(sample_seconds=max(args.sample_seconds, 1))
    actions = ActionController()
    if args.dump_snapshot:
        snapshot = collector.snapshot()
        print(json.dumps({"snapshot": snapshot.to_dict(), "collector_debug": collector.debug_payload()}, indent=2))
        return 0
    if args.ui == "tui":
        run_tui(collector, actions)
        return 0

    serve(args.host, args.port, collector, actions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
