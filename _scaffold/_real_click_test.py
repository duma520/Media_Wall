"""用真实鼠标事件（CDP Input.dispatchMouseEvent）测试按键，排除 el.click() 的假象。

`el.click()` 不是用户激活，`showModal()` 在非激活状态下会抛 InvalidStateError，
会让「功能正常」误判成「按键失效」。本脚本发真实鼠标按下的坐标事件。

用法：python _scaffold/_real_click_test.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import _probe_ui as P

PROG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8802

CLICK_JS = r"""
(function(){
  var out = {};
  ["btn-set","btn-lan","btn-scan","btn-undo","btn-trash","tabs-next","subbar-next"].forEach(function(id){
    var e = document.getElementById(id);
    if (!e) { out[id] = null; return; }
    var r = e.getBoundingClientRect();
    out[id] = {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
               w: Math.round(r.width), h: Math.round(r.height),
               inViewport: r.x >= 0 && r.x + r.width <= window.innerWidth,
               disabled: !!e.disabled};
  });
  out.__state = {setOpen: document.getElementById("set").open,
                 lanOpen: document.getElementById("lan") ? document.getElementById("lan").open : null,
                 tabsSl: Math.round(document.getElementById("tabs").scrollLeft),
                 tabsCw: document.getElementById("tabs").clientWidth};
  return out;
})()
"""


def wait_port(port, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except Exception:
            time.sleep(0.4)
    return False


def real_click(c, x, y):
    c.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none")
    c.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
    c.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)


def main():
    tmp = tempfile.mkdtemp(prefix="mw_click_")
    media = os.path.join(tmp, "media")
    os.makedirs(media, exist_ok=True)
    sub = os.path.join(media, "01 电影", "测试片")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "a.mp4"), "wb") as f:
        f.write(b"\x00" * 64)
    for name in ("server.py", "scanner.py", "index.html"):
        shutil.copy2(os.path.join(PROG, name), os.path.join(tmp, name))
    shutil.copytree(os.path.join(PROG, "mediawall"), os.path.join(tmp, "mediawall"),
                    ignore=shutil.ignore_patterns("__pycache__"))

    srv = subprocess.Popen([sys.executable, "server.py", "--port", str(PORT),
                            "--root", media, "--no-browser"],
                           cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    proc = None
    try:
        if not wait_port(PORT):
            print("服务未起来"); return 1
        proc, ws_url = P.start_edge()
        c = P.CDP(ws_url)
        c.call("Page.enable")
        c.call("Input.setIgnoreInputEvents", ignore=False)
        c.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
        c.call("Page.navigate", url="http://127.0.0.1:%d/" % PORT)
        time.sleep(7)

        print("卡片数:", c.js("document.querySelectorAll('.card').length"))
        box = c.js(CLICK_JS) or {}
        print("\n=== 控件坐标 ===")
        for k, v in box.items():
            if k.startswith("__"):
                continue
            print("  %-12s %s" % (k, v))
        print("  初始状态:", box.get("__state"))

        for target, probe in (("btn-set", "document.getElementById('set').open"),
                              ("btn-lan", "document.getElementById('lan').open"),
                              ("tabs-next", None)):
            info = box.get(target)
            if not info:
                print("MISSING", target); continue
            x, y = info["x"], info["y"]
            if not info["inViewport"]:
                print("%s 不在视口内（x=%d, w=%d），用户点不到 -> 布局问题" % (target, x, info["w"]))
            before = c.js(probe) if probe else c.js("Math.round(document.getElementById('tabs').scrollLeft)")
            real_click(c, x, y)
            time.sleep(0.6)
            after = c.js(probe) if probe else c.js("Math.round(document.getElementById('tabs').scrollLeft)")
            print("%-12s 真实点击: %s -> %s  %s" % (target, before, after, "OK" if before != after else "无变化"))
            if target == "btn-set" and after:
                c.js("document.getElementById('set').close()")
            if target == "btn-lan" and after:
                c.js("document.getElementById('lan').close()")
    finally:
        if proc:
            try:
                proc.terminate(); proc.wait(timeout=10)
            except Exception:
                proc.kill()
        try:
            srv.terminate(); srv.wait(timeout=10)
        except Exception:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
