"""Media_Wall 海报墙扫描器（CLI 入口）。

用途
----
遍历**媒体根目录**（默认 ``\\\\NAS\\Share\\Download``，可配置）下自动发现的
「分区」一级目录（01 电影 / 10 电视节目 / … / 99 其他），把每个分区的
「叶子内容目录」（本层含视频/音频/图片/文档的目录）整理成一条记录，
产出供 index.html / server.py 消费的数据文件：

- ``data/library.json``      结构化数据（UTF-8, ensure_ascii=False）
- ``data/library.js``        ``window.__MW_LIBRARY__ = <同一 JSON>;``
                             （供 file:// 下 <script> 标签加载，绕开 fetch CORS 限制）
- ``data/library.meta.json`` 增量缓存（每个条目目录的签名 + 旧记录 + 分区扫描信息）

以上三个文件都写在**程序目录**（本脚本所在目录）下的 ``data/``。

扫描规则与数据模型详见 ``mediawall/scanner_core.py`` 模块文档。

守卫（继承 AV_Wall v1.8.0，按分区维度泛化）
------------------------------------------
- **全部分区都读不到时（NAS 掉线等），直接报错退出（返回码 3）且不写任何
  输出文件**。原先会把「全部失败」写成 count=0 的空库覆盖真实数据。
- 按分区扫描（``--category``）时，其余分区整体复用缓存，不会丢失。
- 单叶子目录读取失败：记入 warnings，保留其旧缓存记录（若有），不让整个扫描崩。

CLI
---
    python scanner.py                    # 增量扫描全部分区（默认）
    python scanner.py --quiet            # 只打汇总
    python scanner.py --force            # 全量重建（忽略缓存）
    python scanner.py --root PATH        # 覆盖媒体根
    python scanner.py --category 01 电影 # 只重扫一个分区

仅用标准库；Python 3.10。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from mediawall.common import derive_root
from mediawall.scanner_core import (
    SCANNER_VERSION,
    build_library,
    program_data_dir,
    save_meta,
    scan_all,
    write_outputs,
)


def default_root() -> str:
    """推导媒体根目录（与 server 端共用 ``mediawall.common.derive_root``，口径一致）。

    优先级：``--root`` → ``config.json`` 的 ``media_root`` → ``DEFAULT_MEDIA_ROOT``。
    返回值保持 UNC 原始字符串形式（不做 abspath / 规范化），
    否则会与 ``library.meta.json`` 里的 ``root`` 不一致而触发全量重扫。
    """
    return str(derive_root(None))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Media_Wall 海报墙扫描器：遍历媒体根下自动发现的分区目录，"
                    "生成 library.json / library.js",
    )
    parser.add_argument(
        "--root", default=None,
        help="媒体根绝对路径（默认取 config.json 的 media_root，其次写死默认值）",
    )
    parser.add_argument("--force", action="store_true", help="全量重建，忽略增量缓存")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总行")
    parser.add_argument(
        "--category", default=None, metavar="NAME",
        help="只重扫一个分区（如 \"01 电影\"）；其余分区条目从缓存整体复用",
    )
    parser.add_argument("--version", action="version", version=f"mediawall-scanner {SCANNER_VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root if args.root else default_root()
    # UNC 路径保持原样，不做 abspath / 规范化
    if not root.startswith("\\\\"):
        root = os.path.abspath(root)

    if not os.path.isdir(root):
        print(f"[错误] 根目录不存在或不可访问: {root}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(f"Media_Wall 扫描器 v{SCANNER_VERSION} · 媒体根: {root}")
        scope = f"分区 {args.category}" if args.category else "全部分区"
        print(f"模式: {'全量重建' if args.force else '增量扫描'} · 范围: {scope}")

    started = time.perf_counter()
    items, warnings, stats, new_entries, new_cat = scan_all(
        root, force=args.force, only_category=args.category, quiet=args.quiet,
    )

    # NAS 掉线守卫：本次**目标范围内**所有分区都读失败 → 环境故障，
    # 绝不写出空库覆盖真实数据。
    if stats["cats_ok"] == 0 and stats["cats_failed"] > 0:
        print(
            f"[错误] 目标分区全部无法读取（{stats['cats_failed']} 个失败），"
            f"疑似共享不可达：{root}",
            file=sys.stderr,
        )
        for w in warnings:
            print(f"  ! {w}", file=sys.stderr)
        print("[错误] 已保留原有数据文件，未做覆盖。", file=sys.stderr)
        return 3

    library = build_library(root, items, warnings, cat_scan_times=new_cat)

    # 输出到**程序目录**的 data/（不写进媒体根）
    data_dir = program_data_dir()
    json_path, js_path, json_bytes = write_outputs(data_dir, library)

    try:
        save_meta(data_dir / "library.meta.json", root, new_entries, new_cat)
    except OSError as exc:
        warnings.append(f"[缓存写入失败] {data_dir / 'library.meta.json'}: {exc}")

    elapsed = time.perf_counter() - started

    if not args.quiet:
        print(
            f"缓存: 复用 {stats['reused']} 个 / 重扫 {stats['rescanned']} 个 / "
            f"跳过 {stats['skipped']} 个；警告 {len(warnings)} 条"
        )
        for w in warnings:
            print(f"  ! {w}")

    cats_line = " / ".join(
        f"{c['name']}:{c['items']}" for c in library["categories"] if c["items"]
    )
    print(
        f"扫描完成: 条目 {library['count']} 个 / 可播文件 {library['total_videos']} 个 / "
        f"海报 {library['total_posters']} 张 / 截图 {library['total_shots']} 张，"
        f"耗时 {elapsed:.1f}s"
    )
    if cats_line:
        print(f"分区分布: {cats_line}")
    print(f"数据文件: {json_path} ({json_bytes} 字节)")
    print(f"JS 文件:  {js_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
