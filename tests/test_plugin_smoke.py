"""插件加载冒烟测试：用桩模块模拟 astrbot，真正 import 一次 main.py。

为什么需要：main.py 里 `@register(...)` 之类的装饰器/类结构错误，语法检查查不出来，
只有真加载才会炸。历史上就出现过 @register 挂到内部类（_Http）而不是插件类上的问题——
那种情况插件能 import 成功，但面板里一条命令都注册不出来，非常难排查。
所以这里断言：模块能加载、DormPowerPlugin 带了 @register、命令方法齐全、能实例化。

（恢复自作者本地备份，改为 pytest 收集，用例与原版一致；aiohttp/apscheduler
已安装时用真库，未安装时自动打桩。）
"""
import asyncio
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import types
from datetime import datetime, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 布局自适应：发布仓插件文件在仓库根，工作区在 astrbot_plugin_dorm_power/ 子目录
PLUGIN_DIR = ROOT if (ROOT / "main.py").is_file() else ROOT / "astrbot_plugin_dorm_power"
MAIN_PY = PLUGIN_DIR / "main.py"

registered = []


def _install_stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _install_astrbot_stub():
    """最小 astrbot 桩：只要能支撑 main.py 的模块级定义与实例化即可（不连任何真实服务）。"""
    def command(name):
        def deco(fn):
            fn.__is_command__ = name
            return fn
        return deco

    class _EventMessageType:
        ALL = "all"

    def event_message_type(_t):
        def deco(fn):
            fn.__is_event_hook__ = True
            return fn
        return deco

    if "aiohttp" not in sys.modules:
        class _ClientSession:
            closed = False

            def __init__(self, *a, **k):
                pass

            async def close(self):
                return None

        _install_stub("aiohttp", ClientSession=_ClientSession,
                      ClientTimeout=lambda **k: None, TCPConnector=lambda **k: None,
                      ClientError=Exception)

    if "apscheduler.schedulers.asyncio" not in sys.modules:
        class _Scheduler:
            def __init__(self, *a, **k):
                self.running = False
                self.jobs = []

            def add_job(self, *a, **k):
                self.jobs.append((a, k))

            def start(self):
                self.running = True

            def shutdown(self, *a, **k):
                self.running = False

        sched_pkg = _install_stub("apscheduler")
        _install_stub("apscheduler.schedulers")
        _install_stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=_Scheduler)
        sched_pkg.schedulers = sys.modules["apscheduler.schedulers"]

    api = _install_stub("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None)
    api.AstrBotConfig = dict

    event = _install_stub("astrbot.api.event")
    event.filter = types.SimpleNamespace(command=command, event_message_type=event_message_type,
                                         EventMessageType=_EventMessageType)

    class _MessageChain:
        def message(self, _t):
            return self

    event.MessageChain = _MessageChain

    star = _install_stub("astrbot.api.star")

    class Star:
        def __init__(self, context=None, config=None):
            self.context = context

    class StarTools:
        """与真实实现一致：返回绝对路径 data/plugin_data/<plugin_name> 并建好目录。"""

        @staticmethod
        def get_data_dir(plugin_name):
            path = pathlib.Path(tempfile.gettempdir()) / "dp_smoke" / "plugin_data" / str(plugin_name)
            path.mkdir(parents=True, exist_ok=True)
            return path

    def register(name, author, desc, version):
        def deco(cls):
            registered.append((cls, name, author, desc, version))
            cls.__plugin_meta__ = (name, author, desc, version)
            return cls
        return deco

    star.Star, star.register, star.Context, star.StarTools = Star, register, object, StarTools
    _install_stub("astrbot", api=api, **{})
    return registered


@pytest.fixture(scope="module")
def loaded():
    _install_astrbot_stub()
    spec = importlib.util.spec_from_file_location("dorm_power_main_smoke", str(MAIN_PY))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)   # 语法/装饰器/类结构有问题会在这里炸
    assert registered, "main.py 加载了但没有触发 @register"
    return registered, mod


@pytest.fixture(scope="module")
def plugin_cls(loaded):
    return loaded[0][0][0]


@pytest.fixture(scope="module")
def plugin(plugin_cls):
    # 无密钥也能实例化：_DummyBox / 空配置不能把构造函数搞崩
    return plugin_cls(context=object(), config=None)


