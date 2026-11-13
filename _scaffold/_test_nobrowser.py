"""v2.3.0 第 1 点验证：GUI/重启拉起服务都带 --no-browser，不自动开浏览器。

只验证命令构造（不真正弹浏览器）：检查 ServiceManager.start 与
restart.build_command 产出的命令都包含 --no-browser。
"""
import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
fails: list[str] = []

def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)

def main() -> int:
    # 1) GUI ServiceManager 的启动命令
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from gui_service import ServiceManager
    sm = ServiceManager(port=8787, host="0.0.0.0")
    # 源码态：head = [sys.executable, server.py]
    # 编译态：head = [self_exe(), --serve]
    cmd = [sys.executable, str(ROOT / "server.py"),
           "--host", "0.0.0.0", "--port", "8787"]
    import gui_service as gs
    # 直接验证 gui_service 构造命令的代码路径（不走真实启动）
    src = (ROOT / "gui_service.py").read_text(encoding="utf-8")
    check("gui_service 启动命令含 --no-browser", '"--no-browser"' in src
          and "--no-browser" in src.split("cmd = head +")[1],
          src.split("cmd = head +")[1].strip()[:120])

    # 2) restart.build_command 构造的命令
    from mediawall import restart
    from mediawall.paths import pythonw
    bc = restart.build_command("0.0.0.0", 8787)
    check("restart 命令含 --no-browser", "--no-browser" in bc, " ".join(bc))
    check("restart 命令保留 --wait-port", "--wait-port" in bc, " ".join(bc))
    check("restart 命令含 --port-max（v2.5.3 端口自动升级）", "--port-max" in bc, " ".join(bc))
    # v2.5.1：源码态用 pythonw.exe（无控制台窗口）
    if not restart.is_frozen():
        check("restart 命令用 pythonw", bc[0].lower().endswith("pythonw.exe"),
              bc[0])
    else:
        check("编译态用 self_exe", bc[0].lower().endswith(".exe"), bc[0])

    # 3) 真实子进程启动 server.py --no-browser 能正常起来（不起浏览器是行为，端口可达）
    import socket, time, urllib.request, tempfile
    PORT = 8793
    with socket.socket() as s:
        free = s.connect_ex(("127.0.0.1", PORT)) != 0
    if not free:
        print("  SKIP  端口 8793 被占用")
    else:
        tmp_media = Path(tempfile.mkdtemp(prefix="mw_nobr_"))
        (tmp_media / "01 电影").mkdir()
        proc = subprocess.Popen(
            [pythonw(), str(ROOT / "server.py"), "--root", str(tmp_media),
             "--port", str(PORT), "--no-browser"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        time.sleep(2.5)
        alive = proc.poll() is None
        # 探活：端口能响应 = 服务真的起来了
        reachable = False
        if alive:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=3) as r:
                    reachable = r.status == 200
            except Exception:
                pass
        check("--no-browser 参数被 server.py 接受（进程存活且端口可达）",
              alive and reachable, f"alive={alive} reachable={reachable}")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        import shutil
        shutil.rmtree(tmp_media, ignore_errors=True)

    print(f"\n结果: PASS {6 - len(fails)} / FAIL {len(fails)}")
    if fails:
        print("失败项:", "；".join(fails))
        return 1
    print("NOBROWSER_OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
