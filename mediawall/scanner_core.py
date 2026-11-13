"""Media_Wall 扫描核心：分区发现 + 叶子目录判定 + 增量缓存。

被顶层 ``scanner.py``（CLI）与 ``mediawall.scan``（进程管理）共用。
只依赖 Python 3.10 标准库。

数据模型
--------
条目（item）= 分区内一个「内容目录」：目录**本层**含至少一个可识别文件
（视频 / 音频 / 图片 / 文档）。判定为递归：既兼容电影三层布局
（``01 电影/2020/片名/`` 内放 mp4+cover.jpg），也兼容动漫四层套娃
（``30 动漫/1994/狮子王/狮子王1 1994/``）——一个目录下既有文件又有
子目录时，本层文件归本层条目，每个子目录继续递归成独立条目。

字段（延续 AV_Wall 命名，前端可复用）::

    id       叶子目录名（展示标题）
    category 分区名（一级目录，如 "01 电影"）
    path     分区内相对路径（"2020/BBC 杜甫…2020"，以 / 分隔）
    src      同 category（兼容 AV_Wall 前端旧字段名）
    key      f"{category}/{path}"（全局唯一）
    poster   本层第一张图（mtime 升序），相对媒体根
    shots    其余图（上限 MAX_SHOTS_PER_ITEM）
    videos   [{path,name,size,mtime,mtime_iso}]（本层视频，path 相对媒体根）
    audio    同结构（本层音频）
    docs     [{name,size}]（本层文档，上限 MAX_DOCS_PER_ITEM，不支持在线打开）
    vcount   可播文件数（视频+音频）
    has_video bool(videos or audio)
    kind     "video" | "audio" | "images" | "docs"（卡片角标与布局用）

增量缓存（library.meta.json）
-----------------------------
* ``entries[key] = {"sig": [dir_mtime, dir_size, n_files, n_child_dirs], "item": {...}}``
  —— 叶子目录签名；父目录结构变化（增删条目目录）会反映在**上层目录的
  n_child_dirs / mtime**，因此每个分区扫描时都要重列目录树（NAS 只列名
  不 stat 文件，成本远低于逐项 stat），签名一致的叶子直接复用缓存记录。
* ``cat_entries[category] = {"scanned": iso, "keys": [...]}`` —— 记录每个
  分区上次扫描信息（诊断用）。
* 按分区扫描（``--category``）时：目标分区重扫，其余分区条目**整体复用**
  缓存，不触碰 NAS。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import (
    DOC_EXTS,
    IMAGE_EXTS,
    AUDIO_EXTS,
    MAX_DOCS_PER_ITEM,
    MAX_SHOTS_PER_ITEM,
    VIDEO_EXTS,
    atomic_write_bytes,
    list_categories,
)
from .paths import program_dir

SCANNER_VERSION = "2.9.0"   # 与发行版号同步（历史约定：2.0.0=视频锚定规则）。
                            # v2.9.0：VIDEO_EXTS/AUDIO_EXTS 扩容（rmvb/rm/m2ts/…、
                            # mka/ape/wv/tta/aif/amr）——旧缓存条目整体作废重扫，
                            # 否则同签名目录一直复用旧清单，rmvb 等永远不入库
LIBRARY_VERSION = 2
META_VERSION = 3  # 3 = 增加了 scanner_version 门禁
#: 改扫描规则后必须递增它：缓存里记着产出该条目的规则版本，不一致就整份丢弃重算。
#: （历史坑：v2.0.0 改了规则却没动缓存，同签名的旧条目被一直复用，
#:   表现成「目录里明明有视频，扫描后也不出现」，只有 --force 能救。）

JSON_ENSURE_ASCII = False
JSON_INDENT = 1

#: 递归硬上限（分区相对媒体根算第 1 层）。旧版是 8 层且超限静默丢条目；
#: 视频锚定后项目会吸收整棵子树，8 层不够用（实测有 11 个条目正好卡在第 8 层），
#: 现在放宽到 14 层，超限会写 warning，不再静默丢。
HARD_DEPTH = 14

#: 容器目录保护：本层有片、但直接子目录里「本层也有片」的个数达到这个值时，
#: 不再把整棵子树吸收成一条（否则「97 抖音视频」下几十个作者作品会被并成
#: 一条 215 个视频的巨型条目）。实测：抖音类合集 100+，影视作品特典 0-2。
CONTAINER_CHILD_MIN = 3

#: 忽略的子目录名（盘符回收站、常见空标记目录）。
#: b/bb/k 系单双字母目录是 AV_Wall 实测的用户空标记（0 文件），这里同样跳过。
IGNORE_DIR_RE = re.compile(
    r"^(\$recycle\.bin|system volume information|@eaDir|#recycle|\.mediawall_trash)$",
    re.IGNORECASE,
)
MARKER_RE = re.compile(r"^[bBkK]{1,3}$")


Item = dict[str, Any]
Signature = tuple[float, int, int, int]
CacheEntry = dict[str, Any]


def now_iso() -> str:
    """本地时间 ISO8601（带秒级精度与时区偏移）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def to_rel(root: str, path: str) -> str:
    """把绝对路径转成相对根目录、以 ``/`` 分隔的字符串（不做路径规范化）。"""
    rel = os.path.relpath(path, root)
    return rel.replace(os.sep, "/")


