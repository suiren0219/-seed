"""离线验证插件 main.py 里的纯解析逻辑（不 import astrbot、不发请求）。

从 main.py 源码里截取模块级函数段单独 exec，保证测试的就是在插件里跑的同一份代码。
截取边界用「纯函数区」的起止标记（API_URL -> class _ApiStatusError），
不含需要 aiohttp/astrbot 的类，这样插件里新增网络类也不会带崩测试。

（恢复自作者本地备份，改为 pytest 收集，用例与原版一致。）
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "astrbot_plugin_dorm_power" / "main.py").read_text(encoding="utf-8")
START = SRC.index("API_URL =")
END = SRC.index("class _ApiStatusError")
ns = {"re": re}
exec(compile(SRC[START:END], "main_extract", "exec"), ns)

_parse_rooms_text = ns["_parse_rooms_text"]
_norm_text = ns["_norm_text"]
pick_rooms = ns["pick_rooms"]
ROOMS_TEMPLATE = ns["ROOMS_TEMPLATE"]

# 插件里的 mask_id 兜底实现（secure_store.py 缺失时走这条分支）单独取出来验证，
# 它必须和 secure_store.mask_id 给出同样的脱敏结果，否则日志里的账号会漏原号段。
_mask_ns = {"secure_store": None}
exec(compile(SRC[SRC.index("def mask_id("):START], "mask_extract", "exec"), _mask_ns)
plugin_mask_id = _mask_ns["mask_id"]

ROOMS = [
    ("主分区_1栋A_9层_101", "101-1--11-101"),
    ("主分区_1栋A_10层_102", "101-1--12-102"),
]
R1, R2 = ROOMS


# --- pick_rooms：序号并集 ---
def test_pick_rooms_union_of_indexes():
    assert pick_rooms(["1", "2"], ROOMS)[0] == ROOMS
    assert pick_rooms(["2"], ROOMS)[0] == [R2]


# --- 关键字 ---
def test_pick_rooms_keywords():
    assert pick_rooms(["1栋的a"], ROOMS)[2] is True          # 单独的关键字命中多候选 → 歧义
    p, u, amb = pick_rooms(["1栋的a", "102"], ROOMS)          # 日志里的原始输入（去掉指令名）
    assert (p, u, amb) == ([R2], [], False)
    assert pick_rooms(["101"], ROOMS)[0] == [R1]
    assert pick_rooms(["102"], ROOMS)[0] == [R2]              # 越界数字当关键字
    assert pick_rooms(["a", "1"], ROOMS)[0] == [R1]           # 关键字 ∩ 序号
    assert pick_rooms(["101", "102"], ROOMS)[1] == ["102"]    # 矛盾关键字报未匹配
    assert pick_rooms(["嘻嘻"], ROOMS)[1] == ["嘻嘻"]          # 完全不认识的关键字
    assert pick_rooms(["102"], ROOMS)[:2] == ([R2], [])
    assert pick_rooms(["1栋"], ROOMS)[2] is True              # 撞两间 → 歧义
    assert pick_rooms(["x|y"], ROOMS)[0] == []                # name|rv 透传不在 pick_rooms 处理


# --- _parse_rooms_text ---
def test_parse_rooms_text():
    assert _parse_rooms_text(ROOMS_TEMPLATE) == []             # 模板解析出 0 个房间
    assert _parse_rooms_text("# 注释\n\n甲|rv1\n乙|rv2\n") == [("甲", "rv1"), ("乙", "rv2")]
    assert _parse_rooms_text("23东 a8 102") == []               # 缺 | 的行跳过
    assert _parse_rooms_text("garbage") == []                   # 旧格式兜底行为已改：无|不再硬造房间


# --- 粘连/分隔解析（复刻 _args 的核心分支，模拟 event） ---
class FakeEvent:
    def __init__(self, s):
        self.message_str = s


def args_of(text, cmd):
    text = (text or "").strip().lstrip("/").strip()
    if text.startswith(cmd):
        rest = text[len(cmd):]
    elif text.split() and text.split()[0].startswith(cmd):
        tokens = text.split()
        rest = tokens[0][len(cmd):] + " " + " ".join(tokens[1:])
    else:
        rest = ""
    return [a for a in re.split(r"[\s,，、]+", rest) if a]


def test_args_glued_and_separators():
    assert args_of("电量删除 1", "电量删除") == ["1"]
    assert args_of("电量删除1", "电量删除") == ["1"]                       # 粘连
    assert args_of("电量添加1、2", "电量添加") == ["1", "2"]                # 粘连 + 顿号
    assert args_of("电量绑定1000145 421222", "电量绑定") == ["1000145", "421222"]
    assert args_of("/电量搜校 421222", "电量搜校") == ["421222"]            # / 前缀
    assert args_of("电量删除", "电量删除") == []                            # 无参数
    assert args_of("电量删除，1，2", "电量删除") == ["1", "2"]              # 中文逗号


# --- 账号脱敏 ---
# 测试里一律用假号段（440000200001010000 是结构演示用的假身份证），绝不写真实号码：
# 真实号码写进测试文件就等于把个人信息留在仓库里，scan_privacy.py 会拦，但别制造机会。
FAKE_ID = "440000200001010000"


def test_plugin_mask_id_fallback():
    assert plugin_mask_id(FAKE_ID) == "****0020000101****"   # 首尾各隐 4 位
    assert not plugin_mask_id(FAKE_ID).startswith(FAKE_ID[:4])
    assert not plugin_mask_id(FAKE_ID).endswith(FAKE_ID[-4:])
    assert plugin_mask_id("12345678") == "********"          # 短号整体打码
    assert plugin_mask_id("") == "" and plugin_mask_id(None) == ""
