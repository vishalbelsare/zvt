# -*- coding: utf-8 -*-
import calendar
import datetime

import arrow
import pandas as pd

from zvt.common.query_models import TimeUnit

CHINA_TZ = "Asia/Shanghai"

TIME_FORMAT_ISO8601 = "YYYY-MM-DDTHH:mm:ss.SSS"

TIME_FORMAT_MON = "YYYY-MM"

TIME_FORMAT_DAY = "YYYY-MM-DD"

TIME_FORMAT_DAY1 = "YYYYMMDD"

TIME_FORMAT_MINUTE = "YYYYMMDDHHmm"

TIME_FORMAT_SECOND = "YYYYMMDDHHmmss"

TIME_FORMAT_MINUTE1 = "HH:mm"

TIME_FORMAT_MINUTE2 = "YYYY-MM-DD HH:mm:ss"


# ms(int) or second(float) or str
def to_pd_timestamp(the_time) -> pd.Timestamp:
    if the_time is None:
        return None
    if type(the_time) == int:
        return pd.Timestamp.fromtimestamp(the_time / 1000)

    if type(the_time) == float:
        return pd.Timestamp.fromtimestamp(the_time)

    return pd.Timestamp(the_time)


def get_local_timezone():
    now = datetime.datetime.now()
    local_now = now.astimezone()
    local_tz = local_now.tzinfo
    return local_tz


def to_timestamp(the_time):
    return int(to_pd_timestamp(the_time).tz_localize(get_local_timezone()).timestamp() * 1000)


def now_timestamp():
    return int(pd.Timestamp.utcnow().timestamp() * 1000)


def now_pd_timestamp() -> pd.Timestamp:
    return pd.Timestamp.now()


def today() -> pd.Timestamp:
    return pd.Timestamp.today()


def current_date() -> pd.Timestamp:
    return to_pd_timestamp(today().date())


def tomorrow_date():
    return to_pd_timestamp(date_time_by_interval(today(), 1).date())


def to_time_str(the_time, fmt=TIME_FORMAT_DAY):
    try:
        return arrow.get(to_pd_timestamp(the_time)).format(fmt)
    except Exception as e:
        return the_time


def now_time_str(fmt=TIME_FORMAT_DAY):
    return to_time_str(the_time=now_pd_timestamp(), fmt=fmt)


def recent_year_date():
    return date_time_by_interval(current_date(), -365)


def date_time_by_interval(the_time, interval=1, unit: TimeUnit = TimeUnit.day):
    time_delta = None
    if unit == TimeUnit.year:
        time_delta = datetime.timedelta(days=interval * 365)
    elif unit == TimeUnit.month:
        time_delta = datetime.timedelta(days=interval * 30)
    elif unit == TimeUnit.day:
        time_delta = datetime.timedelta(days=interval)
    elif unit == TimeUnit.minute:
        time_delta = datetime.timedelta(minutes=interval)
    elif unit == TimeUnit.second:
        time_delta = datetime.timedelta(seconds=interval)

    return to_pd_timestamp(the_time) + time_delta


def pre_month(t=now_pd_timestamp()):
    t = to_pd_timestamp(t)
    t = t.replace(day=1)
    if t.month > 1:
        year = t.year
        month = t.month - 1
    else:
        year = t.year - 1
        month = 12
    last_valid_date = t.replace(year=year, month=month)
    return last_valid_date


def pre_month_start_date(t=current_date()):
    return month_start_date(pre_month(t))


def pre_month_end_date(t=current_date()):
    return month_end_date(pre_month(t))


def month_start_date(the_date):
    the_date = to_pd_timestamp(the_date)
    return the_date.replace(day=1)


def month_end_date(the_date):
    the_date = to_pd_timestamp(the_date)

    _, day = calendar.monthrange(the_date.year, the_date.month)
    return the_date.replace(day=day)


