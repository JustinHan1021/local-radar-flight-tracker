"""验证核心模块在**没有 PySide6** 的解释器里也能导入。

公网部署的目标是 Linux 服务器，那儿不装 PySide6。只要哪个模块在顶层写了
``from PySide6... import ...``，服务端一启动就是 ImportError——而本机开发时
装着 PySide6，这个错误永远暴露不出来。所以这里在解释器层面装一个拦截器，
让任何 PySide6 / shiboken6 的导入直接抛异常，再逐个导入待验证的模块。

自检方式：``python tests/test_no_qt_import.py``
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from importlib.abc import MetaPathFinder

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: 待验证「无 Qt 也能导入」的模块。
#: 前半是本来就无 Qt 的基线（对照），后半是本轮把 Qt 依赖下沉之后的结果。
QT_FREE_MODULES = (
    "app.config",
    "app.geo",
    "app.airports",
    "app.places",
    "app.roads",
    "app.weather",
    "app.basemap",
    "app.terrain",
    "app.data.models",
    "app.data.sources",
    "app.data.pipeline",
)

#: 禁止出现的顶层包。PySide6 的 C 层是 shiboken6，两个都要挡。
_BANNED = ("PySide6", "shiboken6")


class _QtBlocker(MetaPathFinder):
    """任何对 PySide6 / shiboken6 的导入都直接失败。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _BANNED:
            raise ImportError(f"测试环境禁止导入 {fullname}：服务端不装 Qt")
        return None


def _is_blocked(name: str) -> bool:
    return name.split(".")[0] in _BANNED


class TestQtFreeImports(unittest.TestCase):
    def setUp(self):
        self._saved_modules = dict(sys.modules)
        # sys.modules 里已存在的模块不会再走 meta_path，必须先清掉；
        # 顺带把 PySide6 自己也可能已经导入的条目前一并清掉，拦截器才是真的生效
        for name in [n for n in sys.modules if _is_blocked(n) or n == "app" or n.startswith("app.")]:
            del sys.modules[name]

        self._blocker = _QtBlocker()
        sys.meta_path.insert(0, self._blocker)

    def tearDown(self):
        if self._blocker in sys.meta_path:
            sys.meta_path.remove(self._blocker)
        sys.modules.clear()
        sys.modules.update(self._saved_modules)

    def test_blocker_actually_raises(self):
        """先自证拦截器有效——否则后面的导入测试可能只是碰巧通过。"""
        with self.assertRaises(ImportError):
            self._blocker.find_spec("PySide6.QtCore")
        with self.assertRaises(ImportError):
            importlib.import_module("PySide6")
        with self.assertRaises(ImportError):
            importlib.import_module("shiboken6")

    def test_modules_import_without_qt(self):
        for name in QT_FREE_MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_nothing_leaked_qt_into_sys_modules(self):
        """导入完成之后，sys.modules 里不该出现任何 Qt 模块。"""
        for name in QT_FREE_MODULES:
            importlib.import_module(name)
        leaked = [n for n in sys.modules if _is_blocked(n)]
        self.assertEqual(leaked, [], f"这些模块把 Qt 拖了进来：{leaked}")

    def test_geo_math_still_works(self):
        """导入只是第一步：随便验一个纯计算接口，确认模块是真能用的。"""
        geo = importlib.import_module("app.geo")
        self.assertAlmostEqual(geo.haversine_km(0.0, 0.0, 0.0, 1.0), 111.19, delta=0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
