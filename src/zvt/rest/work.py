# -*- coding: utf-8 -*-
from typing import List, Optional

from fastapi import APIRouter

import zvt.contract.api as contract_api
import zvt.tag.tag_service as tag_service
from zvt.domain import Stock
from zvt.tag.common import TagType
from zvt.tag.tag_models import (
    TagInfoModel,
    CreateTagInfoModel,
    StockTagsModel,
    SimpleStockTagsModel,
    SetStockTagsModel,
    CreateStockPoolInfoModel,
    StockPoolInfoModel,
    CreateStockPoolsModel,
    StockPoolsModel,
    QueryStockTagStatsModel,
    StockTagStatsModel,
    QueryStockTagsModel,
    QuerySimpleStockTagsModel,
    ActivateSubTagsResultModel,
    ActivateSubTagsModel,
    BatchSetStockTagsModel,
    StockTagOptions,
    MainTagIndustryRelation,
    MainTagSubTagRelation,
    IndustryInfoModel,
    ChangeMainTagModel,
)
from zvt.tag.tag_schemas import (
    StockTags,
    MainTagInfo,
    SubTagInfo,
    HiddenTagInfo,
    StockPoolInfo,
    StockPools,
    IndustryInfo,
)
from zvt.utils.time_utils import current_date

work_router = APIRouter(
    prefix="/api/work",
    tags=["work"],
    responses={404: {"description": "Not found"}},
)


@work_router.post("/create_stock_pool_info", response_model=StockPoolInfoModel)
def create_stock_pool_info(create_stock_pool_info_model: CreateStockPoolInfoModel):
    return tag_service.build_stock_pool_info(create_stock_pool_info_model, timestamp=current_date())


@work_router.get("/get_stock_pool_info", response_model=List[StockPoolInfoModel])
def get_stock_pool_info():
    with contract_api.DBSession(provider="zvt", data_schema=StockPoolInfo)() as session:
        stock_pool_info: List[StockPoolInfo] = StockPoolInfo.query_data(session=session, return_type="domain")
        return stock_pool_info


@work_router.post("/create_stock_pools", response_model=StockPoolsModel)
def create_stock_pools(create_stock_pools_model: CreateStockPoolsModel):
    return tag_service.build_stock_pool(create_stock_pools_model, current_date())


@work_router.get("/get_stock_pools", response_model=Optional[StockPoolsModel])
def get_stock_pools(stock_pool_name: str):
    with contract_api.DBSession(provider="zvt", data_schema=StockPools)() as session:
        stock_pools: List[StockPools] = StockPools.query_data(
            session=session,
            filters=[StockPools.stock_pool_name == stock_pool_name],
            order=StockPools.timestamp.desc(),
            limit=1,
            return_type="domain",
        )
        if stock_pools:
            return stock_pools[0]
        return None


@work_router.get("/get_main_tag_info", response_model=List[TagInfoModel])
def get_main_tag_info():
    """
    Get main_tag info
    """
    with contract_api.DBSession(provider="zvt", data_schema=MainTagInfo)() as session:
        tags_info: List[MainTagInfo] = MainTagInfo.query_data(session=session, return_type="domain")
        return tags_info


@work_router.get("/get_sub_tag_info", response_model=List[TagInfoModel])
def get_sub_tag_info():
    """
    Get sub_tag info
    """
    with contract_api.DBSession(provider="zvt", data_schema=SubTagInfo)() as session:
        tags_info: List[SubTagInfo] = SubTagInfo.query_data(session=session, return_type="domain")
        return tags_info


@work_router.get("/get_main_tag_sub_tag_relation", response_model=MainTagSubTagRelation)
def get_main_tag_sub_tag_relation(main_tag):
    return tag_service.get_main_tag_sub_tag_relation(main_tag=main_tag)


@work_router.get("/get_industry_info", response_model=List[IndustryInfoModel])
def get_industry_info():
    """
    Get sub_tag info
    """
    with contract_api.DBSession(provider="zvt", data_schema=IndustryInfo)() as session:
        industry_info: List[IndustryInfo] = IndustryInfo.query_data(session=session, return_type="domain")
        return industry_info


@work_router.get("/get_main_tag_industry_relation", response_model=MainTagIndustryRelation)
def get_main_tag_industry_relation(main_tag):
    return tag_service.get_main_tag_industry_relation(main_tag=main_tag)


@work_router.get("/get_hidden_tag_info", response_model=List[TagInfoModel])
def get_hidden_tag_info():
    """
    Get hidden_tag info
    """
    with contract_api.DBSession(provider="zvt", data_schema=MainTagInfo)() as session:
        tags_info: List[HiddenTagInfo] = HiddenTagInfo.query_data(session=session, return_type="domain")
        return tags_info


@work_router.post("/create_main_tag_info", response_model=TagInfoModel)
def create_main_tag_info(tag_info: CreateTagInfoModel):
    return tag_service.build_tag_info(tag_info, tag_type=TagType.main_tag)


@work_router.post("/create_sub_tag_info", response_model=TagInfoModel)
def create_sub_tag_info(tag_info: CreateTagInfoModel):
    return tag_service.build_tag_info(tag_info, TagType.sub_tag)


