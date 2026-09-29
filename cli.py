"""命令行入口：
  python cli.py probe   —— 只查绑定房间（不记历史）
  python cli.py once    —— 完整查询一次：取电量、记历史、打印播报/预警
  python cli.py watch   —— 独立轮询模式（不装 AstrBot 也能用）
"""
import sys

from config_loader import load_config
from power_service import PowerApiError, bind_rooms, close_session, extract_odd, pick_room, room_power
from scheduler_job import api_of, make_report, poll_once, run_forever, storage_of
from secure_store import mask_id


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    cfg = load_config()
    stu = cfg["student"]
    api = api_of(cfg)

    try:
        if cmd == "probe":
            rooms = bind_rooms(stu["account"], stu["customercode"], api=api)
            print(f"账号 {mask_id(stu['account'])} 绑定房间数: {len(rooms)}")
            show = "--show-secret" in sys.argv   # 默认脱敏，排查时才打印完整 roomverify
            for r in rooms:
                rv = r.get("roomverify") or ""
                # 默认只露房号（roomverify 前段是 zoneid/buildid，属于可定位信息）
                shown = rv if show else "…-" + rv.rsplit("-", 1)[-1]
                print(f"  房间: {r.get('roomfullname')}  verify: {shown}")
                for d in r.get("detaillist") or []:
                    print(f"    详情: {d}")
            if not show:
                print("（roomverify 已脱敏；需要完整值填配置时加 --show-secret）")
            return 0

        if cmd == "room":
            # 强制打印单房完整返回，用于字段探测
            rooms = bind_rooms(stu["account"], stu["customercode"], api=api)
            room = pick_room(rooms, stu.get("room_keyword", ""))
            if not room:
                print("未匹配到房间，先跑 probe 看看")
                return 1
            import json
            detail = room_power(stu["account"], room["roomverify"], stu["customercode"], api=api)
            print(json.dumps(detail, ensure_ascii=False, indent=2))
            odd, probe = extract_odd(detail, api=api)
            print(f"\n识别到剩余电量字段: {probe}")
            return 0

        if cmd == "once":
            print(make_report(cfg, storage_of(cfg)))
            return 0

        if cmd == "watch":
            run_forever()
            return 0

        if cmd == "poll":
            # 单次轮询（只输出预警，无预警静默）
            text = poll_once(cfg, storage_of(cfg))
            if text:
                print(text)
            return 0

        print(__doc__)
        return 1
    except PowerApiError as e:
        print(f"接口错误: {e}")
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        close_session()
