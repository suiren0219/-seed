"""AstrBot Star 插件：宿舍电量查询（完美校园水电接口），多用户自助版。

命令：
  /电量                      查询自己绑定的房间电量
  /电量绑定 <学校代码> <账号>  绑定学校与账号（账号为完美校园 outid，一般=身份证号）
  /电量房间                  列出账号在水电系统里绑定的房间（带序号）
  /电量添加 <序号|关键字...>  按序号或房间名关键字把 /电量房间 列出的房间加入监控
                             如 /电量添加 1 2、/电量添加 102、/电量添加 1栋的a 102
  /电量删除 <序号|关键字...>  移除已添加的房间，如 /电量删除 1、/电量删除 101
  /电量推送                  在当前会话订阅每日定时播报
  /电量解绑                  清空自己的绑定
  /电量搜校 <账号>            扫描学校代码（无需抓包，限速跑，几分钟到几十分钟）

兼容写法：指令可带 / 前缀；参数可与指令名粘连（如 电量删除1、电量绑定1000145 123...）。

安全与稳定性：
* 用户数据（账号=身份证号、房间信息）整体加密落盘，需配置密钥（encrypt_key 或环境变量
  DORM_POWER_KEY / 密钥文件 .dorm_power_key）；没有密钥时退化成明文并启动告警。
* 搜校限速 + 熔断：默认 6 并发 / 6 QPS，带随机抖动，连续失败冷却，疑似被限流直接中止；
  同时用「历史命中过的学校代码」候选池优先探测，通常几十个请求就能命中，避免全量扫描。
* 全插件复用一个 aiohttp session（连接池 + keep-alive）；网络错误 / 429 / 5xx 指数退避重试。
* 接口地址、command、各 cmd 名、剩余电量字段全部可配置：上游小改只改配置，不必改代码。
"""
import asyncio
import importlib.util
import json
import os
import random
import re
import sys
import time
from datetime import datetime

import aiohttp
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_PATH = os.path.join("data", "config", "astrbot_plugin_dorm_power_users.json")
# 命中过的学校代码候选池（只存数字代码，不含任何账号信息）
KNOWN_CODES_PATH = os.path.join("data", "config", "dorm_power_known_schools.json")


def _load_side_module(mod_name: str, filename: str):
    """按文件路径加载同目录模块。

    插件可能被当作单个文件加载，相对导入不一定可用，所以直接用文件路径加载。
    """
    for base in (PLUGIN_DIR, os.path.dirname(PLUGIN_DIR)):
        path = os.path.join(base, filename)
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            return mod
    return None


secure_store = _load_side_module("_dp_secure_store", "secure_store.py")


class _DummyBox:
    """模块缺失/没有密钥时的空实现：行为等价于不加密。"""

    available = False
    key = None
    backend_name = "none"

    def encrypt(self, text):
        return text

    def decrypt(self, text):
        return text


def new_box(key: str | None = None):
    """按 配置 > 环境变量 DORM_POWER_KEY > 密钥文件 取 SecretBox。"""
    if not secure_store:
        return _DummyBox()
    return secure_store.box_from_env(key, PLUGIN_DIR, os.getcwd())


def mask_id(value) -> str:
    """日志/回执里的账号一律脱敏：首尾各藏 4 位，避免身份证号进日志。

    身份证前 4 位是省市地区码、后 4 位是顺序码+校验位，两头都能把范围缩得很小，
    所以都不留：440000200001010000 -> ****0020000101****。
    """
    if secure_store:
        return secure_store.mask_id(value)
    s = str(value or "")
    if len(s) <= 8:
        return "*" * len(s)
    return f"****{s[4:-4]}****"


API_URL = "https://xqh5.17wanxiao.com/smartWaterAndElectricityService/SWAEServlet"
RETRY_STATUS = (429, 500, 502, 503, 504)
# 学校代码扫描范围：旧编码段 1-3000 + 新平台编码段 1000000-1010000
# 分段是为了「按段推进 + 命中即停」，配合限速尽量少发请求
SCAN_SEGMENTS = (("旧编码段 1-3000", list(range(1, 3001))),
                 ("新平台段 1000000-1010000", list(range(1000000, 1010001))))
SCAN_RANGES = [c for _name, seg in SCAN_SEGMENTS for c in seg]
# 搜校节流默认值与硬上限（保守：宁可慢，也不要把接口打死或把自己 IP 送进风控）
SCAN_DEFAULTS = {"qps": 6.0, "concurrency": 6, "max_hits": 3, "cooldown_minutes": 10}
SCAN_LIMITS = {"qps": (1, 20), "concurrency": (1, 16), "max_hits": (1, 10),
               "cooldown_minutes": (0, 1440)}

COMMANDS = ("电量", "电量绑定", "电量房间", "电量添加", "电量删除", "电量推送", "电量解绑", "电量搜校")
# 允许“指令名+参数粘连”的指令（如 电量删除1）：仅限参数以数字开头的这四条，
# 避免“电量好低啊”“电量推送给我看看”这类日常聊天被误当成指令。
GLUED_COMMANDS = ("电量搜校", "电量绑定", "电量添加", "电量删除")

# WebUI rooms 配置框的模板：# 开头的说明行会被解析器忽略，末尾留空行方便直接填写
ROOMS_TEMPLATE = (
    "# 每行一条：房间名|roomverify\n"
    "# 例：1栋A-102|101-1--12-102\n"
    "# 说明行(#开头)和空行不会生效；本表仅作未绑定用户的兜底，普通用户发 /电量绑定 自助绑定即可\n\n"
)


def _now_ts() -> str:
    now = datetime.now()
    return now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"


