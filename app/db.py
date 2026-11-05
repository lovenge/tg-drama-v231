import os, sqlite3, threading
from contextlib import contextmanager

DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "drama.db")
os.makedirs(DATA_DIR, exist_ok=True)
_lock = threading.RLock()

@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
          title TEXT, enabled INTEGER NOT NULL DEFAULT 1,
          last_scanned_id INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS dramas (
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT 'other', cover_url TEXT,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(title, category)
        );
        CREATE TABLE IF NOT EXISTS episodes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          drama_id INTEGER NOT NULL REFERENCES dramas(id) ON DELETE CASCADE,
          channel_id INTEGER NOT NULL REFERENCES channels(id), message_id INTEGER NOT NULL,
          episode_number INTEGER, message_url TEXT NOT NULL, message_text TEXT,
          media_type TEXT, duration INTEGER, quality TEXT, posted_at TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(channel_id, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_episodes_drama ON episodes(drama_id, episode_number);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        -- V2.5：低置信度消息先存这里，人工审核通过才入库
        CREATE TABLE IF NOT EXISTS pending_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
          title TEXT, text TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(channel_id, message_id)
        );
        """)
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('telegram_api_id','')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('telegram_api_hash','')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('telegram_phone','')")
        cols = {r[1] for r in c.execute("PRAGMA table_info(dramas)")}
        if "cover_url" not in cols: c.execute("ALTER TABLE dramas ADD COLUMN cover_url TEXT")
        channel_cols = {r[1] for r in c.execute("PRAGMA table_info(channels)")}
        if "deleted" not in channel_cols: c.execute("ALTER TABLE channels ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        # V2.4：缓存探测到的封装/编码/文件大小，避免每次播放重复探测
        ep_cols = {r[1] for r in c.execute("PRAGMA table_info(episodes)")}
        for col in ("container", "video_codec", "audio_codec", "file_size"):
            if col not in ep_cols:
                c.execute(f"ALTER TABLE episodes ADD COLUMN {col} TEXT")

def setting(key, value=None):
    with conn() as c:
        if value is None:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else ""
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        return value

def add_channel(username):
    username = username.strip().lstrip("@")
    with conn() as c:
        c.execute("INSERT INTO channels(username,enabled,deleted) VALUES(?,1,0) ON CONFLICT(username) DO UPDATE SET enabled=1,deleted=0", (username,))
        return dict(c.execute("SELECT * FROM channels WHERE username=?", (username,)).fetchone())

def list_channels(include_deleted=False):
    with conn() as c:
        sql = """SELECT ch.*,
                  (SELECT COUNT(*) FROM episodes e WHERE e.channel_id=ch.id) AS episode_count
                 FROM channels ch""" + ("" if include_deleted else " WHERE ch.deleted=0") + " ORDER BY ch.id"
        return [dict(r) for r in c.execute(sql)]

def update_channel(channel_id, enabled):
    with conn() as c:
        cur=c.execute("UPDATE channels SET enabled=? WHERE id=? AND deleted=0", (1 if enabled else 0, channel_id))
        return cur.rowcount > 0

def clear_channel_resources(channel_id):
    """只清空该频道已经采集的集数，频道本身保留、继续采集。
    返回删掉了多少集。"""
    with conn() as c:
        if not c.execute("SELECT id FROM channels WHERE id=? AND deleted=0", (channel_id,)).fetchone():
            return -1
        cur = c.execute("DELETE FROM episodes WHERE channel_id=?", (channel_id,))
        c.execute("DELETE FROM pending_audit WHERE channel_id=?", (channel_id,))
        c.execute("DELETE FROM dramas WHERE id NOT IN (SELECT DISTINCT drama_id FROM episodes)")
        return cur.rowcount

def delete_channel(channel_id, cascade=False):
    """删除频道：默认软删并保留历史资源；cascade=True 时连带删掉该频道的所有集数，
    并清理已经没有任何集数的孤儿作品。"""
    with conn() as c:
        cur = c.execute("UPDATE channels SET enabled=0,deleted=1 WHERE id=? AND deleted=0", (channel_id,))
        if cur.rowcount == 0:
            return False
        if cascade:
            c.execute("DELETE FROM episodes WHERE channel_id=?", (channel_id,))
            c.execute("DELETE FROM pending_audit WHERE channel_id=?", (channel_id,))
            c.execute("DELETE FROM dramas WHERE id NOT IN (SELECT DISTINCT drama_id FROM episodes)")
        return True

def drama_sources(drama_id):
    """该作品的集数分别来自哪些频道（去重）。"""
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT DISTINCT c.id, c.username, c.title, c.enabled, c.deleted
               FROM episodes e JOIN channels c ON c.id=e.channel_id
               WHERE e.drama_id=? ORDER BY c.username""", (drama_id,))]

