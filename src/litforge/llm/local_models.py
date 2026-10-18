"""Public GGUF discovery, bounded fit estimates, resumable verified downloads and registration."""
from __future__ import annotations

import configparser
import hashlib
import io
import json
import os
import re
import shutil
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from litforge.config import load_config
from litforge.jobs import check_cancelled
from litforge.llm.files import file_lock
from litforge.llm.hardware import Hardware
from litforge.llm.http import open_url
from litforge.llm.model_setup import ConfigChange, api_model_change, config_change
from litforge.llm.secrets import KeyStore

OFFICIAL = {'ggml-org', 'google', 'Qwen', 'meta-llama', 'mistralai', 'microsoft', 'openai'}
QUANTIZERS = {'unsloth', 'bartowski', 'lmstudio-community'}


def validate_repo(repo: str) -> str:
    if not isinstance(repo, str) or not re.fullmatch(
            r'[A-Za-z0-9_-][A-Za-z0-9._-]*/[A-Za-z0-9_-][A-Za-z0-9._-]*', repo):
        raise ValueError('Invalid Hugging Face repository name (publisher/repository)')
    if '..' in repo:
        raise ValueError('Invalid Hugging Face repository name')
    return repo


def validate_file(file: str) -> str:
    # Nested quantization directories are valid; reject every traversal/escape syntax before URL/path creation.
    if not isinstance(file, str) or len(file) > 512 or not file.endswith('.gguf'):
        raise ValueError('Choose a GGUF filename')
    if any(not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]*', part) or '..' in part for part in file.split('/')):
        raise ValueError('Invalid Hugging Face filename')
    return file


def hf_url(repo: str, file: str) -> str:
    return f'https://huggingface.co/{validate_repo(repo)}/resolve/main/{quote(validate_file(file), safe="/")}'


def publisher_badge(repo: str) -> str:
    publisher = repo.split('/')[0]
    return ('official' if publisher in OFFICIAL else 'well-known quantizer' if publisher in QUANTIZERS
            else 'community — check the model card')


class _HFRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        url = urlsplit(newurl)
        host = url.hostname or ''
        if url.scheme != 'https' or url.username or url.password or not (
                host == 'huggingface.co' or host.endswith('.huggingface.co') or host.endswith('.hf.co')):
            raise ValueError('Untrusted model download redirect')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and host != 'huggingface.co':
            redirected.remove_header('Authorization')
        return redirected


def hf_open(request: urllib.request.Request, *, timeout: float) -> Any:
    # Only validated HF API/resolve URLs enter here. HF's HTTPS weight CDN redirects get no credential.
    if urlsplit(request.full_url).scheme != 'https' or urlsplit(request.full_url).netloc != 'huggingface.co':
        raise ValueError('Only Hugging Face HTTPS URLs are allowed')
    return urllib.request.build_opener(_HFRedirect()).open(request, timeout=timeout)


def hf_json(url: str, *, paginate: bool = True) -> Any:
    original = urlsplit(url)
    if original.scheme != 'https' or original.netloc != 'huggingface.co':
        raise ValueError('Only Hugging Face HTTPS URLs are allowed')
    pages: list[Any] = []
    seen: set[str] = set()
    try:
        while url not in seen and len(seen) < 100:
            seen.add(url)
            headers = {}
            token = KeyStore().resolve('HF_TOKEN')
            if token:
                headers['Authorization'] = 'Bearer ' + token
            with hf_open(urllib.request.Request(url, headers=headers), timeout=15) as response:
                data = json.load(response)
                link = response.headers.get('Link', '')
            next_link = re.search(r'<([^>]+)>;\s*rel="next"', link)
            if not isinstance(data, list):
                return data
            pages.extend(data)
            if not next_link or not paginate:  # search: the first page (sorted by downloads) is enough
                return pages
            next_url = urlsplit(next_link[1])
            if (next_url.scheme != 'https' or next_url.netloc != 'huggingface.co' or next_url.path != original.path or
                    next_url.fragment):
                raise ValueError('Untrusted catalogue pagination')
            params = parse_qsl(next_url.query)
            if any(k not in {'cursor', 'limit', 'recursive', 'expand'} for k, _ in params):
                raise ValueError('Untrusted catalogue pagination')
            # The only variable URL components are query parameters of the same validated API path.
            url = 'https://huggingface.co' + original.path + '?' + urlencode(params)
        raise ValueError('Catalogue pagination limit reached')
    except Exception:
        raise ValueError('Hugging Face unavailable; check repository, access permission and connection') from None


