"""Own the local collector/dashboard processes and open the user's browser."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from importlib.metadata import version
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
import time
import webbrowser

import httpx

APP_ID = "kalshi-paper-research"


def database_id(path: str | Path) -> str:
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


def default_data_dir(mode: str, config_file: str | None = None) -> Path:
    """Keep previous experiments when code, configuration or runtime changes."""
    from .config import BotConfig
    config = BotConfig.load(config_file) if mode == "paper" else BotConfig(clock_uncertainty_ms=0)
    digest = hashlib.sha256(json.dumps(config.to_dict(), sort_keys=True).encode())
    package = Path(__file__).parent
    for source in sorted(package.glob("*.py")):
        digest.update(source.name.encode() + source.read_bytes())
    lock = package.parent / "uv.lock"
    if lock.is_file():
        digest.update(lock.read_bytes())
    if config.market_overrides_file:
        digest.update(Path(config.market_overrides_file).read_bytes())
    digest.update(platform.python_version().encode())
    for name in ("httpx", "websockets", "cryptography", "fastapi", "uvicorn", "pyarrow", "tzdata"):
        digest.update((name + version(name)).encode())
    return Path("data") / "desktop" / f"{mode}-{digest.hexdigest()[:16]}"


@contextmanager
def instance_lock(path: Path):
    """Nonblocking OS lock; process death releases it on Windows and Linux."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _available_port(preferred: int) -> int:
    for candidate in range(preferred, min(preferred + 20, 65536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise OSError("No free local dashboard port. Close the previous launcher and try again.")


def _matches(client: httpx.Client, url: str, identity: str) -> bool:
    try:
        response = client.get(url + "/health")
        status = response.json()
        return status.get("app_id") == APP_ID and status.get("database_id") == identity
    except (httpx.HTTPError, ValueError):
        return False


def _demo_complete(database: Path) -> bool:
    if not database.is_file():
        return False
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            return connection.execute("SELECT COUNT(*) FROM events WHERE type='settlement' "
                                      "AND json_extract(payload,'$.dataset')='demo'").fetchone()[0] >= 3
    except sqlite3.Error:
        return False


def _stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)


def launch(mode="demo", data_dir=None, config_file=None, port=8000, open_browser=True):
    if mode not in {"demo", "paper"} or not 1 <= port <= 65535:
        raise ValueError("Choose demo/paper mode and a port between 1 and 65535")
    directory = (Path(data_dir) if data_dir else default_data_dir(mode, config_file)).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "paper.sqlite3"
    identity = database_id(database)
    state_file = directory / "desktop-instance.json"
    with instance_lock(directory / "desktop.lock") as acquired:
        with httpx.Client(timeout=1, trust_env=False) as client:
            if not acquired:
                # A second double click must open the existing app, never a second collector.
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        state = json.loads(state_file.read_text())
                        existing_port = int(state["port"])
                        if not 1 <= existing_port <= 65535:
                            raise ValueError("Invalid instance port")
                        url = f"http://127.0.0.1:{existing_port}"
                        if _matches(client, url, identity):
                            if open_browser:
                                webbrowser.open(url)
                            print("Opened the existing bot window.", flush=True)
                            return
                    except (OSError, ValueError, KeyError):
                        pass
                    time.sleep(.25)
                raise OSError("The bot is already starting. Wait a moment and open the shortcut again.")
            dashboard = collector = None
            try:
                chosen_port = _available_port(port)
                url = f"http://127.0.0.1:{chosen_port}"
                dashboard = subprocess.Popen([sys.executable, "-m", "kalshi_bot.cli", "dashboard",
                                               "--data-dir", str(directory), "--port", str(chosen_port)])
                deadline = time.monotonic() + 20
                while not _matches(client, url, identity):
                    if dashboard.poll() is not None or time.monotonic() >= deadline:
                        raise OSError("The local dashboard could not start. See the messages in this window.")
                    time.sleep(.2)
                state_file.write_text(json.dumps({"port": chosen_port, "pid": os.getpid()}))
                print(f"PAPER ONLY | {mode.upper()} | {url}\nData: {directory}", flush=True)
                print("Keep this window open. Close it or press Ctrl+C to stop the bot.", flush=True)
                if open_browser:
                    webbrowser.open(url)
                if mode == "paper" or not _demo_complete(database):
                    command = [sys.executable, "-m", "kalshi_bot.cli", mode, "--data-dir", str(directory)]
                    if mode == "paper" and config_file:
                        command.extend(["--config", str(Path(config_file).resolve())])
                    print("Preparing synthetic demo data..." if mode == "demo" else
                          "Starting real-data PAPER collector...", flush=True)
                    collector = subprocess.Popen(command)
                while dashboard.poll() is None:
                    if collector is not None and collector.poll() is not None:
                        if collector.returncode != 0 or mode == "paper":
                            raise OSError("The collector stopped. Read its error above; the launcher will stop.")
                        print("Demo ready. Use Play demo in the chart to replay its trades.", flush=True)
                        collector = None
                    time.sleep(.25)
                if dashboard.returncode:
                    raise OSError("The dashboard stopped with an error.")
            except KeyboardInterrupt:
                print("\nStopping the local bot...", flush=True)
            finally:
                _stop(collector)
                _stop(dashboard)
                state_file.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open the local PAPER bot and browser")
    parser.add_argument("--mode", choices=["demo", "paper"], default="demo")
    parser.add_argument("--config")
    parser.add_argument("--data-dir")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        launch(args.mode, args.data_dir, args.config, args.port, not args.no_browser)
    except (OSError, ValueError) as exc:
        print(f"Kalshi launcher: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
