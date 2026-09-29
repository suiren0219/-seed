"""隐私扫描：检查目录/zip 中是否残留个人信息。

敏感模式来自本地 private_patterns.txt（该文件本身含个人信息，勿外传）。
用法：python scan_privacy.py <目录或zip>
"""
import pathlib
import sys
import zipfile

PATTERN_FILE = pathlib.Path(__file__).with_name("private_patterns.txt")


def load_patterns() -> "re.Pattern":
    import re
    words = [w.strip() for w in PATTERN_FILE.read_text(encoding="utf-8").splitlines()
             if w.strip() and not w.lstrip().startswith("#")]
    if not words:
        # 空模式表会编译成「匹配任意位置」的正则，把每个文件都报成泄露；
        # 这种情况几乎总是误操作（模式被清空/被覆盖成说明模板），直接说清楚。
        raise SystemExit(
            f"✗ {PATTERN_FILE.name} 里没有有效模式（只有注释或空文件）。\n"
            "  请填入要拦截的本人信息片段（一行一个），再运行本脚本。")
    return re.compile("|".join(re.escape(w) for w in words))


def scan_text(name: str, text: str, pat, leaks: list):
    for m in pat.finditer(text):
        leaks.append((name, m.group(0)))


def main():
    pat = load_patterns()
    target = sys.argv[1] if len(sys.argv) > 1 else "astrbot_plugin_dorm_power"
    p = pathlib.Path(target)
    # 本地私有文件白名单：它们本来就该含个人信息（或已是密文/摘要），只提醒不分发即可
    whitelist_names = {"private_patterns.txt", "config.yaml", "history.json",
                       ".dorm_power_key", "schema_probe.log"}
    leaks: list = []
    if p.is_file() and p.suffix == ".zip":
        with zipfile.ZipFile(p) as z:
            for info in z.infolist():
                if info.filename.endswith((".py", ".yaml", ".json", ".md", ".txt")):
                    scan_text(info.filename, z.read(info).decode("utf-8", "ignore"), pat, leaks)
    else:
        for f in p.rglob("*"):
            if not f.is_file() or f.suffix not in (".py", ".yaml", ".json", ".md", ".txt", ""):
                continue
            if f.name in whitelist_names or PATTERN_FILE == f:
                continue
            scan_text(str(f), f.read_text(encoding="utf-8", errors="ignore"), pat, leaks)
    if leaks:
        print("发现个人信息残留：")
        for name, hit in leaks:
            print(f"  {name}: {hit}")
        sys.exit(1)
    print(f"✅ {target} 无个人信息残留")


if __name__ == "__main__":
    main()