def is_ignored_dir(name: str) -> bool:
    if IGNORE_DIR_RE.match(name):
        return True
    return MARKER_RE.match(name) is not None


# ---------------------------------------------------------------------------
# 单目录枚举 / 签名
# ---------------------------------------------------------------------------

def dir_signature(path: str) -> Signature | None:
    """叶子目录签名 ``(mtime, size, 文件数, 子目录数)``；失败返回 None。

    一次 scandir 完成；Windows 上 DirEntry 的 stat 走目录枚举缓存，
    不产生额外网络往返。子目录数计入签名：往目录里增删子目录
    （即使本层文件没动）也会触发重扫。
    """
    try:
        st = os.stat(path)
        n_files = 0
        n_dirs = 0
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        n_dirs += 1
                    else:
                        n_files += 1
                except OSError:
                    continue
        return (st.st_mtime, st.st_size, n_files, n_dirs)
    except OSError:
        return None


def _file_record(root: str, entry: os.DirEntry, with_mtime: bool) -> dict[str, Any]:
    st = entry.stat()
    rec: dict[str, Any] = {
        "path": to_rel(root, entry.path),
        "name": entry.name,
        "size": int(st.st_size),
    }
    if with_mtime:
        mtime = float(st.st_mtime)
        rec["mtime"] = mtime
        rec["mtime_iso"] = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
    return rec


# ---------------------------------------------------------------------------
# 项目发现：视频锚定（方案 A）
# ---------------------------------------------------------------------------

# 「分类层」目录名：年份 / 季 / 卷 / 纯序号。这类目录只是分组，不是作品本身，
# 不允许被「单一分支上提」吞成项目名（否则会冒出叫「2022」「s01」的卡片）。
CATEGORY_LAYER_RE = re.compile(
    r"^(?:\d{1,4}|s\d{1,2}|season\s*\d+|vol\.?\s*\d+|"
    r"第[0-9一二三四五六七八九十百]+[季部卷辑]$)$",
    re.I,
)


def is_category_layer(name: str) -> bool:
    """目录名是否属于「分类层」（年份/季/卷/序号）。"""
    return bool(CATEGORY_LAYER_RE.match((name or "").strip()))


# 子树聚合签名：(文件数, 总字节, 最新 mtime 整数)——用于增量复用
TreeSig = tuple[int, int, int]

# 项目：(目录绝对路径, 是否吸收整个子树, 子树聚合签名)
Project = tuple[str, bool, TreeSig]


def _empty_sig() -> TreeSig:
    return (0, 0, 0)


