"""浏览器播放兼容层。

Telegram 影视频道里大量资源是 MKV 封装 + HEVC/H.264 + AC3/DTS 音轨，
浏览器的 <video> 基本只吃 MP4(WebM) + H.264/VP8/VP9 + AAC/MP3/Opus。
本模块负责判断"能不能直接播"，不能时用 ffmpeg 实时转封装/转码成
fragmented MP4 直接推给浏览器，全程不落盘。
"""
import asyncio
import json
import logging
import os
import shutil
import tempfile

log = logging.getLogger("compat")

FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BIN", "ffprobe")
ALLOW_TRANSCODE = os.getenv("ALLOW_TRANSCODE", "0") == "1"
TRANSCODE_HEIGHT = int(os.getenv("TRANSCODE_HEIGHT", "720"))
TRANSCODE_PRESET = os.getenv("TRANSCODE_PRESET", "veryfast")
PROBE_BYTES = int(os.getenv("PROBE_BYTES", str(4 * 1024 * 1024)))

DIRECT_CONTAINERS = {"mp4", "m4v", "webm", "3gp"}
STREAMABLE_CONTAINERS = {"mkv", "mka", "ts", "m2ts", "mts", "flv", "avi", "mpg", "mpeg", "wmv", "asf", "ogv", "mov", "rmvb"}
BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1", "hevc", "h265"}
BROWSER_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac"}

MIME_CONTAINER = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/x-matroska": "mkv",
    "video/x-msvideo": "avi",
    "video/quicktime": "mov",
    "video/mp2t": "ts",
    "video/x-flv": "flv",
    "video/x-ms-wmv": "wmv",
    "video/ogg": "ogv",
    "video/3gpp": "3gp",
    "application/x-matroska": "mkv",
    "application/octet-stream": None,
}

EXT_CONTAINER = {"mkv": "mkv", "mp4": "mp4", "m4v": "mp4", "avi": "avi", "ts": "ts",
                 "mov": "mov", "flv": "flv", "wmv": "wmv", "webm": "webm",
                 "mpg": "mpg", "mpeg": "mpg", "3gp": "3gp", "rmvb": "rmvb", "m2ts": "ts"}


def guess_container(mime: str | None, filename: str | None) -> str | None:
    c = MIME_CONTAINER.get((mime or "").lower())
    if c:
        return c
    if filename and "." in filename:
        return EXT_CONTAINER.get(filename.rsplit(".", 1)[-1].lower())
    return c


def ffmpeg_available() -> bool:
    return bool(shutil.which(FFMPEG))


