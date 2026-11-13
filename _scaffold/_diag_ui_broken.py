"""诊断：设置面板打不开 + 目录条左右按钮无效（CDP，真实浏览器里逐项验证）。

与 _probe_ui.py 的区别：本脚本直接 try/catch 调函数，定位抛出异常的那一行，
并单独测试 scrollBy / scrollLeft 是否真的能滚动 #tabs、#subbar。

用法：python _scaffold/_diag_ui_broken.py
"""
import json
import sys

import _probe_ui as P


def main():
    proc, ws = P.start_edge()
    try:
        c = P.CDP(ws)
        c.call("Page.enable")
        n = c.wait_cards()
        print("cards:", n)

        print("\n[1] openSettings() 直接调用")
        print(c.js("(function(){try{openSettings();return 'OK open='+document.getElementById('set').open;}"
                   "catch(e){return 'ERR '+e.name+': '+e.message+'\\nSTACK '+((e.stack||'').split('\\n').slice(0,4).join(' | '));}})()",
                   gesture=True))

        print("\n[2] 仅 showModal()")
        print(c.js("(function(){try{document.getElementById('set').showModal();"
                   "return 'open='+document.getElementById('set').open;}catch(e){return 'ERR '+e.name+': '+e.message;}})()",
                   gesture=True))

        print("\n[3] applySettingsToUI()")
        print(c.js("(function(){try{applySettingsToUI();return 'OK';}"
                   "catch(e){return 'ERR '+e.name+': '+e.message+' @ '+((e.stack||'').split('\\n')[1]||'');}})()"))

        print("\n[4] showSetPage(curSetPage)")
        print(c.js("(function(){try{showSetPage(curSetPage);return 'OK curSetPage='+curSetPage;}"
                   "catch(e){return 'ERR '+e.name+': '+e.message;}})()"))

        print("\n[5] watchedText / renderProgressList / loadMediaRoot")
        for fn in ("watchedText", "renderProgressList", "loadMediaRoot"):
            print("  %-18s %s" % (fn, c.js("(function(){try{%s();return 'OK';}"
                                            "catch(e){return 'ERR '+e.name+': '+e.message;}})()" % fn)))

        print("\n[6] #tabs 几何与滚动能力")
        print(c.js("(function(){var t=document.getElementById('tabs');"
                   "return {sw:t.scrollWidth,cw:t.clientWidth,sl:Math.round(t.scrollLeft),"
                   "overflowX:getComputedStyle(t).overflowX,pos:getComputedStyle(t).position};})()"))

        print("\n[7] 直接 scrollBy(200)")
        print(c.js("(async function(){var t=document.getElementById('tabs');var b=Math.round(t.scrollLeft);"
                   "t.scrollBy({left:200});await new Promise(r=>setTimeout(r,400));"
                   "return {before:b,after:Math.round(t.scrollLeft)};})()"))

        print("\n[8] 直接 scrollLeft=200")
        print(c.js("(async function(){var t=document.getElementById('tabs');t.scrollLeft=200;"
                   "await new Promise(r=>setTimeout(r,200));return Math.round(t.scrollLeft);})()"))

        print("\n[9] scrollDir 函数本体")
        print(c.js("typeof scrollDir === 'function' ? scrollDir.toString().slice(0,200) : 'MISSING'"))

        print("\n[10] #tabs-next 状态与父容器")
        print(c.js("(function(){var n=document.getElementById('tabs-next');if(!n)return 'MISSING';"
                   "var r=n.getBoundingClientRect();return {disabled:n.disabled,onclick:!!n.onclick,"
                   "w:Math.round(r.width),h:Math.round(r.height),display:getComputedStyle(n).display,"
                   "parentCls:n.parentElement.className};})()"))

        print("\n[11] #subbar 几何")
        print(c.js("(function(){var t=document.getElementById('subbar');if(!t)return 'MISSING';"
                   "return {sw:t.scrollWidth,cw:t.clientWidth,sl:Math.round(t.scrollLeft),"
                   "overflowX:getComputedStyle(t).overflowX,hidden:t.hidden,cls:t.className};})()"))

        print("\n[12] 我的新按钮是否与箭头重名/遮挡")
        print(c.js("(function(){var ids=['btn-undo','btn-trash','btn-set','btn-lan','btn-scan'];"
                   "return ids.map(function(i){var e=document.getElementById(i);if(!e)return i+':MISSING';"
                   "var r=e.getBoundingClientRect();return i+':'+Math.round(r.x)+','+Math.round(r.width);});})()"))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
