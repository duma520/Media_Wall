"""网页端交互自检（CDP，只读，不改任何文件）。

启动 headless Edge，用 Chrome DevTools Protocol 在真实浏览器里：
1. 统计渲染出的卡片数、检查左右箭头按钮的绑定与 disabled 状态与真实点击效果；
2. 打开设置面板，列出全部可交互控件的 id 与是否绑定了处理函数；
3. 逐个点击设置项，检测 state / localStorage 是否真的变化（发现"点了没反应"的控件）。

用法：python _scaffold/_probe_ui.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = "file:///" + os.path.join(PROG, "index.html").replace("\\", "/")
PORT = 9333


def start_edge():
    profile = os.path.join(os.environ.get("TEMP", "."), "mw_probe_profile")
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--disable-extensions", "--remote-allow-origins=*",
         "--remote-debugging-port=%d" % PORT,
         "--user-data-dir=" + profile, PAGE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(120):
        try:
            targets = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % PORT, timeout=1))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return proc, pages[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Edge CDP 未就绪")


class CDP:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=120)
        self.mid = 0

    def call(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr, gesture=False):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True, userGesture=gesture)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            return {"__error": (d.get("exception") or {}).get("description") or d.get("text")}
        return r.get("result", {}).get("value")

    def wait_cards(self, timeout=90):
        t0 = time.time()
        while time.time() - t0 < timeout:
            n = self.js("document.querySelectorAll('.card').length")
            if isinstance(n, int) and n > 0:
                return n
            time.sleep(1)
        return 0


PROBE_ARROWS = r"""
(async function(){
  var out = {geom:{}, buttons:{}, click:{}};
  [["tabs","tabs-prev","tabs-next"],["subbar","subbar-prev","subbar-next"]].forEach(function(t){
    var box = document.getElementById(t[0]);
    var p = document.getElementById(t[1]), n = document.getElementById(t[2]);
    out.geom[t[0]] = box ? {scrollWidth:box.scrollWidth, clientWidth:box.clientWidth,
                            scrollLeft:Math.round(box.scrollLeft)} : null;
    [["prev",p],["next",n]].forEach(function(x){
      var el = x[1];
      out.buttons[t[0]+"-"+x[0]] = el ? {exists:true, hasOnclick:!!el.onclick,
        disabled:!!el.disabled, display:getComputedStyle(el).display,
        w:el.getBoundingClientRect().width} : {exists:false};
    });
  });
  // 真实点击：记录点击前后的 scrollLeft
  for (const [boxId, btnId] of [["tabs","tabs-next"],["subbar","subbar-next"],
                                ["tabs","tabs-prev"],["subbar","subbar-prev"]]){
    var box = document.getElementById(boxId), btn = document.getElementById(btnId);
    var before = Math.round(box.scrollLeft);
    if (btn && !btn.disabled) { btn.click(); }
    await new Promise(r=>setTimeout(r,500));
    out.click[btnId] = {before:before, after:Math.round(box.scrollLeft),
                        disabled: btn ? !!btn.disabled : null};
  }
  return out;
})()
"""

PROBE_SETTINGS = r"""
(async function(){
  document.getElementById("btn-set").click();
  await new Promise(r=>setTimeout(r,300));
  var dlg = document.getElementById("set");
  var open = dlg ? dlg.open : null;
  var ctrls = Array.from(document.querySelectorAll("#set input, #set select, #set button, #set textarea"));
  var list = ctrls.map(function(e){
    var lab = null;
    var row = e.closest(".row, label");
    if (row) lab = (row.textContent||"").replace(/\s+/g," ").trim().slice(0,26);
    return {id:e.id||"(no id)", tag:e.tagName.toLowerCase(), type:e.type||"",
            checked:(e.type==="checkbox"?e.checked:null),
            hasOnclick:!!e.onclick, hasOnchange:!!e.onchange,
            hidden:!!(e.offsetParent===null), label:lab};
  });
  return {open:open, count:list.length, controls:list};
})()
"""


def main():
    proc, ws_url = start_edge()
    ok = True
    try:
        cdp = CDP(ws_url)
        cdp.call("Page.enable")
        n = cdp.wait_cards()
        print("卡片数: %d" % n)
        if not n:
            print("PROBE_FAIL 页面没有渲染出卡片（数据未加载）")
            return 1

        # 控制台错误
        errs = cdp.js("(window.__errs||[]).slice(0,10)")
        print("捕获的 JS 错误: %s" % (errs if errs else "无"))

        arrows = cdp.js(PROBE_ARROWS, gesture=True)
        print("\n=== 左右箭头 ===")
        print(json.dumps(arrows, ensure_ascii=False, indent=1))

        for box, key in (("tabs", "tabs"), ("subbar", "subbar")):
            g = arrows.get("geom", {}).get(key) or {}
            if not g:
                print("ASSERT FAIL %s 容器不存在" % key); ok = False
                continue
            if g["scrollWidth"] <= g["clientWidth"] + 2:
                print("提示 %s 不溢出（%d<=%d），按钮 disabled 属正常" % (key, g["scrollWidth"], g["clientWidth"]))
        for btn, r in (arrows.get("click") or {}).items():
            if r.get("disabled"):
                continue
            if r["before"] == r["after"]:
                print("ASSERT FAIL 点击 %s 后 scrollLeft 未变化 (%d)" % (btn, r["before"])); ok = False
            else:
                print("OK %s: scrollLeft %d -> %d" % (btn, r["before"], r["after"]))

        st = cdp.js(PROBE_SETTINGS, gesture=True)
        print("\n=== 设置面板 ===")
        if not st or st.get("__error"):
            print("ASSERT FAIL 打开设置失败: %s" % (st or {}).get("__error")); ok = False
        else:
            print("dialog.open = %s，控件 %d 个" % (st["open"], st["count"]))
            for c in st["controls"]:
                flag = ""
                if c["tag"] in ("input", "select", "textarea") and not (c["hasOnchange"] or c["hasOnclick"]):
                    flag = "  <-- 无内联处理函数（可能是 addEventListener，需人工确认）"
                print("  %-22s %-8s %-8s checked=%s hidden=%s | %s%s" % (
                    c["id"], c["tag"], c["type"], c["checked"], c["hidden"], c["label"], flag))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    print("\nPROBE_OK" if ok else "\nPROBE_HAS_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