def month_start_end_ranges(start_date, end_date):
    days = pd.date_range(start=start_date, end=end_date, freq="M")
    return [(month_start_date(d), month_end_date(d)) for d in days]


def is_same_date(one, two):
    return to_pd_timestamp(one).date() == to_pd_timestamp(two).date()


def is_same_time(one, two):
    return to_timestamp(one) == to_timestamp(two)


def get_year_quarter(time):
    time = to_pd_timestamp(time)
    return time.year, ((time.month - 1) // 3) + 1


def day_offset_today(offset=0):
    return now_pd_timestamp() + datetime.timedelta(days=offset)


def get_year_quarters(start, end=pd.Timestamp.now()):
    start_year_quarter = get_year_quarter(start)
    current_year_quarter = get_year_quarter(end)
    if current_year_quarter[0] == start_year_quarter[0]:
        return [(current_year_quarter[0], x) for x in range(start_year_quarter[1], current_year_quarter[1] + 1)]
    elif current_year_quarter[0] - start_year_quarter[0] == 1:
        return [(start_year_quarter[0], x) for x in range(start_year_quarter[1], 5)] + [
            (current_year_quarter[0], x) for x in range(1, current_year_quarter[1] + 1)
        ]
    elif current_year_quarter[0] - start_year_quarter[0] > 1:
        return (
            [(start_year_quarter[0], x) for x in range(start_year_quarter[1], 5)]
            + [(x, y) for x in range(start_year_quarter[0] + 1, current_year_quarter[0]) for y in range(1, 5)]
            + [(current_year_quarter[0], x) for x in range(1, current_year_quarter[1] + 1)]
        )
    else:
        raise Exception("wrong start time:{}".format(start))


def date_and_time(the_date, the_time):
    time_str = "{}T{}:00.000".format(to_time_str(the_date), the_time)

    return to_pd_timestamp(time_str)


def split_time_interval(start, end, method=None, interval=30, freq="D"):
    start = to_pd_timestamp(start)
    end = to_pd_timestamp(end)
    if not method:
        while start < end:
            interval_end = min(date_time_by_interval(start, interval), end)
            yield pd.date_range(start=start, end=interval_end, freq=freq)
            start = date_time_by_interval(interval_end, 1)

    if method == "month":
        while start <= end:
            _, day = calendar.monthrange(start.year, start.month)

            interval_end = min(to_pd_timestamp(f"{start.year}-{start.month}-{day}"), end)
            yield pd.date_range(start=start, end=interval_end, freq=freq)
            start = date_time_by_interval(interval_end, 1)


def count_interval(start_date, end_date):
    start_date = to_pd_timestamp(start_date)
    end_date = to_pd_timestamp(end_date)
    delta = end_date - start_date
    return delta.days


if __name__ == "__main__":
    print(tomorrow_date() > date_time_by_interval(today(), 2))
# the __all__ is generated
__all__ = [
    "CHINA_TZ",
    "TIME_FORMAT_ISO8601",
    "TIME_FORMAT_MON",
    "TIME_FORMAT_DAY",
    "TIME_FORMAT_DAY1",
    "TIME_FORMAT_MINUTE",
    "TIME_FORMAT_SECOND",
    "TIME_FORMAT_MINUTE1",
    "TIME_FORMAT_MINUTE2",
    "to_pd_timestamp",
    "get_local_timezone",
    "to_timestamp",
    "now_timestamp",
    "now_pd_timestamp",
    "today",
    "current_date",
    "tomorrow_date",
    "to_time_str",
    "now_time_str",
    "recent_year_date",
    "date_time_by_interval",
    "pre_month",
    "pre_month_start_date",
    "pre_month_end_date",
    "month_start_date",
    "month_end_date",
    "month_start_end_ranges",
    "is_same_date",
    "is_same_time",
    "get_year_quarter",
    "day_offset_today",
    "get_year_quarters",
    "date_and_time",
    "split_time_interval",
    "count_interval",
]
