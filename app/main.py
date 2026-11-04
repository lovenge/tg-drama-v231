import os, asyncio, secrets, logging, re, mimetypes
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import db
from .collector import collector, poll_loop

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="TG Drama Web Library", version="2.0.0")
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(os.path.join(DATA_DIR, "covers"), exist_ok=True)
app.mount("/covers", StaticFiles(directory=os.path.join(DATA_DIR, "covers")), name="covers")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "please-change-this-token")

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

@app.on_event("startup")
async def startup():
    db.init_db()
    asyncio.create_task(poll_loop())

@app.get("/", response_class=HTMLResponse)
async def home():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return FileResponse(os.path.join(WEB_DIR, "admin.html"))

@app.get("/health")
async def health():
    return {"ok": True, "service": "tg-drama", "telegram_authorized": bool(collector.ready)}

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
async def remove_channel(channel_id: int, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    if not db.delete_channel(channel_id):
        raise HTTPException(404, "频道不存在")
    return {"ok": True, "deleted": True, "note": "频道已从管理列表移除，历史收录数据保留"}

@app.get("/api/admin/telegram/status")
async def telegram_status(x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    try:
        authorized = await collector.connect()
    except Exception:
        authorized = False
    return {"authorized": bool(authorized)}

@app.post("/api/admin/scan")
async def scan(x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    if not await collector.connect():
        raise HTTPException(400, "请先完成 Telegram 登录")
    results=[]
    limit=int(os.getenv("INITIAL_SCAN_LIMIT","3000"))
    for ch in db.list_channels():
        if not ch.get("enabled", 1):
            continue
        try:
            results.append(await collector.scan_channel(ch, limit=limit))
        except Exception as e:
            results.append({"channel":ch["username"],"error":str(e)})
    return {"results":results}


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
    try: return {"ok": True, **db.merge_dramas(body.target_id, body.source_ids)}
    except ValueError as e: raise HTTPException(400, str(e))

@app.post("/api/admin/dramas/{drama_id}/refresh-cover")
async def refresh_cover(drama_id: int, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    drama = db.get_drama(drama_id)
    if not drama: raise HTTPException(404, "作品不存在")
    if not await collector.connect(): raise HTTPException(400, "请先完成 Telegram 登录")
    from .db import conn
    with conn() as c:
        rows = [dict(r) for r in c.execute("SELECT e.message_id,c.username FROM episodes e JOIN channels c ON c.id=e.channel_id WHERE e.drama_id=? ORDER BY e.episode_number IS NULL,e.episode_number", (drama_id,))]
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
    drama=db.get_drama(drama_id)
    if not drama: raise HTTPException(404, "作品不存在")
    return {"drama":drama,"episodes":db.list_episodes(drama_id)}

@app.get("/api/dramas/{drama_id}/episodes")
async def episodes(drama_id: int):
    if not db.get_drama(drama_id): raise HTTPException(404, "作品不存在")
    return db.list_episodes(drama_id)

@app.get("/api/episodes/{episode_id}/play")
async def play(episode_id: int):
    """Return the in-page stream endpoint and Telegram fallback link."""
    from .db import conn
    with conn() as c:
        row=c.execute("SELECT message_url,media_type FROM episodes WHERE id=?",(episode_id,)).fetchone()
    if not row: raise HTTPException(404, "集数不存在")
    return {"url":f"/api/episodes/{episode_id}/stream", "fallback_url":row["message_url"], "mode":"stream"}

@app.get("/api/episodes/{episode_id}/stream")
async def stream_episode(episode_id: int, range_header: str | None = Header(default=None, alias="Range")):
    """Stream Telegram media on demand. No complete video is written to disk."""
    from .db import conn
    with conn() as c:
        row=c.execute("""SELECT e.message_id,e.media_type,c.username
          FROM episodes e JOIN channels c ON c.id=e.channel_id WHERE e.id=?""", (episode_id,)).fetchone()
    if not row:
        raise HTTPException(404, "集数不存在")
    if not await collector.connect():
        raise HTTPException(503, "Telegram 尚未登录或连接失败")
    try:
        entity = await collector.client.get_entity("@" + row["username"])
        msg = await collector.client.get_messages(entity, ids=row["message_id"])
        if not msg or not msg.media or not (msg.video or msg.document):
            raise HTTPException(415, "该消息没有可在线播放的视频文件")
        media = msg.media
        file_info = getattr(msg, "file", None)
        size = int(getattr(file_info, "size", 0) or 0)
        if size <= 0:
            raise HTTPException(502, "无法读取视频文件大小")
        mime = getattr(file_info, "mime_type", None) or ("video/mp4" if msg.video else "application/octet-stream")
        start, end, status = 0, size - 1, 200
        if range_header:
            match = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
            if not match:
                raise HTTPException(416, "不支持的 Range 请求", headers={"Content-Range": f"bytes */{size}"})
            a, b = match.groups()
            if not a and not b:
                raise HTTPException(416, "无效的 Range 请求", headers={"Content-Range": f"bytes */{size}"})
            if not a:
                suffix = int(b)
                if suffix <= 0: raise HTTPException(416, "无效的 Range 请求", headers={"Content-Range": f"bytes */{size}"})
                start = max(0, size - suffix)
            else:
                start = int(a)
                if b: end = min(int(b), size - 1)
            if start >= size or end < start:
                raise HTTPException(416, "Range 超出文件范围", headers={"Content-Range": f"bytes */{size}"})
            status = 206
        length = end - start + 1
        async def content():
            # Telethon iter_download(limit=...) expects a number of chunks,
            # not a byte count. Request only the required chunk count and trim
            # the final chunk so the body exactly matches Content-Length.
            chunk_size = 512 * 1024
            remaining = length
            chunk_count = (length + chunk_size - 1) // chunk_size
            async for chunk in collector.client.iter_download(
                media,
                offset=start,
                limit=chunk_count,
                chunk_size=chunk_size,
                request_size=chunk_size,
            ):
                if not chunk or remaining <= 0:
                    break
                piece = bytes(chunk[:remaining])
                if piece:
                    yield piece
                    remaining -= len(piece)
            if remaining:
                logging.getLogger("stream").error(
                    "Telegram stream short read: episode=%s requested=%s missing=%s start=%s",
                    episode_id, length, remaining, start,
                )
                raise RuntimeError(f"Telegram 视频数据不完整，缺少 {remaining} 字节")
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Disposition": "inline",
            "Cache-Control": "no-store, no-transform",
            "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering": "no",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(content(), status_code=status, media_type=mime, headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        log = logging.getLogger("stream")
        log.exception("Telegram video stream failed for episode %s", episode_id)
        raise HTTPException(502, "从 Telegram 读取视频失败，请稍后重试或使用原消息链接") from e

@app.get("/api/updates")
async def recent_updates(limit: int = Query(50, ge=1, le=200)):
    return db.updates(limit)

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TG短剧库 - 管理面板</title>
<style>
body{font-family:system-ui,-apple-system,"Microsoft Yahei",sans-serif;background:#101827;color:#e8eef7;margin:0;padding:20px}
main{max-width:760px;margin:auto}section{background:#1b293b;padding:18px;border-radius:14px;margin:14px 0}
h1{font-size:24px}h2{font-size:18px;margin-top:0}input,button{box-sizing:border-box;width:100%;padding:12px;margin:6px 0;border-radius:9px;border:1px solid #40516a;background:#111d2c;color:white}
button{background:#237b55;border:0;font-weight:700}small,.muted{color:#9fb0c5}.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
pre{white-space:pre-wrap;word-break:break-word;background:#111d2c;padding:12px;border-radius:8px}
</style>
<main><h1>🎬 TG 短剧库 · 管理面板</h1><p class="muted">首次使用：填写管理口令、Telegram API 凭据，登录后添加公开频道。</p>
<section><h2>① 管理口令</h2><input id="token" type="password" placeholder="docker-compose 中设置的 ADMIN_TOKEN"></section>
<section><h2>② Telegram 登录</h2><p class="muted">API_ID / API_HASH 在 my.telegram.org 获取；手机号使用国际格式，例如 +86...</p>
<input id="apiid" placeholder="API_ID"><input id="apihash" placeholder="API_HASH"><input id="phone" placeholder="Telegram 手机号（含国家区号）">
<button onclick="sendCode()">发送验证码</button><input id="code" placeholder="Telegram 登录验证码">
<input id="password" type="password" placeholder="如开启两步验证，填写云密码（可留空）"><button onclick="verify()">验证并登录</button><div id="loginout" class="muted"></div></section>
<section><h2>③ 添加公开频道</h2><input id="channel" placeholder="频道用户名，例如 tengxun4k，不要带 @"><button onclick="addChannel()">添加频道</button><button onclick="loadChannels()">刷新频道列表</button><pre id="channels">尚未加载</pre></section>
<section><h2>④ 首次历史采集</h2><button onclick="scan()">开始扫描已添加频道</button><pre id="scanout">等待扫描</pre></section>
<section><h2>⑤ 服务状态</h2><button onclick="health()">检查服务</button><pre id="healthout">等待检查</pre><p class="muted">APP API：/api/dramas、/api/updates。请勿将管理面板直接暴露到公网。</p></section>
</main>
<script>
const val=id=>document.getElementById(id).value;
async function req(path,method='GET',body=null){let r=await fetch(path,{method,headers:{'Content-Type':'application/json','X-Admin-Token':val('token')},body:body?JSON.stringify(body):null});let t=await r.text();let j;try{j=JSON.parse(t)}catch{j=t}if(!r.ok)throw new Error(typeof j==='string'?j:JSON.stringify(j));return j}
async function sendCode(){try{let x=await req('/api/admin/telegram/send-code','POST',{api_id:val('apiid'),api_hash:val('apihash'),phone:val('phone')});document.getElementById('loginout').textContent=JSON.stringify(x)}catch(e){alert(e.message)}}
async function verify(){try{let x=await req('/api/admin/telegram/verify','POST',{code:val('code'),password:val('password')||null});document.getElementById('loginout').textContent=JSON.stringify(x)}catch(e){alert(e.message)}}
async function addChannel(){try{let x=await req('/api/admin/channels','POST',{username:val('channel')});alert('已添加：'+x.username);loadChannels()}catch(e){alert(e.message)}}
async function loadChannels(){try{document.getElementById('channels').textContent=JSON.stringify(await req('/api/admin/channels'),null,2)}catch(e){alert(e.message)}}
async function scan(){try{document.getElementById('scanout').textContent=JSON.stringify(await req('/api/admin/scan','POST'),null,2)}catch(e){alert(e.message)}}
async function health(){try{document.getElementById('healthout').textContent=JSON.stringify(await req('/health'),null,2)}catch(e){alert(e.message)}}
</script></html>"""
