# -*- coding: utf-8 -*-
import logging
from typing import List

import pandas as pd
from fastapi import HTTPException
from sqlalchemy import func

import zvt.contract.api as contract_api
from zvt.api.selector import get_entity_ids_by_filter
from zvt.domain import BlockStock, Block, Stock
from zvt.tag.common import TagType, TagStatsQueryType, StockPoolType, InsertMode
from zvt.tag.tag_models import (
    SetStockTagsModel,
    CreateStockPoolInfoModel,
    CreateStockPoolsModel,
    QueryStockTagStatsModel,
    ActivateSubTagsModel,
    BatchSetStockTagsModel,
    TagParameter,
    CreateTagInfoModel,
    StockTagOptions,
    MainTagIndustryRelation,
    MainTagSubTagRelation,
    ChangeMainTagModel,
)
from zvt.tag.tag_schemas import (
    StockTags,
    StockPools,
    StockPoolInfo,
    TagStats,
    StockSystemTags,
    MainTagInfo,
    SubTagInfo,
    HiddenTagInfo,
    IndustryInfo,
)
from zvt.tag.tag_utils import (
    get_sub_tags,
    get_stock_pool_names,
    get_main_tag_by_sub_tag,
    get_main_tag_by_industry,
)
from zvt.utils.time_utils import to_pd_timestamp, to_time_str, current_date, now_pd_timestamp
from zvt.utils.utils import fill_dict, compare_dicts, flatten_list

logger = logging.getLogger(__name__)


def stock_tags_need_update(stock_tags: StockTags, set_stock_tags_model: SetStockTagsModel):
    if (
        stock_tags.main_tag != set_stock_tags_model.main_tag
        or stock_tags.main_tag_reason != set_stock_tags_model.main_tag_reason
        or stock_tags.sub_tag != set_stock_tags_model.sub_tag
        or stock_tags.sub_tag_reason != set_stock_tags_model.sub_tag_reason
        or not compare_dicts(stock_tags.active_hidden_tags, set_stock_tags_model.active_hidden_tags)
    ):
        return True
    return False


def get_stock_tag_options(entity_id):
    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        datas: List[StockTags] = StockTags.query_data(
            entity_id=entity_id, order=StockTags.timestamp.desc(), limit=1, return_type="domain", session=session
        )
        main_tag_options = []
        sub_tag_options = []
        hidden_tag_options = []

        main_tag = None
        sub_tag = None
        active_hidden_tags = None
        stock_tags = None
        if datas:
            stock_tags = datas[0]
            main_tag = stock_tags.main_tag
            sub_tag = stock_tags.sub_tag

            if stock_tags.main_tags:
                main_tag_options = [
                    CreateTagInfoModel(tag=tag, tag_reason=tag_reason)
                    for tag, tag_reason in stock_tags.main_tags.items()
                ]

            if stock_tags.sub_tags:
                sub_tag_options = [
                    CreateTagInfoModel(tag=tag, tag_reason=tag_reason)
                    for tag, tag_reason in stock_tags.sub_tags.items()
                ]

            if stock_tags.active_hidden_tags:
                active_hidden_tags = stock_tags.active_hidden_tags

            if stock_tags.hidden_tags:
                hidden_tag_options = [
                    CreateTagInfoModel(tag=tag, tag_reason=tag_reason)
                    for tag, tag_reason in stock_tags.hidden_tags.items()
                ]

        main_tags_info: List[MainTagInfo] = MainTagInfo.query_data(session=session, return_type="domain")
        if not main_tag:
            main_tag = main_tags_info[0].tag

        main_tag_options = main_tag_options + [
            CreateTagInfoModel(tag=item.tag, tag_reason=item.tag_reason)
            for item in main_tags_info
            if not stock_tags or (not stock_tags.main_tags) or (item.tag not in stock_tags.main_tags)
        ]

        sub_tags_info: List[SubTagInfo] = SubTagInfo.query_data(session=session, return_type="domain")
        if not sub_tag:
            sub_tag = sub_tags_info[0].tag
        sub_tag_options = sub_tag_options + [
            CreateTagInfoModel(tag=item.tag, tag_reason=item.tag_reason)
            for item in sub_tags_info
            if not stock_tags or (not stock_tags.sub_tags) or (item.tag not in stock_tags.sub_tags)
        ]

        hidden_tags_info: List[HiddenTagInfo] = HiddenTagInfo.query_data(session=session, return_type="domain")
        hidden_tag_options = hidden_tag_options + [
            CreateTagInfoModel(tag=item.tag, tag_reason=item.tag_reason)
            for item in hidden_tags_info
            if not stock_tags or (not stock_tags.hidden_tags) or (item.tag not in stock_tags.hidden_tags)
        ]

        return StockTagOptions(
            main_tag=main_tag,
            sub_tag=sub_tag,
            active_hidden_tags=active_hidden_tags,
            main_tag_options=main_tag_options,
            sub_tag_options=sub_tag_options,
            hidden_tag_options=hidden_tag_options,
        )


