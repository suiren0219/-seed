"""历史电量存储：记录每次查询的剩余电量，用于计算掉电速度与预警。

隐私加固：roomverify 可定位到具体学校+宿舍，因此默认只存它的 HMAC 摘要
（`secure_store.room_key`），需要密钥；没有密钥时退化为存原文（行为与旧版一致）。
摘要是确定性的，所以查询/统计不受影响，历史文件里也不再有明文房间凭证。
"""
import json
import os
import threading
from datetime import datetime

from secure_store import room_key


class PowerStorage:
    def __init__(self, path: str = "data/history.json", secret: str | None = None):
        self.path = path
        self.secret = secret
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) and "records" in data else {"records": []}
        except (FileNotFoundError, json.JSONDecodeError):
            return {"records": []}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:  # 私密数据收紧权限（Windows 无效果）
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _key(self, roomverify: str) -> str:
        """房间主键：有密钥就是不可逆摘要，无密钥则原文（兼容既有明文数据）。"""
        return room_key(self.secret, roomverify)

    def _match(self, rec: dict, roomverify: str) -> bool:
        return rec.get("room") in (self._key(roomverify), roomverify)

    def record(self, roomverify: str, odd: float, extra: dict | None = None) -> None:
        """追加一条记录：{"time": iso, "room": <摘要>, "odd": ...}"""
        with self._lock:
            self.data["records"].append({
                "time": datetime.now().isoformat(timespec="seconds"),
                "room": self._key(roomverify),
                "odd": odd,
                **(extra or {}),
            })
            # 只保留最近 2000 条，防止无限膨胀
            if len(self.data["records"]) > 2000:
                self.data["records"] = self.data["records"][-2000:]
            self._save()

    def last_record(self, roomverify: str) -> dict | None:
        """返回该房间最后一条记录；没有任何记录时返回 None。

        调用方（scheduler_job / 插件）都是「先取上一次、再 record 本次」，
        所以这里必须是真正的最后一条——旧实现用 `records[:-1]` 假设最后一条永远是
        本次记录，一旦调用顺序变化（或同一房间连着查两次）就会漏掉最新一条，
        把更早的记录当成「上次」，从而算出虚高的掉电速度。
        """
        with self._lock:
            for rec in reversed(self.data["records"]):
                if self._match(rec, roomverify):
                    return rec
        return None

    def drop_rate(self, roomverify: str, hours: float = 24) -> float | None:
        """计算近 N 小时平均每小时掉电速度（度/小时），数据不足返回 None。"""
        with self._lock:
            recs = [r for r in self.data["records"] if self._match(r, roomverify)]
        if len(recs) < 2:
            return None
        now = datetime.now()
        try:
            recent = [r for r in recs
                      if (now - datetime.fromisoformat(r["time"])).total_seconds() <= hours * 3600]
        except (KeyError, ValueError):
            return None
        if len(recent) < 2:
            return None
        first, last = recent[0], recent[-1]
        span_h = (datetime.fromisoformat(last["time"])
                  - datetime.fromisoformat(first["time"])).total_seconds() / 3600
        if span_h <= 0:
            return None
        return (first["odd"] - last["odd"]) / span_h
