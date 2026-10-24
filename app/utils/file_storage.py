"""Encrypted local file storage for uploaded source documents.

The previous design wrote raw bytes to disk under ``document_storage_path``.
That left all uploaded content readable by anyone with file-system access
on the host, which is a clear violation of multi-tenant data isolation.

This module wraps those writes in Fernet symmetric encryption so the
on-disk files are ciphertext only. The encryption key is loaded from
``settings.document_encryption_key``; production deployments must
configure it explicitly (see :func:`app.main.verify_production_config`).

The layout is unchanged from before — one directory per
(tenant, document). Each file in the directory is the encrypted
payload, so existing code that looks for ``document.source_file_path``
keeps working.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

# A clear marker so a misconfigured deployment shows up immediately in
# logs. We keep a placeholder rather than crash on import so the
# service can start in local development without forcing every
# developer to generate a key.
PLACEHOLDER_KEY_MARKER = "PLACEHOLDER"


def _placeholder_key() -> bytes:
    """Deterministic placeholder derived from app config so the same
    bytes are used across processes in local dev. This is NOT secure
    and the production guard refuses to start with this key.
    """
    seed = f"rag-center-placeholder-{settings.app_env}".encode()
    return base64.urlsafe_b64encode(hashlib.sha256(seed).digest())


def _resolve_key() -> bytes:
    if settings.document_encryption_key:
        try:
            # Fernet accepts a 32-byte url-safe base64 string
            return settings.document_encryption_key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise AppError(
                ErrorCode.SERVER_ERROR,
                msg="document_encryption_key is not ASCII",
                internal_msg=str(exc),
            ) from exc
    # No explicit key — fall back to a placeholder so the service can
    # still start. The production guard in main.py refuses to allow
    # this in production environments.
    if settings.app_env in ("production", "prod"):
        raise AppError(
            ErrorCode.SERVER_ERROR,
            msg="document_encryption_key is required in production",
        )
    logger.warning(
        "DOCUMENT_ENCRYPTION_KEY not set; using placeholder key. "
        "Do NOT run in production without setting DOCUMENT_ENCRYPTION_KEY."
    )
    return _placeholder_key()


def is_placeholder_key() -> bool:
    """True when no real key has been configured and the placeholder
    fallback is in use."""
    if settings.document_encryption_key:
        return False
    return settings.app_env not in ("production", "prod")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class EncryptedFileStorage:
    """Thin wrapper over the legacy on-disk layout that adds Fernet
    encryption on write and decryption on read.

    Layout (unchanged):
        ``{document_storage_path}/{tenant_id}/{document_id}/{filename}``
    """

    def __init__(self, *, base_path: str | None = None, key: bytes | None = None) -> None:
        self.base_path = Path(base_path or settings.document_storage_path)
        self.fernet = Fernet(key or _resolve_key())

    # ----- writes -----

    def save(
        self,
        *,
        tenant_id: str,
        document_id: str,
        filename: str,
        content: bytes,
    ) -> str:
        """Encrypt ``content`` and write it under the document's
        directory. Returns the absolute file path."""
        path = self._path(tenant_id, document_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self.fernet.encrypt(content)
        # Write atomically: write to a sibling temp file, fsync, then
        # rename. This avoids leaving half-written files behind if the
        # process is killed mid-write.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "wb") as fh:
            fh.write(encrypted)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        return str(path)

    # ----- reads -----

    def read(self, path: str) -> bytes:
        """Decrypt the file at ``path`` and return the plaintext bytes."""
        p = Path(path)
        if not p.exists():
            raise AppError(
                ErrorCode.NOT_FOUND,
                msg="Document file is missing on disk",
                context={"path": str(p)},
            )
        encrypted = p.read_bytes()
        try:
            return self.fernet.decrypt(encrypted)
        except InvalidToken as exc:
            logger.error(
                "DOCUMENT_DECRYPT_FAILED | path=%s | error=%s",
                p,
                str(exc),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise AppError(
                ErrorCode.SERVER_ERROR,
                msg="Stored document cannot be decrypted (key changed?)",
                context={"path": str(p)},
            ) from exc

    # ----- deletes -----

    def delete_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
    ) -> int:
        """Delete the entire directory for one document. Returns the
        number of files removed (best-effort)."""
        directory = self._directory(tenant_id, document_id)
        if not directory.exists():
            return 0
        count = sum(1 for _ in directory.rglob("*") if _.is_file())
        shutil.rmtree(directory, ignore_errors=True)
        return count

    def delete_orphans(
        self,
        *,
        known_document_ids: Iterable[str],
        tenant_id: str | None = None,
    ) -> list[str]:
        """Delete document directories that no longer have a row in the
        database. Returns the list of removed document ids."""
        tenant_root = self.base_path if tenant_id is None else self.base_path / tenant_id
        if not tenant_root.exists():
            return []
        removed: list[str] = []
        known = set(known_document_ids)
        if tenant_id is not None:
            candidates = [tenant_root]
        else:
            candidates = [p for p in tenant_root.iterdir() if p.is_dir()]
        for tenant_dir in candidates:
            if not tenant_dir.is_dir():
                continue
            for doc_dir in tenant_dir.iterdir():
                if not doc_dir.is_dir():
                    continue
                if doc_dir.name not in known:
                    shutil.rmtree(doc_dir, ignore_errors=True)
                    removed.append(doc_dir.name)
        return removed

    # ----- retention -----

    def list_files_older_than(self, *, days: int) -> list[tuple[str, str, Path]]:
        """Return (tenant_id, document_id, path) for every file whose
        mtime is older than ``days`` days. Used by the Celery Beat
        retention job."""
        if days <= 0:
            return []
        import time

        cutoff = time.time() - (days * 86400)
        out: list[tuple[str, str, Path]] = []
        if not self.base_path.exists():
            return []
        for tenant_dir in self.base_path.iterdir():
            if not tenant_dir.is_dir():
                continue
            for doc_dir in tenant_dir.iterdir():
                if not doc_dir.is_dir():
                    continue
                for file_path in doc_dir.rglob("*"):
                    if not file_path.is_file():
                        continue
                    try:
                        mtime = file_path.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if mtime < cutoff:
                        out.append((tenant_dir.name, doc_dir.name, file_path))
        return out

    # ----- helpers -----

    def _directory(self, tenant_id: str, document_id: str) -> Path:
        return self.base_path / tenant_id / document_id

    def _path(self, tenant_id: str, document_id: str, filename: str) -> Path:
        # Sanitise the filename so a malicious upload cannot escape the
        # document directory via "..".
        safe = Path(filename).name
        if not safe:
            safe = "document"
        return self._directory(tenant_id, document_id) / safe


@lru_cache
def get_encrypted_file_storage() -> EncryptedFileStorage:
    return EncryptedFileStorage()


__all__ = [
    "PLACEHOLDER_KEY_MARKER",
    "EncryptedFileStorage",
    "get_encrypted_file_storage",
    "is_placeholder_key",
]
