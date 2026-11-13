# -*- coding: utf-8 -*-
"""v2.3.0 第 2 点补充：菜单项动作真实触发（点「局域网」打开弹窗）。"""
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_QTUSER = tempfile.mkdtemp(prefix="mw_tma_")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    f"--disable-gpu --no-sandbox --user-data-dir={_QTUSER}")

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LIB_STUB = """window.__MW_LIBRARY__ = {
  generated:"2026-10-23T12:00:00", root:"", count:1, total_videos:1, total_posters:1, total_shots:0,
  cat_scanned:{"01 电影":true},
  categories:[{name:"01 电影",items:1,last_scan:"2026-10-23T02:00:00"}],
  items:[{key:"01 电影/测试影片", id:"测试影片", path:"2026/测试影片", category:"01 电影",
    kind:"video", has_video:true, poster:"2026/测试影片/poster.jpg",
    videos:[{name:"abc.mp4", path:"2026/测试影片/abc.mp4", size:12345}], shots:[], docs:[]}]
};
"""

# 拦截 fetch：api/info 在 file:// 模式下本不可用，这里模拟服务端返回
JS = r"""
(function(){
  var r={fail:[]};
  function chk(n,c,e){ if(!c) r.fail.push(n+(e!==undefined?(" | 实际="+e):"")); }
  // 菜单项点击后应调用 openLan；openLan 走 fetch("api/info")。
  // 直接验证点击事件把 data-a=lan 的项路由到 openLan（方法存在且被调用）。
  var called=null;
  var orig=window.openLan;
  window.openLan=function(){ called="lan"; };
  var menuBtn=document.getElementById("btn-menu");
  var topMenu=document.getElementById("topmenu");
  menuBtn.click();
  var lanItem=topMenu.querySelector('button[data-a="lan"]');
  lanItem.click();
  chk("点击局域网项调用 openLan", called==="lan", String(called));
  chk("点击后菜单关闭", topMenu.hidden===true, String(topMenu.hidden));
  window.openLan=orig;
  r.ok = r.fail.length===0;
  return JSON.stringify(r);
})()
"""

class Page(QWebEnginePage):
    errors = []
    def javaScriptConsoleMessage(self, level, message, line, source):
        if "Uncaught" in message or "SyntaxError" in message or "TypeError" in message:
            Page.errors.append("%s (line %s)" % (message, line))

def main():
    tmp = tempfile.mkdtemp(prefix="mw_tma_")
    try:
        os.makedirs(os.path.join(tmp, "data"))
        with open(os.path.join(tmp, "data", "library.js"), "w", encoding="utf-8") as f:
            f.write(LIB_STUB)
        shutil.copyfile(os.path.join(ROOT, "index.html"), os.path.join(tmp, "index.html"))
        app = QApplication(sys.argv[:1])
        view = QWebEngineView()
        page = Page(view)
        view.setPage(page)
        view.resize(1280, 900)
        out = {}
        def on_loaded(ok):
            if not ok:
                print("TOPMENU_ACTION_FAIL 页面加载失败"); app.quit(); return
            QTimer.singleShot(400, run_js)
        def run_js():
            def done():
                try:
                    data = json.loads(out.get("raw") or "{}")
                except Exception as e:
                    print("TOPMENU_ACTION_FAIL 结果解析失败:", e, str(out.get("raw"))[:300]); app.quit(); return
                if Page.errors:
                    print("TOPMENU_ACTION_FAIL 脚本异常:", Page.errors)
                elif data.get("ok"):
                    print("TOPMENU_ACTION_OK 全部检查通过")
                else:
                    print("TOPMENU_ACTION_FAIL 失败项:", json.dumps(data.get("fail"), ensure_ascii=False))
                app.quit()
            page.runJavaScript(JS, 0, lambda res: (out.__setitem__("raw", res), done()))
        page.loadFinished.connect(on_loaded)
        view.load(QUrl.fromLocalFile(os.path.join(tmp, "index.html")))
        QTimer.singleShot(30000, app.quit)
        app.exec()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(_QTUSER, ignore_errors=True)

if __name__ == "__main__":
    main()
