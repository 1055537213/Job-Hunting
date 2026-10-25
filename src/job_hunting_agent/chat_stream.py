"""Bounded sync-worker/ASGI bridge; the worker owns cleanup after disconnect."""

import asyncio
import json
import logging
import time
from concurrent.futures import Future, InvalidStateError
from queue import Empty, Full, Queue
from threading import Event, Thread

import anyio
from fastapi.responses import StreamingResponse

MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
HEARTBEAT_SECONDS = 2
STREAM_SECONDS = 300
STREAM_ERROR = {"code": "CHAT_EXECUTION_UNCONFIRMED", "detail": "对话结果暂未确认，请先查看历史记录，勿立即重复发送。"}
logger = logging.getLogger(__name__)


def error_frame():
    return "event: error\ndata: " + json.dumps(STREAM_ERROR, ensure_ascii=False) + "\n\n"


class ClosingStreamResponse(StreamingResponse):
    def __init__(self, content, close):
        super().__init__(content, media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self._close = close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._close()


class StreamWorker:
    """One queued frame, one admitted thread; lease/slot release belongs to factory."""

    def __init__(self, factory, release):
        self.cancelled = Event()
        self.finished = Event()
        self._queue = Queue(maxsize=1)
        self._ready = Future()
        self._started = time.monotonic()
        Thread(target=self._run, args=(factory, release), daemon=True, name="chat-stream").start()

    def _run(self, factory, release):
        source = None
        try:
            source = iter(factory(self.cancelled))
            total = 0
            while not self.cancelled.is_set():
                try:
                    frame = next(source)
                except StopIteration:
                    break
                size = len(frame.encode("utf-8"))
                total += size
                if size > MAX_FRAME_BYTES or total > MAX_STREAM_BYTES:
                    raise ValueError("Stream exceeds response budget")
                if not self._ready.done():
                    try:
                        self._ready.set_result(frame)
                    except InvalidStateError:
                        self.cancelled.set()
                    continue
                while not self.cancelled.is_set():
                    try:
                        self._queue.put(frame, timeout=0.1)
                        break
                    except Full:
                        pass
        except Exception as error:  # noqa: BLE001 - startup errors are mapped by the route.
            if not self._ready.done():
                try:
                    self._ready.set_exception(error)
                except InvalidStateError:
                    pass
            elif not self.cancelled.is_set():
                while not self.cancelled.is_set():
                    try:
                        self._queue.put(error_frame(), timeout=0.1)
                        break
                    except Full:
                        pass
        finally:
            try:
                close = getattr(source, "close", None)
                if close is not None:
                    close()
            except Exception as error:  # noqa: BLE001 - no credentials/content in cleanup logs.
                logger.warning("Chat stream cleanup failed error_type=%s", type(error).__name__)
            finally:
                if not self._ready.done():
                    self._ready.set_exception(RuntimeError("Missing stream scope"))
                release()
                self.finished.set()

    async def ready(self):
        try:
            return await asyncio.wait_for(asyncio.wrap_future(self._ready), timeout=10)
        except BaseException:
            self.cancelled.set()
            raise

    async def close(self):
        # Never close a generator concurrently with next(); its owner drains it safely.
        self.cancelled.set()

    async def frames(self, first):
        try:
            yield first
            while True:
                if time.monotonic() - self._started > STREAM_SECONDS:
                    self.cancelled.set()
                    yield error_frame()
                    return
                try:
                    frame = await asyncio.to_thread(self._queue.get, True, HEARTBEAT_SECONDS)
                except Empty:
                    if self.finished.is_set():
                        return
                    yield ": keepalive\n\n"
                    continue
                yield frame
                if self.finished.is_set() and self._queue.empty():
                    return
        finally:
            self.cancelled.set()
