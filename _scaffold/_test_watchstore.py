"""全域已看记录（v2.10.0）验收：真实起服务 + 真实 HTTP 请求。

覆盖：GET 空表与 rev、POST add/del 增量、rev 陈旧返回 409 且不写库、
clear 令 rev++、replace 整表替换、非法键跳过、坏 body、跨站 Origin 403、
clear 的跨站 403、非 POST 405、重启后仍在（持久化）。

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
import urllib.request
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"


def _port_free() -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", PORT)) != 0


def _req(url: str, data: dict | None = None,
         headers: dict[str, str] | None = None,
         method: str | None = None) -> tuple[int, dict]:
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=body, headers=hdrs,
        method=method or ("POST" if body is not None else "GET"))
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


def _raw(method: str, url: str, body: bytes,
         headers: dict[str, str] | None = None) -> int:
    req = urllib.request.Request(url, data=body, headers=dict(headers or {}),
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


def main() -> int:
    if not _port_free():
        print(f"端口 {PORT} 被占用，先停掉再跑")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="mw_watch_"))
    fails: list[str] = []
    checks = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            fails.append(name)

    media = tmp / "media"
    (media / "01 电影" / "片子A").mkdir(parents=True)
    (media / "01 电影" / "片子A" / "a.mp4").write_bytes(b"\x00" * 1024)

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

        proc = start(app_dir)
        try:
            # 1) 初始为空 + rev=0
            st, js = _req(f"{BASE}/api/sync/watched")
            check("GET 返回 200+ok", st == 200 and js.get("ok") is True, f"status={st}")
            check("初始 map 为空", js.get("map") == {}, str(js.get("map")))
            check("初始 rev=0", js.get("rev") == 0, f"rev={js.get('rev')}")
            check("初始 cleared_at 为空", js.get("cleared_at") == "", str(js.get("cleared_at")))

            # 2) 增量 add（带 rev=0）
            st, js = _req(f"{BASE}/api/sync/watched",
                          {"rev": 0, "add": ["DIR/A.MP4", "DIR/B.MP4"]})
            check("POST add 200 updated=2", st == 200 and js.get("updated") == 2, str(js))
            check("add 不改 rev", js.get("rev") == 0, f"rev={js.get('rev')}")
            st, js = _req(f"{BASE}/api/sync/watched")
            m = js.get("map", {})
            check("拉回含两个键", "DIR/A.MP4" in m and "DIR/B.MP4" in m, str(list(m)))
            check("值带 t 时间戳", bool(m.get("DIR/A.MP4", {}).get("t")), str(m.get("DIR/A.MP4")))

            # 3) 幂等：重复 add 不重复计数
            st, js = _req(f"{BASE}/api/sync/watched", {"rev": 0, "add": ["DIR/A.MP4"]})
            check("重复 add 幂等 updated=0", st == 200 and js.get("updated") == 0, str(js))

            # 4) 非法键跳过（必须含 /；空/裸名/超长都不收）
            st, js = _req(f"{BASE}/api/sync/watched",
                          {"rev": 0, "add": ["", "NO_SLASH", "X/Y", "  d i r / c.mp4  "]})
            check("合法键只收 2 条", js.get("updated") == 2, str(js))
            st, js = _req(f"{BASE}/api/sync/watched")
            check("裸名/空键未入库", "NO_SLASH" not in js["map"] and "X/Y" in js["map"],
                  str(list(js["map"])))

            # 5) 删除 del
            st, js = _req(f"{BASE}/api/sync/watched", {"rev": 0, "del": ["X/Y"]})
            check("del 删除生效", st == 200 and js.get("updated") == 1, str(js))
            st, js = _req(f"{BASE}/api/sync/watched")
            check("del 后 X/Y 不在", "X/Y" not in js.get("map", {}), str(list(js.get("map", {}))))

            # 6) rev 陈旧 → 409 stale，且不写库
            before = set(js.get("map", {}))
            st, js = _req(f"{BASE}/api/sync/watched",
                          {"rev": 999, "add": ["STALE/C.MP4"]})
            check("陈旧 rev 返回 409", st == 409, f"status={st}")
            check("返回 stale 标记", js.get("stale") is True, str(js))
            st, js2 = _req(f"{BASE}/api/sync/watched")
            check("陈旧增量未写库", set(js2.get("map", {})) == before, str(list(js2.get("map", {}))))

            # 7) 坏 body → 400（_drain_body 拒绝非 JSON）
            code = _raw("POST", f"{BASE}/api/sync/watched", b"not json",
                        {"Content-Type": "application/json"})
            check("坏 body 返回 400", code == 400, f"code={code}")

            # 8) 跨站 Origin → 403 且不写库
            st, js = _req(f"{BASE}/api/sync/watched", {"rev": 0, "add": ["EVIL/Z.MP4"]},
                          {"Origin": "http://evil.example"})
            check("跨站 Origin 被拒 403", st == 403, f"status={st} {js}")
            st, js = _req(f"{BASE}/api/sync/watched")
            check("跨站未真的写入", "EVIL/Z.MP4" not in js.get("map", {}), str(list(js.get("map", {}))))

            # 9) clear 的跨站 Origin → 403
            st, _ = _req(f"{BASE}/api/sync/watched/clear", {},
                         {"Origin": "http://evil.example"})
            check("clear 跨站被拒 403", st == 403, f"status={st}")

            # 10) clear 路由只收 POST：用 GET 打应 405
            #     （PUT 没有 do_PUT 会被 BaseHTTPRequestHandler 直接回 501，
            #      测不到 watchstore.handle 的 405 分支，故用 GET 打 clear）
            st, _ = _req(f"{BASE}/api/sync/watched/clear")
            check("GET /watched/clear 返回 405", st == 405, f"status={st}")

            # 11) clear：rev++ 且 map 清空
            st, js = _req(f"{BASE}/api/sync/watched/clear", {})
            check("clear 返回 cleared 条数", st == 200 and js.get("cleared", 0) >= 1, str(js))
            check("clear 令 rev=1", js.get("rev") == 1, f"rev={js.get('rev')}")
            st, js = _req(f"{BASE}/api/sync/watched")
            check("clear 后为空", js.get("map") == {}, str(js.get("map")))
            check("clear 记 cleared_at", bool(js.get("cleared_at")), str(js.get("cleared_at")))

            # 12) replace 整表替换（导入/迁移通道）
            st, js = _req(f"{BASE}/api/sync/watched",
                          {"replace": ["R/1.MP4", "R/2.MP4", "R/3.MP4"]})
            check("replace 200 updated=3", st == 200 and js.get("updated") == 3, str(js))
            check("replace 不改 rev（仍 1）", js.get("rev") == 1, f"rev={js.get('rev')}")
            st, js = _req(f"{BASE}/api/sync/watched")
            check("replace 后恰好 3 键", set(js.get("map", {})) == {"R/1.MP4", "R/2.MP4", "R/3.MP4"},
                  str(list(js.get("map", {}))))

            # 13) 持久化：重启后 rev 与 map 仍在
            st, _ = _req(f"{BASE}/api/shutdown", {})
            proc.wait(timeout=10)
            proc = start(app_dir)
            st, js = _req(f"{BASE}/api/sync/watched")
            check("重启后 rev=1", js.get("rev") == 1, f"rev={js.get('rev')}")
            check("重启后 map 仍在", "R/2.MP4" in js.get("map", {}), str(list(js.get("map", {}))))

            # 14) 进度路由未受影响（独立模块互不干扰）
            st, _ = _req(f"{BASE}/api/sync/progress", {"map": {"P/Q": 50}})
            st2, js2 = _req(f"{BASE}/api/sync/progress")
            check("进度路由未受影响", st == 200 and js2.get("map", {}).get("P/Q", {}).get("p") == 50,
                  str(js2.get("map")))
        finally:
            try:
                _req(f"{BASE}/api/shutdown", {})
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果: PASS {checks - len(fails)} / FAIL {len(fails)} / 共 {checks} 项")
    if fails:
        print("失败项:", "；".join(fails))
        return 1
    print("WATCH_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