def test_secure_store_shipped_with_plugin():
    assert (PLUGIN_DIR / "secure_store.py").is_file()


def test_data_paths_under_plugin_data(loaded):
    """持久化文件必须在 data/plugin_data/<plugin_name>/ 下，不能落在 data/config/。"""
    _reg, mod = loaded
    norm = mod.DATA_DIR.replace("\\", "/")
    assert norm.endswith("/plugin_data/astrbot_plugin_dorm_power"), mod.DATA_DIR
    assert norm.split("/plugin_data/")[1] == mod.PLUGIN_NAME, "目录名应与插件名一致"
    for path in (mod.USER_DATA_PATH, mod.KNOWN_CODES_PATH):
        assert "plugin_data" in path.replace("\\", "/"), path
        assert "data/config" not in path.replace("\\", "/"), path
    assert len({mod.USER_DATA_PATH, mod.KNOWN_CODES_PATH}) == 2


def test_register_and_commands(loaded):
    reg, _mod = loaded
    assert len(reg) == 1, "@register 应只注册一次"
    cls, name, _author, _desc, version = reg[0]
    assert cls.__name__ == "DormPowerPlugin", "注册的必须是插件类本身（不是内部类）"
    assert name == "dorm_power" and version.startswith("v2.")
    assert issubclass(cls, sys.modules["astrbot.api.star"].Star), "必须继承 Star"

    cmds = {fn.__is_command__ for _n, fn in vars(cls).items()
            if callable(fn) and getattr(fn, "__is_command__", None)}
    want = {"电量", "电量绑定", "电量房间", "电量添加", "电量删除",
            "电量推送", "电量解绑", "电量搜校", "电量曲线"}
    assert cmds == want, f"9 条命令应齐全，实际 {cmds}"

    assert _metadata_version() == version, "metadata.yaml 版本应与 @register 一致"


def test_scan_segment_order(loaded):
    _reg, mod = loaded
    assert mod.SCAN_SEGMENTS[0][0].startswith("新平台段") and mod.SCAN_SEGMENTS[0][1][0] == 1000000


def test_glue_hook_exists(loaded):
    cls = loaded[0][0][0]
    assert any(getattr(fn, "__is_event_hook__", False) for _n, fn in vars(cls).items())


def test_format_room_candidates(plugin_cls):
    formatted, odd = plugin_cls._format_room(
        {"modlist": [{"label": "other"}, {"surplus": "18.5"}],
         "roomfullname": "测试房间"}, "备用房间", ("surplus",))
    assert odd == 18.5 and "剩余 18.5 度" in formatted    # 非首个模块 + 候选字段
    formatted, odd = plugin_cls._format_room(
        {"balance": "7.25", "roomfullname": "顶层房间"}, "备用房间", ("balance",))
    assert odd == 7.25 and "剩余 7.25 度" in formatted    # 顶层候选字段


def test_format_room_card_layout(plugin_cls):
    """多行卡片式：一行一个信息层，近7日有汇总行 + 逐日明细行。"""
    detail = {"roomfullname": "23栋A8-1020", "odd": "213.4", "todayuse": "7",
              "weekuselist": [{"weekday": w, "dayuse": str(10 + i)}
                              for i, w in enumerate(("星期日", "星期一", "星期二", "星期三",
                                                     "星期四", "星期五", "星期六"))],
              "sumbuy": "2139.4", "collecdate": "10-20"}
    formatted, odd = plugin_cls._format_room(detail, "23栋A8-1020", ("odd",))
    lines = formatted.splitlines()
    assert odd == 213.4
    assert lines[0] == "🏠 23栋A8-1020"
    assert lines[1] == "⚡ 剩余 213.4 度 ｜ 今日 7 度"
    # 10+..+16 = 91，日均 13.0
    assert lines[2] == "📊 近7日 共91度 · 日均13.0"
    assert lines[3] == "📅 日10 一11 二12 三13 四14 五15 六16"
    assert lines[4] == "💳 累购 2139.4 度 ｜ 🗓 抄表 10-20"
    # 容错：逐日数字解析不出时整块省略，不出乱码
    bad, _odd = plugin_cls._format_room({"odd": 1, "weekuselist": [{"dayuse": "x"}]}, "r", ("odd",))
    assert "近7日" not in bad and "x" not in bad and "📅" not in bad


