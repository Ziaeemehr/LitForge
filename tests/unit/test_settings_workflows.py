from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
from unit.test_models_panel import handler_request

from litforge.analysis.gateway import ModelGateway
from litforge.config import Config, Paths, contact_email, load_config
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.llm.hardware import Hardware
from litforge.llm.local_models import Download, HuggingFace, _HFRedirect, download_change
from litforge.llm.router import CloudApprovalRequired
from litforge.ui import api
from litforge.ui.server import App, Job


def test_library_budget_and_task_diff_preserve_other_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LITFORGE_EXPORT_ROOT')
    (tmp_path / 'config.yaml').write_text('indexing:\n  chunk_target_tokens: 123\n')
    panel = api.ModelsPanel(Paths(tmp_path), load_config(data_root=tmp_path))
    preview = panel.preview({'action': 'library', 'mailto': 'user@example.org', 'exports_root': str(tmp_path / 'out')})
    assert 'settings.json' in preview['diff']
    assert not panel.paths.settings.exists()
    panel.apply(preview['token'], True)
    assert contact_email(panel.paths, panel.config) == 'user@example.org'
    assert Paths(tmp_path).exports_root == tmp_path / 'out'
    preview = panel.preview({'action': 'budget', 'policy': 'hybrid', 'budget': {'max_per_call_usd': .05,
                             'max_per_run_usd': .5, 'max_per_month_usd': 3}})
    panel.apply(preview['token'], True)
    assert panel.config.budget.max_per_month_usd == 3 and not panel.config.privacy.allow_cloud
    assert panel.config.indexing.chunk_target_tokens == 123
    preview = panel.preview({'action': 'assign', 'name': 'local', 'tasks': ['ask']})
    panel.apply(preview['token'], True)
    assert panel.config.models.tasks['ask'].profile == 'local'


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf')])
def test_bad_budget_rejected_without_writing(tmp_path: Path, value: float) -> None:
    panel = api.ModelsPanel(Paths(tmp_path), Config())
    with pytest.raises(ValueError, match='Budget'):
        panel.preview({'action': 'budget', 'budget': {'max_per_call_usd': value}})
    assert not (tmp_path / 'config.yaml').exists()


def test_whole_page_preview_and_hybrid_still_needs_approval(conn: Any) -> None:
    cfg = Config(privacy={'allow_cloud': True}, models={'policy': 'hybrid', 'profiles': {'remote': {
        'provider': 'openai_compatible', 'model': 'm', 'remote': True, 'vision': True,
        'pricing': {'input_per_million_usd': 1, 'output_per_million_usd': 1}}}})
    gateway = ModelGateway(conn, cfg, provider_factory=lambda *args: pytest.fail('transport before approval'))
    evidence = EvidencePacket(items=[EvidenceItem(id='p', paper_id='p', page=1, text='page')])
    preview = gateway.preview_vision('page_rewrite', evidence, [], 'remote', image_count=3)
    assert preview.requires_approval and preview.image_description == '3 page images will be sent'
    with pytest.raises(CloudApprovalRequired):
        gateway.call_vision_text('page_rewrite', evidence, [], [b'image'], 'remote')


def test_hf_token_never_forwarded_to_cdn_or_arbitrary_host() -> None:
    import urllib.request
    request = urllib.request.Request('https://huggingface.co/a/b/resolve/main/m.gguf',
                                     headers={'Authorization': 'Bearer secret'})
    redirected = _HFRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://cdn-lfs.hf.co/file')
    assert 'Authorization' not in redirected.headers
    for url in ['https://evil.com/model', 'http://huggingface.co/model', 'https://huggingface.co.evil.com/model']:
        with pytest.raises(ValueError):
            _HFRedirect().redirect_request(request, None, 302, 'Found', {}, url)


