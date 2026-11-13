# -*- coding: utf-8 -*-
"""v2.3.0 第 2 点验证：顶栏「☰ 菜单」下拉。

QtWebEngine 无头：确认旧 5 按钮消失、新菜单存在、点击展开/收起、
5 个菜单项动作绑定正确。
"""
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_QTUSER = tempfile.mkdtemp(prefix="mw_topmenu_")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    f"--disable-gpu --no-sandbox --user-data-dir={_QTUSER}")

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

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
  var menuBtn=document.getElementById("btn-menu");
  var topMenu=document.getElementById("topmenu");
  chk("菜单按钮存在", !!menuBtn, "btn-menu");
  chk("菜单浮层存在", !!topMenu, "topmenu");
  chk("旧按钮已移除", !document.getElementById("btn-lan")
      && !document.getElementById("btn-scan")
      && !document.getElementById("btn-undo")
      && !document.getElementById("btn-trash")
      && !document.getElementById("btn-restart"));
  chk("设置按钮保留", !!document.getElementById("btn-set"));
  if(topMenu) chk("菜单初始隐藏", topMenu.hidden===true, String(topMenu.hidden));
  if(menuBtn && topMenu){
    menuBtn.click();
    chk("点击展开", topMenu.hidden===false, String(topMenu.hidden));
    var items=topMenu.querySelectorAll("button[data-a]");
    chk("菜单项=5", items.length===5, String(items.length));
    var acts={};
    for(var i=0;i<items.length;i++) acts[items[i].getAttribute("data-a")]=1;
    chk("含局域网", !!acts.lan, Object.keys(acts).join(","));
    chk("含扫描", !!acts.scan, Object.keys(acts).join(","));
    chk("含撤回", !!acts.undo, Object.keys(acts).join(","));
    chk("含回收站", !!acts.trash, Object.keys(acts).join(","));
    chk("含重启", !!acts.restart, Object.keys(acts).join(","));
    // 再点一次收起
    menuBtn.click();
    chk("再点收起", topMenu.hidden===true, String(topMenu.hidden));
    // 打开后点空白处关闭
    menuBtn.click();
    document.body.click();
    chk("点空白关闭", topMenu.hidden===true, String(topMenu.hidden));
  }
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
    tmp = tempfile.mkdtemp(prefix="mw_topmenu_")
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
                print("TOPMENU_FAIL 页面加载失败")
                app.quit()
                return
            QTimer.singleShot(400, run_js)

        def run_js():
            def done():
                try:
                    data = json.loads(out.get("raw") or "{}")
                except Exception as e:
                    print("TOPMENU_FAIL 结果解析失败:", e, str(out.get("raw"))[:300])
                    app.quit()
                    return
                if Page.errors:
                    print("TOPMENU_FAIL 脚本异常:", Page.errors)
                elif data.get("ok"):
                    print("TOPMENU_OK 全部检查通过")
                else:
                    print("TOPMENU_FAIL 失败项:", json.dumps(data.get("fail"), ensure_ascii=False))
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
