"""全域已看记录（v2.10.0）：服务端统一管理「哪些视频点开过」。

设计动机
--------
v2.8.0 把播放进度 % 搬到了服务端（:mod:`mediawall.syncstore`），但「已看」
标记一直留在每设备浏览器的 localStorage：换设备 / 换浏览器就看不到，也无法
备份或迁移。本模块在服务端再存一份**全域权威**的已看集合，所有设备读写同一份，
并在设置里提供文件导入 / 导出。

与 ``syncstore`` 的关系
----------------------
独立文件、独立路由、独立存储 ``data/sync_watched.json``，**不改**进度逻辑：
进度的合并语义是 max 只增不减，而已看记录要支持「清空全部」，两者模型不同，
混在一个文件里只会互相拖累。

路由（由 :func:`handle` 分派，返回 ``True`` 表示已处理）：

=============================  ======  ==========================================
``/api/sync/watched``          GET     全量 ``{"ok":true,"rev":N,"map":{...}}``
``/api/sync/watched``          POST    增量 ``{"rev":N,"add":[...],"del":[...]}``
``/api/sync/watched``          POST    整表 ``{"replace":[...]}``（文件导入）
``/api/sync/watched/clear``    POST    清空全域（``rev++``）
=============================  ======  ==========================================

存储：``<程序目录>/data/sync_watched.json``，结构::

    {"rev": 0, "cleared_at": "", "map": {"<KEY>": {"t": "<iso>"}, ...}}

键 = 前端 normKey 的「叶子目录/文件名」大写口径（目录搬家记录跟着走）。

代次 rev 与「清空复活」问题
--------------------------
进度是只增不减，天然不会复活；已看记录能被清空，就有经典竞态：设备 A 清空，
设备 B 离线期间本地仍攒着清空前的记录，联网后把旧记录增量推回来 → 清空白做。
为此服务端维护 ``rev``：每次 ``clear`` 令 ``rev += 1`` 并记 ``cleared_at``；
前端每次上报 ``add`` 必须带上自己所见的服务端 ``rev``，服务端发现不一致
（说明期间发生过清空）就拒绝这次增量并返回 ``stale:true``，前端据此重新拉取。
这样清空后新点开的视频（带新 rev）正常入库，旧设备的陈旧增量不会复活旧数据。
首次迁移（本地旧 localStorage → 空服务端）只在 ``rev == 0`` 时进行：一旦
服务端发生过清空（rev>0），旧设备不再自动把历史记录推回，避免绕过清空。

安全与并发：写操作要求同源（``_origin_allowed``，与 syncstore 同口径）；
读-改-写全程持 ``threading.Lock``；落盘用 ``atomic_write_bytes``。
值域校验：键必须含 ``/``（normKey 两级口径），非法条目跳过不污染存储。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import atomic_write_bytes
from .paths import program_dir

#: 全域记录上限（与 syncstore 一致；库实测约 1.9 万可播文件，留一倍余量）。
MAX_ENTRIES = 20000
#: 一次增量 POST 允许的最大条数（正常上报 / 迁移远小于此，防御异常大请求）。
MAX_BATCH = 5000

SYNC_NAME = "sync_watched.json"

_lock = threading.Lock()

#: 测试注入用：设置后覆盖存储文件位置，避免污染真实 data/（同 syncstore 口径）。
_path_override: Path | None = None


def sync_path() -> Path:
    if _path_override is not None:
        return _path_override
    return program_dir() / "data" / SYNC_NAME


def _valid_key(k: Any) -> str | None:
    """键规范：去空格、大写、必须含 ``/``（normKey「叶子目录/文件名」）。"""
    key = str(k or "").strip().upper()
    if not key or "/" not in key:
        return None
    return key[:512]


def _load() -> dict[str, Any]:
    """读盘并做结构清洗；坏文件一律当作空表，不抛异常。"""
    path = sync_path()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data.get("map")
                out: dict[str, dict[str, str]] = {}
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        key = _valid_key(k)
                        if key is None:
                            continue
                        if isinstance(v, dict):
                            out[key] = {"t": str(v.get("t") or "")}
                        else:
                            out[key] = {"t": str(v or "")}
                try:
                    rev = max(0, int(data.get("rev") or 0))
                except (TypeError, ValueError):
                    rev = 0
                return {"rev": rev,
                        "cleared_at": str(data.get("cleared_at") or ""),
                        "map": out}
    except (OSError, json.JSONDecodeError):
        pass
    return {"rev": 0, "cleared_at": "", "map": {}}


def _save(data: dict[str, Any]) -> None:
    path = sync_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            path, (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _trim(data: dict[str, Any]) -> None:
    """超限按 t 裁 LRU（保住最近点开过的）。"""
    m = data["map"]
    if len(m) > MAX_ENTRIES:
        keep = sorted(m.items(), key=lambda x: x[1].get("t", ""),
                      reverse=True)[:MAX_ENTRIES]
        data["map"] = dict(keep)


# ---------------------------------------------------------------------------
# 对外操作（均持锁）
# ---------------------------------------------------------------------------

def snapshot() -> dict[str, Any]:
    """全域已看快照（只读拷贝）：``{rev, cleared_at, map:{key:{"t":..}}}``。"""
    with _lock:
        d = _load()
        return {"rev": d["rev"], "cleared_at": d["cleared_at"],
                "map": {k: dict(v) for k, v in d["map"].items()}}


def apply(payload: dict[str, Any]) -> dict[str, Any]:
    """处理一次 POST 变更，返回结果 dict。

    * ``replace``（列表）→ 整表替换，以这批为准（文件导入；不做陈旧检查，
      因为这是用户在设置里的明确动作，且前端导入前会先 GET 最新 rev）。
    * ``add`` / ``del``（列表或对象）→ 增量并集 / 删除；payload 带 ``rev``
      且与服务端不一致时返回 ``stale:true``（HTTP 409），拒绝复活旧数据。
    """
    now = _now_iso()
    with _lock:
        data = _load()
        cur_rev = data["rev"]

        rep = payload.get("replace")
        if isinstance(rep, list):
            new_map: dict[str, dict[str, str]] = {}
            for k in rep[:MAX_ENTRIES]:
                key = _valid_key(k)
                if key is not None:
                    new_map[key] = {"t": now}
            data["map"] = new_map
            _trim(data)
            _save(data)
            return {"ok": True, "rev": data["rev"], "count": len(data["map"]),
                    "updated": len(new_map), "mode": "replace"}

        incoming_rev = payload.get("rev")
        if (isinstance(incoming_rev, (int, float))
                and int(incoming_rev) != cur_rev):
            return {"ok": False, "stale": True, "rev": cur_rev,
                    "count": len(data["map"]),
                    "error": "rev stale, please pull first"}

        updated = 0
        m = data["map"]
        add = payload.get("add")
        if isinstance(add, (list, dict)):
            seq = list(add.keys()) if isinstance(add, dict) else list(add)
            for k in seq[:MAX_BATCH]:
                key = _valid_key(k)
                if key is not None and key not in m:
                    m[key] = {"t": now}
                    updated += 1
        dele = payload.get("del")
        if isinstance(dele, (list, dict)):
            seq = list(dele.keys()) if isinstance(dele, dict) else list(dele)
            for k in seq[:MAX_BATCH]:
                key = _valid_key(k)
                if key is not None and key in m:
                    del m[key]
                    updated += 1
        _trim(data)
        if updated:
            _save(data)
        return {"ok": True, "rev": data["rev"], "count": len(data["map"]),
                "updated": updated, "mode": "merge"}


def clear_all() -> dict[str, Any]:
    """清空全域记录并令 ``rev++``（防离线设备复活旧数据）。"""
    now = _now_iso()
    with _lock:
        data = _load()
        n = len(data["map"])
        data["rev"] = data["rev"] + 1
        data["cleared_at"] = now
        data["map"] = {}
        _save(data)
        return {"ok": True, "cleared": n, "rev": data["rev"], "count": 0}


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
    """处理 ``/api/sync/watched*``；非本模块路由返回 False。"""
    if path == "/api/sync/watched":
        if handler.command in ("GET", "HEAD"):
            d = snapshot()
            handler._send_json(200, {"ok": True, "rev": d["rev"],
                                     "cleared_at": d["cleared_at"],
                                     "map": d["map"]})
            return True
        if handler.command == "POST":
            if not handler._origin_allowed():
                handler._send_json(403, {"ok": False,
                                         "error": "origin not allowed"})
                return True
            payload = _body(handler)
            # v2.10.0：坏 body（非 JSON / 空 body）经 _body() 会退化为 {}，
            # 若直接交给 apply({}) 会返回 200 ok=True 的空合并，前端会误判
            # 写入成功。与 syncstore 进度路由口径对齐：缺任一有效字段即 400。
            if not any(k in payload for k in ("replace", "add", "del")):
                handler._send_json(400, {"ok": False,
                                         "error": "body 需含 replace/add/del 之一"})
                return True
            res = apply(payload)
            handler._send_json(200 if res.get("ok") else 409, res)
            return True
        handler._send_json(405, {"ok": False, "error": "method not allowed"})
        return True
    if path == "/api/sync/watched/clear":
        if handler.command != "POST":
            handler._send_json(405, {"ok": False,
                                     "error": "method not allowed, use POST"})
            return True
        if not handler._origin_allowed():
            handler._send_json(403, {"ok": False,
                                     "error": "origin not allowed"})
            return True
        handler._drain_body()
        handler._send_json(200, clear_all())
        return True
    return False


__all__ = ["handle", "snapshot", "apply", "clear_all", "sync_path",
           "MAX_ENTRIES", "SYNC_NAME"]
