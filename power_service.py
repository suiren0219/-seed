"""完美校园水电接口核心：绑定房间查询 + 单房间电量查询。

接口：POST https://xqh5.17wanxiao.com/smartWaterAndElectricityService/SWAEServlet
特点：无需登录态，仅需学号 + customercode。
返回结构：外层 {"code_":0,"result_":"true","body":"<JSON字符串>"}，body 需二次 json.loads。

工程性加固（应对"接口随时可能变 / 高频请求被风控"）：
* 复用线程级 `requests.Session`（连接池 + keep-alive），不再每次新建连接
* 失败按指数退避重试（含 429/5xx），对端限流时自动放慢而不是猛打
* url / command / 各 cmd 名 / 电量字段候选全部可配置：接口小改只改配置不改代码
* 解析不出电量时把「结构指纹」脱敏落盘（data/schema_probe.log），便于快速适配新字段
"""
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter

from secure_store import mask_id

log = logging.getLogger(__name__)

DEFAULT_URL = "https://xqh5.17wanxiao.com/smartWaterAndElectricityService/SWAEServlet"
DEFAULT_COMMAND = "JBSWaterElecService"
DEFAULT_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
}
RETRY_STATUS = (429, 500, 502, 503, 504)
DEFAULT_ODD_FIELDS = ("odd", "surpluselec", "surplusElec", "surplus_elec", "remain", "balance")
SCHEMA_LOG = os.path.join("data", "schema_probe.log")


class PowerApiError(Exception):
    """接口返回异常（result_ 非 true 或 body 解析失败）。"""


class PowerBusinessError(PowerApiError):
    """接口通了，业务上就是这个结果（如"人员信息不存在"）——重试没用，也不是风控。"""


class PowerSchemaError(PowerApiError):
    """接口能连通，但返回结构认不出来了——通常是上游改版或被风控页拦截，需人工适配/等待。"""


@dataclass
class ApiConfig:
    """接口可配置项：上游一旦改字段名/改地址，改配置即可，不必改代码。"""

    url: str = DEFAULT_URL
    command: str = DEFAULT_COMMAND
    timeout: int = 10
    retries: int = 2                      # 除首次外额外重试次数
    backoff: float = 0.6                  # 退避基数（秒），实际 sleep = backoff * 2^n ± 抖动
    cmd_bind: str = "getbindroom"
    cmd_index: str = "h5_getstuindexpage"
    cmd_login: str = "login"
    odd_fields: tuple[str, ...] = DEFAULT_ODD_FIELDS
    odd_paths: tuple[str, ...] = ("modlist.0.odd",)   # 形如 "modlist.0.odd" 的取值路径
    list_fields: tuple[str, ...] = field(
        default=("electricitylist", "eleclist", "detaillist", "modlist", "list"))

    @classmethod
    def from_dict(cls, d: dict | None, base: "ApiConfig | None" = None) -> "ApiConfig":
        d = d or {}
        base = base or cls()
        get = lambda k, dv: d[k] if d.get(k) not in (None, "", []) else dv  # noqa: E731
        odd_fields = get("odd_fields", base.odd_fields)
        odd_paths = get("odd_paths", base.odd_paths)
        list_fields = get("list_fields", base.list_fields)
        return cls(
            url=get("url", base.url),
            command=get("command", base.command),
            timeout=int(get("timeout", base.timeout)),
            retries=int(get("retries", base.retries)),
            backoff=float(get("backoff", base.backoff)),
            cmd_bind=get("cmd_bind", base.cmd_bind),
            cmd_index=get("cmd_index", base.cmd_index),
            cmd_login=get("cmd_login", base.cmd_login),
            odd_fields=tuple(odd_fields),
            odd_paths=tuple(odd_paths),
            list_fields=tuple(list_fields),
        )


def _timestamp() -> str:
    # 抓包确认格式：yyyyMMddHHmmssSSS（毫秒），如 20260913144947818
    # 注意是 %H 不是字面量 H——旧实现写成 "%Y%m%dH%M%S"，会多出一个字符。
    now = datetime.now()
    return now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"


# ------------------------------------------------------------ 连接复用
_local = threading.local()


def _session() -> requests.Session:
    """线程级复用 Session（连接池 + keep-alive），避免每次请求重建 TCP/TLS。"""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update(DEFAULT_HEADERS)
        _local.session = s
    return s