class HuggingFace:
    def __init__(self, *, fetch: Callable[[str], Any] = hf_json,
                 search_fetch: Callable[[str], Any] | None = None) -> None:
        self.fetch = fetch
        self.search_fetch = search_fetch or (
            (lambda url: hf_json(url, paginate=False)) if fetch is hf_json else fetch)

    def search(self, query: str, *, show_all: bool = False) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError('Enter a model search (1–200 characters)')
        data = self.search_fetch('https://huggingface.co/api/models?' + urlencode(
            {'filter': 'gguf', 'search': query.strip(), 'sort': 'downloads', 'limit': 20,
             'expand[]': ['pipeline_tag', 'tags', 'downloads']}, doseq=True))
        result = []
        for entry in data if isinstance(data, list) else []:
            try:
                repo = validate_repo(entry['id'])
                downloads = max(0, int(entry.get('downloads', 0)))
                tags = entry.get('tags', [])
                metadata = ' '.join([repo.split('/', 1)[1], str(entry.get('pipeline_tag', '')),
                                     *(t for t in tags if isinstance(t, str))] if isinstance(tags, list)
                                    else [repo.split('/', 1)[1], str(entry.get('pipeline_tag', ''))])
                chat_model = not bool(re.search(
                    r'embedd?ing|rerank|sentence-transformers|sentence-similarity|feature-extraction|'
                    r'(?:^|[^a-z])(?:asr|tts)(?:[^a-z]|$)|speech|audio|'
                    r'(?:text|image)[-_ ]to[-_ ]image|image[-_ ]generation|classif(?:ication|ier)|'
                    r'stable[-_ ]diffusion|diffusers|(?:^|[^a-z])(?:flux|sdxl)(?:[^a-z]|$)', metadata, re.I))
                if not chat_model and not show_all:
                    continue
                result.append({'id': repo, 'downloads': downloads, 'badge': publisher_badge(repo),
                               'card_url': 'https://huggingface.co/' + repo, 'chat_model': chat_model,
                               'label': '' if chat_model else 'not a chat model'})
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
        return result

    def files(self, repo: str) -> dict[str, Any]:
        repo = validate_repo(repo)
        data = self.fetch(f'https://huggingface.co/api/models/{repo}/tree/main')
        if isinstance(data, list) and any(isinstance(e, dict) and e.get('type') == 'directory' for e in data):
            data = self.fetch(f'https://huggingface.co/api/models/{repo}/tree/main?recursive=true')
        models: list[dict[str, Any]] = []
        projectors: list[dict[str, Any]] = []
        for entry in data if isinstance(data, list) else []:
            try:
                path = validate_file(entry['path'])
                size = int(entry['size'])
                sha = entry.get('lfs', {}).get('oid', '')
                if size <= 0 or not re.fullmatch(r'[a-fA-F0-9]{64}', sha):
                    continue
                auxiliary = any(part.lower().startswith(('mtp-', 'dflash-', 'draft-', 'adapter-', 'imatrix',
                                                        'tokenizer', 'vocab', 'embedding-'))
                                for part in path.split('/'))
                if auxiliary:
                    continue
                item = {'path': path, 'size': size, 'sha256': sha.lower()}
                (projectors if Path(path).name.lower().startswith('mmproj') else models).append(item)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
        return {'repo': repo, 'models': models, 'projectors': projectors, 'vision': bool(projectors)}


def fit_check(hw: Hardware, size: int, mmproj_size: int = 0, *, context_tokens: int = 16384) -> dict[str, Any]:
    if min(size, mmproj_size) < 0 or not 1 <= context_tokens <= 2_000_000:
        raise ValueError('Invalid file size or context')
    # The specification's stated rough rule: 0.25 MB per 1000 context tokens, plus 1 GiB overhead.
    needed = (size + mmproj_size) / 1024**3 + context_tokens / 1000 * .25 / 1024 + 1
    partial = False
    if hw.unified_memory:
        budget = hw.ram_gb * .75
    elif hw.gpu_vram_gb:
        budget = hw.gpu_vram_gb
        if needed > budget:
            budget += hw.ram_gb * .6
            partial = True
    else:
        budget = hw.ram_gb * .6
    badge = 'fits' if needed <= budget * .9 else 'tight' if needed <= budget else 'too large'
    disk = (size + mmproj_size) / 1024**3
    return {'budget_gb': budget, 'needed_gb': needed, 'badge': badge, 'partial_offload': partial,
            'note': 'slower: partly on CPU' if partial else '', 'disk_ok': hw.free_disk_gb >= disk,
            'kv_rule': 'Rough estimate: context tokens × 0.25 MB per 1000 tokens, plus 1 GB overhead'}


