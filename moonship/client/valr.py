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

import json

import aiohttp
import aiolimiter
import asyncio
import hmac
import hashlib

from datetime import timezone
from moonship.core import *
from moonship.client.web import *
from typing import Optional, Union

API_BASE_URL = "https://api.valr.com"
API_VERSION = "v1"
STREAM_BASE_URL = "wss://api.valr.com"


class ValrClient(AbstractWebClient):
    market_info: dict[str, MarketInfo] = {}
    market_info_lock = asyncio.Lock()
    limiter = aiolimiter.AsyncLimiter(120, 60)
    public_api_limiter = aiolimiter.AsyncLimiter(10, 60)

    def __init__(self, market_name: str, app_config: Config):
        api_key = app_config.get("moonship.valr.api_key")
        if not isinstance(api_key, str):
            raise StartUpException("VALR API key not configured")
        self.api_secret = app_config.get("moonship.valr.api_secret")
        if not isinstance(self.api_secret, str):
            raise StartUpException("VALR API secret not configured")
        headers = {"X-VALR-API-KEY": api_key}
        super().__init__(
            market_name,
            app_config,
            WebClientSessionParameters(headers=headers),
            [
                WebClientStreamParameters(url=f"{STREAM_BASE_URL}/ws/account", headers=headers),
                WebClientStreamParameters(url=f"{STREAM_BASE_URL}/ws/trade", headers=headers)
            ])

    async def get_market_info(self, use_cached=True) -> MarketInfo:
        async with self.market_info_lock:
            if not use_cached or self.market.symbol not in self.market_info:
                try:
                    status = MarketStatus.CLOSED
                    async with self.public_api_limiter:
                        async with self.http_session.get(f"{API_BASE_URL}/{API_VERSION}/public/status") as rsp:
                            await self.handle_error_response(rsp)
                            s = (await rsp.json()).get("status")
                            if s == "online":
                                status = MarketStatus.OPEN
                    async with self.public_api_limiter:
                        async with self.http_session.get(f"{API_BASE_URL}/{API_VERSION}/public/pairs") as rsp:
                            await self.handle_error_response(rsp)
                            pairs = await rsp.json()
                            for pair_info in pairs:
                                quote_asset_precision = 0
                                tick_size = pair_info.get("tickSize")
                                if isinstance(tick_size, str):
                                    s = tick_size.split(".")
                                    if len(s) == 2:
                                        quote_asset_precision = len(s[1])
                                info = MarketInfo(
                                    symbol=pair_info.get("symbol"),
                                    base_asset=pair_info.get("baseCurrency"),
                                    base_asset_precision=int(pair_info.get("baseDecimalPlaces")),
                                    base_asset_min_quantity=Amount(pair_info.get("minBaseAmount")),
                                    quote_asset=pair_info.get("quoteCurrency"),
                                    quote_asset_precision=quote_asset_precision,
                                    status=status)
                                self.market_info[info.symbol] = info
                except Exception as e:
                    raise MarketException(
                        f"Could not retrieve market info for {self.market.symbol}", self.market.name) from e
        return self.market_info[self.market.symbol]

    async def get_ticker(self) -> Ticker:
        try:
            async with self.public_api_limiter:
                async with self.http_session.get(
                        f"{API_BASE_URL}/{API_VERSION}/public/{self.market.symbol}/marketsummary") as rsp:
                    await self.handle_error_response(rsp)
                    ticker = await rsp.json()
                    return Ticker(
                        timestamp=self._to_timestamp(ticker.get("created")),
                        symbol=ticker.get("currencyPair"),
                        bid_price=to_amount(ticker.get("bidPrice")),
                        ask_price=to_amount(ticker.get("askPrice")),
                        current_price=to_amount(ticker.get("lastTradedPrice")))
        except Exception as e:
            raise MarketException(f"Could not retrieve ticker for {self.market.symbol}", self.market.name) from e

    async def get_recent_trades(self, limit) -> list[Trade]:
        try:
            trades: list[Trade] = []
            before_id = None
            for i in range(0, limit, 100):
                async with self.limiter:
                    path = f"{API_VERSION}/marketdata/{self.market.symbol}/tradehistory?limit=100"
                    if before_id is not None:
                        path += f"&beforeId={before_id}"
                    async with self.http_session.get(
                            f"{API_BASE_URL}/{path}",
                            headers=self._get_auth_headers("GET", path)) as rsp:
                        await self.handle_error_response(rsp)
                        trades_data = await rsp.json()
                        if isinstance(trades_data, list):
                            for data in trades_data:
                                trades.append(
                                    Trade(
                                        timestamp=self._to_timestamp(data.get("tradedAt")),
                                        symbol=data.get("currencyPair"),
                                        price=to_amount(data.get("price")),
                                        quantity=to_amount(data.get("quantity")),
                                        taker_action=OrderAction.BUY if data.get(
                                            "takerSide") == "buy" else OrderAction.SELL))
                                before_id = data.get("id")
            return trades
        except Exception as e:
            raise MarketException(f"Could not retrieve recent trades for {self.market.symbol}", self.market.name) from e

    async def place_order(self, order: Union[MarketOrder, LimitOrder]) -> str:
        request = {
            "pair": self.market.symbol,
            "side": order.action.name
        }
        if isinstance(order, LimitOrder):
            order_type = "limit"
            request["postOnly"] = order.post_only
            request["price"] = to_amount_str(order.price)
            request["quantity"] = to_amount_str(order.quantity)
            request["timeInForce"] = "GTC"
        else:
            order_type = "market"
            if order.is_base_quantity:
                request["baseAmount"] = to_amount_str(order.quantity)
            else:
                request["quoteAmount"] = to_amount_str(order.quantity)
        request = json.dumps(request, indent=None)
        try:
            path = f"{API_VERSION}/orders/{order_type}"
            async with self.limiter:
                async with self.http_session.post(
                        f"{API_BASE_URL}/{path}",
                        headers=self._get_auth_headers("POST", path, request),
                        data=request) as rsp:
                    await self.handle_error_response(rsp)
                    order.id = (await rsp.json()).get("id")
                    return order.id
        except Exception as e:
            raise MarketException("Failed to place order", self.market.name, self._get_error_code(e)) from e

    async def get_order(self, order_id: str) -> FullOrderDetails:
        try:
            async with self.limiter:
                path = f"{API_VERSION}/orders/{self.market.symbol}/orderid/{order_id}"
                async with self.http_session.get(
                        f"{API_BASE_URL}/{path}",
                        headers=self._get_auth_headers("GET", path)) as rsp:
                    await self.handle_error_response(rsp)
                    order_data = await rsp.json()
                    quantity = to_amount(order_data.get("originalQuantity"))
                    return FullOrderDetails(
                        id=order_id,
                        symbol=order_data.get("currencyPair"),
                        action=self._to_order_action(order_data.get("orderSide")),
                        quantity=quantity,
                        quantity_filled=quantity - to_amount(order_data.get("remainingQuantity")),
                        limit_price=to_amount(order_data.get("originalPrice")),
                        status=self._to_order_status(order_data),
                        creation_timestamp=self._to_timestamp(order_data.get("orderCreatedAt")))
        except Exception as e:
            raise MarketException(f"Could not retrieve details of order {order_id}", self.market.name) from e

    async def cancel_order(self, order_id: str) -> bool:
        request = {
            "orderId": order_id,
            "pair": self.market.symbol
        }
        try:
            async with self.limiter:
                path = f"{API_VERSION}/orders/order"
                request = json.dumps(request, indent=None)
                async with self.http_session.delete(
                        f"{API_BASE_URL}/{path}",
                        headers=self._get_auth_headers("DELETE", path, request),
                        data=request) as rsp:
                    await self.handle_error_response(rsp)
                    return False  # Cancellation confirmation returned via stream or get_order() call
        except Exception as e:
            raise MarketException("Failed to cancel order", self.market.name) from e

    def _get_auth_headers(self, http_method: str, request_path: str, request_body: str = None) -> dict[str, str]:
        timestamp = str(utc_timestamp_now_msec())
        msg = timestamp + http_method.upper() + request_path
        if request_body is not None:
            msg += request_body
        signature = hmac.new(
            bytes(self.api_secret, encoding="utf-8"),
            bytes(msg, encoding="utf-8"),
            hashlib.sha512).hexdigest()
        return {
            "X-VALR-SIGNATURE": signature,
            "X-VALR-TIMESTAMP": timestamp
        }

    async def on_before_data_stream_connect(self, params: WebClientStreamParameters) -> None:
        auth_headers = self._get_auth_headers("GET", params.url[len(STREAM_BASE_URL) + 1:])
        for name, value in auth_headers.items():
            params.headers[name] = value

    async def on_after_data_stream_connect(
            self,
            websocket: aiohttp.ClientWebSocketResponse,
            params: WebClientStreamParameters) -> None:
        if params.url.endswith("/trade"):
            await websocket.send_json({
                "type": "SUBSCRIBE",
                "subscriptions": [
                    {
                        "event": "AGGREGATED_ORDERBOOK_UPDATE",
                        "pairs": [self.market.symbol]
                    },
                    {
                        "event": "NEW_TRADE",
                        "pairs": [self.market.symbol]
                    }
                ]
            })
        #  TODO: ping-pong messages every 30 seconds

    async def on_data_stream_msg(self, msg: any, websocket: aiohttp.ClientWebSocketResponse) -> None:
        if not isinstance(msg, dict):
            return
        #  TODO

    def _to_timestamp(self, s: Optional[str]) -> Timestamp:
        if s is not None:
            if s[-1] == "Z":
                s = s[:-1] + "+00:00"
            return Timestamp.fromisoformat(s)
        return Timestamp.now(tz=timezone.utc)

    def _to_order_action(self, s: str) -> OrderAction:
        return OrderAction.BUY if s.upper() == "BUY" else OrderAction.SELL

    def _to_order_status(self, order_data: dict[str, any]) -> OrderStatus:
        status = order_data.get("orderStatusType")
        quantity = to_amount(order_data.get("originalQuantity"))
        rem_quantity = to_amount(order_data.get("remainingQuantity"))
        if "Failed" in status:
            return OrderStatus.REJECTED
        elif status == "Cancelled":
            if quantity == rem_quantity:
                return OrderStatus.CANCELLED
            elif rem_quantity > 0:
                return OrderStatus.CANCELLED_AND_PARTIALLY_FILLED
            else:
                return OrderStatus.FILLED
        elif status == "Partially Filled":
            return OrderStatus.PARTIALLY_FILLED
        elif status == "Filled":
            return OrderStatus.FILLED
        return OrderStatus.PENDING

    def _get_error_code(self, e: Exception) -> MarketErrorCode:
        # TODO: if isinstance(e, HttpResponseException) and isinstance(e.body, dict)
        return MarketErrorCode.UNKNOWN