def close_session() -> None:
    """关闭当前线程的 Session（进程退出前可调用）。"""
    s = getattr(_local, "session", None)
    if s is not None:
        s.close()
        _local.session = None


def _sleep_backoff(attempt: int, base: float) -> None:
    time.sleep(base * (2 ** (attempt - 1)) * (0.7 + random.random() * 0.6))


def _post(url: str, payload: dict, timeout: int = 10, retries: int = 2,
          backoff: float = 0.6) -> dict:
    """发起请求并完成两层 JSON 解析，返回内层 body 字典。

    网络错误与 429/5xx 会按指数退避重试；接口返回 result_ != true 时不重试（重试也没用）。
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            _sleep_backoff(attempt, backoff)
        try:
            r = _session().post(url, data=payload, timeout=timeout)
            if r.status_code in RETRY_STATUS:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
        except requests.RequestException as e:
            last_exc = e
            log.warning("请求失败(%s/%s): %s", attempt + 1, retries + 1, e)
            continue
        try:
            outer = r.json()
        except ValueError as e:
            raise PowerApiError(f"响应不是 JSON（上游可能改版或返回了风控页）: {r.text[:200]}") from e
        if outer.get("result_") != "true":
            raise PowerBusinessError(
                f"接口返回失败: code_={outer.get('code_')} message_={outer.get('message_')}")
        body_raw = outer.get("body")
        if body_raw is None or body_raw == "":
            raise PowerSchemaError("接口响应缺少 body")
        if isinstance(body_raw, dict):
            body = body_raw
        elif isinstance(body_raw, str):
            try:
                body = json.loads(body_raw)
            except json.JSONDecodeError as e:
                raise PowerSchemaError(f"body 二次解析失败: {e}") from e
        else:
            raise PowerSchemaError(f"body 类型异常: {type(body_raw).__name__}")
        if not isinstance(body, dict):
            raise PowerSchemaError(f"body 不是对象: {type(body).__name__}")
        return body
    assert last_exc is not None
    raise last_exc


# ------------------------------------------------------------ 接口调用
def bind_rooms(account: str, customercode: int, url: str | None = None,
               command: str | None = None, timeout: int | None = None,
               api: ApiConfig | None = None) -> list[dict]:
    """查询学号绑定的房间列表。

    返回 [{"roomfullname": ..., "roomverify": ..., "detaillist": [...]}]
    """
    api = api or ApiConfig()
    url, command = url or api.url, command or api.command
    timeout = timeout if timeout is not None else api.timeout
    payload = _payload(api.cmd_bind, {"account": account}, customercode, command)
    body = _post(url, payload, timeout, api.retries, api.backoff)
    rooms = body.get("roomlist") or []
    if not rooms:
        log.warning("getbindroom 未返回房间（账号 %s）: %s", mask_id(account), list(body)[:8])
    return rooms


def room_power(account: str, roomverify: str, customercode: int,
               url: str | None = None, command: str | None = None,
               timeout: int | None = None, api: ApiConfig | None = None) -> dict:
    """查询单个房间的电量详情，返回解析后的 body 字典。"""
    api = api or ApiConfig()
    url, command = url or api.url, command or api.command
    timeout = timeout if timeout is not None else api.timeout
    payload = _payload(api.cmd_index, {"account": account, "roomverify": roomverify,
                                       "timestamp": _timestamp()},
                       customercode, command)
    return _post(url, payload, timeout, api.retries, api.backoff)


def check_account(account: str, customercode: int, api: ApiConfig | None = None) -> bool | None:
    """探测某学校代码下该账号是否存在（搜校用）。

    返回 True/False = 探测成功（存在/不存在），None = 请求本身没成功（网络/限速/超时）。
    调用方要区分这两类：把"网络失败"当成"账号不存在"，搜校会在被限流时一路空跑完。
    """
    api = api or ApiConfig()
    payload = _payload(api.cmd_login, {"outid": account, "account": account,
                                       "timestamp": _timestamp()},
                       customercode, api.command)
    try:
        body = _post(api.url, payload, min(8, api.timeout), max(0, api.retries - 1), api.backoff)
    except PowerApiError:
        return False          # 接口通了，只是这个学校代码下没有这个人
    except Exception:
        return None           # 网络错误 / 超时 / HTTP 429、5xx
    return body.get("result") == "0" or bool(body.get("empname"))


def _payload(cmd: str, param: dict, customercode: int, command: str) -> dict:
    return {"param": json.dumps({"cmd": cmd, **param}),
            "customercode": customercode, "method": cmd, "command": command}


def pick_room(rooms: list[dict], keyword: str) -> dict | None:
    """按房间号关键词筛选房间，返回第一个匹配；没有关键词且只有一个房间时返回它。

    多个匹配时返回第一个（调用方拿不到「歧义」信号，需自行提示用户），
    零个匹配返回 None。
    """
    if not keyword:
        return rooms[0] if len(rooms) == 1 else None
    matched = [r for r in rooms if keyword in (r.get("roomfullname") or "")]
    return matched[0] if matched else None


# ------------------------------------------------------------ 容错解析
_seen_schema: set[str] = set()


def _dig(body: dict, path: str):
    """按 `a.0.b` 路径取值，任一层缺失返回 None。"""
    cur: object = body
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _report_schema(detail_body: dict, where: str = "extract_odd") -> None:
    """解析不出电量时记录结构指纹（只有键名和类型，不含任何值），方便快速适配改版。"""
    def shape(obj, depth=0):
        if depth > 2:
            return "..."
        if isinstance(obj, dict):
            return {k: shape(v, depth + 1) for k, v in list(obj.items())[:12]}
        if isinstance(obj, list):
            return [shape(obj[0], depth + 1)] if obj and isinstance(obj[0], (dict, list)) else f"list[{len(obj)}]"
        return type(obj).__name__

    fp = json.dumps(shape(detail_body), ensure_ascii=False, sort_keys=True)
    if fp in _seen_schema:
        return
    _seen_schema.add(fp)
    try:
        os.makedirs(os.path.dirname(SCHEMA_LOG) or ".", exist_ok=True)
        with open(SCHEMA_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{where}\t{fp}\n")
    except OSError:
        pass
    log.warning("电量字段解析失败，结构已记录到 %s（where=%s）：%s", SCHEMA_LOG, where, fp[:300])


def extract_odd(detail_body: dict, fields: Iterable[str] | None = None,
                paths: Iterable[str] | None = None,
                list_fields: Iterable[str] | None = None,
                api: ApiConfig | None = None) -> tuple[float | None, dict]:
    """从容错角度探测剩余电量字段，返回 (数值, 探测信息)。

    探测顺序：配置的取值路径 -> 顶层候选字段 -> 列表子项的候选字段。
    全都没命中时返回 (None, ...)，同时把结构指纹写入 data/schema_probe.log 并告警——
    这是"上游改版"的信号，据此补充 `api.odd_paths` / `api.odd_fields` 即可，无需改代码。
    """
    api = api or ApiConfig()
    paths = tuple(paths or api.odd_paths)
    fields = tuple(fields or api.odd_fields)
    list_fields = tuple(list_fields or api.list_fields)

    for path in paths:
        val = _dig(detail_body, path)
        if val is not None:
            try:
                return float(val), {"field": path, "raw": val}
            except (TypeError, ValueError):
                pass
    for key in fields:
        val = detail_body.get(key)
        if val is not None:
            try:
                return float(val), {"field": key, "raw": val}
            except (TypeError, ValueError):
                pass
    for list_key in list_fields:
        for item in detail_body.get(list_key) or []:
            if isinstance(item, dict):
                for key in fields:
                    if item.get(key) is not None:
                        try:
                            return float(item[key]), {"field": f"{list_key}.{key}", "raw": item[key]}
                        except (TypeError, ValueError):
                            pass
    _report_schema(detail_body)
    return None, {"field": None, "raw": None}


def extract_detail(detail_body: dict) -> dict:
    """提取播报用的完整信息：房间名、设备MAC、剩余、今日/周用量。"""
    info = {
        "roomfullname": detail_body.get("roomfullname", ""),
        "collecdate": detail_body.get("collecdate", ""),
    }
    modlist = detail_body.get("modlist") or []
    if modlist and isinstance(modlist[0], dict):
        mod = modlist[0]
        info.update({
            "odd": mod.get("odd"),
            "mac": mod.get("mac", ""),
            "devicename": mod.get("devicename", ""),
            "todayuse": mod.get("todayuse"),
            "weekuselist": mod.get("weekuselist") or [],
        })
    return info
