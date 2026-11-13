# Media_Wall API 说明（v2.2.0）

本地 HTTP 服务，实现由 AV_Wall v1.9.0 泛化。默认端口 **8788**（避开 AV_Wall 的 8787，可同机共存）。

- 媒体根：`\\NAS\Share\Download`（示例；实际值由 `config.json` 的 `media_root` 决定，也可为本地盘如 `D:\Media`）。
- 分区：**媒体根下的一级子目录**（01 电影 / 10 电视节目 / … / 99 其他，示例 16 个），启动时自动发现，实际数量与名称以你的媒体根为准；`config.json` 的 `categories`（字符串数组或 `null`）可固定白名单与顺序。
- 条目（item）：分区内的「叶子内容目录」= 本层含 视频/音频/图片/文档 的目录（递归判定，深度上限 8）。

## 权限模型（与 AV_Wall 相同）

| 类别 | 允许方 | 说明 |
| --- | --- | --- |
| `GET` 全部 | 局域网任意设备 | 手机/平板浏览与播放 |
| `POST /api/scan、/api/root、/api/config、/api/shutdown` | **仅本机 127.0.0.1/::1** | 且要求同源 `Origin`、`Host` 指向本机（防 DNS 重绑定）；拒绝前先排空请求体 |

失败响应统一 403 `{"error": "..."}`；响应不携带 `Access-Control-Allow-Origin: *`，只回显请求方自身 Origin（等价于不跨域）。

## 端点

### GET /
返回程序目录 `index.html`（UTF-8，`Cache-Control: no-store`）。

### GET /mediawall/<rel> · GET /data/<rel>
程序目录静态资源白名单 `.js/.css/.json/.ico`。`config.json`、`library.meta.json` 在拒绝名单内（403）。逐段 unquote + 黑名单 + `realpath`/`commonpath` 双保险防穿越；>64MB 返回 413。

### GET /api/ping
`{"ok": true}` 健康检查。

### GET /api/library
`data/library.json` 原文；缺失返回 503。结构：

```json
{
  "version": 2,
  "root": "\\\\NAS\\Share\\Download",
  "generated": "2026-10-23T10:31:00+08:00",
  "count": 2332,
  "total_videos": 18656, "total_posters": 1572, "total_shots": 4414,
  "categories": [{"name": "01 电影", "items": 409}, ...],
  "cat_scanned": {"01 电影": "2026-10-23T10:31:00+08:00", ...},
  "items": [ { ...见下... } ],
  "warnings": []
}
```

item 字段（延续 AV_Wall 命名 + 新增）：

| 字段 | 说明 |
| --- | --- |
| `id` | 叶子目录名（展示标题） |
| `category` / `src` | 分区名（`src` 为兼容别名） |
| `path` | 分区内相对路径（`2020/片名`；文件直接在分区根时为 `""`） |
| `key` | `分区/path`（path 空时=分区名），全局唯一 |
| `poster` | 本层第一张图（mtime 升序），相对**媒体根**；无图 `null` |
| `shots` | 其余图，上限 40 |
| `videos` / `audio` | `[{path,name,size,mtime,mtime_iso}]`，path 相对媒体根 |
| `docs` | `[{name,size}]`，上限 100，不支持在线打开 |
| `vcount` | `len(videos)+len(audio)` |
| `has_video` | 有任一可播文件 |
| `kind` | `video`/`audio`/`images`/`docs`（卡片徽标与布局用） |

### GET /api/status

> v1.2.0 未改动任何 API（仅前端设置与播放交互增强，服务端仅版本号 1.1.0 → 1.2.0）。
`{"ok":true,"version":"1.2.0","root":"...","library_exists":true,"scan":{"running","ok","scanner_exists","error","category","output_tail","finished_at","progress"}}`。扫描进度轮询此端点（1s 间隔）。

`scan.progress`（v1.1.0 新增）：`{"done":2,"total":16,"category":"20 综艺","ts":"..."}` 或 `null`。
- `done/total` 以**分区**为计数单位（16 个一级分区 = total 16），`category` 为刚扫完的分区名；
- 由 `scanner.py` 写 `data/scan_progress.json`，`scan.py` 只读转出；文件不存在时 `null`；
- GUI 进度条据此换算真实百分比；浏览器端不依赖此字段（仍用 `running/finished_at`）。

### GET /api/info
局域网 IP、端口、URL 与二维码 0/1 矩阵（缺 `qrcode` 库时 `qr:null`，前端降级显示文本）。

### GET /api/categories
```json
{"ok": true, "categories": [
  {"name": "01 电影", "items": 409, "last_scan": "2026-10-23T10:31:00+08:00"},
  {"name": "94 短剧", "items": 0,   "last_scan": null}
]}
```
分区名实时扫描 NAS；`items/last_scan` 来自上次扫描快照。`items=0 且 last_scan=null` = 还没扫过（前端「待扫」虚线标签 + 空视图一键补扫按钮的数据源）。

### GET /api/root
当前媒体根 + `categories`（分区名数组）+ `default`。

### GET /api/root?path=<候选>
校验路径可用性（不修改状态）。通过：`200 {"valid":true,"categories":{"01 电影":14,...},"path":...}`；任何失败一律 `400` + 同一句文案（防目录存在性 oracle）。

### POST /api/root
`{"path": "\\\\server\\share\\dir"}`：校验 → 落盘 `config.json` → 实时生效 → 同目录仅写法不同则 patch 数据文件 root，否则后台异步全量重扫。响应含 `rescanning`、`same_directory`、`categories_found`。

