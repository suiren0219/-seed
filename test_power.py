"""在线联调脚本：用 config.yaml 里的真实配置测接口。

账号/凭证走 config_loader 读取（会自动解密 `enc:v1:` 密文、展开 ${环境变量}），
输出一律脱敏；加 `--show-secret` 才打印完整 roomverify（排查用，别贴给别人）。
"""
import json
import sys

from config_loader import load_config
from power_service import bind_rooms, close_session, extract_odd, pick_room, room_power
from scheduler_job import api_of
from secure_store import mask_id

SHOW_SECRET = "--show-secret" in sys.argv
cfg = load_config()
stu = cfg.get("student") or {}
api = api_of(cfg)

if not stu.get("account"):
    raise SystemExit("config.yaml 里没有账号（account 为空、${环境变量} 未设置或密文没解出来）")

print(f"账号={mask_id(stu['account'])}  customercode={stu.get('customercode')}  "
      f"房间关键词={stu.get('room_keyword') or '（无）'}")

try:
    print("\n--- 1. getbindroom ---")
    rooms = bind_rooms(stu["account"], stu["customercode"], api=api)
    for r in rooms:
        rv = r.get("roomverify") or ""
        # 默认只露房号（roomverify 前段是 zoneid/buildid，属于可定位信息）
        shown = rv if SHOW_SECRET else "…-" + rv.rsplit("-", 1)[-1]
        print(f"房间: {r.get('roomfullname')}  verify: {shown}")
        print(f"  detaillist: {r.get('detaillist')}")

    if rooms:
        print("\n--- 2. h5_getstuindexpage（匹配房间/第一个）---")
        room = pick_room(rooms, stu.get("room_keyword", "")) or rooms[0]
        detail = room_power(stu["account"], room["roomverify"], stu["customercode"], api=api)
        print(json.dumps(detail, ensure_ascii=False, indent=2))
        odd, probe = extract_odd(detail)
        print(f"\n识别剩余电量: {odd} (字段: {probe['field']})")
finally:
    close_session()
