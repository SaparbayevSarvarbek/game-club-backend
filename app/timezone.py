from datetime import date, datetime, timedelta, timezone


UZBEKISTAN_TZ = timezone(timedelta(hours=5), name="Asia/Tashkent")


def now_uz() -> datetime:
    return datetime.now(UZBEKISTAN_TZ).replace(tzinfo=None)


def today_uz() -> date:
    from datetime import time as dt_time
    dt = now_uz()
    if dt.time() < dt_time(4, 0, 0):
        return (dt - timedelta(days=1)).date()
    return dt.date()

