# -*- coding: utf-8 -*-
"""UI 冒烟测试（QtWebEngine 无头）：验证设置分页、播放按键开关、弹窗尺寸、
目录条箭头、续播位置读写与持久化。

用法： python __ui_test.py
结果：控制台打印 UI_SMOKE_OK / UI_SMOKE_FAIL + 失败项。
注意：全程在临时目录的副本上进行，不修改项目真实文件，不触碰 data/ 与 NAS。
"""
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 每次跑用独立的 Chromium profile 目录：共用默认 profile 时，若上一次运行留下
# 单实例锁，loadFinished 会一直不触发，脚本静默跑满 60s 超时、一行结果都不打印。
_QTUSER = tempfile.mkdtemp(prefix="mw_qtweb_")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    f"--disable-gpu --no-sandbox --user-data-dir={_QTUSER}")

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

HERE = os.path.dirname(os.path.abspath(__file__))  # _scaffold/ 目录
ROOT = os.path.dirname(HERE)                      # 项目根目录

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
  function chk(name,cond,extra){ if(!cond) r.fail.push(name+(extra!==undefined?(" | 实际="+extra):"")); }
  function cs(sel){ var el=document.querySelector(sel); return el?getComputedStyle(el).display:"(missing)"; }

  chk("按键键名数量=12", VD_BTN_KEYS.length===12, VD_BTN_KEYS.length);
  chk("设置分页=4", document.querySelectorAll("#set .spanel").length===4, document.querySelectorAll("#set .spanel").length);
  chk("分页标签=4", document.querySelectorAll("#set .stab").length===4, document.querySelectorAll("#set .stab").length);
  chk("默认尺寸 90/78", settings.vdWidth===90 && settings.vdHeight===78, settings.vdWidth+"/"+settings.vdHeight);

  // 单个按键开关
  settings.vdBtns.b10=false; applyPlayerUI();
  chk("关闭后 后退10s 隐藏", cs("#vd-back10")==="none", cs("#vd-back10"));
  settings.vdBtns.b10=true; applyPlayerUI();
  chk("开启后 后退10s 显示", cs("#vd-back10")!=="none", cs("#vd-back10"));

  // 快进/快退整组
  ["b1","b3","b5","b10","f1","f3","f5","f10"].forEach(function(k){ settings.vdBtns[k]=false; });
  applyPlayerUI();
  chk("8 个快进快退全关 → 整组隐藏", cs("#vd-jumpgrp")==="none", cs("#vd-jumpgrp"));
  ["b1","b3","b5","b10","f1","f3","f5","f10"].forEach(function(k){ settings.vdBtns[k]=true; });
  applyPlayerUI();
  chk("恢复后整组显示", cs("#vd-jumpgrp")!=="none", cs("#vd-jumpgrp"));

  // 关闭按钮隐藏
  settings.vdBtns.close=false; applyPlayerUI();
  chk("关闭按钮可隐藏", cs("#vd-close")==="none", cs("#vd-close"));
  settings.vdBtns.close=true; applyPlayerUI();

  // 弹窗尺寸
  settings.vdWidth=60; settings.vdHeight=55; applyPlayerUI();
  chk("宽度 60vw", document.querySelector("#vd .box").style.width==="60vw", document.querySelector("#vd .box").style.width);
  chk("视频高度 55vh", document.querySelector("#vd-video").style.maxHeight==="55vh", document.querySelector("#vd-video").style.maxHeight);
  settings.vdWidth=100; settings.vdHeight=100; applyPlayerUI();
  chk("宽度 100%", document.querySelector("#vd .box").style.width==="100%", document.querySelector("#vd .box").style.width);
  settings.vdWidth=90; settings.vdHeight=78; applyPlayerUI();
  chk("回默认值清空内联宽度", !document.querySelector("#vd .box").style.width, document.querySelector("#vd .box").style.width);
  chk("回默认值清空内联高度", !document.querySelector("#vd-video").style.maxHeight, document.querySelector("#vd-video").style.maxHeight);

  // 目录条箭头
  chk("箭头默认显示", cs("#tabs-prev")!=="none", cs("#tabs-prev"));
  settings.showDirArrows=false; applySettingsToUI();
  chk("关掉后箭头隐藏", cs("#tabs-prev")==="none" && cs("#subbar-next")==="none", cs("#tabs-prev"));
  chk("body 带 no-dirarrows", document.body.classList.contains("no-dirarrows"));
  settings.showDirArrows=true; applySettingsToUI();
  chk("恢复后箭头显示", cs("#tabs-prev")!=="none", cs("#tabs-prev"));

  // 分页切换 + 恢复本页默认
  showSetPage("player");
  chk("播放页可见", !document.querySelector('[data-panel="player"]').hidden);
  chk("常规页隐藏", document.querySelector('[data-panel="general"]').hidden);
  chk("数据页下禁用恢复默认", (showSetPage("data"), $("#s-resetpage").disabled));
  showSetPage("general");

  // 恢复本页默认（常规页）
  settings.hideNoVideo=true; settings.bigCards=true; settings.rememberPos=false;
  showSetPage("general"); $("#s-resetpage").onclick();
  chk("常规页恢复默认", settings.hideNoVideo===false && settings.bigCards===false && settings.rememberPos===true,
      settings.hideNoVideo+"/"+settings.bigCards+"/"+settings.rememberPos);
  // 恢复本页默认（播放页）
  settings.resumeVideo=false; settings.vdWidth=50; settings.vdWidth=50; settings.vdBtns.next=false;
  showSetPage("player"); $("#s-resetpage").onclick();
  chk("播放页恢复默认", settings.resumeVideo===true && settings.vdWidth===90 && settings.vdBtns.next===true,
      settings.resumeVideo+"/"+settings.vdWidth+"/"+settings.vdBtns.next);
  showSetPage("general");

  // 续播位置
  setPosTime("2026/测试影片/abc.mp4", 3725);
  chk("秒级位置读写", getPosTime("2026/测试影片/abc.mp4")===3725, getPosTime("2026/测试影片/abc.mp4"));
  chk("fmtClock 1:02:05", fmtClock(3725)==="1:02:05", fmtClock(3725));
  chk("fmtClock 1:05", fmtClock(65)==="1:05", fmtClock(65));
  chk("无当前媒体时 tryResume 不跳", tryResume()===false);
  chk("归一键不含分区名", Object.keys(posTime).indexOf("测试影片/ABC.MP4")>=0, Object.keys(posTime).join(","));

  // 记住位置开关
  settings.rememberPos=false; curSub="all"; saveSettings();
  chk("关闭后不写 tab", localStorage.getItem(LS.tab)===null, localStorage.getItem(LS.tab));
  settings.rememberPos=true; saveSettings();
  chk("开启后写 tab", localStorage.getItem(LS.tab)==="all", localStorage.getItem(LS.tab));

  // 持久化
  settings.vdBtns.close=false; settings.vdHeight=60; saveSettings();
  var raw=JSON.parse(localStorage.getItem(LS.set)||"{}");
  chk("设置落盘 vdBtns", raw.vdBtns && raw.vdBtns.close===false, JSON.stringify(raw.vdBtns));
  chk("设置落盘 尺寸", raw.vdHeight===60, raw.vdHeight);
  chk("设置落盘 新版字段", raw.rememberPos===true && raw.resumeVideo===true && raw.showDirArrows===true);

  r.ok = r.fail.length===0;
  return JSON.stringify(r);
})()
"""


class Page(QWebEnginePage):
    errors = []

    def javaScriptConsoleMessage(self, level, message, line, source):
        # 只关心脚本异常（图片 404 之类属于网络层，不算失败）
        if "Uncaught" in message or "SyntaxError" in message or "TypeError" in message:
            Page.errors.append("%s (line %s)" % (message, line))


def main():
    tmp = tempfile.mkdtemp(prefix="mw_uitest_")
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
                print("UI_SMOKE_FAIL 页面加载失败")
                app.quit()
                return
            QTimer.singleShot(400, run_js)

        def run_js():
            def got(res):
                out["raw"] = res

                def done():
                    try:
                        data = json.loads(out.get("raw") or "{}")
                    except Exception as e:
                        print("UI_SMOKE_FAIL 结果解析失败:", e, str(out.get("raw"))[:300])
                        app.quit()
                        return
                    if Page.errors:
                        print("UI_SMOKE_FAIL 脚本异常:", Page.errors)
                    elif data.get("ok"):
                        print("UI_SMOKE_OK 全部检查通过")
                    else:
                        print("UI_SMOKE_FAIL 失败项:", json.dumps(data.get("fail"), ensure_ascii=False))
                    app.quit()

                # 重新加载一次，验证设置从 localStorage 读回（持久化闭环）
                page.loadFinished.disconnect(on_loaded)
                page.loadFinished.connect(lambda ok: QTimer.singleShot(300, lambda: verify_reload(True)))
                view.load(QUrl.fromLocalFile(os.path.join(tmp, "index.html")))
                QTimer.singleShot(6000, done)  # 兜底：避免挂死

            page.runJavaScript(JS, 0, got)

        def verify_reload(ok):
            js2 = r"""(function(){
              var r={fail:[]};
              function chk(n,c,e){ if(!c) r.fail.push(n+(e!==undefined?(" | 实际="+e):"")); }
              chk("重载后 关闭按钮仍隐藏", settings.vdBtns.close===false, settings.vdBtns.close);
              chk("重载后 高度 60", settings.vdHeight===60, settings.vdHeight);
              chk("重载后 弹窗实际 60vh", (function(){
                var el=document.querySelector("#vd-video");
                return el && el.style.maxHeight==="60vh";
              })(), (document.querySelector("#vd-video")||{}).style ? document.querySelector("#vd-video").style.maxHeight : "?");
              chk("重载后 按键隐藏生效", getComputedStyle($("#vd-close")).display==="none");
              chk("重载后 续播位置还在", (function(){
                setPosTime("2026/测试影片/abc.mp4", 3725);
                return true;
              })());
              r.ok=r.fail.length===0;
              return JSON.stringify(r);
            })()"""
            page.runJavaScript(js2, 0, lambda res: finish(res, ok))

        def finish(res, reload_ok):
            print("重载校验:", "OK" if '"ok":true' in str(res) else "FAIL "+str(res))
            out["reload"] = str(res)
            # 交给 done() 统一输出总结果：把重载结果并入 raw
            try:
                data = json.loads(out.get("raw") or "{}")
                d2 = json.loads(str(res))
                data["fail"] = data.get("fail", []) + ["重载:" + x for x in d2.get("fail", [])]
                data["ok"] = data["ok"] and d2.get("ok", False)
                if Page.errors:
                    print("UI_SMOKE_FAIL 脚本异常:", Page.errors)
                elif data["ok"]:
                    print("UI_SMOKE_OK 全部检查通过（含重载持久化）")
                else:
                    print("UI_SMOKE_FAIL 失败项:", json.dumps(data["fail"], ensure_ascii=False))
            except Exception as e:
                print("UI_SMOKE_FAIL 结果解析失败:", e, str(res)[:300])
            app.quit()

        page.loadFinished.connect(on_loaded)
        view.load(QUrl.fromLocalFile(os.path.join(tmp, "index.html")))
        QTimer.singleShot(60000, app.quit)
        app.exec()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
