import os, asyncio, logging
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from . import db
from .parser import parse_message, episode_range

log = logging.getLogger("collector")
DATA_DIR = os.getenv("DATA_DIR", "/data")
SESSION = os.path.join(DATA_DIR, "telegram")

class TelegramCollector:
    def __init__(self):
        self.client = None
        self.lock = asyncio.Lock()
        self.connect_lock = asyncio.Lock()
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
        async with self.connect_lock:
            if self.client and self.client.is_connected():
                self.ready = await self.client.is_user_authorized()
                return self.ready
            self.client = TelegramClient(SESSION, int(api_id), api_hash)
            await self.client.connect()
            self.ready = await self.client.is_user_authorized()
            return self.ready

    async def open_message(self, username: str, message_id: int):
        """取出播放所需的消息对象；返回 (entity, message)。"""
        if not await self.connect():
            raise RuntimeError("Telegram 尚未登录或连接失败")
        entity = await self.client.get_entity("@" + username)
        msg = await self.client.get_messages(entity, ids=message_id)
        if not msg:
            raise RuntimeError("Telegram 消息不存在或已被删除")
        return entity, msg

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

    async def ingest(self, channel_row, msg, force=False, override_text=None, episode_override=None):
        """单条消息入库分流：广告/纯文字/图片/小文件直接跳过；低置信度进待审核；正常入库。
        force=True：人工审核通过后强制入库。
        override_text：相册子消息继承的同组文案。
        episode_override：相册区间集数（第1-3集）按顺序分配的集号。"""
        if not getattr(msg, "video", None):
            return "no_video", None
        size = getattr(getattr(msg, "file", None), "size", 0) or 0
        if size and size < 2 * 1024 * 1024:
            return "too_small", None
        text = override_text if override_text is not None else (msg.message or "")
        media = bool(msg.media)
        duration = None
        if msg.video and getattr(msg.video, "attributes", None):
            for attr in msg.video.attributes:
                if hasattr(attr, "duration"):
                    duration = int(attr.duration)
                    break
        parsed = parse_message(text, duration, media)
        if episode_override is not None:
            parsed["episode"] = episode_override
        if parsed.get("is_ad"):
            return "ad", None
        if not media:
            return "no_media", None
        if not parsed["title"]:
            return "no_title", None
        if not force and parsed["confidence"] == "low":
            db.add_pending(channel_row["id"], msg.id, parsed["title"], text)
            return "pending", None
        saved = db.upsert_episode(channel_row, msg, {**parsed, "duration": duration})
        await self.save_cover(msg, saved["drama_id"])
        return "added", saved["drama_id"]

    async def process_batch(self, channel_row, messages):
        """处理一批消息：同 group_id 的 TG 相册/多附件消息合并识别，
        每条视频单独入库；没有 caption 的子消息继承同组首条文案。返回 (added, pending)。"""
        by_group = {}
        singles = []
        for m in messages:
            gid = getattr(m, "grouped_id", None)
            if gid:
                by_group.setdefault(gid, []).append(m)
            else:
                singles.append(m)
        added = pending = 0
        async def run(m, override=None, ep=None):
            nonlocal added, pending
            action, _ = await self.ingest(channel_row, m, override_text=override, episode_override=ep)
            if action == "added": added += 1
            elif action == "pending": pending += 1
        for m in singles:
            await run(m)
        for items in by_group.values():
            parent = max(((x.message or "") for x in items), key=len)
            # 只对视频按顺序分配区间集号（第1-3集 → 第1/2/3集），图片/小文件由 ingest 过滤
            video_items = [x for x in items if getattr(x, "video", None)]
            rng = episode_range(parent or "")
            for x in items:
                own = (x.message or "").strip()
                ep = None
                if rng and x in video_items:
                    ep = rng[0] + video_items.index(x)
                await run(x, override=None if own else (parent or None), ep=ep)
        return added, pending

    async def approve_message(self, channel_row, message_id: int):
        """人工审核通过待审核消息后，按正式采集流程强制入库。"""
        entity = await self.client.get_entity("@" + channel_row["username"])
        msg = await self.client.get_messages(entity, ids=message_id)
        if not msg:
            return None
        action, drama_id = await self.ingest(channel_row, msg, force=True)
        return {"action": action, "drama_id": drama_id}

    async def scan_channel(self, channel_row, limit=3000):
        if not await self.connect():
            raise RuntimeError("Telegram 尚未登录")
        entity = await self.client.get_entity("@" + channel_row["username"])
        messages = []
        async for msg in self.client.iter_messages(entity, limit=limit):
            messages.append(msg)
        max_id = max([m.id for m in messages], default=0)
        count, pending = await self.process_batch(channel_row, messages)
        if max_id:
            db.set_last_scanned(channel_row["id"], max_id)
        return {"channel": channel_row["username"], "processed": count,
                "pending": pending, "last_message_id": max_id}

    async def poll_new(self):
        if not await self.connect():
            return
        channels = db.list_channels()
        for ch in channels:
            if not ch["enabled"]:
                continue
            try:
                entity = await self.client.get_entity("@" + ch["username"])
                fresh = []
                async for msg in self.client.iter_messages(entity, limit=100):
                    if msg.id <= ch["last_scanned_id"]:
                        break
                    fresh.append(msg)
                if fresh:
                    await self.process_batch(ch, fresh)
                    db.set_last_scanned(ch["id"], max(m.id for m in fresh))
            except Exception:
                log.exception("poll channel failed: %s", ch["username"])

collector = TelegramCollector()

async def poll_loop():
    # 默认每 2 小时自动采集一次；可用 SCAN_INTERVAL_SECONDS 覆盖
    interval = int(os.getenv("SCAN_INTERVAL_SECONDS", str(2 * 3600)))
    while True:
        try:
            if db.setting("auto_scan") != "0":
                await collector.poll_new()
        except Exception:
            log.exception("poll loop error")
        await asyncio.sleep(max(60, interval))
