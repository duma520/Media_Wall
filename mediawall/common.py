"""Media_Wall 公共模块：配置、媒体根、分区发现、路径安全、Range 解析。

由 AV_Wall v1.9.0 的 ``avwall/common.py`` 泛化而来（v1.8.0 起从 server.py 拆出）。
只依赖 Python 3.10 标准库。

与 AV_Wall 的差异
----------------
* 媒体根是 ``\\\\NAS\\Share\\Download``（示例），下面**不再写死内容目录清单**：
  一级子目录（01 电影 … 99 其他 等）即「分区」，启动时自动发现；
  ``config.json`` 可选 ``categories`` 白名单固定顺序/子集。
* 文件类型在视频/图片之外新增音频与文档两组扩展名，
  供「96 音频 / 95 电子书 / 97 小说」这类非视频分区使用。
* 默认端口 8787（v2.5.2 起；与 AV_Wall 相同，不同时运行无冲突）。

职责：

* ``config.json`` 的读取与**原子写**（落盘即持久化，重启自动加载）
* 媒体根推导（``--root`` → ``config.json`` → 写死默认值）与可用性校验
* 分区自动发现（``list_categories``）
* 目录穿越防护（片段黑名单 + ``realpath`` + ``commonpath`` 双保险）
* ``Range`` 头解析（手机播放 / 拖动进度条依赖）
* 局域网 IP 探测
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import VERSION, SERVER_TOKEN  # noqa: F401  (SERVER_TOKEN 供 httpd 复用)
from .netadapter import list_ipv4_adapters
from .paths import program_dir


def no_window() -> int:
    """返回「子进程不创建控制台窗口」的 creationflags（v2.5.4）。

    在 Windows 上，``subprocess.run/Popen`` 启动 netstat / taskkill / ffprobe /
    ffmpeg 等**外部控制台程序**时，子进程会自己弹一个黑色控制台窗口（一闪而过）。
    给 creationflags 传 ``CREATE_NO_WINDOW``（0x08000000）可禁止创建该窗口。

    非 Windows 返回 0（无此概念）。
    """
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x08000000
    return 0

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_HOST = "0.0.0.0"
#: v2.5.2：默认端口改为 8787（与 AV_Wall 保持一致；两者不同时运行则无冲突）
DEFAULT_PORT = 8787
#: 取不到局域网 IP 时的最后兜底。仅为 RFC1918 示例地址，实际值由
#: :func:`detect_lan_ip` 动态探测；本机配置请在 config.json 里指定。
FALLBACK_LAN_IP = "192.168.1.100"

#: 程序目录：源码态是本包的上级，编译态是 exe 所在目录（见 ``mediawall.paths``）。
#: index.html / scanner.py / config.json / data/ 都以它为基准。
PROGRAM_DIR = program_dir()

#: 媒体根默认值（UNC 绝对路径示例）。程序目录与媒体目录分离，媒体根由
#: config.json 的 media_root 决定；这里只是缺配置时的兜底示例。
#: 首次使用请改成你自己的媒体目录（页面「设置 → 指定目录」或 POST /api/root），
#: 也可以直接用本地盘路径，如 ``D:\Media`` 或 ``E:\Videos``。
DEFAULT_MEDIA_ROOT = r"\\NAS\Share\Download"

#: config.json 里 ``categories`` 未配置时的兜底分区顺序（示例清单）。
#: 运行期**以实际存在的目录为准**：清单只用来在无配置时给分区定序，
#: 缺失目录自动跳过，新增目录自动追加。改这里不影响真实分区发现。
DEFAULT_CATEGORY_ORDER: tuple[str, ...] = (
    "01 电影", "10 电视节目", "11 电视剧", "20 综艺",
    "30 动漫", "31 动画电影", "40 系列动漫", "41 系列剧",
    "92 短片", "93 音乐", "94 纪录片", "95 电子书",
    "96 音频", "97 小说", "98 家庭视频", "99 其他",
)

#: 分区发现时忽略的一级目录名（群晖/系统目录等）。
IGNORE_CATEGORY_RE = re.compile(r"^[.#@\[\]]")
IGNORE_CATEGORY_NAMES = frozenset({
    "#recycle", "@eaDir", "@sync", "lost+found", "system volume information",
})

CHUNK_SIZE = 1024 * 1024               # 1MB 分块，UNC/SMB 流式读取
MAX_STATIC_BYTES = 64 * 1024 * 1024    # 静态资源内存上限，超过返回 413
MAX_BODY_BYTES = 1024 * 1024           # 请求体上限，超过返回 413（防内存吃满）
MAX_THUMB_BYTES = 32 * 1024 * 1024     # 缩略图内存上限，超过返回 413
SCAN_TIMEOUT_SEC = 30 * 60             # 单次扫描超时

#: 视频扩展名（小写比较）。服务端 /media/ 可播性与扫描器入库口径**同源**，
#: 避免「服务端能播而库里没有」。浏览器可直接解码的只有 mp4/mov/m4v/webm
#: （桌面 Edge 还能原生播 h264 的 mkv，安卓不行）；mkv/ts/flv 等解不了的
#: v2.9.0 起由 :mod:`mediawall.play` 按需转封装 mp4，hevc/wmv3/rv40 等
#: 编码级不可救的（本机无硬转码）仍靠前端「系统播放器」兜底下载播放。
#: v2.9.0 扩容：rmvb/rm（NAS 实测 616+162 个此前完全不入库）等常见容器；
#: 不收 .dat/.tp 等歧义扩展名（会把非视频数据文件误当视频入库）。
VIDEO_EXTS = frozenset({
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".ts", ".flv", ".wmv",
    ".rmvb", ".rm", ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".3gp",
    ".3g2", ".asf", ".f4v", ".divx", ".ogv", ".m2v",
})
#: 纯音频扩展名（96 录音笔分区 + 各分区 OST/戏曲）。与视频同走 /media/ 流式路由。
#: v2.9.0 扩容：浏览器不能直播的（wma/ape/wv/tta/aif/amr）由 play.py 转 mp3，
#: 能直播的（mp3/m4a/flac/wav/ogg）不转。
AUDIO_EXTS = frozenset({
    ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma",
    ".mka", ".m4b", ".ape", ".wv", ".tta", ".aif", ".aiff", ".amr",
})
#: 扫描器入库的图片扩展名（海报 / 截图 / 漫画页）。
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
#: 服务端 /thumb/ 允许的扩展名（与 IMAGE_EXTS 同源，兼容 gif/bmp）。
THUMB_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})
#: 文档扩展名（97 小说等）：只收录名称与大小，不支持在线打开。
DOC_EXTS = frozenset({".txt", ".pdf", ".epub", ".mobi", ".azw3",
                      ".doc", ".docx", ".chm", ".html", ".htm"})
#: 单个条目最多记录的文档数（小说分卷可能有上千个 txt，防止 library 膨胀）。
MAX_DOCS_PER_ITEM = 100
#: 单个条目最多记录的截图/内页数（漫画一本可能几百页，只留前 N 张预览）。
MAX_SHOTS_PER_ITEM = 40

STATIC_EXTS = frozenset({".js", ".css", ".json", ".ico"})

#: 静态路由里**禁止**访问的文件名（即便扩展名允许）。
#: config.json 含服务端配置、library.meta.json 含 NAS 绝对路径，
#: 都不该通过 HTTP 暴露给局域网里的任何设备。
DENY_STATIC_NAMES = frozenset({"config.json", "library.meta.json"})

#: 媒体根校验失败时的**统一**文案（防目录存在性 oracle）。
_INVALID_ROOT_MSG = "目录不可用（不存在 / 不是目录 / 不可读 / 下面没有任何子目录）"

STATIC_CONTENT_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}

INDEX_HTML_NAME = "index.html"
LIBRARY_REL = Path("data") / "library.json"

RANGE_RE = re.compile(r"^(\d*)-(\d*)$")
# Windows 保留设备名：命中即拒绝，避免 open() 行为异常。
# 注意必须允许尾随的空格与点——Windows 会把 "CON " / "CON." 也当成设备名，
# 但用严格正则匹配会漏掉它们（Windows 的路径规范化会先去掉尾随空格/点）。
RESERVED_NAME_RE = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$",
    re.IGNORECASE,
)

PRIVATE_PREFIXES = ("192.168.", "10.")
_LINK_LOCAL_PREFIX = "169.254."

# 启动时打印的访问信息分隔线宽度
BANNER_WIDTH = 66

#: config.json 与程序目录同处
_CONFIG_PATH = PROGRAM_DIR / "config.json"


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class PathSecurityError(Exception):
    """请求路径越过根目录或含非法片段时抛出。"""


class RangeNotSatisfiable(Exception):
    """``Range`` 起始偏移越界，应返回 416。"""

    def __init__(self, size: int) -> None:
        super().__init__(f"range start beyond size={size}")
        self.size = size


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def paths_equivalent(a: str, b: str) -> bool:
    """比较两个路径是否指向同一位置（Windows 下大小写不敏感）。"""
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except (OSError, ValueError):
        return a == b


def atomic_write_bytes(path: Path, data: bytes, retries: int = 6) -> bool:
    """原子写文件：先写 ``<name>.tmp`` 再 ``os.replace`` 覆盖。

    避免读到「写了一半」的 JSON / 配置。Windows 上目标文件被其它进程占用时
    ``os.replace`` 可能抛 ``PermissionError``，这里做有限次退避重试。
    成功返回 True，失败返回 False（调用方决定是否报错）。
    """
    tmp = path.with_name(path.name + ".tmp")
    for attempt in range(retries):
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
            return True
        except PermissionError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            if attempt < retries - 1:
                time.sleep(0.2 * (attempt + 1))
            continue
        except OSError:
            return False
    return False


def load_config() -> dict:
    """从 config.json 读取配置；缺失/损坏时返回默认值兜底。"""
    if _CONFIG_PATH.is_file():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                return cfg
        except Exception:  # noqa: BLE001 - 读不到配置不影响运行
            pass
    return {
        "media_root": DEFAULT_MEDIA_ROOT,
        "categories": None,   # None = 自动发现全部一级子目录
    }


def save_config(cfg: dict) -> bool:
    """把配置写回 config.json（原子写）；成功返回 True。"""
    payload = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_bytes(_CONFIG_PATH, payload.encode("utf-8"))


def config_path() -> Path:
    """config.json 的绝对路径（供 API 展示用）。"""
    return _CONFIG_PATH


def is_ignored_category(name: str) -> bool:
    """一级目录是否应被排除出分区（系统/回收站/隐藏目录等）。"""
    if IGNORE_CATEGORY_RE.match(name):
        return True
    return name.strip().lower() in IGNORE_CATEGORY_NAMES


# --------------------------------------------------------------------------
# 分区发现
# --------------------------------------------------------------------------


def list_categories(root: Path | str) -> tuple[str, ...]:
    """列出媒体根下的分区（一级子目录）。

    规则：
    1. 只取**实际存在**的一级子目录，排除系统目录（``is_ignored_category``）
       与符号链接（防环）；
    2. ``config.json`` 的 ``categories`` 若为非空字符串列表，按该白名单
       **定序并取交集**（配置里写了但实际不存在的分区跳过）；
    3. 未配置白名单时，先按 ``DEFAULT_CATEGORY_ORDER`` 排已知分区，
       再按名称追加清单之外的新目录（NAS 上新建目录无需改配置）。
    """
    try:
        present: list[str] = []
        with os.scandir(str(root)) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if is_ignored_category(entry.name):
                    continue
                present.append(entry.name)
    except OSError:
        return ()
    present_set = set(present)

    cfg = load_config().get("categories")
    if isinstance(cfg, list) and cfg:
        want = [str(x).strip() for x in cfg if isinstance(x, str) and str(x).strip()]
        want_set = set(want)
        ordered = [n for n in want if n in present_set]
        # 白名单之外的实际目录也追加进来，避免新分区被静默吞掉
        extra = sorted(n for n in present_set if n not in want_set)
        return tuple(ordered + extra)

    head = [n for n in DEFAULT_CATEGORY_ORDER if n in present_set]
    tail = sorted(n for n in present_set if n not in set(DEFAULT_CATEGORY_ORDER))
    return tuple(head + tail)


def category_child_count(root: Path | str, name: str) -> int:
    """分区下二级目录/文件个数（设置面板预览用）；读不到返回 -1。"""
    try:
        with os.scandir(os.path.join(str(root), name)) as it:
            return sum(1 for _ in it)
    except OSError:
        return -1


# --------------------------------------------------------------------------
# 媒体根
# --------------------------------------------------------------------------


def derive_root(explicit: str | None) -> Path:
    """推导媒体根目录（媒体所在处，**不随程序目录移动**）。

    优先级与 AV_Wall 一致：

    1. ``--root`` 命令行参数（最高）；
    2. ``config.json`` 的 ``media_root``；
    3. 写死默认值 ``DEFAULT_MEDIA_ROOT``。

    （AV_Wall 的「旧布局兜底」不适用本项目：媒体根永远是 NAS 共享目录，
    不存在「脚本上一级恰是媒体根」的场景。）

    UNC 路径下 ``resolve()`` 保持原样，不会退化；返回的字符串也保持 UNC 原始形式，
    以便与 ``library.meta.json`` 里的 ``root`` 逐字节一致（否则会触发全量重扫）。
    """
    if explicit:
        return Path(explicit).expanduser()
    cfg_root = load_config().get("media_root")
    if isinstance(cfg_root, str) and cfg_root.strip():
        return Path(cfg_root.strip())
    return Path(DEFAULT_MEDIA_ROOT)


def validate_media_root(raw: str) -> tuple[bool, str, dict[str, Any]]:
    """校验媒体根目录是否可用。

    返回 ``(是否有效, 错误信息, 详情)``。校验项：
    - 非空、是字符串；
    - 是绝对路径；
    - 目录存在、是目录、可读；
    - 至少能发现一个分区（一级子目录）——否则扫不出任何条目。

    所有失败原因**统一文案**（防目录存在性 oracle），详情只在通过时给出。
    详情里带 ``categories``（各分区下二级项个数）与 ``path``。
    """
    detail: dict[str, Any] = {}
    text = str(raw or "").strip()
    if not text:
        return False, "目录不能为空", detail
    try:
        path = Path(text)
    except (OSError, ValueError) as exc:
        return False, f"路径无效：{exc}", detail
    if not path.is_absolute():
        return False, "必须是绝对路径（UNC 如 \\\\server\\share 或盘符如 D:\\...）", detail
    if not path.exists():
        return False, _INVALID_ROOT_MSG, detail
    if not path.is_dir():
        return False, _INVALID_ROOT_MSG, detail
    if not os.access(path, os.R_OK):
        return False, _INVALID_ROOT_MSG, detail
    cats = list_categories(path)
    if not cats:
        return False, _INVALID_ROOT_MSG, detail
    detail["categories"] = {name: category_child_count(path, name) for name in cats}
    detail["path"] = text
    return True, "", detail


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------


def is_private_ip(ip: str) -> bool:
    """判断是否为常见局域网/内网地址（按 RFC1918 段精确判断）。"""
    if ip.startswith("127.") or ip.startswith(_LINK_LOCAL_PREFIX):
        return False
    if ip.startswith("192.168.") or ip.startswith("10."):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


def _udp_route_source_ip(timeout: float = 0.3) -> str | None:
    """用 UDP connect 取默认路由的源地址（不实际发包）。

    connect 到公网地址只是让内核选路，不会真的建立连接、也不发数据，
    因此无网时只是在超时后抛 OSError，不产生流量。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        if ip and not ip.startswith("127.") and not ip.startswith(_LINK_LOCAL_PREFIX):
            return ip
    except OSError:
        pass
    return None