def _add_sig(a: TreeSig, b: TreeSig) -> TreeSig:
    return (a[0] + b[0], a[1] + b[1], max(a[2], b[2]))


def _absorb_media(root: str, proj_dir: str, videos: list[dict[str, Any]],
                  audio: list[dict[str, Any]]) -> None:
    """递归收集 ``proj_dir`` 子树里的视频/音频（不含本层，本层由调用方处理）。

    这里必须用 ``os.scandir`` 的目录句柄递归，**不能**用 ``os.walk`` +
    ``os.stat(os.path.join(...))``：路径长度超过 Windows MAX_PATH（260）时，
    后者会对超长路径抛 OSError 而被 ``continue`` 吞掉，表现为
    「目录里明明有视频，扫描完条目 vcount 仍是 0」。``DirEntry.stat()``
    走的是目录句柄，不受长路径限制（实测乱马剧场版那 3 个 mkv 就是这么丢的）。
    """
    stack = []
    try:
        for e in os.scandir(proj_dir):
            if e.is_dir(follow_symlinks=False) and not is_ignored_dir(e.name):
                stack.append(e.path)
    except OSError:
        return
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not is_ignored_dir(entry.name):
                        stack.append(entry.path)
                    continue
            except OSError:
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in VIDEO_EXTS and ext not in AUDIO_EXTS:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            rec = {
                "path": to_rel(root, entry.path),
                "name": entry.name,
                "size": int(st.st_size),
                "mtime": float(st.st_mtime),
                "mtime_iso": datetime.fromtimestamp(
                    st.st_mtime).isoformat(timespec="seconds"),
            }
            (videos if ext in VIDEO_EXTS else audio).append(rec)


def scan_project(root: str, category: str, proj_dir: str,
                 absorb: bool) -> Item:
    """枚举一个项目的文件，产出条目记录；读取失败抛 OSError。

    ``absorb=True`` 时递归收集整个子树的视频与音频（多碟/CD/特典合一），
    图片只取本层（海报与截图仍以作品目录自身的图为准）。
    """
    try:
        entries = list(os.scandir(proj_dir))
    except OSError as exc:
        raise OSError(f"{proj_dir}: {exc}") from exc

    images: list[tuple[float, str, dict[str, Any]]] = []
    videos: list[dict[str, Any]] = []
    audio: list[dict[str, Any]] = []
    docs: list[dict[str, Any]] = []

    for entry in entries:
        name = entry.name
        try:
            if entry.is_dir():
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in IMAGE_EXTS:
                st = entry.stat()
                images.append((st.st_mtime, name, {
                    "path": to_rel(root, entry.path), "name": name,
                }))
            elif ext in VIDEO_EXTS:
                videos.append(_file_record(root, entry, with_mtime=True))
            elif ext in AUDIO_EXTS:
                audio.append(_file_record(root, entry, with_mtime=True))
            elif ext in DOC_EXTS:
                docs.append(_file_record(root, entry, with_mtime=False))
        except OSError as exc:
            raise OSError(f"{entry.path}: {exc}") from exc

    if absorb:
        _absorb_media(root, proj_dir, videos, audio)

    # 图片按 LastWriteTime 升序；第一张为海报，其余为截图/内页预览
    images.sort(key=lambda t: (t[0], t[1]))
    poster = images[0][2]["path"] if images else None
    shots = [img[2]["path"] for img in images[1:1 + MAX_SHOTS_PER_ITEM]]
    videos.sort(key=lambda v: v["path"].lower())
    audio.sort(key=lambda v: v["path"].lower())
    docs.sort(key=lambda v: v["path"].lower())
    docs = docs[:MAX_DOCS_PER_ITEM]

    prefix = os.path.join(root, category)
    rel = os.path.relpath(proj_dir, prefix).replace(os.sep, "/")
    if rel == ".":
        rel = ""
    proj_name = os.path.basename(proj_dir)

    if videos:
        kind = "video"
    elif audio:
        kind = "audio"
    elif images:
        kind = "images"
    else:
        kind = "docs"
    return {
        "id": proj_name,
        "category": category,
        "src": category,
        "path": rel,
        "key": f"{category}/{rel}" if rel else category,
        "poster": poster,
        "shots": shots,
        "videos": videos,
        "audio": audio,
        "docs": docs,
        "vcount": len(videos) + len(audio),
        "has_video": bool(videos or audio),
        "kind": kind,
    }


