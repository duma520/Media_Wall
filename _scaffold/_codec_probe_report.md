# Media_Wall 编码探测报告（ffprobe 抽样 + 浏览器真实解码验证）

- 生成时间：2026-10-30
- 探测对象：`G:\Python_Code\Media_Wall\data\library.json`（只读，未修改）
- 媒体根：`\\NAS\Share\Download`（UNC 示例）
- 工具：ffprobe / ffmpeg 8.0.1-full_build-www.gyan.dev（`D:\ffmpeg\bin\`）
- 浏览器验证：Microsoft Edge 156.0.0.0（Headless，`HeadlessChrome/156.0.0.0`）
- 性质：**只读探测**。项目源码零改动，`data/library.json` 零改动，未跑扫描，未启停任何服务，`version/` 未触碰。

---

## 0. 先说结论（TL;DR）

| 问题 | 结论 |
|---|---|
| remux（`-c copy` 换容器）能救哪些？ | **`.ts` 和 `.flv` 能救**（内容本身是 h264 时，换进 mkv/mp4 即可播）。**`.mkv` 本来就能直接播，不需要救**。 |
| remux 救不了哪些？ | **`.avi`（wmv3 27/40 + mpeg4 13/40）**、**`.wmv`（wmv3）**、**`.mkv/.ts` 里的 HEVC（8bit 与 10bit 都不行）**、**`.wma` 音频**。这些**换容器完全无效**（解码器根本不存在），只能 **re-encode 或系统播放器兜底**。 |
| 全库可播比例 | 视频 **原生 90.3%**，remux 后 **91.2%**，剩余 **8.8%（约 1606 个）必须转码或兜底**。音频原生 **64.9%**，**35.1%（207 个 .wma）不可播**。 |
| 最反直觉的发现 | **Edge 能原生直接播 `.mkv`**（Matroska 解封装已打开），且 **h264 10bit 能播**、**flac/ac3/eac3/dts 音轨都不影响播放**。所以 `.mkv` 的 4611 个文件里只有 HEVC 那部分是死路。 |

---

## 1. 抽样方法

### 1.1 路径公式修正（重要）

任务书给的公式是 `媒体根 + "/" + src + "/" + path + "/" + name`。**实测这个公式是错的**（拼出来 `os.path.exists()` 全为 False）。

实际情况：

- `item.path` 是**分区内的目录**（如 `193x`）
- `videos[i].path` / `audio[i].path` 是**已经含 src 前缀的完整相对路径**（如 `01 电影/1999/示例影片/示例影片.mkv`）

正确公式：

```
完整路径 = "\\NAS\Share\Download" + "/" + videos[i].path
```

已对全部 18900 条 entry 校验：`path` 一律以 `<src>/` 开头、无空 path，拼接后全部可访问。

### 1.2 抽样参数

- 随机种子：`seed=20261030`（固定，可复现）
- 视频：`.mkv` / `.ts` / `.avi` / `.flv` / `.wmv` 各上限 40
- 音频：`.flac` / `.wma` / `.m4a` / `.mp3` / `.aac` 各上限 25

### 1.3 库规模与抽样量

| 类型 | 扩展名 | 库内总数 | 实际抽样 |
|---|---|---:|---:|
| 视频 | .mp4 | 13078 | 0（不属抽样范围） |
| 视频 | .mkv | 4611 | 40 |
| 视频 | .avi | 334 | 40 |
| 视频 | .ts | 133 | 40 |
| 视频 | .mov | 82 | 0（不属抽样范围） |
| 视频 | .flv | 71 | 40 |
| 视频 | .wmv | 1 | 1（全取） |
| 音频 | .flac | 259 | 25 |
| 音频 | .wma | 207 | 25 |
| 音频 | .m4a | 115 | 25 |
| 音频 | .wav | 6 | 0（不属抽样范围） |
| 音频 | .mp3 | 3 | 3（全取） |
| 音频 | .aac | **0** | **0（池为空）** |

**实际执行抽样：视频 161 个，音频 78 个，合计 239 个。**

库总量：items=2220，videos=18310，audio=590。

### 1.4 探测命令

```bat
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,profile,width,height,pix_fmt,level -of json "<路径>"
ffprobe -v error -select_streams a:0 -show_entries stream=codec_name,profile,channels,sample_rate -of json "<路径>"
```

每文件 20 秒超时；失败不中断，单独归类为 `missing`（文件不存在）/ `error`（超时或其他）/ `no-stream`（无该类型流）。单文件平均耗时约 0.6 秒。

---

## 2. 每格式编码分布（ffprobe 实测）

### 2.1 视频编码分布

| 扩展名 | n | 视频编码分布 | 像素格式分布 |
|---|---:|---|---|
| **.mkv** | 40 | **h264: 30**，hevc: 10 | **yuv420p: 26**，**yuv420p10le: 14** |
| **.ts** | 40 | **h264: 29**，hevc: 11 | **yuv420p: 29**，yuvj420p: 11 |
| **.avi** | 40 | **wmv3: 27**，mpeg4: 13 | yuv420p: 40（全部） |
| **.flv** | 40 | **h264: 40**（全部） | yuv420p: 40（全部） |
| **.wmv** | 1 | wmv3: 1 | yuv420p: 1 |

profile 明细：

- `.mkv`：h264/High 20、hevc/Main 10 **8**、h264/**High 10** 6、h264/Main 4、hevc/Main 2
- `.ts`：h264/High 29、hevc/Main 11
- `.avi`：wmv3/Main 27、mpeg4/None 13
- `.flv`：h264/High 40
- `.wmv`：wmv3/Main 1

**10bit 只出现在 `.mkv`**：14/40 = 35%（hevc Main10 yuv420p10le 8 + h264 High10 yuv420p10le 6）。其余扩展名 10bit 为 0。

分辨率分布（供参考）：

- `.mkv`：1920x1080 ×15、1440x1080 ×10，含 1 个 3840x1610
- `.ts`：1280x720 ×29、1088x1080 ×11
- `.avi`：640x480 ×34、352x240 ×6
- `.flv`：1920x1080 ×33

### 2.2 音轨编码分布（视频文件内 a:0）

| 扩展名 | 音轨分布 |
|---|---|
| .mkv | **flac: 15**、aac: 13、ac3: 5、eac3: 5、mp3: 1、dts: 1 |
| .ts | **aac: 40（全部）** |
| .avi | **mp3: 40（全部）** |
| .flv | **aac: 40（全部）** |
| .wmv | wmav2: 1 |

全部 161 个视频样本的 a:0 汇总：aac 93、mp3 41、flac 15、ac3 5、eac3 5、dts 1、wmav2 1。

### 2.3 音频文件分布

| 扩展名 | 抽样 | 有效 | missing | 编码 | 声道 | 采样率 | profile |
|---|---:|---:|---:|---|---|---|---|
| .flac | 25 | 23 | **2** | flac 23 | 2ch | 44100×13 / 48000×6 / **96000×4** | — |
| .wma | 25 | 25 | 0 | **wmav2 25** | 2ch | 44100×25 | — |
| .m4a | 25 | 25 | 0 | aac 25 | 2ch | 44100×25 | LC 20 / **HE-AACv2 5** |
| .mp3 | 3 | 3 | 0 | mp3 3 | 2ch | 44100×3 | — |
| .aac | 0 | — | — | （库内无此扩展名） | — | — | — |

### 2.4 失败与异常明细（共 2 个 missing + 7 个 video no-stream）

| 类别 | 数量 | 说明 |
|---|---:|---|
| `.flac` 文件 missing | **2** | 文件名以 `.flac` 结尾但磁盘上不存在，同目录有 01/02/03… 可列出 → **库清单陈旧**（扫描后文件被删/改名） |
| `.flac` 的 video 流 no-stream | **7** | 正常现象：.flac 的"视频流"实为**内嵌封面 mjpeg attached_pic**（已验证 `disposition.attached_pic` 存在），不是真视频 |
| `.wma` 的 video 流 no-stream | 25 | 正常现象：纯音频文件无视频流 |
| `.mp3` 的 video 流 no-stream | 3 | 同上 |
| 探测超时 / 其他 error | **0** | 无 |

> 结论：**抽样 239 个文件，真实缺失 2 个（均为 .flac，库清单陈旧），探测错误 0 个。** 其余 "no-stream" 均为纯音频文件的正常表现，不是故障。

---

## 3. 浏览器可播性（真实解码验证，不靠猜）

### 3.1 方法（以及一次踩坑）

`canPlayType` 只能给 `"" / "maybe" / "probably"`，**不足以判定**（例如 `video/x-matroska` 返回 `maybe`，实际能播；`video/mp4` 也返回 `maybe`，但里面的 HEVC 轨播不了）。所以做了**真实解码验证**：

1. 起临时 HTTP 服务（模拟 Media_Wall 的 MIME 表 + **HTTP Range 206** 支持，端口 8733）
2. 页面用 `<video>` 加载，在 5 个时间点 seek + play，把画面画到 canvas 采样 48×48 像素指纹
3. 判定：`PLAY_OK`（有非黑像素且指纹随时间变化）/ `NO_VIDEO_TRACK`（元数据读到但无视频轨，`videoWidth=0`）/ `FAIL_ERROR`（MEDIA_ERR_SRC_NOT_SUPPORTED，带 MediaError.message）
4. 纯音频另用 `<video>` 加载 + 读 `webkitAudioDecodedByteCount` 判定 `AUDIO_OK`

> ⚠️ **踩坑记录（避免后来者重复）**：最初用 `python -m http.server` 做测试服务（**无 Range 支持、MIME 用默认猜测**），结果原生 `.mkv` 超时、`.ts` 报错，结论完全失真。换成带 Range + 正确 MIME 的自建服务后才得到正确结果。另外 `--virtual-time-budget` 与媒体解码冲突会导致**假超时**，必须用真实时间轮询。

### 3.2 canPlayType 实测（Edge 156）

| MIME | canPlayType |
|---|---|
| `video/mp4` | `maybe` |
| `video/mp4; codecs="avc1.640028,mp4a.40.2"` | `probably` |
| `video/mp4; codecs="hvc1.1.6.L120.90"` | **`""`（不支持 HEVC）** |
| `video/mp4; codecs="flac"` | `probably` |
| `video/x-matroska` | **`maybe`（Matroska 解封装是开着的）** |
| `video/x-msvideo`（avi） | `""` |
| `video/mp2t`（ts） | `""` |
| `video/x-flv` | `""` |
| `video/x-ms-wmv` | `""` |
| `video/quicktime`（mov） | `""` |
| `audio/flac` | `probably` |
| `audio/x-flac` | `""` |
| `audio/x-ms-wma` | `""` |
| `audio/mpeg` / `audio/aac` | `probably` |

### 3.3 真实解码验证结果（27 个视频用例）

**可播（PLAY_OK，14 项）**

| 用例 | 内容 | 说明 |
|---|---|---|
| I | 原生 `.mkv` h264 + aac | **原生直接可播** |
| P | 原生 `.mkv` h264 + **flac** | **原生可播** |
| Q | 原生 `.mkv` h264 + **10bit** | **原生可播（10bit h264 能放！）** |
| R | 原生 `.mkv` h264 + ac3 | 原生可播 |
| V | 原生 `.mkv` h264 + eac3 | 原生可播 |
| W | 原生 `.mkv` h264 + **dts** | 原生可播 |
| A | mkv h264+aac → mp4 | remux 成功 |
| B | mkv h264+**flac** → mp4 | remux 成功且可播 |
| C | mkv h264+ac3 → mp4 | remux 成功 |
| F | mkv h264-**10bit**+flac → mp4 | remux 成功 |
| D | ts h264+aac → mp4 | remux 成功 |
| H | flv h264+aac → mp4 | remux 成功 |
| X | **ts 仅换容器成 mkv** | **remux 救活（未重编码）** |
| Y | **flv 仅换容器成 mkv** | **remux 救活（未重编码）** |

**不可播**

| 用例 | 内容 | 失败原因 |
|---|---|---|
| J | 原生 `.ts` | `code4: DEMUXER_ERROR_COULD_NOT_OPEN: FFmpegDemuxer` |
| K | 原生 `.flv` | 同上 code4 |
| L | 原生 `.avi` | 同上 code4 |
| N | 原生 `.wma`（音频） | 同上 code4 |
| M | 原生 `.mkv` **hevc 10bit** + flac | `NO_VIDEO_TRACK`（videoWidth=0） |
| U | mkv **hevc 8bit** + eac3 | `NO_VIDEO_TRACK` → **HEVC 无论 8/10bit 都不行** |
| E | mkv hevc-10bit → mp4 | `NO_VIDEO_TRACK` |
| G | avi mpeg4 → mp4 | `NO_VIDEO_TRACK`（mpeg4 解码器不支持） |
| Z | **avi → mkv（仅换容器）** | `NO_VIDEO_TRACK` → **换容器救不了 mpeg4** |
| AA | **wmv3 装进 mkv（仅换容器）** | `DEMUXER_ERROR_NO_SUPPORTED_STREAMS` → **换容器救不了 wmv3** |

### 3.4 音频真实解码验证

| 用例 | 结果 | audioDecodedBytes |
|---|---|---:|
| 原生 `.flac` | **AUDIO_OK** | 966639 |
| 原生 `.wma` | **FAIL_ERROR code4** | — |
| 原生 `.m4a`（aac LC） | **AUDIO_OK** | 28760 |
| 原生 `.mp3` | **AUDIO_OK** | 28356 |
| mp4 容器内 flac 音轨 | **AUDIO_OK** | 113979 |

---

## 4. 可播比例估算

### 4.1 两套规则的差异（关键）

如果只按"编码名"朴素判断（`h264 + yuv420p/yuvj420p + aac/mp3` 才算可播），会**严重低估**：

| 扩展名 | 朴素规则 | 实测规则（原生） | 差异原因 |
|---|---:|---:|---|
| **.mkv** | **13/40 = 32.5%** | **30/40 = 75.0%** | ① 10bit h264 实际能播 ② flac/ac3/eac3/dts 音轨**不影响** Edge 播放 |
| .ts | 29/40 = 72.5% | **0/40 = 0%** | 朴素规则以为编码对就能播，但 **ts 容器本身打不开** |
| .avi | 0/40 = 0% | 0/40 = 0% | 一致（wmv3/mpeg4 都无解码器） |
| .flv | 40/40 = 100% | **0/40 = 0%** | 同上，**flv 容器打不开**（但 remux 后 100% 可播） |
| .wmv | 0/1 = 0% | 0/1 = 0% | 一致 |

> **这正是本次探测最重要的方法论结论：只看 `codec_name` 会同时产生"假阴性"（mkv 的 10bit/flac）和"假阳性"（ts/flv 编码对但容器打不开）。必须实测。**

### 4.2 抽样内可播比例（实测规则）

| 扩展名 | n | 原生可播 | remux 后可播 | 必须转码/兜底 |
|---|---:|---:|---:|---:|
| .mkv | 40 | **30 (75.0%)** | 30 (75.0%) | 10 (25.0%) — 全是 HEVC |
| .ts | 40 | 0 (0%) | **29 (72.5%)** | 11 (27.5%) — HEVC |
| .avi | 40 | 0 (0%) | 0 (0%) | **40 (100%)** |
| .flv | 40 | 0 (0%) | **40 (100%)** | 0 |
| .wmv | 1 | 0 (0%) | 0 (0%) | **1 (100%)** |

### 4.3 按库规模外推（视频 18310 个）

| 扩展名 | 库总数 | 原生可播 | remux 后可播 | 需转码/兜底 |
|---|---:|---:|---:|---:|
| .mp4 | 13078 | 13078 | 13078 | 0 |
| .mkv | 4611 | 3458 | 3458 | **1153** |
| .avi | 334 | 0 | 0 | **334** |
| .ts | 133 | 0 | 96 | **37** |
| .mov | 82 | 0 | 0 | **82**（未抽样，按"不可播"保守计） |
| .flv | 71 | 0 | **71** | 0 |
| .wmv | 1 | 0 | 0 | **1** |
| **合计** | **18310** | **16536 (90.3%)** | **16704 (91.2%)** | **1606 (8.8%)** |

> `.mp4` 未抽样（不在任务范围），按"浏览器原生支持"计为 100% 可播。若 mp4 里也有 HEVC（本库 mkv/ts 里 HEVC 占比约 25%，mp4 未验证），实际可播率会低于 90.3%。**这是本报告最大的不确定项**，建议后续单独抽查 mp4。

### 4.4 音频可播比例

| 扩展名 | 库总数 | 原生可播 | 不可播 |
|---|---:|---:|---:|
| .flac | 259 | ✅ 259 | 0 |
| .m4a | 115 | ✅ 115 | 0 |
| .mp3 | 3 | ✅ 3 | 0 |
| .wav | 6 | ✅ 6 | 0 |
| .wma | 207 | 0 | **❌ 207** |
| **合计** | **590** | **383 (64.9%)** | **207 (35.1%)** |

---

## 5. remux / re-encode 实验记录

全部实验在 `G:\Python_Code\Media_Wall\_scaffold\_probe_out\` 下执行，ffmpeg 8.0.1，输出文件实验后已删除。

### 5.1 remux 命令模板

```bat
ffmpeg -y -i "<输入>" -map 0:v:0 -map 0:a:0 -c copy -movflags +faststart -f mp4 "<输出>.mp4"
```

### 5.2 实验明细

| # | 输入 | 源大小 | 目标 | 结果 | 耗时 | 输出大小 | 说明 |
|---|---|---|---:|---|---:|---:|---|
| t1 | mkv h264-8bit + aac | 125.2 MB | mp4 | ✅ rc=0 | **4.42s** | 125.3 MB | h264 High yuv420p 1920x1080 + aac LC 2ch 48k |
| t2 | mkv h264-8bit + **flac** | 2008.2 MB | mp4 | ✅ rc=0 | **55.10s** | 1933.8 MB | **flac 原样进了 mp4**，ffmpeg **未报错也未自动转码** |
| t3 | mkv h264-8bit + ac3 | 356.8 MB | mp4 | ✅ rc=0 | 6.51s | 357.5 MB | ac3 2ch 48k 进 mp4 |
| t4 | **.ts** h264 + aac | 879.5 MB | mp4 | ✅ rc=0 | 19.33s | 793.4 MB | 变小（ts 有冗余），h264 High yuv420p 1280x720 + aac |
| t5 | **.wma** | 12.9 MB | m4a（re-encode aac 192k） | ✅ rc=0 | **89.88s** | **18.1 MB** | **比源大 40%**（wma 原码率低）；速度仅 **8.3x 实时** |
| t6 | mkv **hevc-Main10** + flac | 13.0 MB | mp4 | ✅ rc=0 | 1.08s | 12.9 MB | 封装成功，但浏览器**播不了**（见 3.3 E） |
| t7 | mkv **h264-High10** + flac | 490.2 MB | mp4 | ✅ rc=0 | 11.78s | 427.0 MB | 封装成功，浏览器**能播**（见 3.3 F） |
| t8 | avi mpeg4 + mp3 | 61.0 MB | mp4 | ✅ rc=0 | 5.52s | 59.4 MB | 封装成功，但浏览器**播不了**；ffprobe 报 `[mp3float] Header missing` |
| t9 | **.wmv wmv3** + wmav2 | 313.5 MB | mp4 | ❌ **失败** | 0.62s | **0 字节** | 见下方报错原文 |
| t10 | .flv h264 + aac | 34.4 MB | mp4 | ✅ rc=0 | 2.16s | 34.4 MB | 封装成功，浏览器可播 |

### 5.3 t9 失败报错原文

```
[mp4 @ 0000000000000000] Could not find tag for codec wmv3 in stream #0,
    codec not currently supported in container
