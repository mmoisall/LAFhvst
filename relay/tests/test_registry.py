"""레지스트리 단위 테스트 — 형식 검증 / 최초 바인딩 / 상수시간 검증 / 원자적 저장."""

from __future__ import annotations

import json

import pytest

from relay.registry import (
    Registry,
    RegistryError,
    is_valid_host_id,
    registry_path,
    token_hash,
)

HOST = "AbCdEfGh12345678"
TOKEN = "agent-token-1"


def test_host_id_format():
    assert is_valid_host_id(HOST)
    assert is_valid_host_id("a" * 16)
    assert is_valid_host_id("A-Z_a-z-0-9______")
    assert not is_valid_host_id("a" * 15)
    assert not is_valid_host_id("short")
    assert not is_valid_host_id("bad!host_id_1234567890")
    assert not is_valid_host_id("한글" * 10)
    assert not is_valid_host_id(None)
    assert not is_valid_host_id(12345678901234567890)


def test_bind_then_verify(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    assert len(reg) == 0
    assert reg.bind_or_verify(HOST, TOKEN) == "bound"
    assert reg.bind_or_verify(HOST, TOKEN) == "ok"
    assert reg.bind_or_verify(HOST, "other-token") == "mismatch"
    assert reg.bind_or_verify(HOST, "") == "mismatch"
    assert reg.bind_or_verify("bad!", TOKEN) == "bad_host_id"
    assert reg.get(HOST) == token_hash(TOKEN)
    assert reg.get("never-bound-host-id") is None
    assert len(reg) == 1


def test_persisted_without_plaintext(tmp_path):
    path = tmp_path / "registry.json"
    reg = Registry(path)
    assert reg.bind_or_verify(HOST, TOKEN) == "bound"

    raw = path.read_text(encoding="utf-8")
    assert TOKEN not in raw
    data = json.loads(raw)
    assert data["hosts"][HOST] == token_hash(TOKEN)

    # 재시작(새 인스턴스) 후에도 선점이 유지된다.
    again = Registry(path)
    assert again.bind_or_verify(HOST, TOKEN) == "ok"
    assert again.bind_or_verify(HOST, "other-token") == "mismatch"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "registry.json"
    reg = Registry(path)
    for index in range(5):
        reg.bind_or_verify(f"{HOST}{index}", TOKEN)
    leftovers = [entry.name for entry in tmp_path.iterdir() if entry.name != path.name]
    assert leftovers == []
    assert json.loads(path.read_text(encoding="utf-8"))["hosts"]


def test_missing_file_is_empty_and_corrupt_file_fails(tmp_path):
    path = tmp_path / "registry.json"
    assert len(Registry(path)) == 0

    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(RegistryError):
        Registry(path)


def test_registry_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LAF_RELAY_REGISTRY", str(tmp_path / "custom.json"))
    assert registry_path() == tmp_path / "custom.json"
    monkeypatch.delenv("LAF_RELAY_REGISTRY")
    assert registry_path().name == "registry.json"
