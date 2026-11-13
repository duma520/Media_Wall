"""给 GitHub 截真实运行画面（网页端用 Edge/CDP，控制面板用 PySide6 离屏）。

原则
----
1. **真实截图，不是示意图**：启动真实 HTTP 服务、真实浏览器渲染，不画 mock。
2. **不泄露隐私**：媒体根指向 `_screenshot_work/media`（程序化生成的占位海报与
   中性假片名），绝不连用户真实 NAS；截图前再扫一遍敏感串做兜底。
3. 分辨率够看：网页 1440 宽（桌面）/ 420 宽（手机）；控制面板按其真实窗口尺寸。

产物：`docs/screenshots/*.png`

用法：python _scaffold/_capture_screenshots.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
WORK = PROG / "_screenshot_work"
PROG_COPY = WORK / "prog"
MEDIA = WORK / "media"
OUT = PROG / "docs" / "screenshots"

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 8787   # 用默认端口，截图里「主服务端口」写死 8787，端口不一致会显得像 bug
CDP_PORT = 9334


# ---------------------------------------------------------------- 服务
def free_port(start: int) -> int:
    for p in range(start, start + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("没有空闲端口")


DEMO_LAN_IP = "192.168.1.100"
_DEMO_MARK = "# --- 截图专用：强制演示用局域网 IP ---"


def patch_demo_lan_ip() -> None:
    """把**程序副本**里的 detect_lan_ip 改成固定返回演示地址。

    为什么必须这么做：截图要公开发布，而 ``/api/info`` 会把真实内网 IP 送进页面
    （``server.html`` 的「本机」字段与二维码都基于它）。只在浏览器里改 DOM 不够——
    二维码图片是服务端用真实 IP 生成的，会一起泄露。

    因此直接改 ``_screenshot_work/prog/`` 这份**临时副本**的模块：在文件末尾追加
    一个同名函数覆盖掉原实现。真正的 ``mediawall/common.py`` 一个字节都不动。
    注意 ``httpd.py`` 是 ``from mediawall.common import detect_lan_ip`` 导入的，
    只要覆盖发生在 common 模块执行完之后（追加在文件末尾即可），httpd 拿到的就是
    覆盖后的版本。
    """
    target = PROG_COPY / "mediawall" / "common.py"
    text = target.read_text(encoding="utf-8")
    if _DEMO_MARK in text:
        return
    text += (
        "\n\n" + _DEMO_MARK + "\n"
        "# 仅在 _scaffold/_capture_screenshots.py 产出的临时副本里存在，用于截图脱敏。\n"
        "def detect_lan_ip() -> str:  # noqa: F811\n"
        '    """截图专用：返回演示地址，避免把真实内网 IP 画进公开截图。"""\n'
        '    return "%s"\n' % DEMO_LAN_IP
    )
    target.write_text(text, encoding="utf-8", newline="\n")
    print("  [脱敏] 副本 detect_lan_ip → %s" % DEMO_LAN_IP)


def start_server(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROG_COPY)
    # 必须同时给 --port-max：默认值是 DEFAULT_PORT+20（=8807），
    # 若只传 --port 8866，端口上限比起始端口还小，绑定循环一次都不执行，
    # 服务直接打印「端口 8866~8807 均被占用」退出。这个坑实测踩过。
    proc = subprocess.Popen(
        [sys.executable, str(PROG_COPY / "server.py"),
         "--host", "127.0.0.1", "--port", str(port),
         "--port-max", str(port + 20),
         "--root", str(MEDIA), "--no-browser"],
        cwd=str(PROG_COPY), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    for _ in range(80):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
                if r.status == 200:
                    return proc
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("服务未起来")


# ---------------------------------------------------------------- 浏览器
def start_edge(url: str):
    profile = str(WORK / "edge_profile")
    if Path(profile).exists():
        shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--disable-extensions", "--hide-scrollbars",
         "--force-device-scale-factor=2",          # 2x，Retina 清晰度
         "--remote-allow-origins=*",
         "--remote-debugging-port=%d" % CDP_PORT,
         "--user-data-dir=" + profile, url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(120):
        try:
            targets = json.load(urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % CDP_PORT, timeout=1))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return proc, pages[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError("Edge CDP 未就绪")


class CDP:
    def __init__(self, url: str):
        import websocket
        self.ws = websocket.create_connection(url, timeout=180)
        self.mid = 0

    def call(self, method: str, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True, userGesture=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError("JS 异常: %s" % ((d.get("exception") or {}).get("description") or d.get("text")))
        return r.get("result", {}).get("value")

    def viewport(self, w: int, h: int, mobile: bool = False):
        self.call("Emulation.setDeviceMetricsOverride",
                  width=w, height=h, deviceScaleFactor=2, mobile=mobile)

    def wait_cards(self, timeout: int = 90) -> int:
        t0 = time.time()
        while time.time() - t0 < timeout:
            n = self.js("document.querySelectorAll('.card').length")
            if isinstance(n, int) and n > 0:
                return n
            time.sleep(0.8)
        return 0

    def shot(self, path: Path, full: bool = False):
        params = {"format": "png", "captureBeyondViewport": full}
        if full:
            m = self.call("Page.getLayoutMetrics")
            cs = m.get("cssContentSize") or m.get("contentSize")
            params["clip"] = {"x": 0, "y": 0,
                              "width": min(cs["width"], 1600),
                              "height": min(cs["height"], 4000),
                              "scale": 2}
        data = self.call("Page.captureScreenshot", **params)["data"]
        import base64
        path.write_bytes(base64.b64decode(data))


# 等图片全部解码完成，避免截到灰白占位
WAIT_IMAGES = r"""
(async function(){
  var imgs = Array.from(document.images);
  await Promise.all(imgs.map(function(im){
    if (im.complete) return null;
    return new Promise(function(res){ im.onload = im.onerror = res; setTimeout(res, 4000); });
  }));
  await new Promise(function(r){ setTimeout(r, 600); });
  var broken = imgs.filter(function(im){ return im.complete && im.naturalWidth === 0; }).length;
  return {total: imgs.length, broken: broken};
})()
"""


def capture_web(port: int) -> None:
    base = "http://127.0.0.1:%d/" % port
    edge, ws = start_edge(base)
    try:
        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")

        # --- 1) 桌面端海报墙主图
        cdp.viewport(1440, 900)
        n = cdp.wait_cards()
        print("  卡片数:", n)
        info = cdp.js(WAIT_IMAGES)
        print("  图片:", info)
        cdp.js("window.scrollTo(0,0)")
        time.sleep(0.8)
        cdp.shot(OUT / "01-poster-wall.png")
        print("  → 01-poster-wall.png")

        # --- 2) 大图模式（宽卡三列），更能看出海报效果
        # 注意：页面上没有「大图模式」独立按钮，它是设置面板里的复选框 #s-bigcards。
        # 直接改 settings 变量不会重渲染，必须走控件 + 触发 change 事件让 bindSettings 生效。
        cdp.js("""
          (function(){
            var d=document.getElementById('set');
            if(d && !d.open) document.getElementById('btn-set').click();
            return d ? d.open : null;
          })()
        """)
        time.sleep(0.8)
        big = cdp.js("""
          (function(){
            var cb=document.getElementById('s-bigcards');
            if(!cb) return 'no-checkbox';
            cb.checked = true;
            cb.dispatchEvent(new Event('change', {bubbles:true}));
            return 'ok:'+cb.checked;
          })()
        """)
        print("  大图模式:", big)
        cdp.js("""
          (function(){
            var d=document.getElementById('set');
            if(d && d.open) d.close();
            return 1;
          })()
        """)
        time.sleep(1.5)
        info = cdp.js(WAIT_IMAGES)
        print("  大图模式图片:", info)
        cdp.js("window.scrollTo(0,0)")
        time.sleep(0.6)
        cdp.shot(OUT / "02-poster-wall-big.png")
        print("  → 02-poster-wall-big.png")

        # 切回窄卡，避免后续截图受大图模式影响
        cdp.js("""
          (function(){
            document.getElementById('btn-set').click();
            var cb=document.getElementById('s-bigcards');
            if(cb){ cb.checked=false; cb.dispatchEvent(new Event('change',{bubbles:true})); }
            var d=document.getElementById('set');
            if(d && d.open) d.close();
            return 1;
          })()
        """)
        time.sleep(1.0)

        # --- 3) 设置面板
        cdp.js("document.getElementById('btn-set').click()")
        time.sleep(1.0)
        cdp.shot(OUT / "03-settings.png")
        print("  → 03-settings.png")
        cdp.js("""
          (function(){
            var d=document.getElementById('set');
            if(d && d.open) d.close();
            return 1;
          })()
        """)
        time.sleep(0.5)

        # --- 4) 手机竖屏
        cdp.viewport(414, 860, mobile=True)
        time.sleep(1.2)
        info = cdp.js(WAIT_IMAGES)
        print("  手机端图片:", info)
        cdp.shot(OUT / "04-mobile.png")
        print("  → 04-mobile.png")

        # --- 5) 服务控制页
        cdp.viewport(1280, 800)
        cdp.call("Page.navigate", url=base + "server.html")
        time.sleep(2.5)
        cdp.shot(OUT / "05-server-page.png")
        print("  → 05-server-page.png")
    finally:
        try:
            edge.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------- 控制面板
def capture_gui() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Qt 离屏模式（offscreen 插件）不会自动读 Windows 系统字体，中文会渲染成方框（豆腐块）。
    # 正确的开关是 QT_QPA_FONTDIR 环境变量，必须在 QApplication 创建**之前**设置。
    # 往 PySide6/Qt/lib/fonts 里拷文件是无效的（实测 families() 仍为 0）。
    os.environ["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"
    sys.path.insert(0, str(PROG_COPY))

    from PySide6.QtGui import QFontDatabase, QIcon
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    fams = QFontDatabase.families()
    zh = [f for f in fams if any(k in f for k in ("Noto", "Deng", "SimHei", "黑体", "雅黑"))]
    print("  可用字体族 %d 个，中文示例: %s" % (len(fams), zh[:4]))
    if not fams:
        raise RuntimeError("Qt 仍未加载到任何字体，中文会显示成方框，截图无效")
    icon = PROG_COPY / "icon.ico"
    if icon.is_file():
        QApplication.setWindowIcon(QIcon(str(icon)))

    from gui_window import MainWindow
    import gui_window as _gw

    # 截图会公开到 GitHub：局域网 IP 不能出现本机真实内网地址。
    # 临时把 detect_lan_ip 换成演示地址（只影响本进程，不改源码与真实配置）。
    _gw.detect_lan_ip = lambda: "192.168.1.100"

    win = MainWindow(port=8787, host="0.0.0.0", autostart=False)
    win._lan_ip = "192.168.1.100"     # 已算好的属性也覆盖一遍
    win.resize(960, 660)
    win.show()
    for _ in range(12):
        app.processEvents()
        time.sleep(0.08)

    # 先把服务真起来，否则概览页显示「已停止」、端口/媒体根全是「—」，不像在用的软件
    print("  启动服务以便截图显示运行态…")
    win._start_service()
    for _ in range(120):              # 最多等 24 秒
        app.processEvents()
        if win.service.running:
            break
        time.sleep(0.2)
    for _ in range(40):               # 再等状态轮询回填版本号/媒体根
        app.processEvents()
        time.sleep(0.1)
    print("  服务运行中:", win.service.running)

    # 概览页
    win.tabs.setCurrentIndex(0)
    for _ in range(8):
        app.processEvents()
        time.sleep(0.06)
    # 媒体根显示的是本机临时路径（G:\...\_screenshot_work\media），对公开截图而言是噪音，
    # 换成一个通用的 NAS 示例路径；只改界面文字，不动任何配置。
    win._words["root"].setText(r"\\NAS\Share\Download")
    app.processEvents()
    pm = win.grab()
    pm.save(str(OUT / "06-control-panel.png"), "PNG")
    print("  → 06-control-panel.png", pm.size().width(), "x", pm.size().height())

    # 设置页
    win.tabs.setCurrentWidget(win.settings_page)
    for _ in range(8):
        app.processEvents()
        time.sleep(0.06)
    # 媒体根输入框同理：真实值是本机临时目录，换成 NAS 示例路径（只改控件文字，不改配置）
    try:
        win.settings_page.edit_root.setText(r"\\NAS\Share\Download")
        app.processEvents()
    except Exception as exc:            # noqa: BLE001
        print("  [提示] 设置页媒体根文字未替换:", exc)
    pm = win.grab()
    pm.save(str(OUT / "07-control-panel-settings.png"), "PNG")
    print("  → 07-control-panel-settings.png", pm.size().width(), "x", pm.size().height())

    win.close()
    app.processEvents()

    # 关窗后服务可能还在跑，显式停掉，避免残留进程占端口
    try:
        if win.service.running:
            win.service.stop()
            for _ in range(30):
                if not win.service.running:
                    break
                time.sleep(0.1)
    except Exception as exc:            # noqa: BLE001
        print("  [提示] 停止服务时出错:", exc)


def main() -> int:
    if not PROG_COPY.is_dir():
        print("先跑 _scaffold/_make_screenshot_library.py 生成假媒体库")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    port = free_port(PORT)
    print("[1/3] 启动服务，端口 %d（媒体根 = 占位素材）" % port)
    # 先给副本打上「演示 IP」补丁，再起服务——否则 server.html 会显示真实内网 IP
    patch_demo_lan_ip()
    srv = start_server(port)
    try:
        print("[2/3] 网页端截图（Edge CDP，2x 缩放）")
        capture_web(port)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=8)
        except Exception:
            srv.kill()

    print("[3/3] 控制面板截图（PySide6 离屏）")
    capture_gui()

    files = sorted(OUT.glob("*.png"))
    print("\n产物 %d 张：" % len(files))
    for f in files:
        print("  %-34s %8d 字节" % (f.name, f.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