# ---------------------------------------------------------------------------
# 递归发现项目（视频锚定）
# ---------------------------------------------------------------------------

def collect_projects(category_path: str) -> tuple[list[Project], list[str]]:
    """列出一个分区下的项目目录。

    返回 ``(projects, warnings)``，projects 为 ``[(绝对路径, 是否吸收子树)]``。
    """
    projects: list[Project] = []
    warnings: list[str] = []
    _collect(category_path, 0, projects, warnings, top=True)
    return projects, warnings


def _collect(dir_path: str, depth: int, out: list[Project],
             warnings: list[str], top: bool = False
             ) -> tuple[TreeSig, bool, bool]:
    """遍历一个目录并按「视频锚定」规则产出项目。

    返回 ``(子树聚合签名, 本层是否有视频/音频, 子树是否有视频/音频)``：
    后两个布尔值用于上层判断——本层有片时是否该吸收子树（容器目录保护），
    以及图文分区必须沿用旧规则（每层本层有文件即各自成条目）。
    """
    if depth > HARD_DEPTH:
        warnings.append(f"[层级超限] {dir_path}（>{HARD_DEPTH} 层，已跳过）")
        return _empty_sig(), False, False

    try:
        entries = list(os.scandir(dir_path))
    except OSError as exc:
        warnings.append(f"[目录读取失败] {dir_path}: {exc}")
        return _empty_sig(), False, False

    sub_dirs: list[str] = []
    self_video = False        # 本层有视频/音频
    self_any = False          # 本层有任意可识别文件
    sig = _empty_sig()
    for entry in entries:
        name = entry.name
        try:
            if entry.is_dir():
                if is_ignored_dir(name):
                    continue
                sub_dirs.append(entry.path)
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext in IMAGE_EXTS or ext in VIDEO_EXTS or ext in AUDIO_EXTS or ext in DOC_EXTS:
                    self_any = True
                    if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
                        self_video = True
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    sig = _add_sig(sig, (1, int(st.st_size), int(st.st_mtime)))
        except OSError:
            continue

    # 子目录先各产能，再把签名合并上来
    child_projects: list[Project] = []
    child_self_video = 0      # 直接子目录里「本层就有片」的个数
    child_any_video = False   # 子树里是否有片
    for d in sub_dirs:
        child_sig, c_self_video, c_sub_video = _collect(
            d, depth + 1, child_projects, warnings)
        sig = _add_sig(sig, child_sig)
        if c_self_video:
            child_self_video += 1
        if c_sub_video:
            child_any_video = True

    subtree_video = self_video or child_any_video

    if self_video:
        if child_self_video >= CONTAINER_CHILD_MIN:
            # 容器目录：本层只有零星散片，下面还有多个自带视频的独立子项目
            # （如「97 抖音视频」下按作者分成几十个目录）。此时本层不吸收，
            # 否则几十部作品会被合并成一条，条目体积失控。
            out.append((dir_path, False, sig))
            out.extend(child_projects)
        else:
            # 本层就有片：本目录即项目，并把子树（多碟/CD/特典）一并吸收
            out.append((dir_path, True, sig))
        return sig, True, True

    if child_projects:
        if not subtree_video:
            # 图文语义（子树没有任何视频/音频）：沿用旧规则，本层有文件即条目，
            # 同时保留子树各自的条目——漫画/小说这类分区行为必须与升级前一致。
            if self_any:
                out.append((dir_path, False, sig))
            out.extend(child_projects)
        elif len(child_projects) == 1 and not is_category_layer(
                os.path.basename(dir_path)) and not top:
            # 单一分支：上提为当前目录（标题更干净，特典/发布组目录被吃掉）
            out.append((dir_path, True, sig))
        else:
            out.extend(child_projects)
        return sig, False, subtree_video

    # 子树没有任何视频/音频 → 图文语义，沿用原规则（本层有文件即条目）
    if self_any:
        out.append((dir_path, False, sig))
    return sig, False, subtree_video


