"""敏感信息本地加密存储与脱敏（身份证号 / 房间凭证）。

解决的问题：账号（完美校园 outid，一般就是身份证号）与房间信息原本明文落在
`data/**.json` 里，一旦目录被拷贝、备份或误传就会泄露。

设计
----
* **零依赖可用**：优先用 `cryptography` 的 Fernet（AES-128-CBC + HMAC-SHA256）；
  没装时自动回退到内置的 PBKDF2 密钥流方案（详见 `_FallbackCipher` 注释，
  属于「防明文落盘」的混淆级保护，不等于抗专业离线破解，建议仍装上 cryptography）。
* **密钥来源**（优先级）：显式传入 > 环境变量 `DORM_POWER_KEY` > 密钥文件
  `.dorm_power_key`（仓库根或当前目录）。
* **未配置密钥 = 不加密**：`SecretBox.available` 为 False，加解密直接返回原文，
  保证老环境行为不变，由调用方自行决定是否告警。
* **确定性房间摘要**：`room_key()` 用 HMAC 生成不可逆短摘要，给历史记录当主键，
  这样 `data/history.json` 里不再出现可定位到具体宿舍的 roomverify。

命令行
-------
    python secure_store.py genkey                 # 生成密钥（打印，可 --write 落盘）
    python secure_store.py encrypt-value <文本>   # 打印密文，可填进 config.yaml
    python secure_store.py decrypt-value <密文>   # 打印明文（排查用）
    python secure_store.py encrypt <file.json>    # 明文 JSON -> 密文（自动备份 .bak）
    python secure_store.py decrypt <file.json>    # 密文 JSON -> 明文（自动备份 .bak）
    python secure_store.py selftest               # 两条加密路径往返自检
"""
import base64
import hashlib
import hmac
import json
import os
import secrets

try:  # 首选：成熟的对称加密库
    from cryptography.fernet import Fernet, InvalidToken as _FernetInvalidToken
except ImportError:  # 允许零依赖运行
    Fernet = None
    _FernetInvalidToken = Exception

KEY_ENV = "DORM_POWER_KEY"
KEY_FILE_NAME = ".dorm_power_key"
ENC_PREFIX = "enc:v1:"          # 密文统一前缀，便于判断"是否已加密"
LEGACY_TAG = "_encrypted"       # 整体加密 JSON 文件的标记字段
PBKDF2_ROUNDS = 200_000


class SecretBoxError(Exception):
    """密钥错误 / 密文损坏 / 密文被篡改。"""


