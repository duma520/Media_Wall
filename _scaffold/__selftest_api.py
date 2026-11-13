"""Media_Wall HTTP API 冒烟自检（开发期脚本，命名 __selftest_api.js 对应物）。

用法：
    1) 先启动服务：python server.py --no-browser --port 8799
    2) python __selftest_api.py  [--port 8799]

检查项（全部 GET 无需本机门禁；写端点从 127.0.0.1 发起本身即本机）：
 A. /api/ping、/api/status（版本/根/library 就绪）
 B. /api/library（version=2、items、categories、cat_scanned）
 C. /api/categories（16 分区、items 计数与 library 一致）
 D. /api/root（当前根 + categories 列表）与 /api/root?path= 校验（合法/非法统一文案）
 E. /thumb 首海报 200 + image/*；/media Range 206 精确字节 + 非法 Range 416 + 越界 416
 F. 路径安全：/media/../config.json 类穿越 403；/data/config.json 403；/data/library.js 200
 G. POST /api/scan 空 body（增量全扫，应秒级复用缓存）→202，轮询 /api/status 直到完成
 H. POST /api/scan {"category":"20 综艺"} → 202 → 完成
 I. POST /api/shutdown → 200 且端口真的释放（连接被拒，放最后）
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 脚本位于 _scaffold/，让 mediawall 可导入
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8799"
PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


def get(path: str, headers: dict | None = None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, dict(r.headers), r.read()


def get404(path: str, headers: dict | None = None):
    """预期非 2xx：返回状态码。"""
    try:
        req = urllib.request.Request(BASE + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def post(path: str, payload: dict | None = None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, {}


def main() -> int:
    port = 8799
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    global BASE
    BASE = f"http://127.0.0.1:{port}"

    print("A. 健康检查")
    st, _, body = get("/api/ping")
    check("GET /api/ping 200 ok", st == 200 and json.loads(body)["ok"] is True)
    st, _, body = get("/api/status")
    j = json.loads(body)
    # 版本号不写死：直接取包内 VERSION，避免每次发版都要改自检脚本。
    from mediawall import VERSION as _VERSION
    check(f"GET /api/status version={_VERSION}",
          j.get("version") == _VERSION, str(j)[:120])
    check("library_exists", j.get("library_exists") is True)
    root_str = j.get("root", "")
    check("root 非空", bool(root_str), root_str)
    # 与 /api/config 报出的 media_root 比对：不写死具体共享路径，
    # 避免把本机 NAS 地址写进仓库。
    _st_c, _, _body_c = get("/api/config")
    _cfg_root = (json.loads(_body_c).get("media_root") or "").replace("/", "\\").rstrip("\\").lower()
    check("root 与 /api/config 的 media_root 一致",
          bool(_cfg_root) and root_str.replace("/", "\\").rstrip("\\").lower() == _cfg_root,
          f"root={root_str!r} config={_cfg_root!r}")

    print("B. /api/library")
    st, _, body = get("/api/library")
    lib = json.loads(body)
    check("200 且 version=2", st == 200 and lib.get("version") == 2)
    check("items 非空", len(lib.get("items", [])) > 0)
    check("categories 16 个", len(lib.get("categories", [])) == 16, str(len(lib.get("categories", []))))
    check("cat_scanned 含 20 综艺", "20 综艺" in (lib.get("cat_scanned") or {}))
    n_items = len(lib["items"])

    print("C. /api/categories")
    st, _, body = get("/api/categories")
    cats = json.loads(body)["categories"]
    check("200 且 16 分区", st == 200 and len(cats) == 16)
    m = {c["name"]: c for c in cats}
    lib_counts: dict[str, int] = {}
    for it in lib["items"]:
        lib_counts[it["category"]] = lib_counts.get(it["category"], 0) + 1
    check("分区计数与 library 一致",
          all(m[k]["items"] == lib_counts.get(k, 0) for k in m),
          str([(k, m[k]["items"], lib_counts.get(k)) for k in m if m[k]["items"] != lib_counts.get(k, 0)][:3]))
    check("01 电影 last_scan 有值", bool(m.get("01 电影", {}).get("last_scan")))

    print("D. /api/root 读取与校验")
    st, _, body = get("/api/root")
    j = json.loads(body)
    _cats = j.get("categories") or []
    # 分区个数不写死：按实际媒体根下发现的数量判断（不同机器分区数不同）
    check("GET /api/root 200 + categories 非空", st == 200 and len(_cats) >= 1, str(_cats)[:120])
    _first_cat = str(_cats[0]) if _cats else ""
    _probe = str(j.get("path") or "") or None
    if not _probe:
        _st_c, _, _body_c = get("/api/config")
        _probe = json.loads(_body_c).get("media_root") or ""
    _probe_path = _probe.rstrip("\\/") + (("\\" + _first_cat) if _first_cat else "")
    st2, _, body = get("/api/root?path=" + urllib.parse.quote(_probe_path))
    j = json.loads(body)
    check("校验合法子目录 valid=true", st2 == 200 and j.get("valid") is True)
    bad_code = get404("/api/root?path=" + urllib.parse.quote(r"\\127.0.0.1\不存在\xxx"))
    check("非法路径 → 400（统一文案防 oracle）", bad_code == 400, str(bad_code))

    print("E. 媒体流 / 缩略图")
    first_with_poster = next((it for it in lib["items"] if it.get("poster")), None)
    assert first_with_poster
    rel = first_with_poster["poster"]
    st, hdr, body = get("/thumb/" + urllib.parse.quote(rel, safe="/"))
    check("GET /thumb 海报 200 image/*", st == 200 and hdr.get("Content-Type", "").startswith("image/"))
    video = None
    for it in lib["items"]:
        if it.get("videos"):
            video = it["videos"][0]
            break
    assert video
    vrel = video["path"]
    st, hdr, body = get("/media/" + urllib.parse.quote(vrel, safe="/"),
                        headers={"Range": "bytes=0-1023"})
    check("Range 0-1023 → 206", st == 206, str(st))
    check("Content-Length=1024", hdr.get("Content-Length") == "1024", str(hdr.get("Content-Length")))
    check("Content-Range 正确", hdr.get("Content-Range", "").startswith("bytes 0-1023/"), str(hdr.get("Content-Range")))
    size = int(hdr.get("Content-Range", "/0")
               .rsplit("/", 1)[-1]) if hdr.get("Content-Range") else 0
    check("全文件 size 与 library 记录一致（±1 容差）",
          abs(size - int(video["size"])) <= 1, f"api={size} lib={video['size']}")
    st = get404("/media/" + urllib.parse.quote(vrel, safe="/") + "?_",
                headers={"Range": f"bytes={size+500}-"})
    check("起始越界 Range → 416", st == 416, str(st))
    st = get404("/media/" + urllib.parse.quote(vrel, safe="/"), headers={"Range": "bytes=abc"})
    check("非法 Range → 416", st == 416, str(st))
    st = get404("/media/" + urllib.parse.quote(vrel.rsplit("/", 1)[0] + "/../config.json", safe="/"))
    check("穿越 ../config.json → 403/404", st in (403, 404), str(st))
    doc_it = next((it for it in lib["items"] if it.get("docs")), None)
    if doc_it:
        dname = doc_it["docs"][0]["name"]
        drel = doc_it["path"].rsplit("/", 1)
        # 文档扩展名不可走 /media（只允许视频/音频）
        full = doc_it["category"] + "/" + doc_it["path"] + "/" + dname
        st = get404("/media/" + urllib.parse.quote(full, safe="/"))
        check("文档走 /media → 403", st == 403, str(st))

    print("F. 静态资源门禁")
    st = get404("/data/config.json")
    check("/data/config.json → 403", st == 403, str(st))
    st = get404("/data/library.meta.json")
    check("/data/library.meta.json → 403", st == 403, str(st))
    st, _, body = get("/data/library.js")
    check("/data/library.js → 200", st == 200)
    check("library.js 以 __MW_LIBRARY__ 开头", body[:60].decode("utf-8", "replace").startswith("window.__MW_LIBRARY__"))
    st, _, body = get("/")
    check("GET / → index.html", st == 200 and b"__MW_LIBRARY__" in body)
    st = get404("/data/%2e%2e%2fconfig.json")
    check("编码穿越 → 403", st == 403, str(st))

    print("G. 全量增量扫描（应大量复用缓存）")
    # 服务刚启动时的后台初始扫描可能还在跑（新启动实例扫 16 分区需几分钟）。
    # 先等它结束再触发，避免撞上 409「scan already running」——那是正确行为，
    # 但会让本用例的断言失真。
    wait_scan()
    st, j = post("/api/scan", {})
    check("POST /api/scan → 202", st == 202, f"{st} {j}")
    done, ok = wait_scan()
    check("扫描结束且成功", done and ok)

    print("H. 单分区扫描")
    st, j = post("/api/scan", {"category": "20 综艺"})
    check("POST /api/scan category → 202", st == 202, f"{st} {j}")
    done, ok = wait_scan()
    check("单分区扫描结束且成功", done and ok)
    st, j = post("/api/scan", {"category": "不存在分区"})
    check("未知分区 → 400", st == 400, f"{st} {j}")

    print("I. shutdown + 端口释放")
    try:
        st, j = post("/api/shutdown")
        check("POST /api/shutdown → 200", st == 200 and j.get("ok") is True, f"{st} {j}")
    except Exception as exc:  # noqa: BLE001
        check("POST /api/shutdown → 200", False, str(exc))

    # v1.2.2：以前这里只断言 HTTP 200，掩盖了「返回 200 但进程不退出」的 bug；
    # 现在必须看到连接被拒，才算真的停服。
    check(f"shutdown 后端口 {port} 释放（连接被拒）", port_closed(port))

    print(f"\n结果: PASS {PASS} / FAIL {FAIL}")
    return 0 if FAIL == 0 else 1


def port_closed(port: int, timeout: float = 10.0) -> bool:
    """等端口真的拒绝连接（= 服务进程已退出），返回是否释放成功。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
        except OSError:
            return True
        time.sleep(0.2)
    return False


def wait_scan(timeout: float = 900.0) -> tuple[bool, bool]:
    t0 = time.monotonic()
    first = True
    while time.monotonic() - t0 < timeout:
        if first:
            time.sleep(1.0)
            first = False
        try:
            _, _, body = get("/api/status")
            sc = json.loads(body).get("scan", {})
            if not sc.get("running"):
                if sc.get("ok") is False:
                    print("     scan error:", sc.get("error"))
                    return True, False
                return True, True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    return False, False


if __name__ == "__main__":
    raise SystemExit(main())