def build_stock_tags(
    set_stock_tags_model: SetStockTagsModel, timestamp: pd.Timestamp, set_by_user: bool, keep_current=False
):
    logger.info(set_stock_tags_model)

    main_tag_info = CreateTagInfoModel(
        tag=set_stock_tags_model.main_tag, tag_reason=set_stock_tags_model.main_tag_reason
    )
    if not is_tag_info_existed(tag_info=main_tag_info, tag_type=TagType.main_tag):
        build_tag_info(tag_info=main_tag_info, tag_type=TagType.main_tag)

    if set_stock_tags_model.sub_tag:
        sub_tag_info = CreateTagInfoModel(
            tag=set_stock_tags_model.sub_tag, tag_reason=set_stock_tags_model.sub_tag_reason
        )
        if not is_tag_info_existed(tag_info=sub_tag_info, tag_type=TagType.sub_tag):
            build_tag_info(tag_info=sub_tag_info, tag_type=TagType.sub_tag)

    if set_stock_tags_model.active_hidden_tags:
        for tag in set_stock_tags_model.active_hidden_tags:
            hidden_tag_info = CreateTagInfoModel(tag=tag, tag_reason=set_stock_tags_model.active_hidden_tags.get(tag))
            if not is_tag_info_existed(tag_info=hidden_tag_info, tag_type=TagType.hidden_tag):
                build_tag_info(tag_info=hidden_tag_info, tag_type=TagType.hidden_tag)

    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        entity_id = set_stock_tags_model.entity_id
        main_tags = {}
        sub_tags = {}
        hidden_tags = {}
        datas = StockTags.query_data(
            session=session,
            entity_id=entity_id,
            limit=1,
            return_type="domain",
        )

        if datas:
            assert len(datas) == 1
            current_stock_tags: StockTags = datas[0]

            # nothing change
            if not stock_tags_need_update(current_stock_tags, set_stock_tags_model):
                logger.info(f"Not change stock_tags for {set_stock_tags_model.entity_id}")
                return current_stock_tags

            if current_stock_tags.main_tags:
                main_tags = dict(current_stock_tags.main_tags)
            if current_stock_tags.sub_tags:
                sub_tags = dict(current_stock_tags.sub_tags)
            if current_stock_tags.hidden_tags:
                hidden_tags = dict(current_stock_tags.hidden_tags)

        else:
            current_stock_tags = StockTags(
                id=f"{entity_id}_tags",
                entity_id=entity_id,
                timestamp=timestamp,
            )

        # update tag
        if not keep_current:
            current_stock_tags.main_tag = set_stock_tags_model.main_tag
            current_stock_tags.main_tag_reason = set_stock_tags_model.main_tag_reason

            if set_stock_tags_model.sub_tag:
                current_stock_tags.sub_tag = set_stock_tags_model.sub_tag
            if set_stock_tags_model.sub_tag_reason:
                current_stock_tags.sub_tag_reason = set_stock_tags_model.sub_tag_reason
            # could update to None
            current_stock_tags.active_hidden_tags = set_stock_tags_model.active_hidden_tags
        # update tags
        main_tags[set_stock_tags_model.main_tag] = set_stock_tags_model.main_tag_reason
        if set_stock_tags_model.sub_tag:
            sub_tags[set_stock_tags_model.sub_tag] = set_stock_tags_model.sub_tag_reason
        if set_stock_tags_model.active_hidden_tags:
            for k, v in set_stock_tags_model.active_hidden_tags.items():
                hidden_tags[k] = v
        current_stock_tags.main_tags = main_tags
        current_stock_tags.sub_tags = sub_tags
        current_stock_tags.hidden_tags = hidden_tags

        current_stock_tags.set_by_user = set_by_user

        session.add(current_stock_tags)
        session.commit()
        session.refresh(current_stock_tags)
        return current_stock_tags


