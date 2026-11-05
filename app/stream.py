"""Telegram 媒体流读取层。

职责：
1. 从 Telegram 按字节范围读取媒体，带空闲超时、断点续读、file_reference 失效自动刷新。
2. 记录每个集数的读取统计（字节数、速度、最后一次错误），供 /probe 诊断。
3. 不把完整视频写入磁盘（兼容层转封装除外，见 compat.py）。
"""
import asyncio
import logging
import time
from collections import OrderedDict

from telethon import errors

log = logging.getLogger("stream")

CHUNK = 512 * 1024  # Telethon MAX_CHUNK_SIZE
IDLE_TIMEOUT = float(__import__("os").getenv("STREAM_IDLE_TIMEOUT", "45"))

# 最近 50 集播放统计，供诊断使用
_STATS = OrderedDict()
_STATS_LIMIT = 50
_STATS_LOCK = asyncio.Lock()


def note_stat(episode_id: int, **kwargs):
    st = _STATS.get(episode_id)
    if st is None:
        st = {"episode_id": episode_id, "requests": 0, "bytes": 0, "errors": 0,
              "last_error": None, "last_mode": None, "started_at": None,
              "first_byte_ms": None, "avg_kbps": None}
        _STATS[episode_id] = st
        while len(_STATS) > _STATS_LIMIT:
            _STATS.popitem(last=False)
    else:
        _STATS.move_to_end(episode_id)
    for k, v in kwargs.items():
        st[k] = v
    return st


def get_stat(episode_id: int):
    return dict(_STATS.get(episode_id) or {})


class StreamError(Exception):
    def __init__(self, message: str, status: int = 502, hint: str = ""):
        super().__init__(message)
        self.status = status
        self.hint = hint


class EpisodeSource:
    """某一集对应的 Telegram 消息读取句柄。"""

    def __init__(self, client, entity, message):
        self.client = client
        self.entity = entity
        self.message = message

    async def refresh(self):
        """file_reference 过期时重新拉取消息，拿到新的引用。"""
        msg = await self.client.get_messages(self.entity, ids=self.message.id)
        if not msg or not msg.media:
            raise StreamError("该消息已失效或已被删除", status=404,
                              hint="请回到 Telegram 原消息确认内容是否仍存在")
        self.message = msg
        return msg

    async def _raw_chunks(self, offset: int, length: int, idle_timeout: float):
        """按 Telegram 推荐的 512KB 边界读取，并在本地裁掉 Range 起始偏移。

        浏览器 Range 经常从任意字节开始。先向下对齐到 CHUNK 边界，
        再丢弃前缀，可避免反复从非对齐位置请求 Telegram 数据。
        """
        if length <= 0:
            return
        aligned_offset = (offset // CHUNK) * CHUNK
        skip = offset - aligned_offset
        total = skip + length
        chunk_count = max(1, (total + CHUNK - 1) // CHUNK)
        it = self.client.iter_download(
            self.message.media,
            offset=aligned_offset,
            limit=chunk_count,
            chunk_size=CHUNK,
            request_size=CHUNK,
        ).__aiter__()
        skip_remaining = skip
        sent = 0
        try:
            while sent < length:
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), timeout=idle_timeout)
                except StopAsyncIteration:
                    return
                if not chunk:
                    continue
                data = memoryview(chunk)
                if skip_remaining:
                    drop = min(skip_remaining, len(data))
                    data = data[drop:]
                    skip_remaining -= drop
                if not data:
                    continue
                take = min(len(data), length - sent)
                if take:
                    sent += take
                    yield bytes(data[:take])
        finally:
            close = getattr(it, "close", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass

    async def read(self, start: int, length: int, episode_id: int = 0,
                   idle_timeout: float = IDLE_TIMEOUT):
        """从 start 字节开始读取 length 字节，产出 bytes。"""
        pos = start
        remaining = length
        attempts = 0
        t0 = time.time()
        sent = 0
        note_stat(episode_id, started_at=t0, first_byte_ms=None, last_error=None)
        while remaining > 0:
            try:
                got_this_round = 0
                async for chunk in self._raw_chunks(pos, remaining, idle_timeout):
                    if not chunk:
                        continue
                    piece = bytes(chunk[:remaining])
                    if not piece:
                        continue
                    if sent == 0:
                        note_stat(episode_id, first_byte_ms=round((time.time() - t0) * 1000))
                    sent += len(piece)
                    got_this_round += len(piece)
                    pos += len(piece)
                    remaining -= len(piece)
                    # 成功收到数据后，连续失败计数清零，避免偶发超时累计导致中断
                    attempts = 0
                    yield piece
                    if remaining <= 0:
                        break
                if remaining <= 0:
                    return
                # 本轮迭代器结束却仍有剩余字节，按不完整响应处理，避免空转。
                raise StreamError(f"Telegram 返回数据不完整，缺少 {remaining} 字节",
                                  status=502, hint="可能是网络中断，重试或改用下载")
            except (errors.FileReferenceExpiredError, errors.FileReferenceInvalidError):
                attempts += 1
                note_stat(episode_id, errors=get_stat(episode_id).get("errors", 0) + 1,
                          last_error="file_reference 过期，正在刷新")
                if attempts > 2:
                    raise StreamError("Telegram 文件引用已失效，无法读取",
                                      status=502, hint="请在管理页重新扫描该频道以刷新消息")
                await self.refresh()
                continue
            except asyncio.TimeoutError:
                attempts += 1
                note_stat(episode_id, errors=get_stat(episode_id).get("errors", 0) + 1,
                          last_error=f"Telegram 读取超时（{int(idle_timeout)}s 无数据）")
                if attempts > 2:
                    raise StreamError(
                        f"Telegram {int(idle_timeout)} 秒内没有返回数据",
                        status=504,
                        hint="检查 NAS 到 Telegram 的网络连通性；可调高 STREAM_IDLE_TIMEOUT",
                    )
                continue
            except StreamError:
                raise


def content_range_header(start: int, end: int, size: int) -> str:
    return f"bytes {start}-{end}/{size}"


def parse_range(header: str, size: int):
    """解析 Range 头，返回 (start, end)，非法时抛出 StreamError(416)。"""
    import re

    m = re.match(r"^bytes=(\d*)-(\d*)$", (header or "").strip())
    if not m:
        raise StreamError("不支持的 Range 请求", status=416,
                          hint=f"仅支持 bytes=a-b / bytes=a- / bytes=-n，文件大小 {size}")
    a, b = m.groups()
    if not a and not b:
        raise StreamError("无效的 Range 请求", status=416)
    if not a:
        suffix = int(b)
        if suffix <= 0:
            raise StreamError("无效的 Range 请求", status=416)
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(a)
        end = min(int(b), size - 1) if b else size - 1
    if start >= size or end < start:
        raise StreamError("Range 超出文件范围", status=416)
    return start, end
