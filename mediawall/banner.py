"""Media_Wall 启动横幅与 ASCII 二维码（由 AV_Wall 的 avwall/banner.py 泛化）。

纯展示代码：打印访问地址、局域网 IP、二维码矩阵，方便手机/平板扫码访问。
二维码依赖可选的 ``qrcode`` 库，缺失时优雅降级（只打印 URL 文本）。
"""

from __future__ import annotations

from pathlib import Path

from .common import BANNER_WIDTH


def print_banner(host: str, port: int, root: Path, lan_ip: str, scanning: bool,
                 program_dir: Path | None = None) -> None:
    """打印醒目访问信息块（含 ASCII 二维码，方便手机/平板扫码）。"""
    line = "=" * BANNER_WIDTH
    print(line, flush=True)
    print("  MediaWall 多分区海报墙服务已启动".ljust(BANNER_WIDTH - 1), flush=True)
    print(line, flush=True)
    print(f"  本机访问   : http://127.0.0.1:{port}", flush=True)
    if host in ("0.0.0.0", "::", ""):
        url = f"http://{lan_ip}:{port}/"
        print(f"  局域网访问 : {url}", flush=True)
        print("  手机/平板  : 连接同一 Wi-Fi 后，扫码或输入上面的局域网地址", flush=True)
        _print_qr(url)
    else:
        print(f"  服务地址   : http://{host}:{port}", flush=True)
    print(f"  媒体根目录 : {root}", flush=True)
    if program_dir is not None:
        print(f"  程序目录   : {program_dir}", flush=True)
    if scanning:
        print("  初始扫描   : 后台进行中（完成后 /api/library 可用）", flush=True)
    else:
        print("  初始扫描   : 已跳过（未找到 scanner.py）", flush=True)
    print("  停止服务   : 在本窗口按 Ctrl+C；或本机 POST /api/shutdown", flush=True)
    print(line, flush=True)


def _print_qr(url: str) -> None:
    """在控制台打印 ASCII 二维码；缺少 qrcode 库时优雅降级。"""
    try:
        import qrcode  # type: ignore
    except Exception:  # noqa: BLE001 - 纯展示增强，缺失不影响服务
        print("  （未安装 qrcode 库，跳过二维码；手动输入局域网地址即可）", flush=True)
        return
    try:
        matrix = qrcode.QRCode(border=2)
        matrix.add_data(url)
        matrix.make(fit=True)
        for row in matrix.get_matrix():
            print("  " + "".join("#" if cell else " " for cell in row), flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  （二维码生成失败，跳过：{exc}）", flush=True)