def build_tag_parameter(tag_type: TagType, tag, tag_reason, stock_tag: StockTags):
    if tag_type == TagType.main_tag:
        main_tag = tag
        if main_tag in stock_tag.main_tags:
            main_tag_reason = stock_tag.main_tags.get(main_tag, tag_reason)
        else:
            main_tag_reason = tag_reason
        sub_tag = stock_tag.sub_tag
        sub_tag_reason = stock_tag.sub_tag_reason
    elif tag_type == TagType.sub_tag:
        sub_tag = tag
        if sub_tag in stock_tag.sub_tags:
            sub_tag_reason = stock_tag.sub_tags.get(sub_tag, tag_reason)
        else:
            sub_tag_reason = tag_reason
        main_tag = stock_tag.main_tag
        main_tag_reason = stock_tag.main_tag_reason
    else:
        assert False

    return TagParameter(
        main_tag=main_tag, main_tag_reason=main_tag_reason, sub_tag=sub_tag, sub_tag_reason=sub_tag_reason
    )


def batch_set_stock_tags(batch_set_stock_tags_model: BatchSetStockTagsModel):
    if not batch_set_stock_tags_model.entity_ids:
        return []

    tag_info = CreateTagInfoModel(tag=batch_set_stock_tags_model.tag, tag_reason=batch_set_stock_tags_model.tag_reason)
    if not is_tag_info_existed(tag_info=tag_info, tag_type=batch_set_stock_tags_model.tag_type):
        build_tag_info(tag_info=tag_info, tag_type=batch_set_stock_tags_model.tag_type)

    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        tag_type = batch_set_stock_tags_model.tag_type
        if tag_type == TagType.main_tag:
            main_tag = batch_set_stock_tags_model.tag
            stock_tags: List[StockTags] = StockTags.query_data(
                entity_ids=batch_set_stock_tags_model.entity_ids,
                filters=[StockTags.main_tag != main_tag],
                session=session,
                return_type="domain",
            )
        elif tag_type == TagType.sub_tag:
            sub_tag = batch_set_stock_tags_model.tag
            stock_tags: List[StockTags] = StockTags.query_data(
                entity_ids=batch_set_stock_tags_model.entity_ids,
                filters=[StockTags.sub_tag != sub_tag],
                session=session,
                return_type="domain",
            )

        for stock_tag in stock_tags:
            tag_parameter: TagParameter = build_tag_parameter(
                tag_type=tag_type,
                tag=batch_set_stock_tags_model.tag,
                tag_reason=batch_set_stock_tags_model.tag_reason,
                stock_tag=stock_tag,
            )
            set_stock_tags_model = SetStockTagsModel(
                entity_id=stock_tag.entity_id,
                main_tag=tag_parameter.main_tag,
                main_tag_reason=tag_parameter.main_tag_reason,
                sub_tag=tag_parameter.sub_tag,
                sub_tag_reason=tag_parameter.sub_tag_reason,
                active_hidden_tags=stock_tag.active_hidden_tags,
            )

            build_stock_tags(
                set_stock_tags_model=set_stock_tags_model,
                timestamp=now_pd_timestamp(),
                set_by_user=True,
                keep_current=False,
            )
            session.refresh(stock_tag)
        return stock_tags


