"""Media_Wall 服务端内部包（由 AV_Wall v1.9.0 的 avwall 包泛化而来）。

模块划分与 AV_Wall 一致：

- ``mediawall.common`` —— 配置读写、媒体根推导与校验、**分区自动发现**、
  路径安全、Range 解析、局域网 IP
- ``mediawall.scan``   —— 扫描进程管理（``scanner.py`` 的调度，支持按分区扫描）
- ``mediawall.httpd``  —— HTTP 服务与路由
- ``mediawall.media``  —— 视频/音频流与缩略图输出
- ``mediawall.banner`` —— 启动横幅与 ASCII 二维码

顶层 ``server.py`` 只剩入口（参数解析 + 启动），``scanner.py`` 复用
``mediawall.common`` 里的常量，避免两处口径走偏。

与 AV_Wall 的核心差异：媒体根（示例 ``\\\\NAS\\Share\\Download``）下有一级「分区」
目录（01 电影 / 10 电视节目 / … / 99 其他），扫描以「分区内叶子内容目录」为条目，
前端先选分区、再选子目录（年代/系列），最后看海报墙。
"""

from __future__ import annotations

VERSION = "2.10.2"
SERVER_TOKEN = f"MediaWall/{VERSION}"

__all__ = ["VERSION", "SERVER_TOKEN"]
