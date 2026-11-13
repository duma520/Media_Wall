"""为 GitHub 截图构造一个**假媒体库**（不含任何真实片名/海报）。

为什么要造假的
--------------
截图会公开到 GitHub。若直接用真实媒体根截图，等于把本机收藏的
片名、分区名、观影偏好一起公开——这会把发布前的脱敏工作全部作废。
因此这里用程序化生成的占位海报 + 中性假片名，扫出 library.json，
仅用于截图，绝不写入用户真实 data/。

产物
----
<work>/media/<分区>/<年份>/<片名>/poster.jpg + 2 张剧照 + <片名>.mkv（稀疏文件，显示真实体积）
<work>/prog/            程序目录副本（含 mediawall/ + index.html + server.html）
<work>/prog/data/library.json   由真实扫描器生成

用法：python _scaffold/_make_screenshot_library.py
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
WORK = PROG / "_screenshot_work"

# 中性占位片名：不使用任何真实作品名，避免与现实作品混淆
TITLES = [
    "示例影片", "演示剧集", "样例动画", "测试纪录片", "占位短片",
    "虚构综艺", "样例音乐会", "示例剧场版", "演示合集", "样例纪录",
    "虚构纪行", "示例番外", "演示特辑", "样例短片集", "虚构故事",
    "示例单元剧", "演示长片", "样例影像", "虚构短片", "示例纪实",
    "演示片段", "样例作品", "虚构剧场", "示例短剧",
]

CATEGORIES = [
    ("01 电影", 10),
    ("10 电视节目", 9),
    ("30 动漫", 8),
    ("94 纪录片", 6),
]

# 海报底色：低保和的深色系，接近真实海报墙的观感
PALETTE = [
    (214, 96, 74), (58, 108, 176), (39, 143, 128), (186, 138, 62),
    (128, 92, 168), (196, 100, 140), (72, 132, 92), (204, 116, 58),
]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
]


def pick_font(size: int):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_poster(path: Path, title: str, year: str, seed: int) -> None:
    """生成一张 16:9 的占位封面。

    为什么是 16:9 而不是 2:3 竖版海报：界面里封面格子固定 ``aspect-ratio:16/9``
    且图片用 ``object-fit:contain``（index.html:147），竖版海报会被压成窄条、
    两侧留大片黑边，截图很难看。真实媒体目录里第一张图多半就是 16:9 视频帧，
    16:9 封面才是这个界面下的常态。
    """
    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    w, h = 720, 405
    base = PALETTE[seed % len(PALETTE)]
    img = Image.new("RGB", (w, h), base)
    dr = ImageDraw.Draw(img, "RGBA")

    top = tuple(min(255, int(c * 1.35)) for c in base)
    bottom = tuple(max(0, int(c * 0.55)) for c in base)
    for y in range(h):
        k = y / h
        dr.line([(0, y), (w, y)], fill=(
            int(top[0] + (bottom[0] - top[0]) * k),
            int(top[1] + (bottom[1] - top[1]) * k),
            int(top[2] + (bottom[2] - top[2]) * k),
        ))

    # 主体色块制造视觉焦点
    for _ in range(rnd.randint(2, 4)):
        cx, cy = rnd.randint(60, w - 60), rnd.randint(50, h - 120)
        r = rnd.randint(60, 130)
        tone = rnd.choice([(255, 250, 240), (20, 24, 32)])
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tone + (34,))

    # 底部深色信息带，保证片名可读
    dr.rectangle([0, h - 108, w, h], fill=(12, 14, 18, 200))
    f_title = pick_font(30)
    f_year = pick_font(18)
    dr.text((24, h - 92), title, font=f_title, fill=(240, 242, 246))
    dr.text((24, h - 46), f"{year} · 演示素材", font=f_year, fill=(150, 158, 172))

    img.save(path, "JPEG", quality=86)


def make_shot(path: Path, title: str, seed: int, idx: int) -> None:
    """生成一张 16:9 的占位「剧照」（模拟视频截图）。

    真实媒体里一个作品目录常有多张图：第一张当封面，其余作为剧照。
    这里同样给每个作品补 2 张，截图时右侧格子才不会全是「无截图」占位。
    """
    from PIL import Image, ImageDraw

    rnd = random.Random(seed * 31 + idx)
    w, h = 640, 360
    base = PALETTE[(seed + idx) % len(PALETTE)]
    img = Image.new("RGB", (w, h), base)
    dr = ImageDraw.Draw(img, "RGBA")

    top = tuple(min(255, int(c * 1.3)) for c in base)
    bottom = tuple(max(0, int(c * 0.5)) for c in base)
    for y in range(h):
        k = y / h
        dr.line([(0, y), (w, y)], fill=(
            int(top[0] + (bottom[0] - top[0]) * k),
            int(top[1] + (bottom[1] - top[1]) * k),
            int(top[2] + (bottom[2] - top[2]) * k),
        ))

    # 分层色带，像一帧画面的水平构图
    for _ in range(rnd.randint(3, 5)):
        y0 = rnd.randint(0, h - 60)
        bh = rnd.randint(20, 70)
        tone = rnd.choice([(255, 255, 255), (16, 20, 28)])
        dr.rectangle([0, y0, w, y0 + bh], fill=tone + (26,))

    f = pick_font(20)
    dr.text((16, h - 34), f"{title} · 剧照 {idx}", font=f, fill=(235, 238, 244))
    img.save(path, "JPEG", quality=84)


def build_media_root() -> Path:
    media = WORK / "media"
    if media.exists():
        shutil.rmtree(media)
    media.mkdir(parents=True)

    n = 0
    for cat, count in CATEGORIES:
        cat_dir = media / cat
        cat_dir.mkdir(parents=True)
        for i in range(count):
            title = TITLES[n % len(TITLES)]
            if i >= len(TITLES):
                title = f"{TITLES[n % len(TITLES)]} {i + 1}"
            year = str(2018 + (n % 8))
            proj = cat_dir / year / title
            proj.mkdir(parents=True, exist_ok=True)
            make_poster(proj / "poster.jpg", title, year, seed=n)
            # 每个作品补 2 张剧照：真实媒体里「第一张=封面，其余=剧照」，
            # 没有剧照时卡片右侧会全是「无截图」占位，截图观感很差。
            # 假视频用稀疏文件（sparse file）造出真实体积：界面上会显示
            # 「1.4 GB」这类大小，比「0 B」自然得多；而稀疏文件不实际占磁盘。
            size_mb = 700 + (n * 137) % 1500
            vf = proj / f"{title}.mkv"
            with open(vf, "wb") as fh:
                fh.truncate(size_mb * 1024 * 1024)
            # 每个作品补 2 张剧照：真实媒体目录里「第一张=封面，其余=剧照」，
            # 没有剧照时卡片右侧两格会全是「无截图」占位，截图观感很差。
            for si in (1, 2):
                make_shot(proj / f"shot{si}.jpg", title, seed=n, idx=si)
            n += 1
    print(f"[1/3] 假媒体根就绪：{n} 个作品，{len(CATEGORIES)} 个分区 → {media}")
    return media


def build_program_dir(media: Path) -> Path:
    """复制程序到临时目录，避免扫描结果写进用户真实 data/。"""
    prog = WORK / "prog"
    if prog.exists():
        shutil.rmtree(prog)
    prog.mkdir(parents=True)

    shutil.copytree(PROG / "mediawall", prog / "mediawall",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("index.html", "server.html", "server.py", "scanner.py",
                 "Media_Wall.py", "watchdog.py", "icon.ico"):
        src = PROG / name
        if src.is_file():
            shutil.copy2(src, prog / name)

    # 控制面板也要截，GUI 模块一并复制
    for gui_py in PROG.glob("gui_*.py"):
        shutil.copy2(gui_py, prog / gui_py.name)

    # 配置指向假媒体根（截图用，不含任何真实路径）
    (prog / "config.json").write_text(json.dumps({
        "media_root": str(media),
        "categories": [c for c, _ in CATEGORIES],
        "history_limit": 200,
        "history_record_pages": True,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[2/3] 程序副本就绪 → {prog}")
    return prog


def run_scanner(prog: Path, media: Path) -> None:
    # 扫描器在临时程序目录里跑，写自己的 data/library.json
    env = dict(os.environ)
    env["PYTHONPATH"] = str(prog)
    r = subprocess.run(
        [sys.executable, str(prog / "scanner.py"), "--root", str(media), "--force"],
        cwd=str(prog), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    lib = prog / "data" / "library.json"
    if not lib.is_file():
        print("扫描失败，stdout:", r.stdout[-1500:])
        print("stderr:", r.stderr[-1500:])
        raise SystemExit(1)
    data = json.loads(lib.read_text(encoding="utf-8"))
    print(f"[3/3] 扫描完成：{data['count']} 个条目，{len(data['categories'])} 个分区，"
          f"{data['total_posters']} 张封面 → {lib}")


def main() -> int:
    WORK.mkdir(exist_ok=True)
    media = build_media_root()
    prog = build_program_dir(media)
    run_scanner(prog, media)
    print("WORK_DIR=" + str(WORK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
