"""全域播放进度同步（v2.8.0）：跨设备共享「看到百分之多少」。

每个设备本地仍各自记 progress（localStorage，不动）；本模块在服务端再存一份
**全域合并**的进度：任何设备上报的百分比与其他设备的取最大值（只增不减），
所有设备都能看到。前端在本地 % 后追加一个不同颜色的全域 %。

路由（由 :func:`handle` 分派，返回 ``True`` 表示已处理；对齐 fsweb 风格）：

===========================  ======  ====================================
``/api/sync/progress``        GET     全域进度 ``{"ok":true,"map":{k:{"p":..,"t":..}}}``
``/api/sync/progress``        POST    上报 ``{"map": {"key": pct}}``（支持批量）
``/api/sync/progress/clear``  POST    清空全域记录（需二次确认，由前端保证）
===========================  ======  ====================================

存储：``<程序目录>/data/sync_progress.json``，结构 ``{key: {"p": 0..100, "t": iso}}``。
键 = 前端 normKey 的「叶子目录/文件名」大写口径（与本地进度一致，目录搬家进度跟走）。
合并 = max：``p = max(旧, 新)``，全域值恒 ≥ 任一设备本地值。
上限 :data:`MAX_ENTRIES` 条，超限按 ``t`` 裁 LRU（保住最近看过的）。

并发：服务是 ThreadingTCPServer，多个设备可能同时上报 → 读-改-写全程持
``threading.Lock``；落盘用 ``atomic_write_bytes``（写临时再替换，断电不损坏）。

安全：写操作要求同源（``_origin_allowed``，与 fsweb 同口径）；GET 同源即可，
局域网设备可正常读取。值域严格校验（0..100 整数），坏数据拒绝、不污染存储。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import atomic_write_bytes
from .paths import program_dir

#: 全域记录上限（库实测约 1.9 万可播文件，留一倍余量；超限按 t 裁 LRU）。
MAX_ENTRIES = 20000
#: 一次 POST 允许的最大条数（正常批量 flush 远小于此，防御异常大请求）。
MAX_BATCH = 5000

SYNC_NAME = "sync_progress.json"

_lock = threading.Lock()

#: 测试注入用：设置后覆盖存储文件位置，避免污染真实 data/（同 fileops 口径）。
_path_override: Path | None = None


def sync_path() -> Path:
    if _path_override is not None:
        return _path_override
    return program_dir() / "data" / SYNC_NAME


def _load() -> dict[str, dict[str, Any]]:
    path = sync_path()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out: dict[str, dict[str, Any]] = {}
                for k, v in data.items():
                    if isinstance(v, dict) and isinstance(v.get("p"), (int, float)):
                        out[str(k)] = {"p": max(0, min(100, int(v["p"]))),
                                       "t": str(v.get("t") or "")}
                return out
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    path = sync_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            path, (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 对外操作（均持锁）
# ---------------------------------------------------------------------------

def get_all() -> dict[str, dict[str, Any]]:
    """全域进度快照（只读拷贝）。"""
    with _lock:
        return {k: dict(v) for k, v in _load().items()}


def merge(incoming: dict[str, Any]) -> int:
    """合并上报的 ``{key: pct}``，返回实际更新的条数。

    值与键非法的条目跳过（不报错，兼容部分坏数据不拖累整批）；
    同 key 取 ``max``，只增不减。
    """
    now = _now_iso()
    updated = 0
    with _lock:
        data = _load()
        for k, v in list(incoming.items())[:MAX_BATCH]:
            key = str(k or "").strip().upper()
            if not key or "/" not in key:
                continue                      # normKey 口径：目录/文件名 两级
            try:
                pct = int(round(float(v)))
            except (TypeError, ValueError):
                continue
            if pct <= 0 or pct > 100:
                continue
            old = data.get(key)
            if old is None or pct > old["p"]:
                data[key] = {"p": pct, "t": now}
                updated += 1
        if len(data) > MAX_ENTRIES:
            # 按 t 裁 LRU：t 是秒级 ISO 串，字典序即时间序
            keep = sorted(data.items(), key=lambda x: x[1]["t"], reverse=True)[:MAX_ENTRIES]
            data = dict(keep)
        if updated:
            _save(data)
    return updated


def clear_all() -> int:
    """清空全域记录，返回清掉的条数。"""
    with _lock:
        data = _load()
        n = len(data)
        _save({})
    return n


# ---------------------------------------------------------------------------
# HTTP 分派
# ---------------------------------------------------------------------------

def _body(handler) -> dict[str, Any]:
    raw = handler._drain_body()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def handle(handler, path: str) -> bool:
    """处理 ``/api/sync/*``；非本模块路由返回 False。"""
    if path == "/api/sync/progress":
        if handler.command in ("GET", "HEAD"):
            handler._send_json(200, {"ok": True, "map": get_all()})
            return True
        if handler.command == "POST":
            if not handler._origin_allowed():
                handler._send_json(403, {"ok": False, "error": "origin not allowed"})
                return True
            payload = _body(handler)
            m = payload.get("map")
            if not isinstance(m, dict):
                handler._send_json(400, {"ok": False, "error": "body 需为 {\"map\": {key: pct}}"})
                return True
            n = merge(m)
            handler._send_json(200, {"ok": True, "updated": n})
            return True
        handler._send_json(405, {"ok": False, "error": "method not allowed"})
        return True
    if path == "/api/sync/progress/clear":
        if handler.command != "POST":
            handler._send_json(405, {"ok": False, "error": "method not allowed, use POST"})
            return True
        if not handler._origin_allowed():
            handler._send_json(403, {"ok": False, "error": "origin not allowed"})
            return True
        handler._drain_body()
        handler._send_json(200, {"ok": True, "cleared": clear_all()})
        return True
    return False


__all__ = ["handle", "get_all", "merge", "clear_all", "sync_path",
           "MAX_ENTRIES", "SYNC_NAME"]
