"""Flood-control retry helper for Telegram calls."""
import asyncio

from telegram.error import RetryAfter


def _wait_secs(e: RetryAfter) -> float:
    ra = e.retry_after
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


async def with_retry(fn, tries: int = 6):
    """fn() ek coroutine banata hai. RetryAfter (flood) par ruk kar dobara try."""
    for i in range(tries):
        try:
            return await fn()
        except RetryAfter as e:
            if i == tries - 1:
                raise
            await asyncio.sleep(min(_wait_secs(e), 60) + 1)
