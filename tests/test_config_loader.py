"""config_loader 的行为测试：${ENV} 展开、enc:v1 解密、account_env 外置与密钥选择。"""
import textwrap

from config_loader import (box_of, decrypt_values, expand_env, load_config,
                           secret_of)
from secure_store import KEY_ENV, SecretBox, generate_key


# ---------- 环境变量展开 ----------
def test_expand_env_replaces_and_reports_missing(monkeypatch):
    monkeypatch.setenv("DP_TEST_VAR", "123456")
    missing = []
    out = expand_env({"a": "${DP_TEST_VAR}", "b": "${DP_TEST_MISSING_X}"}, missing)
    assert out == {"a": "123456", "b": "${DP_TEST_MISSING_X}"}
    assert missing == ["DP_TEST_MISSING_X"]


def test_expand_env_handles_nested_structures(monkeypatch):
    monkeypatch.setenv("DP_TEST_VAR", "v")
    out = expand_env({"l": ["${DP_TEST_VAR}", "plain"], "d": {"k": "${DP_TEST_VAR}"}})
    assert out == {"l": ["v", "plain"], "d": {"k": "v"}}


# ---------- enc:v1 解密 ----------
def test_decrypt_values_roundtrip():
    box = SecretBox(generate_key())
    cfg = {"student": {"account": box.encrypt("440000200001010000")}, "n": 5}
    out = decrypt_values(cfg, box)
    assert out["student"]["account"] == "440000200001010000"
    assert out["n"] == 5


def test_decrypt_values_without_box_keeps_ciphertext():
    cfg = {"student": {"account": "enc:v1:xyz"}}
    assert decrypt_values(cfg, None)["student"]["account"] == "enc:v1:xyz"
    assert decrypt_values(dict(cfg), SecretBox(None))["student"]["account"] == "enc:v1:xyz"


# ---------- load_config 全流程 ----------
def test_load_config_missing_file_returns_empty(tmp_path):
    assert load_config(str(tmp_path / "nope.yaml")) == {}


def test_load_config_account_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DP_TEST_ACCOUNT", "440000200001010000")
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""\
        student:
          account_env: DP_TEST_ACCOUNT
        warn_threshold: 20
        """), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg["student"]["account"] == "440000200001010000"
    assert "account_env" not in cfg["student"]
    assert cfg["warn_threshold"] == 20


def test_load_config_expands_env_and_decrypts(tmp_path, monkeypatch):
    key = generate_key()
    enc = SecretBox(key).encrypt("440000200001010000")
    monkeypatch.setenv("DP_TEST_KEY", key)
    monkeypatch.setenv("DP_TEST_CODE", "1000123")
    p = tmp_path / "config.yaml"
    p.write_text('security:\n  key_env: DP_TEST_KEY\n'
                 f'student:\n  account: "{enc}"\n  customercode: ${{DP_TEST_CODE}}\n',
                 encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg["student"]["account"] == "440000200001010000"
    assert cfg["student"]["customercode"] == "1000123"


# ---------- 密钥选择优先级 ----------
def test_secret_of_prefers_inline_key():
    assert secret_of({"security": {"key": "inline-key"}}) == "inline-key"


def test_secret_of_reads_named_env(monkeypatch):
    monkeypatch.setenv("DP_TEST_KEY2", "envkey")
    assert secret_of({"security": {"key_env": "DP_TEST_KEY2"}}) == "envkey"


def test_secret_of_none_without_any_source(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)  # 隔离：不读环境变量、不读当前目录密钥文件
    assert secret_of({}, str(tmp_path)) is None


def test_box_of_unavailable_when_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert box_of({}, str(tmp_path)).available is False
