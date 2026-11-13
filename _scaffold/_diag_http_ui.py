"""在真实 HTTP 服务 + 手机视口下复现「按键全部失效」，并抓早期 JS 异常（只读）。

与 file:// 的差别正是要验证的点：用户是通过 http://<ip>:8787 访问的。

用法：python _scaffold/_diag_http_ui.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import _probe_ui as P

PROG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8801

EARLY_HOOK = r"""
window.__errs = [];
window.onerror = function(m, s, l, c, e){
  window.__errs.push("onerror: " + m + " @ " + s + ":" + l + ":" + c);
  return false;
};
window.addEventListener("unhandledrejection", function(ev){
  window.__errs.push("unhandledrejection: " + (ev.reason && ev.reason.message || ev.reason));
});
"""

CHECK_ALL = r"""
(function(){
  var out = {errs: (window.__errs||[]).slice(0,20), clicks:{}, found:{}};
  var ids = ["btn-lan","btn-scan","btn-set","btn-undo","btn-trash","tabs-prev","tabs-next","subbar-prev","subbar-next"];
  ids.forEach(function(id){
    var e = document.getElementById(id);
    out.found[id] = e ? {exists:true, disabled:!!e.disabled, w:Math.round(e.getBoundingClientRect().width)} : {exists:false};
  });
  return out;
})()
"""

CLICK_TEST = r"""
(async function(){
  var out = {};
  function probe(name, fn){
    try { out[name] = fn(); } catch(e){ out[name] = "THROW: " + e.message; }
  }
  // 1. 设置按钮
  var dlg = document.getElementById("set");
  var before = dlg.open;
  document.getElementById("btn-set").click();
  await new Promise(r=>setTimeout(r,400));
  out.settings = {before: before, after: dlg.open};
  if (dlg.open) { try{ document.getElementById("s-cancel") ? 0 : 0; dlg.close(); }catch(e){} }
  // 2. 局域网地址按钮
  var lan = document.getElementById("lan");
  if (lan) { var wasOpen = lan.open; document.getElementById("btn-lan").click();
             await new Promise(r=>setTimeout(r,300)); out.lan = {before: wasOpen, after: lan.open};
             try{ lan.close(); }catch(e){} }
  // 3. 目录条箭头
  ["tabs","subbar"].forEach(function(k){
    var box = document.getElementById(k), n = document.getElementById(k+"-next");
    if (!box || !n) { out[k+"Arrow"] = "MISSING"; return; }
    var b = Math.round(box.scrollLeft);
    if (!n.disabled) n.click();
    out[k+"Arrow"] = {before: b, disabled: n.disabled, clientWidth: box.clientWidth, scrollWidth: box.scrollWidth};
  });
  await new Promise(r=>setTimeout(r,500));
  out.tabsAfter = Math.round(document.getElementById("tabs").scrollLeft);
  // 4. 卡片上的按钮
  var b0 = document.querySelector(".card .fbtn");
  out.cardButton = b0 ? {exists:true, w:Math.round(b0.getBoundingClientRect().width)} : {exists:false};
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


def main():
    tmp = tempfile.mkdtemp(prefix="mw_http_")
    media = os.path.join(tmp, "media")
    os.makedirs(media, exist_ok=True)
    with open(os.path.join(media, "a.txt"), "w", encoding="utf-8") as f:
        f.write("x")
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
            print("服务未起来，输出：\n%s" % (srv.stdout.read() or "")[-2000:])
            return 1
        url = "http://127.0.0.1:%d/" % PORT
        proc, ws_url = P.start_edge()
        c = P.CDP(ws_url)
        c.call("Page.enable")
        c.call("Runtime.enable")
        c.call("Page.addScriptToEvaluateOnNewDocument", source=EARLY_HOOK)
        c.call("Emulation.setDeviceMetricsOverride", width=375, height=780, deviceScaleFactor=2, mobile=True)
        c.call("Page.navigate", url=url)
        time.sleep(6)
        print("页面 URL:", c.js("location.href"))
        print("\n=== 早期异常 ===")
        errs = c.js("(window.__errs||[])")
        print(json.dumps(errs, ensure_ascii=False, indent=1) if errs else "无")
        print("\n=== 控件存在性 ===")
        print(json.dumps(c.js(CHECK_ALL), ensure_ascii=False, indent=1))
        print("\n=== 点击测试（手机视口 375）===")
        print(json.dumps(c.js(CLICK_TEST, gesture=True), ensure_ascii=False, indent=1))
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