def _parse_rooms_text(text: str) -> list[tuple[str, str]]:
    targets = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue  # 空行、# 说明行、缺 | 的行都跳过
        name, rv = line.split("|", 1)
        if name.strip() and rv.strip():
            targets.append((name.strip(), rv.strip()))
    return targets


def _norm_text(s: str) -> str:
    """房间名归一化：去空白/下划线/横线/逗号/「的」并转小写，
    让 '1栋的a' 能匹配 '主分区_1栋A_9层_101'。"""
    return re.sub(r"[\s_\-—,，、的]", "", str(s)).lower()


def pick_rooms(args: list[str], rooms: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[str], bool]:
    """按序号或名称关键字从 rooms 里挑房间。

    序号参数（如 1 2）取并集；关键字参数（如 102、a8）之间取交集，最后再与序号求交。
    纯数字若超出序号范围，则当作关键字按名称匹配（如两间房时 /电量添加 102 仍能命中 102）。
    返回 (picks, 没匹配上的参数, 关键字命中多个房间)。
    """
    kw: set | None = None
    idx: set[int] = set()
    for a in args:
        if a.isdigit() and 1 <= int(a) <= len(rooms):
            idx.add(int(a) - 1)
            continue
        hit = {i for i in range(len(rooms)) if _norm_text(a) in _norm_text(rooms[i][0])}
        if not hit:
            return [], [a], False
        kw = hit if kw is None else kw & hit
        if not kw:
            return [], [a], False
    if kw is not None and idx:
        final = kw & idx
    elif kw is not None:
        final = kw
    else:
        final = idx
    picks = [rooms[i] for i in sorted(final)]
    return picks, [], kw is not None and not idx and len(picks) > 1


class _ApiStatusError(RuntimeError):
    """HTTP 层错误（429/5xx 等），可重试。"""


class _ApiResultError(RuntimeError):
    """接口通了但 result_ != true：属于业务结果，不该当成网络失败去重试/熔断。"""


def _known_codes_path() -> str:
    """已知命中过的学校代码候选池（只存数字代码，不含任何个人信息）。"""
    return KNOWN_CODES_PATH


class _Http:
    """插件级复用的 aiohttp 会话（连接池 + keep-alive）。

    之前每次请求都 `async with aiohttp.ClientSession()`，等于每次重建 TCP+TLS；
    改成单例后在 terminate 时统一关闭。
    """

    _session: aiohttp.ClientSession | None = None

    @classmethod
    def session(cls) -> aiohttp.ClientSession:
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300),
                headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"})
        return cls._session

    @classmethod
    async def close(cls) -> None:
        if cls._session is not None and not cls._session.closed:
            await cls._session.close()
        cls._session = None


class _RateLimiter:
    """令牌桶：在并发闸门之上再加一道全局 QPS 限制。"""

    def __init__(self, qps: float, burst: int | None = None):
        self.qps = max(0.1, float(qps))
        self.burst = max(1, int(burst or self.qps))
        self._tokens = float(self.burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.burst,
                                   self._tokens + (now - self._updated) * self.qps)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.qps)


class _ScanGuard:
    """搜校保护：并发闸门 + QPS 限速 + 抖动 + 连续失败冷却 + 熔断中止。

    背景：旧版 24 并发猛扫 1.3 万个学校代码，等于对第三方接口做压测，
    很容易被限流、封 IP 或触发 WAF。这里默认降到 6 并发 / 6 QPS，
    并在疑似被限流时主动降温，多次熔断后直接放弃。
    """

    def __init__(self, concurrency: int = 6, qps: float = 6.0, max_hits: int = 3,
                 cooldown: float = 30.0, max_trips: int = 3, fail_threshold: int = 25):
        self.workers = max(1, concurrency)
        self.sem = asyncio.Semaphore(self.workers)
        self.limiter = _RateLimiter(qps)
        self.max_hits = max(1, max_hits)
        self.cooldown = cooldown
        self.max_trips = max(1, max_trips)
        self.fail_threshold = fail_threshold
        self._fails = 0
        self.trips = 0
        self._pause_until = 0.0
        self.aborted = False
        self.reason = ""
        self.done = 0
        self._lock = asyncio.Lock()

    async def slot(self) -> None:
        """取一个请求额度（并发闸门 -> 冷却等待 -> 令牌 -> 随机抖动）。"""
        await self.sem.acquire()
        try:
            while True:
                wait = self._pause_until - time.monotonic()
                if wait <= 0:
                    break
                await asyncio.sleep(min(wait, 1.0))
            await self.limiter.acquire()
            await asyncio.sleep(random.uniform(0.02, 0.15))  # 抖动：避免整齐的并发脉冲
        except Exception:
            self.sem.release()
            raise

    def release(self) -> None:
        self.sem.release()

    async def report(self, ok: bool) -> bool:
        """上报一次探测结果；返回 False 表示应当中止整个扫描。"""
        async with self._lock:
            self.done += 1
            if self.aborted:
                return False
            if ok:
                self._fails = 0
                return True
            self._fails += 1
            if self._fails < self.fail_threshold:
                return True
            self._fails = 0
            self.trips += 1
            if self.trips > self.max_trips:
                self.aborted = True
                self.reason = "连续多次探测失败（疑似被限流/封禁），已主动停止"
                return False
            self._pause_until = time.monotonic() + self.cooldown * self.trips
            logger.warning("[dorm_power] 探测连续失败，第 %s 次熔断，冷却 %ss",
                           self.trips, round(self.cooldown * self.trips))
        return True


