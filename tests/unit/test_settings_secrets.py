from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from unit.test_models_panel import handler_request

from litforge.config import Config, Paths
from litforge.llm.secrets import KeyStore, SecretError
from litforge.ui.server import App


class Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.value = ""

    def __call__(self, args: list[str], value: str | None = None) -> str:
        self.calls.append((args, value))
        if 'find-generic-password' in args or 'lookup' in args:
            return self.value
        return ''


@pytest.mark.parametrize('system', ['Darwin', 'Linux'])
def test_resolution_write_delete_and_no_secret_argv(system: str) -> None:
    runner = Runner()
    env: dict[str, str] = {}
    keys = KeyStore(system=system, runner=runner, which=lambda _: '/fake/secret-tool', environ=env)
    keys.set('OPENAI_API_KEY', 'SENTINEL "$\\ secret')
    args, stdin = runner.calls[-1]
    assert 'SENTINEL' not in ' '.join(args)
    assert stdin and 'SENTINEL' in stdin
    if system == 'Darwin':
        assert args == ['/usr/bin/security', '-i']
        assert 'add-generic-password' in stdin
    runner.value = 'stored'
    assert keys.resolve('OPENAI_API_KEY') == 'stored'
    assert keys.source('OPENAI_API_KEY') == 'keychain'
    env['OPENAI_API_KEY'] = 'environment'
    count = len(runner.calls)
    assert keys.resolve('OPENAI_API_KEY') == 'environment'
    assert keys.source('OPENAI_API_KEY') == 'env'
    assert len(runner.calls) == count
    keys.remove('OPENAI_API_KEY')
    assert 'delete-generic-password' in runner.calls[-1][0] or 'clear' in runner.calls[-1][0]


@pytest.mark.parametrize('name', ['../KEY', 'a', 'ABC\n', 'UNKNOWN_KEY', 'A' * 65, 'https://bad'])
def test_known_key_validation(name: str) -> None:
    keys = KeyStore(system='Darwin', runner=Runner(), environ={})
    with pytest.raises(SecretError):
        keys.set(name, 'secret')


def test_linux_without_secret_tool() -> None:
    keys = KeyStore(system='Linux', which=lambda _: None, environ={})
    assert not keys.available
    assert keys.source('HF_TOKEN') == 'keychain error'
    with pytest.raises(SecretError, match='environment'):
        keys.set('HF_TOKEN', 'secret')


def test_windows_injected_credentials() -> None:
    class Credentials:
        value: str | None = None
        def read(self, name: str) -> str | None:
            assert name == 'litforge:HF_TOKEN'
            return self.value
        def write(self, name: str, value: str) -> None:
            assert name == 'litforge:HF_TOKEN'
            self.value = value
        def delete(self, name: str) -> None:
            self.value = None
    layer = Credentials()
    keys = KeyStore(system='Windows', credentials=layer, environ={})
    keys.set('HF_TOKEN', 'secret')
    assert keys.resolve('HF_TOKEN') == 'secret'
    keys.remove('HF_TOKEN')
    assert keys.source('HF_TOKEN') == 'missing'