def build_default_main_tag(entity_ids=None, force_rebuild=False):
    """
    build default main tag by industry

    :param entity_ids: entity ids
    :param force_rebuild: always rebuild it if True otherwise only build which not existed
    """
    if not entity_ids:
        entity_ids = get_entity_ids_by_filter(
            provider="em", ignore_delist=True, ignore_st=False, ignore_new_stock=False
        )

    df_block = Block.query_data(provider="em", filters=[Block.category == "industry"])
    industry_codes = df_block["code"].tolist()
    block_stocks: List[BlockStock] = BlockStock.query_data(
        provider="em",
        filters=[BlockStock.code.in_(industry_codes), BlockStock.stock_id.in_(entity_ids)],
        return_type="domain",
    )
    entity_id_block_mapping = {block_stock.stock_id: block_stock for block_stock in block_stocks}

    for entity_id in entity_ids:
        stock_tags: List[StockTags] = StockTags.query_data(entity_id=entity_id, return_type="domain")
        if not force_rebuild and stock_tags:
            logger.info(f"{entity_id} main tag has been set.")
            continue

        logger.info(f"build main tag for: {entity_id}")

        block_stock: BlockStock = entity_id_block_mapping.get(entity_id)
        if block_stock:
            main_tag = get_main_tag_by_industry(industry_name=block_stock.name)
            main_tag_reason = f"来自行业:{block_stock.name}"
        else:
            main_tag = "其他"
            main_tag_reason = "其他"

        build_stock_tags(
            set_stock_tags_model=SetStockTagsModel(
                entity_id=entity_id,
                main_tag=main_tag,
                main_tag_reason=main_tag_reason,
                sub_tag=None,
                sub_tag_reason=None,
                active_hidden_tags=None,
            ),
            timestamp=now_pd_timestamp(),
            set_by_user=False,
            keep_current=False,
        )


def build_default_sub_tags(entity_ids=None):
    if not entity_ids:
        entity_ids = get_entity_ids_by_filter(
            provider="em", ignore_delist=True, ignore_st=False, ignore_new_stock=False
        )

    for entity_id in entity_ids:
        logger.info(f"build sub tag for: {entity_id}")
        datas = StockTags.query_data(entity_id=entity_id, limit=1, return_type="domain")
        if not datas:
            raise AssertionError(f"Main tag must be set at first for {entity_id}")

        current_stock_tags: StockTags = datas[0]
        keep_current = False
        if current_stock_tags.set_by_user:
            logger.info(f"keep current tags set by user for: {entity_id}")
            keep_current = True

        current_sub_tag = current_stock_tags.sub_tag
        filters = [BlockStock.stock_id == entity_id]
        if current_sub_tag:
            logger.info(f"{entity_id} current_sub_tag: {current_sub_tag}")
            current_sub_tags = current_stock_tags.sub_tags.keys()
            filters = filters + [BlockStock.name.notin_(current_sub_tags)]

        df_block = Block.query_data(provider="em", filters=[Block.category == "concept"])
        concept_codes = df_block["code"].tolist()
        filters = filters + [BlockStock.code.in_(concept_codes)]

        block_stocks: List[BlockStock] = BlockStock.query_data(
            provider="em",
            filters=filters,
            return_type="domain",
        )
        if not block_stocks:
            logger.info(f"no block_stocks for: {entity_id}")
            continue

        for block_stock in block_stocks:
            sub_tag = block_stock.name
            if sub_tag in get_sub_tags():
                sub_tag_reason = f"来自概念:{sub_tag}"

                main_tag = get_main_tag_by_sub_tag(sub_tag)
                main_tag_reason = sub_tag_reason
                if (main_tag == "其他" or not main_tag) and current_stock_tags.main_tag:
                    main_tag = current_stock_tags.main_tag
                    main_tag_reason = current_stock_tags.main_tag_reason

                build_stock_tags(
                    set_stock_tags_model=SetStockTagsModel(
                        entity_id=entity_id,
                        main_tag=main_tag,
                        main_tag_reason=main_tag_reason,
                        sub_tag=sub_tag,
                        sub_tag_reason=sub_tag_reason,
                        active_hidden_tags=current_stock_tags.active_hidden_tags,
                    ),
                    timestamp=now_pd_timestamp(),
                    set_by_user=False,
                    keep_current=keep_current,
                )
            else:
                logger.info(f"ignore {sub_tag} not in sub_tag_info yet")


