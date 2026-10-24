"""Tests for production config validation and encrypted file storage."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.utils.file_storage import (
    EncryptedFileStorage,
    PLACEHOLDER_KEY_MARKER,
    get_encrypted_file_storage,
    is_placeholder_key,
)


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    # Reset the lru_cache on the factory so a fresh storage is built
    # against the tmp path.
    get_encrypted_file_storage.cache_clear()
    yield tmp_path
    get_encrypted_file_storage.cache_clear()


@pytest.fixture
def real_key():
    return Fernet.generate_key()


def test_encrypted_file_storage_round_trip(tmp_storage, real_key):
    storage = EncryptedFileStorage(base_path=str(tmp_storage), key=real_key)
    path = storage.save(
        tenant_id="t1",
        document_id="d1",
        filename="report.pdf",
        content=b"hello world",
    )
    # The on-disk file is ciphertext, not plaintext
    on_disk = Path(path).read_bytes()
    assert b"hello world" not in on_disk
    # The read path decrypts and returns the original bytes
    assert storage.read(path) == b"hello world"


def test_encrypted_file_storage_delete_document(tmp_storage, real_key):
    storage = EncryptedFileStorage(base_path=str(tmp_storage), key=real_key)
    path = storage.save(
        tenant_id="t1",
        document_id="d1",
        filename="a.txt",
        content=b"x",
    )
    assert Path(path).exists()
    removed = storage.delete_document(tenant_id="t1", document_id="d1")
    assert removed == 1
    assert not Path(path).exists()


def test_encrypted_file_storage_rejects_wrong_key(tmp_storage, real_key):
    storage_a = EncryptedFileStorage(base_path=str(tmp_storage), key=real_key)
    path = storage_a.save(
        tenant_id="t1", document_id="d1", filename="a.txt", content=b"x"
    )
    # Reading with a different key must surface an error, not return
    # garbage plaintext.
    other_key = Fernet.generate_key()
    storage_b = EncryptedFileStorage(base_path=str(tmp_storage), key=other_key)
    with pytest.raises(Exception):
        storage_b.read(path)


def test_encrypted_file_storage_filename_traversal_blocked(tmp_storage, real_key):
    storage = EncryptedFileStorage(base_path=str(tmp_storage), key=real_key)
    path = storage.save(
        tenant_id="t1",
        document_id="d1",
        filename="../../etc/passwd",
        content=b"x",
    )
    # The file lands inside the document directory — never escapes
    assert str(tmp_storage) in path
    assert "/etc/passwd" not in path


def test_save_document_file_writes_encrypted_and_returns_relative_path(
    tmp_storage, real_key, monkeypatch
):
    from app.utils import document_storage

    # Force the module-level cache to use our test key
    monkeypatch.setattr(settings, "document_encryption_key", real_key.decode())
    document_storage.get_encrypted_file_storage.cache_clear()

    rel = document_storage.save_document_file(
        tenant_id="t1",
        document_id="d1",
        filename="x.bin",
        file_bytes=b"secret data",
    )
    # The on-disk file in tmp_storage is ciphertext, not plaintext
    on_disk = (Path(tmp_storage) / "t1" / "d1" / "x.bin").read_bytes()
    assert b"secret data" not in on_disk
    # read_document_file decrypts back to the original bytes regardless
    # of whether the stored path is relative or absolute.
    full = str((Path(tmp_storage) / rel).resolve())
    assert document_storage.read_document_file(full) == b"secret data"

    document_storage.get_encrypted_file_storage.cache_clear()


def test_is_placeholder_key_when_unset(monkeypatch):
    """Local dev with no key configured reports placeholder usage so
    operators can spot the misconfiguration in logs."""
    monkeypatch.setattr(settings, "document_encryption_key", "")
    monkeypatch.setattr(settings, "app_env", "local")
    assert is_placeholder_key() is True

    monkeypatch.setattr(settings, "document_encryption_key", Fernet.generate_key().decode())
    assert is_placeholder_key() is False


def test_production_config_validation_blocks_missing_auth(monkeypatch):
    from app.main import verify_production_config

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "model_api_key", "sk-real")
    monkeypatch.setattr(settings, "llm_api_key", "sk-real")
    monkeypatch.setattr(settings, "document_encryption_key", "k")

    with pytest.raises(RuntimeError) as exc:
        verify_production_config()
    assert "auth_enabled" in str(exc.value)


def test_production_config_validation_blocks_placeholder_keys(monkeypatch):
    from app.main import verify_production_config

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "model_api_key", "your-deepseek-api-key")
    monkeypatch.setattr(settings, "llm_api_key", "sk-real")
    monkeypatch.setattr(settings, "document_encryption_key", "k")

    with pytest.raises(RuntimeError) as exc:
        verify_production_config()
    assert "model_api_key" in str(exc.value)


def test_production_config_validation_blocks_missing_encryption_key(monkeypatch):
    from app.main import verify_production_config

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "model_api_key", "sk-real")
    monkeypatch.setattr(settings, "llm_api_key", "sk-real")
    monkeypatch.setattr(settings, "document_encryption_key", "")

    with pytest.raises(RuntimeError) as exc:
        verify_production_config()
    assert "document_encryption_key" in str(exc.value)


def test_production_config_validation_passes_with_full_config(monkeypatch):
    from app.main import verify_production_config

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "model_api_key", "sk-real")
    monkeypatch.setattr(settings, "llm_api_key", "sk-real")
    monkeypatch.setattr(settings, "document_encryption_key", "k")

    verify_production_config()  # no raise


def test_production_config_validation_skipped_in_local(monkeypatch):
    from app.main import verify_production_config

    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "model_api_key", "your-")
    monkeypatch.setattr(settings, "llm_api_key", "your-")
    monkeypatch.setattr(settings, "document_encryption_key", "")

    verify_production_config()  # no raise
