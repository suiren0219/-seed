"""离线验证插件 main.py 里的纯解析逻辑（不 import astrbot、不发请求）。

从 main.py 源码里截取模块级函数段单独 exec，保证测试的就是在插件里跑的同一份代码。
截取边界用「纯函数区」的起止标记（API_URL -> class _ApiStatusError），
不含需要 aiohttp/astrbot 的类，这样插件里新增网络类也不会带崩测试。
"""
import re

SRC = open("astrbot_plugin_dorm_power/main.py", encoding="utf-8").read()
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

passed = 0


def check(desc, got, want):
    global passed
    assert got == want, f"{desc}: got {got!r}, want {want!r}"
    passed += 1


# --- pick_rooms：序号并集 ---
check("序号 1 2 = 全部", pick_rooms(["1", "2"], ROOMS)[0], ROOMS)
check("序号 2", pick_rooms(["2"], ROOMS)[0], [R2])
# --- 关键字 ---
check("关键字 1栋的a 单独用=多候选", pick_rooms(["1栋的a"], ROOMS)[2], True)
p, u, amb = pick_rooms(["1栋的a", "102"], ROOMS)   # 日志里的原始输入（去掉指令名）
check("1栋的a+102 命中102", (p, u, amb), ([R2], [], False))
check("关键字 101", pick_rooms(["101"], ROOMS)[0], [R1])
check("关键字 102（越界数字当关键字）", pick_rooms(["102"], ROOMS)[0], [R2])
check("关键字 a 1（关键字∩序号）", pick_rooms(["a", "1"], ROOMS)[0], [R1])
check("矛盾关键字报未匹配", pick_rooms(["101", "102"], ROOMS)[1], ["102"])
check("完全不认识的关键字", pick_rooms(["嘻嘻"], ROOMS)[1], ["嘻嘻"])
check("模糊关键字 102 只命中102", pick_rooms(["102"], ROOMS)[:2], ([R2], []))
check("模糊关键字 23 撞两间→歧义", pick_rooms(["1栋"], ROOMS)[2], True)
check("name|rv 透传不在 pick_rooms 处理", pick_rooms(["x|y"], ROOMS)[0], [])  # 调用方自行处理
# --- _parse_rooms_text ---
check("模板解析出 0 个房间", _parse_rooms_text(ROOMS_TEMPLATE), [])
check("说明行+空行+有效行", _parse_rooms_text("# 注释\n\n甲|rv1\n乙|rv2\n"), [("甲", "rv1"), ("乙", "rv2")])
check("缺 | 的行跳过", _parse_rooms_text("23东 a8 102"), [])
check("旧格式兜底行为已改：无|不再硬造房间", _parse_rooms_text("garbage"), [])
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


check("带空格", args_of("电量删除 1", "电量删除"), ["1"])
check("粘连 电量删除1", args_of("电量删除1", "电量删除"), ["1"])
check("粘连+多参数 电量添加1、2", args_of("电量添加1、2", "电量添加"), ["1", "2"])
check("粘连带空格参数 电量绑定1000145 421...", args_of("电量绑定1000145 421222", "电量绑定"), ["1000145", "421222"])
check("/ 前缀", args_of("/电量搜校 421222", "电量搜校"), ["421222"])
check("无参数", args_of("电量删除", "电量删除"), [])
check("中文逗号", args_of("电量删除，1，2", "电量删除"), ["1", "2"])
# --- 账号脱敏 ---
# 测试里一律用假号段（440000200001010000 是结构演示用的假身份证），绝不写真实号码：
# 真实号码写进测试文件就等于把个人信息留在仓库里，scan_privacy.py 会拦，但别制造机会。
FAKE_ID = "440000200001010000"
check("插件兜底脱敏：首尾各隐 4 位", plugin_mask_id(FAKE_ID), "****0020000101****")
check("插件兜底脱敏：不露原号首/尾",
      (plugin_mask_id(FAKE_ID).startswith(FAKE_ID[:4]),
       plugin_mask_id(FAKE_ID).endswith(FAKE_ID[-4:])), (False, False))
check("插件兜底脱敏：短号整体打码", plugin_mask_id("12345678"), "********")
check("插件兜底脱敏：空值安全", (plugin_mask_id(""), plugin_mask_id(None)), ("", ""))

print(f"解析逻辑 {passed} 项全部通过 ✅")
