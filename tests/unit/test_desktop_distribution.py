import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provision = _load("lf_provision", ROOT / "packaging" / "desktop" / "provision.py")


def test_sync_keeps_lock_and_user_added_extras():
    argv = provision.sync_command("uv", ["desktop", "embed"])
    assert argv[:2] == ["uv", "sync"]
    for flag in ("--frozen", "--no-dev", "--inexact"):
        assert flag in argv
    assert argv[argv.index("--python") + 1] == "3.12"
    assert argv[-4:] == ["--extra", "desktop", "--extra", "embed"]


def test_uv_env_writes_only_under_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else")
    env = provision.uv_env(tmp_path, tmp_path / "venv")
    assert "VIRTUAL_ENV" not in env
    for key in ("UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"


def test_failed_sync_is_not_marked_ready_and_can_be_retried(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / "manifest.json").write_text(json.dumps({"app_version": "0.5.0", "target": "linux-x64"}))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "state.json").write_text("{}")  # a previous success must not survive a failed update
    fail = [sys.executable, "-c", "import sys; sys.exit(3)"]
    ok = [sys.executable, "-c", "pass"]
    monkeypatch.setattr(provision, "sync_command", lambda uv, extras: fail)
    assert provision.main(["--app", str(app), "--runtime", str(runtime), "--uv", "uv"]) == 1
    assert not (runtime / "state.json").exists()
    monkeypatch.setattr(provision, "sync_command", lambda uv, extras: ok)
    assert provision.main(["--app", str(app), "--runtime", str(runtime), "--uv", "uv", "--extra", "desktop"]) == 0
    assert json.loads((runtime / "state.json").read_text())["extras"] == ["desktop"]
