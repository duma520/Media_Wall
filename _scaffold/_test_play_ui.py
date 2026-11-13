# -*- coding: utf-8 -*-
"""v2.9.0 前端播放路由冒烟（QtWebEngine 无头）：扩展名分组、模式切换、转换条、音乐模式。"""
import json, os, shutil, sys, tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_QTUSER = tempfile.mkdtemp(prefix="mw_playui_")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
    f"--disable-gpu --no-sandbox --user-data-dir={_QTUSER}")

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LIB_STUB = """window.__MW_LIBRARY__ = {
  generated:"2026-10-30T12:00:00", root:"", count:1, total_videos:1, total_posters:1, total_shots:0,
  cat_scanned:{"01 \u7535\u5f71":true},
  categories:[{name:"01 \u7535\u5f71",items:1,last_scan:"2026-10-30T02:00:00"}],
  items:[{key:"01 \u7535\u5f71/\u6d4b\u8bd5\u5f71\u7247", id:"\u6d4b\u8bd5\u5f71\u7247", path:"2026/\u6d4b\u8bd5\u5f71\u7247",
    category:"01 \u7535\u5f71", kind:"video", has_video:true, poster:"2026/\u6d4b\u8bd5\u5f71\u7247/poster.jpg",
    videos:[{name:"abc.mp4", path:"2026/\u6d4b\u8bd5\u5f71\u7247/abc.mp4", size:12345}], shots:[], docs:[]}]
};
"""

JS = r"""
(function(){
  var r={fail:[]};
  function chk(n,c,e){ if(!c) r.fail.push(n+(e!==undefined?(" | 实际="+e):"")); }

  // ---- 扩展名解析 ----
  chk("extOf ts", extOf("a/b/c.TS")==="ts", extOf("a/b/c.TS"));
  chk("extOf 无扩展名", extOf("noext")==="", extOf("noext"));
  chk("isAudioExt mp3", isAudioExt("x.mp3")===true);
  chk("isAudioExt wma", isAudioExt("x.WMA")===true);
  chk("isAudioExt flac", isAudioExt("x.flac")===true);
  chk("isAudioExt mp4=false", isAudioExt("x.mp4")===false);
  chk("isAudioExt mkv=false", isAudioExt("x.mkv")===false);

  // ---- 分组表 ----
  chk("CONV_DIRECT 含 mp4", CONV_DIRECT.indexOf("mp4")>=0);
  chk("CONV_DIRECT 含 mp3", CONV_DIRECT.indexOf("mp3")>=0);
  chk("CONV_TRYDIRECT 含 mkv", CONV_TRYDIRECT.indexOf("mkv")>=0);
  chk("CONV_TRYDIRECT 含 avi", CONV_TRYDIRECT.indexOf("avi")>=0);
  chk("ts 不在 DIRECT", CONV_DIRECT.indexOf("ts")<0);
  chk("ts 不在 TRYDIRECT", CONV_TRYDIRECT.indexOf("ts")<0);
  chk("wma 不在 DIRECT", CONV_DIRECT.indexOf("wma")<0);

  // ---- 元素存在 ----
  chk("转换条存在", !!document.getElementById("vd-conv"));
  chk("音乐容器存在", !!document.getElementById("vd-audio"));
  chk("音乐 audio 元素", !!document.getElementById("vd-aud-src"));
  chk("音乐封面 img", !!document.getElementById("vd-aud-img"));
  chk("音乐占位 ph", !!document.getElementById("vd-aud-ph"));
  chk("音乐标题", !!document.getElementById("vd-aud-name"));
  chk("清缓存按钮", !!document.getElementById("s-clearcache"));

  // ---- 函数存在 ----
  ["routePlayback","activeMedia","setPlayerMode","setAudioCover","playInfo",
   "directSrc","fmtFor","convSrc","showConv","setConvPct","hideConv","convAbort",
   "pollConv","tryConvertFallback","clearPlayCache"].forEach(function(f){
    chk("函数 "+f, typeof window[f]==="function");
  });

  // ---- 模式切换 ----
  setPlayerMode(true);
  chk("音乐模式容器可见", document.getElementById("vd-audio").classList.contains("on"));
  chk("音乐模式 video 隐藏", document.getElementById("vd-video").style.display==="none");
  setPlayerMode(false);
  chk("视频模式容器隐藏", !document.getElementById("vd-audio").classList.contains("on"));
  chk("视频模式 video 可见", document.getElementById("vd-video").style.display!=="none");

  // ---- activeMedia 双通道 ----
  setPlayerMode(true);
  chk("activeMedia 音乐模式返回 audio",
      activeMedia()===document.getElementById("vd-aud-src"));
  setPlayerMode(false);
  chk("activeMedia 视频模式返回 video",
      activeMedia()===document.getElementById("vd-video"));

  // ---- 转换条显示/进度/隐藏 ----
  showConv("准备中…");
  chk("转换条显示", document.getElementById("vd-conv").classList.contains("on"));
  setConvPct(42);
  var bar=document.querySelector("#vd-conv .cvbar i");
  chk("进度条 42%", bar.style.width==="42%", bar.style.width);
  chk("进度文字 42%", document.querySelector("#vd-conv .cvp").textContent==="42%",
      document.querySelector("#vd-conv .cvp").textContent);
  hideConv();
  chk("转换条隐藏", !document.getElementById("vd-conv").classList.contains("on"));

  // ---- 目标格式判定 ----
  chk("fmtFor 视频=mp4", fmtFor("a/b.ts")==="mp4", fmtFor("a/b.ts"));
  chk("fmtFor 音频=mp3", fmtFor("a/b.wma")==="mp3", fmtFor("a/b.wma"));

  // ---- 封面：有海报用 img，无海报用占位 ----
  setAudioCover({poster:"2026/x/poster.jpg"});
  chk("有海报 img 显示", document.getElementById("vd-aud-img").style.display!=="none");
  chk("有海报 ph 隐藏", document.getElementById("vd-aud-ph").style.display==="none");
  setAudioCover({});
  chk("无海报 img 隐藏", document.getElementById("vd-aud-img").style.display==="none");
  chk("无海报 ph 显示", document.getElementById("vd-aud-ph").style.display==="flex");

  // ---- 自动回退只触发一次（注意：file:// 下函数本身返回 false，属设计行为）----
  _fallbackTried=false;
  if(location.protocol==="file:"){
    chk("file:// 下不回退（设计行为）", tryConvertFallback()===false);
  }else{
    chk("回退首次可尝试", (function(){
      var called=false; var old=window.playInfo;
      window.playInfo=function(rel,b,cb){ called=true; cb(null); };
      var r=tryConvertFallback(); window.playInfo=old; return r===true && called;
    })());
    chk("回退二次被拦", tryConvertFallback()===false);
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
    tmp = tempfile.mkdtemp(prefix="mw_playui_")
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
                print("PLAYUI_FAIL 页面加载失败"); app.quit(); return
            QTimer.singleShot(500, run_js)
        def run_js():
            def done():
                try:
                    data = json.loads(out.get("raw") or "{}")
                except Exception as e:
                    print("PLAYUI_FAIL 结果解析失败:", e, str(out.get("raw"))[:300]); app.quit(); return
                if Page.errors:
                    print("PLAYUI_FAIL 脚本异常:", Page.errors)
                elif data.get("ok"):
                    print("PLAYUI_OK 全部检查通过")
                else:
                    print("PLAYUI_FAIL 失败项:", json.dumps(data.get("fail"), ensure_ascii=False))
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
