from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from litforge.config import load_config
from litforge.llm.hardware import Hardware
from litforge.llm.local_models import (
    Download,
    HuggingFace,
    download_change,
    fit_check,
    hf_url,
    ollama_pull,
    publisher_badge,
)

FIXTURES = Path(__file__).parents[1] / 'fixtures/models'
REPO = 'ggml-org/gemma-4-26B-A4B-it-GGUF'


def test_recorded_hf_search_and_main_files() -> None:
    calls: list[str] = []
    def get(url: str) -> Any:
        calls.append(url)
        return json.loads((FIXTURES / ('hf-tree.json' if '/tree/' in url else 'hf-search.json')).read_text())
    hf = HuggingFace(fetch=get)
    result = hf.search('gemma-4')
    assert len(result) == 3 and result[0]['badge'] == 'official'
    assert result[2]['badge'] == 'community — check the model card'
    tree = hf.files(REPO)
    assert len(tree['models']) == 2 and len(tree['projectors']) == 1
    assert tree['vision'] and tree['models'][0]['sha256'] == 'a' * 64
    assert 'filter=gguf&search=gemma-4&sort=downloads&limit=20' in calls[0]
    assert calls[1] == f'https://huggingface.co/api/models/{REPO}/tree/main'
    assert publisher_badge('unsloth/Example') == 'well-known quantizer'


@pytest.mark.parametrize('repo,file', [('..','a.gguf'), ('a/../b','a.gguf'), ('a/b','../a.gguf'),
    ('https://evil.com/a','a.gguf'), ('a/b','a%2fb.gguf'), ('a/b','/tmp/a.gguf'), ('a/b','a\\b.gguf')])
def test_names_reject_traversal_and_arbitrary_hosts(repo: str, file: str) -> None:
    with pytest.raises(ValueError):
        hf_url(repo, file)


def test_fit_arithmetic() -> None:
    apple = Hardware(os='Darwin', arch='arm64', ram_gb=24, unified_memory=True, free_disk_gb=100)
    fit = fit_check(apple, 14 * 1024**3, 1024**3, context_tokens=16384)
    assert fit['budget_gb'] == 18
    assert fit['needed_gb'] == pytest.approx(16.004)
    assert fit['badge'] == 'fits'
    apple.ram_gb = 21
    assert fit_check(apple, 14 * 1024**3)['badge'] == 'tight'
    cpu = Hardware(os='Linux', arch='x86_64', ram_gb=8, free_disk_gb=1)
    fit = fit_check(cpu, 14 * 1024**3)
    assert fit['badge'] == 'too large' and not fit['disk_ok']
    gpu = Hardware(os='Linux', arch='x86_64', ram_gb=32, gpu_vram_gb=8)
    assert fit_check(gpu, 14 * 1024**3)['partial_offload']


