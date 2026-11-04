import re
from typing import Optional

EPISODE_PATTERNS = [
    re.compile(r"(?:第\s*)0*(\d{1,4})\s*(?:集|話|话)"),
    re.compile(r"\bEP[\s._-]*0*(\d{1,4})\b", re.I),
    re.compile(r"\bE[\s._-]*0*(\d{1,4})\b", re.I),
    re.compile(r"(?<!\d)0*(\d{1,4})\s*集"),
]
META_TAGS = {"电视剧", "短剧", "电影", "动漫", "综艺", "4k", "8k", "1080", "1080p", "720p", "2160p", "hdr", "完结", "更新", "中字", "国语", "粤语", "正片", "加更"}
AD_HINTS = ("点击关注", "广告", "推广", "商务合作", "频道推荐", "搜索引擎", "加入群组", "投稿联系")

def episode_number(text: str) -> Optional[int]:
    for pattern in EPISODE_PATTERNS:
        m = pattern.search(text or "")
        if m:
            try: return int(m.group(1))
            except ValueError: pass
    return None

def clean_title(text: str) -> str:
    raw = (text or "").replace("\u200b", " ")
    tags = re.findall(r"#([\w\u4e00-\u9fff-]+)", raw)
    title_tag = next((t for t in tags if t.lower() not in META_TAGS and not re.fullmatch(r"\d+", t)), "")
    if title_tag: return title_tag[:120].strip()
    s = re.sub(r"#[\w\u4e00-\u9fff-]+", " ", raw)
    s = re.sub(r"(?:第\s*)?0*\d{1,4}\s*(?:集|話|话)", " ", s, flags=re.I)
    s = re.sub(r"\b(?:EP|E)[\s._-]*0*\d{1,4}\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:1080p?|720p|2160p|4k|8k|HDR|WEB-DL|蓝光)\b", " ", s, flags=re.I)
    s = re.sub(r"(需要其他集数.*|评论区.*|网盘.*|点击.*蓝色.*|https?://\S+)", " ", s, flags=re.I)
    s = re.sub(r"[【】\[\]（）()「」『』《》<>]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_·|#")
    return s[:120].strip()

def classify(text: str, duration: Optional[int], has_episode: bool) -> str:
    raw = text or ""
    t = raw.lower()
    # Explicit type tags/phrases take priority over duration heuristics.
    if any(x in t for x in ("#电影", "电影正片", "院线电影", "蓝光电影", "movie", "film")):
        return "movie"
    if "#短剧" in t or "短剧" in t or "微短剧" in t:
        return "short_drama"
    if any(x in t for x in ("#电视剧", "电视剧", "连续剧", "剧集")):
        return "tv"
    # Duration is only a weak signal; avoid classifying every short clip as a short drama.
    if has_episode and duration is not None and duration <= 600 and any(x in t for x in ("短篇", "短剧场", "全集短剧")):
        return "short_drama"
    if has_episode:
        return "tv"
    return "other"

def detect_quality(text: str) -> Optional[str]:
    t=(text or "").lower()
    for q in ("8k", "4k", "2160p", "1080p", "1080", "720p", "480p"):
        if q in t: return q.upper()
    return None

def parse_message(text: str, duration: Optional[int], has_media: bool):
    ep=episode_number(text); title=clean_title(text)
    if len(title)<2 or title.isdigit(): title=""
    return {"title":title,"episode":ep,"category":classify(text,duration,ep is not None),"quality":detect_quality(text),"has_media":has_media,
            "is_ad": any(x in (text or "") for x in AD_HINTS)}
