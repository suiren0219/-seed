"""工作区整体体检（只读，不改任何文件）。

检查项：文件清单/语法/JSON/YAML/配置解密/插件目录完整性/安装包与源码一致性/
schema 与实际读取的配置项对照/版本号一致性/隐私残留/与 GitHub 克隆的差异。
用法：python check_workspace.py
"""
import ast
import hashlib
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(ROOT, "astrbot_plugin_dorm_power")
ZIP = os.path.join(ROOT, "astrbot_plugin_dorm_power.zip")
# 克隆可能在桌面上，也可能被挪进插件目录里（两种都认）
CLONE_CANDIDATES = (r"C:\Users\MECHREVO\Desktop\astrbot_plugin_dorm_power\astrbot_plugin_dorm_power",
                    os.path.join(PLUGIN, "astrbot_plugin_dorm_power"))
problems: list[str] = []
notes: list[str] = []


def ok(msg):
    print(f"  [OK] {msg}")


def bad(msg):
    problems.append(msg)
    print(f"  [!!] {msg}")


def note(msg):
    notes.append(msg)
    print(f"  [--] {msg}")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


print("[1] 文件清单")
present = []
for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
    for f in files:
        full = os.path.join(base, f)
        present.append(os.path.relpath(full, ROOT))
for p in sorted(present):
    print(f"      {os.path.getsize(os.path.join(ROOT, p)):>7}  {p}")
expected_top = {".dorm_power_key", "ANNOUNCEMENT.md", "README.md", "astrbot_plugin_dorm_power.zip",
                "cli.py", "config.example.yaml", "config.yaml", "config_loader.py",
                "dorm_power_full_backup.zip", "notifier.py", "pack_all.py", "power_service.py",
                "private_patterns.txt", "requirements.txt", "scan_privacy.py", "scheduler_job.py",
                "secure_store.py", "storage.py", "test_plugin_parsing.py", "test_plugin_smoke.py",
                "test_power.py", "test_security_scan.py", "test_v2_logic.py", "check_workspace.py"}
top = {p for p in present if os.sep not in p}
extra = top - expected_top
if extra:
    note(f"顶层多出来的文件：{sorted(extra)}")
else:
    ok("顶层文件与预期一致（无临时脚本残留）")
nested_clone = [p for p in present if p.count("astrbot_plugin_dorm_power") >= 2]
if nested_clone:
    note(f"插件目录里有一份嵌套仓库副本（{len(nested_clone)} 个文件），"
         f"打包已跳过；建议挪出插件目录以免误压进包")

print("[2] 语法与格式")
failed = False
for p in present:
    if p.endswith(".py"):
        try:
            ast.parse(open(os.path.join(ROOT, p), encoding="utf-8").read(), p)
        except SyntaxError as e:
            bad(f"{p} 语法错误: {e}")
            failed = True
if not failed:
    ok(f"{sum(1 for p in present if p.endswith('.py'))} 个 .py 语法检查通过")
for p in ("astrbot_plugin_dorm_power/_conf_schema.json", "data/history.json"):
    if p in present:
        try:
            json.load(open(os.path.join(ROOT, p), encoding="utf-8"))
        except Exception as e:
            bad(f"{p} JSON 解析失败: {e}")
ok("JSON 文件解析通过")

print("[3] 配置解密")
sys.path.insert(0, ROOT)
from config_loader import load_config            # noqa: E402
from secure_store import mask_id                # noqa: E402
cfg = load_config(os.path.join(ROOT, "config.yaml"))
stu = cfg.get("student") or {}
account = stu.get("account") or ""
if account.startswith("enc:v1:") or not account:
    bad(f"config.yaml 的账号没解出明文（当前 {account[:12]!r}）——密钥或密文有问题")
else:
    ok(f"账号解密成功且已脱敏显示：{mask_id(account)}")
rooms = stu.get("rooms") or []
if not rooms:
    bad("config.yaml 里没有房间")
for r in rooms:
    rv = r.get("roomverify") or ""
    if not rv or rv.startswith("enc:v1:"):
        bad(f"房间 {r.get('name')} 的 roomverify 没解出来")
if rooms and all(r.get("roomverify") and not r["roomverify"].startswith("enc:v1:") for r in rooms):
    ok(f"{len(rooms)} 个房间的 roomverify 都能解密")
if os.path.isfile(os.path.join(ROOT, "config.yaml")) and "account: \"enc:v1:" not in open(
        os.path.join(ROOT, "config.yaml"), encoding="utf-8").read():
    note("config.yaml 里账号不是密文？请确认是否退回明文存储")

print("[4] 插件目录完整性")
need = {"metadata.yaml", "main.py", "secure_store.py", "_conf_schema.json",
        "README.md", "ANNOUNCEMENT.md", "config.yaml"}
have = {f for f in os.listdir(PLUGIN) if os.path.isfile(os.path.join(PLUGIN, f))}
missing, surplus = need - have, have - need
if missing:
    bad(f"插件目录缺少：{sorted(missing)}")
if surplus:
    note(f"插件目录里多了：{sorted(surplus)}（不会进安装包）")
if not missing:
    ok("插件目录必需文件齐全")
plug_cfg = open(os.path.join(PLUGIN, "config.yaml"), encoding="utf-8").read()
if "account: \"\"" in plug_cfg and "rooms: []" in plug_cfg:
    ok("插件自带 config.yaml 是空模板（无个人信息）")
else:
    bad("插件自带 config.yaml 可能含个人信息，必须清空再分发")

print("[5] 安装包")
if not os.path.isfile(ZIP):
    bad("没有 astrbot_plugin_dorm_power.zip，跑一下 python pack_all.py")