### GET/POST /api/config
GET 返回 `{media_root, default_media_root, stop_mode:"manual", config:{...}}`。
POST 可写键：`media_root`（等价 /api/root）、`categories`（数组或 null；**改白名单不会自动重扫**，需要再调 /api/scan 对齐库数据）。

### POST /api/scan
异步触发扫描，立即返回 `202 {"ok":true,"started":true,"category":...}`；已在跑返回 `409`；分区名不存在返回 `400`。

- body 空/`{}` → 全部分区；
- `{"category": "01 电影"}` → 只重扫该分区，其余分区条目从缓存整体复用（不触碰 NAS）。

进度用 `GET /api/status` 轮询。CLI 等价：`python scanner.py [--quiet] [--force] [--root PATH] [--category NAME]`。

### POST /api/keepalive
**兼容占位**：200 `{"ok":true,"deprecated":true}`，什么都不做。服务只在 Ctrl+C 或 shutdown 时停止。

### POST /api/shutdown
平滑停服（仅本机）。

### POST /api/restart（v2.2.0）
网页端「重启服务」。权限：**本机 + 局域网私有网段**（手机/平板经 IP 访问也能用），
仍要求同源（`Origin` 与 `Host` 一致）；公网来源与跨站请求一律 403。
返回 `202 {"ok":true,"message":"restarting"}` 后，由服务进程自己拉起新实例，
旧进程在新实例就绪（`data/restart.ready`）后让出端口并退出；新实例带
`--wait-port` 重试绑定，因此旧进程直到确认新实例真起来才退出。
重启期间播放中的视频会中断约 1~3 秒；失败（20 秒未就绪）时旧进程回滚继续服务，
原因写入 `data/restart.log`。

### POST /api/shutdown
平滑停服（仅本机）。

### 网页端文件管理（v2.1.0）

让浏览器里的海报墙也能删/移文件。写操作统一要求 **POST + 同源**（`Origin` 与 `Host` 一致，
且 `Host` 必须是本机地址：127.0.0.1 / localhost / 本机局域网 IP），因此局域网里的手机能操作，
恶意网页的 CSRF 拿不到写权限。路径安全全部在 `mediawall/fileops.py`：
不许越出媒体根、不许删媒体根/一级分区/回收站内部、目标重名拒绝、不能移进自己内部。

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/fs/dirs?path=<相对路径>` | GET | 列出子目录（「移动到」选择器用）。返回 `{ok, path, parent, dirs:[{name,rel}], truncated}`；隐藏目录与回收站不出现；最多 500 条 |
| `/api/fs/delete` | POST | `{"path":"01 电影/片子A"}` → 移到回收站。返回 `{ok, entry, trash:{count,bytes}, message, refresh}` |
| `/api/fs/move` | POST | `{"path":"01 电影/A","dest":"01 电影/新目录"}` → 移动。同父目录、目标重名、移进自己内部都返回 400 |
| `/api/trash` | GET | 回收站列表 `{ok, items:[{id,name,orig_rel,kind,bytes,ts,note}], count, bytes}`，最新在前 |
| `/api/trash/restore` | POST | `{"id":"20261024-052231__片子A"}` → 恢复回原位置。原位置被占用则 400，不覆盖 |
| `/api/trash/empty` | POST | 清空回收站（真删除），返回 `{ok, count, bytes, errors}` |
| `/api/undo` | POST | 撤回上一次删除/移动（栈深 50，`data/undo_log.json`） |

回收站实体在**媒体根下的 `.mediawall_trash/`**：与媒体根同卷，删除/恢复都是 `move` 元数据操作，
几十 GB 的 4K 片也是秒级，不复制、不占额外空间。扫描器已把该目录加入忽略名单，不会变成条目。

成功响应里的 `refresh` 字段说明是否已触发该分区重扫（异步，进度看 `GET /api/status`）。

### GET /media/<相对路径>
视频/音频流（`VIDEO_EXTS ∪ AUDIO_EXTS`；文档/图片返回 403）。
- `Range: bytes=s-e | s- | -n` → 206 + `Content-Range`；覆盖全文件的 Range → 200；非法/越界 → 416 + `Content-Range: bytes */size`；
- 先 open 成功再发头；读不满强制关连接（防客户端永久挂死）；
- 媒体流不主动 `Connection: close`（安卓/鸿蒙媒体框架同连接连续 Range 依赖 keep-alive）；
- MIME 显式映射（mp4/m4v→video/mp4、mov→video/quicktime、mkv/avi/ts/flv/wmv、mp3/m4a/wav/flac/aac/ogg/opus/wma）；
- 1MB 分块适配 UNC/SMB；`HEAD` 支持。

### GET /thumb/<相对路径>
图片（jpg/jpeg/png/webp/gif/bmp）。`HEAD` 支持，不做 Range；>32MB 返回 413。

## file:// 离线模式

直接双击 `index.html`：数据来自 `<script src="data/library.js">` 注入的
`window.__MW_LIBRARY__`；媒体/缩略图 URL 拼 `FILE_ROOT`（library.root 的 UNC 形式）。
无服务端 → 扫描与改根不可用（页面已提示）。

## 冒烟自检

`python _scaffold\__selftest_api.py --port 8799`（在项目根目录执行；需先起一个测试端口实例）。
覆盖 35 项断言：健康/库/分区/防 oracle/Range/穿越/门禁/异步扫描/单分区扫描/停服。
基线（v1.0.0，2026-10-23）：**35 PASS / 0 FAIL**。
