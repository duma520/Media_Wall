"""网卡枚举与虚拟网卡识别（纯标准库，Nuitka 编译态可用）。

为什么需要这个模块
------------------
v2.10.2 修复「编译后局域网地址显示成虚拟网卡地址」的缺陷。

根因：``gethostbyname_ex(hostname)`` 会把**所有**网卡地址一起返回，
Hyper-V / WSL 的 ``vEthernet (Default Switch)`` 也是 ``172.16.0.0/12``
内网段，能通过 ``is_private_ip()`` 检查，且常常排在最前面 ——
于是被当成了「局域网 IP」。但虚拟网卡的地址对其他设备不可达，
用它生成的 URL 和二维码必然扫不开。

本模块用 Windows 的 ``GetAdaptersAddresses`` 拿到每个地址**所属网卡的
友好名称**，从而能把 ``vEthernet`` / ``VMware`` / ``TAP`` 这类虚拟网卡
排除，只挑真实物理网卡（或真实出口网卡）的地址。

对外只暴露两件事：
* :func:`list_ipv4_adapters`  —— 列出所有 IPv4 地址及其网卡信息；
* :func:`is_virtual_adapter_name` —— 判断网卡名是否属于虚拟/隧道类。

非 Windows 平台时 :func:`list_ipv4_adapters` 返回空列表，调用方自行兜底。
"""

from __future__ import annotations

import ctypes
import socket
import sys
from dataclasses import dataclass

#: 网卡名里出现这些片段即视为虚拟/隧道网卡（不参与「局域网 IP」评选）。
#: 全部小写，比较时统一 lower()。
_VIRTUAL_HINTS = (
    "vethernet",          # Hyper-V / WSL2 默认交换机（本项目踩的坑）
    "hyper-v",
    "vmware",
    "virtualbox",
    "vbox",
    "tap-",               # OpenVPN TAP
    "tap0",
    "wsl",
    "docker",
    "npcap",
    "loopback",
    "bluetooth",
    "wi-fi direct",       # Windows 的 Wi-Fi Direct 虚拟网卡
    "microsoft wi-fi direct",
    "zerotier",
    "tailscale",
    "wireguard",
    "hamachi",
    "openvpn",
    "vpn",
    "tunnel",
)

#: 明确**不是**虚拟的常见物理网卡名片段（用于同分时优先）。
_PHYSICAL_HINTS = (
    "realtek",
    "intel",
    "ethernet",
    "以太网",
    "无线",
    "wi-fi",
    "wlan",
    "gbe",
    "gigabit",
    "killer",
    "broadcom",
    "marvell",
    "atheros",
)

_AF_INET = 2
_GAA_FLAG_SKIP_ANYCAST = 0x2
_GAA_FLAG_SKIP_MULTICAST = 0x4
_GAA_FLAG_SKIP_DNS_SERVER = 0x8
_ERROR_BUFFER_OVERFLOW = 111
_IF_OPER_STATUS_UP = 1


@dataclass(frozen=True)
class AdapterAddress:
    """一个 IPv4 地址及其所属网卡的信息。"""

    ip: str
    friendly_name: str      # 网卡友好名，如 "vEthernet (Default Switch)"
    description: str        # 驱动描述
    is_up: bool             # 网卡是否处于「已连接」状态

    @property
    def is_virtual(self) -> bool:
        return is_virtual_adapter_name(self.friendly_name) or is_virtual_adapter_name(
            self.description
        )

    @property
    def looks_physical(self) -> bool:
        blob = f"{self.friendly_name} {self.description}".lower()
        return any(h in blob for h in _PHYSICAL_HINTS)


def is_virtual_adapter_name(name: str) -> bool:
    """网卡名/描述是否属于虚拟、隧道或回环类网卡。"""
    if not name:
        return False
    low = name.lower()
    return any(hint in low for hint in _VIRTUAL_HINTS)


# --------------------------------------------------------------------------
# ctypes 结构（GetAdaptersAddresses）
# --------------------------------------------------------------------------