def _usable(ip: str) -> bool:
    """地址是否可以作为「局域网 IP」候选（排除回环、链路本地、0.0.0.0）。"""
    if not ip:
        return False
    if ip.startswith("127.") or ip.startswith(_LINK_LOCAL_PREFIX):
        return False
    return ip != "0.0.0.0"


def detect_lan_ip() -> str:
    """动态获取本机**真实局域网** IP；无法判断时回退 :data:`FALLBACK_LAN_IP`。

    v2.10.2 重写。旧实现把 ``gethostbyname_ex()`` 放在第一优先级，而它会把
    **所有网卡**的地址一起返回 —— Hyper-V / WSL 的 ``vEthernet (Default Switch)``
    同样是 ``172.16/12`` 内网段，能骗过 ``is_private_ip()`` 且常排在最前，
    于是被当成局域网 IP。实测本机曾同时返回虚拟网卡与物理网卡两个地址，
    旧逻辑取到的是**排在最前的虚拟网卡**（其他设备不可达）。

    现在的优先级（越靠前越可信）：

    1. **枚举网卡**，只要「已连接 + RFC1918 内网 + 非虚拟网卡」的地址；
       其中优先带物理网卡特征（Realtek / Intel / Ethernet / 无线…）的，
       并在多个候选里优先选择与默认路由源地址一致的那个；
    2. **默认路由源地址**（UDP connect 探测，不实际发包）；
    3. ``gethostbyname_ex`` 的结果，但**剔除**已知属于虚拟网卡的地址；
    4. 最后才回退写死值 :data:`FALLBACK_LAN_IP`。

    步骤 3 保留是有意的：某些环境下 ``GetAdaptersAddresses`` 拿不到地址，
    但 DNS 解析仍能给出真实网卡地址；剔除虚拟网卡则避免重蹈旧实现覆辙。
    """
    # 1) 枚举网卡，筛出「已连接 + 内网 + 非虚拟」的候选
    adapters = list_ipv4_adapters()
    route_ip = _udp_route_source_ip()

    virtual_ips: set[str] = set()
    candidates: list[tuple[bool, str]] = []  # (是否物理网卡, IP)
    for adapter in adapters:
        if adapter.is_virtual:
            virtual_ips.add(adapter.ip)
            continue
        if not adapter.is_up or not _usable(adapter.ip):
            continue
        if not is_private_ip(adapter.ip):
            continue
        candidates.append((adapter.looks_physical, adapter.ip))

    if candidates:
        # 物理网卡优先；同等条件下优先默认路由源地址（= 真正出口网卡）
        def _rank(item: tuple[bool, str]) -> tuple[int, int]:
            physical, ip = item
            return (0 if physical else 1, 0 if ip == route_ip else 1)

        candidates.sort(key=_rank)
        return candidates[0][1]

    # 2) 默认路由源地址
    if route_ip and is_private_ip(route_ip):
        return route_ip

    # 3) DNS 解析兜底，但剔除虚拟网卡地址
    try:
        resolved = list(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        resolved = []
    real = [ip for ip in resolved if _usable(ip) and ip not in virtual_ips]
    for ip in real:
        if is_private_ip(ip):
            return ip
    for ip in real:
        return ip

    # 4) 兜底
    if route_ip:
        return route_ip
    return FALLBACK_LAN_IP


# --------------------------------------------------------------------------
# 路径安全
# --------------------------------------------------------------------------


def _is_reserved_device_name(name: str) -> bool:
    """判断一个路径片段是否是 Windows 保留设备名。

    Windows 会先对名字做规范化（**去掉尾随的空格与点**），再交给设备名解析，
    所以下面这些全部是设备名、都能被 ``open()`` 劫持成设备：

    * ``CON``  ``CON.txt``  ``CON.``  ``CON .txt``
    * ``NUL``  ``NUL ``      ``COM1``  ``LPT1``

    做法：去掉尾随空格与点 → 取第一个点之前的**基名** → 匹配设备名表。
    """
    trimmed = name.rstrip(" .")
    if not trimmed:
        return False
    # 基名 = 第一个点之前的内容；再去掉尾随空格
    # （Windows 会把 "CON .txt" 里的 "CON " 也当成设备名 CON）
    base = trimmed.split(".")[0].rstrip(" ")
    return bool(RESERVED_NAME_RE.match(base))


def decode_rel_segments(rel: str) -> list[str]:
    """把 URL 相对路径逐段 unquote 解码为安全片段列表。

    任一片段为 ``.``/``..``、解码后仍含路径分隔符、盘符、NUL，
    或命中 Windows 保留设备名，立即抛 :class:`PathSecurityError`。
    先按 ``/`` 切分再逐段解码，因此 ``%2f`` 无法伪造新目录层级。
    """
    parts = [part for part in rel.split("/") if part != ""]
    if not parts:
        raise PathSecurityError("empty relative path")
    decoded: list[str] = []
    for part in parts:
        seg = urllib.parse.unquote(part)
        if seg in ("", ".", ".."):
            raise PathSecurityError(f"非法路径片段: {seg!r}")
        if "/" in seg or "\\" in seg or ":" in seg or "\x00" in seg:
            raise PathSecurityError(f"解码后仍含分隔符: {seg!r}")
        if _is_reserved_device_name(seg):
            raise PathSecurityError(f"保留设备名: {seg!r}")
        decoded.append(seg)
    return decoded


def resolve_within(root: Path, rel: str) -> Path:
    """解析 ``root`` 内相对路径，越界抛 :class:`PathSecurityError`。

    双保险：先做片段级黑名单校验，再用 ``realpath`` + ``commonpath``
    做最终归一化校验（可抵御已存在的符号链接/联接）。
    """
    segments = decode_rel_segments(rel)
    candidate = root.joinpath(*segments)
    real_root = Path(os.path.realpath(root))
    real_target = Path(os.path.realpath(candidate))
    try:
        common = os.path.commonpath([str(real_target), str(real_root)])
    except ValueError:
        raise PathSecurityError("路径不可比较（跨卷或相对/绝对混用）") from None
    if common != str(real_root):
        raise PathSecurityError(f"越界路径: {real_target}")
    return real_target


# --------------------------------------------------------------------------
# Range
# --------------------------------------------------------------------------


def parse_range_header(header: str | None, size: int) -> tuple[int, int] | None:
    """解析单个 ``Range`` 头，返回闭区间 ``(start, end)``。

    * ``bytes=s-e`` / ``bytes=s-`` / ``bytes=-n``（后缀形式）
    * ``end`` 越界 clamp 到 ``size-1``
    * 格式非法或范围为空返回 ``None``
      （调用方按「带了 Range 却解析失败」处理，返回 416）
    * 起始偏移越界抛 :class:`RangeNotSatisfiable`（调用方返回 416）
    """
    if not header:
        return None
    text = header.strip()
    if text.lower().startswith("bytes="):
        text = text[6:]
    if "," in text:            # 多段 Range 不支持
        return None
    match = RANGE_RE.match(text)
    if not match:
        return None
    start_raw, end_raw = match.group(1), match.group(2)
    if start_raw == "" and end_raw == "":
        return None
    if start_raw == "":        # 后缀范围 bytes=-n
        try:
            suffix = int(end_raw)
        except ValueError:
            return None
        if suffix <= 0:
            return None
        if size <= 0:
            raise RangeNotSatisfiable(size)
        start = max(0, size - suffix)
        end = size - 1
    else:
        try:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
        except ValueError:
            return None
        if end >= size:
            end = size - 1
        if start >= size:
            # 起始偏移越界：规范要求 416，绝不能退化成 200 全量
            # （否则会向手机推 3.6GB 全量流，表现为「卡死」）
            raise RangeNotSatisfiable(size)
        if start < 0 or start > end:
            return None
    if start < 0 or start > end:
        return None
    return start, end


# ---------------------------------------------------------------------------
# Windows 长路径（MAX_PATH = 260）
# ---------------------------------------------------------------------------

#: 路径长度达到这个值就认为「可能超长」，失败时再用长路径前缀重试。
#: 留 20 字符余量覆盖边界情况（不同 Windows API 的 260 口径略有差异）。
LONG_PATH_HINT = 240

#: UNC 长路径前缀：\\?\UNC\  ——不能写成 \\?\server\share（那样会被当成相对名）
_PFX = "\\\\?\\"
_PFX_UNC = "\\\\?\\UNC\\"


def long_path(path):
    r"""返回 Windows 长路径形式（\\?\ 或 \\?\UNC\ 前缀）。

    超过 260 字符的路径不加前缀时 open/stat 都会报「系统找不到指定的路径」
    （FileNotFoundError），很容易被误判成文件不存在——实测 NAS 上
    「权力的游戏」那批剧集路径 268 字符，扫描收录了但播放 404 就是它。
    """
    p = os.fspath(path)
    if os.name != "nt" or not p or p.startswith(_PFX):
        return p
    try:
        p = os.path.abspath(p)
    except (OSError, ValueError):
        return p
    if p.startswith("\\\\"):
        return _PFX_UNC + p[2:]
    return _PFX + p


def stat_path(path):
    """os.stat 的长路径版本：短路径行为完全不变，疑似超长时才走前缀重试。"""
    raw = os.fspath(path)
    try:
        return os.stat(raw)
    except OSError:
        if len(raw) < LONG_PATH_HINT:
            raise
        return os.stat(long_path(raw))


def open_path(path, mode="rb"):
    """open 的长路径版本（先普通、失败且疑似超长再加前缀重试）。"""
    raw = os.fspath(path)
    try:
        return open(raw, mode)
    except OSError:
        if len(raw) < LONG_PATH_HINT:
            raise
        return open(long_path(raw), mode)
