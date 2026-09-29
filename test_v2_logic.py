"""本地验证 v2 插件核心逻辑（账号/房间从 config.yaml 读取，不在代码里写死）。

账号走 config_loader（自动解密 enc:v1: 密文 / 展开 ${环境变量}），输出脱敏。
"""
import json

import requests

from config_loader import load_config
from secure_store import mask_id

cfg = load_config()
stu = cfg.get("student") or {}
ACCOUNT = stu.get("account") or ""
GOOD = stu.get("customercode")
BAD = 645  # 已知的其他学校，用于验证搜校探测的假阳性
ROOMS = [(r["name"], r["roomverify"]) for r in stu.get("rooms", [])]
if not ACCOUNT:
    raise SystemExit("config.yaml 里没有账号（account 为空或密文没解出来）")

API_URL = (cfg.get("api") or {}).get(
    "url", "https://xqh5.17wanxiao.com/smartWaterAndElectricityService/SWAEServlet")
COMMAND = (cfg.get("api") or {}).get("command", "JBSWaterElecService")


def ts():
    from datetime import datetime
    n = datetime.now()
    return n.strftime("%Y%m%d%H%M%S") + f"{n.microsecond // 1000:03d}"


def post(code, param):
    payload = {"param": json.dumps(param, ensure_ascii=False), "customercode": code,
               "method": param["cmd"], "command": COMMAND}
    r = requests.post(API_URL, data=payload, timeout=10,
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "User-Agent": "Mozilla/5.0 Wanxiao/5.8.6"})
    outer = r.json()
    if outer.get("result_") != "true":
        raise RuntimeError(outer.get("message_"))
    return json.loads(outer.get("body") or "{}")


def check_school(code):
    try:
        body = post(code, {"cmd": "login", "outid": ACCOUNT, "account": ACCOUNT, "timestamp": ts()})
        return body.get("result") == "0" or bool(body.get("empname"))
    except Exception:
        return False


def fmt(val):
    try:
        return f"{float(val):g}"
    except (TypeError, ValueError):
        return str(val) if val is not None else "-"


def render(detail, name):
    """与插件 _format_room 一致的格式化输出预览（一行一个房间，抗平台吞换行）。"""
    mod = (detail.get("modlist") or [{}])[0]
    parts = [f"🏠{detail.get('roomfullname') or name}", f"剩余{fmt(mod.get('odd'))}度"]
    if mod.get("todayuse") is not None:
        parts.append(f"今日{fmt(mod['todayuse'])}度")
    week = [x for x in (mod.get("weekuselist") or []) if isinstance(x, dict)]
    if week:
        parts.append("近7日 " + " ".join(
            f"{str(x.get('weekday', '')).replace('星期', '')}{fmt(x.get('dayuse', x.get('use')))}" for x in week))
    month = [x for x in (mod.get("monthuselist") or []) if isinstance(x, dict)]
    if month:
        last = month[-1]
        parts.append(f"上月{fmt(last.get('monthuse'))}度")
    if mod.get("sumbuy") is not None:
        parts.append(f"累购{fmt(mod['sumbuy'])}度")
    return "｜".join(parts)


def main():
    ok_good, ok_bad = check_school(GOOD), check_school(BAD)
    print(f"check_school({GOOD}) = {ok_good}  (期望 True)")
    print(f"check_school({BAD})  = {ok_bad}  (期望 False)")
    print(f"  账号 {mask_id(ACCOUNT)}；注意：网络失败也会返回 False，"
          "若两项都是 False 应先确认网络/接口是否可达，别急着改逻辑")
    assert ok_good and not ok_bad, "搜校探测逻辑不符合预期！"

    body = post(GOOD, {"cmd": "getbindroom", "account": ACCOUNT, "timestamp": ts()})
    rooms = [(r.get("roomfullname"), r.get("roomverify")) for r in body.get("roomlist", [])]
    print(f"getbindroom: {len(rooms)} 个房间（roomverify 不打印）")

    print("\n--- 插件新格式预览 ---")
    for name, rv in (rooms or ROOMS):
        d = post(GOOD, {"cmd": "h5_getstuindexpage", "account": ACCOUNT,
                        "roomverify": rv, "timestamp": ts()})
        print(render(d, name))
        print()
    print("v2 核心逻辑验证通过 ✅")


main()
