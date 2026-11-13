"""历史埋点验证：真实起服务 + 真实 HTTP 请求，确认 page/play 都写进 history.json。

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
PORT = 8790


def _free_port() -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", PORT)) != 0


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    if not _free_port():
        print(f"端口 {PORT} 被占用，先停掉再跑")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="mw_hooks_"))
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            fails.append(name)

    try:
        # 1) 造一个临时程序副本 + 媒体树
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
        (media / "01 电影" / "测试片" / "a.mp4").write_bytes(b"\x00" * 2048)
        (media / "01 电影" / "测试片" / "cover.jpg").write_bytes(b"\xff\xd8\xff")

        proc = subprocess.Popen(
            [PY, "server.py", "--root", str(media), "--port", str(PORT),
             "--no-browser"],
            cwd=str(app_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        # 等端口起来
        for _ in range(60):
            if not _free_port():
                break
            time.sleep(0.25)
        else:
            print("服务没起来")
            return 3

        try:
            # 2) 首页（应记一条 page）
            status, body = _get(f"http://127.0.0.1:{PORT}/")
            check("GET / 返回 200", status == 200, f"status={status}, {len(body)}B")

            # 3) 播放请求（Range 0-0，应记一条 play）
            st2, body2 = _get(
                f"http://127.0.0.1:{PORT}/media/01%20%E7%94%B5%E5%BD%B1/"
                f"%E6%B5%8B%E8%AF%95%E7%89%87/a.mp4",
                {"Range": "bytes=0-0"})
            check("GET /media Range 返回 206", st2 == 206, f"status={st2}")

            # 4) 同文件再请求一次（30 秒去重，不应新增）
            _get(f"http://127.0.0.1:{PORT}/media/01%20%E7%94%B5%E5%BD%B1/"
                 f"%E6%B5%8B%E8%AF%95%E7%89%87/a.mp4", {"Range": "bytes=0-1"})
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        # 5) 读 history.json
        hist = app_dir / "data" / "history.jsonl"
        events: list[dict] = []
        if hist.is_file():
            events = [json.loads(line) for line in
                      hist.read_text(encoding="utf-8").splitlines() if line.strip()]
        kinds = [e.get("kind") for e in events]
        check("history.jsonl 已生成", hist.is_file())
        check("记录了 page 事件", "page" in kinds, str(kinds))
        check("记录了 play 事件", "play" in kinds, str(kinds))
        check("去重生效（总共 2 条，不因重复请求增加）", len(events) == 2,
              f"实际 {len(events)} 条")
        play = next((e for e in events if e.get("kind") == "play"), {})
        check("play 记录带 IP", bool(play.get("ip")), str(play.get("ip")))
        check("play 记录带分区", play.get("category") == "01 电影",
              str(play.get("category")))
        check("play 记录带文件名", play.get("name") == "a.mp4", str(play.get("name")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果: PASS {8 - len(fails)} / FAIL {len(fails)}")
    if fails:
        print("失败项:", "；".join(fails))
        return 1
    print("HOOKS_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