def test_format_room_drops_month_usage(plugin_cls):
    """monthuselist 语义无可靠依据（实测与近7日对不上），不得再拼进播报。"""
    detail = {"roomfullname": "X-101", "odd": "12.3", "todayuse": "4",
              "weekuselist": [{"weekday": "星期一", "dayuse": "5"}],
              "monthuselist": [{"monthuse": "41"}], "sumbuy": "99.9"}
    formatted, _odd = plugin_cls._format_room(detail, "X-101", ("odd",))
    assert "上月" not in formatted and "41" not in formatted, formatted
    for keep in ("剩余 12.3 度", "今日 4 度", "近7日", "📅", "累购 99.9 度"):
        assert keep in formatted, formatted


def test_rooms_config_not_monaco_editor():
    """rooms 配置不用面板的 Monaco 编辑器：它固定白底主题、不随面板明暗切换，太突兀。"""
    import json
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    rooms = schema["rooms"]
    assert rooms["type"] == "text"
    assert not rooms.get("editor_mode"), "rooms 不应启用 Monaco 编辑器"
    assert "editor_theme" not in rooms and "editor_language" not in rooms


def test_box_backend_consistency(plugin):
    box = plugin._box
    assert (box.backend_name != "none") == bool(getattr(box, "key", None))
    if box.available:
        assert box.decrypt(box.encrypt("abc")) == "abc"


def test_auto_key_generated_and_reused(loaded):
    """无任何密钥来源时应自动生成并落盘，下次直接复用；显式传参优先。

    cwd 切到空目录：工作区根目录放着自己的 .dorm_power_key，会被密钥文件查找命中，
    那是插件该有的行为，但会盖住「自动生成」分支，测试里要避开。
    """
    _reg, mod = loaded
    keyfile = os.path.join(mod.DATA_DIR, ".dorm_power_key")
    old_cwd, old_env = os.getcwd(), os.environ.pop("DORM_POWER_KEY", None)
    try:
        os.chdir(tempfile.mkdtemp())
        if os.path.isfile(keyfile):
            os.remove(keyfile)                       # 从零开始，验证自动生成
        box1 = mod.new_box(None)
        assert box1.available and box1.key, "无密钥来源时应自动生成，而不是退化为明文"
        assert os.path.isfile(keyfile), "自动生成的密钥必须落盘，否则重启后旧数据解不开"
        box2 = mod.new_box(None)
        assert box2.key == box1.key, "重启后必须复用同一把密钥"
        assert box2.decrypt(box1.encrypt("abc")) == "abc"
        assert mod.new_box("explicit").key == "explicit", "显式传入的密钥优先"
    finally:
        os.chdir(old_cwd)
        if old_env is not None:
            os.environ["DORM_POWER_KEY"] = old_env


def test_should_warn_matches_standalone(loaded):
    """预警判定应与独立模式 scheduler_job.should_warn 同语义。"""
    _reg, mod = loaded
    f = mod._should_warn
    assert f(50.0, 40.0, 20, 5, 10.0) is None, "1 度/小时的正常消耗不算异常"
    assert "掉电异常" in f(50.0, 40.0, 20, 5, 1.0), "10 度/小时要报掉电异常"
    assert "充值" in f(None, 8.0, 20, 5), "低于阈值报充值提醒"
    assert f(50.0, 40.0, 20, 5, None), "间隔未知时退回按差值保守报"
    assert f(50.0, 48.0, 20, 5, 1.0) is None, "电量上升（充值到账）不报掉电"


def test_readings_store_roundtrip(plugin, loaded):
    """上次读数持久化往返；房间主键必须是摘要，不能落明文 roomverify。"""
    _reg, mod = loaded
    try:
        plugin._save_readings({"rk_x": {"odd": 1.5, "time": "2026-10-24T08:00:00"}})
        assert plugin._load_readings()["rk_x"]["odd"] == 1.5
        tag = plugin._room_tag("101-1--11-101")
        assert tag and tag != "101-1--11-101", "读数主键必须是摘要"
        assert tag == plugin._room_tag("101-1--11-101"), "同一房间的摘要必须稳定"
    finally:
        if os.path.exists(mod.LAST_READINGS_PATH):
            os.remove(mod.LAST_READINGS_PATH)


