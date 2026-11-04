import os, asyncio, logging
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from . import db
from .parser import parse_message

log = logging.getLogger("collector")
DATA_DIR = os.getenv("DATA_DIR", "/data")
SESSION = os.path.join(DATA_DIR, "telegram")

class TelegramCollector:
    def __init__(self):
        self.client = None
        self.lock = asyncio.Lock()
        self.phone_code_hash = None
        self.phone = None
        self.ready = False
        self.cover_dir = os.path.join(DATA_DIR, "covers")
        os.makedirs(self.cover_dir, exist_ok=True)

    async def connect(self):
        api_id = db.setting("telegram_api_id")
        api_hash = db.setting("telegram_api_hash")
        if not api_id or not api_hash:
            return False
        if self.client and self.client.is_connected():
            self.ready = await self.client.is_user_authorized()
            return self.ready
        self.client = TelegramClient(SESSION, int(api_id), api_hash)
        await self.client.connect()
        self.ready = await self.client.is_user_authorized()
        return self.ready

    async def send_code(self, api_id, api_hash, phone):
        async with self.lock:
            db.setting("telegram_api_id", api_id)
            db.setting("telegram_api_hash", api_hash)
            db.setting("telegram_phone", phone)
            if self.client:
                await self.client.disconnect()
            self.client = TelegramClient(SESSION, int(api_id), api_hash)
            await self.client.connect()
            sent = await self.client.send_code_request(phone)
            self.phone_code_hash = sent.phone_code_hash
            self.phone = phone
            return {"sent": True}

    async def sign_in(self, code, password=None):
        if not self.client:
            raise RuntimeError("请先发送登录验证码")
        try:
            if password:
                await self.client.sign_in(password=password)
            else:
                await self.client.sign_in(self.phone, code, phone_code_hash=self.phone_code_hash)
            self.ready = True
            return {"authorized": True}
        except SessionPasswordNeededError:
            return {"password_required": True}

    async def save_cover(self, message, drama_id, force=False):
        """Save only a Telegram thumbnail/photo, never the full video."""
        path = os.path.join(self.cover_dir, f"{drama_id}.jpg")
        if os.path.exists(path) and not force: return
        if force and os.path.exists(path):
            try: os.remove(path)
            except OSError: pass
        try:
            if getattr(message, "photo", None):
                await self.client.download_media(message.photo, file=path)
            elif getattr(message, "video", None) or getattr(message, "document", None):
                await self.client.download_media(message, file=path, thumb=-1)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                db.set_cover(drama_id, f"/covers/{drama_id}.jpg")
            elif os.path.exists(path):
                os.remove(path)
        except Exception:
            log.debug("cover extraction unavailable for message %s", getattr(message, "id", "?"), exc_info=True)

    async def scan_channel(self, channel_row, limit=3000):
        if not await self.connect():
            raise RuntimeError("Telegram 尚未登录")
        entity = await self.client.get_entity("@" + channel_row["username"])
        max_id = 0
        count = 0
        async for msg in self.client.iter_messages(entity, limit=limit):
            max_id = max(max_id, msg.id)
            text = msg.message or ""
            media = bool(msg.media)
            duration = None
            if msg.video and getattr(msg.video, "attributes", None):
                for attr in msg.video.attributes:
                    if hasattr(attr, "duration"):
                        duration = int(attr.duration)
                        break
            if text.strip() or media:
                parsed = parse_message(text, duration, media)
                if parsed.get("is_ad"):
                    continue
                # Avoid creating placeholder titles for unrelated media posts.
                if parsed["title"]:
                    saved = db.upsert_episode(channel_row, msg, {**parsed, "duration": duration})
                    await self.save_cover(msg, saved["drama_id"])
                    count += 1
        if max_id:
            db.set_last_scanned(channel_row["id"], max_id)
        return {"channel": channel_row["username"], "processed": count, "last_message_id": max_id}

    async def poll_new(self):
        if not await self.connect():
            return
        channels = db.list_channels()
        for ch in channels:
            if not ch["enabled"]:
                continue
            try:
                entity = await self.client.get_entity("@" + ch["username"])
                async for msg in self.client.iter_messages(entity, limit=100):
                    if msg.id <= ch["last_scanned_id"]:
                        break
                    text = msg.message or ""
                    media = bool(msg.media)
                    duration = None
                    if msg.video and getattr(msg.video, "attributes", None):
                        for attr in msg.video.attributes:
                            if hasattr(attr, "duration"):
                                duration = int(attr.duration)
                                break
                    parsed = parse_message(text, duration, media)
                    if parsed["title"] and not parsed.get("is_ad"):
                        saved = db.upsert_episode(ch, msg, {**parsed, "duration": duration})
                        await self.save_cover(msg, saved["drama_id"])
                    db.set_last_scanned(ch["id"], msg.id)
            except Exception:
                log.exception("poll channel failed: %s", ch["username"])

collector = TelegramCollector()

async def poll_loop():
    interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "90"))
    while True:
        try:
            await collector.poll_new()
        except Exception:
            log.exception("poll loop error")
        await asyncio.sleep(max(30, interval))