[out#0/mp4 @ 0000000000000000] Could not write header (incorrect codec parameters ?): Invalid argument
Conversion failed!
```

退出码 `4294967274`（即 `-22` / `EINVAL`）。

> **注意区分"封装失败"和"封装成功但播不了"**：t6/t7/t8 都是 `rc=0` 封装成功，但 t6（hevc）和 t8（mpeg4）浏览器仍然播不了——**remux 成功 ≠ 能播**。而 t9 是连封装都进不去。

### 5.4 flac 进 mp4 的实际行为（任务重点）

**结论：ffmpeg 8.0.1 不报错、不转码，直接把 flac 原样放进 mp4，且 Edge 能播。**

- 命令：`-c copy` + `-f mp4`
- ffmpeg 输出：无警告、无错误，rc=0
- ffprobe 校验输出文件：`A: flac, None, 2ch, 48000`
- 浏览器实测（用例 B）：`PLAY_OK`，且 `audioDecodedByteCount=113979` → **音频确实解出来了**

所以"mp4 容器不支持 flac"这个常见说法，在 **ffmpeg 8.x + Edge 156** 这个组合上**不成立**（MP4 规范后来加入了 FLAC 的正式封装支持）。

**但仍建议 flac → aac 转码**：兼容更老的 Safari / Chrome / 安卓 WebView。若要省 CPU，可保留 flac 并接受"老浏览器可能播不出声"。

### 5.5 remux 成本 vs re-encode 成本

| 操作 | 实测速度 | 2GB 文件耗时 | 1 小时音频耗时 |
|---|---:|---:|---:|
| **remux（`-c copy`）** | **50 ~ 570x 实时** | **55 秒**（t2 实测） | — |
| **re-encode（wma → aac）** | **8.3x 实时** | — | **约 7 分钟** |

> remux 成本极低（几乎只是磁盘 I/O），**能 remux 就绝不 re-encode**。re-encode 慢一个数量级，且对 NAS 上的大文件是明显的 CPU 负担。

---

## 6. 明确结论

> **remux 能救的：**
> 1. **`.ts`（133 个）** —— 容器本身浏览器打不开（`DEMUXER_ERROR_COULD_NOT_OPEN`），但内容 72.5% 是 h264。`-c copy` 换进 mkv 或 mp4 即可播，**无需重编码**（用例 X 已验证）。
> 2. **`.flv`（71 个）** —— 同上，内容 100% 是 h264 + aac。换容器即 100% 可播（用例 Y 已验证）。
> 3. **`.mkv` 本来就不用救** —— Edge 直接能播（含 h264 10bit、含 flac/ac3/eac3/dts 音轨）。只有里面的 **HEVC 那 25%** 救不了。
> 4. **`.avi` 里的 mpeg4/mp3 → mp4** 虽然能封装成功，但**播不了**，属于"假救活"，别做。
>
> **remux 救不了、必须 re-encode 或系统播放器兜底的（约 1606 个视频 + 207 个音频）：**
> 1. **`.avi` 334 个（wmv3 27/40 + mpeg4 13/40）** —— 解码器缺失，**换容器完全无效**（用例 Z 已验证：avi→mkv 仍 `NO_VIDEO_TRACK`）。
> 2. **`.wmv` 1 个（wmv3）** —— 同 wmv3 无解码器；且 wmv3 **连 mp4 容器都进不去**（t9 报 `Could not find tag for codec wmv3`）。
> 3. **`.mkv`/`.ts` 里的 HEVC（约 1153 + 37 个）** —— `canPlayType('mp4;codecs="hvc1"')` 返回空，**8bit 与 10bit 都不行**（用例 U/M/E 已验证），换任何容器都没用。本机 Edge 没启用 HEVC 硬解。
> 4. **`.wma` 音频 207 个** —— 原生 `code4` 打不开；需 re-encode 成 aac（实测 8.3x 实时，1 小时音频约 7 分钟）或系统播放器兜底。
> 5. **`.mov` 82 个** —— 未抽样验证，`video/quicktime` canPlayType 返回空，保守按不可播计。
>
> **一句话总结：**
> **remux 只能救"容器不认、但编码认"的 `.ts` / `.flv`（约 204 个，成本几十秒/个）；救不了"编码本身没解码器"的 wmv3 / mpeg4 / HEVC / wma（约 1606 视频 + 207 音频，必须 re-encode 或走系统播放器）。**

---

## 7. 对 Media_Wall 的具体建议

1. **`.mkv` 不要无脑转码**。当前 `mediawall/tracks.py` 已实现按音轨 remux 缓存（`-c:v copy -c:a copy`），方向正确。`.mkv` 里 h264 的 75% 可以直接原生播，**不需要任何预处理**。
2. **`.ts` / `.flv` 做「首次播放时 remux 缓存」**。成本 50~570x 实时，几十秒即可，收益是 204 个文件从"完全打不开"变成"能播"。
3. **`.avi` / `.wmv` / `.wma` / HEVC 直接走 `sysOpen()` 系统播放器兜底**，不要尝试 remux（已验证无效，白耗 I/O）。前端 `index.html:1820-1832` 的 sysOpen 已经是正确的兜底路径。
4. **前端可考虑按编码提前提示**。当前 `onVdError`（`index.html:1802-1818`）是**播放失败后**才提示；若后端在 `/api` 里带上 codec 信息，可在点击播放前就标出"该文件需系统播放器"，体验更好。
5. **`data/library.json` 有 2 个陈旧条目**（.flac，文件已不存在）。建议后续扫描时增加"文件存在性校验"或提供清理入口。

---

## 8. 验证情况与不确定项

| 项 | 状态 |
|---|---|
| 抽样可复现 | ✅ seed=20261030 固定 |
| ffprobe 数据 | ✅ 239 个样本实测，无探测错误 |
| 浏览器可播性 | ✅ 27 个视频用例 + 5 个音频用例**真实解码**验证（非 canPlayType 推测） |
| remux 实验 | ✅ 10 组，含成功/失败/耗时/大小/报错原文 |
| **`.mp4`（13078 个）未抽样** | ⚠️ 按"原生可播"计入 90.3%，若其中含 HEVC 则实际可播率会下降。**建议后续单独抽查 mp4 的编码分布** |
| **`.mov`（82 个）未抽样** | ⚠️ 保守按不可播计 |
| **仅 Edge 156 验证** | ⚠️ Safari / Firefox / 安卓 WebView 行为可能不同（尤其 flac-in-mp4、mkv 原生支持） |
| **仅 Windows 端** | ⚠️ 手机端浏览器（鸿蒙/安卓）解码能力与本机 Edge 不同，需单独验证 |

---

*本报告不含任何凭据。项目源码、`data/library.json`、`version/` 均未改动；临时输出目录 `_scaffold\_probe_out\` 已删除。*
