"""secure_store 的补充测试（与 test_security_scan.py 不重叠的部分）：

- 内置 PBKDF2 回退方案的防篡改 / 错误密钥细节
- JSON 整体加密落盘 / 无密钥读取报错
- 密钥来源优先级（显式参数 > 环境变量 > 密钥文件）
"""
import json
import pathlib

import pytest

from secure_store import (KEY_ENV, SecretBox, SecretBoxError, _FallbackCipher,
                          generate_key, load_json, load_key, save_json)


# ---------- 内置 PBKDF2 回退方案 ----------
def test_fallback_cipher_roundtrip():
    c = _FallbackCipher("key-1")
    token = c.encrypt("宿舍电量 66.5".encode("utf-8"))
    assert token != "宿舍电量 66.5"
    assert c.decrypt(token).decode("utf-8") == "宿舍电量 66.5"


def test_fallback_cipher_detects_tamper():
    c = _FallbackCipher("key-1")
    token = c.encrypt(b"secret")
    bad = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    with pytest.raises(SecretBoxError):
        c.decrypt(bad)


def test_fallback_cipher_wrong_key_fails():
    token = _FallbackCipher("key-1").encrypt(b"secret")
    with pytest.raises(SecretBoxError):
        _FallbackCipher("key-2").decrypt(token)


def test_backend_name_is_known():
    assert SecretBox(generate_key()).backend_name in ("fernet", "fallback-pbkdf2")


# ---------- 密钥来源优先级 ----------
def test_load_key_prefers_explicit_arg(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "env-key")
    assert load_key("arg-key") == "arg-key"


def test_load_key_reads_env(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "env-key")
    assert load_key(None) == "env-key"


def test_load_key_reads_file(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)  # 隔离当前目录，避免读到别的密钥文件
    (tmp_path / ".dorm_power_key").write_text("file-key\n", encoding="utf-8")
    assert load_key(None, str(tmp_path)) == "file-key"


# ---------- JSON 整体加密落盘 ----------
def test_save_load_json_encrypted_roundtrip(tmp_path):
    box = SecretBox(generate_key())
    path = str(tmp_path / "users.json")
    data = {"user1": {"account": "440000200001010000", "rooms": [["1栋A-101", "rv"]]}}
    save_json(path, data, box)
    raw_text = pathlib.Path(path).read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert raw.get("_encrypted") is True
    assert "440000200001010000" not in raw_text, "落盘文件里看不到明文账号"
    assert load_json(path, box) == data


def test_load_json_encrypted_without_key_raises(tmp_path):
    path = str(tmp_path / "users.json")
    save_json(path, {"a": 1}, SecretBox(generate_key()))
    with pytest.raises(SecretBoxError):
        load_json(path, SecretBox(None))


def test_save_load_json_plaintext_without_box(tmp_path):
    path = str(tmp_path / "users.json")
    save_json(path, {"a": 1}, None)
    assert load_json(path, None) == {"a": 1}
