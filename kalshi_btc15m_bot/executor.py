from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .config import BotConfig
from .indicators import compute_rsi, estimate_expected_range
from .kalshi_client import KalshiClient
from .price_feed import CoinbasePriceFeed
from .storage import BotStorage
from .strategy import choose_market, choose_side, rsi_regime
from .util import (
    OrderbookTop,
    isoformat_z,
    parse_iso8601,
    utc_now,
    max_affordable_contracts,
)


@dataclass(frozen=True)
class LockedDecision:
    close_time: str
    window_start: str         # theoretical window start (close_time - 15m)
    decision_time: str        # when signals were actually computed (should be ~= window_start)
    series_ticker: str

    market_ticker: str
    strike: Optional[float]

    spot: float
    rsi: float
    regime: str
    atr: Optional[float]
    expected_range: Optional[float]

    side: str  # "yes" | "no"


class TradingBot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.storage = BotStorage(cfg.storage.sqlite_path)
        self.logger_path = cfg.storage.window_log_jsonl

        # Price feed
        if cfg.price_feed.provider.lower() != "coinbase":
            raise ValueError("Only coinbase price_feed.provider is implemented in this template.")
        self.price_feed = CoinbasePriceFeed(product_id=cfg.price_feed.product_id)

        # Kalshi client
        signer = None
        if cfg.kalshi.api_key_id and cfg.kalshi.private_key_pem_path:
            from .kalshi_auth import KalshiSigner
            signer = KalshiSigner.from_pem_file(cfg.kalshi.api_key_id, cfg.kalshi.private_key_pem_path)

        self.kalshi = KalshiClient(base_url=cfg.kalshi.base_url, signer=signer)

        # PnL state
        self.pnl_key = "cumulative_pnl_cents"
        self.cumulative_pnl_cents = self.storage.get_state_int(self.pnl_key, default=0)

        from .logger import JsonlLogger
        self.jsonl = JsonlLogger(self.logger_path)

    # ---------- market discovery ----------
    def _fetch_upcoming_markets(self) -> List[Dict[str, Any]]:
        # Only one status filter may be supplied at a time in Get Markets.
        # We retrieve open + unopened + paused and merge.
        open_markets = self.kalshi.iter_markets(series_ticker=self.cfg.series_ticker, status="open")
        unopened_markets = self.kalshi.iter_markets(series_ticker=self.cfg.series_ticker, status="unopened")
        paused_markets = self.kalshi.iter_markets(series_ticker=self.cfg.series_ticker, status="paused")
        # De-dup by ticker (deterministic last-write-wins)
        by_ticker: Dict[str, Dict[str, Any]] = {}
        for m in open_markets + unopened_markets + paused_markets:
            t = str(m.get("ticker") or "")
            if t:
                by_ticker[t] = m
        return list(by_ticker.values())

    def _next_close_group(self, now_dt) -> Tuple[str, List[Dict[str, Any]]]:
        markets = self._fetch_upcoming_markets()

        future: List[Tuple[float, str, Dict[str, Any]]] = []
        for m in markets:
            ct = m.get("close_time")
            if not ct:
                continue
            try:
                ct_dt = parse_iso8601(ct)
            except Exception:
                continue
            if ct_dt > now_dt:
                future.append((ct_dt.timestamp(), ct, m))

        if not future:
            raise RuntimeError("No upcoming markets found for series. (Check series_ticker.)")

        # Find earliest close_time (timestamp sort is deterministic)
        future.sort(key=lambda x: (x[0], x[1]))
        next_close = future[0][1]
        group = [m for (_, ct, m) in future if ct == next_close]
        return next_close, group

    # ---------- signal computation (locked per window) ----------
    def _compute_locked_decision(self, close_time: str, close_group: List[Dict[str, Any]]) -> LockedDecision:
        import datetime as dt

        close_dt = parse_iso8601(close_time)
        window_start_dt = close_dt - dt.timedelta(minutes=self.cfg.execution.window_minutes)

        # We compute signals as soon as we enter the window (should be very close to window_start).
        decision_dt = utc_now()
        decision_time = isoformat_z(decision_dt)

        # Spot + candles at decision time (the window start signal snapshot)
        spot = self.price_feed.get_spot()

        lookback_minutes = self.cfg.strategy.history_lookback_minutes
        gran = self.cfg.strategy.candle_granularity_seconds

        start_dt = decision_dt - dt.timedelta(minutes=lookback_minutes)
        end_dt = decision_dt

        candles = self.price_feed.get_candles(start=start_dt, end=end_dt, granularity_seconds=gran)
        closes = [c.close for c in candles]
        rsi = compute_rsi(closes, self.cfg.strategy.rsi_period)
        if rsi is None:
            raise RuntimeError("Insufficient candle history to compute RSI.")
        regime = rsi_regime(rsi, self.cfg.strategy.bullish_rsi, self.cfg.strategy.bearish_rsi)

        vol = estimate_expected_range(
            candles=candles,
            atr_period=self.cfg.strategy.atr_period,
            candle_seconds=gran,
            window_minutes=self.cfg.execution.window_minutes,
        )

        chosen = choose_market(
            markets=close_group,
            spot=spot,
            rsi=rsi,
            bullish_th=self.cfg.strategy.bullish_rsi,
            bearish_th=self.cfg.strategy.bearish_rsi,
        )
        side = choose_side(
            spot=spot,
            rsi=rsi,
            bullish_th=self.cfg.strategy.bullish_rsi,
            bearish_th=self.cfg.strategy.bearish_rsi,
            strike=chosen.strike,
            neutral_fallback_side=self.cfg.strategy.neutral_fallback_side,
        )

        return LockedDecision(
            close_time=close_time,
            window_start=isoformat_z(window_start_dt),
            decision_time=decision_time,
            series_ticker=self.cfg.series_ticker,
            market_ticker=chosen.ticker,
            strike=chosen.strike,
            spot=float(spot),
            rsi=float(rsi),
            regime=regime,
            atr=(vol.atr if vol else None),
            expected_range=(vol.expected_range if vol else None),
            side=side,
        )

    # ---------- execution ----------
    def _compute_limit_price(self, bid: int, ask: int, walk_level: int) -> int:
        tick = self.cfg.execution.tick_size_cents
        step = self.cfg.execution.walk_step_cents
        max_walk = self.cfg.execution.max_walk_cents

        base = bid + tick if bid > 0 else ask  # if no bid, start at ask (if exists)
        if base <= 0:
            base = 1

        desired = base + (walk_level * step)
        if max_walk > 0:
            desired = min(desired, base + max_walk)

        # Respect ask bound if we have one
        if ask > 0:
            desired = min(desired, ask)

        # Respect global max
        desired = min(desired, self.cfg.execution.entry_max_cents)
        desired = max(1, min(99, desired))
        return int(desired)

    def _paper_simulate_fill(
        self,
        orderbook: Dict[str, Any],
        side: str,
        limit_price_cents: int,
        desired_count: int,
    ) -> Tuple[int, int, int]:
        """Deterministic fill simulation.

        Returns (filled_count, fill_cost_cents, fees_cents).
        - Fills by consuming available 'ask' liquidity up to limit (derived from opposite-side bids).
        - If nothing is executable at placement snapshot, returns 0 filled.
        - Fees modeled as taker fees across execution levels and rounded up to nearest cent.
        """
        ob = orderbook.get("orderbook", orderbook)
        yes_bids = ob.get("yes") or []
        no_bids = ob.get("no") or []

        # Build ask ladder for the side we're buying:
        # - To buy YES, we hit NO bids at price y => YES ask = 100 - y
        # - To buy NO, we hit YES bids at price x => NO ask = 100 - x
        ladder: List[Tuple[int, int]] = []  # (ask_price_cents, qty)
        if side == "yes":
            for price_cents, qty in reversed(no_bids):
                ask = 100 - int(price_cents)
                ladder.append((ask, int(qty)))
        else:
            for price_cents, qty in reversed(yes_bids):
                ask = 100 - int(price_cents)
                ladder.append((ask, int(qty)))

        remaining = int(desired_count)
        filled = 0
        cost = 0

        # Fee is non-linear in price, so sum raw fee across levels then round up.
        from decimal import Decimal, ROUND_CEILING
        raw_fee_dollars = Decimal(0)

        for ask_price, qty in ladder:
            if remaining <= 0:
                break
            if ask_price > limit_price_cents:
                break
            take = min(remaining, qty)
            remaining -= take
            filled += take
            cost += take * ask_price
            P = Decimal(ask_price) / Decimal(100)
            raw_fee_dollars += Decimal("0.07") * Decimal(take) * P * (Decimal(1) - P)

        if filled <= 0:
            return 0, 0, 0

        fee_cents = int((raw_fee_dollars * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_CEILING))
        return filled, cost, fee_cents

    def _place_live_order(
        self,
        *,
        ticker: str,
        side: str,
        limit_price_cents: int,
        count: int,
        expiration_ts: Optional[int],
    ) -> Dict[str, Any]:
        order: Dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": "buy",
            "type": "limit",
            "count": int(count),
            "client_order_id": str(uuid.uuid4()),
            "time_in_force": "good_till_canceled",
            "cancel_order_on_pause": True,
            "subaccount": int(self.cfg.kalshi.subaccount),
        }
        if expiration_ts is not None:
            order["expiration_ts"] = int(expiration_ts)

        if side == "yes":
            order["yes_price"] = int(limit_price_cents)
        else:
            order["no_price"] = int(limit_price_cents)

        return self.kalshi.create_order(order)

    def _attempt_entries_until_cutoff(self, decision: LockedDecision) -> Tuple[str, Dict[str, Any]]:
        """Returns (status, details). status in {'filled', 'no_fill'}"""
        import datetime as dt

        close_dt = parse_iso8601(decision.close_time)
        cutoff_dt = close_dt - dt.timedelta(seconds=self.cfg.execution.min_seconds_to_close)
        budget_cents = int(round(self.cfg.execution.usd_budget * 100))

        attempts = 0
        walk_level = 0
        first_reason: Optional[str] = None

        while True:
            now = utc_now()
            if now >= cutoff_dt:
                break

            seconds_to_close = (close_dt - now).total_seconds()

            # Refresh orderbook
            try:
                ob = self.kalshi.get_orderbook(decision.market_ticker)
            except Exception as e:
                if first_reason is None:
                    first_reason = f"orderbook_unavailable:{type(e).__name__}"
                time.sleep(self.cfg.execution.attempt_sleep_seconds)
                continue

            top = OrderbookTop.from_orderbook(ob)
            bid, ask, spread = top.side_bid_ask_spread(decision.side)

            # Liquidity / price constraints
            if ask > self.cfg.execution.entry_max_cents:
                if first_reason is None:
                    first_reason = f"ask_too_high:{ask}"
                time.sleep(self.cfg.execution.attempt_sleep_seconds)
                continue

            if spread > self.cfg.execution.spread_max_cents:
                if first_reason is None:
                    first_reason = f"spread_too_wide:{spread}"
                time.sleep(self.cfg.execution.attempt_sleep_seconds)
                continue

            # Determine limit price for this attempt
            limit = self._compute_limit_price(bid, ask, walk_level)

            # Size within budget, including fees (conservative: taker)
            count = max_affordable_contracts(budget_cents=budget_cents, limit_price_cents=limit, fee_mode="taker")
            if count < 1:
                if first_reason is None:
                    first_reason = "budget_cannot_afford_1_contract"
                time.sleep(self.cfg.execution.attempt_sleep_seconds)
                continue

            attempts += 1

            if self.cfg.mode == "paper":
                filled_count, fill_cost_cents, fee_cents = self._paper_simulate_fill(
                    orderbook=ob,
                    side=decision.side,
                    limit_price_cents=limit,
                    desired_count=count,
                )
                if filled_count > 0:
                    entry_details = {
                        "mode": "paper",
                        "entry_time": isoformat_z(now),
                        "seconds_to_close_at_entry": seconds_to_close,
                        "bid_cents": bid,
                        "ask_cents": ask,
                        "spread_cents": spread,
                        "limit_price_cents": limit,
                        "filled_count": filled_count,
                        "fill_cost_cents": fill_cost_cents,
                        "fees_cents": fee_cents,
                        "total_spent_cents": fill_cost_cents + fee_cents,
                        "attempts": attempts,
                    }
                    return "filled", entry_details

                walk_level += 1
                time.sleep(self.cfg.execution.attempt_sleep_seconds)
                continue

            # LIVE mode
            expiration_buffer = self.cfg.execution.order_expiration_buffer_seconds
            expiration_ts = int((cutoff_dt - dt.timedelta(seconds=expiration_buffer)).timestamp())

            try:
                create_resp = self._place_live_order(
                    ticker=decision.market_ticker,
                    side=decision.side,
                    limit_price_cents=limit,
                    count=count,
                    expiration_ts=expiration_ts,
                )
            except Exception as e:
                if first_reason is None:
                    first_reason = f"create_order_failed:{type(e).__name__}"
                time.sleep(self.cfg.execution.attempt_sleep_seconds)
                continue

            order_obj = create_resp.get("order", {})
            order_id = str(order_obj.get("order_id") or "")
            if not order_id:
                if first_reason is None:
                    first_reason = "create_order_missing_order_id"
                time.sleep(self.cfg.execution.attempt_sleep_seconds)
                continue

            # Wait briefly for fill
            time.sleep(self.cfg.execution.wait_for_fill_seconds)

            # Check order
            try:
                ord_resp = self.kalshi.get_order(order_id)
            except Exception as e:
                # best-effort cancel
                try:
                    self.kalshi.cancel_order(order_id, subaccount=self.cfg.kalshi.subaccount)
                except Exception:
                    pass
                if first_reason is None:
                    first_reason = f"get_order_failed:{type(e).__name__}"
                time.sleep(self.cfg.execution.attempt_sleep_seconds)
                continue

            ord_obj = ord_resp.get("order", {})
            fill_count = int(ord_obj.get("fill_count") or 0)
            remaining = int(ord_obj.get("remaining_count") or 0)
            taker_fees = int(ord_obj.get("taker_fees") or 0)
            maker_fees = int(ord_obj.get("maker_fees") or 0)
            taker_cost = int(ord_obj.get("taker_fill_cost") or 0)
            maker_cost = int(ord_obj.get("maker_fill_cost") or 0)

            # If any fill happened, this is our ONE filled entry. Cancel remainder and stop.
            if fill_count > 0:
                if remaining > 0:
                    try:
                        self.kalshi.cancel_order(order_id, subaccount=self.cfg.kalshi.subaccount)
                    except Exception:
                        pass

                fill_cost = taker_cost + maker_cost
                fees = taker_fees + maker_fees

                entry_details = {
                    "mode": "live",
                    "order_id": order_id,
                    "entry_time": isoformat_z(now),
                    "seconds_to_close_at_entry": seconds_to_close,
                    "bid_cents": bid,
                    "ask_cents": ask,
                    "spread_cents": spread,
                    "limit_price_cents": limit,
                    "filled_count": fill_count,
                    "fill_cost_cents": fill_cost,
                    "fees_cents": fees,
                    "total_spent_cents": fill_cost + fees,
                    "attempts": attempts,
                    "order_status": str(ord_obj.get("status")),
                }
                return "filled", entry_details

            # No fill yet; cancel and *re-check* to avoid race (fill while cancel in-flight)
            try:
                self.kalshi.cancel_order(order_id, subaccount=self.cfg.kalshi.subaccount)
            except Exception:
                pass

            # One extra check: if it filled just before/while canceling, treat as filled and stop.
            try:
                ord_resp2 = self.kalshi.get_order(order_id)
                ord_obj2 = ord_resp2.get("order", {})
                fill2 = int(ord_obj2.get("fill_count") or 0)
                rem2 = int(ord_obj2.get("remaining_count") or 0)
                if fill2 > 0:
                    if rem2 > 0:
                        try:
                            self.kalshi.cancel_order(order_id, subaccount=self.cfg.kalshi.subaccount)
                        except Exception:
                            pass
                    fill_cost2 = int(ord_obj2.get("taker_fill_cost") or 0) + int(ord_obj2.get("maker_fill_cost") or 0)
                    fees2 = int(ord_obj2.get("taker_fees") or 0) + int(ord_obj2.get("maker_fees") or 0)
                    entry_details = {
                        "mode": "live",
                        "order_id": order_id,
                        "entry_time": isoformat_z(now),
                        "seconds_to_close_at_entry": seconds_to_close,
                        "bid_cents": bid,
                        "ask_cents": ask,
                        "spread_cents": spread,
                        "limit_price_cents": limit,
                        "filled_count": fill2,
                        "fill_cost_cents": fill_cost2,
                        "fees_cents": fees2,
                        "total_spent_cents": fill_cost2 + fees2,
                        "attempts": attempts,
                        "order_status": str(ord_obj2.get("status")),
                        "note": "filled_during_cancel_race",
                    }
                    return "filled", entry_details
            except Exception:
                pass

            walk_level += 1
            time.sleep(self.cfg.execution.attempt_sleep_seconds)

        # Cutoff reached
        details = {
            "mode": self.cfg.mode,
            "attempts": attempts,
            "reason": first_reason or "cutoff_reached_no_fill",
        }
        return "no_fill", details

    # ---------- settlement ----------
    def _get_market_result_if_settled(self, ticker: str) -> Optional[str]:
        try:
            m = self.kalshi.get_market(ticker)
        except Exception:
            return None
        market = m.get("market", m)
        status = (market.get("status") or "").lower()
        if status != "settled":
            return None

        res = market.get("result") or market.get("market_result") or market.get("resolution")
        if res is None:
            return None
        return str(res).lower()

    def _settle_open_positions(self) -> None:
        open_positions = self.storage.list_open_positions()
        for p in open_positions:
            res = self._get_market_result_if_settled(p.market_ticker)
            if not res:
                continue

            win = (res == p.side)
            payoff_cents = p.count * 100 if win else 0
            realized_pnl_cents = payoff_cents - (p.entry_cost_cents + p.fees_cents)

            # Update cumulative pnl
            self.cumulative_pnl_cents += realized_pnl_cents
            self.storage.set_state_int(self.pnl_key, self.cumulative_pnl_cents)

            settle_time = isoformat_z(utc_now())
            self.storage.settle_position(
                p.id,
                result=res,
                realized_pnl_cents=realized_pnl_cents,
                settlement_time=settle_time,
            )

            # Update window record settlement_json if present
            w = self.storage.get_window(p.close_time)
            if w:
                decision = json.loads(w.decision_json)
                entry = json.loads(w.entry_json) if w.entry_json else None
                settlement = {
                    "market_result": res,
                    "win": bool(win),
                    "payoff_cents": payoff_cents,
                    "realized_pnl_cents": realized_pnl_cents,
                    "cumulative_pnl_cents": self.cumulative_pnl_cents,
                    "settlement_time": settle_time,
                }
                self.storage.upsert_window(
                    close_time=p.close_time,
                    window_start=w.window_start,
                    status="settled",
                    decision=decision,
                    attempts=w.attempts,
                    entry=entry,
                    settlement=settlement,
                )

                record = {
                    "timestamps": {
                        "window_start": w.window_start,
                        "close_time": p.close_time,
                        "log_time": settle_time,
                    },
                    "decision": decision,
                    "entry": entry,
                    "settlement": settlement,
                    "status": "settled",
                }
                self.jsonl.append(record)

    # ---------- main loop ----------
    def run_forever(self) -> None:
        import datetime as dt

        while True:
            # First: settle any positions that resolved
            self._settle_open_positions()

            now = utc_now()

            try:
                next_close, group = self._next_close_group(now)
            except Exception:
                # Nothing upcoming; back off
                time.sleep(5.0)
                continue

            close_dt = parse_iso8601(next_close)
            window_start_dt = close_dt - dt.timedelta(minutes=self.cfg.execution.window_minutes)

            # If we're before the window, sleep until window start (wake frequently for settlement checks).
            if now < window_start_dt:
                delta = (window_start_dt - now).total_seconds()
                if delta > 60:
                    sleep_s = 10.0
                elif delta > 5:
                    sleep_s = 1.0
                else:
                    sleep_s = max(0.1, delta)
                time.sleep(sleep_s)
                continue

            # We are inside the trading window (or after)
            window_key = next_close  # one window per close_time group
            existing = self.storage.get_window(window_key)

            if existing and existing.status in {"no_fill", "skipped", "settled"}:
                # Already finished; wait for next close group (which starts at this close time)
                time.sleep(0.5)
                continue

            if not existing:
                # Create locked decision at (near) window start
                try:
                    decision = self._compute_locked_decision(next_close, group)
                except Exception as e:
                    # Log as skipped
                    decision_dict = {
                        "close_time": next_close,
                        "window_start": isoformat_z(window_start_dt),
                        "series_ticker": self.cfg.series_ticker,
                        "error": f"decision_failed:{type(e).__name__}",
                    }
                    settlement = {
                        "realized_pnl_cents": 0,
                        "cumulative_pnl_cents": self.cumulative_pnl_cents,
                    }
                    self.storage.upsert_window(
                        close_time=window_key,
                        window_start=isoformat_z(window_start_dt),
                        status="skipped",
                        decision=decision_dict,
                        attempts=0,
                        entry=None,
                        settlement=settlement,
                    )
                    self.jsonl.append({
                        "timestamps": {
                            "window_start": isoformat_z(window_start_dt),
                            "close_time": next_close,
                            "log_time": isoformat_z(utc_now()),
                        },
                        "decision": decision_dict,
                        "entry": None,
                        "settlement": settlement,
                        "status": "skipped",
                    })
                    time.sleep(0.5)
                    continue

                decision_dict = {
                    "series_ticker": decision.series_ticker,
                    "market_ticker": decision.market_ticker,
                    "strike": decision.strike,
                    "spot": decision.spot,
                    "rsi": decision.rsi,
                    "regime": decision.regime,
                    "atr": decision.atr,
                    "expected_range": decision.expected_range,
                    "side": decision.side,
                    "decision_time": decision.decision_time,
                }
                self.storage.upsert_window(
                    close_time=window_key,
                    window_start=decision.window_start,
                    status="planned",
                    decision=decision_dict,
                    attempts=0,
                    entry=None,
                    settlement=None,
                )
                existing = self.storage.get_window(window_key)

            # Resume from existing decision
            assert existing is not None
            decision_data = json.loads(existing.decision_json)

            decision = LockedDecision(
                close_time=existing.close_time,
                window_start=existing.window_start,
                decision_time=str(decision_data.get("decision_time") or existing.window_start),
                series_ticker=str(decision_data.get("series_ticker")),
                market_ticker=str(decision_data.get("market_ticker")),
                strike=decision_data.get("strike"),
                spot=float(decision_data.get("spot")),
                rsi=float(decision_data.get("rsi")),
                regime=str(decision_data.get("regime")),
                atr=decision_data.get("atr"),
                expected_range=decision_data.get("expected_range"),
                side=str(decision_data.get("side")),
            )

            # Execute entry attempts if not already filled
            if existing.status not in {"filled_open", "settled"}:
                status, details = self._attempt_entries_until_cutoff(decision)

                if status == "filled":
                    # Record entry + create position
                    entry = details
                    attempts = int(details.get("attempts", existing.attempts))
                    self.storage.upsert_window(
                        close_time=existing.close_time,
                        window_start=existing.window_start,
                        status="filled_open",
                        decision=decision_data,
                        attempts=attempts,
                        entry=entry,
                        settlement=None,
                    )

                    # Save open position for later settlement
                    self.storage.insert_position(
                        close_time=existing.close_time,
                        market_ticker=decision.market_ticker,
                        side=decision.side,
                        count=int(entry["filled_count"]),
                        entry_cost_cents=int(entry["fill_cost_cents"]),
                        fees_cents=int(entry["fees_cents"]),
                        entry_price_cents=int(entry["limit_price_cents"]),
                        entry_time=str(entry["entry_time"]),
                    )

                    # Don't log final until settlement
                    time.sleep(0.5)
                    continue

                # No fill - finalize the window (realized pnl = 0)
                attempts = int(details.get("attempts", existing.attempts))
                settlement = {
                    "realized_pnl_cents": 0,
                    "cumulative_pnl_cents": self.cumulative_pnl_cents,
                }
                self.storage.upsert_window(
                    close_time=existing.close_time,
                    window_start=existing.window_start,
                    status="no_fill",
                    decision=decision_data,
                    attempts=attempts,
                    entry=details,
                    settlement=settlement,
                )
                self.jsonl.append({
                    "timestamps": {
                        "window_start": existing.window_start,
                        "close_time": existing.close_time,
                        "log_time": isoformat_z(utc_now()),
                    },
                    "decision": decision_data,
                    "entry": details,
                    "settlement": settlement,
                    "status": "no_fill",
                })
                time.sleep(0.5)
                continue

            # If filled_open, just wait for settlement (handled by _settle_open_positions) while still looping.
            time.sleep(0.5)