@work_router.post("/create_hidden_tag_info", response_model=TagInfoModel)
def create_hidden_tag_info(tag_info: CreateTagInfoModel):
    return tag_service.build_tag_info(tag_info, TagType.hidden_tag)


@work_router.post("/query_stock_tags", response_model=List[StockTagsModel])
def query_stock_tags(query_stock_tags_model: QueryStockTagsModel):
    """
    Get entity tags
    """
    filters = [StockTags.entity_id.in_(query_stock_tags_model.entity_ids)]

    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        tags: List[StockTags] = StockTags.query_data(
            session=session, filters=filters, return_type="domain", order=StockTags.timestamp.desc()
        )
        tags_dict = {tag.entity_id: tag for tag in tags}
        sorted_tags = [tags_dict[entity_id] for entity_id in query_stock_tags_model.entity_ids]
        return sorted_tags


@work_router.post("/query_simple_stock_tags", response_model=List[SimpleStockTagsModel])
def query_simple_stock_tags(query_simple_stock_tags_model: QuerySimpleStockTagsModel):
    """
    Get simple entity tags
    """

    entity_ids = query_simple_stock_tags_model.entity_ids

    filters = [StockTags.entity_id.in_(entity_ids)]
    with contract_api.DBSession(provider="zvt", data_schema=StockTags)() as session:
        tags: List[dict] = StockTags.query_data(
            session=session, filters=filters, return_type="dict", order=StockTags.timestamp.desc()
        )
        entity_tag_map = {item["entity_id"]: item for item in tags}
        result_tags = []
        stocks = Stock.query_data(provider="em", entity_ids=[tag["entity_id"] for tag in tags], return_type="domain")
        stocks_map = {item.entity_id: item for item in stocks}
        for entity_id in entity_ids:
            tag = entity_tag_map.get(entity_id)
            tag["name"] = stocks_map.get(entity_id).name
            if stocks_map.get(entity_id).controlling_holder_parent:
                tag["controlling_holder_parent"] = stocks_map.get(entity_id).controlling_holder_parent
            else:
                tag["controlling_holder_parent"] = stocks_map.get(entity_id).controlling_holder
            tag["top_ten_ratio"] = stocks_map.get(entity_id).top_ten_ratio
            result_tags.append(tag)
        return result_tags


@work_router.get("/get_stock_tag_options", response_model=StockTagOptions)
def get_stock_tag_options(entity_id: str):
    """
    Get stock tag options
    """
    return tag_service.get_stock_tag_options(entity_id=entity_id)


@work_router.post("/set_stock_tags", response_model=StockTagsModel)
def set_stock_tags(set_stock_tags_model: SetStockTagsModel):
    """
    Set stock tags
    """
    return tag_service.build_stock_tags(
        set_stock_tags_model=set_stock_tags_model, timestamp=current_date(), set_by_user=True
    )


@work_router.post("/build_stock_tags", response_model=List[StockTagsModel])
def build_stock_tags(set_stock_tags_model_list: List[SetStockTagsModel]):
    """
    Set stock tags in batch
    """
    return [
        tag_service.build_stock_tags(
            set_stock_tags_model=set_stock_tags_model, timestamp=current_date(), set_by_user=True
        )
        for set_stock_tags_model in set_stock_tags_model_list
    ]


@work_router.post("/query_stock_tag_stats", response_model=List[StockTagStatsModel])
def query_stock_tag_stats(query_stock_tag_stats_model: QueryStockTagStatsModel):
    """
    Get stock tag stats
    """

    return tag_service.query_stock_tag_stats(query_stock_tag_stats_model=query_stock_tag_stats_model)


@work_router.post("/activate_sub_tags", response_model=ActivateSubTagsResultModel)
def activate_sub_tags(activate_sub_tags_model: ActivateSubTagsModel):
    """
    Activate sub tags
    """

    return tag_service.activate_sub_tags(activate_sub_tags_model=activate_sub_tags_model)


@work_router.post("/batch_set_stock_tags", response_model=List[StockTagsModel])
def batch_set_stock_tags(batch_set_stock_tags_model: BatchSetStockTagsModel):
    return tag_service.batch_set_stock_tags(batch_set_stock_tags_model=batch_set_stock_tags_model)


@work_router.post("/build_main_tag_industry_relation", response_model=str)
def build_main_tag_industry_relation(relation: MainTagIndustryRelation):
    tag_service.build_main_tag_industry_relation(main_tag_industry_relation=relation)
    tag_service.activate_industry_list(industry_list=relation.industry_list)
    return "success"


@work_router.post("/build_main_tag_sub_tag_relation", response_model=str)
def build_main_tag_sub_tag_relation(relation: MainTagSubTagRelation):
    tag_service.build_main_tag_sub_tag_relation(main_tag_sub_tag_relation=relation)
    # tag_service.activate_sub_tags(activate_sub_tags_model=ActivateSubTagsModel(sub_tags=relation.sub_tag_list))
    return "success"


@work_router.post("/change_main_tag", response_model=List[StockTagsModel])
def change_main_tag(change_main_tag_model: ChangeMainTagModel):
    return tag_service.change_main_tag(change_main_tag_model=change_main_tag_model)