class _SOCKADDR(ctypes.Structure):
    _fields_ = [
        ("sa_family", ctypes.c_ushort),
        ("sa_data", ctypes.c_ubyte * 14),
    ]


class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("lpSockaddr", ctypes.POINTER(_SOCKADDR)),
        ("iSockaddrLength", ctypes.c_int),
    ]


class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", ctypes.c_ulong),
    ("PreferredLifetime", ctypes.c_ulong),
    ("LeaseLifetime", ctypes.c_ulong),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
    # union { ULONGLONG Alignment; struct { ULONG Length; IF_INDEX IfIndex; }; }
    ("Length", ctypes.c_ulong),
    ("IfIndex", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Mtu", ctypes.c_ulong),
    ("IfType", ctypes.c_ulong),
    ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", ctypes.c_ulong),
    ("ZoneIndices", ctypes.c_ulong * 16),
    ("FirstPrefix", ctypes.c_void_p),
]


def _sockaddr_to_ipv4(ptr: ctypes.POINTER(_SOCKADDR)) -> str | None:
    """把 sockaddr 指针对应的 IPv4 地址转成点分字符串；非 AF_INET 返回 None。

    ``sockaddr_in`` 布局：family(2) + port(2) + addr(4)，
    所以 IPv4 四字节在偏移 4..8。
    """
    if not ptr:
        return None
    raw = ctypes.string_at(ptr, 16)
    if len(raw) < 8:
        return None
    family = int.from_bytes(raw[0:2], "little")
    if family != _AF_INET:
        return None
    try:
        return socket.inet_ntoa(raw[4:8])
    except OSError:
        return None


def list_ipv4_adapters() -> list[AdapterAddress]:
    """列出本机所有 IPv4 地址及其网卡信息（仅 Windows；其他平台返回空）。"""
    if not sys.platform.startswith("win"):
        return []
    try:
        return _list_ipv4_adapters_windows()
    except Exception:  # noqa: BLE001 - 枚举失败不该拖垮主流程，调用方会兜底
        return []


def _list_ipv4_adapters_windows() -> list[AdapterAddress]:
    get_adapters = ctypes.windll.iphlpapi.GetAdaptersAddresses
    get_adapters.argtypes = [
        ctypes.c_ulong,      # Family
        ctypes.c_ulong,      # Flags
        ctypes.c_void_p,     # Reserved
        ctypes.c_void_p,     # AdapterAddresses
        ctypes.POINTER(ctypes.c_ulong),  # SizePointer
    ]
    get_adapters.restype = ctypes.c_ulong

    flags = _GAA_FLAG_SKIP_ANYCAST | _GAA_FLAG_SKIP_MULTICAST | _GAA_FLAG_SKIP_DNS_SERVER

    size = ctypes.c_ulong(15000)
    for _ in range(4):  # 网卡多时缓冲区会不够，按返回值重试几次
        buf = ctypes.create_string_buffer(size.value)
        ret = get_adapters(_AF_INET, flags, None, buf, ctypes.byref(size))
        if ret == 0:
            break
        if ret != _ERROR_BUFFER_OVERFLOW:
            return []
    else:
        return []

    result: list[AdapterAddress] = []
    node = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
    while node:
        item = node.contents
        friendly = item.FriendlyName or ""
        desc = item.Description or ""
        is_up = item.OperStatus == _IF_OPER_STATUS_UP

        unicast = item.FirstUnicastAddress
        while unicast:
            ip = _sockaddr_to_ipv4(unicast.contents.Address.lpSockaddr)
            if ip:
                result.append(
                    AdapterAddress(
                        ip=ip,
                        friendly_name=friendly,
                        description=desc,
                        is_up=is_up,
                    )
                )
            unicast = unicast.contents.Next

        node = item.Next
    return result


__all__ = [
    "AdapterAddress",
    "is_virtual_adapter_name",
    "list_ipv4_adapters",
]
