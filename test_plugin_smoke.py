"""插件加载冒烟测试：用桩模块模拟 astrbot，真正 import 一次 main.py。

为什么需要：main.py 里 `@register(...)` 之类的装饰器/类结构错误，语法检查查不出来，
只有真加载才会炸。历史上就出现过 @register 挂到内部类（_Http）而不是插件类上的问题——
那种情况插件能 import 成功，但面板里一条命令都注册不出来，非常难排查。
所以这里断言：模块能加载、DormPowerPlugin 带了 @register、命令方法齐全、能实例化。

运行：python test_plugin_smoke.py
"""
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import types

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "astrbot_plugin_dorm_power")
MAIN_PY = os.path.join(PLUGIN_DIR, "main.py")

passed = 0


def check(desc, cond):
    global passed
    assert cond, f"✗ {desc}"
    passed += 1
    print(f"  [OK] {desc}")


def _install_stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _install_astrbot_stub():
    """最小 astrbot / aiohttp / apscheduler 桩：只要能支撑 main.py 的模块级定义与实例化即可
    （不连任何真实服务）。本机没装这两个第三方库，所以桩是必需的。"""
    registered = []

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


def main():
    print("[插件加载冒烟]")
    check("secure_store.py 与插件同目录（安装包必须带上）",
          os.path.isfile(os.path.join(PLUGIN_DIR, "secure_store.py")))

    registered = _install_astrbot_stub()
    spec = importlib.util.spec_from_file_location("dorm_power_main_smoke", MAIN_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)   # 语法/装饰器/类结构有问题会在这里炸
    check("main.py 能正常 import", True)

    check("@register 只注册了一次", len(registered) == 1)
    cls, name, author, desc, version = registered[0]
    check(f"注册的是插件类本身（不是内部类）：{cls.__name__}", cls.__name__ == "DormPowerPlugin")
    check(f"注册名与版本：{name} / {version}", name == "dorm_power" and version.startswith("v2."))

    # 插件类必须继承 Star，否则 AstrBot 不会把它当插件实例化
    star_mod = sys.modules["astrbot.api.star"]
    check("DormPowerPlugin 继承 Star", issubclass(cls, star_mod.Star))

    # 命令方法齐全（装饰器标在方法上，缺一个用户就少一条指令）
    cmds = {fn.__is_command__ for _n, fn in vars(cls).items()
            if callable(fn) and getattr(fn, "__is_command__", None)}
    want = {"电量", "电量绑定", "电量房间", "电量添加", "电量删除",
            "电量推送", "电量解绑", "电量搜校"}
    check(f"8 条命令齐全：{len(cmds)}", cmds == want)
    check("搜校分段先扫新平台段（默认 1000000 所在段）",
          mod.SCAN_SEGMENTS[0][0].startswith("新平台段") and mod.SCAN_SEGMENTS[0][1][0] == 1000000)
    check("粘连兜底监听器存在（event_message_type 钩子）",
          any(getattr(fn, "__is_event_hook__", False) for _n, fn in vars(cls).items()))
    check("插件文件里的 metadata 版本与 @register 一致",
          _metadata_version() == version)

    # 无密钥也能实例化：_DummyBox / 空配置不能把构造函数搞崩
    plugin = cls(context=object(), config=None)
    check("无配置、无密钥时可实例化", plugin is not None)
    formatted, odd = cls._format_room(
        {"modlist": [{"label": "other"}, {"surplus": "18.5"}],
         "roomfullname": "测试房间"}, "备用房间", ("surplus",))
    check("电量解析支持非首个模块和候选字段", odd == 18.5 and "剩余18.5度" in formatted)
    formatted, odd = cls._format_room(
        {"balance": "7.25", "roomfullname": "顶层房间"}, "备用房间", ("balance",))
    check("电量解析支持顶层候选字段", odd == 7.25 and "剩余7.25度" in formatted)
    box = plugin._box
    check(f"加密后端可用性正确（{box.backend_name}）",
          (box.backend_name != "none") == bool(getattr(box, "key", None)))
    if box.available:
        check("加密后端往返一致", box.decrypt(box.encrypt("abc")) == "abc")

    # 真·无密钥环境（临时 cwd + 清空环境变量）：必须退化成明文，而不是抛异常
    tmp = tempfile.mkdtemp()
    old_cwd = os.getcwd()
    old_env = os.environ.pop("DORM_POWER_KEY", None)
    try:
        os.chdir(tmp)
        nokey = mod.new_box(None)
        check("无密钥时退化为不加密（backend=none）", nokey.backend_name == "none")
        check("不加密时 encrypt/decrypt 原样返回", nokey.encrypt("x") == "x" and nokey.decrypt("x") == "x")
    finally:
        os.chdir(old_cwd)
        if old_env is not None:
            os.environ["DORM_POWER_KEY"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)

    # 空 config 时各项读取要有默认值，不能抛
    check("_global_cfg 容错（无 config.yaml）", isinstance(plugin._global_cfg(), dict))
    check("_threshold 容错", isinstance(plugin._threshold(), float))

    # 面板把 object 分组保存成嵌套字典：读配置必须两种形状都认
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
    check("分组配置：cron_hours 读到分组内的值", p2._cfg_get("cron_hours") == "7,19")
    check("分组配置：_threshold 优先分组内的 warn_threshold", p2._threshold() == 33.0)
    check("分组配置：搜校参数从分组读到", p2._scan_opts()["qps"] == 9.0)
    check("分组配置：电量字段候选按配置覆盖",
          p2._odd_fields() == ("odd", "surplus"))
    check("分组配置：cmd 名可覆盖", p2._cmd("cmd_index", "default") == "mycmd")
    # 顶层残留的旧默认值不能盖住用户真正设置的分组值
    nested["warn_threshold"] = 5
    check("分组值优先于顶层旧默认值", p2._threshold() == 33.0)
    check("写回配置落在分组里", p2._cfg_set("warn_threshold", 44)
          and nested["schedule"]["warn_threshold"] == 44)
    check("未暴露的键仍可写回顶层", p2._cfg_set("brand_new", 1) and nested["brand_new"] == 1)
    check("分组模式 _global_cfg 不抛", isinstance(p2._global_cfg(), dict))

    # 搜校 worker 池：1.3 万个代码不能建出 1.3 万个 task
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
    check(f"有界 worker 扫描命中即停：hits={hits}，共探测 {probed} 个（远小于 500）",
          hits == [7] and 0 < probed < 100)

    print(f"\n插件加载冒烟 {passed} 项全部通过 ✅")


def _metadata_version() -> str:
    for line in open(os.path.join(PLUGIN_DIR, "metadata.yaml"), encoding="utf-8"):
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return ""


if __name__ == "__main__":
    main()
