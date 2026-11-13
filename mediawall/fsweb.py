"""网页端文件管理（v2.1.0）：列出目录、删除到回收站、移动、撤回。

这些端点让「浏览器里的海报墙」也能删/移文件，而不是只能在桌面 GUI 里操作。

路由（全部由 :func:`handle` 分派，返回 ``True`` 表示已处理）：

===========================  ======  ====================================
路径                         方法    作用
===========================  ======  ====================================
``/api/fs/dirs?path=``        GET     列出某目录下的子目录（「移动到」选择用）
``/api/fs/delete``            POST    删除到回收站 ``{"path": 相对路径}``
``/api/fs/move``              POST    移动 ``{"path":, "dest": 目标目录}``
``/api/trash``                GET     回收站列表 + 占用空间
``/api/trash/restore``        POST    从回收站恢复 ``{"id": 条目 id}``
``/api/trash/empty``          POST    清空回收站
``/api/undo``                 POST    撤回上一次删除/移动
===========================  ======  ====================================

安全边界全部落在 :mod:`mediawall.fileops`（禁止越出媒体根、禁止删媒体根/一级分区/
回收站内部、目标重名拒绝），本模块只做 HTTP 包装，不重复实现一遍守卫逻辑。
写操作额外要求同源（:meth:`Handler._origin_allowed`），并把 ``Host`` 限制为本机地址，
这样局域网里的手机能删、但恶意网页的 CSRF 拿不到写权限。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import fileops
from .common import list_categories

#: 列目录时超过这个数量就截断（避免一次返回几千个子目录把手机卡住）。
MAX_DIRS = 500


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _json_body(handler) -> dict[str, Any]:
    """读取并解析 JSON body；空 body 或坏 JSON 一律当空字典。"""
    import json

    raw = handler._drain_body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _query(handler) -> dict[str, list[str]]:
    """解析 URL query（保持空白值，便于区分 ?path= 与没有 path）。"""
    import urllib.parse

    try:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query,
                                     keep_blank_values=True)
    except ValueError:
        return {}


def _guard_write(handler, action: str) -> bool:
    """写操作的统一门禁：必须是 POST + 同源。"""
    if handler.command != "POST":
        handler._send_json(405, {"ok": False, "error": "method not allowed, use POST"})
        return False
    if not handler._origin_allowed():
        handler._send_json(403, {"ok": False, "error": "origin not allowed"})
        return False
    return True


def _refresh(handler, rel: str) -> str:
    """操作成功后重扫受影响的分区，返回触发说明（失败也不影响操作结果）。

    分区 = 相对路径的第一段（``01 电影/xxx`` → ``01 电影``）。
    重扫是异步的，这里只负责发出去；进度由 ``GET /api/status`` 暴露。
    """
    root = handler._root
    seg = str(rel or "").replace("\\", "/").strip("/").split("/")[0]
    if not seg:
        return ""
    try:
        if seg not in list_categories(root):
            return ""
        if handler._srv.scan.running:
            return "扫描进行中，稍后自动生效"
        handler._srv.scan.run_async(category=seg)
        return f"已触发 {seg} 重扫"
    except Exception:  # noqa: BLE001 - 重扫失败不该让文件操作报错
        return ""


def _trash_summary(handler) -> dict[str, Any]:
    try:
        items = fileops.list_trash(handler._root)
    except Exception:  # noqa: BLE001
        return {"count": 0, "bytes": 0}
    total = 0
    for it in items:
        try:
            total += int(it.get("bytes") or 0)
        except (TypeError, ValueError):
            pass
    return {"count": len(items), "bytes": total}


# ---------------------------------------------------------------------------
# 各端点
# ---------------------------------------------------------------------------

def _list_dirs(handler, query: dict[str, list[str]]) -> None:
    """``GET /api/fs/dirs?path=<相对路径>``：列出子目录，供「移动到」选择。"""
    raw = (query.get("path") or [""])[0]
    rel = str(raw or "").replace("\\", "/").strip("/")
    root = handler._root
    try:
        base = fileops.resolve(root, rel)
    except fileops.FileOpError as exc:
        handler._send_json(400, {"ok": False, "error": str(exc)})
        return
    if not base.is_dir():
        handler._send_json(400, {"ok": False, "error": "目标不是目录"})
        return

    dirs: list[dict[str, Any]] = []
    try:
        with os.scandir(str(base)) as it:
            for entry in it:
                if len(dirs) >= MAX_DIRS:
                    break
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if entry.name.startswith("."):
                    continue
                try:
                    if fileops.is_protected(root, Path(entry.path)):
                        continue
                except Exception:  # noqa: BLE001 - 单个目录判定失败就跳过它
                    continue
                child = (rel + "/" + entry.name).strip("/") if rel else entry.name
                dirs.append({"name": entry.name, "rel": child})
    except OSError as exc:
        handler._send_json(500, {"ok": False, "error": f"读取目录失败：{exc}"})
        return

    dirs.sort(key=lambda d: d["name"])
    # 上级目录（方便在手机上一层层往回退）
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    handler._send_json(200, {"ok": True, "path": rel, "parent": parent,
                             "dirs": dirs, "truncated": len(dirs) >= MAX_DIRS})


def _delete(handler) -> None:
    if not _guard_write(handler, "delete"):
        return
    payload = _json_body(handler)
    rel = str(payload.get("path") or "").strip()
    if not rel:
        handler._send_json(400, {"ok": False, "error": "缺少 path"})
        return
    note = str(payload.get("note") or "")
    try:
        entry = fileops.move_to_trash(handler._root, rel, note=note)
    except fileops.FileOpError as exc:
        handler._send_json(400, {"ok": False, "error": str(exc)})
        return
    refresh = _refresh(handler, rel)
    handler._send_json(200, {"ok": True, "entry": entry, "trash": _trash_summary(handler),
                             "message": "已移入回收站", "refresh": refresh})


def _move(handler) -> None:
    if not _guard_write(handler, "move"):
        return
    payload = _json_body(handler)
    src = str(payload.get("path") or "").strip()
    dest = str(payload.get("dest") or "").strip()
    if not src:
        handler._send_json(400, {"ok": False, "error": "缺少 path"})
        return
    try:
        entry = fileops.move_item(handler._root, src, dest)
    except fileops.FileOpError as exc:
        handler._send_json(400, {"ok": False, "error": str(exc)})
        return
    refresh = _refresh(handler, src)
    handler._send_json(200, {"ok": True, "entry": entry, "message": "已移动", "refresh": refresh})


def _trash(handler) -> None:
    try:
        items = fileops.list_trash(handler._root)
    except Exception as exc:  # noqa: BLE001
        handler._send_json(500, {"ok": False, "error": f"读取回收站失败：{exc}"})
        return
    summary = _trash_summary(handler)
    handler._send_json(200, {"ok": True, "items": items, **summary})


def _trash_restore(handler) -> None:
    if not _guard_write(handler, "restore"):
        return
    payload = _json_body(handler)
    entry_id = str(payload.get("id") or "").strip()
    if not entry_id:
        handler._send_json(400, {"ok": False, "error": "缺少 id"})
        return
    try:
        orig = fileops.restore(handler._root, entry_id)
    except fileops.FileOpError as exc:
        handler._send_json(400, {"ok": False, "error": str(exc)})
        return
    refresh = _refresh(handler, orig)
    handler._send_json(200, {"ok": True, "path": orig, "message": "已恢复",
                             "refresh": refresh})


def _trash_empty(handler) -> None:
    if not _guard_write(handler, "empty"):
        return
    try:
        result = fileops.empty_trash(handler._root)
    except Exception as exc:  # noqa: BLE001
        handler._send_json(500, {"ok": False, "error": f"清空失败：{exc}"})
        return
    handler._send_json(200, {"ok": True, **result, "message": "回收站已清空"})


def _undo(handler) -> None:
    if not _guard_write(handler, "undo"):
        return
    try:
        message = fileops.undo_last(handler._root)
    except fileops.FileOpError as exc:
        handler._send_json(400, {"ok": False, "error": str(exc)})
        return
    refresh = _refresh(handler, "")
    handler._send_json(200, {"ok": True, "message": message, "refresh": refresh})


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------

_GET_ROUTES = {
    "/api/fs/dirs": lambda h, q: _list_dirs(h, q),
    "/api/trash": lambda h, q: _trash(h),
}

_POST_ROUTES = {
    "/api/fs/delete": lambda h, q: _delete(h),
    "/api/fs/move": lambda h, q: _move(h),
    "/api/trash/restore": lambda h, q: _trash_restore(h),
    "/api/trash/empty": lambda h, q: _trash_empty(h),
    "/api/undo": lambda h, q: _undo(h),
}

ROUTES = frozenset(_GET_ROUTES) | frozenset(_POST_ROUTES)


def handle(handler, path: str) -> bool:
    """处理文件管理相关请求；不属于本模块的路由返回 ``False``。"""
    if path not in ROUTES:
        return False
    query = _query(handler)
    if path in _GET_ROUTES:
        if handler.command not in ("GET", "HEAD"):
            handler._send_json(405, {"ok": False, "error": "method not allowed, use GET"})
            return True
        _GET_ROUTES[path](handler, query)
        return True
    _POST_ROUTES[path](handler, query)
    return True
