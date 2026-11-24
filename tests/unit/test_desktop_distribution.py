import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from litforge import distribution, tools
from litforge.desktop import setup

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
    uv = tmp_path / "uv"
    uv.write_text("")
    monkeypatch.setattr(provision, "sync_command", lambda uv, extras: fail)
    assert provision.main(["--app", str(app), "--runtime", str(runtime), "--uv", str(uv)]) == 1
    assert not (runtime / "state.json").exists()
    monkeypatch.setattr(provision, "sync_command", lambda uv, extras: ok)
    assert provision.main(["--app", str(app), "--runtime", str(runtime), "--uv", str(uv), "--extra", "desktop"]) == 0
    assert json.loads((runtime / "state.json").read_text())["extras"] == ["desktop"]


build = _load("lf_build_desktop", ROOT / "scripts" / "build_desktop.py")
RESOURCES = [
    "configs/default.yaml", "configs/workflows", "src/litforge/ui/static/index.html",
    "src/litforge/ui/static/icon.svg", "src/litforge/ui/static/vendor/pdfjs", "src/litforge/ui/static/vendor/dict/en",
    "src/litforge/ui/static/vendor/dict/fa", "src/litforge/ui/static/vendor/fonts",
    "src/litforge/writing/templates/article", "src/litforge/writing/templates/persian",
    "src/litforge/storage/migrations", "uv.lock", "pyproject.toml", "LICENSE",
]


@pytest.fixture(scope="module")
def payload(tmp_path_factory):
    return build.stage("linux-x64", tmp_path_factory.mktemp("dist"), Path(sys.executable))


def test_payload_bundles_resources_and_nothing_local(payload):
    app = payload / "app"
    for rel in RESOURCES:
        assert (app / rel).exists(), rel
    names = {p.name for p in app.rglob("*")}
    assert not names & {".venv", "__pycache__", ".omc", ".git", "data", ".litforge-uv-cache"}
    assert (payload / "provision.py").is_file()


def test_manifest_checksums_match_payload(payload):
    manifest = json.loads((payload / "app" / "manifest.json").read_text())
    assert manifest["target"] == "linux-x64" and manifest["app_version"]
    assert manifest["sha256"]["app/pyproject.toml"] == build.sha256(payload / "app" / "pyproject.toml")
    assert len(manifest["sha256"]) == sum(1 for p in payload.rglob("*") if p.is_file()) - 1  # all but itself


def test_staged_app_finds_its_resources_from_any_directory(payload, tmp_path):
    probe = ("from litforge.config import DEFAULT_CONFIG_PATH; from litforge.console.workflows import _workflow_dir;"
             "from litforge.desktop import ICON; import litforge, json;"
             "print(json.dumps([str(DEFAULT_CONFIG_PATH), str(_workflow_dir()), str(ICON), litforge.__file__]))")
    env = {**os.environ, "PYTHONPATH": str(payload / "app" / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True,
                         check=True).stdout
    for found in map(Path, json.loads(out)):
        assert found.exists() and found.is_relative_to(payload / "app"), found


def _packaged(tmp_path, monkeypatch) -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "venv").mkdir(parents=True)
    (runtime / "state.json").write_text("{}")
    monkeypatch.setattr(sys, "prefix", str(runtime / "venv"))
    return runtime


def test_source_install_syncs_its_own_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere"))
    assert distribution.packaged_runtime() is None
    flags, env = distribution.uv_sync_env(tmp_path, {"VIRTUAL_ENV": "x"})
    assert flags == [] and "VIRTUAL_ENV" not in env
    assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / ".venv")
    assert distribution.writable_root(tmp_path) == tmp_path


def test_packaged_install_writes_only_in_its_runtime_and_keeps_the_lock(tmp_path, monkeypatch):
    runtime = _packaged(tmp_path, monkeypatch)
    app = tmp_path / "app"
    flags, env = distribution.uv_sync_env(app, {})
    assert flags == ["--frozen"]
    assert env["UV_PROJECT_ENVIRONMENT"] == str(runtime / "venv")
    for key in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"):
        assert Path(env[key]).is_relative_to(runtime)
    assert not any(str(app) in value for value in env.values())
    assert distribution.writable_root(app) == runtime


def test_optional_tool_install_in_a_packaged_app_targets_the_runtime(tmp_path, monkeypatch):
    runtime = _packaged(tmp_path, monkeypatch)
    seen = {}

    class Done(Exception):
        pass

    def fake_popen(argv, **kw):
        seen.update(argv=argv, env=kw["env"], cwd=kw["cwd"])
        raise Done

    monkeypatch.setattr(tools, "_uv_path", lambda: "uv")
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen)
    with pytest.raises(Done):
        tools._uv_sync(tmp_path / "app", ["embed"], lambda line: None, 5)
    assert "--frozen" in seen["argv"] and "--inexact" in seen["argv"]
    assert seen["env"]["UV_PROJECT_ENVIRONMENT"] == str(runtime / "venv")


def test_linux_desktop_setup_in_a_packaged_app_writes_to_the_runtime(tmp_path, monkeypatch):
    runtime = _packaged(tmp_path, monkeypatch)
    commands = []

    def run(cmd, **kw):
        commands.append((cmd, kw))
        if cmd[:2] == ["apt-get", "download"]:
            (Path(kw["cwd"]) / "libxcb-cursor0_1_amd64.deb").write_bytes(b"deb")
        if cmd[:2] == ["dpkg-deb", "-x"]:
            lib = Path(cmd[3]) / "libxcb-cursor.so.0"
            lib.parent.mkdir(parents=True, exist_ok=True)
            lib.touch()
        return subprocess.CompletedProcess(cmd, 1 if "import gi" in " ".join(cmd) else 0, "", "")

    app = tmp_path / "app"
    result = setup.setup_desktop(app, tmp_path / "config", platform="linux", env={"DISPLAY": ":0"}, run=run,
                                 which=lambda name: f"/usr/bin/{name}", find_library=lambda name: None,
                                 uv="uv", system_python=str(tmp_path / "missing-python"))
    sync = next(kw for cmd, kw in commands if cmd[1:2] == ["sync"])
    assert sync["env"]["UV_PROJECT_ENVIRONMENT"] == str(runtime / "venv")
    assert result.preload and all(Path(p).is_relative_to(runtime) for p in result.preload)
    assert not app.exists()


def test_packaged_app_prefers_its_bundled_uv(tmp_path, monkeypatch):
    runtime = _packaged(tmp_path, monkeypatch)
    assert distribution.bundled_uv() is None
    uv = runtime / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    uv.parent.mkdir()
    uv.write_text("")
    assert distribution.bundled_uv() == tools._uv_path() == str(uv)