def test_norm_capture_hours(loaded):
    """采样间隔配置：纯数字 = 每 N 小时；空串 = 关闭；缺省 = 每 2 小时。"""
    _reg, mod = loaded
    f = mod._norm_capture_hours
    assert f(None) == "*/2"
    assert f("") == ""                      # 留空 = 关闭采样（只靠每日播报记点）
    assert f("4") == "*/4"                  # 「多少小时一次」最直观的写法
    assert f("0") == "*/1"
    assert f("*/6") == "*/6"
    assert f("0,6,12,18") == "0,6,12,18"


def test_capture_schedule_config(loaded):
    """initialize 应按配置注册采样任务；留空时不注册（只留播报任务）。"""
    _reg, mod = loaded
    cls = loaded[0][0][0]

    class _Cfg(dict):
        def save_config(self):
            pass

    async def jobs_for(capture_val):
        cfg = _Cfg({"schedule": {"cron_hours": "8,20", "chart_capture_hours": capture_val},
                    "rooms": "", "account": "", "customercode": 1000000,
                    "encrypt_key": "", "notify_origin": ""})
        p = cls(context=object(), config=cfg)
        await p.initialize()
        hours = [k.get("hour") for _a, k in p.scheduler.jobs]
        return hours

    hours = asyncio.run(jobs_for("6"))
    assert hours.count("*/6") == 1 and "8,20" in hours, hours
    hours = asyncio.run(jobs_for(""))
    assert "*/2" not in hours and "*/6" not in hours and "8,20" in hours, "留空应不注册采样任务"


def test_history_roundtrip_and_prune(plugin, loaded):
    """/电量曲线 的数据源：追加、同分钟覆盖、过期裁剪。"""
    _reg, mod = loaded
    now = datetime.now()
    old = (now - timedelta(days=30)).isoformat(timespec="seconds")
    try:
        plugin._save_history({"rk_old": [[old, 9.0]]})       # 30 天前的过期数据
        plugin._append_history({"rk_a": 5.0}, now)
        plugin._append_history({"rk_a": 6.0}, now)           # 同分钟 → 覆盖不重复
        plugin._append_history({"rk_b": 7.0}, now)
        h = plugin._load_history()
        assert "rk_old" not in h, "过期摘要应被裁掉"
        assert h["rk_a"][-1][1] == 6.0, "同分钟重复采样应覆盖"
        assert h["rk_b"][-1][1] == 7.0
        assert len(h["rk_a"]) == 1
    finally:
        if os.path.exists(mod.HISTORY_PATH):
            os.remove(mod.HISTORY_PATH)


def test_render_curve_chart(loaded):
    """深色折线图：能出 PNG、尺寸正确、背景是深色。"""
    _reg, mod = loaded
    pytest.importorskip("PIL")
    font = mod._find_cjk_font(None)
    if not font:
        pytest.skip("本机没有常见中文字体")
    now = datetime.now()
    pts = [(now - timedelta(hours=h), 100.0 + (12 - h) * 1.5) for h in range(12, -1, -2)]
    out = os.path.join(mod.DATA_DIR, "curve_test.png")
    try:
        mod._render_curve_chart(out, [("测试房间", pts)], font,
                                now - timedelta(hours=12), now)
        from PIL import Image
        with Image.open(out) as im:
            assert im.size[0] == 900 and im.size[1] >= 400
            assert im.convert("RGB").getpixel((10, 10)) == (21, 23, 30), "背景应为深色"
    finally:
        if os.path.exists(out):
            os.remove(out)


