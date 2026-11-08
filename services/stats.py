"""Stats for the Telegram 'Stats' screen."""
import db


async def collect() -> dict:
    """Numbers for the 📊 Stats screen."""
    days = await db.views_last_days(7)
    return {
        "files": await db.count_files(),
        "views_today": days[0][1],
        "views_total": await db.views_total(),
        "last_days": [{"day": d, "views": v} for d, v in days],
    }
