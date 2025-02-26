# -*- coding: utf-8 -*-
import json
import logging
import time
from enum import Enum
from typing import List, Union, Optional, Type

import pandas as pd

from zvt.contract import IntervalLevel
from zvt.contract import zvt_context
from zvt.contract.api import get_data, df_to_db, del_data
from zvt.contract.base_service import EntityStateService
from zvt.contract.reader import DataReader, DataListener
from zvt.contract.schema import Mixin, TradableEntity
from zvt.contract.zvt_info import FactorState
from zvt.utils.pd_utils import pd_is_not_null, drop_continue_duplicate, is_filter_result_df, is_score_result_df
from zvt.utils.str_utils import to_snake_str
from zvt.utils.time_utils import to_pd_timestamp


class TargetType(Enum):
    positive = "positive"
    negative = "negative"
    keep = "keep"


class Indicator(object):
    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.indicators = []


class Transformer(Indicator):
    def __init__(self) -> None:
        super().__init__()

    def transform(self, input_df: pd.DataFrame) -> pd.DataFrame:
        """
        input_df format::

                                      col1    col2    col3    ...
            entity_id    timestamp
                                      1.2     0.5     0.3     ...
                                      1.0     0.7     0.2     ...

        the return result would change the columns and  keep the format

        :param input_df:
        :return:
        """
        g = input_df.groupby(level=0)
        if len(g.groups) == 1:
            entity_id = input_df.index[0][0]

            df = input_df.reset_index(level=0, drop=True)
            ret_df = self.transform_one(entity_id=entity_id, df=df)
            ret_df["entity_id"] = entity_id

            return ret_df.set_index("entity_id", append=True).swaplevel(0, 1)
        else:
            return g.apply(lambda x: self.transform_one(x.index[0][0], x.reset_index(level=0, drop=True)))

    def transform_one(self, entity_id: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        df format::

                         col1    col2    col3    ...
            timestamp
                         1.2     0.5     0.3     ...
                         1.0     0.7     0.2     ...

        the return result would change the columns and  keep the format

        :param entity_id:
        :param df:
        :return:
        """
        return df


class Accumulator(Indicator):
    def __init__(self, acc_window: int = 1) -> None:
        """

        :param acc_window: the window size of acc for computing,default is 1
        """
        super().__init__()
        self.acc_window = acc_window

    def acc(self, input_df: pd.DataFrame, acc_df: pd.DataFrame, states: dict) -> (pd.DataFrame, dict):
        """

        :param input_df: new input
        :param acc_df: previous result
        :param states: current states of the entity
        :return: new result and states
        """
        g = input_df.groupby(level=0)
        if len(g.groups) == 1:
            entity_id = input_df.index[0][0]

            df = input_df.reset_index(level=0, drop=True)
            if pd_is_not_null(acc_df) and (entity_id == acc_df.index[0][0]):
                acc_one_df = acc_df.reset_index(level=0, drop=True)
            else:
                acc_one_df = None
            ret_df, state = self.acc_one(entity_id=entity_id, df=df, acc_df=acc_one_df, state=states.get(entity_id))
            if pd_is_not_null(ret_df):
                ret_df["entity_id"] = entity_id
                ret_df = ret_df.set_index("entity_id", append=True).swaplevel(0, 1)
                ret_df["entity_id"] = entity_id
                return ret_df, {entity_id: state}
            return None, {entity_id: state}
        else:
            new_states = {}

            def cal_acc(x):
                entity_id = x.index[0][0]
                if pd_is_not_null(acc_df):
                    acc_g = acc_df.groupby(level=0)
                    acc_one_df = None
                    if entity_id in acc_g.groups:
                        acc_one_df = acc_g.get_group(entity_id)
                        if pd_is_not_null(acc_one_df):
                            acc_one_df = acc_one_df.reset_index(level=0, drop=True)
                else:
                    acc_one_df = None

                one_result, state = self.acc_one(
                    entity_id=entity_id,
                    df=x.reset_index(level=0, drop=True),
                    acc_df=acc_one_df,
                    state=states.get(x.index[0][0]),
                )

                new_states[entity_id] = state
                return one_result

            ret_df = g.apply(lambda x: cal_acc(x))
            return ret_df, new_states

    def acc_one(self, entity_id, df: pd.DataFrame, acc_df: pd.DataFrame, state: dict) -> (pd.DataFrame, dict):
        """
        df format::

                         col1    col2    col3    ...
            timestamp
                         1.2     0.5     0.3     ...
                         1.0     0.7     0.2     ...

        the new result and state

        :param df: current input df
        :param entity_id: current computing entity_id
        :param acc_df: current result of the entity_id
        :param state: current state of the entity_id
        :return: new result and state of the entity_id
        """
        return acc_df, state


class Scorer(object):
    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)

    def score(self, input_df: pd.DataFrame) -> pd.DataFrame:
        """

        :param input_df: current input df
        :return: df with normal score
        """
        return input_df


def _register_class(target_class):
    if target_class.__name__ not in ("Factor", "FilterFactor", "ScoreFactor", "StateFactor"):
        zvt_context.factor_cls_registry[target_class.__name__] = target_class


class FactorMeta(type):
    def __new__(meta, name, bases, class_dict):
        cls = type.__new__(meta, name, bases, class_dict)
        _register_class(cls)
        return cls


class Factor(DataReader, EntityStateService, DataListener):
    #: Schema for storing states
    state_schema = FactorState
    #: define the schema for persist,its columns should be same as indicators in transformer or accumulator
    factor_schema: Type[Mixin] = None

    #: transformer for this factor if not passed as __init__ argument
    transformer: Transformer = None
    #: accumulator for this factor if not passed as __init__ argument
    accumulator: Accumulator = None

    def __init__(
        self,
        data_schema: Type[Mixin],
        entity_schema: Type[TradableEntity] = None,
        provider: str = None,
        entity_provider: str = None,
        entity_ids: List[str] = None,
        exchanges: List[str] = None,
        codes: List[str] = None,
        start_timestamp: Union[str, pd.Timestamp] = None,
        end_timestamp: Union[str, pd.Timestamp] = None,
        columns: List = None,
        filters: List = None,
        order: object = None,
        limit: int = None,
        level: Union[str, IntervalLevel] = IntervalLevel.LEVEL_1DAY,
        category_field: str = "entity_id",
        time_field: str = "timestamp",
        keep_window: int = None,
        keep_all_timestamp: bool = False,
        fill_method: str = "ffill",
        effective_number: int = None,
        transformer: Transformer = None,
        accumulator: Accumulator = None,
        need_persist: bool = False,
        only_compute_factor: bool = False,
        factor_name: str = None,
        clear_state: bool = False,
        only_load_factor: bool = False,
    ) -> None:
        """
        :param keep_all_timestamp:
        :param fill_method:
        :param effective_number:
        :param transformer:
        :param accumulator:
        :param need_persist: whether persist factor
        :param only_compute_factor: only compute factor nor result
        :param factor_name:
        :param clear_state:
        :param only_load_factor: only load factor and compute result
        """
        self.only_load_factor = only_load_factor

        #: define unique name of your factor if you want to keep factor state
        #: the factor state is defined by factor_name and entity_id
        if not factor_name:
            self.name = to_snake_str(type(self).__name__)
        else:
            self.name = factor_name

        DataReader.__init__(
            self,
            data_schema,
            entity_schema,
            provider,
            entity_provider,
            entity_ids,
            exchanges,
            codes,
            start_timestamp,
            end_timestamp,
            columns,
            filters,
            order,
            limit,
            level,
            category_field,
            time_field,
            keep_window,
        )

        EntityStateService.__init__(self, entity_ids=entity_ids)

        self.clear_state = clear_state

        self.keep_all_timestamp = keep_all_timestamp
        self.fill_method = fill_method
        self.effective_number = effective_number

        if transformer:
            self.transformer = transformer
        else:
            self.transformer = self.__class__.transformer

        if accumulator:
            self.accumulator = accumulator
        else:
            self.accumulator = self.__class__.accumulator

        self.need_persist = need_persist
        self.only_compute_factor = only_compute_factor

        #: 中间结果，不持久化
        #: data_df->pipe_df
        self.pipe_df: pd.DataFrame = None

        #: 计算因子的结果，可持久化,通过对pipe_df的计算得到
        #: pipe_df->factor_df
        self.factor_df: pd.DataFrame = None

        #: result_df是用于选股的标准df,通过对factor_df的计算得到
        #: factor_df->result_df
        self.result_df: pd.DataFrame = None

        if self.clear_state:
            self.clear_state_data()
        elif self.need_persist or self.only_load_factor:
            self.load_factor()

            #: 根据已经计算的factor_df和computing_window来保留data_df
            #: 因为读取data_df的目的是为了计算factor_df,选股和回测只依赖factor_df
            #: 所以如果有持久化的factor_df,只需保留需要用于计算的data_df即可
            if pd_is_not_null(self.data_df) and self.computing_window:
                dfs = []
                for entity_id, df in self.data_df.groupby(level=0):
                    latest_laved = get_data(
                        provider="zvt",
                        data_schema=self.factor_schema,
                        entity_id=entity_id,
                        order=self.factor_schema.timestamp.desc(),
                        limit=1,
                        index=[self.category_field, self.time_field],
                        return_type="domain",
                    )
                    if latest_laved:
                        df1 = df[df.timestamp < latest_laved[0].timestamp].iloc[-self.computing_window :]
                        if pd_is_not_null(df1):
                            df = df[df.timestamp >= df1.iloc[0].timestamp]
                    dfs.append(df)

                self.data_df = pd.concat(dfs)

        self.register_data_listener(self)

        #: the compute logic is not triggered from load data
        #: for the case:1)load factor from db 2)compute the result
        if self.only_load_factor:
            self.compute()

    def load_data(self):
        if self.only_load_factor:
            return
        super().load_data()

    def load_factor(self):
        if self.only_compute_factor:
            #: 如果只是为了计算因子，只需要读取acc_window的factor_df
            if self.accumulator is not None:
                self.factor_df = self.load_window_df(
                    provider="zvt", data_schema=self.factor_schema, window=self.accumulator.acc_window
                )
        else:
            self.factor_df = get_data(
                provider="zvt",
                data_schema=self.factor_schema,
                start_timestamp=self.start_timestamp,
                entity_ids=self.entity_ids,
                end_timestamp=self.end_timestamp,
                index=[self.category_field, self.time_field],
            )

        self.decode_factor_df(self.factor_df)

    def decode_factor_df(self, df):
        col_map_object_hook = self.factor_col_map_object_hook()
        if pd_is_not_null(df) and col_map_object_hook:
            for col in col_map_object_hook:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda x: json.loads(x, object_hook=col_map_object_hook.get(col)) if x else None
                    )

    def factor_col_map_object_hook(self) -> dict:
        """

        :return:{col:object_hook}
        """
        return {}

    def clear_state_data(self, entity_id=None):
        super().clear_state_data(entity_id=entity_id)
        if entity_id:
            del_data(self.factor_schema, filters=[self.factor_schema.entity_id == entity_id], provider="zvt")
        else:
            del_data(self.factor_schema, provider="zvt")

    def pre_compute(self):
        if not self.only_load_factor and not pd_is_not_null(self.pipe_df):
            self.pipe_df = self.data_df

    def do_compute(self):
        self.logger.info("compute factor start")
        self.compute_factor()
        self.logger.info("compute factor finish")

        self.logger.info("compute result start")
        self.compute_result()
        self.logger.info("compute result finish")

    def compute_factor(self):
        if self.only_load_factor:
            return
        #: 无状态的转换运算
        if pd_is_not_null(self.data_df) and self.transformer:
            self.pipe_df = self.transformer.transform(self.data_df)
        else:
            self.pipe_df = self.data_df

        #: 有状态的累加运算
        if pd_is_not_null(self.pipe_df) and self.accumulator:
            self.factor_df, self.states = self.accumulator.acc(self.pipe_df, self.factor_df, self.states)
        else:
            self.factor_df = self.pipe_df

    def compute_result(self):
        if pd_is_not_null(self.factor_df):
            cols = []
            if is_filter_result_df(self.factor_df):
                cols.append("filter_result")
            if is_score_result_df(self.factor_df):
                cols.append("score_result")

            if cols:
                self.result_df = self.factor_df[cols]

    def after_compute(self):
        if self.only_load_factor:
            return
        if self.keep_all_timestamp:
            self.fill_gap()

        if self.need_persist and pd_is_not_null(self.factor_df):
            self.persist_factor()

    def compute(self):
        self.pre_compute()

        self.logger.info(f"[[[ ~~~~~~~~factor:{self.name} ~~~~~~~~]]]")
        self.logger.info("do_compute start")
        start_time = time.time()
        self.do_compute()
        cost_time = time.time() - start_time
        self.logger.info("do_compute finished,cost_time:{}s".format(cost_time))

        self.logger.info("after_compute start")
        start_time = time.time()
        self.after_compute()
        cost_time = time.time() - start_time
        self.logger.info("after_compute finished,cost_time:{}s".format(cost_time))
        self.logger.info(f"[[[ ^^^^^^^^factor:{self.name} ^^^^^^^^]]]")

    def drawer_main_df(self) -> Optional[pd.DataFrame]:
        if self.only_load_factor:
            return self.factor_df
        return self.data_df

    def drawer_factor_df_list(self) -> Optional[List[pd.DataFrame]]:
        if (self.transformer is not None or self.accumulator is not None) and pd_is_not_null(self.factor_df):
            indicators = None
            if self.transformer is not None:
                indicators = self.transformer.indicators
            elif self.accumulator is not None:
                indicators = self.accumulator.indicators

            if indicators:
                return [self.factor_df[indicators]]
            else:
                return [self.factor_df]
        return None

    def drawer_sub_df_list(self) -> Optional[List[pd.DataFrame]]:
        if (self.transformer is not None or self.accumulator is not None) and pd_is_not_null(self.result_df):
            return [self.result_df]
        return None

    def drawer_annotation_df(self) -> Optional[pd.DataFrame]:
        def order_type_flag(order_type):
            if order_type is None:
                return None
            if order_type:
                return "B"
            if not order_type:
                return "S"

        def order_type_color(order_type):
            if order_type:
                return "#ec0000"
            else:
                return "#00da3c"

        if is_filter_result_df(self.result_df):
            annotation_df = self.result_df[["filter_result"]].copy()
            annotation_df = annotation_df[~annotation_df["filter_result"].isna()]
            annotation_df = drop_continue_duplicate(annotation_df, "filter_result")
            annotation_df["value"] = self.factor_df.loc[annotation_df.index]["close"]
            annotation_df["flag"] = annotation_df["filter_result"].apply(lambda x: order_type_flag(x))
            annotation_df["color"] = annotation_df["filter_result"].apply(lambda x: order_type_color(x))
            return annotation_df

    def fill_gap(self):
        #: 该操作较慢，只适合做基本面的运算
        idx = pd.date_range(self.start_timestamp, self.end_timestamp)
        new_index = pd.MultiIndex.from_product(
            [self.result_df.index.levels[0], idx], names=[self.category_field, self.time_field]
        )
        self.result_df = self.result_df.loc[~self.result_df.index.duplicated(keep="first")]
        self.result_df = self.result_df.reindex(new_index)
        self.result_df = self.result_df.groupby(level=0).fillna(method=self.fill_method, limit=self.effective_number)

    def add_entities(self, entity_ids):
        if (self.entity_ids and entity_ids) and (set(self.entity_ids) == set(entity_ids)):
            self.logger.info(f"current: {self.entity_ids}")
            self.logger.info(f"refresh: {entity_ids}")
            return
        new_entity_ids = None
        if entity_ids:
            new_entity_ids = list(set(entity_ids) - set(self.entity_ids))
            self.entity_ids = list(set(self.entity_ids + entity_ids))

        if new_entity_ids:
            self.logger.info(f"added new entity: {new_entity_ids}")
            if not self.only_load_factor:
                new_data_df = self.data_schema.query_data(
                    entity_ids=new_entity_ids,
                    provider=self.provider,
                    columns=self.columns,
                    start_timestamp=self.start_timestamp,
                    end_timestamp=self.end_timestamp,
                    filters=self.filters,
                    order=self.order,
                    limit=self.limit,
                    level=self.level,
                    index=[self.category_field, self.time_field],
                    time_field=self.time_field,
                )
                self.data_df = pd.concat([self.data_df, new_data_df], sort=False)
                self.data_df.sort_index(level=[0, 1], inplace=True)

            new_factor_df = get_data(
                provider="zvt",
                data_schema=self.factor_schema,
                start_timestamp=self.start_timestamp,
                entity_ids=new_entity_ids,
                end_timestamp=self.end_timestamp,
                index=[self.category_field, self.time_field],
            )
            self.decode_factor_df(new_factor_df)

            self.factor_df = pd.concat([self.factor_df, new_factor_df], sort=False)
            self.factor_df.sort_index(level=[0, 1], inplace=True)

    def on_data_loaded(self, data: pd.DataFrame):
        self.compute()

    def on_data_changed(self, data: pd.DataFrame):
        """
        overwrite it for computing after data added

        :param data:
        """
        self.compute()

    def on_entity_data_changed(self, entity, added_data: pd.DataFrame):
        """
        overwrite it for computing after entity data added

        :param entity:
        :param added_data:
        """
        pass

    def persist_factor(self):
        df = self.factor_df.copy()
        #: encode json columns
        if pd_is_not_null(df) and self.factor_col_map_object_hook():
            for col in self.factor_col_map_object_hook():
                if col in df.columns:
                    df[col] = df[col].apply(lambda x: json.dumps(x, cls=self.state_encoder()))

        if self.states:
            g = df.groupby(level=0)
            for entity_id in self.states:
                state = self.states[entity_id]
                try:
                    if state:
                        self.persist_state(entity_id=entity_id)
                    if entity_id in g.groups:
                        df_to_db(
                            df=df.loc[(entity_id,)], data_schema=self.factor_schema, provider="zvt", force_update=False
                        )
                except Exception as e:
                    self.logger.error(f"{self.name} {entity_id} save state error")
                    self.logger.exception(e)
                    #: clear them if error happen
                    self.clear_state_data(entity_id)
        else:
            df_to_db(df=df, data_schema=self.factor_schema, provider="zvt", force_update=False)

    def get_filter_df(self):
        if is_filter_result_df(self.result_df):
            return self.result_df[["filter_result"]]

    def get_score_df(self):
        if is_score_result_df(self.result_df):
            return self.result_df[["score_result"]]

    def get_trading_signal_df(self):
        df = self.result_df[["filter_result"]].copy()
        df = df[~df["filter_result"].isna()]
        df = drop_continue_duplicate(df, "filter_result")
        return df

    def get_targets(
        self,
        timestamp=None,
        start_timestamp=None,
        end_timestamp=None,
        target_type: TargetType = TargetType.positive,
        positive_threshold=0.8,
        negative_threshold=-0.8,
    ):
        if timestamp and (start_timestamp or end_timestamp):
            raise ValueError("Use timestamp or (start_timestamp, end_timestamp)")
        # select by filter
        filter_df = self.get_filter_df()
        selected_df = None
        target_df = None
        if pd_is_not_null(filter_df):
            if target_type == TargetType.positive:
                selected_df = filter_df[filter_df["filter_result"] == True]
            elif target_type == TargetType.negative:
                selected_df = filter_df[filter_df["filter_result"] == False]
            else:
                selected_df = filter_df[filter_df["filter_result"].isna()]

        # select by score
        score_df = self.get_score_df()
        if pd_is_not_null(score_df):
            if pd_is_not_null(selected_df):
                # filter at first
                score_df = score_df.loc[selected_df.index, :]
            if target_type == TargetType.positive:
                selected_df = score_df[score_df["score_result"] >= positive_threshold]
            elif target_type == TargetType.negative:
                selected_df = score_df[score_df["score_result"] <= negative_threshold]
            else:
                selected_df = score_df[
                    (score_df["score_result"] > negative_threshold) & (score_df["score"] < positive_threshold)
                ]
        print(selected_df)
        if pd_is_not_null(selected_df):
            selected_df = selected_df.reset_index(level="entity_id")
            if timestamp:
                if to_pd_timestamp(timestamp) in selected_df.index:
                    target_df = selected_df.loc[[to_pd_timestamp(timestamp)], ["entity_id"]]
            else:
                target_df = selected_df.loc[
                    slice(to_pd_timestamp(start_timestamp), to_pd_timestamp(end_timestamp)), ["entity_id"]
                ]

        if pd_is_not_null(target_df):
            return target_df["entity_id"].tolist()
        return []


class ScoreFactor(Factor):
    scorer: Scorer = None

    def compute_result(self):
        super().compute_result()
        if pd_is_not_null(self.factor_df) and self.scorer:
            self.result_df = self.scorer.score(self.factor_df)


# the __all__ is generated
__all__ = ["TargetType", "Indicator", "Transformer", "Accumulator", "Scorer", "FactorMeta", "Factor", "ScoreFactor"]