def get_tag_info_schema(tag_type: TagType):
    if tag_type == TagType.main_tag:
        data_schema = MainTagInfo
    elif tag_type == TagType.sub_tag:
        data_schema = SubTagInfo
    elif tag_type == TagType.hidden_tag:
        data_schema = HiddenTagInfo
    else:
        assert False

    return data_schema


def is_tag_info_existed(tag_info: CreateTagInfoModel, tag_type: TagType):
    data_schema = get_tag_info_schema(tag_type=tag_type)
    with contract_api.DBSession(provider="zvt", data_schema=data_schema)() as session:
        current_tags_info = data_schema.query_data(
            session=session, filters=[data_schema.tag == tag_info.tag], return_type="domain"
        )
        if current_tags_info:
            return True
        return False


def build_tag_info(tag_info: CreateTagInfoModel, tag_type: TagType):
    """
    Create tags info
    """
    if is_tag_info_existed(tag_info=tag_info, tag_type=tag_type):
        raise HTTPException(status_code=409, detail=f"This tag has been registered in {tag_type}")

    data_schema = get_tag_info_schema(tag_type=tag_type)
    with contract_api.DBSession(provider="zvt", data_schema=data_schema)() as session:
        timestamp = current_date()
        entity_id = "admin"
        tag_info_db = data_schema(
            id=f"admin_{tag_info.tag}",
            entity_id=entity_id,
            timestamp=timestamp,
            tag=tag_info.tag,
            tag_reason=tag_info.tag_reason,
        )
        session.add(tag_info_db)
        session.commit()
        session.refresh(tag_info_db)
        return tag_info_db


def build_stock_pool_info(create_stock_pool_info_model: CreateStockPoolInfoModel, timestamp):
    with contract_api.DBSession(provider="zvt", data_schema=StockPoolInfo)() as session:
        stock_pool_info = StockPoolInfo(
            entity_id="admin",
            timestamp=to_pd_timestamp(timestamp),
            id=f"admin_{create_stock_pool_info_model.stock_pool_name}",
            stock_pool_type=create_stock_pool_info_model.stock_pool_type.value,
            stock_pool_name=create_stock_pool_info_model.stock_pool_name,
        )
        session.add(stock_pool_info)
        session.commit()
        session.refresh(stock_pool_info)
        return stock_pool_info


def build_stock_pool(create_stock_pools_model: CreateStockPoolsModel, target_date=current_date()):
    with contract_api.DBSession(provider="zvt", data_schema=StockPools)() as session:
        if create_stock_pools_model.stock_pool_name not in get_stock_pool_names():
            build_stock_pool_info(
                CreateStockPoolInfoModel(
                    stock_pool_type=StockPoolType.custom, stock_pool_name=create_stock_pools_model.stock_pool_name
                ),
                timestamp=target_date,
            )
        # one instance per day
        stock_pool_id = f"admin_{to_time_str(target_date)}_{create_stock_pools_model.stock_pool_name}"
        datas: List[StockPools] = StockPools.query_data(
            session=session,
            filters=[
                StockPools.timestamp == to_pd_timestamp(target_date),
                StockPools.stock_pool_name == create_stock_pools_model.stock_pool_name,
            ],
            return_type="domain",
        )
        if datas:
            stock_pool = datas[0]
            if create_stock_pools_model.insert_mode == InsertMode.overwrite:
                stock_pool.entity_ids = create_stock_pools_model.entity_ids
            else:
                stock_pool.entity_ids = list(set(stock_pool.entity_ids + create_stock_pools_model.entity_ids))
        else:
            stock_pool = StockPools(
                entity_id="admin",
                timestamp=to_pd_timestamp(target_date),
                id=stock_pool_id,
                stock_pool_name=create_stock_pools_model.stock_pool_name,
                entity_ids=create_stock_pools_model.entity_ids,
            )
        session.add(stock_pool)
        session.commit()
        session.refresh(stock_pool)
        return stock_pool


