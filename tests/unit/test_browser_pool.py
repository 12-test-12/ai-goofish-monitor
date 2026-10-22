import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from src.services.browser_pool import browser_slot


def test_browser_capacity_is_shared_and_cancelled_waiters_do_not_leak(tmp_path):
    async def scenario():
        active = maximum = 0

        async def job():
            nonlocal active, maximum
            async with browser_slot(limit=2, directory=tmp_path):
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.wait_for(asyncio.gather(*(job() for _ in range(5))), timeout=3)
        assert maximum == 2
        async with browser_slot(limit=1, directory=tmp_path):
            waiter = asyncio.create_task(job_one())
            await asyncio.sleep(0.02)
            assert not waiter.done()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        await asyncio.wait_for(job_one(), timeout=1)

    async def job_one():
        async with browser_slot(limit=1, directory=tmp_path):
            pass

    asyncio.run(scenario())


def test_browser_slot_recovers_when_another_process_is_killed(tmp_path):
    script = "from src.infrastructure.file_lock import FileLock; import sys; lock=FileLock(sys.argv[1]); lock.acquire(); print('locked', flush=True); sys.stdin.readline()"
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path / "browser-0.lock")],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    async def take_slot():
        async with browser_slot(limit=1, directory=tmp_path):
            pass

    async def scenario():
        ready = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=5)
        assert ready.strip() == "locked"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(take_slot(), timeout=0.1)
        process.kill()
        await asyncio.to_thread(process.wait)
        await asyncio.wait_for(take_slot(), timeout=2)

    try:
        asyncio.run(scenario())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