else:
    z = zipfile.ZipFile(ZIP)
    names = z.namelist()
    tops = {n.split("/")[0] for n in names}
    if tops != {"astrbot_plugin_dorm_power"}:
        bad(f"包内顶层目录不是单一插件目录：{tops}")
    else:
        ok("包内顶层是单一插件目录 astrbot_plugin_dorm_power/")
    inner = {n.split("/", 1)[1] for n in names if "/" in n}
    for req in ("metadata.yaml", "main.py", "secure_store.py", "README.md"):
        if req not in inner:
            bad(f"包内缺 {req}（AstrBot 会拒收或跑不起来）")
    diff = [n for n in names if "/" in n and n.rsplit("/", 1)[-1] != "BUILD.txt"
            and hashlib.sha256(z.read(n)).hexdigest()
            != sha(os.path.join(PLUGIN, *n.split("/")[1:]))]
    if diff:
        bad(f"包内容与源码不一致（需重新打包）：{diff}")
    else:
        ok("包内文件与源码逐字节一致（含 BUILD.txt 与源码哈希）")
    if "astrbot_plugin_dorm_power/BUILD.txt" in names:
        ver = re.search(r"插件版本: (\S+)", z.read("astrbot_plugin_dorm_power/BUILD.txt").decode())
        ok(f"包内有 BUILD.txt 校验单，版本 {ver.group(1) if ver else '?'}")

print("[6] 版本号一致性")
meta = open(os.path.join(PLUGIN, "metadata.yaml"), encoding="utf-8").read()
ver_meta = re.search(r"version:\s*(\S+)", meta).group(1)
main_src = open(os.path.join(PLUGIN, "main.py"), encoding="utf-8").read()
ver_reg = re.search(r'@register\([^)]*?"(v[\d.]+)"\)', main_src).group(1)
if ver_meta == ver_reg:
    ok(f"metadata.yaml 与 @register 版本一致：{ver_meta}")
else:
    bad(f"版本不一致：metadata={ver_meta}，@register={ver_reg}（面板看不出更新）")

print("[7] schema 与实际读取的配置项")
schema = json.load(open(os.path.join(PLUGIN, "_conf_schema.json"), encoding="utf-8"))
read_keys = set(re.findall(r'_cfg_get\(\s*"([a-z_]+)"', main_src))
read_keys |= set(re.findall(r'config\.get\(\s*"([a-z_]+)"', main_src))


def schema_keys(node: dict) -> set:
    """展开 schema 的所有键：object 分组（items）里的子项同样算已暴露。"""
    out = set()
    for key, meta in (node or {}).items():
        if not isinstance(meta, dict):
            continue
        out.add(key)
        if meta.get("type") == "object":
            out |= schema_keys(meta.get("items") or {})
    return out


exposed = schema_keys(schema)
missing_schema = sorted(read_keys - exposed)
if missing_schema:
    note(f"插件会读但面板没暴露的配置项：{missing_schema}（有代码默认值，不影响运行）")
else:
    ok("插件读取的配置项都在面板 schema 里")
groups = [k for k, v in schema.items() if isinstance(v, dict) and v.get("type") == "object"]
if groups:
    nested = sum(len(schema[g].get("items") or {}) for g in groups)
    ok(f"面板按 {len(groups)} 个分组收纳 {nested} 项：{'、'.join(groups)}")

print("[8] 隐私残留")
sys.path.insert(0, ROOT)
import scan_privacy                                # noqa: E402
pat = scan_privacy.load_patterns()
leaks = []
for p in present:
    if p.endswith((".py", ".yaml", ".json", ".md", ".txt")):
        if os.path.basename(p) in {"private_patterns.txt", "config.yaml", "history.json",
                                   "schema_probe.log", ".dorm_power_key"}:
            continue
        scan_privacy.scan_text(p, open(os.path.join(ROOT, p), encoding="utf-8", errors="ignore").read(),
                               pat, leaks)
if leaks:
    for name, hit in leaks[:10]:
        bad(f"隐私残留 {name}: {hit}")
else:
    ok("工作区源码/文档无真实个人信息残留")
z = zipfile.ZipFile(ZIP)
zleaks = []
for n in z.namelist():
    if n.endswith((".py", ".yaml", ".json", ".md", ".txt")):
        scan_privacy.scan_text(n, z.read(n).decode("utf-8", "ignore"), pat, zleaks)
if zleaks:
    for name, hit in zleaks[:10]:
        bad(f"安装包隐私残留 {name}: {hit}")
else:
    ok("安装包无个人信息残留")

print("[9] 与 GitHub 克隆的一致性")
CLONE = next((p for p in CLONE_CANDIDATES if os.path.isdir(os.path.join(p, ".git"))), None)
if not CLONE:
    note("没找到克隆目录（桌面和插件目录下都没有），跳过一致性检查")
else:
    print(f"      克隆位置：{CLONE}")
    diffs = []
    for f in sorted(need):
        a, b = os.path.join(PLUGIN, f), os.path.join(CLONE, f)
        if not os.path.isfile(b) or sha(a) != sha(b):
            diffs.append(f)
    clone_extra = sorted(set(os.listdir(CLONE)) - need - {".git"})
    if diffs:
        bad(f"克隆与工作区不一致（需要重新 push）：{diffs}")
    else:
        ok("克隆里的插件文件与工作区完全一致（GitHub 已是最新）")
    if clone_extra:
        note(f"克隆目录里多出：{clone_extra}")

print("\n=== 结论 ===")
if problems:
    print(f"发现 {len(problems)} 个问题：")
    for p in problems:
        print(f"  - {p}")
else:
    print("未发现问题 ✅")
if notes:
    print(f"另有 {len(notes)} 条提示（不影响使用）：")
    for n in notes:
        print(f"  - {n}")
sys.exit(1 if problems else 0)
