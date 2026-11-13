"""全域播放进度（v2.8.0）验收：真实起服务 + 真实 HTTP 请求。

覆盖：GET 空表、POST 上报、max 合并（只增不减）、非法值拒绝、坏 body、
跨站 Origin 拒绝（403）、clear、重启后仍在（持久化）。

全程在临时副本里跑，不碰项目 data/ 与 NAS。
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8792


def _port_free() -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", PORT)) != 0


def _req(url: str, data: dict | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, dict]:
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}


def main() -> int:
    if not _port_free():
        print(f"端口 {PORT} 被占用，先停掉再跑")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="mw_sync_"))
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            fails.append(name)

    def start(app_dir: Path) -> subprocess.Popen:
        proc = subprocess.Popen(
            [PY, "server.py", "--root", str(media), "--port", str(PORT),
             "--no-browser"],
            cwd=str(app_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        for _ in range(80):
            if not _port_free():
                return proc
            time.sleep(0.25)
        return proc

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
        film = media / "01 电影"
        (film / "片子A").mkdir(parents=True)
        (film / "片子A" / "a.mp4").write_bytes(b"\x00" * 1024)

        base = f"http://127.0.0.1:{PORT}"
        proc = start(app_dir)
        try:
            # 1) 初始为空
            st, js = _req(f"{base}/api/sync/progress")
            check("GET 返回 200+ok", st == 200 and js.get("ok"), f"status={st}")
            check("初始 map 为空", js.get("map") == {}, str(js.get("map")))

            # 2) 上报
            st, js = _req(f"{base}/api/sync/progress",
                          {"map": {"01电影/片子A/A.MP4": 45}})
            check("POST 上报 200 updated=1", st == 200 and js.get("updated") == 1, str(js))
            st, js = _req(f"{base}/api/sync/progress")
            m = js.get("map", {})
            check("拉回含该键", "01电影/片子A/A.MP4" in m, str(list(m))[:80])
            check("值 p=45 带 t", m.get("01电影/片子A/A.MP4", {}).get("p") == 45, str(m))

            # 3) max 合并：低值不回退，高值更新
            _req(f"{base}/api/sync/progress", {"map": {"01电影/片子A/A.MP4": 30}})
            st, js = _req(f"{base}/api/sync/progress")
            check("30 不上顶（仍 45）",
                  js["map"]["01电影/片子A/A.MP4"]["p"] == 45, str(js["map"]))
            _req(f"{base}/api/sync/progress", {"map": {"01电影/片子A/A.MP4": 62}})
            st, js = _req(f"{base}/api/sync/progress")
            check("62 覆盖 45", js["map"]["01电影/片子A/A.MP4"]["p"] == 62, str(js["map"]))

            # 4) 非法值/键跳过
            st, js = _req(f"{base}/api/sync/progress",
                          {"map": {"": 50, "NO_SLASH": 50, "X/Y": 0, "Z/W": 150,
                                   "P/Q": "abc", "R/S": 88}})
            check("批量只收合法 1 条", js.get("updated") == 1, str(js))
            st, js = _req(f"{base}/api/sync/progress")
            check("非法键未入库", "R/S" in js["map"] and len(js["map"]) == 2, str(list(js["map"])))

            # 5) 坏 body
            req = urllib.request.Request(f"{base}/api/sync/progress",
                                         data=b"not json",
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    code, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                code, body = exc.code, exc.read()
            check("坏 body 返回 400", code == 400, f"code={code}")

            # 6) CSRF：跨站 Origin 拒绝（GET 同源/无 Origin 放行，POST 写必须同源）
            st, js = _req(f"{base}/api/sync/progress", {"map": {"T/U": 99}},
                          {"Origin": "http://evil.example"})
            check("跨站 Origin 被拒 403", st == 403, f"status={st} {js}")
            st, js = _req(f"{base}/api/sync/progress")
            check("跨站未真的写入", "T/U" not in js.get("map", {}), str(list(js.get("map", {}))))

            # 7) 持久化：重启后还在
            st, _ = _req(f"{base}/api/shutdown", {})
            proc.wait(timeout=10)
            proc = start(app_dir)
            st, js = _req(f"{base}/api/sync/progress")
            check("重启后仍在", js.get("map", {}).get("01电影/片子A/A.MP4", {}).get("p") == 62,
                  str(js.get("map")))

            # 8) clear
            st, js = _req(f"{base}/api/sync/progress/clear", {})
            check("clear 返回条数", st == 200 and js.get("cleared") == 2, str(js))
            st, js = _req(f"{base}/api/sync/progress")
            check("clear 后为空", js.get("map") == {}, str(js.get("map")))

            # 9) GET 不允许的方法（PUT 落到 404/405 —— dispatch 只有 GET/HEAD/POST）
            st, js = _req(f"{base}/api/trash/restore")  # GET 到 POST-only 路由（fsweb）对照
            check("fsweb 路由不受影响（GET 405）", st == 405, f"status={st}")
        finally:
            try:
                _req(f"{base}/api/shutdown", {})
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = 16
    print(f"\n结果: PASS {total - len(fails)} / FAIL {len(fails)}")
    if fails:
        print("失败项:", "；".join(fails))
        return 1
    print("SYNC_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