def test_download_preview_job_registration_and_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = App(Paths(tmp_path), Config())
    content = b'GGUFbytes'
    item = {'type': 'file', 'path': 'm.gguf', 'size': len(content),
            'lfs': {'oid': hashlib.sha256(content).hexdigest()}}
    app.models.hf = HuggingFace(fetch=lambda url: [item])
    app.models.hardware = lambda root: Hardware(os='Darwin', arch='arm64', ram_gb=32,
                                               unified_memory=True, free_disk_gb=100)
    class Response(io.BytesIO):
        status = 200
    app.models.downloader = Download(tmp_path, opener=lambda *a, **k: Response(content), token=lambda: None)
    def synchronous(kind: str, project: str, work: Any) -> Job:
        job = Job(id='download', kind=kind, project=project)
        app.jobs[job.id] = job
        work(job)
        job.status = 'done'
        return job
    monkeypatch.setattr(app, 'start_job', synchronous)
    code, preview = handler_request(app, '/api/models/download-preview', {
        'repo': 'user/model', 'file': 'm.gguf', 'name': 'downloaded', 'tasks': ['matrix_extract']})
    assert code == 200 and 'model = ' in preview['diff']
    assert not (tmp_path / 'config.yaml').exists()
    code, _ = handler_request(app, '/api/models/download', {'token': preview['token'], 'confirmed': False})
    assert code == 400
    code, job = handler_request(app, '/api/models/download', {'token': preview['token'], 'confirmed': True})
    assert code == 200 and job['report']['state'] == 'registered'
    assert app.config.models.tasks['matrix_extract'].profile == 'downloaded'
    target = tmp_path / 'models/user/model/m.gguf'
    assert target.read_bytes() == content
    preview = app.models.preview({'action': 'remove', 'name': 'downloaded', 'delete_files': True})
    assert 'Delete downloaded model file' in preview['diff']
    app.models.apply(preview['token'], True)
    assert not target.exists()


def test_image_profile_preset(tmp_path: Path) -> None:
    base = tmp_path / 'models/user/model'
    base.mkdir(parents=True)
    model, projector = base / 'm.gguf', base / 'mmproj.gguf'
    model.touch()
    projector.touch()
    change = download_change(tmp_path, 'user/model', model, mmproj=projector, name='vision')
    change.apply()
    cfg = load_config(data_root=tmp_path)
    assert cfg.models.profiles['vision'].vision
    preset = (tmp_path / 'llamacpp-models.ini').read_text()
    assert 'mmproj = ' + str(projector) in preset and 'ubatch-size = 1152' in preset
    assert 'n-gpu-layers = 99' in preset


