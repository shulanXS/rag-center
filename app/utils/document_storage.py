"""Compatibility shim that routes document file IO through the
encrypted file storage layer.

The previous implementation wrote plaintext bytes to disk. We now use
Fernet encryption (see :mod:`app.utils.file_storage`) so that files
are ciphertext on disk. The interface here is unchanged so the rest of
the codebase can keep calling ``save_document_file`` /
``read_document_file``.
"""

from pathlib import Path

from app.core.config import PROJECT_ROOT, settings
from app.utils.file_storage import get_encrypted_file_storage


def get_storage_root() -> Path:
    root = Path(settings.document_storage_path)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_document_file(
    *,
    tenant_id: str,
    document_id: str,
    filename: str,
    file_bytes: bytes,
) -> str:
    """Persist an uploaded file. The bytes are encrypted at rest via
    Fernet. Returns the relative path stored in the document row."""
    storage = get_encrypted_file_storage()
    path = storage.save(
        tenant_id=tenant_id,
        document_id=document_id,
        filename=filename,
        content=file_bytes,
    )
    # Store the relative path so deployments with different absolute
    # roots stay portable, matching the legacy contract. When the
    # storage root lives outside PROJECT_ROOT (e.g. some test
    # environments) we keep the absolute path so reads still work.
    try:
        return str(Path(path).relative_to(PROJECT_ROOT))
    except ValueError:
        return path


def resolve_storage_path(relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def read_document_file(relative_path: str) -> bytes:
    """Read a previously-stored document file. Decrypts on the fly."""
    storage = get_encrypted_file_storage()
    abs_path = resolve_storage_path(relative_path)
    if not abs_path.is_file():
        raise FileNotFoundError(f"document file not found: {relative_path}")
    try:
        return storage.read(str(abs_path))
    except Exception as exc:
        # Re-raise as FileNotFoundError so the ingestion layer's
        # existing error path keeps working.
        if "not found" in str(exc).lower() or "missing" in str(exc).lower():
            raise FileNotFoundError(str(exc)) from exc
        raise


def delete_document_files(tenant_id: str, document_id: str) -> int:
    """Remove the encrypted file directory for a document. Returns the
    number of files removed."""
    return get_encrypted_file_storage().delete_document(
        tenant_id=tenant_id,
        document_id=document_id,
    )
