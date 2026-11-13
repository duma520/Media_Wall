"""局域网访问防火墙自检与一键修复（Windows）。

为什么需要这个模块
------------------
v2.10.2 修复「编译后的 exe 局域网其他设备访问不了」。

根因：Windows 在程序首次监听端口时弹「防火墙」询问窗口，如果用户点了
取消（或点了「允许访问」但后来在设置里被改成阻止），Windows 会为这个
**exe 路径**生成一条 *Block* 入站规则。Windows 防火墙里 Block 优先级
**高于** Allow，所以即使程序正确绑定 ``0.0.0.0``，外部也连不进来。

实测本机就存在两条：
``media_wall | Inbound | Block | ...\\Media_Wall\\Media_Wall.exe``
源码态能访问是因为 ``python.exe`` 自己有 Allow 规则 —— 这解释了
「源码跑得通、编译后连不上」的现象。

为什么走 COM 而不是 ``Get-NetFirewallRule``
-------------------------------------------
``Get-NetFirewallRule`` 走 WMI/CIM，本机实测**单次查询 9~25 秒**
（1222 条规则逐条取 ApplicationFilter），检测一慢界面就一直卡在
「检测中」。改用 ``HNetCfg.FwPolicy2`` COM 接口后实测 **1.0~1.6 秒**，
快一个数量级，且同样只需要读权限。

设计原则
--------
* **只读检测**（:func:`check_lan_access`）不触发任何权限提示，可随时调用；
* **修复**（:func:`fix_lan_access`）必须由用户点击按钮触发，用
  ``ShellExecuteW(..., "runas", ...)`` 起一次 UAC 提升的 PowerShell，
  修完自校验并回读结果；
* 幂等：重复执行不会堆出重复规则（先删同名旧规则再建）；
* 不改动与本程序无关的规则，只处理 ``ApplicationName`` 命中当前 exe 的条目。
"""

from __future__ import annotations

import base64
import ctypes
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .paths import self_exe

#: 我们创建的入站规则统一用这个名字前缀，便于识别与清理。
RULE_NAME = "Media_Wall LAN"

_CREATE_NO_WINDOW = 0x08000000

#: COM 常量：HNetCfg.FwPolicy2
_NET_FW_RULE_DIR_IN = 1


@dataclass
class LanAccessReport:
    """局域网可达性检测结果。"""

    program: str = ""
    blocked_rules: list[dict] = field(default_factory=list)
    allow_rules: list[dict] = field(default_factory=list)
    supported: bool = True
    error: str = ""

    @property
    def needs_fix(self) -> bool:
        """是否必须修复才能被局域网访问。

        有 Block 规则 → 一定需要修；
        完全没有 Allow 规则也不一定坏（Windows 可能整体放行），
        所以只在「有 Block」时判定需要修复。
        """
        return bool(self.blocked_rules)

    @property
    def has_allow(self) -> bool:
        return bool(self.allow_rules)


def _decode(raw: bytes) -> str:
    """PowerShell 输出解码：优先 UTF-8，其次系统本地代码页。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc, errors="replace")
        except (LookupError, AttributeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_json(text: str) -> object | None:
    """从 PowerShell 输出里取出第一段 JSON（容忍其它横幅/警告行）。"""
    text = text.strip()
    if not text:
        return None
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            chunk = text[start:end + 1]
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    return None


def _run_powershell(script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """以无窗口方式跑 PowerShell（5.1）并返回结果。"""
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
    )


def _ps_single_quote(value: str) -> str:
    """把字符串安全转成 PowerShell 单引号字面量（内部单引号翻倍）。"""
    return "'" + value.replace("'", "''") + "'"


#: 用 COM 枚举「ApplicationName 命中本程序」的入站规则（检测与修复共用）。
_COM_ENUM_TEMPLATE = """
$ErrorActionPreference = 'SilentlyContinue'
$prog = {prog}
$fw = New-Object -ComObject HNetCfg.FwPolicy2
$out = @()
foreach ($r in $fw.Rules) {{
    if ($r.Direction -ne 1) {{ continue }}
    if (-not $r.ApplicationName) {{ continue }}
    if ($r.ApplicationName.ToLower() -ne $prog.ToLower()) {{ continue }}
    $act = switch ([int]$r.Action) {{ 0 {{ 'Block' }} 1 {{ 'Allow' }} default {{ 'Unknown' }} }}
    $out += [pscustomobject]@{{
        Name = [string]$r.Name
        DisplayName = [string]$r.Name
        Action = $act
        Enabled = [bool]$r.Enabled
        Profile = [string]$r.Profile
    }}
}}
$out | ConvertTo-Json -Compress
"""


def _collect_rules(program: str) -> tuple[list[dict], list[dict]]:
    """查询入站规则中 ApplicationName 命中 ``program`` 的条目。

    返回 ``(Block 列表, Allow 列表)``；走 COM 接口（实测 1~1.6 秒）。
    """
    script = _COM_ENUM_TEMPLATE.format(prog=_ps_single_quote(program))
    result = _run_powershell(script)
    if result.returncode != 0:
        raise RuntimeError(_decode(result.stderr).strip() or "PowerShell 查询失败")

    data = _extract_json(_decode(result.stdout))
    if data is None:
        return [], []
    if isinstance(data, dict):
        data = [data]

    blocked: list[dict] = []
    allow: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        action = str(item.get("Action") or "").lower()
        if action == "block":
            blocked.append(item)
        elif action == "allow":
            allow.append(item)
    return blocked, allow


def check_lan_access() -> LanAccessReport:
    """检测当前程序是否被防火墙拦住（只读，不需要管理员权限）。

    源码态（``python.exe``）通常已有 Allow 规则，返回的 Block 为空即正常；
    编译态若出现 Block 规则，即为「其他设备访问不了」的直接原因。
    """
    report = LanAccessReport()
    if not sys.platform.startswith("win"):
        report.supported = False
        report.error = "非 Windows 平台，无需处理"
        return report

    prog = self_exe()
    report.program = prog
    try:
        blocked, allow = _collect_rules(prog)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        report.error = str(exc)
        return report

    report.blocked_rules = blocked
    report.allow_rules = allow
    return report


def _fix_script(program: str) -> str:
    """生成提权执行的修复脚本（COM 删 Block → 建 Allow → 自校验）。"""
    prog = _ps_single_quote(program)
    rule = _ps_single_quote(RULE_NAME)
    rule_udp = _ps_single_quote(f"{RULE_NAME} UDP")
    return f"""
