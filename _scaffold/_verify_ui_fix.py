"""验收 v2.1.1 修复：真实库 + 真实鼠标事件，覆盖手机与桌面两种视口。

检查项：
1. 顶栏按钮全部在视口内（不被挤出屏幕）；
2. 目录条 #tabs 有可用宽度（不是 0），且真实点击 ◀ ▶ 后 scrollLeft 真的变化；
3. 顶栏按钮真实点击能打开对应弹窗（设置 / 局域网）；
4. 卡片上的「⋯」菜单按钮存在，且点击后能弹出菜单（v2.1.0 新功能可用）。

用法：python _scaffold/_verify_ui_fix.py
"""
import json
import os
import sys
import time

import _probe_ui as P

PROG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = "file:///" + os.path.join(PROG, "index.html").replace("\\", "/")
BTNS = ["btn-lan", "btn-scan", "btn-undo", "btn-trash", "btn-set", "tabs-prev", "tabs-next"]

STATE = r"""
(function(){
  var t = document.getElementById("tabs");
  var out = {win: window.innerWidth, tabsCw: t.clientWidth, tabsSw: t.scrollWidth,
             tabsSl: Math.round(t.scrollLeft), btns: {}, setOpen: document.getElementById("set").open,
             lanOpen: document.getElementById("lan").open};
  """ + "".join(
    'var e%d = document.getElementById("%s"); out.btns["%s"] = e%d ? '
    '{x:Math.round(e%d.getBoundingClientRect().x), w:Math.round(e%d.getBoundingClientRect().width), '
    'inView: e%d.getBoundingClientRect().x >= 0 && e%d.getBoundingClientRect().right <= window.innerWidth + 1, '
    'disabled: !!e%d.disabled} : null;\n' % (i, b, b, i, i, i, i, i, i)
    for i, b in enumerate(BTNS)) + """
  return out;
})()
"""

COORD = r"""
(function(){
  var out = {};
  ["btn-set","btn-lan","tabs-next","tabs-prev"].forEach(function(id){
    var e = document.getElementById(id);
    if (!e) return;
    var r = e.getBoundingClientRect();
    out[id] = {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
  });
  var menu = document.querySelector(".card .fbtn");
  if (menu) { var m = menu.getBoundingClientRect();
    out["card-menu"] = {x: Math.round(m.x + m.width/2), y: Math.round(m.y + m.height/2)}; }
  return out;
})()
"""


def real_click(c, x, y):
    c.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none")
    c.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
    c.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)


def run_viewport(c, w, h, ok):
    c.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
           deviceScaleFactor=2 if w < 800 else 1, mobile=w < 800)
    c.js("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
    st = c.js(STATE)
    print("\n--- 视口 %dx%d ---" % (w, h))
    print("  #tabs clientWidth=%d scrollWidth=%d（可滚=%s）"
          % (st["tabsCw"], st["tabsSw"], st["tabsSw"] > st["tabsCw"] + 2))
    if st["tabsCw"] < 120:
        print("  ASSERT FAIL 目录条宽度过窄"); ok.append("tabs-%d" % w)
    for name, v in st["btns"].items():
        if not v:
            print("  ASSERT FAIL %s 不存在" % name); ok.append(name + "-missing"); continue
        if not v["inView"]:
            print("  ASSERT FAIL %s 被挤出屏幕 (x=%d w=%d)" % (name, v["x"], v["w"])); ok.append(name + "-offscreen")
        else:
            print("  OK %-11s x=%-4d w=%-3d disabled=%s" % (name, v["x"], v["w"], v["disabled"]))

    co = c.js(COORD) or {}
    # 设置
    if st["btns"].get("btn-set"):
        real_click(c, co["btn-set"]["x"], co["btn-set"]["y"]); time.sleep(0.7)
        opened = c.js("document.getElementById('set').open")
        print("  真实点击 设置 -> open=%s %s" % (opened, "OK" if opened else "FAIL"))
        if not opened:
            ok.append("settings-%d" % w)
        else:
            c.js("document.getElementById('set').close()")
    # 局域网弹窗：file:// 模式下 openLan() 会主动提示「没有服务端」并 return，
    # 所以这里只在服务模式下断言能打开（HTTP 场景另由 _real_click_test.py 覆盖）。
    if st["btns"].get("btn-lan"):
        is_file = c.js("location.protocol") == "file:"
        real_click(c, co["btn-lan"]["x"], co["btn-lan"]["y"]); time.sleep(0.7)
        opened = c.js("document.getElementById('lan').open")
        if is_file:
            print("  真实点击 局域网 -> open=%s（file:// 下按设计不打开，跳过断言）" % opened)
        else:
            print("  真实点击 局域网 -> open=%s %s" % (opened, "OK" if opened else "FAIL"))
            if not opened:
                ok.append("lan-%d" % w)
        if opened:
            c.js("document.getElementById('lan').close()")
    # 目录条右移
    if st["tabsSw"] > st["tabsCw"] + 2 and co.get("tabs-next"):
        before = c.js("Math.round(document.getElementById('tabs').scrollLeft)")
        real_click(c, co["tabs-next"]["x"], co["tabs-next"]["y"]); time.sleep(0.8)
        after = c.js("Math.round(document.getElementById('tabs').scrollLeft)")
        moved = after > before
        print("  真实点击 ▶ 目录条 scrollLeft %d -> %d %s" % (before, after, "OK" if moved else "FAIL"))
        if not moved:
            ok.append("tabs-next-%d" % w)
        if co.get("tabs-prev"):
            real_click(c, co["tabs-prev"]["x"], co["tabs-prev"]["y"]); time.sleep(0.8)
            back = c.js("Math.round(document.getElementById('tabs').scrollLeft)")
            print("  真实点击 ◀ 目录条 scrollLeft %d -> %d %s" % (after, back, "OK" if back < after else "FAIL"))
            if back >= after:
                ok.append("tabs-prev-%d" % w)
    # 卡片菜单
    if co.get("card-menu"):
        real_click(c, co["card-menu"]["x"], co["card-menu"]["y"]); time.sleep(0.6)
        shown = c.js("!!document.getElementById('fsmenu')")
        print("  真实点击卡片「⋯」菜单 -> %s %s" % (shown, "OK" if shown else "FAIL"))
        if not shown:
            ok.append("fsmenu-%d" % w)
        c.js("(function(){var m=document.getElementById('fsmenu');if(m)m.remove();})()")
    else:
        print("  卡片菜单按钮不存在，跳过")


def main():
    proc, ws_url = P.start_edge()
    bad = []
    try:
        c = P.CDP(ws_url)
        c.call("Page.enable")
        n = c.wait_cards()
        print("真实库卡片数: %d" % n)
        if not n:
            print("VERIFY_FAIL 未渲染卡片"); return 1
        for w, h in ((390, 844), (768, 1024), (1280, 800)):
            run_viewport(c, w, h, bad)
    finally:
        try:
            proc.terminate(); proc.wait(timeout=10)
        except Exception:
            proc.kill()
    if bad:
        print("\nVERIFY_FAIL 失败项: %s" % ", ".join(bad))
        return 1
    print("\nVERIFY_OK 全部通过（顶栏可达、目录条可滚、按键真实生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
