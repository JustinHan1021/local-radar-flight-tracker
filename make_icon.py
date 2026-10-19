"""生成程序图标（雷达准星 + 扫描扇形）。

用 Pillow 现画，不依赖任何外部素材；输出的 .ico 供 PyInstaller 使用。
配色与界面主题保持一致（深炭黑底 + 亮绿扫描线）。
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "assets" / "icons" / "app.ico"

# 与 app/theme.py 保持一致的配色
BG = (11, 14, 15, 255)
DISH = (13, 21, 18, 255)
SCAN = (63, 224, 138, 255)
GLOW = (63, 224, 138, 70)
RING = (63, 224, 138, 46)
AIRCRAFT = (232, 197, 71, 255)

#: 需要的尺寸（Windows 会按场合挑选）
SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_icon(size: int) -> Image.Image:
    """按给定边长绘制一张图标。所有几何尺寸都按比例缩放。"""
    # 先在大画布上画再缩小，边缘更干净
    scale = 4
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")

    unit = canvas_size / 100.0          # 以 100 为设计单位
    center = canvas_size / 2.0
    dish_radius = 42.0 * unit

    # 盘底（带圆角感的实心圆）
    draw.ellipse(
        [center - dish_radius, center - dish_radius,
         center + dish_radius, center + dish_radius],
        fill=DISH,
    )

    # 扫描扇形：从正北顺时针 70°，用若干层细扇形叠出渐变拖尾。
    #
    # 必须**从大到小**画：后画的会盖住先画的，所以最小的那层（贴近扫描前沿）
    # 要最后画，才会呈现"前沿最亮、向尾部渐隐"的效果。反过来画的话，
    # 最终看到的颜色由最大的那层决定，渐变方向就颠倒了。
    tail_degrees = 70.0
    layers = 26
    for index in range(layers, 0, -1):
        fraction = index / layers                     # 1 = 尾部，趋近 0 = 前沿
        angle_end = -90.0 + tail_degrees * fraction
        alpha = int(160 * (1.0 - fraction) ** 1.5) + 10
        draw.pieslice(
            [center - dish_radius, center - dish_radius,
             center + dish_radius, center + dish_radius],
            start=-90.0,
            end=angle_end,
            fill=(63, 224, 138, alpha),
        )

    # 同心刻度圈
    for ratio in (0.35, 0.65, 0.92):
        radius = dish_radius * ratio
        width = max(1, int(0.7 * unit))
        draw.ellipse(
            [center - radius, center - radius, center + radius, center + radius],
            outline=RING,
            width=width,
        )

    # 外圈
    width = max(1, int(1.6 * unit))
    draw.ellipse(
        [center - dish_radius, center - dish_radius,
         center + dish_radius, center + dish_radius],
        outline=SCAN,
        width=width,
    )

    # 扫描前沿线
    tip_x = center
    tip_y = center - dish_radius
    draw.line([(center, center), (tip_x, tip_y)],
              fill=SCAN, width=max(1, int(1.8 * unit)))

    # 一架小飞机（指向正北，用简单的三角 + 机翼表示）
    plane_size = 15.0 * unit
    py = center - dish_radius * 0.45
    draw.polygon(
        [
            (center, py - plane_size),                       # 机头
            (center + plane_size * 0.16, py - plane_size * 0.1),
            (center + plane_size * 0.95, py + plane_size * 0.45),   # 右翼尖
            (center + plane_size * 0.95, py + plane_size * 0.62),
            (center + plane_size * 0.16, py + plane_size * 0.3),
            (center + plane_size * 0.2, py + plane_size * 0.85),
            (center, py + plane_size * 0.72),
            (center - plane_size * 0.2, py + plane_size * 0.85),
            (center - plane_size * 0.16, py + plane_size * 0.3),
            (center - plane_size * 0.95, py + plane_size * 0.62),
            (center - plane_size * 0.95, py + plane_size * 0.45),
            (center - plane_size * 0.16, py - plane_size * 0.1),
        ],
        fill=AIRCRAFT,
    )

    # 圆心站位
    dot = max(1, int(1.4 * unit))
    draw.ellipse([center - dot, center - dot, center + dot, center + dot], fill=SCAN)

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw_icon(size) for size in SIZES]
    # 以最大尺寸为主帧，其余作为多尺寸 ico 帧写入
    frames[-1].save(OUTPUT, format="ICO",
                    sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print(f"图标已生成: {OUTPUT}")
    for size in SIZES:
        print(f"   {size}×{size}")

    # 另外导出一张 PNG 便于预览
    preview = ROOT / "assets" / "icons" / "app_preview.png"
    draw_icon(256).save(preview)
    print(f"预览图: {preview}")


if __name__ == "__main__":
    main()