$ErrorActionPreference = 'Continue'
$prog = {prog}

# 1) COM 枚举本程序的入站 Block 规则并删除（幂等：重复跑不会堆积）
$fw = New-Object -ComObject HNetCfg.FwPolicy2
$toDelete = @()
foreach ($r in $fw.Rules) {{
    if ($r.Direction -ne 1) {{ continue }}
    if (-not $r.ApplicationName) {{ continue }}
    if ($r.ApplicationName.ToLower() -ne $prog.ToLower()) {{ continue }}
    if ([int]$r.Action -ne 0) {{ continue }}
    $toDelete += [string]$r.Name
}}
foreach ($name in $toDelete) {{ $fw.Rules.Remove($name) }}

# 2) 先删我们上次建的规则，再重建（避免重复条目）
foreach ($n in @({rule}, {rule_udp})) {{
    try {{ $fw.Rules.Remove($n) }} catch {{ }}
}}

# 3) 建立 TCP + UDP 入站放行（Private + Public，换网络也能用）
New-NetFirewallRule -Name {rule} -DisplayName 'Media_Wall 局域网访问' `
    -Direction Inbound -Action Allow -Protocol TCP -Program $prog `
    -Profile Private,Public -ErrorAction Stop | Out-Null
New-NetFirewallRule -Name {rule_udp} -DisplayName 'Media_Wall 局域网访问 (UDP)' `
    -Direction Inbound -Action Allow -Protocol UDP -Program $prog `
    -Profile Private,Public -ErrorAction Stop | Out-Null

# 4) 自校验：回读本程序的入站规则
$after = @()
foreach ($r in $fw.Rules) {{
    if ($r.Direction -ne 1) {{ continue }}
    if (-not $r.ApplicationName) {{ continue }}
    if ($r.ApplicationName.ToLower() -ne $prog.ToLower()) {{ continue }}
    $after += [pscustomobject]@{{ Name=[string]$r.Name; Action=([int]$r.Action) }}
}}
$after | ConvertTo-Json -Compress
"""


def fix_lan_access() -> tuple[bool, str]:
    """提权修复局域网访问（会弹一次 UAC）。返回 (是否成功, 说明文本)。

    流程：拼修复脚本 → ``ShellExecuteW runas`` 提权执行 → 轮询复核规则状态。
    """
    if not sys.platform.startswith("win"):
        return False, "非 Windows 平台，无需处理"

    prog = self_exe()
    if not prog:
        return False, "无法确定当前程序路径"

    script = _fix_script(prog)
    # 用 -EncodedCommand 传递，避免命令行长度限制与引号转义问题
    b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")

    try:
        # SW_HIDE = 0，避免黑框一闪；runas 触发 UAC
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "powershell",
            f"-NoProfile -ExecutionPolicy Bypass -EncodedCommand {b64}",
            None, 0,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"调用提权失败：{exc}"

    # ShellExecuteW 返回值 > 32 表示成功启动
    if int(ret) <= 32:
        if int(ret) == 5:
            return False, "已取消管理员授权（UAC 被拒绝），防火墙规则未修改"
        return False, f"提权启动失败（代码 {ret}）"

    # ShellExecute 是异步的：轮询复核最多 25 秒
    deadline = time.time() + 25
    last_report: LanAccessReport | None = None
    while time.time() < deadline:
        time.sleep(1.0)
        report = check_lan_access()
        last_report = report
        if report.supported and not report.blocked_rules and report.has_allow:
            return True, "已放行局域网访问（入站 TCP/UDP 允许，Block 规则已清除）"

    if last_report is None:
        return False, "修复后未能复核（查询超时），请手动检查防火墙"
    if last_report.blocked_rules:
        return False, "仍有 Block 规则存在，请手动在防火墙中删除"
    if not last_report.has_allow:
        return False, "未检测到放行规则，请手动添加"
    return False, "修复结果未知，请手动检查防火墙"


def describe_report(report: LanAccessReport) -> str:
    """把检测结果转成一句中文说明，供 GUI 状态栏显示。"""
    if not report.supported:
        return "当前平台不需要防火墙配置"
    if report.error:
        return f"检测失败：{report.error}"
    if report.blocked_rules:
        return "检测到阻止规则，局域网设备无法访问 —— 点右侧按钮修复"
    if report.has_allow:
        return "防火墙已放行局域网访问"
    return "未检测到阻止规则（若仍无法访问，请点右侧按钮添加放行规则）"


__all__ = [
    "LanAccessReport",
    "RULE_NAME",
    "check_lan_access",
    "describe_report",
    "fix_lan_access",
]
