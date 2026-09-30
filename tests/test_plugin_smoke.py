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

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "astrbot_plugin_dorm_power"
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

    def register(name, author, desc, version):
        def deco(cls):
            registered.append((cls, name, author, desc, version))
            cls.__plugin_meta__ = (name, author, desc, version)
            return cls
        return deco

    star.Star, star.register, star.Context = Star, register, object
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
            "电量推送", "电量解绑", "电量搜校"}
    assert cmds == want, f"8 条命令应齐全，实际 {cmds}"

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
    assert odd == 18.5 and "剩余18.5度" in formatted    # 非首个模块 + 候选字段
    formatted, odd = plugin_cls._format_room(
        {"balance": "7.25", "roomfullname": "顶层房间"}, "备用房间", ("balance",))
    assert odd == 7.25 and "剩余7.25度" in formatted    # 顶层候选字段


def test_box_backend_consistency(plugin):
    box = plugin._box
    assert (box.backend_name != "none") == bool(getattr(box, "key", None))
    if box.available:
        assert box.decrypt(box.encrypt("abc")) == "abc"


def test_no_key_degrades_to_plaintext(loaded):
    _reg, mod = loaded
    tmp = tempfile.mkdtemp()
    old_cwd = os.getcwd()
    old_env = os.environ.pop("DORM_POWER_KEY", None)
    try:
        os.chdir(tmp)
        nokey = mod.new_box(None)
        assert nokey.backend_name == "none", "无密钥时必须退化为不加密"
        assert nokey.encrypt("x") == "x" and nokey.decrypt("x") == "x"
    finally:
        os.chdir(old_cwd)
        if old_env is not None:
            os.environ["DORM_POWER_KEY"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


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
                   "account": "", "customercode": 1000145, "rooms": "",
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