class Response(io.BytesIO):
    def __init__(self, value: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        super().__init__(value)
        self.status, self.headers = status, headers or {}


def test_download_progress_resume_sha_and_registration(tmp_path: Path) -> None:
    content = b'gguf model bytes' * 100
    calls: list[Any] = []
    def open_http(request: Any, **options: Any) -> Response:
        calls.append(request)
        start = int(request.headers.get('Range', 'bytes=0-')[6:-1])
        return Response(content[start:], status=206 if start else 200,
                        headers={'Content-Range': f'bytes {start}-{len(content)-1}/{len(content)}'})
    item = {'path': 'model.gguf', 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    progress: list[dict[str, Any]] = []
    download = Download(tmp_path, opener=open_http, token=lambda: 'HF_SECRET', clock=lambda: 1.)
    part = tmp_path / 'models' / REPO / 'model.gguf.part'
    part.parent.mkdir(parents=True)
    part.write_bytes(content[:10])
    path = download.run(REPO, item, progress=progress.append)
    assert path and path.read_bytes() == content and not part.exists()
    assert calls[0].headers['Range'] == 'bytes=10-'
    assert calls[0].headers['Authorization'] == 'Bearer HF_SECRET'
    assert progress[-1]['bytes'] == len(content)
    change = download_change(tmp_path, REPO, path, name='chosen', tasks=['ask'])
    assert 'model = ' + str(path) in change.diff
    change.apply()
    cfg = load_config(data_root=tmp_path)
    assert cfg.models.profiles['chosen'].server == 'llamacpp'
    assert cfg.models.tasks['ask'].profile == 'chosen'
    assert 'HF_SECRET' not in ''.join(p.read_text(errors='ignore') for p in tmp_path.rglob('*') if p.is_file())


def test_pause_cancel_mismatch_and_range_restart(tmp_path: Path) -> None:
    content = b'data'
    item = {'path': 'm.gguf', 'size': 4, 'sha256': hashlib.sha256(content).hexdigest()}
    download = Download(tmp_path, opener=lambda *a, **k: Response(content), token=lambda: None)
    assert download.run(REPO, item, control=lambda: 'pause') is None
    part = tmp_path / 'models' / REPO / 'm.gguf.part'
    assert part.exists()
    assert download.run(REPO, item, control=lambda: 'cancel') is None and not part.exists()
    part.write_bytes(b'd')
    # Server ignoring Range must restart rather than append a complete response.
    assert download.run(REPO, item).read_bytes() == content
    item['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='sha256'):
        download.run(REPO, item)
    assert not part.exists()


def test_interrupted_download_can_resume(tmp_path: Path) -> None:
    class Interrupted(Response):
        def read(self, size: int = -1) -> bytes:
            if self.tell():
                raise OSError('disconnected')
            return super().read(2)
    data = b'abcd'
    item = {'path': 'm.gguf', 'size': 4, 'sha256': hashlib.sha256(data).hexdigest()}
    download = Download(tmp_path, opener=lambda *a, **k: Interrupted(data), token=lambda: None)
    with pytest.raises(ValueError, match='resume'):
        download.run(REPO, item)
    assert (tmp_path / 'models' / REPO / 'm.gguf.part').read_bytes() == b'ab'


def test_ollama_pull_stream() -> None:
    messages: list[dict[str, Any]] = []
    result = ollama_pull('qwen3:4b', opener=lambda *a, **kw: Response(
        b'{"status":"pulling","total":100,"completed":50}\n{"status":"success"}\n'), progress=messages.append)
    assert result == 'qwen3:4b' and messages[0]['completed'] == 50
    with pytest.raises(ValueError):
        ollama_pull('../bad')


def test_hf_tree_nested_files_and_auxiliary_filter() -> None:
    calls: list[str] = []
    def fetch(url: str) -> Any:
        calls.append(url)
        if '?' not in url:
            return [{'type': 'directory', 'path': 'Q4_K_M'}]
        return [{'path': 'Q4_K_M/main.gguf', 'size': 10, 'lfs': {'oid': 'a' * 64}},
                {'path': 'tokenizer.gguf', 'size': 1, 'lfs': {'oid': 'b' * 64}}]
    tree = HuggingFace(fetch=fetch).files('user/model')
    assert [m['path'] for m in tree['models']] == ['Q4_K_M/main.gguf']
    assert calls[-1].endswith('?recursive=true')


def test_hf_pagination_restricts_next_origin_and_keeps_token_off_other_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm import local_models
    requests: list[Any] = []
    class Page(Response):
        pass
    def open_http(request: Any, **options: Any) -> Page:
        requests.append(request)
        if len(requests) == 1:
            return Page(b'[{"path":"a"}]', headers={'Link':
                '<https://huggingface.co/api/models/user/model/tree/main?cursor=next>; rel="next"'})
        return Page(b'[{"path":"b"}]')
    monkeypatch.setattr(local_models, 'hf_open', open_http)
    monkeypatch.setattr(local_models.KeyStore, 'resolve', lambda *a: 'HF_SECRET')
    data = local_models.hf_json('https://huggingface.co/api/models/user/model/tree/main')
    assert len(data) == 2 and len(requests) == 2
    assert all(r.headers['Authorization'] == 'Bearer HF_SECRET' for r in requests)
    requests.clear()
    monkeypatch.setattr(local_models, 'hf_open', lambda *a, **k: Page(b'[]', headers={
        'Link': '<https://evil.com/steal>; rel="next"'}))
    with pytest.raises(ValueError):
        local_models.hf_json('https://huggingface.co/api/models/user/model/tree/main')


def test_short_response_retains_partial_and_cancel_during_transfer_deletes_it(tmp_path: Path) -> None:
    item = {'path': 'm.gguf', 'size': 4, 'sha256': hashlib.sha256(b'abcd').hexdigest()}
    download = Download(tmp_path, opener=lambda *a, **k: Response(b'ab'), token=lambda: None)
    with pytest.raises(ValueError, match='resume'):
        download.run('user/model', item)
    part = tmp_path / 'models/user/model/m.gguf.part'
    assert part.read_bytes() == b'ab'
    part.unlink()
    state = {'action': ''}
    download.opener = lambda *a, **k: Response(b'abcd')
    result = download.run('user/model', item, control=lambda: state['action'],
                          progress=lambda info: state.update(action='cancel'))
    assert result is None and not part.exists()


def test_model_path_symlinks_rejected(tmp_path: Path) -> None:
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'models').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        Download(tmp_path).path('user/model', 'm.gguf')


def test_search_reads_only_the_first_page_even_with_a_next_link(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real API returns a Link rel="next" that repeats filter/search; search must not follow it.
    from litforge.llm import local_models
    requests: list[Any] = []

    def open_http(request: Any, **options: Any) -> Response:
        requests.append(request)
        return Response(b'[{"id":"ggml-org/Qwen3-4B-GGUF","downloads":5}]', headers={'Link':
            '<https://huggingface.co/api/models?filter=gguf&search=qwen3&limit=20&cursor=abc>; rel="next"'})

    monkeypatch.setattr(local_models, 'hf_open', open_http)
    monkeypatch.setattr(local_models.KeyStore, 'resolve', lambda *a: None)
    assert [r['id'] for r in local_models.HuggingFace().search('qwen3')] == ['ggml-org/Qwen3-4B-GGUF']
    assert len(requests) == 1