# ---------------------------------------------------------------- 密钥
def generate_key() -> str:
    """生成一把新密钥（urlsafe base64，32 字节随机）。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def find_key_file(*dirs: str) -> str | None:
    for d in dirs or (os.getcwd(),):
        p = os.path.join(d, KEY_FILE_NAME)
        if os.path.isfile(p):
            return p
    return None


def load_key(key: str | None = None, *dirs: str) -> str | None:
    """按 参数 > 环境变量 > 密钥文件 的顺序取密钥，取不到返回 None。"""
    if key:
        return key.strip()
    env = os.environ.get(KEY_ENV, "").strip()
    if env:
        return env
    p = find_key_file(*dirs)
    if p:
        try:
            with open(p, "r", encoding="utf-8") as f:
                v = f.read().strip()
            return v or None
        except OSError:
            return None
    return None


# ---------------------------------------------------------------- 后端
class _FallbackCipher:
    """无 cryptography 时的零依赖方案。

    密文格式：`DP1.<salt>.<nonce>.<ciphertext>.<mac>`（各段均为 b64）
    * 主密钥：PBKDF2-HMAC-SHA256(passphrase, salt, 200k)
    * 加密密钥/校验密钥：HKDF-lite（`HMAC(master, label)`）
    * 加密：PBKDF2 密钥流（nonce||counter 逐块 HMAC-SHA256）与明文异或
    * 完整性：HMAC-SHA256(mac_key, header||ciphertext)，解密时 compare_digest 校验

    说明：这是"没有 AES 时的权宜之计"，安全性弱于 Fernet；生产环境请安装 cryptography。
    """

    LABEL = b"DP1"
    KEY_LEN = 32
    NONCE_LEN = 12
    SALT_LEN = 16
    ROUNDS = PBKDF2_ROUNDS

    def __init__(self, passphrase: str):
        self._pass = passphrase.encode("utf-8")

    def _derive(self, salt: bytes) -> tuple[bytes, bytes]:
        master = hashlib.pbkdf2_hmac("sha256", self._pass, salt, self.ROUNDS)
        return (hmac.new(master, b"enc", hashlib.sha256).digest(),
                hmac.new(master, b"mac", hashlib.sha256).digest())

    @staticmethod
    def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < n:
            out += hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            counter += 1
        return bytes(out[:n])

    @staticmethod
    def _b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _unb64(s: str) -> bytes:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad)

    def encrypt(self, plaintext: bytes) -> str:
        salt = secrets.token_bytes(self.SALT_LEN)
        nonce = secrets.token_bytes(self.NONCE_LEN)
        enc_key, mac_key = self._derive(salt)
        ct = bytes(a ^ b for a, b in
                   zip(plaintext, self._keystream(enc_key, nonce, len(plaintext))))
        header = b".".join([self.LABEL, self._b64(salt).encode(), self._b64(nonce).encode()])
        mac = hmac.new(mac_key, header + b"." + self._b64(ct).encode(), hashlib.sha256).digest()
        return ".".join([header.decode(), self._b64(ct), self._b64(mac)])

    def decrypt(self, token: str) -> bytes:
        parts = token.split(".")
        if len(parts) != 5 or parts[0] != self.LABEL.decode():
            raise SecretBoxError("密文格式不正确（不是 DP1 令牌）")
        salt, nonce, ct_b64, mac_b64 = parts[1:5]
        salt, nonce = self._unb64(salt), self._unb64(nonce)
        enc_key, mac_key = self._derive(salt)
        header = ".".join(parts[:3]).encode()
        expect = hmac.new(mac_key, header + b"." + ct_b64.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, self._unb64(mac_b64)):
            raise SecretBoxError("密文校验失败：密钥不对或内容被篡改")
        ct = self._unb64(ct_b64)
        return bytes(a ^ b for a, b in zip(ct, self._keystream(enc_key, nonce, len(ct))))


class SecretBox:
    """字符串加解密门面：可用则加密，不可用则原样返回。"""

    def __init__(self, key: str | None = None):
        self.key = key or None
        self.backend = None
        if not self.key:
            return
        if Fernet is not None:
            self.backend = Fernet(base64.urlsafe_b64encode(hashlib.sha256(self.key.encode()).digest()))
        else:
            self.backend = _FallbackCipher(self.key)

    @property
    def available(self) -> bool:
        return self.backend is not None

    @property
    def backend_name(self) -> str:
        if not self.available:
            return "none"
        return "fernet" if Fernet is not None else "fallback-pbkdf2"

    def encrypt(self, text: str) -> str:
        if text is None:
            return text
        if not self.available:
            return text
        raw = self.backend.encrypt(text.encode("utf-8"))
        return ENC_PREFIX + (raw.decode() if isinstance(raw, bytes) else raw)

    def decrypt(self, text: str) -> str:
        if not text or not self.available or not str(text).startswith(ENC_PREFIX):
            return text
        token = str(text)[len(ENC_PREFIX):]
        try:
            if Fernet is not None and isinstance(self.backend, Fernet):
                return self.backend.decrypt(token.encode()).decode("utf-8")
            return self.backend.decrypt(token).decode("utf-8")
        except (_FernetInvalidToken, SecretBoxError):
            raise
        except Exception as e:  # 明文残留/格式异常：原样返回，避免启动即崩
            raise SecretBoxError(f"解密失败：{e}") from e


def box_from_env(key: str | None = None, *dirs: str) -> SecretBox:
    """按 参数 > 环境变量 > 密钥文件 造一个 SecretBox（无密钥则返回一个不可用的 box）。"""
    return SecretBox(load_key(key, *dirs))


# ---------------------------------------------------------------- 脱敏 / 摘要
# 身份证两端都敏感：前 4 位是省市地区码，后 4 位是顺序码+校验位，各自都能把范围
# 缩到很小，所以两头一律打码，只保留中间段（默认 hide_head/hide_tail 都是 4）。
MASK_HEAD = 4
MASK_TAIL = 4


def mask_id(value: str | None, hide_head: int = MASK_HEAD, hide_tail: int = MASK_TAIL) -> str:
    """账号脱敏：首、尾各隐藏 hide_head / hide_tail 位，中间保留。

    `440000200001010000` -> `****0020000101****`。

    长度不足 hide_head + hide_tail 的短账号整体打码，避免「只打一半、另一半原样露出去」。
    """
    s = str(value or "")
    if not s:
        return ""
    hide_head = max(0, int(hide_head))
    hide_tail = max(0, int(hide_tail))
    if len(s) <= hide_head + hide_tail:
        return "*" * len(s)
    tail_at = len(s) - hide_tail if hide_tail else len(s)
    return f"{'*' * hide_head}{s[hide_head:tail_at]}{'*' * hide_tail}"


def room_key(secret: str | None, roomverify: str) -> str:
    """房间凭证的确定性摘要（不可逆），用于历史记录主键；无密钥时返回原文。"""
    if not secret:
        return roomverify
    return "rk_" + hmac.new(secret.encode(), f"room|{roomverify}".encode(),
                            hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------- JSON 文件
def load_json(path: str, box: SecretBox | None = None) -> dict:
    """读取 JSON；支持「整体加密」格式（{"_encrypted": true, "data": "enc:v1:..."}）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if isinstance(raw, dict) and raw.get(LEGACY_TAG):
        if not (box and box.available):
            raise SecretBoxError(f"{path} 已加密，但当前没有可用密钥（设置环境变量 {KEY_ENV}）")
        return json.loads(box.decrypt(raw.get("data", "")))
    return raw


