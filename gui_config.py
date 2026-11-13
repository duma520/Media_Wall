"""Media_Wall 桌面控制面板 —— 设置持久化（分两处落盘，互不干扰）。

* ``config.json``       —— 服务端配置（``media_root`` / ``categories`` /
  历史记录相关），与网页「设置」共用同一份文件；
* ``gui_settings.json`` —— 仅桌面面板使用的偏好（端口 / 自动启动）。

所有设置**改完即落盘**（原子写），下次启动自动加载。
"""

from __future__ import annotations

import json
from typing import Any

from mediawall.common import (
    DEFAULT_MEDIA_ROOT,
    DEFAULT_PORT,
    atomic_write_bytes,
    load_config,
    save_config,
)

from mediawall.paths import program_dir

PROGRAM_DIR = program_dir()
GUI_SETTINGS_PATH = PROGRAM_DIR / "gui_settings.json"

#: 历史记录保留条数默认值；0 表示不限制
DEFAULT_HISTORY_LIMIT = 500

DEFAULT_GUI_SETTINGS: dict[str, Any] = {
    "port": DEFAULT_PORT,
    #: 监听地址（v2.4.0）：对应 server.py 的 --host。默认 0.0.0.0（手机/平板可访问），
    #: 可改 127.0.0.1（仅本机）。
    "host": "0.0.0.0",
    "auto_start": True,
    #: 全量重建偏好（v2.4.0）：勾选后「重新扫描全部」走 --force（忽略增量缓存）。
    "scan_force": False,
    #: 控制面板窗口尺寸（v2.2.1）：用户调过窗口大小就记住，下次启动直接恢复。
    "win_w": 940,
    "win_h": 760,
}

#: 允许的监听地址集合（v2.4.0）。0.0.0.0 / 127.0.0.1 之外，
#: 只放行「本机局域网 IPv4」格式（以 192.168. / 10. / 172.16-31. 开头）。
_ALLOWED_HOSTS = ("0.0.0.0", "127.0.0.1", "::1", "localhost")


def normalize_host(host: Any) -> str:
    """校验并归一监听地址；非法值回退 ``0.0.0.0``。"""
    raw = str(host or "").strip()
    if raw in _ALLOWED_HOSTS:
        return raw
    # 局域网 IPv4（本机可能出现的私有网段），供用户在多个网卡间选择
    if raw.startswith("192.168.") or raw.startswith("10.") or raw.startswith("172."):
        parts = raw.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return raw
    return "0.0.0.0"


def load_gui_settings() -> dict[str, Any]:
    """读取 GUI 偏好；缺失/损坏时用默认值兜底。"""
    settings = dict(DEFAULT_GUI_SETTINGS)
    try:
        if GUI_SETTINGS_PATH.is_file():
            data = json.loads(GUI_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in DEFAULT_GUI_SETTINGS:
                    if key in data:
                        settings[key] = data[key]
    except Exception:  # noqa: BLE001 - 读不到偏好不影响使用
        pass
    try:
        settings["port"] = int(settings.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        settings["port"] = DEFAULT_PORT
    settings["host"] = normalize_host(settings.get("host"))
    settings["auto_start"] = bool(settings.get("auto_start", True))
    settings["scan_force"] = bool(settings.get("scan_force", False))
    # 窗口尺寸做范围校验：太小会看不到内容，太大超出屏幕也没意义
    try:
        settings["win_w"] = max(720, min(int(settings.get("win_w") or 940), 4000))
        settings["win_h"] = max(480, min(int(settings.get("win_h") or 760), 3000))
    except (TypeError, ValueError):
        settings["win_w"], settings["win_h"] = 940, 760
    return settings


def save_gui_settings(settings: dict[str, Any]) -> bool:
    """保存 GUI 偏好（原子写，**合并**进现有设置）。

    v2.3.0 修复：原先整体覆盖写入，会把 v2.2.1 的窗口尺寸记忆
    （win_w/win_h）一并冲掉——设置页保存一次，窗口记忆就没了。
    现在先读现有值再合并，任何调用方只更新自己关心的键。
    """
    current = load_gui_settings()
    current.update(settings)
    payload = json.dumps(current, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_bytes(GUI_SETTINGS_PATH, payload.encode("utf-8"))


def service_config() -> dict[str, Any]:
    """当前服务端配置（含默认值兜底）。"""
    cfg = load_config()
    cfg.setdefault("media_root", DEFAULT_MEDIA_ROOT)
    cfg.setdefault("categories", None)
    cfg.setdefault("history_limit", DEFAULT_HISTORY_LIMIT)
    cfg.setdefault("history_record_pages", True)
    return cfg


def save_service_config(media_root: str, categories: Any,
                        history_limit: int | None = None,
                        history_record_pages: bool | None = None) -> bool:
    """写回服务端配置（保留 config.json 里的其它字段）。

    ``history_limit`` / ``history_record_pages`` 为 None 时保持原值不动，
    这样「只改媒体根」不会顺手把历史设置覆盖成默认值。
    """
    cfg = load_config()
    cfg["media_root"] = media_root
    cfg["categories"] = categories
    if history_limit is not None:
        cfg["history_limit"] = max(0, int(history_limit))
    if history_record_pages is not None:
        cfg["history_record_pages"] = bool(history_record_pages)
    return save_config(cfg)


def categories_to_text(categories: Any) -> str:
    """分区白名单 → 便于编辑的逗号分隔文本。"""
    if not categories:
        return ""
    try:
        return "，".join(str(c) for c in categories)
    except TypeError:
        return ""


def text_to_categories(text: str) -> list[str] | None:
    """逗号分隔文本 → 分区白名单；空文本返回 None（= 自动发现全部）。"""
    raw = (text or "").strip().replace("，", ",")
    items = [c.strip() for c in raw.split(",") if c.strip()]
    return items or None


def history_settings() -> tuple[int, bool]:
    """读取历史记录设置：``(保留条数, 是否记录页面访问)``。"""
    cfg = service_config()
    try:
        limit = max(0, int(cfg.get("history_limit", DEFAULT_HISTORY_LIMIT)))
    except (TypeError, ValueError):
        limit = DEFAULT_HISTORY_LIMIT
    return limit, bool(cfg.get("history_record_pages", True))
