# -*- coding: utf-8 -*-
"""v2.5.0 前端音轨 UI 冒烟（QtWebEngine 无头）：按钮显隐/菜单/记忆函数。"""
import json, os, shutil, sys, tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_QTUSER = tempfile.mkdtemp(prefix="mw_trackui_")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
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

JS = r"""
(function(){
  var r={fail:[]};
  function chk(n,c,e){ if(!c) r.fail.push(n+(e!==undefined?(" | 实际="+e):"")); }
  chk("VD_BTN_KEYS 含 track", VD_BTN_KEYS.indexOf("track")>=0, VD_BTN_KEYS.join(","));
  chk("音轨按钮存在", !!document.getElementById("vd-track"));
  chk("音轨菜单存在", !!document.getElementById("vd-trackmenu"));
  chk("音轨菜单初始隐藏", document.getElementById("vd-trackmenu").hidden===true);
  setTrackPref("2026/测试影片/abc.mp4", 1);
  chk("记忆音轨写入", getTrackPref("2026/测试影片/abc.mp4")===1, String(getTrackPref("2026/测试影片/abc.mp4")));
  var raw=JSON.parse(localStorage.getItem("mediawall.track")||"{}");
  chk("记忆落盘 localStorage", raw["测试影片/ABC.MP4"]===1, JSON.stringify(raw));
  setTrackPref("2026/测试影片/abc.mp4", 0);
  chk("改回 0", getTrackPref("2026/测试影片/abc.mp4")===0, String(getTrackPref("2026/测试影片/abc.mp4")));
  chk("默认 vdBtns.track=true", settings.vdBtns.track!==false, String(settings.vdBtns.track));
  settings.vdBtns.track=false; applyPlayerUI();
  chk("关闭后音轨按钮隐藏", document.getElementById("vd-track").style.display==="none",
      document.getElementById("vd-track").style.display);
  settings.vdBtns.track=true; applyPlayerUI();
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
    tmp = tempfile.mkdtemp(prefix="mw_trackui_")
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
                print("TRACKUI_FAIL 页面加载失败"); app.quit(); return
            QTimer.singleShot(400, run_js)
        def run_js():
            def done():
                try:
                    data = json.loads(out.get("raw") or "{}")
                except Exception as e:
                    print("TRACKUI_FAIL 结果解析失败:", e, str(out.get("raw"))[:300]); app.quit(); return
                if Page.errors:
                    print("TRACKUI_FAIL 脚本异常:", Page.errors)
                elif data.get("ok"):
                    print("TRACKUI_OK 全部检查通过")
                else:
                    print("TRACKUI_FAIL 失败项:", json.dumps(data.get("fail"), ensure_ascii=False))
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
