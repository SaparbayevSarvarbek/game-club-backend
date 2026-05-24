from datetime import date, datetime, timedelta, timezone


UZBEKISTAN_TZ = timezone(timedelta(hours=5), name="Asia/Tashkent")


def now_uz() -> datetime:
    return datetime.now(UZBEKISTAN_TZ).replace(tzinfo=None)


def today_uz() -> date:
    return now_uz().date()
