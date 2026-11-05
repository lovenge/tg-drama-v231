import asyncio, os, re, secrets, logging, mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, compat
from .collector import collector, poll_loop
from .stream import EpisodeSource, StreamError, parse_range, get_stat, note_stat

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(os.path.join(DATA_DIR, "covers"), exist_ok=True)

MAX_CONCURRENT_STREAMS = int(os.getenv("MAX_CONCURRENT_STREAMS", "2"))
STREAM_SLOTS = asyncio.Semaphore(MAX_CONCURRENT_STREAMS)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "please-change-this-token")
VERSION = "2.4.0"

_play_locks: dict[int, asyncio.Lock] = {}


def play_lock(episode_id: int) -> asyncio.Lock:
    lock = _play_locks.get(episode_id)
    if lock is None:
        lock = asyncio.Lock()
        _play_locks[episode_id] = lock
    return lock


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(poll_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="TG Drama Web Library", version=VERSION, lifespan=lifespan)
app.mount("/covers", StaticFiles(directory=os.path.join(DATA_DIR, "covers")), name="covers")


# ---------- models ----------
class LoginCode(BaseModel):
    api_id: str
    api_hash: str
    phone: str

class VerifyCode(BaseModel):
    code: str
    password: str | None = None

class ChannelIn(BaseModel):
    username: str

class DramaEdit(BaseModel):
    title: str
    category: str

class DramaMerge(BaseModel):
    target_id: int
    source_ids: list[int]


def admin(x_admin_token: str | None):
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(401, "管理口令错误，请在请求头 X-Admin-Token 中提供管理口令")


# ---------- pages ----------
@app.get("/", response_class=HTMLResponse)
async def home():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return FileResponse(os.path.join(WEB_DIR, "admin.html"))

@app.get("/health")
async def health():
    return {"ok": True, "service": "tg-drama", "version": VERSION,
            "telegram_authorized": bool(collector.ready),
            "ffmpeg": compat.ffmpeg_available(),
            "allow_transcode": compat.ALLOW_TRANSCODE,
            "max_concurrent_streams": MAX_CONCURRENT_STREAMS}