def test_keys_never_persist_or_echo(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    application = App(Paths(tmp_path), Config())
    runner = Runner()
    application.keys = KeyStore(system='Darwin', runner=runner, environ={})
    secret = 'SENTINEL_PRIVATE_KEY_12345'
    code, response = handler_request(application, '/api/keys', {'name': 'OPENAI_API_KEY', 'value': secret})
    assert code == 200
    runner.value = secret
    status, listing = handler_request(application, '/api/keys')
    assert status == 200 and {'name': 'OPENAI_API_KEY', 'source': 'keychain'} in listing['keys']
    assert secret not in json.dumps([response, listing, {k: v.__dict__ for k, v in application.jobs.items()}])
    conn = application.db()
    assert secret not in '\n'.join(conn.iterdump())
    conn.close()
    assert secret not in caplog.text
    assert all(secret.encode() not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())


def test_secret_runner_error_is_sanitized() -> None:
    def fail(args: list[str], value: str | None = None) -> Any:
        raise RuntimeError('SECRET_VALUE')
    keys = KeyStore(system='Darwin', runner=fail, environ={})
    with pytest.raises(SecretError) as error:
        keys.set('HF_TOKEN', 'SECRET_VALUE')
    assert 'SECRET_VALUE' not in str(error.value)


def test_client_resolves_per_call_without_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm import secrets
    from litforge.llm.openai_compatible import OpenAICompatibleClient
    values = iter(['first', 'second'])
    monkeypatch.setattr(secrets.KeyStore, 'resolve', lambda self, name: next(values))
    seen: list[str] = []
    class Transport:
        def request(self, url: str, headers: dict[str, str], body: Any, timeout_s: float) -> Any:
            seen.append(headers['Authorization'])
            return {'choices': [{'message': {'content': '{"ok":true}'}}]}
    client = OpenAICompatibleClient('https://provider.invalid', 'm', 'OPENAI_API_KEY', transport=Transport())
    client.chat_json([{'role': 'user', 'content': 'test'}], {'type': 'object'})
    client.chat_json([{'role': 'user', 'content': 'test'}], {'type': 'object'})
    assert seen == ['Bearer first', 'Bearer second']
    assert 'first' not in repr(client.__dict__) and 'second' not in repr(client.__dict__)


def test_keys_origin_and_delete_without_sockets(tmp_path: Path) -> None:
    import io

    from litforge.ui.server import make_handler
    app = App(Paths(tmp_path), Config())
    runner = Runner()
    app.keys = KeyStore(system='Darwin', runner=runner, environ={})
    for method in ['do_POST', 'do_DELETE']:
        handler = object.__new__(make_handler(app))
        handler.path, handler.headers, handler.wfile = '/api/keys', {}, io.BytesIO()  # type: ignore[assignment]
        result: list[Any] = []
        handler._error = lambda code, message, output=result: output.append(code)  # type: ignore[method-assign]
        handler._same_origin = lambda: False  # type: ignore[method-assign]
        getattr(handler, method)()
        assert result == [403] and not runner.calls
    handler._same_origin = lambda: True  # type: ignore[method-assign]
    handler._body = lambda: {'name': 'HF_TOKEN'}  # type: ignore[method-assign]
    handler._json = lambda body: result.append(body)  # type: ignore[method-assign]
    handler.do_DELETE()
    assert result[-1] == {'ok': True} and 'delete-generic-password' in runner.calls[-1][0]


def test_windows_ctypes_layer_read_write_free_delete() -> None:
    import ctypes

    from litforge.llm.secrets import WindowsCredentials
    class Function:
        def __init__(self, fn: Any) -> None:
            self.fn = fn
        def __call__(self, *args: Any) -> Any:
            return self.fn(*args)
    class DLL:
        def __init__(self) -> None:
            self.blob = b''
            self.freed = False
            self.CredWriteW = Function(self.write)
            self.CredReadW = Function(self.read)
            self.CredDeleteW = Function(lambda *a: True)
            self.CredFree = Function(lambda *a: setattr(self, 'freed', True))
        def write(self, ptr: Any, flags: int) -> bool:
            cred = ptr._obj
            assert cred.TargetName == 'litforge:HF_TOKEN' and cred.Type == 1
            self.blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return True
        def read(self, name: str, kind: int, flags: int, ptr: Any) -> bool:
            assert name == 'litforge:HF_TOKEN' and kind == 1
            self.buffer = (ctypes.c_ubyte * len(self.blob)).from_buffer_copy(self.blob)
            self.credential = layer.structure(CredentialBlobSize=len(self.blob), CredentialBlob=self.buffer)
            ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(layer.structure)))[0] = ctypes.pointer(self.credential)
            return True
    dll = DLL()
    layer = WindowsCredentials(dll)
    layer.write('litforge:HF_TOKEN', 'unicode-κλειδί')
    assert layer.read('litforge:HF_TOKEN') == 'unicode-κλειδί' and dll.freed
    layer.delete('litforge:HF_TOKEN')


def test_cli_key_commands_and_prompt_do_not_echo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from litforge.cli import app
    from litforge.llm import commands
    runner = Runner()
    monkeypatch.setattr(commands, 'KeyStore', lambda *a, **kw: KeyStore(system='Darwin', runner=runner, environ={}))
    cli = CliRunner()
    secret = 'SENTINEL_CLI_KEY'
    result = cli.invoke(app, ['--data-dir', str(tmp_path), 'models', 'key', 'set', 'HF_TOKEN'],
                        input=secret + '\n' + secret + '\n')
    assert result.exit_code == 0, result.output
    assert secret not in result.output
    assert secret not in ' '.join(runner.calls[0][0])
    assert secret in runner.calls[0][1]
    runner.value = secret
    result = cli.invoke(app, ['--data-dir', str(tmp_path), 'models', 'key', 'status'])
    assert result.exit_code == 0 and 'HF_TOKEN: keychain' in result.output and secret not in result.output
    result = cli.invoke(app, ['--data-dir', str(tmp_path), 'models', 'key', 'remove', 'HF_TOKEN'])
    assert result.exit_code == 0


def test_backend_error_message_cannot_echo_the_key() -> None:
    def runner(args: list[str], value: str | None = None) -> str:
        raise SecretError('SENTINEL_BACKEND_KEY')
    keys = KeyStore(system='Darwin', runner=runner, environ={})
    with pytest.raises(SecretError) as error:
        keys.set('HF_TOKEN', 'SENTINEL_BACKEND_KEY')
    assert 'SENTINEL_BACKEND_KEY' not in str(error.value)
