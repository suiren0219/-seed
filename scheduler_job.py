"""定时任务 + 双预警逻辑（阈值预警 / 掉电速度预警）。

独立运行时用 APScheduler 循环；AstrBot 插件里复用 should_warn / make_report。
"""
import logging

import requests

from config_loader import box_of, rooms_encrypted
from power_service import (ApiConfig, PowerApiError, bind_rooms, extract_detail,
                           extract_odd, pick_room, room_power)
from secure_store import mask_id
from storage import PowerStorage

log = logging.getLogger(__name__)


def api_of(cfg: dict) -> ApiConfig:
    """从配置构造接口参数（接口地址/命令/字段均可配置，上游改版时改配置即可）。"""
    return ApiConfig.from_dict(cfg.get("api"))


def storage_of(cfg: dict) -> PowerStorage:
    """按配置构造存储：房间凭证默认只存 HMAC 摘要。"""
    secret = box_of(cfg).key if rooms_encrypted(cfg) else None
    return PowerStorage(cfg.get("storage", {}).get("path", "data/history.json"), secret=secret)


def should_warn(prev_odd: float | None, cur_odd: float, threshold: float = 20,
                drop_per_hour: float = 5, hours_between: float | None = None) -> str | None:
    """返回预警文案；无需预警返回 None。

    掉电预警按每小时速率判断：hours_between 为距上次记录的小时数；
    未知间隔时退回按单次差值判断（保守）。
    """
    if cur_odd <= threshold:
        return f"⚠️ 剩余 {cur_odd:g} 度，低于 {threshold:g} 度，请及时充值！"
    if prev_odd is not None:
        diff = prev_odd - cur_odd
        rate = diff / hours_between if hours_between and hours_between > 0 else diff
        if rate >= drop_per_hour:
            span = f"（{hours_between:.1f} 小时掉 {diff:.1f} 度）" if hours_between else f"（降 {diff:.1f} 度）"
            return f"📉 掉电异常：{prev_odd:g} → {cur_odd:g} 度{span}"
    return None


def _fmt(val) -> str:
    try:
        return f"{float(val):g}"
    except (TypeError, ValueError):
        return str(val) if val is not None else "-"


def _query_one(cfg: dict, storage: PowerStorage, roomverify: str, name: str,
               api: ApiConfig | None = None) -> list[str]:
    """查询单个房间，返回文案行列表。"""
    stu = cfg["student"]
    api = api or api_of(cfg)

    detail = room_power(stu["account"], roomverify, stu["customercode"], api=api)
    odd, probe = extract_odd(detail, api=api)
    if odd is None:
        msg = detail.get("message") if isinstance(detail, dict) else None
        return [f"❌ {name}: 未能识别剩余电量（{msg or '未知原因'}）"]

    info = extract_detail(detail)
    prev = storage.last_record(roomverify)
    prev_odd = prev["odd"] if prev else None
    storage.record(roomverify, odd, extra={"field": probe["field"]})

    hours_between = None
    if prev and prev.get("time"):
        try:
            from datetime import datetime
            hours_between = (datetime.now() - datetime.fromisoformat(prev["time"])).total_seconds() / 3600
        except (ValueError, TypeError):
            hours_between = None

    device = info.get("devicename") or ""
    lines = [f"🏠 {info.get('roomfullname') or name}" + (f"（{device}）" if device else ""),
             f"⚡ 剩余电量：{_fmt(odd)} 度"]
    if info.get("todayuse") is not None:
        lines.append(f"📈 今日用电：{_fmt(info['todayuse'])} 度")
    week = [d for d in info.get("weekuselist") or [] if isinstance(d, dict)]
    if week:
        seg = " ".join(f"{d.get('weekday', '')}{_fmt(d.get('dayuse', d.get('use')))}" for d in week)
        lines.append(f"📊 近7日：{seg}")
    if prev_odd is not None:
        diff = prev_odd - odd
        if diff > 0:
            lines.append(f"（较上次 {prev_odd:g} 度下降 {diff:.1f} 度）")
        elif diff < 0:
            lines.append(f"（较上次 {prev_odd:g} 度增加 {-diff:.1f} 度，可能是充值到账）")
    # 预警配置：优先读面板的 warn_threshold / warn_drop_per_hour，回退到 config.yaml 的 warn 块
    warn_threshold = cfg.get("warn_threshold")
    warn_drop = cfg.get("warn_drop_per_hour")
    if warn_threshold is None or warn_drop is None:
        warn_cfg = cfg.get("warn", {})
        warn_threshold = warn_threshold or warn_cfg.get("threshold", 20)
        warn_drop = warn_drop or warn_cfg.get("drop_per_hour", 5)
    warn = should_warn(prev_odd, odd, warn_threshold, warn_drop, hours_between=hours_between)
    if warn:
        lines.append(warn)
    return lines


def make_report(cfg: dict, storage: PowerStorage) -> str:
    """拉取所有配置房间并生成播报文案（含预警行）。"""
    stu = cfg["student"]
    api = api_of(cfg)

    targets = [(r.get("name", r["roomverify"]), r["roomverify"])
               for r in stu.get("rooms") or [] if r.get("roomverify")]

    # 兜底：未配置 rooms 时走 getbindroom + 关键词
    if not targets:
        rooms = bind_rooms(stu["account"], stu["customercode"], api=api)
        if not rooms:
            return "❌ 未查询到绑定房间，请检查 account/customercode 或在配置里填 roomverify。"
        room = pick_room(rooms, stu.get("room_keyword", "")) or rooms[0]
        targets = [(room.get("roomfullname", "未知房间"), room["roomverify"])]

    blocks = []
    for name, rv in targets:
        try:
            blocks.append("\n".join(_query_one(cfg, storage, rv, name, api)))
        except (PowerApiError, requests.RequestException) as e:
            log.error("查询 %s（账号 %s）失败: %s", name, mask_id(stu.get("account")), e)
            blocks.append(f"❌ {name}: 查询失败 {e}")
    return "\n\n".join(blocks)


def poll_once(cfg: dict, storage: PowerStorage) -> str:
    """定时轮询入口：记录 + 预警判断，返回需要推送的文案或空串。"""
    try:
        return make_report(cfg, storage)
    except Exception:
        log.exception("未知错误")
        return ""


def run_forever(cfg_path: str = "config.yaml") -> None:
    """独立运行：按配置里的间隔轮询并推送/预警。"""
    import time as _time

    from config_loader import load_config
    from notifier import notify
    from power_service import close_session

    cfg = load_config(cfg_path)
    storage = storage_of(cfg)
    interval = cfg.get("schedule", {}).get("interval_minutes", 60) * 60
    log.info("独立轮询已启动，间隔 %s 秒", interval)
    try:
        while True:
            text = poll_once(cfg, storage)
            if text:
                notify(cfg, "宿舍电量播报", text)
            _time.sleep(interval)
    finally:
        close_session()
