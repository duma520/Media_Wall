"""程序图标：程序目录下没有 ``icon.ico`` 时自动生成一个。

为什么要自动生成：Windows 任务栏/标题栏和 exe 图标都需要 ``icon.ico``，
但图标是"包装饰"，不应成为运行前提。所以这里的策略是：

* 已有 ``icon.ico`` → 直接用，不覆盖（用户可能自己换了图标）；
* 没有 → 用 Pillow 现画一个（海报墙 + 播放键），画失败也**不报错**，
  只是界面上继续显示默认图标（与全局规则「图标存在性检测」一致）。

Pillow 只在需要生成时才 import，缺 Pillow 时静默跳过。
"""

from __future__ import annotations

from pathlib import Path

from .paths import program_dir

ICON_NAME = "icon.ico"

#: ICO 里包含的尺寸（Windows 会按 DPI 挑最合适的一张）
_SIZES = (16, 24, 32, 48, 64, 128, 256)


def icon_path() -> Path:
    """图标文件应放的位置（程序目录）。"""
    return program_dir() / ICON_NAME


def ensure_icon() -> Path | None:
    """确保图标存在；返回可用的图标路径，生成失败返回 None。"""
    path = icon_path()
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        return None
    try:
        _draw_icon(path)
    except Exception:  # noqa: BLE001 - 图标失败绝不影响启动
        return None
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _draw_icon(path: Path) -> None:
    """画一个 256×256 的图标：深色圆角底 + 3×2 海报格 + 播放三角。"""
    from PIL import Image, ImageDraw

    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 背景：深蓝灰圆角矩形
    draw.rounded_rectangle(
        (8, 8, size - 8, size - 8), radius=44, fill=(30, 41, 59, 255))

    # 海报墙：3 列 × 2 行小方块，深浅交替，做出"墙"的感觉
    colors = [
        (56, 189, 248), (129, 140, 248), (244, 114, 182),
        (52, 211, 153), (251, 191, 36), (148, 163, 184),
    ]
    left, top = 34, 40
    card_w, card_h, gap = 52, 68, 12
    for row in range(2):
        for col in range(3):
            x0 = left + col * (card_w + gap)
            y0 = top + row * (card_h + gap)
            draw.rounded_rectangle(
                (x0, y0, x0 + card_w, y0 + card_h), radius=8,
                fill=colors[row * 3 + col] + (255,))

    # 播放三角：半透明黑底圆 + 白色三角
    cx, cy, r = 176, 176, 54
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(15, 23, 42, 230))
    draw.polygon(
        [(cx - 18, cy - 26), (cx - 18, cy + 26), (cx + 30, cy)],
        fill=(255, 255, 255, 255))

    img.save(path, format="ICO", sizes=[(s, s) for s in _SIZES])
