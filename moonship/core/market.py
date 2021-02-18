#  Copyright (c) 2021, Marlon Paulse
#  All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice, this
#     list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
#  FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
#  DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#  SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#  CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#  OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import abc
import asyncio
import collections
import logging
import sortedcontainers

from dataclasses import dataclass, field
from moonship.core import *
from typing import Iterator, Union

__all__ = [
    "Market",
    "MarketClient",
    "MarketFeed",
    "MarketEvent",
    "MarketFeedSubscriber",
    "MarketStatusEvent",
    "OrderBookEntriesView",
    "OrderBookEntry",
    "OrderBookItemAddedEvent",
    "OrderBookItemRemovedEvent",
    "OrderBookInitEvent",
    "TickerEvent",
    "TradeEvent",
]

logger = logging.getLogger(__name__)


class MarketClient(abc.ABC):

    def __init__(self, market_name: str, symbol: str, app_config: Config):
        self.market_name = market_name
        self.symbol = symbol

    @abc.abstractmethod
    async def connect(self):
        pass

    @abc.abstractmethod
    async def close(self):
        pass

    @abc.abstractmethod
    async def get_ticker(self) -> Ticker:
        pass

    @abc.abstractmethod
    async def place_order(self, order: Union[MarketOrder, LimitOrder]) -> str:
        pass

    @abc.abstractmethod
    async def get_order(self, order_id: str) -> FullOrderDetails:
        pass

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        pass


@dataclass()
class MarketEvent:
    timestamp: Timestamp
    market_name: str
    symbol: str


@dataclass()
class TickerEvent(MarketEvent):
    ticker: Ticker


@dataclass()
class OrderBookInitEvent(MarketEvent):
    status: MarketStatus
    orders: list[LimitOrder] = field(default_factory=list)


@dataclass()
class OrderBookItemAddedEvent(MarketEvent):
    order: LimitOrder


@dataclass()
class OrderBookItemRemovedEvent(MarketEvent):
    order_id: str


@dataclass()
class TradeEvent(MarketEvent):
    base_amount: Amount
    counter_amount: Amount
    maker_order_id: str
    taker_order_id: str


@dataclass()
class MarketStatusEvent(MarketEvent):
    status: MarketStatus


class MarketFeedSubscriber:

    async def on_ticker(self, event: TickerEvent) -> None:
        pass

    async def on_order_book_init(self, event: OrderBookInitEvent) -> None:
        pass

    async def on_order_book_item_added(self, event: OrderBookItemAddedEvent) -> None:
        pass

    async def on_order_book_item_removed(self, event: OrderBookItemRemovedEvent) -> None:
        pass

    async def on_trade(self, event: TradeEvent) -> None:
        pass

    async def on_market_status_update(self, event: MarketStatusEvent) -> None:
        pass


class MarketFeed(abc.ABC):
    subscribers: list[MarketFeedSubscriber] = []

    def __init__(self, market_name: str, symbol: str, app_config: Config):
        self.market_name = market_name
        self.symbol = symbol
        pass

    @abc.abstractmethod
    async def connect(self):
        pass

    @abc.abstractmethod
    async def close(self):
        pass

    def subscribe(self, subscriber) -> None:
        self.subscribers.append(subscriber)

    def unsubscribe(self, subscriber) -> None:
        self.subscribers.remove(subscriber)

    def raise_event(self, event: MarketEvent) -> None:
        for sub in self.subscribers:
            func = None
            if isinstance(event, OrderBookItemAddedEvent):
                func = sub.on_order_book_item_added(event)
            elif isinstance(event, OrderBookItemRemovedEvent):
                func = sub.on_order_book_item_removed(event)
            elif isinstance(event, TradeEvent):
                func = sub.on_trade(event)
            elif isinstance(event, TickerEvent):
                func = sub.on_ticker(event)
            elif isinstance(event, OrderBookInitEvent):
                func = sub.on_order_book_init(event)
            elif isinstance(event, MarketStatusEvent):
                func = sub.on_market_status_update(event)
            if func is not None:
                asyncio.create_task(func)