def query_stock_tag_stats(query_stock_tag_stats_model: QueryStockTagStatsModel):
    with contract_api.DBSession(provider="zvt", data_schema=TagStats)() as session:
        datas = TagStats.query_data(
            session=session,
            filters=[TagStats.stock_pool_name == query_stock_tag_stats_model.stock_pool_name],
            order=TagStats.timestamp.desc(),
            limit=1,
            return_type="domain",
        )
        if not datas:
            return []

        target_date = datas[0].timestamp

        tag_stats_list: List[dict] = TagStats.query_data(
            session=session,
            filters=[
                TagStats.stock_pool_name == query_stock_tag_stats_model.stock_pool_name,
                TagStats.timestamp == target_date,
            ],
            return_type="dict",
            order=TagStats.position.asc(),
        )

        if query_stock_tag_stats_model.query_type == TagStatsQueryType.simple:
            return tag_stats_list

        entity_ids = flatten_list([tag_stats["entity_ids"] for tag_stats in tag_stats_list])

        # get stocks meta
        stocks = Stock.query_data(provider="em", entity_ids=entity_ids, return_type="domain")
        entity_map = {item.entity_id: item for item in stocks}

        # get stock tags
        tags_dict = StockTags.query_data(
            session=session,
            filters=[StockTags.entity_id.in_(entity_ids)],
            return_type="dict",
        )
        entity_tags_map = {item["entity_id"]: item for item in tags_dict}

        # get stock system tags
        system_tags_dict = StockSystemTags.query_data(
            session=session,
            filters=[StockSystemTags.timestamp == target_date, StockSystemTags.entity_id.in_(entity_ids)],
            return_type="dict",
        )
        entity_system_tags_map = {item["entity_id"]: item for item in system_tags_dict}

        for tag_stats in tag_stats_list:
            stock_details = []
            for entity_id in tag_stats["entity_ids"]:
                stock_details_model = {
                    "entity_id": entity_id,
                    "main_tag": tag_stats["main_tag"],
                    "code": entity_map.get(entity_id).code,
                    "name": entity_map.get(entity_id).name,
                }

                stock_tags = entity_tags_map.get(entity_id)
                stock_details_model["sub_tag"] = stock_tags["sub_tag"]
                if stock_tags["active_hidden_tags"] is not None:
                    stock_details_model["hidden_tags"] = stock_tags["active_hidden_tags"].keys()
                else:
                    stock_details_model["hidden_tags"] = None

                stock_system_tags = entity_system_tags_map.get(entity_id)
                stock_details_model = fill_dict(stock_system_tags, stock_details_model)

                stock_details.append(stock_details_model)
            tag_stats["stock_details"] = stock_details

        return tag_stats_list


def refresh_main_tag_by_sub_tag(stock_tag: StockTags, set_by_user=False) -> StockTags:
    if not stock_tag.sub_tags:
        logger.warning(f"{stock_tag.entity_id} has no sub_tags yet")
        return stock_tag

    sub_tag = stock_tag.sub_tag
    sub_tag_reason = stock_tag.sub_tags[sub_tag]

    main_tag = get_main_tag_by_sub_tag(sub_tag)
    main_tag_reason = sub_tag_reason
    if main_tag == "其他":
        main_tag = stock_tag.main_tag
        main_tag_reason = stock_tag.main_tag_reason

    set_stock_tags_model = SetStockTagsModel(
        entity_id=stock_tag.entity_id,
        main_tag=main_tag,
        main_tag_reason=main_tag_reason,
        sub_tag=sub_tag,
        sub_tag_reason=sub_tag_reason,
        active_hidden_tags=stock_tag.active_hidden_tags,
    )
    logger.info(f"set_stock_tags_model:{set_stock_tags_model}")

    return build_stock_tags(
        set_stock_tags_model=set_stock_tags_model,
        timestamp=stock_tag.timestamp,
        set_by_user=set_by_user,
        keep_current=False,
    )