async def ffprobe_bytes(data: bytes, timeout: float = 20.0) -> dict:
    """用一段字节前缀探测编码信息。数据为空或探测失败时返回 {'ok': False}。"""
    if not data or not shutil.which(FFPROBE):
        return {"ok": False, "reason": "no probe data or ffprobe missing"}
    fd, path = tempfile.mkstemp(prefix="probe-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            FFPROBE, "-hide_banner", "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"start_new_session": True} if os.name == "posix" else {}),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "reason": "ffprobe 超时"}
        if proc.returncode != 0:
            return {"ok": False, "reason": (err or b"").decode("utf-8", "ignore")[:200]}
        info = json.loads(out or b"{}")
        streams = info.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        return {
            "ok": bool(video),
            "container": (info.get("format") or {}).get("format_name"),
            "video_codec": (video or {}).get("codec_name"),
            "audio_codec": (audio or {}).get("codec_name"),
            "width": (video or {}).get("width"),
            "height": (video or {}).get("height"),
        }
    except Exception as e:  # noqa: BLE001
        log.debug("ffprobe failed: %s", e)
        return {"ok": False, "reason": str(e)[:200]}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def plan(container: str | None, probe: dict | None = None) -> dict:
    """决定播放模式：direct / remux / transcode / download。"""
    probe = probe or {}
    vcodec = (probe.get("video_codec") or "").lower()
    acodec = (probe.get("audio_codec") or "").lower()
    container = (container or "").lower() or None
    info = {"container": container, "video_codec": vcodec or None,
            "audio_codec": acodec or None, "seekable": True}

    if probe and not probe.get("ok") and container not in DIRECT_CONTAINERS:
        info.update(mode="download", seekable=False,
                    reason="ffprobe 无法解析该文件，浏览器无法直接播放",
                    hint="建议下载后用本地播放器观看")
        return info

    # H.265/HEVC：绝大多数桌面/手机 Chrome 没有硬解，直接播和转封装都不行，
    # 必须软件转码成 H.264；这里不受 ALLOW_TRANSCODE 开关限制（本来就播不了）。
    if vcodec in ("hevc", "h265"):
        # 严格遵守开关：默认禁止软件转码，避免 RK3566 被高负载转码拖慢。
        if not ALLOW_TRANSCODE:
            info.update(mode="download", seekable=False,
                        reason=f"视频是 {vcodec}（H.265），当前已禁用软件转码",
                        hint="可下载后使用本地播放器；如确实需要浏览器转码，再显式开启 ALLOW_TRANSCODE=1")
            return info
        if not ffmpeg_available():
            info.update(mode="download", seekable=False,
                        reason=f"视频是 {vcodec}，浏览器可能不支持，且容器内没有 ffmpeg",
                        hint="安装 ffmpeg 后可启用软件转码")
            return info
        info.update(mode="transcode", seekable=False,
                    reason=f"视频编码 {vcodec}（H.265），按设置软件转码为 H.264 {TRANSCODE_HEIGHT}p",
                    note="RK3566 软件转码会占 CPU，启动头几秒可能较慢")
        return info

    if container in DIRECT_CONTAINERS:
        if vcodec and vcodec not in BROWSER_VIDEO_CODECS:
            info.update(mode="download", seekable=False,
                        reason=f"MP4 内是 {vcodec} 编码，浏览器普遍不支持",
                        hint="建议下载后用本地播放器观看")
            return info
        note = ""
        if vcodec in ("hevc", "h265"):
            note = "HEVC 编码：Safari/Edge 可播，部分 Chrome 不支持"
        if acodec and acodec not in BROWSER_AUDIO_CODECS:
            # MP4 里出现 AC3/DTS，浏览器通常只有画面没有声音
            if ffmpeg_available():
                info.update(mode="remux", seekable=False,
                            reason=f"音轨是 {acodec}，浏览器不支持，需要转 AAC",
                            note=note)
                return info
            info.update(mode="download", seekable=False,
                        reason=f"音轨是 {acodec}，浏览器不支持", hint="安装 ffmpeg 后可自动转 AAC")
            return info
        # MP4 容器本身可直接交给浏览器；探测为空不等于编码不兼容。
        # 尤其 ALLOW_TRANSCODE=0 时绝不能因缺少探测信息而自动转码。
        if not vcodec:
            info.update(mode="direct", seekable=True,
                        reason="MP4 编码信息未知，按原始文件直连（不自动转码）",
                        note="若浏览器黑屏或无声，请尝试下载原片或使用支持更多编码的播放器")
            return info
        info.update(mode="direct", seekable=True, note=note)
        return info

    if not ffmpeg_available():
        info.update(mode="download", seekable=False,
                    reason="容器不是 MP4/WebM，且容器内没有 ffmpeg，无法转封装",
                    hint="在 Dockerfile 中安装 ffmpeg 后可自动兼容")
        return info

    streamable = container in STREAMABLE_CONTAINERS or container is None
    if not streamable:
        info.update(mode="remux", seekable=False,
                    reason=f"{container} 容器需要 ffmpeg 转封装",
                    note="先尝试转封装，若失败请下载原片")
        return info

    # 视频编码可在浏览器播放时，只转封装不转码（RK3566 上开销极小）
    if not vcodec or vcodec in ("h264", "hevc", "h265", "vp8", "vp9", "av1"):
        note = ""
        if vcodec in ("hevc", "h265") and container not in DIRECT_CONTAINERS:
            note = "HEVC：部分浏览器无解码能力，若黑屏请用下载"
        info.update(mode="remux", seekable=False,
                    reason=f"{container} 封装 + {vcodec or '未知'} 视频，实时转封装为 MP4",
                    note=note)
        return info

    # mpeg4 part2 / msmpeg4 / rv 等浏览器完全不支持的视频编码
    if ALLOW_TRANSCODE:
        info.update(mode="transcode", seekable=False,
                    reason=f"视频编码 {vcodec} 浏览器不支持，软件转码为 H.264 {TRANSCODE_HEIGHT}p",
                    note="RK3566 软件转码可能跟不上，会明显卡顿或启动较慢")
        return info
    info.update(mode="download", seekable=False,
                reason=f"视频编码 {vcodec} 浏览器不支持",
                hint="设置 ALLOW_TRANSCODE=1 可启用软件转码（CPU 占用高），或直接下载原片")
    return info


def ffmpeg_args(plan_info: dict, input_path: str = "pipe:0") -> list[str]:
    """构造 ffmpeg 命令：从 stdin/文件读，fMP4 写 stdout。
    input_path 为 pipe:0 时走管道；否则从可 seek 的本地文件读（用于 moov 在尾部的 MP4）。"""
    mode = plan_info.get("mode")
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-fflags", "+genpts", "-i", input_path,
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if mode == "transcode":
        h = plan_info.get("height_override") or TRANSCODE_HEIGHT
        args += ["-c:v", "libx264", "-preset", TRANSCODE_PRESET, "-crf", "26",
                 # 只缩小不放大：原片高度小于目标高度时保持原尺寸
                 "-vf", f"scale=-2:min({h}\\,ih)", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    else:
        args += ["-c:v", "copy"]
        acodec = (plan_info.get("audio_codec") or "").lower()
        if acodec in ("aac", "mp3"):
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    args += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
             "-f", "mp4", "pipe:1"]
    return args