class OrderBookEntry:

    def __init__(self, order: LimitOrder) -> None:
        self._price = order.price
        self._orders = {order.id: order}

    def __contains__(self, order_id: str):
        return self._orders.__contains__(order_id)

    @property
    def price(self) -> Amount:
        return self._price

    @property
    def volume(self) -> Amount:
        volume = Amount(0)
        for order in self._orders.values():
            volume += order.volume
        return volume


class OrderBook:
    bids = sortedcontainers.SortedDict[Amount, OrderBookEntry]()
    asks = sortedcontainers.SortedDict[Amount, OrderBookEntry]()
    order_entry_index: dict[str, OrderBookEntry] = {}

    def add_order(self, order: LimitOrder) -> None:
        entries = self._get_entries_list(order)
        entry: OrderBookEntry = entries.get(order.price)
        if entry is None:
            entry = OrderBookEntry(order)
            entries[order.price] = entry
        else:
            entry._orders[order.id] = order
        self.order_entry_index[order.id] = entry

    def remove_order(self, order_id: str) -> None:
        entry = self.order_entry_index.get(order_id)
        if entry is not None:
            order = entry._orders.get(order_id)
            if order is not None:
                del entry._orders[order_id]
                if entry.volume == 0:
                    entries = self._get_entries_list(order)
                    del entries[entry.price]
            del self.order_entry_index[order_id]

    def clear(self) -> None:
        self.order_entry_index.clear()
        self.bids.clear()
        self.asks.clear()

    def _get_entries_list(self, order) -> sortedcontainers.SortedDict[Amount, OrderBookEntry]:
        return self.bids if order.action == OrderAction.BUY else self.asks


class OrderBookEntriesView(collections.Mapping):

    def __init__(self, entries: sortedcontainers.SortedDict[Amount, OrderBookEntry]) -> None:
        self._entries = entries

    def __getitem__(self, amount: Amount) -> OrderBookEntry:
        return self._entries.get(amount)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Amount]:
        return iter(self._entries)

    def values(self) -> sortedcontainers.SortedValuesView:
        return self._entries.values()


class Market:

    def __init__(self, name: str, symbol: str, client: MarketClient, feed: MarketFeed) -> None:
        self._name = name
        self._symbol = symbol
        self._client = client
        self._feed = feed
        self._current_price = Amount(0)
        self._order_book = OrderBook()
        self._status = MarketStatus.CLOSED

    @property
    def name(self) -> str:
        return self._name

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def status(self) -> MarketStatus:
        return self._status

    @property
    def current_price(self) -> Amount:
        return self._current_price

    @property
    def bid_price(self) -> Amount:
        return self._order_book.bids.peekitem(-1)[0] if len(self._order_book.bids) > 0 else Amount(0)

    @property
    def ask_price(self) -> Amount:
        return self._order_book.asks.peekitem(0)[0] if len(self._order_book.asks) > 0 else Amount(0)

    @property
    def spread(self) -> Amount:
        return self.ask_price - self.bid_price

    @property
    def bids(self) -> OrderBookEntriesView:
        return OrderBookEntriesView(self._order_book.bids)

    @property
    def asks(self) -> OrderBookEntriesView:
        return OrderBookEntriesView(self._order_book.asks)

    async def get_ticker(self) -> Ticker:
        if self._status == MarketStatus.CLOSED:
            raise MarketException(f"Market closed", self.name)
        return await self._client.get_ticker()

    async def place_order(self, order: Union[MarketOrder, LimitOrder]) -> str:
        if self._status == MarketStatus.CLOSED:
            raise MarketException(f"Market closed", self.name)
        return await self._client.place_order(order)

    async def get_order(self, order_id: str) -> FullOrderDetails:
        if self._status == MarketStatus.CLOSED:
            raise MarketException(f"Market closed", self.name)
        return await self._client.get_order(order_id)

    async def cancel_order(self, order_id: str) -> bool:
        if self._status == MarketStatus.CLOSED:
            raise MarketException(f"Market closed", self.name)
        return await self._client.cancel_order(order_id)

    def subscribe_to_feed(self, subscriber) -> None:
        self._feed.subscribe(subscriber)

    def unsubscribe_from_feed(self, subscriber) -> None:
        self._feed.unsubscribe(subscriber)