def refresh_all_main_tag_by_sub_tag():
    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        stock_tags = StockTags.query_data(
            session=session,
            return_type="domain",
        )
        for stock_tag in stock_tags:
            refresh_main_tag_by_sub_tag(stock_tag)


def reset_to_default_main_tag(current_main_tag: str):
    df = StockTags.query_data(
        filters=[StockTags.main_tag == current_main_tag],
        columns=[StockTags.entity_id],
        return_type="df",
    )
    entity_ids = df["entity_id"].tolist()
    if not entity_ids:
        logger.info(f"all stocks with main_tag: {current_main_tag} has been reset")
        return
    build_default_main_tag(entity_ids=entity_ids, force_rebuild=True)


def activate_industry_list(industry_list: List[str]):
    df_block = Block.query_data(provider="em", filters=[Block.category == "industry", Block.name.in_(industry_list)])
    industry_codes = df_block["code"].tolist()
    block_stocks: List[BlockStock] = BlockStock.query_data(
        provider="em",
        filters=[BlockStock.code.in_(industry_codes)],
        return_type="domain",
    )
    entity_ids = [block_stock.stock_id for block_stock in block_stocks]

    if not entity_ids:
        logger.info(f"No stocks in {industry_list}")
        return

    build_default_main_tag(entity_ids=entity_ids, force_rebuild=True)


def activate_sub_tags(activate_sub_tags_model: ActivateSubTagsModel):
    sub_tags = activate_sub_tags_model.sub_tags
    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        result = {}
        for sub_tag in sub_tags:
            # df = StockTags.query_data(
            #     session=session,
            #     filters=[StockTags.sub_tag != sub_tag],
            #     columns=[StockTags.entity_id],
            #     return_type="df",
            # )
            # entity_ids = df["entity_id"].tolist()
            entity_ids = None

            # stock_tag with sub_tag but not set to related main_tag yet
            stock_tags = StockTags.query_data(
                session=session,
                entity_ids=entity_ids,
                # 需要sqlite3版本>=3.37.0
                filters=[func.json_extract(StockTags.sub_tags, f'$."{sub_tag}"') != None],
                return_type="domain",
            )
            if not stock_tags:
                logger.info(f"all stocks with sub_tag: {sub_tag} has been activated")
                continue
            for stock_tag in stock_tags:
                stock_tag.sub_tag = sub_tag
                session.commit()
                session.refresh(stock_tag)
                result[stock_tag.entity_id] = refresh_main_tag_by_sub_tag(stock_tag, set_by_user=True)
        return result


def _create_main_tag_if_not_existed(main_tag, main_tag_reason):
    main_tag_info = CreateTagInfoModel(tag=main_tag, tag_reason=main_tag_reason)
    if not is_tag_info_existed(tag_info=main_tag_info, tag_type=TagType.main_tag):
        build_tag_info(tag_info=main_tag_info, tag_type=TagType.main_tag)


def get_main_tag_industry_relation(main_tag):
    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        df = IndustryInfo.query_data(
            session=session,
            columns=[IndustryInfo.industry_name],
            filters=[IndustryInfo.main_tag == main_tag],
            return_type="df",
        )
        return {"main_tag": main_tag, "industry_list": df["industry_name"].tolist()}


def get_main_tag_sub_tag_relation(main_tag):
    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        df = SubTagInfo.query_data(
            session=session,
            columns=[SubTagInfo.tag],
            filters=[SubTagInfo.main_tag == main_tag],
            return_type="df",
        )
        return {"main_tag": main_tag, "sub_tag_list": df["tag"].tolist()}