class Download:
    def __init__(self, root: Path, *, opener: Callable[..., Any] = hf_open,
                 token: Callable[[], str | None] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.root, self.opener, self.clock = root.resolve(), opener, clock
        self.token = token or (lambda: KeyStore().resolve('HF_TOKEN'))

    def path(self, repo: str, file: str) -> Path:
        validate_repo(repo)
        validate_file(file)
        base = (self.root / 'models').resolve()
        target = base / repo / file
        if base != self.root / 'models' or not target.resolve().is_relative_to(base):
            raise ValueError('Model directory must stay under the data directory')
        return target

    @staticmethod
    def _verified(path: Path, size: int, sha: str) -> bool:
        if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
            return False
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest() == sha

    def run(self, repo: str, item: dict[str, Any], *, progress: Callable[[dict[str, Any]], None] = lambda p: None,
            control: Callable[[], str] = lambda: '') -> Path | None:
        url = hf_url(repo, item['path'])
        target = self.path(repo, item['path'])
        size, sha = int(item['size']), item['sha256']
        if size <= 0 or not re.fullmatch('[a-f0-9]{64}', sha):
            raise ValueError('A valid LFS sha256 and size are required')
        part = target.with_suffix(target.suffix + '.part')
        target.parent.mkdir(parents=True, exist_ok=True)
        if part.is_symlink() or target.is_symlink():
            raise ValueError('Model files cannot be symlinks')
        guard = self.root / 'state' / 'download-locks' / (hashlib.sha256(str(target).encode()).hexdigest() + '.lock')
        with file_lock(guard):
            try:
                with file_lock(target.with_suffix('.lock')):
                    return self._transfer(target, part, url, size, sha, progress, control)
            finally:
                target.with_suffix('.lock').unlink(missing_ok=True)

    def _transfer(self, target: Path, part: Path, url: str, size: int, sha: str,
                  progress: Callable[[dict[str, Any]], None], control: Callable[[], str]) -> Path | None:
        if self._verified(target, size, sha):
            progress({'bytes': size, 'total': size, 'speed': 0, 'eta_s': 0, 'verified': True})
            return target
        start = part.stat().st_size if part.exists() else 0
        if start > size:
            part.unlink()
            start = 0
        if shutil.disk_usage(target.parent).free < size - start:
            raise ValueError('Not enough free disk space for this download')
        part.touch(exist_ok=True)
        if control() in {'pause', 'cancel'}:
            if control() == 'cancel':
                part.unlink(missing_ok=True)
            return None
        headers = {'Range': f'bytes={start}-'} if start else {}
        token = self.token()
        if token:
            headers['Authorization'] = 'Bearer ' + token
        began = self.clock()
        received = start
        try:
            if start < size:
                with self.opener(urllib.request.Request(url, headers=headers), timeout=60) as response:
                    if start and response.status == 200:
                        start = received = 0
                    elif start and (response.status != 206 or
                                    not response.headers.get('Content-Range', '').startswith(f'bytes {start}-')):
                        raise ValueError('Invalid resume response')
                    with part.open('ab' if start else 'wb') as stream:
                        while True:
                            state = control()
                            if state in {'pause', 'cancel'}:
                                if state == 'cancel':
                                    stream.close()
                                    part.unlink(missing_ok=True)
                                return None
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            received += len(block)
                            if received > size:
                                stream.close()
                                part.unlink(missing_ok=True)
                                raise ValueError('Download exceeds expected size')
                            stream.write(block)
                            elapsed = max(.001, self.clock() - began)
                            speed = max(0, received - start) / elapsed
                            progress({'bytes': received, 'total': size, 'speed': speed,
                                      'eta_s': max(0, size - received) / speed if speed else None})
                        stream.flush()
                        os.fsync(stream.fileno())
        except Exception:
            if control() == 'cancel':
                part.unlink(missing_ok=True)
                return None
            if control() == 'pause':
                return None
            raise ValueError('Download interrupted; resume to continue') from None
        if part.stat().st_size != size:
            raise ValueError('Download interrupted; resume to continue')
        digest = hashlib.sha256()
        with part.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                state = control()
                if state in {'pause', 'cancel'}:
                    stream.close()
                    if state == 'cancel':
                        part.unlink(missing_ok=True)
                    return None
                digest.update(block)
        if part.stat().st_size != size or digest.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise ValueError('Download sha256/size verification failed; partial file deleted')
        os.replace(part, target)
        progress({'bytes': size, 'total': size, 'speed': 0, 'eta_s': 0, 'verified': True})
        return target


def download_change(root: Path, repo: str, model: Path, *, mmproj: Path | None = None, name: str | None = None,
                    tasks: list[str] | None = None, context_tokens: int = 16384,
                    require_files: bool = True) -> ConfigChange:
    validate_repo(repo)
    name = name or re.sub(r'[^a-zA-Z0-9_-]', '_', model.stem)
    if not re.fullmatch('[a-zA-Z0-9_-]+', name) or name in {'local', 'local_vision'}:
        raise ValueError('Choose a profile name with letters, numbers, hyphens or underscores')
    downloader = Download(root)
    for path in [model, *([mmproj] if mmproj else [])]:
        relative = path.resolve().relative_to(root.resolve() / 'models' / repo)
        if downloader.path(repo, str(relative)) != path.resolve() or require_files and not path.is_file():
            raise ValueError('Downloaded file missing or outside model directory')
    cfg = load_config(data_root=root)
    preset_path = root / cfg.models.servers['llamacpp'].preset if 'llamacpp' in cfg.models.servers else (
        root / 'llamacpp-models.ini')
    if preset_path.resolve() != root.resolve() / 'llamacpp-models.ini':
        raise ValueError('Local downloads require the data-directory llamacpp-models.ini preset')
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(preset_path)
    parser[name] = {'model': str(model.resolve()), 'ctx-size': str(context_tokens), 'n-gpu-layers': '99'}
    if mmproj:
        parser[name].update({'mmproj': str(mmproj.resolve()), 'ubatch-size': '1152'})
    stream = io.StringIO()
    parser.write(stream)
    profile = {'provider': 'openai_compatible', 'model': name, 'server': 'llamacpp', 'vision': bool(mmproj),
               'context_tokens': context_tokens, 'remote': False, 'reasoning_param': 'chat_template_kwargs'}
    patch: dict[str, Any] = {'models': {'profiles': {name: profile},
                                      'tasks': {task: {'profile': name} for task in tasks or []}}}
    if 'llamacpp' not in cfg.models.servers:
        patch['models']['servers'] = {'llamacpp': {'managed': True}}
    # Release setup ownership for explicitly assigned profiles/tasks.
    owned = cfg.models.setup_owned.model_dump()
    owned['profiles'] = [n for n in owned['profiles'] if n != name]
    owned['tasks'] = [t for t in owned['tasks'] if t not in (tasks or [])]
    if owned.get('server'):
        owned['server'] = None  # downloaded presets must survive subsequent tier changes
    patch['models']['setup_owned'] = owned
    change = config_change(root, patch, preset=stream.getvalue(), remove=[('models', 'profiles', name)])
    server = cfg.models.servers.get('llamacpp')
    if server and server.managed:
        change.stop_server = server
    return change


def ollama_change(root: Path, model: str, *, tasks: list[str] | None = None) -> ConfigChange:
    validate_ollama(model)
    name = 'ollama_' + re.sub('[^a-zA-Z0-9_-]', '_', model)
    return api_model_change(root, {'models': {'profiles': {name: {'provider': 'ollama', 'model': model,
        'remote': False}}, 'tasks': {t: {'profile': name} for t in tasks or []}}})


def validate_ollama(name: str) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._:/-]{0,199}', name) or '..' in name or '://' in name:
        raise ValueError('Invalid Ollama model name')


def ollama_pull(name: str, *, base_url: str = 'http://localhost:11434', opener: Callable[..., Any] = open_url,
                progress: Callable[[dict[str, Any]], None] = lambda p: None) -> str:
    validate_ollama(name)
    parsed = urlsplit(base_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Ollama pulls require a loopback server')
    request = urllib.request.Request(base_url.rstrip('/') + '/api/pull', headers={'Content-Type': 'application/json'},
                                     data=json.dumps({'name': name, 'stream': True}).encode())
    complete = False
    try:
        check_cancelled()
        with opener(request, timeout=900) as response:
            for line in response:
                check_cancelled()
                item = json.loads(line)
                if not isinstance(item, dict) or item.get('error'):
                    raise ValueError('Ollama pull failed')
                progress({'status': str(item.get('status', '')), 'total': max(0, int(item.get('total', 0))),
                          'completed': max(0, int(item.get('completed', 0)))})
                complete = complete or item.get('status') == 'success'
    except Exception:
        raise ValueError('Ollama pull interrupted; retry to resume') from None
    if not complete:
        raise ValueError('Ollama pull did not complete; retry to resume')
    return name
