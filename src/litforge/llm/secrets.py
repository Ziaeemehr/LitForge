"""Ephemeral environment/keychain resolution. No secret is persisted by LitForge."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from litforge.config import Config
from litforge.llm.model_setup import PROVIDERS


class SecretError(ValueError):
    pass


class SecretMissing(SecretError):
    pass


class Credentials(Protocol):
    def read(self, name: str) -> str | None: ...
    def write(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


def _run(args: list[str], value: str | None = None) -> str:
    try:
        result = subprocess.run(args, input=value, capture_output=True, text=True, timeout=15, check=False)
    except Exception:
        raise SecretError("keychain error") from None
    if result.returncode:
        # macOS errSecItemNotFound (-25300) is exit 44. Other failures are never absence.
        missing = (args[0] == '/usr/bin/security' and result.returncode == 44 or
                   args[0] != '/usr/bin/security' and result.returncode == 1 and not result.stderr)
        if missing:
            raise SecretMissing("No keychain entry")
        raise SecretError("keychain error")
    return result.stdout.rstrip('\r\n')


class WindowsCredentials:
    """Credential Manager with an injectable ctypes DLL for platform-independent tests."""

    def __init__(self, dll: object | None = None) -> None:
        import ctypes
        from ctypes import wintypes

        class Credential(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                        ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME),
                        ('CredentialBlobSize', wintypes.DWORD), ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
                        ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD),
                        ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
                        ('UserName', wintypes.LPWSTR)]
        self.ctypes, self.structure = ctypes, Credential
        self.dll: Any = dll
        if self.dll is None:
            self.dll = ctypes.WinDLL('Advapi32.dll', use_last_error=True)  # type: ignore[attr-defined]
        self.dll.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        self.dll.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.POINTER(ctypes.POINTER(Credential))]
        self.dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.dll.CredFree.argtypes = [ctypes.c_void_p]
        self.dll.CredFree.restype = None

    def read(self, name: str) -> str | None:
        ptr = self.ctypes.POINTER(self.structure)()
        if not self.dll.CredReadW(name, 1, 0, self.ctypes.byref(ptr)):
            if self.ctypes.get_last_error() == 1168:  # type: ignore[attr-defined]
                return None
            raise SecretError('keychain error')
        try:
            return self.ctypes.string_at(ptr.contents.CredentialBlob,
                                         ptr.contents.CredentialBlobSize).decode('utf-16-le')
        finally:
            self.dll.CredFree(ptr)

    def write(self, name: str, value: str) -> None:
        blob = value.encode('utf-16-le')
        buffer = (self.ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = self.structure(Type=1, TargetName=name, CredentialBlobSize=len(blob), CredentialBlob=buffer,
                              Persist=2, UserName=name)
        if not self.dll.CredWriteW(self.ctypes.byref(cred), 0):
            raise SecretError('System keychain write failed')

    def delete(self, name: str) -> None:
        if not self.dll.CredDeleteW(name, 1, 0):
            if self.ctypes.get_last_error() == 1168:  # type: ignore[attr-defined]
                raise SecretMissing('No keychain entry')
            raise SecretError('keychain error')


class KeyStore:
    def __init__(self, config: Config | None = None, *, system: str | None = None,
                 runner: Callable[[list[str], str | None], str] | None = None,
                 which: Callable[[str], str | None] = shutil.which,
                 environ: Mapping[str, str] | None = None, credentials: Credentials | None = None) -> None:
        self.system, self.runner = system or platform.system(), runner or _run
        self.environ = os.environ if environ is None else environ
        self.tool = which('secret-tool') if self.system == 'Linux' else None
        self.credentials = credentials
        self.names = {entry[1] for entry in PROVIDERS.values()} | {'HF_TOKEN'}
        if config:
            self.names.update(p.api_key_env for p in [config.models.local, *config.models.profiles.values()]
                              if p.api_key_env)

    @property
    def available(self) -> bool:
        return self.system in {'Darwin', 'Windows'} or bool(self.tool)

    def validate(self, name: str) -> None:
        if not isinstance(name, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', name) or name not in self.names:
            raise SecretError('Use a known API key variable name')

    def _windows(self) -> Credentials:
        if self.credentials is None:
            self.credentials = WindowsCredentials()
        return self.credentials

    def resolve(self, name: str | None) -> str | None:
        if not name:
            return None
        if self.environ.get(name):
            return self.environ[name]
        # Existing profiles may use older arbitrary env names; never pass them to system tools.
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', name):
            return None
        try:
            return self._read(name)
        except SecretError:
            return None

    def _read(self, name: str) -> str | None:
        try:
            if self.system == 'Darwin':
                return self.runner(['/usr/bin/security', 'find-generic-password', '-s', 'litforge',
                                    '-a', name, '-w'], None) or None
            if self.system == 'Windows':
                return self._windows().read('litforge:' + name)
            if self.tool:
                return self.runner([self.tool, 'lookup', 'service', 'litforge', 'account', name], None) or None
            raise SecretError('keychain error')
        except SecretMissing:
            return None
        except Exception:
            raise SecretError('keychain error') from None

    def source(self, name: str) -> str:
        if self.environ.get(name):
            return 'env'
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', name):
            return 'missing'
        try:
            return 'keychain' if self._read(name) else 'missing'
        except SecretError:
            return 'keychain error'

    def status(self) -> dict[str, Any]:
        return {'available': self.available, 'keys': [{'name': name, 'source': self.source(name)}
                                                    for name in sorted(self.names)]}

    def set(self, name: Any, value: Any) -> None:
        self.validate(name)
        if not self.available:
            raise SecretError('Set API keys as environment variables; system keychain is unavailable')
        if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise SecretError('Enter a nonempty API key without control characters (maximum 4096 characters)')
        try:
            if self.system == 'Darwin':
                # security's interactive parser uses double quotes and backslash escaping, not a shell.
                quoted = '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
                self.runner(['/usr/bin/security', '-i'],
                            f'add-generic-password -U -s litforge -a {name} -w {quoted}\n')
            elif self.system == 'Windows':
                self._windows().write('litforge:' + name, value)
            elif self.tool:
                self.runner([self.tool, 'store', '--label=LitForge', 'service', 'litforge', 'account', name], value)
            else:
                raise SecretError('Set API keys as environment variables; system keychain is unavailable')
        except Exception:
            raise SecretError('System keychain write failed') from None

    def remove(self, name: Any) -> bool:
        self.validate(name)
        if not self.available:
            raise SecretError('Set API keys as environment variables; system keychain is unavailable')
        try:
            if self.system == 'Darwin':
                self.runner(['/usr/bin/security', 'delete-generic-password', '-s', 'litforge', '-a', name], None)
            elif self.system == 'Windows':
                self._windows().delete('litforge:' + name)
            elif self.tool:
                self.runner([self.tool, 'clear', 'service', 'litforge', 'account', name], None)
            else:
                raise SecretError('Set API keys as environment variables; system keychain is unavailable')
        except SecretMissing:
            return False
        except Exception:
            raise SecretError('System keychain removal failed') from None
        return True
