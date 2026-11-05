# TG影视仓库

把 Telegram 频道里的影视资源整理成网页影视库：网页浏览、搜索、分类、点开即播。
服务端只保存索引与封面，视频按需从 Telegram 读取，不落地到 NAS 磁盘。

## 当前版本：V2.4（网页播放修复版）

- 直连播放：MP4/H.264 原样流式传输，支持拖动。
- 转封装播放：MKV + AC3/DTS 等浏览器不支持的封装，由容器内 ffmpeg 实时转成 MP4（复制视频流、音轨转 AAC），不落盘。
- 无法在线播放时，页面直接给出原因，并提供「下载原片」和「原 Telegram 消息」入口，不再无限转圈。
- 新增 `/api/episodes/<id>/probe` 播放自检接口。

详见 **V2.4升级说明.md**。

## 部署

```bash
# 项目目录下
docker compose build
docker compose up -d
```

默认端口 8787：首页 `/`，管理页 `/admin`，健康检查 `/health`。

- 首次使用在管理页填写 `ADMIN_TOKEN`、Telegram API_ID/API_HASH 与手机号登录，再添加公开频道。
- `data/` 保存数据库、封面和 Telegram session，升级前务必备份。
- 本版镜像内置 ffmpeg，可以不转码但能转封装；`ALLOW_TRANSCODE=1` 才启用软件转码（RK3566 上很吃 CPU）。

## 接口

- `GET /api/dramas`、`/api/dramas/{id}`、`/api/stats`、`/api/updates`
- `GET /api/episodes/{id}/play` 播放方案
- `GET /api/episodes/{id}/stream` 视频流（direct 支持 Range，remux 为 200 顺序流）
- `GET /api/episodes/{id}/download` 原片下载（支持 Range）
- `GET /api/episodes/{id}/probe` 播放自检



## V34.1 外网播放源码优化

本版本以 V34 为基础，保留 API、8787 端口、容器名 `tg-drama-v2`、数据卷和 FN Connect 访问方式。

优化内容：
- Telegram Range 读取先对齐到 512KB 边界，再丢弃起始偏移，减少浏览器任意 Range 偏移带来的额外读取工作。
- 读取过程中收到有效数据后重置连续超时计数，降低偶发网络抖动造成的误中断。
- ffprobe 预读数据改用 bytearray 累积，避免重复复制内存。
- 默认探测前缀从 2MB 调整为 1MB，减少非 MP4 视频播放前的预读流量。若某些文件识别失败，可在 compose 中将 PROBE_BYTES 改回 2097152。

注意：
- 本版本不能突破 FN Connect 免费中继带宽或家庭宽带上行带宽限制。
- 不安装 cryptg：ARM64 环境下避免引入额外编译/依赖风险。
- 不改 Docker 网络模式，不新增容器，不改 FN Connect。
- 兼容性缓存/FFmpeg 策略保留，避免影响 V34 已修复的 moov 在文件尾部 MP4。
- 当前 compose 仍使用原容器名和 8787 端口，因此这是原项目的升级包，不是并行安装包。

升级前备份 data 目录和 compose 配置。建议先在项目副本中测试，再替换正式项目文件并重建容器。


## V34.2 MP4 播放策略修复

- `ALLOW_TRANSCODE=0` 时，H.265/HEVC 不再绕过开关触发软件转码；会明确给出下载提示。
- MP4 已知容器但编码探测为空时，默认原始直连，不再因缺少探测信息自动触发 FFmpeg 转码。
- Telegram -> FFmpeg 读取异常改用 `%r` 记录，并在异常文本为空时回退到异常类型/表示，避免诊断日志出现空的“读取失败：”。
- 保留原有容器名 `tg-drama-v2`、8787 端口、`./data:/data` 数据挂载、FN Connect 入口及 V34.1 分段读取优化。

注意：编码探测为空时直连属于兼容性优先策略；若源视频编码实际不被浏览器支持，浏览器可能黑屏或无声，此时请使用原片下载/本地播放器。V34.2 不会擅自开启软件转码。
