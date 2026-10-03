from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from .kalshi_auth import KalshiSigner


TRADE_API_PREFIX = "/trade-api/v2"


class KalshiApiError(RuntimeError):
    pass


@dataclass
class KalshiClient:
    base_url: str
    signer: Optional[KalshiSigner] = None
    timeout_seconds: float = 10.0

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        auth: bool = False,
    ) -> Dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        headers: Dict[str, str] = {"Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"

        # Kalshi signature uses PATH WITHOUT query parameters.
        if auth:
            if self.signer is None:
                raise KalshiApiError("Authenticated request requires signer (api key + private key).")
            headers.update(self.signer.sign_headers(method=method, path=path))

        resp = requests.request(
            method=method,
            url=url,
            params=params,
            json=json,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        if resp.status_code >= 400:
            raise KalshiApiError(f"{method} {path} failed: {resp.status_code} {resp.text}")
        if resp.status_code == 204:
            return {}
        return resp.json()

    # -------- Market data --------

    def get_markets(
        self,
        *,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 1000,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"{TRADE_API_PREFIX}/markets", params=params, auth=False)

    def iter_markets(
        self,
        *,
        series_ticker: Optional[str],
        status: Optional[str],
        limit: int = 1000,
        max_pages: int = 50,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        pages = 0
        while True:
            pages += 1
            if pages > max_pages:
                break
            data = self.get_markets(series_ticker=series_ticker, status=status, limit=limit, cursor=cursor)
            markets = data.get("markets", []) or []
            out.extend(markets)
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_market(self, ticker: str) -> Dict[str, Any]:
        return self._request("GET", f"{TRADE_API_PREFIX}/markets/{ticker}", auth=False)

    def get_orderbook(self, ticker: str, depth: int = 0) -> Dict[str, Any]:
        params = {"depth": depth} if depth is not None else None
        return self._request("GET", f"{TRADE_API_PREFIX}/markets/{ticker}/orderbook", params=params, auth=False)

    # -------- Portfolio / Orders --------

    def create_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", f"{TRADE_API_PREFIX}/portfolio/orders", json=order, auth=True)

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self._request("GET", f"{TRADE_API_PREFIX}/portfolio/orders/{order_id}", auth=True)

    def cancel_order(self, order_id: str, *, subaccount: Optional[int] = None) -> Dict[str, Any]:
        params = {}
        if subaccount is not None:
            params["subaccount"] = subaccount
        return self._request("DELETE", f"{TRADE_API_PREFIX}/portfolio/orders/{order_id}", params=params or None, auth=True)

    def get_positions(
        self,
        *,
        ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        count_filter: Optional[str] = None,
        limit: int = 1000,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if count_filter:
            params["count_filter"] = count_filter
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"{TRADE_API_PREFIX}/portfolio/positions", params=params, auth=True)

    def get_balance(self) -> Dict[str, Any]:
        return self._request("GET", f"{TRADE_API_PREFIX}/portfolio/balance", auth=True)

    def get_settlements(
        self,
        *,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,
        limit: int = 1000,
        cursor: Optional[str] = None,
        subaccount: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        if max_ts is not None:
            params["max_ts"] = int(max_ts)
        if cursor:
            params["cursor"] = cursor
        if subaccount is not None:
            params["subaccount"] = int(subaccount)
        return self._request("GET", f"{TRADE_API_PREFIX}/portfolio/settlements", params=params, auth=True)
