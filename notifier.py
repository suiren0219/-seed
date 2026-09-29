"""飞书群机器人推送（自定义机器人 Webhook + 签名校验）。

config.yaml 示例：
notify:
  channel: "feishu"          # feishu / astrbot / console
  feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
  feishu_sign: "xxxx"        # 未开启签名校验则留空
"""
import base64
import hashlib
import hmac
import threading
import time

import requests

_local = threading.local()


def _session() -> requests.Session:
    """复用连接（webhook 每次推都要建 TLS，复用后省一次握手）。"""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        _local.session = s
    return s


def gen_sign(timestamp: int, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def send_feishu(webhook: str, title: str, text: str, secret: str = "") -> None:
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"content": title, "tag": "plain_text"}},
            "elements": [{"tag": "div", "text": {"content": text, "tag": "lark_md"}}],
        },
    }
    if secret:
        ts = int(time.time())
        payload["timestamp"] = ts
        payload["sign"] = gen_sign(ts, secret)
    r = _session().post(webhook, json=payload, timeout=10)
    r.raise_for_status()


def notify(cfg: dict, title: str, text: str) -> None:
    n = cfg.get("notify") or {}
    channel = n.get("channel", "console")
    if channel == "feishu":
        webhook = (n.get("feishu_webhook") or "").strip()
        if not webhook:
            # 配了 feishu 却忘填 webhook：明确报错，别甩 KeyError/requests 的原始异常
            raise ValueError("notify.channel=feishu 但 notify.feishu_webhook 为空，请先填 webhook 地址")
        send_feishu(webhook, title, text, n.get("feishu_sign", ""))
    elif channel == "console":
        print(text)
    # channel == "astrbot" 时由插件自身处理推送，这里不做任何事
