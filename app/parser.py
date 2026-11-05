import re
from typing import Optional

EPISODE_PATTERNS = [
    re.compile(r"(?:第\s*)0*(\d{1,4})\s*(?:集|話|话)"),
    re.compile(r"\bEP[\s._-]*0*(\d{1,4})\b", re.I),
    re.compile(r"\bE[\s._-]*0*(\d{1,4})\b", re.I),
    re.compile(r"(?<!\d)0*(\d{1,4})\s*集"),
]
# 区间：第1-3集 / 1～3集 / 第1至3集
EP_RANGE_PATTERN = re.compile(r"第?\s*0*(\d{1,4})\s*[-~—–至到]\s*0*(\d{1,4})\s*(?:集|話|话)")
# 全10集 / 10集全 → 第1集到第N集
EP_FULL_PATTERN = re.compile(r"(?:全\s*0*(\d{1,4})\s*(?:集|話|话))|(?:0*(\d{1,4})\s*(?:集|話|话)\s*全)")

def episode_range(text: str):
    """返回 (起始集, 结束集) 或 None。支持 "第1-3集" 和 "全10集"。"""
    m = EP_RANGE_PATTERN.search(text or "")
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= b <= 3000:
            return a, b
    m = EP_FULL_PATTERN.search(text or "")
    if m:
        n = int(m.group(1) or m.group(2))
        if 1 <= n <= 3000:
            return 1, n
    return None
META_TAGS = {"电视剧", "短剧", "电影", "动漫", "综艺", "4k", "8k", "1080", "1080p", "720p", "2160p", "hdr", "完结", "更新", "中字", "国语", "粤语", "正片", "加更"}
AD_HINTS = ("点击关注", "广告", "推广", "商务合作", "频道推荐", "搜索引擎", "加入群组", "投稿联系",
            "加微信", "私聊", "加群", "点击下方", "点击链接", "复制链接", "扫码", "二维码",
            "福利", "红包", "兼职", "刷单", "客服", "微信号", "qq群", "QQ群",
            "评论区见", "评论区获取", "关注公众号", "双击屏幕", "收藏", "求关注")

CATEGORY_KEYWORDS = ("电影", "短剧", "电视剧", "动漫", "综艺", "院线", "蓝光")

CN_DIGITS = {"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
CN_EP_PATTERN = re.compile(r"第?\s*([一二两三四五六七八九十百零]{1,6})\s*(?:集|話|话)")

def cn_to_int(s: str) -> Optional[int]:
    """中文数字转阿拉伯数字，支持 六 / 十 / 十二 / 二十 / 二十三 / 一百零五。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = 0
    if "百" in s:
        parts = s.split("百", 1)
        total += CN_DIGITS.get(parts[0], 1) * 100
        s = parts[1]
    if "十" in s:
        parts = s.split("十", 1)
        tens = CN_DIGITS.get(parts[0], 1) if parts[0] else 1
        total += tens * 10
        ones = parts[1]
        if ones:
            total += CN_DIGITS.get(ones, 0)
        return total
    for ch in s:
        total = total * 10 + CN_DIGITS.get(ch, 0)
    return total or None

def episode_number(text: str) -> Optional[int]:
    for pattern in EPISODE_PATTERNS:
        m = pattern.search(text or "")
        if m:
            try: return int(m.group(1))
            except ValueError: pass
    m = CN_EP_PATTERN.search(text or "")
    if m:
        return cn_to_int(m.group(1))
    return None

def clean_title(text: str) -> str:
    raw = (text or "").replace("\u200b", " ")
    tags = re.findall(r"#([\w\u4e00-\u9fff-]+)", raw)
    title_tag = next((t for t in tags if t.lower() not in META_TAGS and not re.fullmatch(r"\d+", t)), "")
    if title_tag: return title_tag[:120].strip()
    s = re.sub(r"#[\w\u4e00-\u9fff-]+", " ", raw)
    s = re.sub(r"第?\s*0*\d{1,4}\s*[-~—–至到]\s*0*\d{1,4}\s*(?:集|話|话)", " ", s)
    s = re.sub(r"(?:第\s*)?0*\d{1,4}\s*(?:集|話|话)", " ", s, flags=re.I)
    s = re.sub(r"第?\s*[一二两三四五六七八九十百零]{1,6}\s*(?:集|話|话)", " ", s)
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
    # 置信度：有集号或明确分类词 → 高；标题足够长 → 中；其余 → 进待审核
    t = text or ""
    has_category_key = any(k in t for k in CATEGORY_KEYWORDS)
    if ep is not None or has_category_key:
        confidence = "high"
    elif title and len(title) >= 4:
        confidence = "medium"
    else:
        confidence = "low"
    return {"title":title,"episode":ep,"category":classify(text,duration,ep is not None),"quality":detect_quality(text),"has_media":has_media,
            "is_ad": any(x in t for x in AD_HINTS), "confidence": confidence}