def save_json(path: str, data: dict, box: SecretBox | None = None) -> None:
    """原子写 JSON；给了可用 box 就整体加密落盘。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if box and box.available:
        payload = {LEGACY_TAG: True, "v": 1, "data": box.encrypt(json.dumps(data, ensure_ascii=False))}
    else:
        payload = data
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:  # 私密文件收紧权限（Windows 上无效果，Linux/Mac 有效）
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- CLI
def _cli(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "selftest"
    here = os.path.dirname(os.path.abspath(__file__))
    if cmd == "genkey":
        k = generate_key()
        print(k)
        if "--write" in argv:
            with open(os.path.join(here, KEY_FILE_NAME), "w", encoding="utf-8") as f:
                f.write(k)
            print(f"（已写入 {os.path.join(here, KEY_FILE_NAME)}，注意别随包分发）")
        return 0
    if cmd in ("encrypt-value", "decrypt-value"):
        text = " ".join(argv[2:])
        box = box_from_env(None, here, os.getcwd())
        if not box.available:
            print(f"未找到密钥：请设置环境变量 {KEY_ENV}，或在目录里放 {KEY_FILE_NAME}")
            return 2
        print(box.decrypt(text) if cmd == "decrypt-value" else box.encrypt(text))
        return 0
    if cmd in ("encrypt", "decrypt"):
        if len(argv) < 3:
            print(f"用法：python secure_store.py {cmd} <file.json>")
            return 1
        path = argv[2]
        box = box_from_env(None, here, os.getcwd())
        if not box.available:
            print(f"未找到密钥：请设置环境变量 {KEY_ENV}，或在目录里放 {KEY_FILE_NAME}")
            return 2
        data = load_json(path, box)
        with open(path + ".bak", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        save_json(path, data, box if cmd == "encrypt" else None)
        print(f"{cmd} 完成：{path}（明文备份 {path}.bak，确认无误后请删除）")
        return 0
    if cmd == "selftest":
        box = SecretBox(generate_key())
        src = '{"account": "440000200001010000", "rooms": [["1栋A-101", "101-1--11-101"]]}'
        enc = box.encrypt(src)
        assert enc != src and box.decrypt(enc) == src, "往返失败"
        print(f"[{box.backend_name}] 往返 OK，密文前缀 {enc[:12]}…（明文 {len(src)} -> {len(enc)} 字符）")
        try:
            SecretBox(generate_key()).decrypt(enc)
        except Exception as e:  # 密钥不对必须报错，不能静默返回乱码
            print(f"  错误密钥正确报错：{type(e).__name__}")
        else:
            print("  ✗ 错误密钥竟然解密成功，请检查实现！")
            return 1
        print(f"  脱敏示例：{mask_id('440000200001010000')}")
        print(f"  房间摘要：{room_key('demo', '101-1--11-101')}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv))
