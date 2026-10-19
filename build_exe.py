"""把程序打包成 Windows 可执行文件。

用法::

    python build_exe.py              # 打包成单文件 exe（推荐分发用）
    python build_exe.py --onedir     # 打包成目录（启动更快，便于排查）
    python build_exe.py --console    # 保留控制台窗口（排查启动问题时用）

打包说明
--------
* 程序运行时要下载底图、机场、地名等数据（实测约 81 MB，25→33 个文件），这些**不打进 exe**，
  首次运行时自动下载到 ``%LOCALAPPDATA%\\LocalRadarFlightTracker``。
  这样 exe 体积可控，数据也能随用随更新。
* 打包后 ``app/config.py`` 会自动把数据根目录切到上述用户目录——
  onefile 模式的解包目录会被清空，装在 Program Files 下程序目录又是只读的。
* 排除了一大批用不到的 Qt 模块（WebEngine / Qml / 3D 等），
  否则体积会翻好几倍。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "main.py"
ICON = ROOT / "assets" / "icons" / "app.ico"
APP_NAME = "本地雷达航班追踪"

#: 这些模块 PyInstaller 不一定能自动发现（动态导入或纯 C 扩展）
HIDDEN_IMPORTS = [
    "PySide6.QtSvg",
    "PySide6.QtNetwork",
    "PySide6.QtPrintSupport",
    "shapely",
    "shapely.geometry",
    "shapely.speedups",
    "numpy",
    "PIL.Image",
    "winsdk.windows.devices.geolocation",
]

#: 需要整包收集的模块。
#:
#: PyInstaller 6.10 自带的 numpy hook 跟不上 numpy 2.x 的目录布局——
#: ``numpy/_core/`` 下的扩展模块会被漏收（实测漏了 ``_exceptions``），
#: 症状是启动即 ``No module named 'numpy._core._exceptions'``，
#: 再被包成 PyInstaller 的 "Unhandled exception in script" 对话框（窗口无任何提示）。
#: 用 ``--collect-all`` 把子模块/二进制/数据一并收进来，不依赖 hook 的判断。
COLLECT_ALL = [
    "numpy",
]

#: 用不到的大块头，排掉能显著减小体积
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtTest",
    "matplotlib",
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
    # numba / llvmlite 本体项目从未 import，但它们若装在环境里
    # PyInstaller 会当成潜在依赖收进来——单是 llvmlite.dll 就占 101.7 MB。
    # 这是打包体积最大的一处浪费，实测排除后 exe 目录从 331 MB 降到 230 MB。
    "numba",
    "llvmlite",
    "IPython",
    "pytest",
    # 纯 QWidget + QPainter 走光栅引擎，不需要软件 OpenGL 回退实现
    "OpenGL",
]

#: 需要显式排除的二进制/数据文件（--exclude-module 管不到这些）。
#:
#: ``opengl32sw.dll`` 是 Qt 的软件 OpenGL 回退实现，19.7 MB。
#: 本程序是 QWidget + QPainter 光栅绘制，不创建 OpenGL 上下文，用不到它。
EXCLUDE_DLLS = [
    "opengl32sw.dll",
]


def build(onefile: bool = True, console: bool = False) -> int:
    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        print("未安装 PyInstaller，请先执行：pip install pyinstaller")
        return 1

    if not ENTRY.exists():
        print(f"找不到入口文件：{ENTRY}")
        return 1

    args = [
        str(ENTRY),
        f"--name={APP_NAME}",
        "--noconfirm",
        "--clean",
        "--onefile" if onefile else "--onedir",
        "--console" if console else "--windowed",
        f"--distpath={ROOT / 'dist'}",
        f"--workpath={ROOT / 'build'}",
        f"--specpath={ROOT / 'build'}",
    ]

    if ICON.exists():
        args.append(f"--icon={ICON}")
    else:
        print("提示：未找到图标，将使用默认图标")

    for module in HIDDEN_IMPORTS:
        args.append(f"--hidden-import={module}")
    for module in COLLECT_ALL:
        args.append(f"--collect-all={module}")
    for module in EXCLUDES:
        args.append(f"--exclude-module={module}")

    print("=" * 70)
    print(f"开始打包（{'单文件' if onefile else '目录'}模式）")
    print("=" * 70)
    print("  " + " ".join(args[:6]) + " …")
    print()

    pyinstaller.run(args)

    target = (ROOT / "dist" / f"{APP_NAME}.exe") if onefile \
        else (ROOT / "dist" / APP_NAME / f"{APP_NAME}.exe")

    trimmed = _trim_binaries(ROOT / "dist")

    print()
    print("=" * 70)
    if target.exists():
        size_mb = target.stat().st_size / 1048576
        print(f"打包成功：{target}")
        print(f"  主程序 {size_mb:.0f} MB")
        if trimmed:
            print(f"  已清理 {len(trimmed)} 个用不到的二进制：")
            for name, mb in trimmed:
                print(f"    {name}  {mb:.1f} MB")
        total = sum(f.stat().st_size for f in (ROOT / "dist").rglob("*") if f.is_file())
        print(f"  目录总计 {total / 1048576:.0f} MB")
        if onefile:
            print("  单文件模式首次启动需解包，约需 5–15 秒，之后会快一些；")
            print("  若觉得启动慢，可改用 --onedir 模式。")
        print()
        print("  数据目录：%LOCALAPPDATA%\\LocalRadarFlightTracker")
        print("  首次运行会自动下载底图 / 机场 / 地名（约 81 MB）。")
    else:
        print(f"打包似乎未成功，未找到：{target}")
        return 1
    return 0


def _trim_binaries(dist: Path) -> list[tuple[str, float]]:
    """删除 PyInstaller 收进来但运行时用不到的二进制。

    ``--exclude-module`` 只管 Python 模块，管不到已被复制进产物目录的 DLL，
    所以这里做一次后处理。删掉的文件都有明确理由（见 EXCLUDE_DLLS），
    且打包后应当实跑一次确认没删错。
    """
    removed: list[tuple[str, float]] = []
    if not dist.exists():
        return removed
    for name in EXCLUDE_DLLS:
        for path in dist.rglob(name):
            try:
                size_mb = path.stat().st_size / 1048576
                path.unlink()
                removed.append((name, size_mb))
            except OSError as exc:
                print(f"  警告：无法删除 {path}（{exc}）")
    return removed


def clean() -> None:
    for name in ("build", "dist"):
        target = ROOT / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            print(f"已清理 {target}")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean()
        sys.exit(0)
    sys.exit(build(
        onefile="--onedir" not in sys.argv,
        console="--console" in sys.argv,
    ))
