# _scaffold —— 脚手架 / 自检脚本

本目录只放**开发期自检与回归脚本**，不属于主程序运行链路；`Media_Wall.py`、`server.py`、
`scanner.py`、`mediawall/`、`index.html` 仍是主程序，留在根目录。

| 脚本 | 作用 | 运行前提 | 是否会碰真实数据 |
| --- | --- | --- | --- |
| `__selftest_api.py` | HTTP API 冒烟自检（35 项：健康/库/分区/流式 Range/路径安全/扫描/关机） | 先起一个测试端口实例 | 只读真实库；扫描复用缓存 |
| `__e2e_test.py` | GUI 服务管理器 + 服务端端到端 | 无 | **否**（全程临时副本 + 临时媒体树） |
| `__smoke_gui.py` | GUI 窗口构建冒烟（offscreen） | 无 | 否（不启动服务） |
| `__ui_test.py` | 前端 UI 无头回归（QtWebEngine 真实渲染） | 无 | 否（临时副本 + 桩库） |
| `_test_fileops.py` | 回收站/恢复/移动/撤回/安全边界/历史裁剪单测（33 项） | 无 | **否**（全程临时目录） |
| `_test_fsweb.py` | 网页端文件管理验收：临时起服务 + 真实 HTTP，覆盖列目录/删除/回收站/恢复/撤回/移动/保护目录/CSRF/405 共 23 项 | 无 | 否（临时媒体树，不碰 NAS 与 data/） |
| `_test_hooks.py` | 历史埋点（真起服务 + 真 HTTP GET / 与 Range /media） | 无 | 否（临时副本 + 临时媒体树） |
| `_test_longpath.py` | 长路径（>260 字符）stat/open 回归：样本 269 字符必须可读、短路径行为不变、不存在的长路径仍抛错 | 无 | 否（只读 NAS 上已收录的样本文件） |
| `_test_longmedia.py` | 超长路径视频的 HTTP Range 实测：临时起服务对 248 字符相对路径发 `Range: bytes=0-1023`，必须 206 + 1024 字节 | 无 | 否（只读；服务用随机端口，结束 shutdown） |
| `_diag_unlisted_videos.py` | 诊断：遍历媒体根，找出「本层有视频但库里一条都没收录」的目录 | 无 | 否（只读，约 75–220s） |
| `_verify_exe.py` | 编译产物验收（`Media_Wall.exe --serve` 起服务、版本/PAGE_VER、shutdown 释放端口、GUI 12s 无崩溃） | 先跑 `0_nuitka_pyside6_clang_auto.bat` | 否（临时媒体根；结束清理 dist 里自动生成的 data/config） |
| `_verify_ui_fix.py` | 三视口 UI 验收：按钮可达、目录条可滚、设置/卡片菜单真实生效 | `python _scaffold/_verify_ui_fix.py` |
| `_real_click_test.py` | 用真实鼠标事件（CDP Input）测按键，排除 `el.click()` 假象 | `python _scaffold/_real_click_test.py` |
| `_probe_ui.py` | CDP 公共框架（headless Edge + websocket），被上面脚本 import | 不单独运行 |
| `_measure_topbar.py` | 多视口测量顶栏各组件宽度 | `python _scaffold/_measure_topbar.py` |
| `_diag_ui_broken.py` | 逐函数 try/catch 定位前端抛错点 | `python _scaffold/_diag_ui_broken.py` |
| `_diag_http_ui.py` | HTTP + 手机视口复现并抓早期 JS 异常 | `python _scaffold/_diag_http_ui.py` |
| `_test_restart.py` | 重启服务端到端：真实进程 + HTTP，验证旧进程退出/新进程接管/跨站 403/权限单元 | 无 | 否（临时副本 + 临时媒体树） |
| `_test_gui_adopt.py` | GUI 探活容错：旧子进程退出 + 新进程接管 → 状态保持运行中（不误判 stopped） | 无 | 否（临时副本 + 临时端口） |
| `_test_win_size.py` | 窗口大小记忆：默认/resize 落盘/下次恢复/夹取/损坏兜底 | 无 | 否（offscreen + 临时设置） |

## 运行方式

在**项目根目录**执行（脚本内部会把项目根加入 `sys.path`）：

```bat
python _scaffold\__selftest_api.py --port 8799
python _scaffold\__e2e_test.py
python _scaffold\__smoke_gui.py
python _scaffold\__ui_test.py
python _scaffold\_test_fileops.py
python _scaffold\_test_fsweb.py
python _scaffold\_test_hooks.py
python _scaffold\_test_longpath.py
python _scaffold\_test_longmedia.py
python _scaffold\_test_restart.py
python _scaffold\_verify_exe.py
python _scaffold\_diag_unlisted_videos.py
```

通过标志：`结果: PASS 36 / FAIL 0`、`E2E_OK`、`SMOKE_OK`、`UI_SMOKE_OK`、
`FILEOPS_OK`、`FSWEB_OK`、`HOOKS_OK`、`EXE_OK`、`LONGPATH_MEDIA_OK`、`RESTART_E2E_OK`、`GUI_ADOPT_OK`、`WIN_SIZE_OK`。

## 约定

- 脚本文件名以 `__` 开头（历史惯例），便于与主程序文件区分；
- 需要临时文件的一律用 `tempfile.mkdtemp()`，结束即清理，不在根目录留垃圾；
- 想新增脚手架文件，放这里，不要再丢到根目录。

## 排障

**UI 自检无任何输出、只跑满 60s 就退出**：`loadFinished` 没触发，QtWebEngine 的渲染
进程没起来。先看有没有残留 `QtWebEngineProcess.exe`（`Get-Process QtWebEngineProcess`
→ `Stop-Process -Force`），仍不行就重启一次——本机曾出现「连 `view.setHtml()` 都不触发
`loadFinished`」的环境态异常，重启后恢复。脚本已改为每次用独立 `--user-data-dir`
临时 profile，减少这类互相踩踏。