def test_remote_provider_errors_never_leak_keys_to_files_db_logs_api_or_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litforge.llm.openai_compatible import OpenAICompatibleClient
    from litforge.llm.secrets import KeyStore
    secret = 'SENTINEL_PROVIDER_KEY'
    monkeypatch.setattr(KeyStore, 'resolve', lambda self, name: secret)
    monkeypatch.setattr(KeyStore, 'source', lambda self, name: 'keychain')
    class Transport:
        def request(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError(secret)
    cfg = Config(privacy={'allow_cloud': True}, models={'policy': 'hybrid', 'profiles': {'remote': {
        'provider': 'openai_compatible', 'model': 'm', 'remote': True, 'api_key_env': 'OPENAI_API_KEY',
        'pricing': {'input_per_million_usd': 1, 'output_per_million_usd': 1}}}})
    app = App(Paths(tmp_path), cfg, make_gateway=lambda conn: ModelGateway(conn, cfg,
        providers={'remote': OpenAICompatibleClient('https://invalid', 'm', 'OPENAI_API_KEY', transport=Transport())}))
    # Execute the same diagnostic worker synchronously, including its failure-report path.
    captured: list[Job] = []
    def synchronous(kind: str, project: str, work: Any) -> Job:
        job = Job(id='test', kind=kind, project=project)
        try:
            work(job)
        except Exception as exc:
            import traceback
            job.status, job.error = 'failed', str(exc)
            app.log.emit('ui.job_failed', error=job.error, trace=traceback.format_exc())
        app.jobs[job.id] = job
        captured.append(job)
        return job
    monkeypatch.setattr(app, 'start_job', synchronous)
    status, response = handler_request(app, '/api/models/test', {'profile': 'remote', 'approved': True})
    assert status == 200 and captured[0].status == 'failed'
    assert secret not in json.dumps(response)
    conn = app.db()
    assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 1
    assert secret not in '\n'.join(conn.iterdump())
    conn.close()
    assert secret not in json.dumps({k: v.__dict__ for k, v in app.jobs.items()})
    assert all(secret.encode() not in f.read_bytes() for f in tmp_path.rglob('*') if f.is_file())


def test_download_too_large_needs_separate_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = App(Paths(tmp_path), Config())
    app.models.hardware = lambda root: Hardware(os='Linux', arch='x86_64', ram_gb=1, free_disk_gb=100)
    app.models.hf = HuggingFace(fetch=lambda url: [{'path': 'm.gguf', 'size': 1024**3,
                                                  'lfs': {'oid': 'a' * 64}}])
    preview = app.models.prepare_download({'repo': 'user/model', 'file': 'm.gguf'})
    monkeypatch.setattr(app, 'start_job', lambda *args: pytest.fail('download before large-model approval'))
    with pytest.raises(ValueError, match='too large'):
        app.download_job({'token': preview['token'], 'confirmed': True})
    assert not (tmp_path / 'models').exists()


def test_download_job_pause_resume_and_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = App(Paths(tmp_path), Config())
    app.models.hardware = lambda root: Hardware(os='Linux', arch='x86_64', ram_gb=32, free_disk_gb=100)
    app.models.hf = HuggingFace(fetch=lambda url: [{'path': 'm.gguf', 'size': 10, 'lfs': {'oid': 'a' * 64}}])
    preview = app.models.prepare_download({'repo': 'user/model', 'file': 'm.gguf'})
    works: list[Any] = []
    def queue(kind: str, project: str, work: Any) -> Job:
        job = Job(id=str(len(works)), kind=kind, project=project)
        works.append(work)
        app.jobs[job.id] = job
        return job
    monkeypatch.setattr(app, 'start_job', queue)
    def download(repo: str, item: Any, *, control: Any, progress: Any) -> None:
        assert control() in {'pause', 'cancel'}
        return None
    monkeypatch.setattr(app.models.downloader, 'run', download)
    first = app.download_job({'token': preview['token'], 'confirmed': True})
    app.download_control({'id': first.id, 'action': 'pause'})
    works[0](first)
    first.status = 'done'
    assert first.report['state'] == 'paused'
    second = app.download_control({'id': first.id, 'action': 'resume'})
    app.download_control({'id': second.id, 'action': 'cancel'})
    works[1](second)
    second.status = 'done'
    assert second.report['state'] == 'cancelled'
    assert not (tmp_path / 'config.yaml').exists()


def test_export_preview_reports_count_without_sending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.analysis.gateway import CallPreview
    from litforge.projects import create_project
    from litforge.ui import server
    app = App(Paths(tmp_path), Config())
    conn = app.db()
    create_project(conn, app.paths, 'project')
    conn.close()
    page = CallPreview(profile='local_vision', provider='ollama', model='m', remote=False, evidence_ids=[],
                       estimated_input_tokens=1, max_output_tokens=1, max_cost_usd=0, requires_approval=False)
    monkeypatch.setattr(server, 'plan_project', lambda *a, **k: [page, page, page])
    code, preview = handler_request(app, '/api/export-preview', {'project': 'project'})
    assert code == 200 and preview['image_description'] == '3 page images will be sent'
    assert not app.jobs


def test_catalogue_http_route_uses_filters(tmp_path: Path) -> None:
    from litforge.llm.pricing import OpenRouterPricing
    app = App(Paths(tmp_path), Config())
    fixture = Path(__file__).parents[1] / 'fixtures/models/openrouter.json'
    app.models.catalogue = OpenRouterPricing(tmp_path, fetch=lambda: json.loads(fixture.read_text()))
    code, models = handler_request(app, '/api/models/catalogue?vision=1&max_input_price=0.5&q=fixture')
    assert code == 200 and [m['id'] for m in models] == ['fixture/vision-cheap']


def test_downloaded_preset_restarts_owned_router_on_next_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm.llamacpp_server import LlamaCppServer
    from litforge.llm.model_setup import setup_proposal
    setup_proposal(tmp_path, Hardware(os='Darwin', arch='arm64', ram_gb=32, unified_memory=True,
                                     llama_server='/fake/llama-server'), 'large').change.apply()
    base = tmp_path / 'models/user/model'
    base.mkdir(parents=True)
    model = base / 'm.gguf'
    model.touch()
    stopped: list[str] = []
    monkeypatch.setattr(LlamaCppServer, 'stop', lambda self, cfg: stopped.append(cfg.preset) or True)
    change = download_change(tmp_path, 'user/model', model, name='downloaded')
    change.apply()
    assert stopped == ['llamacpp-models.ini']
    cfg = load_config(data_root=tmp_path)
    assert cfg.models.profiles['gemma4'].server == cfg.models.profiles['downloaded'].server == 'llamacpp'