@register("dorm_power", "user", "宿舍电量查询与定时播报（完美校园，多用户自助）", "v2.2.0")
class DormPowerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._pending = {}  # user_id -> [(name, roomverify)]，/电量房间 的暂存
        self._users = {}    # user_id -> {"customercode","account","rooms":[(name,rv)],"origin":str}
        self._scan_running = False        # 同一时间只允许一个搜校任务
        self._scan_last = {}              # 账号摘要 -> 上次扫描时间戳（冷却用）
        self._box = new_box(self._cfg_get("encrypt_key") if self.config is not None else None)
        self._load_users()

    def _cfg_get(self, key: str, default=None):
        """安全取插件配置项（self.config 可能是 None 或不支持 get）。"""
        try:
            return self.config.get(key, default) if self.config is not None else default
        except Exception:
            return default

    # ---------- 用户数据（含身份证号，落盘整体加密） ----------
    def _load_users(self):
        """读取用户数据；支持加密文件与历史明文文件（读进来后下次保存自动加密）。"""
        raw = {}
        try:
            with open(USER_DATA_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._users = {}
            return
        if isinstance(raw, dict) and raw.get("_encrypted"):
            if not self._box.available:
                logger.error("[dorm_power] 用户数据已加密，但没找到密钥（设置环境变量 DORM_POWER_KEY），本次无法读取")
                self._users = {}
                return
            try:
                raw = json.loads(self._box.decrypt(raw.get("data", "")))
            except Exception as e:
                logger.error("[dorm_power] 用户数据解密失败（密钥可能已更换）: %s", e)
                self._users = {}
                return
        users = {}
        for uid, u in (raw or {}).items():
            if not isinstance(u, dict):
                continue
            u["rooms"] = self._norm_rooms(u.get("rooms"))
            users[uid] = u
        self._users = users

    def _save_users(self):
        """原子保存；有密钥时整体加密落盘（身份证号/房间信息不再明文）。"""
        os.makedirs(os.path.dirname(USER_DATA_PATH) or ".", exist_ok=True)
        data = {uid: {**u, "rooms": [list(r) for r in self._norm_rooms(u.get("rooms"))]}
                for uid, u in self._users.items()}
        payload = data
        if self._box.available:
            payload = {"_encrypted": True, "v": 1,
                       "data": self._box.encrypt(json.dumps(data, ensure_ascii=False))}
        tmp = USER_DATA_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USER_DATA_PATH)
        try:  # 收紧文件权限（Windows 无效果）
            os.chmod(USER_DATA_PATH, 0o600)
        except OSError:
            pass

    # ---------- 全局默认配置（管理员，兜底用） ----------
    def _dec(self, value):
        """配置里的 `enc:v1:` 值还原成明文（有密钥才行）。"""
        if isinstance(value, str) and value.startswith("enc:v1:") and self._box.available:
            try:
                return self._box.decrypt(value)
            except Exception as e:
                logger.error("[dorm_power] 配置项解密失败（密钥不对？）: %s", e)
        return value

    def _global_cfg(self) -> dict:
        if self.config is not None and self.config.get("account"):
            return {"account": self._dec(self.config.get("account", "")),
                    "customercode": self.config.get("customercode", 1000145),
                    "rooms": _parse_rooms_text(self.config.get("rooms", "")),
                    "origin": self.config.get("notify_origin", "")}
        try:
            with open(os.path.join(PLUGIN_DIR, "config.yaml"), "r", encoding="utf-8") as f:
                local = yaml.safe_load(f) or {}
            stu = local.get("student", {})
            return {"account": self._dec(stu.get("account", "")),
                    "customercode": stu.get("customercode", 1000145),
                    "rooms": [(r.get("name", ""), self._dec(r["roomverify"]))
                              for r in stu.get("rooms", []) if r.get("roomverify")],
                    "origin": local.get("notify", {}).get("target_id", "")}
        except FileNotFoundError:
            return {"account": "", "customercode": 1000145, "rooms": [], "origin": ""}

    def _threshold(self) -> float:
        """低电量预警阈值：优先读面板 warn_threshold，回退到 config.yaml warn.threshold。"""
        if self.config is not None and self.config.get("warn_threshold") is not None:
            return float(self.config["warn_threshold"])
        if self.config is not None and self.config.get("threshold"):
            return float(self.config["threshold"])
        try:
            with open(os.path.join(PLUGIN_DIR, "config.yaml"), "r", encoding="utf-8") as f:
                return float((yaml.safe_load(f) or {}).get("warn", {}).get("threshold", 20))
        except (FileNotFoundError, ValueError):
            return 20.0

    def _cmd(self, key: str, default: str) -> str:
        """接口 cmd 名可配置：上游改名时改插件配置即可，不必改代码。"""
        return str(self._cfg_get(key) or default)

    def _odd_fields(self) -> tuple[str, ...]:
        """剩余电量字段候选（逗号分隔配置），解析时按顺序尝试。"""
        raw = self._cfg_get("odd_fields") or ""
        fields = [f.strip() for f in str(raw).split(",") if f.strip()]
        return tuple(fields) or ("odd", "surpluselec", "surplusElec", "remain", "balance")

    # ---------- 生命周期 ----------
    def _migrate_rooms_hint(self):
        """旧配置里 rooms 填的不是有效格式时，换成带格式说明的模板，
        让面板输入框一打开就有格式、示例和可填写的空行。

        只在「确实填了东西但解析不出房间」时才替换：留空是合法的（多数用户走
        /电量绑定，不填这张兜底表），旧实现会把空值也写成模板，等于往用户的
        配置里塞了一堆说明文字，还每次启动都判定为「需要迁移」。
        """
        try:
            cur = self.config.get("rooms")
        except Exception:
            return
        if not cur or cur == ROOMS_TEMPLATE or _parse_rooms_text(cur):
            return
        try:
            self.config["rooms"] = ROOMS_TEMPLATE
            self.config.save_config()
            logger.info("[dorm_power] rooms 配置解析不出房间，已替换为带格式说明的模板")
        except Exception as e:
            logger.warning(f"[dorm_power] rooms 配置模板迁移失败: {e}")

    async def initialize(self):
        hours = "8,20"
        if self.config is not None:
            hours = str(self.config.get("cron_hours") or "8,20")
            self._migrate_rooms_hint()
        self.scheduler.add_job(self._scheduled_report, "cron", hour=hours, minute=0)
        self.scheduler.start()
        if self._box.available:
            logger.info("[dorm_power] 用户数据加密存储已启用（后端 %s）", self._box.backend_name)
        else:
            logger.warning("[dorm_power] 未配置密钥，用户数据将明文存储；"
                           "设置环境变量 DORM_POWER_KEY 或插件配置 encrypt_key 可加密（推荐装 cryptography）")
        logger.info("[dorm_power] 插件已加载(v2.2 多用户)，定时播报小时: %s，已注册用户: %d",
                    hours, len(self._users))

    async def terminate(self):
        # initialize 可能没跑过（加载失败/被管理员禁用），scheduler.running 属性那时还不存在
        if getattr(self.scheduler, "running", False):
            self.scheduler.shutdown(wait=False)
        await _Http.close()

    # ---------- API ----------
    async def _post(self, customercode: int, param: dict, timeout_s: int = 10,
                    retries: int = 1) -> dict:
        """发请求：复用全局 session；网络错误 / 429 / 5xx 按指数退避重试。"""
        url = self._cfg_get("api_url") or API_URL
        command = self._cfg_get("api_command") or "JBSWaterElecService"
        payload = {"param": json.dumps(param, ensure_ascii=False), "customercode": customercode,
                   "method": param["cmd"], "command": command}
        last: Exception | None = None
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(0.6 * (2 ** (attempt - 1)) * (0.7 + random.random() * 0.6))
            try:
                async with _Http.session().post(
                        url, data=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                    if resp.status in RETRY_STATUS:
                        raise _ApiStatusError(f"HTTP {resp.status}")
                    outer = await resp.json(content_type=None)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as e:
                last = e  # 风控页/网关错误：退避重试几次再放弃
        else:
            raise last  # type: ignore[misc]
        if outer.get("result_") != "true":
            raise _ApiResultError(f"接口返回失败: {outer.get('message_')}")
        return json.loads(outer.get("body") or "{}")

    @staticmethod
    def _fmt_num(val) -> str:
        """数值容错格式化：'31'/'31.0'/31 都输出干净文本。"""
        try:
            return f"{float(val):g}"
        except (TypeError, ValueError):
            return str(val) if val is not None else "-"

    @classmethod
    def _format_room(cls, detail: dict, name: str,
                     odd_fields: tuple[str, ...] = ("odd",)) -> tuple[str, float]:
        """把 h5_getstuindexpage 的 JSON 解析成一行一个房间的紧凑文本。

        单行格式（用全角｜分隔）是为了抗平台/分段插件吞换行——即使被压成一段，
        每个房间仍以 🏠 房间名 开头，能读。返回 (文本, 剩余电量)；
        解析不出电量时抛 ValueError（带干净原因，不甩原始 JSON）。
        """
        modlist = detail.get("modlist") or []
        if not modlist or not isinstance(modlist[0], dict):
            msg = detail.get("message") or "接口未返回电量模块"
            raise ValueError(f"未返回电量数据（{msg}）")
        mod = modlist[0]

        odd = None
        for field in odd_fields:  # 上游改字段名时，配置 odd_fields 即可继续用
            if mod.get(field) is not None:
                try:
                    odd = float(mod[field])
                    break
                except (TypeError, ValueError):
                    continue
        if odd is None:
            raise ValueError(f"电量字段解析失败（已试 {', '.join(odd_fields)}）")

        room_name = detail.get("roomfullname") or name
        parts = [f"🏠{room_name}", f"剩余{cls._fmt_num(odd)}度"]

        today = mod.get("todayuse")
        if today is not None:
            parts.append(f"今日{cls._fmt_num(today)}度")

        week = [d for d in (mod.get("weekuselist") or []) if isinstance(d, dict)]
        if week:
            seg = " ".join(f"{str(d.get('weekday', '')).replace('星期', '')}"
                           f"{cls._fmt_num(d.get('dayuse', d.get('use')))}" for d in week)
            parts.append(f"近7日 {seg}")

        month = [m for m in (mod.get("monthuselist") or []) if isinstance(m, dict)]
        if month:
            last = month[-1]
            parts.append(f"上月{cls._fmt_num(last.get('monthuse'))}度")

        if mod.get("sumbuy") is not None:
            parts.append(f"累购{cls._fmt_num(mod['sumbuy'])}度")

        if detail.get("collecdate"):
            parts.append(f"抄表{detail['collecdate']}")
        return "｜".join(parts), odd

    async def _query_room(self, customercode: int, account: str, roomverify: str, name: str) -> tuple[str, float]:
        detail = await self._post(customercode, {
            "cmd": self._cmd("cmd_index", "h5_getstuindexpage"), "account": account,
            "roomverify": roomverify, "timestamp": _now_ts()})
        return self._format_room(detail, name, self._odd_fields())

    async def _check_school(self, account: str, code: int) -> bool | None:
        """login 探测：该学校代码下此账号是否存在。

        返回 True/False = 探测成功（存在/不存在）；None = 请求没打成功（网络/限流），
        用于扫描熔断统计——不能把"账号不存在"当成失败，否则永远在熔断。
        """
        try:
            body = await self._post(code, {"cmd": self._cmd("cmd_login", "login"),
                                           "outid": account, "account": account,
                                           "timestamp": _now_ts()},
                                    timeout_s=8, retries=0)
        except _ApiResultError:
            return False          # 接口通了，只是这个代码下没这个人
        except Exception:
            return None           # 网络错误 / 超时 / HTTP 429、5xx
        return body.get("result") == "0" or bool(body.get("empname"))

    # ---------- 参数解析 ----------
    @staticmethod
    def _args(event, cmd: str) -> list[str]:
        """取指令参数：兼容 带空格/不带空格（电量删除1）/带 / 前缀/中文逗号顿号分隔。"""
        text = (event.message_str or "").strip().lstrip("/").strip()
        if text.startswith(cmd):
            rest = text[len(cmd):]
        elif text.split() and text.split()[0].startswith(cmd):
            tokens = text.split()
            rest = tokens[0][len(cmd):] + " " + " ".join(tokens[1:])
        else:
            rest = ""
        return [a for a in re.split(r"[\s,，、]+", rest) if a]

    def _user_of(self, event) -> tuple[str, dict]:
        self._load_users()  # 每次命令都重读文件：容忍手工/外部修改（如面板或脚本直接改 JSON）
        uid = event.get_sender_id()
        return uid, self._users.get(uid)

    # ---------- 命令入口（装饰器只做路由，逻辑在 _h_* 里，便于粘连兜底复用） ----------
    @filter.command("电量")
    async def cmd_power(self, event):
        """查询自己绑定的房间电量（未绑定则查询默认房间）"""
        async for r in self._h_power(event):
            yield r

    @filter.command("电量绑定")
    async def cmd_bind(self, event):
        """绑定学校代码与账号：/电量绑定 <学校代码> <账号>"""
        async for r in self._h_bind(event, self._args(event, "电量绑定")):
            yield r

    @filter.command("电量房间")
    async def cmd_rooms(self, event):
        """列出账号在水电系统绑定的房间"""
        async for r in self._h_rooms(event):
            yield r

    @filter.command("电量添加")
    async def cmd_add(self, event):
        """添加房间：/电量添加（全部）或 /电量添加 1 2 / /电量添加 102（序号或关键字）"""
        async for r in self._h_add(event, self._args(event, "电量添加")):
            yield r

    @filter.command("电量删除")
    async def cmd_del(self, event):
        """删除房间：/电量删除 <序号或房间关键字>"""
        async for r in self._h_del(event, self._args(event, "电量删除")):
            yield r

    @filter.command("电量推送")
    async def cmd_push(self, event):
        """在当前会话订阅每日定时播报"""
        async for r in self._h_push(event):
            yield r

    @filter.command("电量解绑")
    async def cmd_unbind(self, event):
        """清空自己的绑定"""
        async for r in self._h_unbind(event):
            yield r

    @filter.command("电量搜校")
    async def cmd_scan(self, event):
        """自动扫描学校代码：/电量搜校 <账号>（限速扫描，完成后回传结果）"""
        async for r in self._h_scan(event, self._args(event, "电量搜校")):
            yield r

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def cmd_glued(self, event):
        """兜底：兼容指令名和参数粘连的写法（电量删除1、电量绑定1000145 xxx 等）。

        AstrBot 按首个分词精确匹配命令名，粘连写法不会进命令管线而是落给 LLM；
        这里只拦截「参数以数字开头」的粘连消息，其余一律放行，避免误伤日常聊天。
        """
        text = (event.message_str or "").strip().lstrip("/").strip()
        head = text.split()[0] if text.split() else ""
        if not head or event.get_extra("parsed_params") is not None:
            return  # 正式命令已接手，或不是指令消息
        for cmd in GLUED_COMMANDS:
            if not (head.startswith(cmd) and len(head) > len(cmd)):
                continue
            if not head[len(cmd):][:1].isdigit():
                continue
            args = self._args(event, cmd)
            logger.info("[dorm_power] 粘连指令兜底 %r -> %s %s", head, cmd, args)
            event.stop_event()  # 不再传给 LLM
            if cmd == "电量绑定":
                gen = self._h_bind(event, args)
            elif cmd == "电量添加":
                gen = self._h_add(event, args)
            elif cmd == "电量删除":
                gen = self._h_del(event, args)
            else:
                gen = self._h_scan(event, args)
            async for r in gen:
                yield r
            return

    # ---------- 命令实现 ----------
    async def _h_power(self, event):
        uid, user = self._user_of(event)
        g = self._global_cfg()
        if user and user.get("rooms"):
            code, account, rooms = user["customercode"], user["account"], user["rooms"]
        elif g["rooms"]:
            code, account, rooms = g["customercode"], g["account"], g["rooms"]
            yield event.plain_result("ℹ️ 你还未绑定，以下为默认房间。\n绑定方式：/电量绑定 <学校代码> <账号>\n")
        else:
            yield event.plain_result(
                "你还没有绑定电量查询。\n\n"
                "三步搞定：\n"
                "1️⃣ /电量绑定 <学校代码> <账号>   账号为完美校园 outid（一般是身份证号）\n"
                "2️⃣ /电量房间 后再发 /电量添加    自动列出并加入你绑定的房间\n"
                "3️⃣ /电量                         查询电量；/电量推送 订阅每日播报\n\n"
                "不知道学校代码？用 /电量搜校 <账号> 自动查找")
            return
        blocks = []
        warn_lines = []
        th = self._threshold()
        for name, rv in rooms:
            try:
                text, odd = await self._query_room(code, account, rv, name)
                blocks.append(text)
                if odd <= th:
                    warn_lines.append(f"⚠️ {name} 剩余 {odd:g} 度，低于 {th:g} 度，请及时充值！")
            except Exception as e:
                blocks.append(f"❌ {name}: {e}")
        result = "\n".join(blocks)
        if warn_lines:
            result += "\n" + "\n".join(warn_lines)
        yield event.plain_result(result)

    async def _h_bind(self, event, args):
        if len(args) < 2 or not args[0].isdigit():
            yield event.plain_result("用法：/电量绑定 <学校代码> <账号>\n例：/电量绑定 1000145 440000200001010000\n不知道学校代码？/电量搜校 <账号>")
            return
        uid, _user = self._user_of(event)
        user = self._users.get(uid) or {"rooms": [], "origin": ""}
        user["customercode"] = int(args[0])
        user["account"] = args[1]
        self._users[uid] = user
        self._save_users()
        logger.info("[dorm_power] /电量绑定 uid=%s code=%s account=%s",
                    uid, args[0], mask_id(args[1]))
        yield event.plain_result(f"✅ 已绑定学校代码 {args[0]}\n下一步：/电量房间 列出你账号绑定的房间，然后 /电量添加 <序号> 加入监控")

    async def _h_rooms(self, event):
        uid, user = self._user_of(event)
        if not user or not user.get("account"):
            yield event.plain_result("请先 /电量绑定 <学校代码> <账号>")
            return
        try:
            body = await self._post(user["customercode"], {
                "cmd": self._cmd("cmd_bind", "getbindroom"),
                "account": user["account"], "timestamp": _now_ts()})
        except Exception as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return
        roomlist = body.get("roomlist") or []
        if not roomlist:
            yield event.plain_result("该账号在水电系统没有绑定房间。请先在完美校园小程序里绑定房间后再试。")
            return
        pending = []
        lines = ["你账号绑定的房间："]
        for i, r in enumerate(roomlist, 1):
            name = r.get("roomfullname") or f"房间{i}"
            rv = r.get("roomverify", "")
            pending.append((name, rv))
            odd = ""
            d = (r.get("detaillist") or [{}])[0]
            if d.get("odd") is not None:
                odd = f"  当前 {d['odd']} 度"
            lines.append(f"{i}. {name}{odd}")
        self._pending[uid] = pending
        lines.append("")
        lines.append("➡️ 全部加入监控：/电量添加")
        lines.append("➡️ 只加部分：/电量添加 1 2")
        if len(pending) > 1:
            lines.append("➡️ 加全部时若只想留一间，再加 /电量删除 <序号> 移除即可")
        yield event.plain_result("\n".join(lines))

    async def _h_add(self, event, args):
        uid, user = self._user_of(event)
        pending = self._pending.get(uid)
        if not pending:
            yield event.plain_result(
                "还没获取房间列表。完整流程示例：\n\n"
                "1️⃣ /电量绑定 1000145 你的账号\n"
                "2️⃣ /电量房间          ← 列出房间和序号\n"
                "3️⃣ /电量添加          ← 不带数字=全部加入\n"
                "    /电量添加 1 2     ← 或只加指定序号\n"
                "4️⃣ /电量              ← 查询电量\n"
                "5️⃣ /电量推送          ← 订阅每日 8 点、20 点播报")
            return
        if not user or not user.get("account"):
            yield event.plain_result("请先 /电量绑定 <学校代码> <账号>")
            return
        if not args:
            picks = list(pending)  # 不带参数 = 全部加入
            unmatched, ambiguous = [], False
        else:
            picks, unmatched, ambiguous = pick_rooms(args, pending)
        if unmatched:
            lst = "\n".join(f"{i}. {n}" for i, (n, _rv) in enumerate(pending, 1))
            yield event.plain_result(
                f"❌ 没看懂参数「{'」「'.join(unmatched)}」。当前房间列表：\n{lst}\n"
                "用法：/电量添加 <序号>（可多个，如 1 2）、/电量添加（全部）、"
                "或 /电量添加 <房间关键字>（如 102；多个关键字用空格隔开会自动缩小范围）")
            return
        if ambiguous or not picks:
            lst = "\n".join(f"{i}. {n}" for i, (n, _rv) in enumerate(pending, 1))
            yield event.plain_result(
                f"❌ 关键字匹配到多个房间，请用序号指定。当前房间列表：\n{lst}\n"
                "例：/电量添加 1，或加更多关键字缩小范围（如 /电量添加 a8 102）")
            return
        user = self._users.get(uid) or {"customercode": 0, "account": "", "rooms": [], "origin": ""}
        if not user.get("customercode"):
            user["customercode"] = self._global_cfg()["customercode"]
        added, existed = [], []
        for name, rv in picks:
            if (name, rv) in user["rooms"]:
                existed.append(name)
            else:
                user["rooms"].append((name, rv))
                added.append(name)
        self._users[uid] = user
        self._save_users()
        logger.info("[dorm_power] /电量添加 uid=%s args=%s 新增=%s 已存在=%s",
                    uid, args, added, existed)
        msg = []
        if added:
            msg.append(f"✅ 已添加：{'、'.join(added)}")
        if existed:
            msg.append(f"☑️ 已在监控，无需重复添加：{'、'.join(existed)}")
        if not added:
            msg.append("没有新增房间")
        msg.append(f"当前监控 {len(user['rooms'])} 个房间")
        if added:
            msg.append("发 /电量 查询；/电量推送 订阅每日播报")
        yield event.plain_result("\n".join(msg))

    async def _h_del(self, event, args):
        uid, user = self._user_of(event)
        if not user or not user["rooms"]:
            yield event.plain_result("你还没有添加房间")
            return
        if not args:
            lst = "\n".join(f"{i}. {n}" for i, (n, _rv) in enumerate(user["rooms"], 1))
            yield event.plain_result(
                f"用法：/电量删除 <序号> 或 <房间关键字>\n例：/电量删除 1、/电量删除 101\n你当前的房间：\n{lst}")
            return
        picks, unmatched, ambiguous = pick_rooms(args, user["rooms"])
        if unmatched or ambiguous or not picks:
            if unmatched:
                why = f"没看懂参数「{'」「'.join(unmatched)}」"
            elif ambiguous:
                why = "关键字匹配到多个房间"
            else:
                why = "没有匹配到任何房间"
            lst = "\n".join(f"{i}. {n}" for i, (n, _rv) in enumerate(user["rooms"], 1))
            yield event.plain_result(
                f"❌ {why}。你当前的房间：\n{lst}\n用法：/电量删除 <序号>（如 1）或 <关键字>（如 101）")
            return
        removed = []
        for name, rv in picks:
            if (name, rv) in user["rooms"]:
                user["rooms"].remove((name, rv))
                removed.append(name)
        self._save_users()
        logger.info("[dorm_power] /电量删除 uid=%s args=%s 删除=%s", uid, args, removed)
        yield event.plain_result(f"✅ 已删除：{'、'.join(removed)}\n剩余 {len(user['rooms'])} 个房间："
                                 + ("、".join(n for n, _rv in user["rooms"]) or "（无）"))

    async def _h_push(self, event):
        uid, _user = self._user_of(event)
        user = self._users.get(uid) or {"customercode": 0, "account": "", "rooms": [], "origin": ""}
        user["origin"] = event.unified_msg_origin
        self._users[uid] = user
        self._save_users()
        yield event.plain_result("✅ 已订阅每日 8 点、20 点电量播报（发到当前会话）")

    async def _h_unbind(self, event):
        uid, _user = self._user_of(event)
        if uid in self._users:
            del self._users[uid]
            self._save_users()
        self._pending.pop(uid, None)
        yield event.plain_result("✅ 已解绑并清除你的电量数据")

    # ---------- 搜校（限速 / 熔断 / 候选池，避免高频扫描被风控） ----------
    def _scan_opts(self) -> dict:
        """扫描参数（可在插件配置里调；这里做上下限夹取，防止配置把接口打爆）。"""
        def num(key, lo, hi):
            try:
                v = float(self._cfg_get(key, SCAN_DEFAULTS[key]))
            except (TypeError, ValueError, KeyError):
                v = float(SCAN_DEFAULTS[key])
            return max(lo, min(hi, v))

        return {"qps": num("scan_qps", *SCAN_LIMITS["qps"]),
                "concurrency": int(num("scan_concurrency", *SCAN_LIMITS["concurrency"])),
                "max_hits": int(num("scan_max_hits", *SCAN_LIMITS["max_hits"])),
                "cooldown_min": num("scan_cooldown_minutes", *SCAN_LIMITS["cooldown_minutes"])}

    def _known_codes(self) -> list[int]:
        """历史命中过的学校代码（候选池，只存数字，不含个人信息）。"""
        try:
            with open(_known_codes_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return [int(c) for c in (data or {}).get("codes", []) if str(c).isdigit()]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _remember_codes(self, codes) -> None:
        """把命中代码写回候选池：下次搜校先试这些，通常几十个请求就能命中。"""
        if not codes:
            return
        path = _known_codes_path()
        try:
            merged = sorted(set(self._known_codes()) | {int(c) for c in codes})
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"codes": merged}, f)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("[dorm_power] 候选池写入失败: %s", e)

    def _scan_cooldown_left(self, masked: str) -> float:
        last = self._scan_last.get(masked or "")
        if not last:
            return 0.0
        return max(0.0, self._scan_opts()["cooldown_min"] * 60 - (time.time() - last))

    async def _probe_codes(self, account: str, codes, guard: _ScanGuard,
                           max_hits: int) -> list[int]:
        """按 guard 的节奏探测一批代码，返回命中的代码。

        用固定数量的 worker 从队列里取代码：旧实现给整段（最多 1.3 万个）每个代码
        都建一个 task，全部挂在同一把并发信号量上排队——峰值几万个协程对象，
        取消时还要逐个 cancel。worker 数等于并发上限，扫描照样按限速跑，
        但常驻任务数从「代码总数」降到「并发数」。
        """
        hits: list[int] = []
        queue: asyncio.Queue[int] = asyncio.Queue()
        for code in codes:
            queue.put_nowait(code)

        def enough() -> bool:
            return guard.aborted or (max_hits and len(hits) >= max_hits)

        async def worker():
            while not enough():
                try:
                    code = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await guard.slot()
                try:
                    res = await self._check_school(account, code)
                except Exception:
                    res = None
                finally:
                    guard.release()
                if res is True and not enough() and code not in hits:
                    hits.append(code)
                await guard.report(res is not None)

        workers = [asyncio.create_task(worker()) for _ in range(guard.workers)]
        try:
            await asyncio.gather(*workers)
        finally:
            for t in workers:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return hits

    async def _h_scan(self, event, args):
        if not args:
            yield event.plain_result("用法：/电量搜校 <账号>（完美校园 outid，一般=身份证号）")
            return
        account = args[0]
        masked = mask_id(account)
        if self._scan_running:
            yield event.plain_result("⏳ 已有一个搜校任务在跑，请等它结束（同一时间只跑一个，避免请求翻倍）")
            return
        left = self._scan_cooldown_left(masked)
        if left > 0:
            yield event.plain_result(f"⏳ 该账号刚搜过，{int(left // 60) + 1} 分钟后可再试（防止高频请求被风控）")
            return
        opts = self._scan_opts()
        known = self._known_codes()
        eta = len(SCAN_RANGES) / opts["qps"] / 60
        yield event.plain_result(
            f"🔍 开始搜校：先试已记录的 {len(known)} 个学校代码，未命中再全量扫 {len(SCAN_RANGES)} 个。\n"
            f"为防风控已限速：{opts['qps']:g} 请求/秒、并发 {opts['concurrency']}，"
            f"全量最多约 {eta:.0f} 分钟（命中即提前结束）。\n"
            "完成后把结果发到这里，期间可正常使用其他命令。")
        asyncio.create_task(self._scan_schools(account, event.unified_msg_origin))

    async def _scan_schools(self, account: str, origin: str):
        if self._scan_running:
            return
        self._scan_running = True
        masked = mask_id(account)
        opts = self._scan_opts()
        guard = _ScanGuard(qps=opts["qps"], concurrency=opts["concurrency"])
        hits: list[int] = []
        try:
            # 阶段 0：候选池（历史命中过的代码，通常几十个请求内就能命中）
            hits += await self._probe_codes(account, self._known_codes(), guard,
                                            opts["max_hits"] - len(hits))
            # 阶段 1+：分段全量扫描，命中够数或熔断就停
            for name, codes in SCAN_SEGMENTS:
                if guard.aborted or len(hits) >= opts["max_hits"]:
                    break
                hits += await self._probe_codes(account, codes, guard, opts["max_hits"] - len(hits))
                logger.info("[dorm_power] 搜校阶段 %s 完成，累计命中 %s", name, len(hits))
                if not guard.aborted and len(hits) < opts["max_hits"] and name != SCAN_SEGMENTS[-1][0]:
                    await self._send(origin, f"🔍 搜校进度：{name} 已扫完，暂未命中，继续下一段……")
        except Exception as e:
            logger.error("[dorm_power] 搜校异常: %s", e)
        finally:
            self._scan_running = False
            self._scan_last[masked] = time.time()

        hits = sorted(set(hits))
        if hits:
            self._remember_codes(hits)
            text = (f"🔍 搜校完成！账号 {masked} 在以下学校代码下存在：\n"
                    + "\n".join(f"· {c}" for c in hits)
                    + "\n\n然后发：/电量绑定 <学校代码> 你刚才填的账号\n"
                    + "如有多个，逐一绑定后用 /电量房间 验证哪个能查到房间。")
        else:
            text = ("🔍 搜校完成，未在已知编码段（1-3000、1000000-1010000）找到该账号。\n"
                    "可能原因：账号不是身份证号 / 学校在别的编码段 / 被风控拦截。"
                    "请确认账号后重试，或联系管理员抓包获取。")
        if guard.aborted:
            text += f"\n\n⚠️ 扫描中途被中止：{guard.reason}。请过一阵子再试。"
        await self._send(origin, text)

    async def _send(self, origin: str, text: str) -> None:
        try:
            await self.context.send_message(origin, MessageChain().message(text))
        except Exception as e:
            logger.error(f"[dorm_power] 消息发送失败: {e}")

    @staticmethod
    def _norm_rooms(rooms) -> list[tuple[str, str]]:
        """只保留「名字+凭证」成对的房间：用户数据被手工改过时，缺字段的行直接丢掉，
        不让定时播报因为一条坏记录整体崩掉。"""
        out = []
        for r in rooms or []:
            if isinstance(r, (list, tuple)) and len(r) >= 2 and r[1]:
                out.append((r[0], r[1]))
        return out

    @classmethod
    def _report_key(cls, origin, code, account, rooms) -> tuple:
        """定时播报的去重键：同会话 + 同账号 + 同房间集合视为一条播报。

        房间列表排序后入键，这样「房间顺序不同但内容相同」的两条绑定也会被认成同一个，
        避免同一个人被播报两遍。
        """
        pairs = cls._norm_rooms(rooms)
        return (origin, code, account, tuple(sorted((str(a), str(b)) for a, b in pairs)))

    # ---------- 定时播报 ----------
    async def _scheduled_report(self):
        self._load_users()  # 播报前重读，拿到最新绑定
        jobs = []  # (origin, code, account, rooms)
        seen = set()
        for user in self._users.values():
            rooms = self._norm_rooms(user.get("rooms"))
            if user.get("origin") and rooms:
                key = self._report_key(user["origin"], user.get("customercode"),
                                       user.get("account"), rooms)
                if key not in seen:
                    seen.add(key)
                    jobs.append((user["origin"], user.get("customercode"),
                                 user.get("account"), rooms))
        g = self._global_cfg()
        rooms = self._norm_rooms(g.get("rooms"))
        if g.get("origin") and rooms:
            key = self._report_key(g["origin"], g.get("customercode"), g.get("account"), rooms)
            if key not in seen:
                seen.add(key)
                jobs.append((g["origin"], g.get("customercode"), g.get("account"), rooms))
        th = self._threshold()
        for origin, code, account, rooms in jobs:
            try:
                blocks, warns = [], []
                for name, rv in rooms:
                    try:
                        text, odd = await self._query_room(code, account, rv, name)
                        blocks.append(text)
                        if odd <= th:
                            warns.append(f"⚠️ {name} 剩余 {odd:g} 度，低于 {th:g} 度，请及时充值！")
                    except Exception as e:
                        blocks.append(f"❌ {name}: {e}")
                result = "\n".join(blocks)
                if warns:
                    result += "\n" + "\n".join(warns)
                await self._send(origin, result)
            except Exception as e:
                logger.error(f"[dorm_power] 定时播报失败: {e}")
