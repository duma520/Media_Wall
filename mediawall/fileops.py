"""文件与目录操作：删除到回收站、移动、撤回、清空/恢复回收站。

安全边界（这些是硬约束，不是可选项）
------------------------------------
1. **只允许在媒体根之内操作**：所有相对路径解析后必须仍在根内（``..`` 逃逸
   一律拒绝），根目录本身、分区根（第一层目录）、回收站内部目录都不可删除。
2. **删除 = 移动到回收站**，永不直接物理删除；物理删除只发生在
   :func:`empty_trash`，且需要调用方二次确认。
3. **不覆盖**：目标已存在同名项时一律报错，绝不静默覆盖。
4. 回收站放在**媒体根内的隐藏目录** ``.mediawall_trash``：同一卷内 ``os.replace``
   是瞬时操作，46GB 的目录也是秒级；若放到程序目录（另一个卷）会变成跨盘复制，
   几十 GB 的片子会卡死。扫描器必须忽略该目录（见 scanner_core.IGNORE_DIR_RE）。

撤回栈：``<程序目录>/data/undo_log.json``，最多 :data:`UNDO_MAX` 步，
跨重启保留；清空回收站不可撤回（不入栈）。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import atomic_write_bytes
from .paths import program_dir

TRASH_DIRNAME = ".mediawall_trash"
TRASH_INDEX = ".trash_index.json"
UNDO_NAME = "undo_log.json"
UNDO_MAX = 50

OP_TRASH = "trash"
OP_MOVE = "move"


class FileOpError(Exception):
    """可预期的操作失败（路径非法、目标已存在、源不存在等）。"""


# ---------------------------------------------------------------------------
# 路径守卫
# ---------------------------------------------------------------------------

def norm_root(root: str | Path) -> Path:
    """规范化媒体根（去尾分隔符、解析 UNC）。"""
    return Path(str(root)).expanduser()


def _abs(p: Path) -> str:
    """用于比较的绝对路径字符串（Windows 大小写不敏感）。"""
    return os.path.normcase(os.path.normpath(str(p)))


def inside(root: Path, target: Path) -> bool:
    """target 是否在 root 之内（含等于 root 的情况由调用方另行判断）。"""
    try:
        a = _abs(root)
        b = _abs(target)
    except OSError:
        return False
    return b == a or b.startswith(a + os.sep)


def is_protected(root: Path, p: Path) -> bool:
    """不可删除/移动的对象：根本身、分区根（第一层）、回收站内部。"""
    if _abs(p) == _abs(root):
        return True
    try:
        rel = p.relative_to(root)
    except ValueError:
        return True
    parts = [x for x in rel.parts if x]
    if not parts:
        return True
    if parts[0].lower() == TRASH_DIRNAME:
        return True
    # 第一层 = 分区目录（01 电影 / 30 动漫 …），删它等于删整个分区
    if len(parts) == 1:
        return True
    return False


def resolve(root: Path, rel: str) -> Path:
    """相对路径 → 绝对路径，并校验在根内且存在。"""
    rel = (rel or "").strip().replace("\\", "/").strip("/")
    if not rel:
        raise FileOpError("路径为空")
    target = root / rel
    if not inside(root, target):
        raise FileOpError(f"路径越界：{rel}")
    if not target.exists():
        raise FileOpError(f"目标不存在：{rel}")
    return target


def rel_of(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# 回收站
# ---------------------------------------------------------------------------

def trash_root(root: Path) -> Path:
    return root / TRASH_DIRNAME


def _index_path(root: Path) -> Path:
    return trash_root(root) / TRASH_INDEX


def _load_index(root: Path) -> dict[str, Any]:
    path = _index_path(root)
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": []}


def _save_index(root: Path, data: dict[str, Any]) -> None:
    path = _index_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            path, (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    except OSError:
        pass


def _new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def list_trash(root: Path) -> list[dict[str, Any]]:
    """回收站内容，最新在前；顺手剔掉磁盘上已不存在的条目。"""
    data = _load_index(root)
    items = []
    dirty = False
    for it in data["items"]:
        stored = trash_root(root) / str(it.get("stored") or "")
        if not stored.exists():
            dirty = True
            continue
        items.append(it)
    if dirty:
        data["items"] = items
        _save_index(root, data)
    items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return items


def _unique_stored(root: Path, base: str) -> Path:
    """在回收站里取一个不冲突的存储名。"""
    tr = trash_root(root)
    stored = tr / base
    if not stored.exists():
        return stored
    stem, suffix = os.path.splitext(base)
    for i in range(2, 1000):
        cand = tr / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    raise FileOpError("回收站内同名文件过多，请先清空回收站")


def move_to_trash(root: Path, rel: str, note: str = "") -> dict[str, Any]:
    """把 root 下的 rel 移到回收站，返回撤回记录。"""
    root = norm_root(root)
    target = resolve(root, rel)
    if is_protected(root, target):
        raise FileOpError("该目录受保护（媒体根、分区根或回收站内部），不允许删除")

    tr = trash_root(root)
    tr.mkdir(parents=True, exist_ok=True)
    stamp = _new_id()
    base = f"{stamp}__{target.name}"
    stored = _unique_stored(root, base)
    entry_id = stored.name
    kind = "dir" if target.is_dir() else "file"
    # 记录占用空间：回收站面板要显示「共占多少」，不记就得每次刷新重算整棵子树。
    # 删除的分区根/媒体根本身已被 is_protected 挡住，这里的子树规模可控。
    size = _size_of(target)

    try:
        os.replace(target, stored)
    except OSError as exc:
        raise FileOpError(f"移动到回收站失败：{exc}") from exc

    item = {
        "id": entry_id,
        "stored": entry_id,
        "name": target.name,
        "orig_rel": rel_of(root, target),
        "kind": kind,
        "bytes": size,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": note or "",
    }
    data = _load_index(root)
    data["items"].append(item)
    _save_index(root, data)

    entry = {
        "op": OP_TRASH,
        "ts": item["ts"],
        "trash_id": entry_id,
        "orig_rel": item["orig_rel"],
        "name": item["name"],
    }
    push_undo(entry)
    return entry


def restore(root: Path, entry_id: str) -> str:
    """把回收站里的一项恢复到原位置；原位置已有同名项时报错（不覆盖）。"""
    root = norm_root(root)
    data = _load_index(root)
    item = next((x for x in data["items"] if x.get("id") == entry_id), None)
    if item is None:
        raise FileOpError("回收站里找不到该项（可能已被恢复或清空）")

    stored = trash_root(root) / str(item.get("stored") or "")
    if not stored.exists():
        raise FileOpError("回收站文件已丢失，无法恢复")

    orig_rel = str(item.get("orig_rel") or "")
    dest = root / orig_rel
    if not inside(root, dest):
        raise FileOpError("原路径越界，拒绝恢复")
    if dest.exists():
        raise FileOpError(f"原位置已存在同名项，请先处理：{orig_rel}")

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stored, dest)
    except OSError as exc:
        raise FileOpError(f"恢复失败：{exc}") from exc

    data["items"] = [x for x in data["items"] if x.get("id") != entry_id]
    _save_index(root, data)
    return orig_rel


def empty_trash(root: Path) -> dict[str, Any]:
    """物理删除回收站全部内容，返回 (项数, 释放字节)。不可撤回。"""
    root = norm_root(root)
    tr = trash_root(root)
    if not tr.exists():
        return {"count": 0, "bytes": 0}
    count = 0
    freed = 0
    errors: list[str] = []
    for child in list(tr.iterdir()):
        if child.name == TRASH_INDEX:
            continue
        try:
            freed += _size_of(child)
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=False)
            else:
                child.unlink()
            count += 1
        except OSError as exc:
            errors.append(f"{child.name}: {exc}")
    _save_index(root, {"items": []})
    return {"count": count, "bytes": freed, "errors": errors}


def _size_of(p: Path) -> int:
    """递归统计大小（仅用于清空回收站时报告释放空间）。"""
    try:
        if p.is_file():
            return p.stat().st_size
        total = 0
        for base, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(base, f))
                except OSError:
                    continue
        return total
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 移动
# ---------------------------------------------------------------------------

def move_item(root: Path, src_rel: str, dst_dir_rel: str) -> dict[str, Any]:
    """把 root 下的 src_rel 移动到 dst_dir_rel 目录下（同卷瞬移）。"""
    root = norm_root(root)
    src = resolve(root, src_rel)
    if is_protected(root, src):
        raise FileOpError("该目录受保护（媒体根、分区根或回收站内部），不允许移动")

    dst_dir_rel = (dst_dir_rel or "").strip().replace("\\", "/").strip("/")
    dst_dir = resolve(root, dst_dir_rel) if dst_dir_rel else root
    if not dst_dir.is_dir():
        raise FileOpError("目标不是目录")
    if _abs(dst_dir) == _abs(src.parent):
        raise FileOpError("源和目标父目录相同，无需移动")
    if src.is_dir() and inside(src, dst_dir):
        raise FileOpError("不能把目录移动到自己内部")

    dest = dst_dir / src.name
    if dest.exists():
        raise FileOpError(f"目标目录下已存在同名项：{src.name}")

    try:
        os.replace(src, dest)
    except OSError as exc:
        raise FileOpError(f"移动失败：{exc}") from exc

    entry = {
        "op": OP_MOVE,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "src_rel": rel_of(root, src),
        "dst_rel": rel_of(root, dest),
        "name": src.name,
    }
    push_undo(entry)
    return entry


# ---------------------------------------------------------------------------
# 撤回栈
# ---------------------------------------------------------------------------

#: 测试注入用：设置后覆盖撤回栈文件位置，避免污染真实 data/
_undo_override: Path | None = None


def undo_path() -> Path:
    if _undo_override is not None:
        return _undo_override
    return program_dir() / "data" / UNDO_NAME


def load_undo() -> list[dict[str, Any]]:
    path = undo_path()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _save_undo(entries: list[dict[str, Any]]) -> None:
    path = undo_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            path, (json.dumps(entries[-UNDO_MAX:], ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    except OSError:
        pass


def push_undo(entry: dict[str, Any]) -> None:
    entries = load_undo()
    entries.append(entry)
    _save_undo(entries)


def undo_last(root: Path) -> str:
    """撤回最近一次操作，返回人类可读结果；栈空时报错。"""
    root = norm_root(root)
    entries = load_undo()
    if not entries:
        raise FileOpError("没有可撤回的操作")
    entry = entries[-1]
    op = entry.get("op")

    if op == OP_TRASH:
        restored = restore(root, str(entry.get("trash_id") or ""))
        entries.pop()
        _save_undo(entries)
        return f"已从回收站恢复：{restored}"

    if op == OP_MOVE:
        src_rel = str(entry.get("src_rel") or "")
        dst_rel = str(entry.get("dst_rel") or "")
        dest = root / dst_rel          # 该文件当前所在位置
        back_dir = (root / src_rel).parent  # 它原本所在的目录
        if not dest.exists():
            raise FileOpError(f"被移动的项已不存在，无法撤回：{dst_rel}")
        back = back_dir / dest.name
        if inside(root, back) is False:
            raise FileOpError("原路径越界，拒绝撤回")
        if back.exists():
            raise FileOpError(f"原位置已存在同名项，无法撤回：{src_rel}")
        try:
            back_dir.mkdir(parents=True, exist_ok=True)
            os.replace(dest, back)
        except OSError as exc:
            raise FileOpError(f"撤回移动失败：{exc}") from exc
        entries.pop()
        _save_undo(entries)
        return f"已撤回移动：{dst_rel} → {src_rel}"

    raise FileOpError(f"未知操作类型：{op}")
