# -*- coding: utf-8 -*-
"""v2.10.0 已看记录前端验收（QtWebEngine 无头，file:// 副本）。

覆盖两组：
  A. 纯解析函数：parseWatchedInput（两段键直接采纳 / 裸名补全 / 反斜杠归一 /
     未匹配计 miss）、parseWatchedFileText（JSON keys / 纯数组 / map 对象 /
     TXT 每行 / 坏 JSON 报错 / 空文件）。
  B. 迁移与一致性（mock fetch + 覆盖 refreshSeen/render）：
     rev=0 时本地旧缓存与服务端并集、本地独有键补推、换设备拉取能看到、
     服务端清空（rev++）后本地旧缓存不复活、rev 对齐。

全程在临时目录的 index.html 副本上跑，不碰项目真实文件与 data/。
"""
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_QTUSER = tempfile.mkdtemp(prefix="mw_watchui_")
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

JS = r"""
(function(){
  var R={fail:[]};
  function chk(n,c,e){ if(!c) R.fail.push(n+(e!==undefined?(" | 实际="+e):"")); }

  /* ---------- A. 解析函数 ---------- */
  // 用真实的多段 Windows 全路径（资源管理器复制出来的形态）：归一为 / 后
  // 取末两段 -> 某目录/片子.MP4。若只给两段（C:\某目录\片子.mp4），盘符会
  // 并入第一段得到 C:某目录/片子.MP4 —— 这是 normKey 既有行为，不是本次改动。
  var p1=parseWatchedInput(["测试影片/ABC.MP4","abc.mp4","不存在.mp4","","D:\\\u7535\u5f71\\2026\\\u67d0\u76ee\u5f55\\\u7247\u5b50.mp4"]);
  chk("A1 两段键直接采纳", p1.keys.indexOf("测试影片/ABC.MP4")>=0, JSON.stringify(p1.keys));
  chk("A2 裸名补全且与两段键去重",
      p1.keys.filter(function(k){return k==="测试影片/ABC.MP4";}).length===1, JSON.stringify(p1.keys));
  chk("A3 反斜杠路径归一为末两段", p1.keys.indexOf("某目录/片子.MP4")>=0, JSON.stringify(p1.keys));
  chk("A4 未匹配裸名计 miss=1", p1.miss===1, p1.miss);

  var f1=parseWatchedFileText('{"keys":["A/B.MP4","C/D.MP4"]}',"x.json");
  chk("A5 JSON keys 解析", f1.lines.length===2 && f1.lines[0]==="A/B.MP4", JSON.stringify(f1));
  var f2=parseWatchedFileText('["A/B.MP4"]',"x.txt");
  chk("A6 纯数组按 JSON 解析", f2.lines.length===1 && f2.lines[0]==="A/B.MP4", JSON.stringify(f2));
  var f3=parseWatchedFileText('{"map":{"A/B.MP4":{"t":"x"}}}',"x.json");
  chk("A7 map 对象解析", f3.lines.length===1 && f3.lines[0]==="A/B.MP4", JSON.stringify(f3));
  var f4=parseWatchedFileText("A/B.MP4\nC/D.MP4","x.txt");
  chk("A8 TXT 每行一条", f4.lines.length===2, JSON.stringify(f4));
  var f5=parseWatchedFileText("{bad","x.json");
  chk("A9 坏 JSON 报错", !!f5.err, JSON.stringify(f5));
  var f6=parseWatchedFileText("   ","x.txt");
  chk("A10 空文件报错", !!f6.err, JSON.stringify(f6));

  /* ---------- B. 迁移与一致性（mock fetch） ---------- */
  var S={rev:0, cleared_at:"", map:{}};
  var realFetch=window.fetch, realRefresh=window.refreshSeen, realRender=window.render;
  window.fetch=function(url,opt){
    if(opt && opt.method==="POST"){
      var b=JSON.parse(opt.body||"{}");
      if(b.add) b.add.forEach(function(k){ S.map[k]={t:"x"}; });
      if(b.del) b.del.forEach(function(k){ delete S.map[k]; });
      return Promise.resolve({json:function(){return Promise.resolve(
        {ok:true,rev:S.rev,count:Object.keys(S.map).length,updated:0,mode:"merge"});}});
    }
    return Promise.resolve({json:function(){return Promise.resolve(
      {ok:true,rev:S.rev,cleared_at:S.cleared_at,map:S.map});}});
  };
  window.refreshSeen=function(){}; window.render=function(){};
  WATCH_OK=true; watchedRev=0; watchPending={add:{},del:{}};
  watched={"OLD/A.MP4":true,"OLD/B.MP4":true};

  watchPull().then(function(){
    chk("B1 rev=0 并集保留本地记录",
        Object.keys(watched).length===2 && !!watched["OLD/A.MP4"], JSON.stringify(Object.keys(watched)));
    chk("B2 本地独有键入队待补推",
        Object.keys(watchPending.add).length===2, JSON.stringify(Object.keys(watchPending.add)));
    return watchFlushPush();
  }).then(function(){
    chk("B3 首次迁移写入服务端",
        !!S.map["OLD/A.MP4"] && !!S.map["OLD/B.MP4"], JSON.stringify(Object.keys(S.map)));
    // 模拟另一台设备：本地空，从服务端拉
    watched={}; watchPending={add:{},del:{}}; watchedRev=0;
    return watchPull();
  }).then(function(){
    chk("B4 换设备能看到已看记录",
        Object.keys(watched).length===2 && !!watched["OLD/A.MP4"], JSON.stringify(Object.keys(watched)));
    // 模拟服务端清空：rev++ 且 map 清空，但本机仍残留旧缓存
    S.rev=1; S.map={}; S.cleared_at="now";
    watched={"STALE/OLD.MP4":true}; watchPending={add:{},del:{}}; watchedRev=0;
    return watchPull();
  }).then(function(){
    chk("B5 清空后旧缓存不复活", Object.keys(watched).length===0, JSON.stringify(Object.keys(watched)));
    chk("B6 rev 对齐为 1", watchedRev===1, watchedRev);
    window.fetch=realFetch; window.refreshSeen=realRefresh; window.render=realRender;
    WATCH_OK=false;
    R.ok=R.fail.length===0;
    window.__R__=JSON.stringify(R);
  }).catch(function(e){
    R.fail.push("异常:"+e); R.ok=false;
    window.__R__=JSON.stringify(R);
  });
})();
"""


