"""端到端测试（全程临时目录，不碰真实 NAS、不覆盖真实 data/）。

覆盖：
1) server.py --root 临时媒体根 → /api/status 的 version/root；
2) 扫描完成后 scan.progress（done/total/category）与 library.json 分区；
3) 模拟扫描进行中的进度文件 → /api/status 实时暴露（GUI 进度条数据源）；
4) ServiceManager 启停 + 状态信号；
5) GUI 进度换算 _emit_scan_progress 的百分比映射。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent  # 脚本位于 _scaffold/，项目根是上一级
PY = sys.executable


def fetch_status(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as r:
        return json.loads(r.read())


def wait_http(port: int, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return fetch_status(port)
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"服务未在 {timeout}s 内应答 (port={port})")


def post_json(port: int, path: str, body: dict | None = None) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body or {}).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mw_e2e_"))
    print("temp:", tmp)

    # ---- 造临时程序副本 + 临时媒体树 ----
    shutil.copy2(PROG / "server.py", tmp / "server.py")
    shutil.copy2(PROG / "scanner.py", tmp / "scanner.py")
    shutil.copy2(PROG / "gui_service.py", tmp / "gui_service.py")
    shutil.copytree(PROG / "mediawall", tmp / "mediawall",
                    ignore=shutil.ignore_patterns("__pycache__"))

    media = tmp / "media"
    cases = [
        ("01 电影", "复仇者联盟", "复仇者联盟.jpg"),
        ("01 电影", "星际穿越", "星际穿越.mkv"),
        ("20 综艺", "歌手", "歌手.mp4"),
        ("20 综艺", "歌手", "歌手海报.jpg"),
    ]
    for cat, book, fname in cases:
        d = media / cat / book
        d.mkdir(parents=True, exist_ok=True)
        (d / fname).write_bytes(b"x")

    (tmp / "config.json").write_text(
        json.dumps({"media_root": str(media), "categories": None},
                   ensure_ascii=False), encoding="utf-8")

    # ---- 阶段 1：直接拉起 server.py ----
    port1 = 8899
    proc = subprocess.Popen(
        [PY, str(tmp / "server.py"), "--host", "127.0.0.1",
         "--port", str(port1), "--root", str(media), "--no-browser"],
        cwd=str(tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        st = wait_http(port1)
        print("1) version:", st["version"], "| root ok:", st["root"] == str(media))
        sys.path.insert(0, str(PROG))
        from mediawall import VERSION as EXPECT_VER
        assert st["version"] == EXPECT_VER, (st["version"], EXPECT_VER)

        # 等自动扫描结束
        deadline = time.time() + 60
        while time.time() < deadline:
            st = wait_http(port1)
            scan = st["scan"]
            if scan.get("ok") is True:
                break
            if scan.get("ok") is False:
                raise SystemExit(f"扫描失败: {scan.get('error')}")
            time.sleep(0.15)
        prog = st["scan"].get("progress") or {}
        print("2) 扫描完成 progress:", prog)
        assert prog.get("done") == prog.get("total") == 2, prog
        assert prog.get("category"), prog

        lib = json.loads((tmp / "data" / "library.json").read_text(encoding="utf-8"))
        cats = {c["name"]: c["items"] for c in lib.get("categories", [])}
        print("3) library 分区(名→条目数):", cats, "| 书籍:", len(lib.get("books", [])))
        assert cats.get("01 电影") == 2 and cats.get("20 综艺") == 1, cats

        # ---- 模拟「扫描进行中」：进度文件被 scanner 写到一半 ----
        progress_file = tmp / "data" / "scan_progress.json"
        backup = progress_file.read_bytes()
        progress_file.write_text(json.dumps(
            {"done": 1, "total": 4, "category": "01 电影", "ts": "x"},
            ensure_ascii=False), encoding="utf-8")
        live = wait_http(port1)["scan"]["progress"]
        print("4) 实时进度暴露:", live)
        assert live.get("done") == 1 and live.get("total") == 4, live
        progress_file.write_bytes(backup)
    finally:
        try:
            post_json(port1, "/api/shutdown")
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ---- 阶段 2：ServiceManager 启停 ----
    sys.path.insert(0, str(tmp))
    # v1.2.4：阶段 1 已从真实 PROG 导入过 mediawall，进程内模块缓存会让
    # mediawall.paths.PROGRAM_DIR 仍指向真实项目根（而非临时副本），
    # 于是 ServiceManager 起的服务去读真实 config.json、root 校验失败。
    # 清掉缓存后阶段 2 才真正隔离在临时副本里（断言也补上了 root 校验）。
    for _m in [m for m in sys.modules if m == "mediawall" or m.startswith("mediawall.")]:
        del sys.modules[_m]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import gui_service  # noqa: E402 - 从临时副本导入

    from PySide6.QtWidgets import QApplication  # noqa: E402

    app = QApplication([])
    states: list[str] = []
    sm = gui_service.ServiceManager(port=8898, host="127.0.0.1")
    sm.state_changed.connect(states.append)

    ok = sm.start()
    st = wait_http(8898)
    print("5) ServiceManager.start:", ok, "| running:", sm.running,
          "| root ok:", st["root"] == str(media))
    sm.stop()
    print("   states:", states)
    assert ok and "running" in states and "stopped" in states, states
    # 这条以前只打印不校验，root 出错也会显示 E2E_OK（掩盖过一次误命中）
    assert st["root"] == str(media), (st["root"], str(media))

    # ---- 阶段 3：GUI 进度换算 ----
    seen: list[tuple[int, str]] = []
    sm.scan_progress.connect(lambda pct, text: seen.append((pct, text)))
    sm._emit_scan_progress({"scan": {"running": True, "progress": {
        "done": 5, "total": 16, "category": "01 电影"}}})
    sm._emit_scan_progress({"scan": {"running": True, "progress": {}}})
    sm._emit_scan_progress({"scan": {"ok": True}})
    sm._emit_scan_progress({"scan": {"ok": False, "error": "boom"}})
    print("6) 进度映射:", seen)
    assert seen[0][0] == 31 and "5/16" in seen[0][1], seen
    assert seen[1][0] == -1, seen
    assert seen[2][0] == 100, seen
    assert seen[3][0] == -1 and "boom" in seen[3][1], seen

    shutil.rmtree(tmp, ignore_errors=True)
    print("E2E_OK")


if __name__ == "__main__":
    main()
