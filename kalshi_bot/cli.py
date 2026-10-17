"""CLI entry points for a paper-only local engine and read-only dashboard."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import gzip
import json
from pathlib import Path
import signal
import sys

from .config import BotConfig
from .demo import demo_events
from .engine import Engine


def _json(value):
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _read_events(source):
    path = Path(source)
    if path.suffix in {".sqlite3", ".db", ".sqlite"}:
        import sqlite3
        from urllib.parse import quote
        connection = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
        try:
            for row in connection.execute("SELECT payload FROM events ORDER BY id"):
                yield json.loads(row[0])
        finally:
            connection.close()
    else:
        files = sorted(path.glob("*.jsonl.gz")) if path.is_dir() else [path]
        for file in files:
            opener = gzip.open if file.suffix == ".gz" else open
            with opener(file, "rt", encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        yield json.loads(line)


async def _collect(engine, config, duration):
    from .feeds import live_events
    settings = config.to_dict()
    if engine.frozen_overrides:
        settings["market_overrides_file"] = str(engine.frozen_overrides)
    settings["track_tickers"] = sorted({p["market_ticker"] for p in engine.ledger.positions()}
                                       | {p["market_ticker"] for p in engine.ledger.pending_orders()})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(number, stop.set)

    async def consume():
        async for event in live_events(settings):
            engine.process(event)
            if event["type"] == "health" and event.get("details", {}).get("status") != "ok":
                details = event.get("details", {})
                print(f"{details.get('component', 'feed')}: {details.get('status')} — "
                      f"{details.get('reason', '')}", flush=True)

    worker = asyncio.create_task(consume())
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({worker, stopper}, timeout=duration,
                                     return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
    finally:
        worker.cancel()
        stopper.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        with suppress(asyncio.CancelledError):
            await stopper


def _parser():
    parser = argparse.ArgumentParser(description="Kalshi BTC/ETH/SOL research bot — PAPER ONLY")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run a deterministic synthetic round; requires no credentials")
    demo.add_argument("--data-dir", default="data/demo")
    paper = sub.add_parser("paper", help="Collect real market data and simulate eligible trades")
    paper.add_argument("--config")
    paper.add_argument("--data-dir", default="data/paper")
    paper.add_argument("--duration", type=float, help="Optional collection duration in seconds")
    dashboard = sub.add_parser("dashboard", help="Serve the read-only local dashboard")
    dashboard.add_argument("--data-dir", default="data/demo")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8000)
    desktop = sub.add_parser("desktop", help="Start the bot and open its dashboard in your browser")
    desktop.add_argument("--mode", choices=["demo", "paper"], default="paper")
    desktop.add_argument("--config")
    desktop.add_argument("--data-dir")
    desktop.add_argument("--port", type=int, default=8000)
    desktop.add_argument("--no-browser", action="store_true")
    replay = sub.add_parser("replay", help="Replay recorded JSONL/gzip/SQLite events into a new directory")
    replay.add_argument("source")
    replay.add_argument("--data-dir", required=True)
    replay.add_argument("--config")
    for name, helptext in (("report", "Descriptive P&L and trajectory report"),
                           ("compare-exits", "Paired paper exit-policy comparison"),
                           ("export", "Export the SQLite research tables to Parquet")):
        command = sub.add_parser(name, help=helptext)
        command.add_argument("--data-dir", default="data/demo")
        if name == "export":
            command.add_argument("--output", default="data/exports")
    return parser


def _replay_settings(source, config_file, dataset):
    if config_file:
        return BotConfig.load(config_file)
    path = Path(source)
    metadata = None
    if path.suffix in {".sqlite3", ".db", ".sqlite"}:
        import sqlite3
        from urllib.parse import quote
        connection = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT config_json FROM run_config ORDER BY rowid DESC LIMIT 1").fetchone()
            metadata = json.loads(row[0]) if row else None
        finally:
            connection.close()
    else:
        candidates = [(path if path.is_dir() else path.parent) / "run.json",
                      (path if path.is_dir() else path.parent).parent / "run.json"]
        for candidate in candidates:
            if candidate.is_file():
                metadata = json.loads(candidate.read_text())
                break
    if metadata:
        values = {key: value for key, value in metadata.items()
                  if key in BotConfig.__dataclass_fields__}
        if values.get("market_overrides_file"):
            root = path.parent if path.is_file() and path.suffix in {".sqlite3", ".db", ".sqlite"} else (
                path if path.is_dir() else path.parent)
            for candidate in (root / "market-overrides.json", root.parent / "market-overrides.json"):
                if candidate.is_file():
                    values["market_overrides_file"] = str(candidate)
                    break
        return BotConfig(**values)
    if dataset == "demo":
        return BotConfig(clock_uncertainty_ms=0)
    raise ValueError("Real-data replay requires --config or the source run's run.json metadata")


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "desktop":
            from .desktop import launch
            launch(args.mode, args.data_dir, args.config, args.port, not args.no_browser)
            return
        if args.command == "dashboard":
            import uvicorn
            from .dashboard import create_app
            uvicorn.run(create_app(Path(args.data_dir) / "paper.sqlite3"),
                        host=args.host, port=args.port, access_log=False)
            return
        if args.command in {"report", "compare-exits", "export"}:
            from .research import analyze, compare_exits, export_parquet
            database = Path(args.data_dir) / "paper.sqlite3"
            result = (analyze(database) if args.command == "report" else
                      compare_exits(database) if args.command == "compare-exits" else
                      export_parquet(database, args.output))
            _json(result)
            return
        if args.command == "replay":
            destination = Path(args.data_dir)
            if (destination / "paper.sqlite3").exists():
                raise ValueError("Replay requires a new data directory to avoid mixing portfolios")
            stream = iter(_read_events(args.source))
            first = next(stream, None)
            if first is None:
                raise ValueError("No events found in replay input")
            dataset = first.get("dataset", "real")
            config = _replay_settings(args.source, args.config, dataset)
            engine = Engine(destination, config, mode="replay", dataset=dataset)
            try:
                engine.process(first)
                count = 1
                for event in stream:
                    count += engine.process(event)
                _json({"events_processed": count, "dataset": dataset,
                       "account": engine.ledger.account(engine.last_received)})
            finally:
                engine.close()
            return
        config = BotConfig.load(args.config) if args.command == "paper" else BotConfig(clock_uncertainty_ms=0)
        if args.command == "paper" and args.duration is not None and args.duration <= 0:
            raise ValueError("--duration must be positive")
        engine = Engine(args.data_dir, config, mode=args.command)
        try:
            if args.command == "demo":
                count = sum(engine.process(event) for event in demo_events())
                _json({"dataset": "demo", "notice": "Synthetic demonstration; no performance inference",
                       "events_processed": count, "account": engine.ledger.account(engine.last_received)})
            else:
                print("PAPER ONLY — collecting real data; all account balances and orders are simulated.", flush=True)
                asyncio.run(_collect(engine, config, args.duration))
        finally:
            engine.close()
    except (ValueError, FileNotFoundError, PermissionError, OSError) as exc:
        print(f"kalshi-bot: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
