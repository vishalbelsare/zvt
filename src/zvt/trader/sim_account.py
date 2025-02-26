# -*- coding: utf-8 -*-
import logging
import math
from typing import List, Optional

from zvt.api.kdata import get_kdata, get_kdata_schema
from zvt.contract import IntervalLevel, TradableEntity, AdjustType
from zvt.contract.api import get_db_session, decode_entity_id
from zvt.trader import TradingSignal, AccountService, OrderType, trading_signal_type_to_order_type
from zvt.trader.errors import (
    NotEnoughMoneyError,
    InvalidOrderError,
    NotEnoughPositionError,
    InvalidOrderParamError,
    WrongKdataError,
)
from zvt.trader.trader_info_api import get_trader_info, clear_trader
from zvt.trader.trader_models import AccountStatsModel, PositionModel
from zvt.trader.trader_schemas import AccountStats, Position, Order, TraderInfo
from zvt.utils.pd_utils import pd_is_not_null
from zvt.utils.time_utils import to_pd_timestamp, to_time_str, TIME_FORMAT_ISO8601, is_same_date
from zvt.utils.utils import fill_domain_from_dict


class SimAccountService(AccountService):
    def __init__(
        self,
        entity_schema: TradableEntity,
        trader_name,
        timestamp,
        provider=None,
        level=IntervalLevel.LEVEL_1DAY,
        base_capital=1000000,
        buy_cost=0.001,
        sell_cost=0.001,
        slippage=0.001,
        rich_mode=True,
        adjust_type: AdjustType = None,
        keep_history=False,
        real_time=False,
        kdata_use_begin_time=False,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)

        self.entity_schema = entity_schema
        self.base_capital = base_capital
        self.buy_cost = buy_cost
        self.sell_cost = sell_cost
        self.slippage = slippage
        self.rich_mode = rich_mode
        self.adjust_type = adjust_type
        self.trader_name = trader_name

        self.session = get_db_session("zvt", data_schema=TraderInfo)
        self.provider = provider
        self.level = level
        self.start_timestamp = timestamp
        self.keep_history = keep_history
        self.real_time = real_time
        self.kdata_use_begin_time = kdata_use_begin_time

        self.account = self.init_account()

        account_info = (
            f"init_account,holding size:{len(self.account.positions)} profit:{self.account.profit} input_money:{self.account.input_money} "
            f"cash:{self.account.cash} value:{self.account.value} all_value:{self.account.all_value}"
        )
        self.logger.info(account_info)

    def input_money(self, money=1000000):
        self.account.input_money += money
        self.account.cash += money

    def clear_account(self):
        trader_info = get_trader_info(session=self.session, trader_name=self.trader_name, return_type="domain", limit=1)

        if trader_info:
            self.logger.warning("trader:{} has run before,old result would be deleted".format(self.trader_name))
            clear_trader(session=self.session, trader_name=self.trader_name)

    def init_account(self) -> AccountStats:
        # 清除历史数据
        if not self.keep_history:
            self.clear_account()

        # 读取之前保存的账户
        if self.keep_history:
            self.account = self.load_account()
            if self.account:
                return self.account

        # init trader info
        entity_type = self.entity_schema.__name__.lower()
        sim_account = TraderInfo(
            id=self.trader_name,
            entity_id=f"trader_zvt_{self.trader_name}",
            timestamp=self.start_timestamp,
            trader_name=self.trader_name,
            entity_type=entity_type,
            start_timestamp=self.start_timestamp,
            provider=self.provider,
            level=self.level.value,
            real_time=self.real_time,
            kdata_use_begin_time=self.kdata_use_begin_time,
            kdata_adjust_type=self.adjust_type.value,
        )
        self.session.add(sim_account)
        self.session.commit()

        return AccountStats(
            entity_id=f"trader_zvt_{self.trader_name}",
            timestamp=self.start_timestamp,
            trader_name=self.trader_name,
            cash=self.base_capital,
            input_money=self.base_capital,
            all_value=self.base_capital,
            value=0,
            closing=False,
        )

    def load_account(self) -> AccountStats:
        records = AccountStats.query_data(
            filters=[AccountStats.trader_name == self.trader_name],
            order=AccountStats.timestamp.desc(),
            limit=1,
            return_type="domain",
        )
        if not records:
            return self.account
        latest_record: AccountStats = records[0]

        # create new orm object from latest record
        account_stats_model = AccountStatsModel.from_orm(latest_record)
        account = AccountStats()
        fill_domain_from_dict(account, account_stats_model.model_dump(exclude={"id", "positions"}))

        positions: List[Position] = []
        for position_domain in latest_record.positions:
            position_model = PositionModel.from_orm(position_domain)
            self.logger.debug("current position:{}".format(position_model))
            position = Position()
            fill_domain_from_dict(position, position_model.model_dump())
            positions.append(position)

        account.positions = positions

        return account

    def on_trading_open(self, timestamp):
        self.logger.info("on_trading_open:{}".format(timestamp))
        if is_same_date(timestamp, self.start_timestamp):
            return
        self.account = self.load_account()

    def on_trading_error(self, timestamp, error):
        pass

    def on_trading_finish(self, timestamp):
        pass

    def on_trading_signals(self, trading_signals: List[TradingSignal]):
        for trading_signal in trading_signals:
            try:
                self.handle_trading_signal(trading_signal)
            except Exception as e:
                self.logger.exception(e)
                self.on_trading_error(timestamp=trading_signal.happen_timestamp, error=e)

    def handle_trading_signal(self, trading_signal: TradingSignal):
        entity_id = trading_signal.entity_id
        happen_timestamp = trading_signal.happen_timestamp
        order_type = trading_signal_type_to_order_type(trading_signal.trading_signal_type)
        trading_level = trading_signal.trading_level.value
        if order_type:
            try:
                kdata = get_kdata(
                    provider=self.provider,
                    entity_id=entity_id,
                    level=trading_level,
                    start_timestamp=happen_timestamp,
                    end_timestamp=happen_timestamp,
                    limit=1,
                    adjust_type=self.adjust_type,
                )
            except Exception as e:
                self.logger.error(e)
                raise WrongKdataError("could not get kdata")

            if pd_is_not_null(kdata):
                entity_type, _, _ = decode_entity_id(kdata["entity_id"][0])

                the_price = kdata["close"][0]

                if the_price:
                    if trading_signal.position_pct:
                        self.order_by_position_pct(
                            entity_id=entity_id,
                            order_price=the_price,
                            order_timestamp=happen_timestamp,
                            order_position_pct=trading_signal.position_pct,
                            order_type=order_type,
                        )
                    elif trading_signal.order_money:
                        self.order_by_money(
                            entity_id=entity_id,
                            order_price=the_price,
                            order_timestamp=happen_timestamp,
                            order_money=trading_signal.order_money,
                            order_type=order_type,
                        )
                    elif trading_signal.order_amount:
                        self.order_by_amount(
                            entity_id=entity_id,
                            order_price=the_price,
                            order_timestamp=happen_timestamp,
                            order_amount=trading_signal.order_amount,
                            order_type=order_type,
                        )
                    else:
                        assert False
                else:
                    self.logger.warning(
                        "ignore trading signal,wrong kdata,entity_id:{},timestamp:{},kdata:{}".format(
                            entity_id, happen_timestamp, kdata.to_dict(orient="records")
                        )
                    )

            else:
                self.logger.warning(
                    "ignore trading signal,could not get kdata,entity_id:{},timestamp:{}".format(
                        entity_id, happen_timestamp
                    )
                )

    def on_trading_close(self, timestamp):
        self.logger.info("on_trading_close:{}".format(timestamp))
        # remove the empty position
        self.account.positions = [
            position for position in self.account.positions if position.long_amount > 0 or position.short_amount > 0
        ]

        # clear the data which need recomputing
        the_id = "{}_{}".format(self.trader_name, to_time_str(timestamp, TIME_FORMAT_ISO8601))

        self.account.value = 0
        self.account.all_value = 0
        for position in self.account.positions:
            entity_type, _, _ = decode_entity_id(position.entity_id)
            data_schema = get_kdata_schema(entity_type, level=IntervalLevel.LEVEL_1DAY, adjust_type=self.adjust_type)

            kdata = get_kdata(
                provider=self.provider,
                level=IntervalLevel.LEVEL_1DAY,
                entity_id=position.entity_id,
                order=data_schema.timestamp.desc(),
                end_timestamp=timestamp,
                limit=1,
                adjust_type=self.adjust_type,
            )

            closing_price = kdata["close"][0]

            position.available_long = position.long_amount
            position.available_short = position.short_amount

            if closing_price:
                if (position.long_amount is not None) and position.long_amount > 0:
                    position.value = position.long_amount * closing_price
                    self.account.value += position.value
                elif (position.short_amount is not None) and position.short_amount > 0:
                    position.value = 2 * (position.short_amount * position.average_short_price)
                    position.value -= position.short_amount * closing_price
                    self.account.value += position.value

                # refresh profit
                position.profit = (closing_price - position.average_long_price) * position.long_amount
                position.profit_rate = position.profit / (position.average_long_price * position.long_amount)

            else:
                self.logger.warning(
                    "could not refresh close value for position:{},timestamp:{}".format(position.entity_id, timestamp)
                )

            position.id = "{}_{}_{}".format(
                self.trader_name, position.entity_id, to_time_str(timestamp, TIME_FORMAT_ISO8601)
            )
            position.timestamp = to_pd_timestamp(timestamp)
            position.account_stats_id = the_id

        self.account.id = the_id
        self.account.all_value = self.account.value + self.account.cash
        self.account.closing = True
        self.account.timestamp = to_pd_timestamp(timestamp)
        self.account.profit = self.account.all_value - self.account.input_money
        self.account.profit_rate = self.account.profit / self.account.input_money

        self.session.add(self.account)
        self.session.commit()
        account_info = (
            f"on_trading_close,holding size:{len(self.account.positions)} profit:{self.account.profit} input_money:{self.account.input_money} "
            f"cash:{self.account.cash} value:{self.account.value} all_value:{self.account.all_value}"
        )
        self.logger.info(account_info)

    def get_current_position(self, entity_id, create_if_not_exist=False) -> Optional[Position]:
        """
        get position for entity_id

        :param entity_id: the entity id
        :param create_if_not_exist: create an empty position if not exist in current account
        :return:
        """
        for position in self.account.positions:
            if position.entity_id == entity_id:
                return position
        if create_if_not_exist:
            trading_t = self.entity_schema.get_trading_t()
            current_position = Position(
                trader_name=self.trader_name,
                entity_id=entity_id,
                long_amount=0,
                available_long=0,
                average_long_price=0,
                short_amount=0,
                available_short=0,
                average_short_price=0,
                profit=0,
                value=0,
                trading_t=trading_t,
            )
            # add it to account
            self.account.positions.append(current_position)
            return current_position
        return None

    def get_current_account(self):
        return self.account

    def update_position(self, current_position, order_amount, current_price, order_type, timestamp):
        """

        :param timestamp:
        :type timestamp:
        :param current_position:
        :type current_position: Position
        :param order_amount:
        :type order_amount:
        :param current_price:
        :type current_price:
        :param order_type:
        :type order_type:
        """
        if order_type == OrderType.order_long:
            need_money = (order_amount * current_price) * (1 + self.slippage + self.buy_cost)
            if self.account.cash < need_money:
                if self.rich_mode:
                    self.input_money()
                else:
                    raise NotEnoughMoneyError()

            self.account.cash -= need_money

            # 计算平均价
            long_amount = current_position.long_amount + order_amount
            if long_amount == 0:
                current_position.average_long_price = 0
            current_position.average_long_price = (
                current_position.average_long_price * current_position.long_amount + current_price * order_amount
            ) / long_amount

            current_position.long_amount = long_amount

            if current_position.trading_t == 0:
                current_position.available_long += order_amount

        elif order_type == OrderType.order_short:
            need_money = (order_amount * current_price) * (1 + self.slippage + self.buy_cost)
            if self.account.cash < need_money:
                if self.rich_mode:
                    self.input_money()
                else:
                    raise NotEnoughMoneyError()

            self.account.cash -= need_money

            short_amount = current_position.short_amount + order_amount
            current_position.average_short_price = (
                current_position.average_short_price * current_position.short_amount + current_price * order_amount
            ) / short_amount

            current_position.short_amount = short_amount

            if current_position.trading_t == 0:
                current_position.available_short += order_amount

        elif order_type == OrderType.order_close_long:
            self.account.cash += order_amount * current_price * (1 - self.slippage - self.sell_cost)
            # FIXME:如果没卖完，重新计算计算平均价

            current_position.available_long -= order_amount
            current_position.long_amount -= order_amount

        elif order_type == OrderType.order_close_short:
            self.account.cash += 2 * (order_amount * current_position.average_short_price)
            self.account.cash -= order_amount * current_price * (1 + self.slippage + self.sell_cost)

            current_position.available_short -= order_amount
            current_position.short_amount -= order_amount
        else:
            assert False

        # save the order info to db
        order_id = "{}_{}_{}_{}".format(
            self.trader_name, order_type, current_position.entity_id, to_time_str(timestamp, TIME_FORMAT_ISO8601)
        )
        order = Order(
            id=order_id,
            timestamp=to_pd_timestamp(timestamp),
            trader_name=self.trader_name,
            entity_id=current_position.entity_id,
            order_price=current_price,
            order_amount=order_amount,
            order_type=order_type.value,
            level=self.level.value,
            status="success",
        )
        self.session.add(order)
        self.session.commit()

    def cal_amount_by_money(
        self,
        order_price: float,
        order_money: float,
    ):
        if order_money > self.account.cash:
            if self.rich_mode:
                self.input_money()
            else:
                raise NotEnoughMoneyError()

        cost = order_price * (1 + self.slippage + self.buy_cost)
        order_amount = order_money // cost

        return order_amount

    def cal_amount_by_position_pct(self, entity_id, order_price: float, order_position_pct: float, order_type):
        if order_type == OrderType.order_long or order_type == OrderType.order_short:
            cost = order_price * (1 + self.slippage + self.buy_cost)
            want_pay = self.account.cash * order_position_pct
            order_amount = want_pay // cost

            if order_amount < 1:
                if self.rich_mode:
                    self.input_money()
                    order_amount = max((self.account.cash * order_position_pct) // cost, 1)
                else:
                    raise NotEnoughMoneyError()
            return order_amount
        elif order_type == OrderType.order_close_long or order_type == OrderType.order_close_short:
            current_position = self.get_current_position(entity_id=entity_id, create_if_not_exist=True)
            if order_type == OrderType.order_close_long:
                available = current_position.available_long
            else:
                available = current_position.available_short
            if available > 0:
                if order_position_pct == 1.0:
                    order_amount = available
                else:
                    order_amount = math.floor(available * order_position_pct)
                return order_amount
            else:
                raise NotEnoughPositionError()

    def order_by_position_pct(
        self,
        entity_id,
        order_timestamp,
        order_price: float,
        order_type: OrderType,
        order_position_pct: float = 0.2,
    ):
        order_amount = self.cal_amount_by_position_pct(
            entity_id=entity_id, order_price=order_price, order_position_pct=order_position_pct, order_type=order_type
        )

        self.order_by_amount(
            entity_id=entity_id,
            order_price=order_price,
            order_amount=order_amount,
            order_timestamp=order_timestamp,
            order_type=order_type,
        )

    def order_by_money(
        self,
        entity_id,
        order_timestamp,
        order_price: float,
        order_type: OrderType,
        order_money: float,
    ):
        if order_type not in (OrderType.order_long, OrderType.order_short):
            raise InvalidOrderParamError(f"order type: {order_type.value} not support order_by_money")

        order_amount = self.cal_amount_by_money(order_price=order_price, order_money=order_money)
        self.order_by_amount(
            entity_id=entity_id,
            order_price=order_price,
            order_amount=order_amount,
            order_timestamp=order_timestamp,
            order_type=order_type,
        )

    def order_by_amount(
        self,
        entity_id,
        order_price,
        order_timestamp,
        order_type,
        order_amount,
    ):
        current_position = self.get_current_position(entity_id=entity_id, create_if_not_exist=True)

        # 开多
        if order_type == OrderType.order_long:
            if current_position.short_amount > 0:
                raise InvalidOrderError("close the short position before open long")

            self.update_position(current_position, order_amount, order_price, order_type, order_timestamp)
        # 开空
        elif order_type == OrderType.order_short:
            if current_position.long_amount > 0:
                raise InvalidOrderError("close the long position before open short")

            self.update_position(current_position, order_amount, order_price, order_type, order_timestamp)
        # 平多
        elif order_type == OrderType.order_close_long:
            if current_position.available_long >= order_amount:
                self.update_position(current_position, order_amount, order_price, order_type, order_timestamp)
            else:
                raise NotEnoughPositionError()
        # 平空
        elif order_type == OrderType.order_close_short:
            if current_position.available_short >= order_amount:
                self.update_position(current_position, order_amount, order_price, order_type, order_timestamp)
            else:
                raise Exception("not enough position")


# the __all__ is generated
__all__ = ["AccountService", "SimAccountService"]
