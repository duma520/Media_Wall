"""网页端文件管理（v2.1.0）验收：真实起服务 + 真实 HTTP 请求。

覆盖：列目录、越界拒绝、删除到回收站、回收站列表、恢复、撤回、移动、
保护目录拒绝、CSRF（跨站 Origin）拒绝。

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
PORT = 8791


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

    tmp = Path(tempfile.mkdtemp(prefix="mw_fsweb_"))
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            fails.append(name)

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
        (film / "片子A" / "a.mp4").write_bytes(b"\x00" * 4096)
        (film / "片子A" / "cover.jpg").write_bytes(b"\xff\xd8\xff")
        (film / "片子B").mkdir()
        (film / "片子B" / "b.mp4").write_bytes(b"\x00" * 4096)
        (film / "新目录").mkdir()
        (film / "新目录" / "c.mp4").write_bytes(b"\x00" * 1024)

        proc = subprocess.Popen(
            [PY, "server.py", "--root", str(media), "--port", str(PORT),
             "--no-browser"],
            cwd=str(app_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        for _ in range(80):
            if not _port_free():
                break
            time.sleep(0.25)
        else:
            print("服务没起来")
            return 3

        base = f"http://127.0.0.1:{PORT}"
        q = urllib.parse.quote
        try:
            # 1) 列目录
            st, js = _req(f"{base}/api/fs/dirs?path={q('01 电影')}")
            names = [d["name"] for d in js.get("dirs", [])]
            check("列目录返回 200", st == 200, f"status={st}")
            check("列出 3 个作品目录", sorted(names) == ["新目录", "片子A", "片子B"],
                  str(names))
            check("不带回收站/隐藏目录", ".mediawall_trash" not in names, str(names))

            # 2) 越界
            st, js = _req(f"{base}/api/fs/dirs?path={q('../../etc')}")
            check("越界路径被拒", st == 400 and not js.get("ok"), f"status={st}")

            # 3) 删除到回收站
            st, js = _req(f"{base}/api/fs/delete", {"path": "01 电影/片子B"})
            check("删除返回 200", st == 200 and js.get("ok"), f"status={st} {js}")
            check("目录已从磁盘消失", not (film / "片子B").exists())
            check("回收站计数 1", js.get("trash", {}).get("count") == 1,
                  str(js.get("trash")))
            trash_dir = media / ".mediawall_trash"
            check("回收站目录已建立", trash_dir.is_dir())

            # 4) 回收站列表
            st, js = _req(f"{base}/api/trash")
            items = js.get("items", [])
            check("回收站列表 1 条", st == 200 and len(items) == 1,
                  f"status={st}, n={len(items)}")
            entry_id = items[0].get("id") if items else ""
            check("回收站条目带 id", bool(entry_id), str(entry_id))
            check("回收站条目记录了占用字节", int(items[0].get("bytes") or 0) > 0,
                  str(items[0].get("bytes")))
            check("回收站汇总字节 > 0", int(js.get("bytes") or 0) > 0,
                  str(js.get("bytes")))

            # 5) 恢复
            st, js = _req(f"{base}/api/trash/restore", {"id": entry_id})
            check("恢复返回 200", st == 200 and js.get("ok"), f"status={st} {js}")
            check("文件回到原位置", (film / "片子B" / "b.mp4").is_file())

            # 6) 删除后撤回
            st, js = _req(f"{base}/api/fs/delete", {"path": "01 电影/片子B"})
            check("再次删除成功", st == 200, f"status={st}")
            st, js = _req(f"{base}/api/undo", {})
            check("撤回返回 200", st == 200 and js.get("ok"), f"status={st} {js}")
            check("撤回后文件复原", (film / "片子B" / "b.mp4").is_file())

            # 7) 移动：同父目录应拒绝
            st, js = _req(f"{base}/api/fs/move",
                          {"path": "01 电影/片子B", "dest": "01 电影"})
            check("移动到同一父目录被拒", st == 400, f"status={st}")

            # 8) 移动：到子目录
            st, js = _req(f"{base}/api/fs/move",
                          {"path": "01 电影/片子B", "dest": "01 电影/新目录"})
            check("移动返回 200", st == 200 and js.get("ok"), f"status={st} {js}")
            check("文件已到新位置",
                  (film / "新目录" / "片子B" / "b.mp4").is_file())
            check("原位置已不存在", not (film / "片子B").exists())

            # 9) 撤回移动
            st, js = _req(f"{base}/api/undo", {})
            check("撤回移动返回 200", st == 200 and js.get("ok"), f"status={st} {js}")
            check("移动已撤回", (film / "片子B" / "b.mp4").is_file())

            # 10) 保护目录：一级分区不可删
            st, js = _req(f"{base}/api/fs/delete", {"path": "01 电影"})
            check("一级分区不可删除", st == 400 and "受保护" in str(js.get("error")),
                  f"status={st} {js.get('error')}")

            # 11) CSRF：跨站 Origin 必须拒绝
            st, js = _req(f"{base}/api/fs/delete", {"path": "01 电影/片子B"},
                          {"Origin": "http://evil.example"})
            check("跨站 Origin 被拒", st == 403, f"status={st} {js.get('error')}")
            check("跨站请求未真的删除", (film / "片子B" / "b.mp4").is_file())

            # 12) GET 上不允许删除
            st, js = _req(f"{base}/api/fs/delete")
            check("GET 删除返回 405", st == 405, f"status={st}")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = 23
    print(f"\n结果: PASS {total - len(fails)} / FAIL {len(fails)}")
    if fails:
        print("失败项:", "；".join(fails))
        return 1
    print("FSWEB_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