# ---------------------------------------------------------------------------
# 增量缓存
# ---------------------------------------------------------------------------

def load_meta(meta_path: Path, root: str) -> dict[str, Any]:
    """读取增量缓存；文件缺失 / 损坏 / 根目录不一致时返回空结构。

    根目录比对用「路径等价」判定（Windows 下大小写不敏感），
    避免仅大小写写法不同就触发全量重扫。
    """
    empty = {"entries": {}, "cat_entries": {}}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict) or raw.get("version") != META_VERSION:
        return empty
    # 规则版本门禁：扫描规则一变，旧条目语义可能已失效，宁可全量重算。
    if raw.get("scanner_version") != SCANNER_VERSION:
        return empty
    cached_root = raw.get("root")
    if not (isinstance(cached_root, str) and (
        cached_root == root or _same_root(cached_root, root)
    )):
        return empty
    entries = raw.get("entries")
    cat_entries = raw.get("cat_entries")
    return {
        "entries": entries if isinstance(entries, dict) else {},
        "cat_entries": cat_entries if isinstance(cat_entries, dict) else {},
    }


def _same_root(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except (OSError, ValueError):
        return False


def save_meta(meta_path: Path, root: str, entries: dict[str, CacheEntry],
              cat_entries: dict[str, Any]) -> None:
    """写入增量缓存（原子写）。"""
    payload = {
        "version": META_VERSION, "scanner_version": SCANNER_VERSION, "root": root,
        "entries": entries, "cat_entries": cat_entries,
    }
    data = json.dumps(payload, ensure_ascii=JSON_ENSURE_ASCII, indent=JSON_INDENT).encode("utf-8")
    if not atomic_write_bytes(meta_path, data):
        raise OSError(f"写入缓存失败：{meta_path}")


# ---------------------------------------------------------------------------
# 扫描主流程
# ---------------------------------------------------------------------------

#: 扫描进度文件（供桌面 GUI 轮询；scanner 每扫完一个分区写一次）。
SCAN_PROGRESS_NAME = "scan_progress.json"


def scan_progress_path() -> Path:
    """扫描进度文件路径（程序目录 ``data/`` 下）。"""
    return program_data_dir() / SCAN_PROGRESS_NAME


def write_scan_progress(done: int, total: int, category: str = "") -> None:
    """写扫描进度 JSON。失败静默：进度只是增强信息，绝不能影响扫描本身。

    GUI 轮询 ``data/scan_progress.json`` 得到 ``{done,total,category}``，
    换算成真实百分比进度条；CLI 下无害（多写一个小文件）。

    注意：首次扫描时 ``data/`` 目录尚不存在（它由扫描结束后的
    ``write_outputs`` 才创建），而进度写入发生在扫描**开始**阶段。
    因此这里必须自己 ``mkdir``：否则首扫的每次写入都会
    ``FileNotFoundError`` 并被静默吞掉 —— GUI 首次启动看不到进度条。
    """
    try:
        path = scan_progress_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"done": int(done), "total": int(total), "category": category,
             "ts": now_iso()},
            ensure_ascii=False,
        )
        path.write_text(payload, encoding="utf-8")
    except OSError:
        pass