class Page(QWebEnginePage):
    errors = []

    def javaScriptConsoleMessage(self, level, message, line, source):
        if "Uncaught" in message or "SyntaxError" in message or "TypeError" in message:
            Page.errors.append("%s (line %s)" % (message, line))


def main():
    tmp = tempfile.mkdtemp(prefix="mw_watchui_")
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

        def on_loaded(ok):
            if not ok:
                print("WATCH_UI_FAIL 页面加载失败")
                app.quit()
                return
            QTimer.singleShot(400, lambda: page.runJavaScript(JS))
            QTimer.singleShot(3000, collect)

        def collect():
            page.runJavaScript("window.__R__ || '{}'", got)

        def got(res):
            try:
                data = json.loads(res or "{}")
            except Exception as e:
                print("WATCH_UI_FAIL 结果解析失败:", e, str(res)[:300])
                app.quit()
                return
            if Page.errors:
                print("WATCH_UI_FAIL 脚本异常:", Page.errors)
            elif data.get("ok"):
                print("WATCH_UI_OK 全部检查通过")
            elif not data:
                print("WATCH_UI_FAIL 无结果（Promise 未完成）")
            else:
                print("WATCH_UI_FAIL 失败项:", json.dumps(data.get("fail"), ensure_ascii=False))
            app.quit()

        page.loadFinished.connect(on_loaded)
        view.load(QUrl.fromLocalFile(os.path.join(tmp, "index.html")))
        QTimer.singleShot(15000, app.quit)  # 兜底
        app.exec()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
