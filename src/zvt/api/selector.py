# -*- coding: utf-8 -*-
import logging

import pandas as pd
from sqlalchemy import or_, and_

from zvt.api.kdata import default_adjust_type, get_kdata_schema, get_latest_kdata_date
from zvt.contract import IntervalLevel, AdjustType
from zvt.contract.api import get_entity_ids
from zvt.domain import DragonAndTiger, Stock1dHfqKdata, Stock, LimitUpInfo, StockQuote, StockQuoteLog
from zvt.utils.pd_utils import pd_is_not_null
from zvt.utils.time_utils import to_pd_timestamp, date_time_by_interval, current_date, now_timestamp

logger = logging.getLogger(__name__)

# 500亿
BIG_CAP = 50000000000
# 150亿
MIDDLE_CAP = 15000000000
# 40亿
SMALL_CAP = 4000000000

# 买入榜单
IN_DEPS = ["dep1", "dep2", "dep3", "dep4", "dep5"]
# 卖出入榜单
OUT_DEPS = ["dep_1", "dep_2", "dep_3", "dep_4", "dep_5"]


def get_entity_ids_by_filter(
    provider="em",
    ignore_delist=True,
    ignore_st=True,
    ignore_new_stock=False,
    target_date=None,
    entity_schema=Stock,
    entity_ids=None,
    ignore_bj=False,
):
    filters = []
    if ignore_new_stock:
        if not target_date:
            target_date = current_date()
        pre_year = date_time_by_interval(target_date, -365)
        filters += [entity_schema.timestamp <= pre_year]
    else:
        if target_date:
            filters += [entity_schema.timestamp <= target_date]
    if ignore_delist:
        filters += [
            entity_schema.name.not_like("%退%"),
            entity_schema.name.not_like("%PT%"),
        ]

    if ignore_st:
        filters += [
            entity_schema.name.not_like("%ST%"),
            entity_schema.name.not_like("%*ST%"),
        ]
    if ignore_bj:
        filters += [entity_schema.exchange != "bj"]

    return get_entity_ids(provider=provider, entity_schema=entity_schema, filters=filters, entity_ids=entity_ids)


def get_limit_up_stocks(timestamp):
    df = LimitUpInfo.query_data(start_timestamp=timestamp, end_timestamp=timestamp, columns=[LimitUpInfo.entity_id])
    if pd_is_not_null(df):
        return df["entity_id"].tolist()


def get_dragon_and_tigger_player(start_timestamp, end_timestamp=None, direction="in"):
    assert direction in ("in", "out")

    filters = None
    if direction == "in":
        filters = [DragonAndTiger.change_pct > 0]
        columns = ["dep1", "dep2", "dep3"]
    elif direction == "out":
        filters = [DragonAndTiger.change_pct > 0]
        columns = ["dep_1", "dep_2", "dep_3"]

    df = DragonAndTiger.query_data(start_timestamp=start_timestamp, end_timestamp=end_timestamp, filters=filters)
    counts = []
    for col in columns:
        counts.append(df[[col, f"{col}_rate"]].groupby(col).count().sort_values(f"{col}_rate", ascending=False))
    return counts


