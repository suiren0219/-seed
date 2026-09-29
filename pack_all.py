"""打包脚本：生成两个 zip。

1. `astrbot_plugin_dorm_power.zip` —— 插件安装包，只含插件目录里可外发的文件
   （已脱敏，无个人信息，可直接传 AstrBot 面板 / 发给别人）。
2. `dorm_power_full_backup.zip` —— 整包备份，含 config.yaml / .dorm_power_key /
   data/ 等本地私有文件，**勿外传**。

注意：AstrBot 面板上传插件 zip 时要求包内含 README.md，否则报错拒收。
"""
import hashlib
import os
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(ROOT, "astrbot_plugin_dorm_power")
PLUGIN_ZIP = os.path.join(ROOT, "astrbot_plugin_dorm_power.zip")
BACKUP_ZIP = os.path.join(ROOT, "dorm_power_full_backup.zip")

# 插件安装包里必须有的文件；缺失会导致面板拒收或插件跑不起来
PLUGIN_FILES = ("metadata.yaml", "main.py", "secure_store.py", "_conf_schema.json",
                "README.md", "ANNOUNCEMENT.md", "config.yaml")

EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv"}


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _build_manifest() -> str:
    """出厂校验单：打包时间 + 版本 + 每个文件的 sha256。

    为什么需要：zip 是压缩过的，改了几千行代码体积可能只差几百字节，
    光看大小分不清新旧包。装之前对一下这里的时间/哈希就能确认拿到的是哪一版。
    """
    lines = ["# 本包由 pack_all.py 生成，可用于核对版本（不是插件运行时需要的文件）",
             f"打包时间: {datetime.now().isoformat(timespec='seconds')}"]
    meta_path = os.path.join(PLUGIN_DIR, "metadata.yaml")
    if os.path.isfile(meta_path):
        for line in open(meta_path, encoding="utf-8"):
            if line.startswith("version:"):
                lines.append(f"插件版本: {line.split(':', 1)[1].strip()}")
    lines.append("")
    lines.append("文件大小 / sha256：")
    for name in PLUGIN_FILES:
        full = os.path.join(PLUGIN_DIR, name)
        lines.append(f"  {os.path.getsize(full):>7}  {_sha256(full)[:32]}  {name}")
    return "\n".join(lines) + "\n"


def _write_zip(out: str, entries) -> list[str]:
    tmp = out + ".tmp"
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for full, arc in entries:
            z.write(full, arc)
    os.replace(tmp, out)
    with zipfile.ZipFile(out) as z:
        return z.namelist()


def pack_plugin() -> None:
    missing = [f for f in PLUGIN_FILES if not os.path.isfile(os.path.join(PLUGIN_DIR, f))]
    if missing:
        raise SystemExit(f"✗ 插件目录缺少必需文件：{', '.join(missing)}")
    entries = [(os.path.join(PLUGIN_DIR, name),
                os.path.join("astrbot_plugin_dorm_power", name)) for name in PLUGIN_FILES]
    manifest_arc = os.path.join("astrbot_plugin_dorm_power", "BUILD.txt")

    tmp = PLUGIN_ZIP + ".tmp"
    if os.path.exists(PLUGIN_ZIP):
        os.remove(PLUGIN_ZIP)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for full, arc in entries:
            z.write(full, arc)
        z.writestr(manifest_arc, _build_manifest())
    os.replace(tmp, PLUGIN_ZIP)

    with zipfile.ZipFile(PLUGIN_ZIP) as z:
        names = z.namelist()
    print(f"插件安装包: {PLUGIN_ZIP}")
    print(f"  {os.path.getsize(PLUGIN_ZIP) / 1024:.1f} KB，{len(names)} 个文件，"
          f"zip sha256 {_sha256(PLUGIN_ZIP)[:16]}…")
    for n in sorted(names):
        print("  ", n)


def pack_backup() -> None:
    entries = []
    for base, dirs, files in os.walk(ROOT):
        # 跳过缓存目录，以及任何「自己就是一个 git 仓库」的子目录
        # （比如有人在工作区里又 clone 了一份仓库，别把副本和它的 .git 打进备份）
        dirs[:] = [d for d in dirs
                   if d not in EXCLUDE_DIRS
                   and not os.path.isdir(os.path.join(base, d, ".git"))]
        for f in files:
            full = os.path.join(base, f)
            if full.endswith(".zip"):
                continue  # 别把上一个包塞进新包（越滚越大）
            entries.append((full, os.path.relpath(full, ROOT)))
    names = _write_zip(BACKUP_ZIP, entries)
    print(f"整包备份: {BACKUP_ZIP}（{os.path.getsize(BACKUP_ZIP) / 1024:.1f} KB，{len(names)} 个文件，含个人信息勿外传）")


if __name__ == "__main__":
    pack_plugin()
    pack_backup()
