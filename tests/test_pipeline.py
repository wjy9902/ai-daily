from datetime import datetime
from zoneinfo import ZoneInfo

from ai_daily.pipeline import collection_window


def test_collection_window_uses_beijing_run_time() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 12, 4, 20, tzinfo=timezone)
    cutoff, run_time = collection_window(now.date(), "Asia/Shanghai", 36, now)
    assert cutoff == datetime(2026, 8, 10, 16, 20, tzinfo=timezone)
    assert run_time == now
