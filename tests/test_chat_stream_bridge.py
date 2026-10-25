import asyncio
from threading import Event

import pytest

from job_hunting_agent.chat_stream import StreamWorker, MAX_FRAME_BYTES


def test_one_frame_queue_applies_backpressure_and_worker_owns_release():
    produced, released, closed = [], Event(), Event()
    def source(cancelled):
        try:
            for i in range(20):
                produced.append(i)
                yield str(i)
        finally:
            closed.set()
    async def scenario():
        worker = StreamWorker(source, released.set)
        assert await worker.ready() == "0"
        await asyncio.sleep(0.05)
        assert len(produced) <= 3 and not released.is_set()
        await worker.close()
        assert await asyncio.to_thread(worker.finished.wait, 2)
        assert closed.is_set() and released.is_set()
    asyncio.run(scenario())


def test_oversized_first_frame_rejects_without_leaking_admission_slot():
    released = Event()
    async def scenario():
        worker = StreamWorker(lambda cancelled: iter(["x" * (MAX_FRAME_BYTES + 1)]), released.set)
        with pytest.raises(ValueError):
            await worker.ready()
        assert await asyncio.to_thread(worker.finished.wait, 2)
        assert released.is_set()
    asyncio.run(scenario())