def get_big_players(start_timestamp, end_timestamp=None, count=40):
    dep1, dep2, dep3 = get_dragon_and_tigger_player(start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    # 榜1前40
    bang1 = dep1.index.tolist()[:count]

    # 榜2前40
    bang2 = dep2.index.tolist()[:count]

    # 榜3前40
    bang3 = dep3.index.tolist()[:count]

    return list(set(bang1 + bang2 + bang3))


def get_player_performance(start_timestamp, end_timestamp=None, days=5, players="机构专用", provider="em", buy_rate=5):
    filters = []
    if isinstance(players, str):
        players = [players]

    if isinstance(players, list):
        for player in players:
            filters.append(
                or_(
                    and_(DragonAndTiger.dep1 == player, DragonAndTiger.dep1_rate >= buy_rate),
                    and_(DragonAndTiger.dep2 == player, DragonAndTiger.dep2_rate >= buy_rate),
                    and_(DragonAndTiger.dep3 == player, DragonAndTiger.dep3_rate >= buy_rate),
                    and_(DragonAndTiger.dep4 == player, DragonAndTiger.dep4_rate >= buy_rate),
                    and_(DragonAndTiger.dep5 == player, DragonAndTiger.dep5_rate >= buy_rate),
                )
            )
    else:
        raise AssertionError("players should be list or str type")

    df = DragonAndTiger.query_data(
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        filters=filters,
        index=["entity_id", "timestamp"],
        provider=provider,
    )
    df = df[~df.index.duplicated(keep="first")]
    records = []
    for entity_id, timestamp in df.index:
        end_date = date_time_by_interval(timestamp, days + round(days + days * 2 / 5 + 30))
        kdata = Stock1dHfqKdata.query_data(
            entity_id=entity_id,
            start_timestamp=timestamp,
            end_timestamp=end_date,
            provider=provider,
            index="timestamp",
        )
        if len(kdata) <= days:
            logger.warning(f"ignore {timestamp} -> end_timestamp: {end_date}")
            break
        close = kdata["close"]
        change_pct = (close[days] - close[0]) / close[0]
        records.append({"entity_id": entity_id, "timestamp": timestamp, f"change_pct": change_pct})
    return pd.DataFrame.from_records(records)


def get_player_success_rate(
    start_timestamp,
    end_timestamp=None,
    intervals=(3, 5, 10, 60),
    players=("机构专用", "东方财富证券股份有限公司拉萨团结路第二证券营业部"),
    provider="em",
):
    records = []
    for player in players:
        record = {"player": player}
        for days in intervals:
            df = get_player_performance(
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                days=days,
                players=player,
                provider=provider,
            )
            rate = len(df[df["change_pct"] > 0]) / len(df)
            record[f"rate_{days}"] = rate
        records.append(record)
    return pd.DataFrame.from_records(records, index="player")


def get_players(entity_id, start_timestamp, end_timestamp, provider="em", direction="in", buy_rate=5):
    columns = ["entity_id", "timestamp"]
    if direction == "in":
        for i in range(5):
            columns.append(f"dep{i + 1}")
            columns.append(f"dep{i + 1}_rate")
    elif direction == "out":
        for i in range(5):
            columns.append(f"dep_{i + 1}")
            columns.append(f"dep_{i + 1}_rate")

    df = DragonAndTiger.query_data(
        entity_id=entity_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        provider=provider,
        columns=columns,
        index=["entity_id", "timestamp"],
    )
    dfs = []
    if direction == "in":
        for i in range(5):
            p_df = df[[f"dep{i + 1}", f"dep{i + 1}_rate"]].copy()
            p_df.columns = ["player", "buy_rate"]
            dfs.append(p_df)
    elif direction == "out":
        for i in range(5):
            p_df = df[[f"dep_{i + 1}", f"dep_{i + 1}_rate"]].copy()
            p_df.columns = ["player", "buy_rate"]
            dfs.append(p_df)

    player_df = pd.concat(dfs, sort=True)
    return player_df.sort_index(level=[0, 1])


def get_good_players(timestamp=current_date(), recent_days=400, intervals=(3, 5, 10)):
    end_timestamp = date_time_by_interval(timestamp, -intervals[-1] - 30)
    # recent year
    start_timestamp = date_time_by_interval(end_timestamp, -recent_days)
    print(f"{start_timestamp} to {end_timestamp}")
    # 最近一年牛x的营业部
    players = get_big_players(start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    logger.info(players)
    df = get_player_success_rate(
        start_timestamp=start_timestamp, end_timestamp=end_timestamp, intervals=intervals, players=players
    )
    good_players = df[(df["rate_3"] > 0.4) & (df["rate_5"] > 0.3) & (df["rate_10"] > 0.3)].index.tolist()
    return good_players


def get_entity_list_by_cap(
    timestamp, cap_start, cap_end, entity_type="stock", provider=None, adjust_type=None, retry_times=20
):
    if not adjust_type:
        adjust_type = default_adjust_type(entity_type=entity_type)

    kdata_schema = get_kdata_schema(entity_type, level=IntervalLevel.LEVEL_1DAY, adjust_type=adjust_type)
    df = kdata_schema.query_data(
        provider=provider,
        filters=[kdata_schema.timestamp == to_pd_timestamp(timestamp)],
        index="entity_id",
    )
    if pd_is_not_null(df):
        df["cap"] = df["turnover"] / df["turnover_rate"]
        df_result = df.copy()
        if cap_start:
            df_result = df_result.loc[(df["cap"] >= cap_start)]
        if cap_end:
            df_result = df_result.loc[(df["cap"] <= cap_end)]
        return df_result.index.tolist()
    else:
        if retry_times == 0:
            return []
        return get_entity_list_by_cap(
            timestamp=date_time_by_interval(timestamp, 1),
            cap_start=cap_start,
            cap_end=cap_end,
            entity_type=entity_type,
            provider=provider,
            adjust_type=adjust_type,
            retry_times=retry_times - 1,
        )


def get_big_cap_stock(timestamp, provider="em"):
    return get_entity_list_by_cap(
        timestamp=timestamp, cap_start=BIG_CAP, cap_end=None, entity_type="stock", provider=provider
    )


def get_middle_cap_stock(timestamp, provider="em"):
    return get_entity_list_by_cap(
        timestamp=timestamp, cap_start=MIDDLE_CAP, cap_end=BIG_CAP, entity_type="stock", provider=provider
    )


def get_small_cap_stock(timestamp, provider="em"):
    return get_entity_list_by_cap(
        timestamp=timestamp, cap_start=SMALL_CAP, cap_end=MIDDLE_CAP, entity_type="stock", provider=provider
    )


def get_mini_cap_stock(timestamp, provider="em"):
    return get_entity_list_by_cap(
        timestamp=timestamp, cap_start=None, cap_end=SMALL_CAP, entity_type="stock", provider=provider
    )


def get_mini_and_small_stock(timestamp, provider="em"):
    return get_entity_list_by_cap(
        timestamp=timestamp, cap_start=None, cap_end=MIDDLE_CAP, entity_type="stock", provider=provider
    )


def get_middle_and_big_stock(timestamp, provider="em"):
    return get_entity_list_by_cap(
        timestamp=timestamp, cap_start=MIDDLE_CAP, cap_end=None, entity_type="stock", provider=provider
    )


def get_limit_up_today():
    df = StockQuote.query_data(filters=[StockQuote.is_limit_up], columns=[StockQuote.entity_id])
    if pd_is_not_null(df):
        return df["entity_id"].to_list()


def get_top_up_today(n=100):
    df = StockQuote.query_data(columns=[StockQuote.entity_id], order=StockQuote.change_pct.desc(), limit=n)
    if pd_is_not_null(df):
        return df["entity_id"].to_list()


def get_shoot_today(up_change_pct=0.03, down_change_pct=-0.03, interval=2):
    current_time = now_timestamp()
    latest = StockQuoteLog.query_data(
        columns=[StockQuoteLog.time], return_type="df", limit=1, order=StockQuoteLog.time.desc()
    )
    latest_time = int(latest["time"][0])
    print(latest_time)

    delay = (current_time - latest_time) / (60 * 1000)
    if delay > 2:
        logger.warning(f"delay {delay} minutes")

    # interval minutes
    start_time = latest_time - (interval * 60 * 1000)
    filters = [StockQuoteLog.time > start_time]
    df = StockQuoteLog.query_data(
        filters=filters, columns=[StockQuoteLog.entity_id, StockQuoteLog.time, StockQuoteLog.price], return_type="df"
    )
    if pd_is_not_null(df):
        df.sort_values(by=["entity_id", "time"], inplace=True)

        g_df = df.groupby("entity_id").agg(
            first_price=("price", "first"),
            last_price=("price", "last"),
            last_time=("time", "last"),
            change_pct=("price", lambda x: (x.iloc[-1] - x.iloc[0]) / x.iloc[0]),
        )
        print(g_df.sort_values(by=["change_pct"]))
        up = g_df[g_df["change_pct"] > up_change_pct]
        down = g_df[g_df["change_pct"] < down_change_pct]
        return up.index.tolist(), down.index.tolist()


def get_top_vol(
    entity_ids,
    target_date=None,
    limit=500,
    provider="qmt",
):
    if provider == "qmt":
        df = StockQuote.query_data(
            entity_ids=entity_ids,
            columns=[StockQuote.entity_id],
            order=StockQuote.turnover.desc(),
            limit=limit,
        )
        return df["entity_id"].to_list()
    else:
        if not target_date:
            target_date = get_latest_kdata_date(provider="em", entity_type="stock", adjust_type=AdjustType.hfq)
        df = Stock1dHfqKdata.query_data(
            provider="em",
            filters=[Stock1dHfqKdata.timestamp == to_pd_timestamp(target_date)],
            entity_ids=entity_ids,
            columns=[Stock1dHfqKdata.entity_id],
            order=Stock1dHfqKdata.turnover.desc(),
            limit=limit,
        )
        return df["entity_id"].to_list()


def get_top_down_today(n=100):
    df = StockQuote.query_data(columns=[StockQuote.entity_id], order=StockQuote.change_pct.asc(), limit=n)
    if pd_is_not_null(df):
        return df["entity_id"].to_list()


def get_limit_down_today():
    df = StockQuote.query_data(filters=[StockQuote.is_limit_down], columns=[StockQuote.entity_id])
    if pd_is_not_null(df):
        return df["entity_id"].to_list()


def get_high_days_count(entity_ids=None, target_date=current_date(), days=10):
    recent_days = date_time_by_interval(target_date, -days)
    df = LimitUpInfo.query_data(
        entity_ids=entity_ids,
        start_timestamp=recent_days,
        columns=[LimitUpInfo.timestamp, LimitUpInfo.entity_id, LimitUpInfo.high_days, LimitUpInfo.high_days_count],
    )
    df_sorted = df.sort_values(by=["entity_id", "timestamp"])
    df_latest = df_sorted.drop_duplicates(subset="entity_id", keep="last").reset_index(drop=True)

    entity_id_to_high_days_map = df_latest.set_index("entity_id")["high_days"].to_dict()
    return entity_id_to_high_days_map


if __name__ == "__main__":
    # stocks = get_top_vol(entity_ids=None, provider="em")
    # assert len(stocks) == 500
    print(get_high_days_count())


# the __all__ is generated
__all__ = [
    "get_entity_ids_by_filter",
    "get_limit_up_stocks",
    "get_dragon_and_tigger_player",
    "get_big_players",
    "get_player_performance",
    "get_player_success_rate",
    "get_players",
    "get_good_players",
    "get_entity_list_by_cap",
    "get_big_cap_stock",
    "get_middle_cap_stock",
    "get_small_cap_stock",
    "get_mini_cap_stock",
    "get_mini_and_small_stock",
    "get_middle_and_big_stock",
]
