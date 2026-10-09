"""Private file storage for uploaded books.

Files live under STORAGE_DIR, which is never inside the web root and is never
served directly: every read goes through an authorised API endpoint. Keys are
random (`ab/<32 hex>`), validated by a strict pattern, so a key can never be
used for path traversal, and existing keys are never overwritten.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path

from .config import settings

KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{32}$")


class StorageError(Exception):
    pass


class LocalStorage:
    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or settings.storage_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        if not KEY_RE.match(key or ""):
            raise StorageError("invalid storage key")
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise StorageError("invalid storage key")
        return p

    def put(self, data: bytes) -> str:
        for _ in range(5):
            h = secrets.token_hex(16)
            key = f"{h[:2]}/{h}"
            p = self._path(key)
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            return key
        raise StorageError("could not allocate a storage key")

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.exists():
            raise StorageError("file missing from storage")
        return p.read_bytes()

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).exists()
        except StorageError:
            return False

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_store: LocalStorage | None = None


def get_storage() -> LocalStorage:
    global _store
    if _store is None:
        _store = LocalStorage()
    return _store


def set_storage(s: LocalStorage) -> None:  # tests
    global _store
    _store = s
