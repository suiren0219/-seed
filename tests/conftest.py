"""pytest 根配置：把仓库根目录加入 sys.path，让测试能按顶层模块导入根目录代码。

插件 main.py 的加载不依赖这里的桩——test_plugin_smoke.py 自带完整的 astrbot 桩；
其他测试用「源码分段 exec」的方式直接测插件文件里的真实代码，不重复维护拷贝。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