def test_curve_handler(plugin, loaded):
    """/电量曲线：无数据给文本提示；有数据且找得到字体时返回图片。"""
    _reg, mod = loaded
    if not mod._find_cjk_font(None):
        pytest.skip("本机没有常见中文字体")

    class FakeEvent:
        def __init__(self):
            self.texts, self.images = [], []

        def get_sender_id(self):
            return "u1"

        def plain_result(self, t):
            self.texts.append(t)
            return ("plain", t)

        def image_result(self, p):
            self.images.append(p)
            return ("image", p)

    orig_load = plugin._load_users
    orig_users = plugin._users
    now = datetime.now()
    try:
        plugin._load_users = lambda: None   # 挡住重读文件，直接注入绑定
        plugin._users = {"u1": {"customercode": 1000145, "account": "x",
                                "rooms": [["测试房", "101-1--12-102"]], "origin": ""}}

        async def collect(ev, args):
            return [r async for r in plugin._h_curve(ev, args)]

        ev = FakeEvent()
        asyncio.run(collect(ev, []))
        assert ev.texts and not ev.images, "无数据应给文本提示"

        tag = plugin._room_tag("101-1--12-102")
        pts = [[(now - timedelta(hours=h)).isoformat(timespec="seconds"), 50.0 + (12 - h)]
               for h in range(12, -1, -2)]
        plugin._save_history({tag: pts})
        ev2 = FakeEvent()
        asyncio.run(collect(ev2, ["7"]))
        assert ev2.images and not ev2.texts, "有数据应返回图片"
        assert os.path.isfile(ev2.images[0])
        os.remove(ev2.images[0])

        ev3 = FakeEvent()
        old_pts = [[(now - timedelta(days=8)).isoformat(timespec="seconds"), 40.0]]
        plugin._save_history({tag: old_pts})   # 数据在 8 天前，3 天视图里应该没有
        asyncio.run(collect(ev3, ["3"]))
        assert ev3.texts and not ev3.images, "采样点都在范围之外时应提示无数据"
    finally:
        plugin._load_users = orig_load
        plugin._users = orig_users
        if os.path.exists(mod.HISTORY_PATH):
            os.remove(mod.HISTORY_PATH)


def test_cfg_defaults_tolerance(plugin):
    assert isinstance(plugin._global_cfg(), dict)
    assert isinstance(plugin._threshold(), float)


def test_grouped_config(loaded):
    cls = loaded[0][0][0]

    class _Cfg(dict):
        saved = 0

        def save_config(self):
            self.saved += 1

    nested = _Cfg({"schedule": {"cron_hours": "7,19", "warn_threshold": 33,
                                "warn_drop_per_hour": 2.5},
                   "scan": {"scan_qps": 9, "scan_max_hits": 5},
                   "advanced": {"odd_fields": "odd,surplus", "cmd_index": "mycmd"},
                   "account": "", "customercode": 1000000, "rooms": "",
                   "encrypt_key": "", "notify_origin": ""})
    p2 = cls(context=object(), config=nested)
    assert p2._cfg_get("cron_hours") == "7,19"                       # 分组内的值能读到
    assert p2._threshold() == 33.0                                   # _threshold 优先分组值
    assert p2._scan_opts()["qps"] == 9.0
    assert p2._odd_fields() == ("odd", "surplus")
    assert p2._cmd("cmd_index", "default") == "mycmd"
    nested["warn_threshold"] = 5                                     # 顶层残留的旧默认值
    assert p2._threshold() == 33.0, "分组值必须优先于顶层旧默认值"
    assert p2._cfg_set("warn_threshold", 44) and nested["schedule"]["warn_threshold"] == 44
    assert p2._cfg_set("brand_new", 1) and nested["brand_new"] == 1  # 未暴露的键写回顶层
    assert isinstance(p2._global_cfg(), dict)


def test_bounded_worker_scan(plugin, loaded):
    _reg, mod = loaded

    async def scan_smoke():
        probed = []

        async def fake_check(account, code):
            probed.append(code)
            await asyncio.sleep(0)
            return code == 7      # 只让 7 命中

        plugin._check_school = fake_check
        guard = mod._ScanGuard(concurrency=4, qps=1000, max_hits=1)
        hits = await plugin._probe_codes("x", list(range(500)), guard, 1)
        return hits, len(probed)

    hits, probed = asyncio.run(scan_smoke())
    # 1.3 万个代码不能建出 1.3 万个 task：命中即停，探测数远小于候选总数
    assert hits == [7] and 0 < probed < 100


def _metadata_version() -> str:
    for line in (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return ""
