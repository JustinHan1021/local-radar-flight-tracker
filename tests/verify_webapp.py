"""一条命令跑完那些**还没被真正验证过**的东西。

背景：负责底图与 Qt 惰性化的子代理，其 shell 被权限规则拒绝了，所以三件事
它写了代码但一次都没跑过。这个脚本把它们串起来：

    python tests/verify_webapp.py

三项各自针对一种不同的失效模式，缺一不可：

1. **Qt 惰性化** —— 必须在*没有 PySide6* 的环境里 import 成功。
   否则公网部署（Linux，不装 PySide6）会在启动时直接挂。
2. **桌面版没被改坏** —— 只验 `import` 是不够的。Qt 导入被移进了
   `Basemap.paint()` / `terrain.elevation_color()` 这类函数体，
   写错名字在 import 阶段完全不报错，**只在真正绘制那一刻才炸**。
   所以要离屏真的画一张出来。
3. **底图能出图** —— 能出图不等于画对了。所以这里只报尺寸与像素统计，
   图写到临时目录，由人（或带视觉的模型）去看。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(tempfile.gettempdir())

CENTER = (22.30314, 114.178832)  # 香港，开发机上数据已下好
LAYERS = ("terrain", "roads", "clouds", "places", "airports")


# ----------------------------------------------------------------------
# 子模式：离屏绘制桌面版底图（真正触发被移进函数体的 Qt 导入）
# ----------------------------------------------------------------------


def _paint_child() -> int:
    # 用 `python tests/verify_webapp.py` 跑时 sys.path[0] 是 tests/ 而不是项目根，
    # 子进程里 import app 会直接失败。父进程 cwd 是项目根，但那只影响相对路径、
    # 不进 sys.path，所以必须显式插进去。
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from app.basemap import Basemap, _color
    from app.geo import Projector
    from app.terrain import elevation_color

    app = QApplication.instance() or QApplication([])
    del app

    # 这一行就够触发 basemap 的惰性 QColor 导入
    swatch = _color("#3FE08A", 0.5)
    if swatch.alphaF() <= 0:
        print(json.dumps({"ok": False, "error": "_color 返回了全透明色"}))
        return 1

    # 地形色标的惰性 QColor 导入
    for height in (0.0, 600.0, 3000.0, 6500.0):
        color = elevation_color(height)
        if not color.isValid():
            print(json.dumps({"ok": False, "error": f"elevation_color({height}) 无效"}))
            return 1

    basemap = Basemap()
    if not basemap.load():
        print(json.dumps({"ok": False, "error": "底图数据未就绪，无法绘制"}))
        return 1

    size = 700
    projector = Projector(
        CENTER[0], CENTER[1], 50.0, size / 2, size / 2, size / 2
    )
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    painter = QPainter(image)
    # theme 参数在 paint() 体内并未被使用（配色由 app.theme 的模块常量直接取），
    # 所以传 None 是安全的；若将来改成用 theme，这里会立刻报错而不是静默出错。
    basemap.paint(painter, projector, None, size / 2)
    painter.end()

    target = OUT_DIR / "verify_desktop_basemap.png"
    if not image.save(str(target)):
        print(json.dumps({"ok": False, "error": "QImage.save 失败"}))
        return 1

    rings, points = basemap.ring_counts, basemap.point_counts
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(target),
                "size": size,
                "rings": rings,
                "points": points,
            },
            ensure_ascii=False,
        )
    )
    return 0


# ----------------------------------------------------------------------
# 子模式：渲染 Web 底图 PNG
# ----------------------------------------------------------------------


def _render_child() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from PIL import Image

    from webapp.render import render_basemap

    data = render_basemap(
        CENTER[0], CENTER[1], 50.0, 768, frozenset(LAYERS)
    )
    if not data:
        print(json.dumps({"ok": False, "error": "render_basemap 返回空"}))
        return 1

    target = OUT_DIR / "verify_web_basemap.png"
    target.write_bytes(data)

    with Image.open(target) as image:
        converted = image.convert("RGB")
        stats = {
            "mode": image.mode,
            "size": list(image.size),
            "bytes": len(data),
            "distinct_colors": len(converted.getcolors(maxcolors=1 << 20) or []),
            "mean_rgb": [round(v, 1) for v in converted.resize((1, 1)).getpixel((0, 0))],
        }

    print(json.dumps({"ok": True, "path": str(target), "stats": stats}, ensure_ascii=False))
    return 0


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def _run(args, extra_env=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    # 1. 无 Qt 环境下的可导入性
    proc = _run([sys.executable, str(ROOT / "tests" / "test_no_qt_import.py")])
    ok = proc.returncode == 0
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    results.append(("Qt 惰性化：无 PySide6 时可导入", ok, tail[-1] if tail else ""))

    # 2. 桌面版未被改坏（import 层）
    proc = _run([sys.executable, "-c", "import app.ui.main_window; print('OK')"])
    results.append(
        (
            "桌面版 UI 仍可导入",
            proc.returncode == 0 and "OK" in proc.stdout,
            (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or "",
        )
    )

    # 3. 桌面版真的画一次（才是惰性导入的真正考验）
    proc = _run([sys.executable, str(Path(__file__).resolve()), "--paint-child"])
    if proc.returncode == 0:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        results.append(
            (
                "桌面版底图可离屏绘制",
                info.get("ok", False),
                f"{info.get('path', '')} rings={info.get('rings')}",
            )
        )
    else:
        results.append(
            (
                "桌面版底图可离屏绘制",
                False,
                (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or "",
            )
        )

    # 4. Web 底图渲染
    proc = _run([sys.executable, str(Path(__file__).resolve()), "--render-child"])
    if proc.returncode == 0 and proc.stdout.strip():
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        results.append(
            (
                "Web 底图可渲染出 PNG",
                info.get("ok", False),
                f"{info.get('path', '')} {info.get('stats', '')}",
            )
        )
    else:
        results.append(
            (
                "Web 底图可渲染出 PNG",
                False,
                (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or "",
            )
        )

    # 5. 前端语法
    node = os.environ.get("MIMO_NODE") or shutil.which("node")
    app_js = ROOT / "webapp" / "static" / "app.js"
    if not node:
        results.append(("app.js 语法检查", False, "找不到 node（MIMO_NODE 未设且 PATH 无 node）"))
    elif not app_js.is_file():
        results.append(("app.js 语法检查", False, "app.js 不存在"))
    else:
        proc = _run([node, "--check", str(app_js)])
        results.append(
            ("app.js 语法检查", proc.returncode == 0, (proc.stderr or "ok").strip()[:200])
        )

    print()
    print("=" * 72)
    failed = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  {mark}  {name}")
        if detail:
            text = detail if isinstance(detail, str) else " ".join(detail)
            print(f"        {text[:160]}")
    print("=" * 72)
    print(f"  {len(results) - failed} passed, {failed} failed")
    print()
    print("  底图 PNG 已写到 %TEMP%，请人眼确认投影与配色：")
    print(f"    {OUT_DIR / 'verify_desktop_basemap.png'}")
    print(f"    {OUT_DIR / 'verify_web_basemap.png'}")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--paint-child" in sys.argv:
        sys.exit(_paint_child())
    if "--render-child" in sys.argv:
        sys.exit(_render_child())
    sys.exit(main())
