"""本地雷达航班追踪 · 主入口。

负责三件事：

1. **依赖自检**——启动前检查必需包，缺失则自动调用 pip 安装，
   让首次运行不需要用户手动配环境；
2. **高 DPI 与主题初始化**——设置缩放策略、套用军用暗色主题；
3. **创建主窗口并进入事件循环**。

运行方式::

    python main.py
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

#: 递归哨兵：子进程一旦看到它就直接退出。
#:
#: 打包后 ``sys.executable`` 就是 exe 自己，任何以它为可执行文件拉起子进程的代码路径
#: 都会变成"程序启动自己"。用哨兵从根上掐断，比逐个判断调用点是否写对更可靠——
#: 实测漏判一次就是上百个进程、系统提交内存耗尽，显卡驱动会被 Windows 停成 Code 43。
_GUARD_ENV = "LOCAL_RADAR_CHILD"

#: 运行必需的导入名 → pip 包名。两者在个别包上并不一致，故分开列出。
REQUIRED_PACKAGES: dict[str, str] = {
    "PySide6": "PySide6",
    "requests": "requests",
    "numpy": "numpy",
    "shapely": "shapely",
    "winsdk": "winsdk",
}


def missing_packages() -> list[str]:
    """返回缺失的 pip 包名列表。

    **打包成可执行文件后一律返回空**：所有依赖都已经打进去了，
    此时 ``importlib`` 的检测结果并不可靠（模块可能在压缩包里、或以不同方式注册），
    一旦误判就会走到下面的安装流程——而冻结环境下 ``sys.executable`` 指向
    exe 自己，``exe -m pip install ...`` 等于让程序反复启动自己，
    实测瞬间就能堆出上百个进程把内存吃光。
    """
    if getattr(sys, "frozen", False):
        return []

    missing: list[str] = []
    for module_name, package_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(package_name)
    return missing


def install_packages(packages: list[str]) -> bool:
    """调用 pip 安装缺失依赖。返回是否成功。

    同样地，冻结环境下直接拒绝——这时没有可信的 pip 可用，
    硬走一遍只会把程序自己当成 pip 启动。
    """
    if getattr(sys, "frozen", False):
        print("已打包运行，跳过依赖安装")
        return True

    print("检测到缺失依赖，正在自动安装：" + "、".join(packages))
    command = [sys.executable, "-m", "pip", "install", *packages]
    try:
        subprocess.check_call(command)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"\n依赖安装失败：{exc}")
        print("请手动执行：pip install -r requirements.txt")
        return False
    print("依赖安装完成。\n")
    return True


def main() -> int:
    if os.environ.get(_GUARD_ENV):
        # 本进程是被上级进程当作子进程拉起来的，立即退出，否则会无限自我复制
        return 0
    os.environ[_GUARD_ENV] = "1"

    packages = missing_packages()
    if packages and not install_packages(packages):
        return 1

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from app.config import Config
    from app.theme import Theme
    from app.ui.main_window import MainWindow

    # 必须在 QApplication 构造之前设置缩放策略，否则高 DPI 屏幕上雷达会发虚
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("本地雷达航班追踪")
    app.setApplicationDisplayName("本地雷达航班追踪")
    # Fusion 风格在各平台表现一致，也让 QSS 覆盖不到的绘制路径跟随暗色调色板
    app.setStyle("Fusion")

    config = Config()
    theme = Theme(
        font_size=config.font_size,
        scan_color=config.scan_color,
        bg_level=config.bg_level,
    )
    app.setPalette(theme.palette())
    app.setStyleSheet(theme.qss())

    window = MainWindow(config, theme)
    window.start()          # 内部会 show() 并在后台启动底图加载与定位

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