def build_main_tag_industry_relation(main_tag_industry_relation: MainTagIndustryRelation):
    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        main_tag = main_tag_industry_relation.main_tag
        _create_main_tag_if_not_existed(main_tag=main_tag, main_tag_reason=main_tag)

        industry_list = main_tag_industry_relation.industry_list

        datas: List[IndustryInfo] = IndustryInfo.query_data(
            session=session,
            filters=[IndustryInfo.main_tag == main_tag, IndustryInfo.industry_name.notin_(industry_list)],
            return_type="domain",
        )
        for data in datas:
            data.main_tag = "其他"
        session.commit()

        industry_info_list: List[IndustryInfo] = IndustryInfo.query_data(
            session=session,
            filters=[IndustryInfo.industry_name.in_(industry_list)],
            return_type="domain",
        )
        for industry_info in industry_info_list:
            industry_info.main_tag = main_tag
        session.commit()


def build_main_tag_sub_tag_relation(main_tag_sub_tag_relation: MainTagSubTagRelation):
    with contract_api.DBSession(provider="zvt", data_schema=SubTagInfo)() as session:
        main_tag = main_tag_sub_tag_relation.main_tag
        _create_main_tag_if_not_existed(main_tag=main_tag, main_tag_reason=main_tag)

        sub_tag_list = main_tag_sub_tag_relation.sub_tag_list

        datas: List[SubTagInfo] = SubTagInfo.query_data(
            session=session,
            filters=[SubTagInfo.main_tag == main_tag, SubTagInfo.tag.notin_(sub_tag_list)],
            return_type="domain",
        )
        for data in datas:
            data.main_tag = "其他"
        session.commit()

        sub_tag_info_list: List[SubTagInfo] = SubTagInfo.query_data(
            session=session,
            filters=[SubTagInfo.tag.in_(sub_tag_list)],
            return_type="domain",
        )
        for sub_tag_info in sub_tag_info_list:
            sub_tag_info.main_tag = main_tag
        session.commit()


def change_main_tag(change_main_tag_model: ChangeMainTagModel):
    new_main_tag = change_main_tag_model.new_main_tag
    _create_main_tag_if_not_existed(main_tag=new_main_tag, main_tag_reason=new_main_tag)
    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        stock_tags: List[StockTags] = StockTags.query_data(
            filters=[StockTags.main_tag == change_main_tag_model.current_main_tag],
            session=session,
            return_type="domain",
        )

        for stock_tag in stock_tags:
            tag_parameter: TagParameter = build_tag_parameter(
                tag_type=TagType.main_tag,
                tag=new_main_tag,
                tag_reason=new_main_tag,
                stock_tag=stock_tag,
            )
            set_stock_tags_model = SetStockTagsModel(
                entity_id=stock_tag.entity_id,
                main_tag=tag_parameter.main_tag,
                main_tag_reason=tag_parameter.main_tag_reason,
                sub_tag=tag_parameter.sub_tag,
                sub_tag_reason=tag_parameter.sub_tag_reason,
                active_hidden_tags=stock_tag.active_hidden_tags,
            )

            build_stock_tags(
                set_stock_tags_model=set_stock_tags_model,
                timestamp=now_pd_timestamp(),
                set_by_user=True,
                keep_current=False,
            )
            session.refresh(stock_tag)
        return stock_tags


if __name__ == "__main__":
    activate_industry_list(industry_list=["半导体"])
    # activate_sub_tags(ActivateSubTagsModel(sub_tags=["航天概念", "天基互联", "北斗导航", "通用航空"]))


# the __all__ is generated
__all__ = [
    "stock_tags_need_update",
    "get_stock_tag_options",
    "build_stock_tags",
    "build_tag_parameter",
    "batch_set_stock_tags",
    "build_default_main_tag",
    "build_default_sub_tags",
    "get_tag_info_schema",
    "is_tag_info_existed",
    "build_tag_info",
    "build_stock_pool_info",
    "build_stock_pool",
    "query_stock_tag_stats",
    "refresh_main_tag_by_sub_tag",
    "refresh_all_main_tag_by_sub_tag",
    "reset_to_default_main_tag",
    "activate_industry_list",
    "activate_sub_tags",
]