# ---------- V2.5 待审核队列 ----------
def add_pending(channel_id, message_id, title="", text=""):
    with conn() as c:
        c.execute("""INSERT OR IGNORE INTO pending_audit(channel_id,message_id,title,text)
                     VALUES(?,?,?,?)""", (channel_id, message_id, (title or "")[:200], (text or "")[:4000]))

def list_pending(limit=100):
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT p.*, ch.username, ch.title AS channel_title FROM pending_audit p
               JOIN channels ch ON ch.id=p.channel_id
               ORDER BY p.id DESC LIMIT ?""", (limit,))]

def get_pending(pending_id):
    with conn() as c:
        r = c.execute("SELECT * FROM pending_audit WHERE id=?", (pending_id,)).fetchone()
        return dict(r) if r else None

def delete_pending(pending_id):
    with conn() as c:
        c.execute("DELETE FROM pending_audit WHERE id=?", (pending_id,))

def get_channel(channel_id):
    with conn() as c:
        r = c.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        return dict(r) if r else None

def set_last_scanned(channel_id, message_id):
    with conn() as c: c.execute("UPDATE channels SET last_scanned_id=MAX(last_scanned_id,?) WHERE id=?", (message_id, channel_id))

def upsert_episode(channel, message, parsed):
    title = parsed["title"] or f"未命名作品-{channel['username']}-{message.id}"
    category = parsed["category"]
    with conn() as c:
        drama = c.execute("SELECT id,category FROM dramas WHERE title=? ORDER BY CASE WHEN category=? THEN 0 ELSE 1 END,id LIMIT 1", (title, category)).fetchone()
        if drama is None:
            c.execute("INSERT INTO dramas(title,category) VALUES(?,?)", (title, category))
            drama = c.execute("SELECT id,category FROM dramas WHERE title=? AND category=? ORDER BY id DESC LIMIT 1", (title, category)).fetchone()
        elif drama["category"] == "other" and category != "other":
            c.execute("UPDATE dramas SET category=? WHERE id=?", (category, drama["id"]))
        media = "video" if getattr(message, "video", None) else ("photo" if getattr(message, "photo", None) else ("document" if getattr(message, "document", None) else "text"))
        date = message.date.isoformat() if getattr(message, "date", None) else None
        c.execute("""INSERT INTO episodes
          (drama_id,channel_id,message_id,episode_number,message_url,message_text,media_type,duration,quality,posted_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(channel_id,message_id) DO UPDATE SET
          drama_id=excluded.drama_id, episode_number=excluded.episode_number,
          message_url=excluded.message_url, message_text=excluded.message_text,
          media_type=excluded.media_type, duration=excluded.duration, quality=excluded.quality,
          posted_at=excluded.posted_at""",
          (drama["id"], channel["id"], message.id, parsed["episode"],
           f"https://t.me/{channel['username']}/{message.id}", (message.message or "")[:4000], media,
           parsed.get("duration"), parsed.get("quality"), date))
        c.execute("UPDATE dramas SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (drama["id"],))
        return {"title": title, "drama_id": drama["id"]}

def list_dramas(category=None, q=None, limit=100, offset=0):
    sql = """SELECT d.id,d.title,d.category,d.cover_url,d.updated_at,
      COUNT(e.id) AS episode_count, MAX(e.episode_number) AS latest_episode
      FROM dramas d LEFT JOIN episodes e ON e.drama_id=d.id"""
    args=[]; where=[]
    if category: where.append("d.category=?"); args.append(category)
    if q: where.append("d.title LIKE ?"); args.append(f"%{q}%")
    if where: sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY d.id ORDER BY d.updated_at DESC,d.title LIMIT ? OFFSET ?"
    args += [limit, offset]
    with conn() as c: return [dict(r) for r in c.execute(sql,args)]

def get_drama(drama_id):
    with conn() as c:
        r=c.execute("SELECT * FROM dramas WHERE id=?",(drama_id,)).fetchone()
        return dict(r) if r else None

def list_episodes(drama_id):
    with conn() as c:
        return [dict(r) for r in c.execute("""SELECT id,episode_number,message_url,message_text,media_type,duration,quality,posted_at
          FROM episodes WHERE drama_id=? ORDER BY CASE WHEN episode_number IS NULL THEN 1 ELSE 0 END, episode_number, posted_at""",(drama_id,))]

def updates(limit=50):
    with conn() as c:
        return [dict(r) for r in c.execute("""SELECT e.*,d.title,d.category,c.username FROM episodes e
          JOIN dramas d ON d.id=e.drama_id JOIN channels c ON c.id=e.channel_id
          ORDER BY e.posted_at DESC LIMIT ?""",(limit,))]

def set_cover(drama_id, cover_url, force=False):
    with conn() as c:
        if force:
            c.execute("UPDATE dramas SET cover_url=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (cover_url, drama_id))
        else:
            c.execute("UPDATE dramas SET cover_url=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND (cover_url IS NULL OR cover_url='')", (cover_url, drama_id))

def stats():
    with conn() as c:
        total=c.execute("SELECT COUNT(*) n FROM dramas").fetchone()["n"]
        eps=c.execute("SELECT COUNT(*) n FROM episodes").fetchone()["n"]
        channels=c.execute("SELECT COUNT(*) n FROM channels WHERE enabled=1 AND deleted=0").fetchone()["n"]
        no_cover=c.execute("SELECT COUNT(*) n FROM dramas WHERE cover_url IS NULL OR cover_url='' ").fetchone()["n"]
        cats={r["category"]:r["n"] for r in c.execute("SELECT category,COUNT(*) n FROM dramas GROUP BY category")}
        return {"dramas":total,"episodes":eps,"channels":channels,"missing_covers":no_cover,"categories":cats}

def get_episode(episode_id):
    """取播放所需的集数信息（含频道用户名）。"""
    with conn() as c:
        r = c.execute("""SELECT e.id,e.drama_id,e.channel_id,e.message_id,e.message_url,e.message_text,
          e.media_type,e.quality,e.container,e.video_codec,e.audio_codec,e.file_size,c.username
          FROM episodes e JOIN channels c ON c.id=e.channel_id WHERE e.id=?""", (episode_id,)).fetchone()
        return dict(r) if r else None

def set_probe(episode_id, container=None, video_codec=None, audio_codec=None, file_size=None):
    with conn() as c:
        c.execute("""UPDATE episodes SET container=COALESCE(?,container), video_codec=COALESCE(?,video_codec),
          audio_codec=COALESCE(?,audio_codec), file_size=COALESCE(?,file_size) WHERE id=?""",
                  (container, video_codec, audio_codec, None if file_size is None else str(file_size), episode_id))


def admin_dramas(q=None, category=None, limit=100):
    return list_dramas(category, q, limit, 0)

def update_drama(drama_id, title, category):
    title=(title or "").strip()
    if not title: raise ValueError("作品名称不能为空")
    if category not in ("short_drama","tv","movie","other"): raise ValueError("分类无效")
    with conn() as c:
        if not c.execute("SELECT id FROM dramas WHERE id=?",(drama_id,)).fetchone(): return False
        dup=c.execute("SELECT id FROM dramas WHERE title=? AND category=? AND id<>?",(title,category,drama_id)).fetchone()
        if dup: raise ValueError(f"同名同分类作品已存在，作品ID={dup['id']}；请使用合并功能")
        c.execute("UPDATE dramas SET title=?,category=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(title,category,drama_id))
        return True

def merge_dramas(target_id, source_ids):
    ids=list(dict.fromkeys(int(x) for x in source_ids if int(x)!=int(target_id)))
    if not ids: raise ValueError("请选择至少一个需要合并的作品")
    with conn() as c:
        target=c.execute("SELECT * FROM dramas WHERE id=?",(target_id,)).fetchone()
        if not target: raise ValueError("目标作品不存在")
        for sid in ids:
            src=c.execute("SELECT * FROM dramas WHERE id=?",(sid,)).fetchone()
            if not src: continue
            if not target["cover_url"] and src["cover_url"]:
                c.execute("UPDATE dramas SET cover_url=? WHERE id=?",(src["cover_url"],target_id))
            # Duplicate message rows are avoided by the unique channel/message constraint.
            c.execute("UPDATE OR IGNORE episodes SET drama_id=? WHERE drama_id=?",(target_id,sid))
            c.execute("DELETE FROM episodes WHERE drama_id=?",(sid,))
            c.execute("DELETE FROM dramas WHERE id=?",(sid,))
        c.execute("UPDATE dramas SET updated_at=CURRENT_TIMESTAMP WHERE id=?",(target_id,))
        return {"merged_into":target_id,"source_ids":ids}

def candidates(limit=100):
    with conn() as c:
        return [dict(r) for r in c.execute("""SELECT title,category,COUNT(*) count,GROUP_CONCAT(id) ids
          FROM dramas GROUP BY title,category HAVING COUNT(*)>1 ORDER BY count DESC LIMIT ?""",(limit,))]
