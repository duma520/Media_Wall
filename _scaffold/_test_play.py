# -*- coding: utf-8 -*-
"""v2.9.0 播放格式支持验收：真实起服务 + 真实 ffmpeg 转换 + 真实 HTTP 流。

覆盖：decide 决策 / playinfo 端点 / 触发转换并轮询 ready / /media?fmt= Range 206
/ direct 文件 fmt=409 / wma→mp3 全链路 / 穿越与未知扩展名 / 清缓存 + 跨站 403
/ api/tracks 回归 / 扫描扩容（rmvb/m2ts 入库）。

全程在临时副本里跑，不碰项目 data/ 与 NAS。用法：python _scaffold/_test_play.py
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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8794
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


def _port_free() -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", PORT)) != 0


def _req(url: str, data: dict | None = None, headers: dict | None = None,
         method: str | None = None) -> tuple[int, bytes, dict]:
    body = json.dumps(data).encode() if data is not None else None
    hdrs = dict(headers or {})
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=body, headers=hdrs,
        method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _jreq(url: str, **kw) -> tuple[int, dict]:
    st, raw, _ = _req(url, **kw)
    try:
        return st, json.loads(raw or b"{}")
    except ValueError:
        return st, {"raw": raw[:200].decode("utf-8", "replace")}


FFMPEG = shutil.which("ffmpeg") or r"D:\ffmpeg\bin\ffmpeg.exe"


def synth(media: Path) -> bool:
    """生成 4 秒小样本。返回 wma 是否成功（部分 ffmpeg 构建无 wmav2）。"""
    media.mkdir(parents=True, exist_ok=True)
    lav_a = ["-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
    lav_v = ["-f", "lavfi", "-i", "testsrc=size=128x96:rate=10:duration=4"]
    vid = [FFMPEG, "-y", "-v", "error"] + lav_a + lav_v + \
          ["-map", "1:v", "-map", "0:a", "-c:v", "libx264", "-preset", "ultrafast"]
    for out, acodec in [("sample.ts", "aac"), ("sample.mkv", "mp3"),
                        ("audio_ac3.mkv", "ac3"), ("sample.mp4", "aac")]:
        subprocess.run(vid + ["-c:a", acodec, str(media / out)], check=True, timeout=120)
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "sine=frequency=660:duration=4",
                    "-c:a", "wmav2", str(media / "song.wma")],
                   capture_output=True, timeout=120,
                   creationflags=0x08000000 if os.name == "nt" else 0)
    (media / "fake.rmvb").write_bytes(b"\x2e\x52\x4d\x01" + b"\x00" * 64)
    (media / "fake.m2ts").write_bytes(b"G\x47\x01\x10" + b"\x00" * 187)
    return (media / "song.wma").is_file()


def main() -> int:
    if not _port_free():
        print(f"端口 {PORT} 被占用")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="mw_play_"))
    try:
        app_dir = tmp / "prog"
        app_dir.mkdir()
        for name in ("server.py", "scanner.py", "index.html"):
            shutil.copy2(PROG / name, app_dir / name)
        shutil.copytree(PROG / "mediawall", app_dir / "mediawall",
                        ignore=shutil.ignore_patterns("__pycache__"))

        media = tmp / "media" / "01 测试区" / "sub"
        wma_ok = synth(media)

        proc = subprocess.Popen(
            [PY, "server.py", "--root", str(tmp / "media"), "--port", str(PORT),
             "--no-browser"],
            cwd=str(app_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(80):
            if not _port_free():
                break
            time.sleep(0.25)

        # 等首轮扫描结束（library.json 出现）
        libp = app_dir / "data" / "library.json"
        for _ in range(120):
            if libp.is_file():
                break
            time.sleep(0.5)
        time.sleep(1.0)

        # ---- 1. 扫描扩容 ----
        try:
            lib = json.loads(libp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            check("扫描产出 library.json", False, str(e))
            return 1
        items = lib.get("items") or []
        vids = {v["name"] for it in items for v in it.get("videos") or []}
        auds = {a["name"] for it in items for a in it.get("audio") or []}
        check("fake.rmvb 入视频", "fake.rmvb" in vids, str(sorted(vids))[:120])
        check("fake.m2ts 入视频", "fake.m2ts" in vids, str(sorted(vids))[:120])
        check("sample.ts 入视频", "sample.ts" in vids, str(sorted(vids))[:120])
        if wma_ok:
            check("song.wma 入音频", "song.wma" in auds, str(sorted(auds))[:120])

        rel = urllib.parse.quote("01 测试区/sub/sample.ts")
        # ---- 2. playinfo 决策 ----
        st, j = _jreq(f"{base}/api/playinfo?path={rel}")
        check("playinfo ts→remux", st == 200 and j.get("decision") == "remux", str(j)[:160])
        check("初始 not_started", j.get("status") == "not_started", str(j)[:160])

        st, j = _jreq(f"{base}/api/playinfo?path="
                      + urllib.parse.quote("01 测试区/sub/audio_ac3.mkv"))
        check("playinfo ac3→remux_audio", j.get("decision") == "remux_audio", str(j)[:160])
        st, j = _jreq(f"{base}/api/playinfo?path="
                      + urllib.parse.quote("01 测试区/sub/sample.mp4"))
        check("playinfo mp4→direct", j.get("decision") == "direct", str(j)[:160])

        # ---- 3. build=1 触发并轮询 ready ----
        _jreq(f"{base}/api/playinfo?path={rel}&build=1")
        st = ""
        for _ in range(240):
            _, j = _jreq(f"{base}/api/playinfo?path={rel}")
            st = j.get("status", "")
            if st in ("ready", "error"):
                break
            time.sleep(0.5)
        check("remux ready", st == "ready", f"status={st} {str(j)[:160]}")

        # ---- 4. /media?fmt=mp4 流式 ----
        st, raw, hd = _req(f"{base}/media/{rel}?fmt=mp4",
                           headers={"Range": "bytes=0-1023"})
        check("fmt=mp4 Range 206", st == 206 and len(raw) == 1024, f"st={st} n={len(raw)}")
        check("fmt=mp4 类型", "video/mp4" in (hd.get("Content-Type") or ""), str(hd)[:120])
        st, raw2, _ = _req(f"{base}/media/{rel}?fmt=mp4")
        check("输出为 mp4 容器", st == 200 and b"ftyp" in raw2[:16], raw2[:16])

        # ---- 5. direct 文件 fmt= → 409 ----
        st, _, _ = _req(f"{base}/media/"
                        + urllib.parse.quote("01 测试区/sub/sample.mp4") + "?fmt=mp4")
        check("direct fmt=409", st == 409, f"st={st}")

        # ---- 6. wma → mp3 全链路 ----
        if wma_ok:
            relw = urllib.parse.quote("01 测试区/sub/song.wma")
            _, j = _jreq(f"{base}/api/playinfo?path={relw}&build=1")
            check("wma→transcode_audio", j.get("decision") == "transcode_audio", str(j)[:160])
            for _ in range(240):
                _, j = _jreq(f"{base}/api/playinfo?path={relw}")
                if j.get("status") in ("ready", "error"):
                    break
                time.sleep(0.5)
            check("wma→mp3 ready", j.get("status") == "ready", str(j)[:200])
            st, raw, hd = _req(f"{base}/media/{relw}?fmt=mp3")
            check("fmt=mp3 流", st == 200 and len(raw) > 500, f"st={st} n={len(raw)}")
            check("fmt=mp3 类型", "audio/mpeg" in (hd.get("Content-Type") or ""), str(hd)[:120])

        # ---- 7. 安全 ----
        st, _, _ = _req(f"{base}/api/playinfo?path={urllib.parse.quote('../../windows/win.ini')}")
        check("穿越拒绝", st in (403, 404), f"st={st}")
        st, _, _ = _req(f"{base}/api/playinfo?path={urllib.parse.quote('01 测试区/sub/no.xyz')}")
        check("未知扩展 404", st == 404, f"st={st}")

        # ---- 8. 清缓存 ----
        st, j = _jreq(f"{base}/api/play/clear", data={}, method="POST",
                      headers={"Origin": base})
        check("clear 同源 200", st == 200 and j.get("ok"), str(j)[:120])
        check("clear 删除>0", (j.get("removed") or 0) >= 1, str(j))
        _, j = _jreq(f"{base}/api/playinfo?path={rel}")
        check("clear 后 not_started", j.get("status") == "not_started", str(j)[:160])
        st, _, _ = _req(f"{base}/api/play/clear", data={}, method="POST",
                        headers={"Origin": "http://evil.example"})
        check("clear 跨站 403", st == 403, f"st={st}")

        # ---- 9. /api/tracks 回归（搬入 play.py 后） ----
        st, j = _jreq(f"{base}/api/tracks?path={rel}")
        check("api/tracks 正常", st == 200 and j.get("ok") and "tracks" in j, str(j)[:160])
        check("ts 探测到音轨", len(j.get("tracks") or []) >= 1, str(j.get("tracks"))[:120])

        try:
            _jreq(f"{base}/api/shutdown", data={}, method="POST", headers={"Origin": base})
        except OSError:
            pass
        proc.wait(timeout=15)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nPLAY_E2E {'OK' if not fails else 'FAILED: ' + ', '.join(fails)}"
          f"  （{len(fails)} 项失败）")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
