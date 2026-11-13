"""测量不同视口宽度下顶栏各组件的实际宽度（CDP，只读）。

用途：定位「目录条左右按钮不好用」的根因——#tabs 被压到多宽、还能不能滚。

用法：python _scaffold/_measure_topbar.py
"""
import json
import sys

import _probe_ui as P

SIZES = [(375, 780), (414, 896), (768, 1024), (900, 800), (1180, 800), (1440, 900), (1920, 1080)]

PROBE = r"""
(function(){
  function r(sel){
    var e = document.querySelector(sel);
    if(!e) return null;
    var b = e.getBoundingClientRect();
    return {x:Math.round(b.x), w:Math.round(b.width)};
  }
  var t = document.getElementById("tabs");
  return {
    win: window.innerWidth,
    brand: r("#brand"),
    hbar: r(".hbar"),
    tabs: r("#tabs"),
    tabsScroll: {sw:t.scrollWidth, cw:t.clientWidth, canScroll:t.scrollWidth > t.clientWidth+2},
    sub: r("#subbar"),
    sq: r("#sqwrap"),
    lan: r("#btn-lan"), scan: r("#btn-scan"),
    undo: r("#btn-undo"), trash: r("#btn-trash"), set: r("#btn-set")
  };
})()
"""


def main():
    proc, ws = P.start_edge()
    rows = []
    try:
        c = P.CDP(ws)
        c.call("Page.enable")
        c.wait_cards()
        for w, h in SIZES:
            c.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
                   deviceScaleFactor=1, mobile=w < 800)
            c.js("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
            v = c.js(PROBE)
            if not v:
                print("测不到 %dx%d" % (w, h)); continue
            rows.append(v)
            print("%4d  brand=%4s hbar=%4s tabs=%4s/%4s(sw=%d) sub=%4s sq=%4s | lan=%3s scan=%3s undo=%3s trash=%3s set=%3s"
                  % (v["win"],
                     (v["brand"] or {}).get("w", "-"), (v["hbar"] or {}).get("w", "-"),
                     (v["tabs"] or {}).get("w", "-"), (v["tabs"] or {}).get("x", "-"),
                     v["tabsScroll"]["sw"], (v["sub"] or {}).get("w", "-"), (v["sq"] or {}).get("w", "-"),
                     (v["lan"] or {}).get("w", "-"), (v["scan"] or {}).get("w", "-"),
                     (v["undo"] or {}).get("w", "-"), (v["trash"] or {}).get("w", "-"),
                     (v["set"] or {}).get("w", "-")))
        print("\n判定：tabs 宽度 < 120px 时，一次点击只滚 ~0.7*宽，用户几乎看不出变化 → 表现为「按键失效」。")
        bad = [v["win"] for v in rows if (v["tabs"] or {}).get("w", 0) < 120]
        print("受影响宽度: %s" % (bad or "无"))
    finally:
        try:
            proc.terminate(); proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
