"""Windows has no system IANA timezone database; use the packaged fallback."""
import json
import os
import subprocess
import sys


def test_demo_runs_without_system_timezone_files(tmp_path):
    environment = {**os.environ, "PYTHONTZPATH": ""}
    timezone = subprocess.run(
        [sys.executable, "-c",
         "from zoneinfo import TZPATH, ZoneInfo; "
         "assert TZPATH == (); "
         "assert ZoneInfo('America/New_York').key == 'America/New_York'"],
        env=environment, capture_output=True, text=True, timeout=30,
    )
    assert timezone.returncode == 0, timezone.stderr
    demo = subprocess.run(
        [sys.executable, "-m", "kalshi_bot.cli", "demo", "--data-dir", str(tmp_path / "demo")],
        env=environment, capture_output=True, text=True, timeout=90,
    )
    assert demo.returncode == 0, demo.stderr
    result = json.loads(demo.stdout)
    assert result["dataset"] == "demo"
    assert result["events_processed"] > 0
    assert result["account"]["gross_outcome_exposure"] == 0
