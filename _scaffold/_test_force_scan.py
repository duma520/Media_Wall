"""v2.4.0 全量重建（--force）链路验证：真实起服务 + POST /api/scan {"force":true}。

验证三层：
1. ScanManager.run_async(force=True) → _scan_once 拼出带 --force 的命令；
2. HTTP POST /api/scan {"force":true} 返回 202 且 force 字段为 true；
3. scanner.py 收到 --force 后真的全量重建（输出「全量重建」而非「增量扫描」）。

全程在临时副本里跑，不碰项目 data/ 与 NAS。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8795

fails: list[str] = []

def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


def _free_port() -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", PORT)) != 0


def main() -> int:
    # 1) ScanManager 命令构造（不需要真跑扫描，只验证拼参）
    sys.path.insert(0, str(PROG))
    from mediawall import scan as scan_mod

    # 直接调用 _scan_once 路径会真跑 scanner；改为验证 run_async 挂起的 force
    # 会在 _scan_once 里被取用 —— 用 monkeypatch subprocess.run 捕获命令。
    import subprocess as _sp

    captured: list[list[str]] = []

    orig_run = _sp.run

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        # 返回成功空输出
        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""
        return FakeProc()

    _sp.run = fake_run
    try:
        mgr = scan_mod.ScanManager(PROG / "scanner.py", Path("D:/fake_root"))
        mgr._running = False  # 直接调用内部路径需要解锁
        mgr._scan_lock = type("L", (), {"acquire": lambda s, timeout=0: True,
                                        "release": lambda s: None})()
        ok, _out = mgr.run_sync(force=True)
        check("run_sync(force=True) 成功", ok)
        check("扫描命令含 --force", bool(captured) and "--force" in captured[0],
              str(captured[0] if captured else None))
        check("扫描命令含 --root", bool(captured) and "--root" in captured[0],
              str(captured[0] if captured else None))
    finally:
        _sp.run = orig_run

    # 2) 真实起服务 + POST /api/scan {"force":true}（不真扫完整目录，用空媒体根）
    if not _free_port():
        print(f"端口 {PORT} 被占用，跳过 HTTP 验证")
        return 1 if fails else 0
    tmp = Path(tempfile.mkdtemp(prefix="mw_force_"))
    try:
        app_dir = tmp / "prog"
        app_dir.mkdir()
        for name in ("server.py", "scanner.py", "index.html", "config.json"):
            src = PROG / name
            if src.exists():
                shutil.copy2(src, app_dir / name)
        shutil.copytree(PROG / "mediawall", app_dir / "mediawall",
                        ignore=shutil.ignore_patterns("__pycache__"))
        media = tmp / "media"
        (media / "01 电影" / "测试片").mkdir(parents=True)
        (media / "01 电影" / "测试片" / "a.mp4").write_bytes(b"\x00" * 1024)

        proc = subprocess.Popen(
            [PY, "server.py", "--root", str(media), "--port", str(PORT), "--no-browser"],
            cwd=str(app_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        for _ in range(60):
            if not _free_port():
                break
            time.sleep(0.25)
        else:
            print("服务没起来")
            return 3
        try:
            # 等待首次扫描完成（空媒体根，很快）
            for _ in range(40):
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{PORT}/api/status", timeout=3) as r:
                        j = json.loads(r.read().decode("utf-8"))
                    if not (j.get("scan") or {}).get("running"):
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            # POST /api/scan {"force":true}
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/scan",
                data=json.dumps({"force": True}).encode("utf-8"),
                method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                resp = json.loads(r.read().decode("utf-8"))
            check("POST /api/scan force 返回 202/ok", r.status == 202 and resp.get("ok"),
                  f"status={r.status} resp={resp}")
            check("响应带 force=true", resp.get("force") is True, str(resp.get("force")))

            # 等扫描结束，确认真的跑了（quiet 模式下没有「全量重建」字样，
            # 因为 scanner 只在非 quiet 时打印模式；force 的行为差异见下方直接调用验证）
            for _ in range(60):
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{PORT}/api/status", timeout=3) as r:
                        j = json.loads(r.read().decode("utf-8"))
                    sc = j.get("scan") or {}
                    if not sc.get("running"):
                        tail = str(sc.get("output_tail") or "")
                        check("扫描确实完成（output_tail 非空）", bool(tail), tail[:60])
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            else:
                check("扫描结束", False, "60 次轮询未结束")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        # 3) 直接跑 scanner.py（不带 --quiet）验证 --force 输出「全量重建」
        out = subprocess.run(
            [PY, str(PROG / "scanner.py"), "--root", str(media), "--force"],
            cwd=str(app_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        merged = (out.stdout or "") + (out.stderr or "")
        check("scanner --force 打印「全量重建」", "全量重建" in merged, merged[:80])
        check("scanner --force 退出码 0", out.returncode == 0, str(out.returncode))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果: PASS {8 - len(fails)} / FAIL {len(fails)}")
    if fails:
        print("失败项:", "；".join(fails))
        return 1
    print("FORCE_SCAN_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