# ---------- telegram login ----------
@app.post("/api/admin/telegram/send-code")
async def send_code(body: LoginCode, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    try:
        return await collector.send_code(body.api_id, body.api_hash, body.phone)
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/admin/telegram/verify")
async def verify(body: VerifyCode, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    try:
        return await collector.sign_in(body.code, body.password)
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/admin/telegram/status")
async def telegram_status(x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    try:
        authorized = await collector.connect()
    except Exception:
        authorized = False
    return {"authorized": bool(authorized)}


# ---------- channels ----------
@app.post("/api/admin/channels")
async def add_channel(body: ChannelIn, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    return db.add_channel(body.username)

@app.get("/api/admin/channels")
async def channels(x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    return db.list_channels()

@app.patch("/api/admin/channels/{channel_id}")
async def update_channel(channel_id: int, body: dict, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        raise HTTPException(400, "enabled 必须是布尔值")
    if not db.update_channel(channel_id, body["enabled"]):
        raise HTTPException(404, "频道不存在")
    return {"ok": True, "channel_id": channel_id, "enabled": body["enabled"]}

@app.delete("/api/admin/channels/{channel_id}")
async def remove_channel(channel_id: int,
                         cascade: bool = Query(False, description="是否同时删除该频道产出的所有集数"),
                         x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    if not db.delete_channel(channel_id, cascade=cascade):
        raise HTTPException(404, "频道不存在")
    return {"ok": True, "deleted": True, "cascade": cascade,
            "note": "已级联删除该频道全部资源" if cascade else "频道已停用并移出列表，历史收录数据保留"}

@app.post("/api/admin/scan")
async def scan(x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    if not await collector.connect():
        raise HTTPException(400, "请先完成 Telegram 登录")
    results = []
    limit = int(os.getenv("INITIAL_SCAN_LIMIT", "3000"))
    for ch in db.list_channels():
        if not ch.get("enabled", 1):
            continue
        try:
            results.append(await collector.scan_channel(ch, limit=limit))
        except Exception as e:
            results.append({"channel": ch["username"], "error": str(e)})
    return {"results": results}

@app.get("/api/admin/scan/status")
async def scan_status(x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    return {"auto": db.setting("auto_scan") != "0"}

@app.post("/api/admin/scan/auto")
async def scan_auto(on: bool = Query(True, description="是否开启自动轮询采集"),
                    x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    db.setting("auto_scan", "1" if on else "0")
    return {"ok": True, "auto": on}

@app.post("/api/admin/channels/{channel_id}/clear-resources")
async def clear_channel_resources(channel_id: int,
                                  x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    n = db.clear_channel_resources(channel_id)
    if n < 0:
        raise HTTPException(404, "频道不存在")
    return {"ok": True, "deleted_episodes": n, "note": f"已清空该频道 {n} 条资源，频道保留并继续采集"}


# ---------- admin data ----------
@app.get("/api/admin/stats")
async def admin_stats(x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    return db.stats()

@app.get("/api/admin/dramas")
async def admin_dramas(q: str | None = None, category: str | None = None,
                       limit: int = Query(200, ge=1, le=500), x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    return db.admin_dramas(q, category, limit)

@app.patch("/api/admin/dramas/{drama_id}")
async def edit_drama(drama_id: int, body: DramaEdit, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    try:
        if not db.update_drama(drama_id, body.title, body.category):
            raise HTTPException(404, "作品不存在")
        return {"ok": True, "drama": db.get_drama(drama_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post("/api/admin/dramas/merge")
async def merge_dramas(body: DramaMerge, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    try:
        return {"ok": True, **db.merge_dramas(body.target_id, body.source_ids)}
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post("/api/admin/dramas/{drama_id}/refresh-cover")
async def refresh_cover(drama_id: int, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    drama = db.get_drama(drama_id)
    if not drama:
        raise HTTPException(404, "作品不存在")
    if not await collector.connect():
        raise HTTPException(400, "请先完成 Telegram 登录")
    from .db import conn
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT e.message_id,c.username FROM episodes e JOIN channels c ON c.id=e.channel_id "
            "WHERE e.drama_id=? ORDER BY e.episode_number IS NULL,e.episode_number", (drama_id,))]
    for row in rows:
        try:
            entity = await collector.client.get_entity("@" + row["username"])
            msg = await collector.client.get_messages(entity, ids=row["message_id"])
            if msg and (msg.photo or msg.video or msg.document):
                await collector.save_cover(msg, drama_id, force=True)
                updated = db.get_drama(drama_id)
                if updated and updated.get("cover_url"):
                    return {"ok": True, "cover_url": updated["cover_url"]}
        except Exception:
            continue
    raise HTTPException(404, "该作品的消息中没有可提取的图片或视频缩略图")


# ---------- public data ----------
@app.get("/api/stats")
async def public_stats():
    return db.stats()

@app.get("/api/dramas")
async def dramas(category: str | None = None, q: str | None = None,
                 limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
    if category not in (None, "short_drama", "tv", "movie", "other"):
        raise HTTPException(400, "category 只能是 short_drama、tv、movie 或 other")
    return db.list_dramas(category, q, limit, offset)

@app.get("/api/dramas/{drama_id}")
async def drama_detail(drama_id: int):
    drama = db.get_drama(drama_id)
    if not drama:
        raise HTTPException(404, "作品不存在")
    return {"drama": drama, "episodes": db.list_episodes(drama_id),
            "sources": db.drama_sources(drama_id)}

@app.get("/api/dramas/{drama_id}/episodes")
async def episodes(drama_id: int):
    if not db.get_drama(drama_id):
        raise HTTPException(404, "作品不存在")
    return db.list_episodes(drama_id)

@app.get("/api/updates")
async def recent_updates(limit: int = Query(50, ge=1, le=200)):
    return db.updates(limit)


# ---------- V2.5 待审核队列 ----------
@app.get("/api/admin/audit")
async def audit_list(limit: int = Query(100, ge=1, le=500),
                     x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    return db.list_pending(limit)

@app.post("/api/admin/audit/{pending_id}/accept")
async def audit_accept(pending_id: int, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    row = db.get_pending(pending_id)
    if not row:
        raise HTTPException(404, "待审核记录不存在")
    channel = db.get_channel(row["channel_id"])
    if not channel:
        raise HTTPException(400, "原频道已删除，无法审核")
    if not await collector.connect():
        raise HTTPException(400, "请先完成 Telegram 登录")
    try:
        result = await collector.approve_message(channel, row["message_id"])
    except Exception as e:
        raise HTTPException(400, f"入库失败：{e}")
    db.delete_pending(pending_id)
    return {"ok": True, "result": result}

@app.post("/api/admin/audit/{pending_id}/reject")
async def audit_reject(pending_id: int, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    if not db.get_pending(pending_id):
        raise HTTPException(404, "待审核记录不存在")
    db.delete_pending(pending_id)
    return {"ok": True}


# ---------- playback preparation ----------
async def prepare(episode_id: int, force_mode: str | None = None, height: int | None = None):
    """解析该集的封装/编码并给出播放方案；结果缓存到数据库。
    force_mode：手动强制播放模式 direct/remux/transcode/download。
    height：手动指定转码高度（如 480）。"""
    ep = db.get_episode(episode_id)
    if not ep:
        raise HTTPException(404, "集数不存在")
    async with play_lock(episode_id):
        ep = db.get_episode(episode_id)
        try:
            entity, msg = await collector.open_message(ep["username"], ep["message_id"])
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        if not msg or not msg.media or not (msg.video or msg.document):
            raise HTTPException(415, "该消息没有可在线播放的视频文件（可能是图片或文字）")
        file_info = getattr(msg, "file", None)
        size = int(getattr(file_info, "size", 0) or 0)
        if size <= 0:
            raise HTTPException(502, "无法读取视频文件大小")
        container = compat.guess_container(getattr(file_info, "mime_type", None),
                                           getattr(file_info, "name", None))
        cached = ep.get("container") and ep.get("file_size") == str(size)
        probe = None
        if cached:
            probe = {"ok": True, "video_codec": ep.get("video_codec"),
                     "audio_codec": ep.get("audio_codec")}
        elif container not in compat.DIRECT_CONTAINERS and compat.ffmpeg_available():
            source = EpisodeSource(collector.client, entity, msg)
            # bytearray 避免 data += piece 在多块探测数据上反复复制内存
            probe_data = bytearray()
            try:
                async for piece in source.read(0, min(size, compat.PROBE_BYTES), episode_id):
                    probe_data.extend(piece)
            except StreamError as e:
                log.warning("probe read failed for episode %s: %s", episode_id, e)
            probe = await compat.ffprobe_bytes(bytes(probe_data))
            if probe.get("ok"):
                db.set_probe(episode_id, container=container,
                             video_codec=probe.get("video_codec"),
                             audio_codec=probe.get("audio_codec"), file_size=size)
        plan_info = compat.plan(container, probe)
        if force_mode in ("direct", "remux", "transcode", "download"):
            plan_info = {**plan_info, "mode": force_mode,
                         "seekable": force_mode == "direct",
                         "reason": f"手动模式：{force_mode}"}
        if height and 180 <= height <= 2160:
            plan_info["height_override"] = height
        return {"ep": ep, "msg": msg, "entity": entity, "size": size,
                "container": container, "probe": probe, "plan": plan_info,
                "mime": getattr(file_info, "mime_type", None) or "video/mp4",
                "filename": getattr(file_info, "name", None) or f"episode-{episode_id}.mp4"}


@app.get("/api/episodes/{episode_id}/play")
async def play(episode_id: int, mode: str | None = Query(None),
               height: int | None = Query(None)):
    info = await prepare(episode_id, force_mode=mode, height=height)
    plan_info = info["plan"]
    stream_url = f"/api/episodes/{episode_id}/stream?"
    qs = []
    if mode: qs.append(f"mode={mode}")
    if height: qs.append(f"height={height}")
    if qs: stream_url = f"/api/episodes/{episode_id}/stream?" + "&".join(qs)
    return {
        "url": stream_url,
        "download_url": f"/api/episodes/{episode_id}/download",
        "fallback_url": info["ep"].get("message_url"),
        "mode": plan_info.get("mode"),
        "seekable": bool(plan_info.get("seekable")),
        "reason": plan_info.get("reason"),
        "note": plan_info.get("note"),
        "container": plan_info.get("container"),
        "video_codec": plan_info.get("video_codec"),
        "audio_codec": plan_info.get("audio_codec"),
        "size": info["size"],
    }

@app.get("/api/episodes/{episode_id}/probe")
async def probe_episode(episode_id: int):
    info = await prepare(episode_id)
    plan_info = info["plan"]
    return {
        "episode_id": episode_id,
        "size": info["size"],
        "mime": info["mime"],
        "container": info["container"],
        "video_codec": plan_info.get("video_codec"),
        "audio_codec": plan_info.get("audio_codec"),
        "mode": plan_info.get("mode"),
        "reason": plan_info.get("reason"),
        "note": plan_info.get("note"),
        "ffmpeg": compat.ffmpeg_available(),
        "allow_transcode": compat.ALLOW_TRANSCODE,
        "last_run": get_stat(episode_id),
    }


# ---------- streaming ----------
def _headers(mode: str, extra: dict | None = None):
    base = {
        "Cache-Control": "no-store, no-transform",
        "X-Content-Type-Options": "nosniff",
        "X-Accel-Buffering": "no",
        "X-Play-Mode": mode,
    }
    base.update(extra or {})
    return base

def _direct_body(source, start: int, length: int, episode_id: int, release_slot: bool = False):
    async def gen():
        sent = 0
        try:
            async for piece in source.read(start, length, episode_id):
                sent += len(piece)
                yield piece
        finally:
            note_stat(episode_id, bytes=sent)
            if release_slot:
                STREAM_SLOTS.release()
    return gen()

async def _acquire_slot(episode_id: int):
    try:
        await asyncio.wait_for(STREAM_SLOTS.acquire(), timeout=20)
    except asyncio.TimeoutError:
        raise HTTPException(429, "同时播放数已达上限，请稍后重试或调大 MAX_CONCURRENT_STREAMS")

@app.get("/api/episodes/{episode_id}/stream")
async def stream_episode(episode_id: int, request: Request,
                         mode: str | None = Query(None),
                         height: int | None = Query(None),
                         range_header: str | None = Header(default=None, alias="Range")):
    info = await prepare(episode_id, force_mode=mode, height=height)
    plan_info = info["plan"]
    mode = plan_info.get("mode") or "download"
    if mode == "download":
        reason = plan_info.get("reason") or "该视频浏览器无法直接播放，请使用下载"
        raise HTTPException(415, reason,
                            headers={"X-Play-Mode": "download",
                                     # HTTP 头只能放 ASCII，中文原因做百分号编码
                                     "X-Play-Reason": _urlquote(reason)[:200]})

    source = EpisodeSource(collector.client, info["entity"], info["msg"])
    size = info["size"]
    await _acquire_slot(episode_id)

    if mode == "direct":
        start, end = 0, size - 1
        status = 200
        if range_header:
            try:
                start, end = parse_range(range_header, size)
            except StreamError as e:
                STREAM_SLOTS.release()
                raise HTTPException(e.status, str(e), headers={"Content-Range": f"bytes */{size}"})
            status = 206
        length = end - start + 1
        headers = _headers("direct", {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Disposition": "inline",
        })
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        note_stat(episode_id, requests=get_stat(episode_id).get("requests", 0) + 1,
                  last_mode="direct", last_error=None)
        body = _direct_body(source, start, length, episode_id, release_slot=True)
        return StreamingResponse(body, status_code=status,
                                 media_type=info["mime"] if info["mime"].startswith("video/") else "video/mp4",
                                 headers=headers)

    # remux / transcode：从 Telegram 顺序读入，ffmpeg 输出 fMP4 边转边发
    if not compat.ffmpeg_available():
        STREAM_SLOTS.release()
        raise HTTPException(500, "容器内没有 ffmpeg，无法转封装，请在 Dockerfile 中安装 ffmpeg")
    if range_header:
        log.info("episode %s 请求带 Range，但当前为 %s 模式，忽略 Range", episode_id, mode)

    args = compat.ffmpeg_args(plan_info)
    # 小文件整段落到临时盘：MP4 的 moov 常在文件尾部，管道顺序读会让 ffmpeg 找不到头部而报错
    import tempfile
    cache_limit = int(os.getenv("TRANSCODE_CACHE_MB", "300")) * 1024 * 1024
    tmp_path = None
    if size <= cache_limit:
        try:
            note_stat(episode_id, last_error=None, requests=get_stat(episode_id).get("requests", 0) + 1,
                      last_mode=f"{mode}(cache)")
            fd, tmp_path = tempfile.mkstemp(prefix=f"tg_ep_{episode_id}_", suffix=".mp4")
            fh = os.fdopen(fd, "wb")
            try:
                async for piece in source.read(0, size, episode_id):
                    fh.write(piece)
                fh.flush()
            finally:
                fh.close()
            args = compat.ffmpeg_args(plan_info, input_path=tmp_path)
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            feed_err = []
            feeder = None
        except Exception as e:  # noqa: BLE001
            log.warning("cache download failed for episode %s: %r", episode_id, e)
            note_stat(episode_id, last_error=f"缓存读取失败：{str(e).strip() or repr(e)}")
            if tmp_path and os.path.exists(tmp_path):
                try: os.unlink(tmp_path)
                except Exception: pass
            tmp_path = None
            args = compat.ffmpeg_args(plan_info)
    if tmp_path is None:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        feed_err: list[str] = []
        async def feed():
            try:
                async for piece in source.read(0, size, episode_id):
                    proc.stdin.write(piece)
                    await proc.stdin.drain()
            except Exception as e:  # noqa: BLE001
                detail = str(e).strip() or repr(e) or type(e).__name__
                log.warning("feed telegram -> ffmpeg failed for episode %s: %r", episode_id, e)
                feed_err.append(detail)
                note_stat(episode_id, last_error=f"读取失败：{detail}")
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        feeder = asyncio.create_task(feed())
    stderr_chunks: list[bytes] = []

    async def drain_stderr():
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                stderr_chunks.append(line)
                if len(stderr_chunks) > 20:
                    stderr_chunks.pop(0)
        except Exception:
            pass

    stderr_task = asyncio.create_task(drain_stderr())

    try:
        first = await asyncio.wait_for(proc.stdout.read(32 * 1024), timeout=float(os.getenv("REMUX_START_TIMEOUT", "30")))
    except asyncio.TimeoutError:
        if feeder: feeder.cancel()
        proc.kill(); stderr_task.cancel(); STREAM_SLOTS.release()
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except Exception: pass
        raise HTTPException(504, "ffmpeg 转封装 30 秒内没有输出，可能是 Telegram 读取过慢或文件头部结构不支持",
                            headers={"X-Play-Mode": mode})
    if not first:
        await proc.wait()
        if feeder: feeder.cancel()
        stderr_task.cancel(); STREAM_SLOTS.release()
        tail = b"".join(stderr_chunks).decode("utf-8", "ignore")[-400:]
        cause = ("Telegram 读流失败：" + feed_err[0]) if feed_err else tail
        log.error("ffmpeg produced no output for episode %s: %s", episode_id, tail)
        note_stat(episode_id, last_error=f"ffmpeg 无输出：{cause}")
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except Exception: pass
        raise HTTPException(502, f"转封装失败，浏览器无法播放该文件：{cause or '未知原因'}",
                            headers={"X-Play-Mode": mode})

    note_stat(episode_id, requests=get_stat(episode_id).get("requests", 0) + 1,
              last_mode=mode, last_error=None)

    async def body():
        try:
            yield first
            while True:
                chunk = await proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            if feeder: feeder.cancel()
            stderr_task.cancel()
            STREAM_SLOTS.release()
            if tmp_path and os.path.exists(tmp_path):
                try: os.unlink(tmp_path)
                except Exception: pass

    return StreamingResponse(body(), status_code=200, media_type="video/mp4",
                             headers=_headers(mode, {"Accept-Ranges": "none"}))


@app.head("/api/episodes/{episode_id}/stream")
async def stream_head(episode_id: int):
    info = await prepare(episode_id)
    plan_info = info["plan"]
    return JSONResponse(status_code=200, content=None,
                        headers=_headers(plan_info.get("mode") or "download",
                                         {"Accept-Ranges": "bytes" if plan_info.get("mode") == "direct" else "none"}))


@app.get("/api/episodes/{episode_id}/download")
async def download_episode(episode_id: int, range_header: str | None = Header(default=None, alias="Range")):
    info = await prepare(episode_id)
    source = EpisodeSource(collector.client, info["entity"], info["msg"])
    size = info["size"]
    start, end, status = 0, size - 1, 200
    if range_header:
        try:
            start, end = parse_range(range_header, size)
        except StreamError as e:
            raise HTTPException(e.status, str(e), headers={"Content-Range": f"bytes */{size}"})
        status = 206
    length = end - start + 1
    name = info["filename"] or f"episode-{episode_id}.mp4"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else "mp4"
    ascii_name = f"episode-{episode_id}.{ext}"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        # filename 用 ASCII 兜底，真实文件名走 RFC5987 的 filename*
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{_urlquote(name)}",
        "Content-Type": "application/octet-stream",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    await _acquire_slot(episode_id)
    body = _direct_body(source, start, length, episode_id, release_slot=True)
    return StreamingResponse(body, status_code=status, headers=headers)


def _urlquote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")
