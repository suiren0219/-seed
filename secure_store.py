"""加载垫片：加密实现的唯一一份在 astrbot_plugin_dorm_power/secure_store.py。

插件目录必须自包含（AstrBot 只拷贝该子目录），因此真实实现放在那里维护；
独立命令行版（cli.py / config_loader.py / storage.py 等）通过本垫片共用同一份，
避免两边各留一个拷贝改着改着就分叉。

被 import 时：加载真实实现并替换 sys.modules 里的自己，`from secure_store import mask_id`
等写法不受影响；直接运行（python secure_store.py genkey）时：真实文件末尾的 CLI
入口会接管执行，用法与以前完全一致。
"""
import importlib.util as _ilu
import os as _os
import sys as _sys

_REAL = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                      "astrbot_plugin_dorm_power", "secure_store.py")


def _load():
    spec = _ilu.spec_from_file_location(__name__, _REAL)
    mod = _ilu.module_from_spec(spec)
    if __name__ != "__main__":  # 作为脚本跑时不替换 __main__，CLI 由真实文件自己接管
        _sys.modules[__name__] = mod
    spec.loader.exec_module(mod)
    return mod


_load()
