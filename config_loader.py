"""配置加载：环境变量展开 + 敏感项外置。

支持两种写法，避免身份证号明文落在 config.yaml 里：

    student:
      account_env: DORM_POWER_ACCOUNT      # 直接指定环境变量名
      account: "${DORM_POWER_ACCOUNT}"     # 或在任意字符串里写 ${VAR}（自动展开）

`security.key_env` 指定加密密钥所在的环境变量名（默认 DORM_POWER_KEY）；
密钥用于：账号加密落盘（插件）、历史记录里 roomverify 的不可逆摘要。
"""
import os
import re

import yaml

from secure_store import ENC_PREFIX, KEY_ENV, SecretBox, box_from_env, load_key

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def decrypt_values(obj, box: SecretBox | None):
    """递归把配置里 `enc:v1:...` 的值还原成明文（只有拿到密钥才还原）。"""
    if not (box and box.available):
        return obj
    if isinstance(obj, dict):
        return {k: decrypt_values(v, box) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decrypt_values(v, box) for v in obj]
    if isinstance(obj, str) and obj.startswith(ENC_PREFIX):
        return box.decrypt(obj)
    return obj


def expand_env(obj, _missing: list | None = None):
    """递归把字符串里的 `${VAR}` 换成环境变量值；未设置的变量原样保留并记到 _missing。"""
    if isinstance(obj, dict):
        return {k: expand_env(v, _missing) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v, _missing) for v in obj]
    if isinstance(obj, str):
        def sub(m):
            val = os.environ.get(m.group(1))
            if val is None:
                if _missing is not None and m.group(1) not in _missing:
                    _missing.append(m.group(1))
                return m.group(0)
            return val
        return _ENV_REF.sub(sub, obj)
    return obj


def load_config(path: str = "config.yaml") -> dict:
    """读 YAML + 展开环境变量 + 解密 `enc:v1:` 值 + 处理 `student.account_env`。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    missing: list = []
    cfg = expand_env(cfg, missing)
    box = box_of(cfg, os.path.dirname(os.path.abspath(path)))
    cfg = decrypt_values(cfg, box)
    stu = cfg.setdefault("student", {}) or {}
    env_name = stu.pop("account_env", None)
    if env_name and not stu.get("account"):
        stu["account"] = os.environ.get(env_name, "")
    if missing:
        cfg.setdefault("_missing_env", missing)
    return cfg


def secret_of(cfg: dict, *dirs: str) -> str | None:
    """取存储密钥：security.key / security.key_env 指定的环境变量 / 密钥文件。"""
    sec = cfg.get("security") or {}
    if sec.get("key"):
        return str(sec["key"])
    name = sec.get("key_env") or KEY_ENV
    key = load_key(None, *dirs) if name == KEY_ENV else None
    if key:
        return key
    val = os.environ.get(name, "").strip()
    return val or None


def box_of(cfg: dict, *dirs: str):
    """按配置造 SecretBox（无密钥时返回不可用的 box，行为等价于不加密）。"""
    return box_from_env(secret_of(cfg, *dirs), *dirs)


def rooms_encrypted(cfg: dict) -> bool:
    """历史记录里的 roomverify 是否以摘要形式存储（默认开启）。"""
    return bool((cfg.get("storage") or {}).get("encrypt_rooms", True))
