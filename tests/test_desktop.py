from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import httpx
import pytest

from kalshi_bot import desktop


def test_instance_lock_excludes_another_process_and_releases(tmp_path):
    lock = tmp_path / "desktop.lock"
    code = ("import sys;from pathlib import Path;from kalshi_bot.desktop import instance_lock;"
            "\nwith instance_lock(Path(sys.argv[1])) as acquired: sys.exit(1 if acquired else 0)")
    with desktop.instance_lock(lock) as acquired:
        assert acquired
        result = subprocess.run([sys.executable, "-c", code, str(lock)], timeout=20)
        assert result.returncode == 0
    with desktop.instance_lock(lock) as acquired:
        assert acquired


def test_health_matching_rejects_other_apps_and_accepts_preparing_database():
    states = iter([{"app_id": "someone-else", "database_id": "db"},
                   {"app_id": desktop.APP_ID, "database_id": "other-db"},
                   {"app_id": desktop.APP_ID, "database_id": "db", "database": {"status": "uninitialized"}}])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503, json=next(states)))) as client:
        assert not desktop._matches(client, "http://127.0.0.1:8000", "db")
        assert not desktop._matches(client, "http://127.0.0.1:8000", "db")
        assert desktop._matches(client, "http://127.0.0.1:8000", "db")


def test_port_selection_leaves_other_listener_alone():
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        port = other.getsockname()[1]
        if port > 65515:
            pytest.skip("Ephemeral port too close to the maximum for the port search")
        assert desktop._available_port(port) != port
        assert other.getsockname()[1] == port


def test_runtime_changes_choose_new_data_directory_without_deleting_old_data(monkeypatch):
    original = desktop.default_data_dir("demo")
    assert original == desktop.default_data_dir("demo")
    monkeypatch.setattr(desktop.platform, "python_version", lambda: "3.12.changed")
    assert desktop.default_data_dir("demo") != original


def test_launcher_stops_owned_server_on_ctrl_c(tmp_path, monkeypatch):
    class Process:
        terminated = False
        def poll(self): return 0 if self.terminated else None
        def terminate(self): self.terminated = True
        def wait(self, timeout): return 0
    server = Process()
    browser = []
    commands = []
    monkeypatch.setattr(desktop, "_matches", lambda *args: True)
    monkeypatch.setattr(desktop, "_demo_complete", lambda database: True)
    monkeypatch.setattr(desktop, "_available_port", lambda preferred: 8123)
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda command: commands.append(command) or server)
    monkeypatch.setattr(desktop.webbrowser, "open", browser.append)
    def interrupt(_): raise KeyboardInterrupt
    monkeypatch.setattr(desktop.time, "sleep", interrupt)
    desktop.launch(mode="demo", data_dir=tmp_path)
    assert len(commands) == 1  # Completed demo is reused; no second collector.
    assert commands[0][-1] == "8123"
    assert server.terminated
    assert browser == ["http://127.0.0.1:8123"]
    assert not (tmp_path / "desktop-instance.json").exists()


def test_second_double_click_opens_existing_app_without_new_process(tmp_path, monkeypatch):
    @contextmanager
    def busy(_): yield False
    monkeypatch.setattr(desktop, "instance_lock", busy)
    monkeypatch.setattr(desktop, "_matches", lambda *args: True)
    (tmp_path / "desktop-instance.json").write_text(json.dumps({"port": 8137}))
    browser = []
    monkeypatch.setattr(desktop.webbrowser, "open", browser.append)
    def unexpected(*args): raise AssertionError("A second launcher started another process")
    monkeypatch.setattr(desktop.subprocess, "Popen", unexpected)
    desktop.launch(data_dir=tmp_path)
    assert browser == ["http://127.0.0.1:8137"]


@pytest.mark.parametrize("mode,port", [("live-money", 8000), ("demo", 0), ("demo", 65536)])
def test_invalid_launch_configuration_is_rejected(mode, port):
    with pytest.raises(ValueError):
        desktop.launch(mode=mode, port=port)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows shortcut integration")
def test_windows_shortcut_installer_handles_spaces_and_repeated_install(tmp_path):
    destination = tmp_path / "My Desktop"
    destination.mkdir()
    root = Path(__file__).resolve().parents[1]
    for _ in range(2):
        installed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-File", str(root / "Create Desktop Shortcut.ps1"),
                                    "-Destination", str(destination)], capture_output=True, text=True, timeout=30)
        assert installed.returncode == 0, installed.stderr
    link = destination / "Kalshi Bot (Live Data).lnk"
    assert link.is_file()
    literal = "'" + str(link).replace("'", "''") + "'"
    command = ("$link=(New-Object -ComObject WScript.Shell).CreateShortcut(" + literal + ");"
               "@{target=$link.TargetPath;arguments=$link.Arguments;working=$link.WorkingDirectory}|ConvertTo-Json -Compress")
    inspected = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, timeout=30)
    assert inspected.returncode == 0, inspected.stderr
    values = json.loads(inspected.stdout)
    assert Path(values["target"]) == root / "Launch Bot.cmd"
    assert values["arguments"] == "--mode paper --config config.example.toml"
    assert Path(values["working"]) == root


def test_default_launch_starts_real_collector_and_cleans_up(tmp_path, monkeypatch):
    class Process:
        terminated = False
        def poll(self): return 0 if self.terminated else None
        def terminate(self): self.terminated = True
        def wait(self, timeout): return 0
    processes, commands = [], []
    def start(command):
        commands.append(command)
        process = Process()
        processes.append(process)
        return process
    monkeypatch.setattr(desktop, "_matches", lambda *args: True)
    monkeypatch.setattr(desktop, "_available_port", lambda preferred: 8123)
    monkeypatch.setattr(desktop.subprocess, "Popen", start)
    def interrupt(_): raise KeyboardInterrupt
    monkeypatch.setattr(desktop.time, "sleep", interrupt)
    desktop.launch(data_dir=tmp_path, open_browser=False)
    assert len(commands) == 2
    assert commands[1][3] == "paper"
    assert all(process.terminated for process in processes)