def scan_all(root: str, force: bool, only_category: str | None,
             quiet: bool) -> tuple[list[Item], list[str], dict[str, int],
                                   dict[str, CacheEntry], dict[str, Any]]:
    """扫描（全部或单个分区），返回 (items, warnings, stats, entries, cat_entries)。

    * ``only_category=None``：全部分区逐一遍历；
    * ``only_category="01 电影"``：只重扫该分区，其余分区条目整体复用缓存；
    * ``force=True``：忽略缓存全量重建（仍只作用于目标范围）。
    """
    warnings: list[str] = []
    meta_path = program_data_dir() / "library.meta.json"
    meta = {"entries": {}, "cat_entries": {}} if force else load_meta(meta_path, root)
    cached: dict[str, CacheEntry] = meta["entries"]
    cat_cached: dict[str, Any] = meta["cat_entries"]

    new_entries: dict[str, CacheEntry] = {}
    new_cat: dict[str, Any] = {}
    items: list[Item] = []
    stats = {"reused": 0, "rescanned": 0, "skipped": 0,
             "cats_ok": 0, "cats_failed": 0, "cats_from_cache": 0}

    def reuse_category(name: str) -> None:
        """把某分区的全部缓存条目复用到本次结果（并保住缓存，避免无谓重扫）。"""
        n = 0
        for key, e in cached.items():
            if not (isinstance(e, dict) and isinstance(e.get("item"), dict)):
                continue
            item = e["item"]
            if item.get("category") == name and "videos" in item:
                items.append(item)
                new_entries[key] = e
                n += 1
        if n:
            stats["reused"] += n

    categories = list_categories(root)
    if only_category is not None:
        if only_category not in categories:
            warnings.append(f"[指定分区不存在] {only_category}")
            target_cats: tuple[str, ...] = ()
        else:
            target_cats = (only_category,)
        # 未扫描的分区：整体复用缓存条目（不触碰 NAS）
        for name in categories:
            if name == only_category:
                continue
            reuse_category(name)
            stats["cats_from_cache"] += 1
    else:
        target_cats = categories

    if not target_cats and only_category is None:
        warnings.append("[媒体根下没有发现任何分区目录]")

    total_cats = len(target_cats)
    # 进度起点（GUI 轮询用）：0 / total
    write_scan_progress(0, total_cats)
    for idx, cat in enumerate(target_cats):
        cat_path = os.path.join(root, cat)
        projects, leaf_warn = collect_projects(cat_path)
        # collect_leaves 对分区根本身读失败也会记 warning（空列表）；
        # 区分「读不到」与「空分区」：前者复用缓存、计 failed，后者计 ok。
        if not projects and leaf_warn and any(
            w.startswith("[目录读取失败]") and cat_path in w for w in leaf_warn
        ):
            warnings.append(f"[分区读取失败] {cat_path}")
            stats["cats_failed"] += 1
            # 分区读不到（NAS 抖动）：复用该分区缓存，绝不产出缺该分区的库
            reuse_category(cat)
            continue
        stats["cats_ok"] += 1
        warnings.extend(leaf_warn)

        rescanned = 0
        reused = 0
        for proj_dir, absorb, psig in projects:
            rel = os.path.relpath(proj_dir, cat_path).replace(os.sep, "/")
            if rel == ".":
                rel = ""
            key = f"{cat}/{rel}" if rel else cat
            # 签名由 collect_projects 遍历时顺带算出（覆盖整棵子树），不额外 stat
            sig = psig
            old = cached.get(key)
            if sig is not None and old is not None:
                old_sig = old.get("sig") if isinstance(old, dict) else None
                same = False
                if old_sig is not None:
                    try:
                        same = tuple(old_sig) == sig
                    except TypeError:
                        same = False
                if same:
                    item = old.get("item") if isinstance(old, dict) else None
                    if isinstance(item, dict) and "videos" in item:
                        items.append(item)
                        new_entries[key] = old
                        reused += 1
                        continue
            try:
                item = scan_project(root, cat, proj_dir, absorb)
            except OSError as exc:
                warnings.append(f"[叶子目录读取失败] {exc}")
                stats["skipped"] += 1
                # 读取失败时保留旧缓存记录（若有），避免条目从库里凭空消失
                if isinstance(old, dict) and isinstance(old.get("item"), dict):
                    old_item = old["item"]
                    if "videos" in old_item and old_item.get("category") == cat:
                        items.append(old_item)
                        new_entries[key] = old
                continue
            items.append(item)
            new_entries[key] = {"sig": list(sig) if sig is not None else None,
                                "item": item}
            rescanned += 1

        # 目标分区已完整重列：缓存中该分区未被再发现的 key 视为已删除（自然淘汰）。
        new_cat[cat] = {"scanned": now_iso(), "items": reused + rescanned}
        stats["reused"] += reused
        stats["rescanned"] += rescanned
        if not quiet:
            print(f"  · {cat}: {len(projects)} 个条目目录"
                  f"（复用 {reused} / 重扫 {rescanned}）", flush=True)
        # 每扫完一个分区推进一次进度（GUI 进度条数据源）
        write_scan_progress(idx + 1, total_cats, cat)

    # 未扫描分区（含按分区扫描时的其余分区）的 cat_entries 原样保留
    for name, info in cat_cached.items():
        if name not in new_cat and name not in {c for c in categories if c in target_cats}:
            new_cat[name] = info

    items.sort(key=lambda it: (it["category"], it["path"].lower()))
    return items, warnings, stats, new_entries, new_cat


