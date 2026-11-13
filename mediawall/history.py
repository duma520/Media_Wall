"""访问历史：记录「谁（IP）在什么时间、从哪个页面、打开了什么文件」。

设计要点
--------
* **不用网页端上报**：所有播放都走 ``/media/<相对路径>``，服务端在响应时
  就能拿到 ``client_address`` 与文件名，前端零改动。
* **追加写 JSONL**：每条一行，避免每次请求都重写整个文件；条数超过上限
  的 2 倍时才压缩重写一次，压缩后保留最新 ``history_limit`` 条。
* **去重**：同一个 (IP, 文件) 在 ``_DEDUPE_SECONDS`` 内只记一条——播放一段
  视频会连续发很多 Range 请求，不去重会把历史刷爆。
* **线程安全**：服务端是 ThreadingTCPServer，多线程会同时写，全部操作走
  同一把锁。
* **保留条数可配**：``config.json`` 的 ``history_limit``（0 / 负数 = 不限制），
  以及 ``history_record_pages``（是否记录「打开网页」事件）。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import atomic_write_bytes, load_config
from .paths import program_dir

HISTORY_NAME = "history.jsonl"
DEFAULT_LIMIT = 500
#: 同一 (IP, 文件) 的去重窗口（秒）
_DEDUPE_SECONDS = 30.0
#: 页面上报的「打开网页」事件，rel 固定为空
KIND_PAGE = "page"
KIND_PLAY = "play"

_lock = threading.Lock()
_dedupe: dict[tuple[str, str], float] = {}


#: 测试注入用：设置后覆盖历史文件位置，避免污染真实 data/
_path_override: Path | None = None


def history_path() -> Path:
    """历史文件位置：``<程序目录>/data/history.jsonl``。"""
    if _path_override is not None:
        return _path_override
    return program_dir() / "data" / HISTORY_NAME


def _limits() -> tuple[int, bool]:
    """从 ``config.json`` 读保留条数与「是否记录页面访问」。"""
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001 - 配置读不到就用默认
        cfg = {}
    try:
        limit = int(cfg.get("history_limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    pages = bool(cfg.get("history_record_pages", True))
    return limit, pages


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def record(ip: str, kind: str, rel: str = "", category: str = "",
           name: str = "") -> bool:
    """记一条历史。失败静默返回 False，绝不影响正常请求。

    ``kind`` 为 ``"play"``（打开文件）或 ``"page"``（打开网页）。
    """
    limit, record_pages = _limits()
    if kind == KIND_PAGE and not record_pages:
        return False

    ip = (ip or "").strip()
    rel = (rel or "").strip()
    if not ip:
        return False
    if kind == KIND_PLAY and not rel:
        return False

    key = (ip, rel)
    now = time.time()
    with _lock:
        last = _dedupe.get(key)
        if last is not None and now - last < _DEDUPE_SECONDS:
            return False
        _dedupe[key] = now
        if len(_dedupe) > 4096:  # 防止长期运行后字典无限膨胀
            cutoff = now - _DEDUPE_SECONDS
            for k in [k for k, v in _dedupe.items() if v < cutoff]:
                _dedupe.pop(k, None)

        event = {
            "ts": _now_iso(),
            "ip": ip,
            "kind": kind,
            "rel": rel,
            "category": category,
            "name": name or (Path(rel).name if rel else ""),
        }
        path = history_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            return False
        _maybe_trim(path, limit)
        return True


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _maybe_trim(path: Path, limit: int) -> None:
    """条数超过上限 2 倍时重写文件，只保留最新 ``limit`` 条。"""
    if limit <= 0:
        return
    if _count_lines(path) <= limit * 2:
        return
    keep = list_events(limit=limit)
    # list_events 返回「最新在前」，而文件里的物理顺序必须是「时间正序」（追加写），
    # 否则每次裁剪都会把整个文件顺序翻转一次 —— 曾因此让历史列表顺序错乱。
    keep.reverse()
    payload = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep)
    atomic_write_bytes(path, payload.encode("utf-8"))


def list_events(limit: int = 200) -> list[dict[str, Any]]:
    """读取历史，**最新在前**。损坏的行直接跳过。"""
    path = history_path()
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    events.reverse()
    if limit > 0:
        return events[:limit]
    return events


def clear() -> bool:
    """清空历史。"""
    with _lock:
        _dedupe.clear()
        path = history_path()
        try:
            if path.exists():
                path.unlink()
            return True
        except OSError:
            return False


def event_count() -> int:
    """当前历史条数（用于界面显示）。"""
    return _count_lines(history_path())