def program_data_dir() -> Path:
    """数据输出目录：始终是**程序目录**下的 ``data/``（不跟随媒体根）。

    程序目录由 :func:`mediawall.paths.program_dir` 给出：源码运行时是项目根，
    Nuitka 编译后是 exe 所在目录，两种形态都落在同一个 ``data/``。
    """
    return program_dir() / "data"


def build_library(root: str, items: list[Item], warnings: list[str],
                  cat_scan_times: dict[str, Any] | None = None) -> dict[str, Any]:
    """组装 library.json 的顶层结构。

    ``cat_scan_times``：``{分区: {"scanned": iso, ...}}``（来自 new_cat），
    转成顶层 ``cat_scanned = {分区: iso}`` 供 ``GET /api/categories`` 判断
    哪些分区还没扫过（前端分区标签上的「未扫描」徽标数据源）。
    """
    total_videos = sum(int(it["vcount"]) for it in items)
    total_posters = sum(1 for it in items if it["poster"])
    total_shots = sum(len(it["shots"]) for it in items)
    cat_counts: dict[str, int] = {}
    for it in items:
        cat_counts[it["category"]] = cat_counts.get(it["category"], 0) + 1
    scanned: dict[str, str] = {}
    if isinstance(cat_scan_times, dict):
        for name, info in cat_scan_times.items():
            if isinstance(info, dict) and info.get("scanned"):
                scanned[name] = str(info["scanned"])
    return {
        "version": LIBRARY_VERSION,
        "root": root,
        "generated": now_iso(),
        "count": len(items),
        "total_videos": total_videos,
        "total_posters": total_posters,
        "total_shots": total_shots,
        "categories": [{"name": c, "items": cat_counts.get(c, 0)}
                       for c in list_categories(root)],
        "cat_scanned": scanned,
        "items": items,
        "warnings": warnings,
    }


def write_outputs(data_dir: Path, library: dict[str, Any]) -> tuple[Path, Path, int]:
    """写出 library.json 与 library.js，返回 (json 路径, js 路径, json 字节数)。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(library, ensure_ascii=JSON_ENSURE_ASCII, indent=JSON_INDENT)

    json_path = data_dir / "library.json"
    if not atomic_write_bytes(json_path, text.encode("utf-8")):
        raise OSError(f"写入失败：{json_path}")
    json_bytes = json_path.stat().st_size

    js_path = data_dir / "library.js"
    js_text = "window.__MW_LIBRARY__ = " + text + ";"
    if not atomic_write_bytes(js_path, js_text.encode("utf-8")):
        raise OSError(f"写入失败：{js_path}")
    return json_path, js_path, json_bytes
