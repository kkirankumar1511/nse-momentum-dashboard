"""
Thin wrapper around Kite Connect (Zerodha).

Handles: login flow, instrument-token lookup, historical candles,
positions/holdings, order placement and square-off.

Daily flow:
  1. Run `python kite_client.py login` -> prints login URL.
  2. Open URL, log in, copy `request_token` from the redirect URL.
  3. Run `python kite_client.py token <request_token>` -> saves access token to .env.
"""

from __future__ import annotations

import sys
import time
import datetime as dt
from functools import lru_cache

import pandas as pd
from kiteconnect import KiteConnect

import config


def get_kite() -> KiteConnect:
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    if config.KITE_ACCESS_TOKEN:
        kite.set_access_token(config.KITE_ACCESS_TOKEN)
    return kite


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def print_login_url() -> None:
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    print("Open this URL, log in, then copy request_token from redirect URL:")
    print(kite.login_url())


def exchange_request_token(request_token: str) -> str:
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    session = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    token = session["access_token"]
    _update_env("KITE_ACCESS_TOKEN", token)
    print("Access token saved to .env (valid until ~6 AM next day).")
    return token


def _update_env(key: str, value: str, path: str = ".env") -> None:
    lines, found = [], False
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + "="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    except FileNotFoundError:
        pass
    if not found:
        lines.append(f"{key}={value}\n")
    with open(path, "w") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Instruments & historical data
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def instrument_map() -> dict:
    """symbol -> instrument_token for NSE equities."""
    kite = get_kite()
    instruments = kite.instruments("NSE")
    return {
        row["tradingsymbol"]: row["instrument_token"]
        for row in instruments
        if row["segment"] == "NSE" and row["instrument_type"] == "EQ"
    }


_MAX_DAY_INTERVAL_SPAN = 2000  # Kite's historical API hard limit for "day" candles


def fetch_daily_candles(symbol: str, days: int = 400) -> pd.DataFrame:
    """Daily OHLCV for `symbol` covering the last `days` calendar days.

    Kite's historical API rejects a single "day"-interval request spanning
    more than ~2000 days ("interval exceeds max limit: 2000 days") -- deep
    backtests (5y history + lookback buffer) exceed that, so requests longer
    than the limit are split into sequential chunks and concatenated.
    """
    kite = get_kite()
    token = instrument_map().get(symbol)
    if token is None:
        raise ValueError(f"Unknown NSE symbol: {symbol}")
    to_date = dt.date.today()
    from_date = to_date - dt.timedelta(days=days)

    if days <= _MAX_DAY_INTERVAL_SPAN:
        candles = kite.historical_data(token, from_date, to_date, "day")
    else:
        candles = []
        chunk_start = from_date
        while chunk_start < to_date:
            chunk_end = min(chunk_start + dt.timedelta(days=_MAX_DAY_INTERVAL_SPAN),
                            to_date)
            candles += kite.historical_data(token, chunk_start, chunk_end, "day")
            chunk_start = chunk_end + dt.timedelta(days=1)
            if chunk_start < to_date:
                time.sleep(0.35)  # stay under the historical API rate limit

    df = pd.DataFrame(candles)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset="date").sort_values("date")
    return df.set_index("date")


def fetch_universe_candles(symbols: list[str], days: int = 400,
                           pause: float = 0.35) -> dict[str, pd.DataFrame]:
    """Fetch candles for many symbols, respecting Kite's ~3 req/s historical
    API rate limit."""
    out = {}
    for sym in symbols:
        try:
            out[sym] = fetch_daily_candles(sym, days)
        except Exception as e:  # keep going; surface errors in the dashboard
            out[sym] = pd.DataFrame()
            print(f"[warn] {sym}: {e}")
        time.sleep(pause)
    return out


def benchmark_candles(days: int = 400) -> pd.DataFrame:
    """NIFTY 50 index candles for relative-strength calculations."""
    kite = get_kite()
    # NIFTY 50 index token on Kite is 256265
    to_date = dt.date.today()
    from_date = to_date - dt.timedelta(days=days)

    if days <= _MAX_DAY_INTERVAL_SPAN:
        candles = kite.historical_data(256265, from_date, to_date, "day")
    else:
        candles = []
        chunk_start = from_date
        while chunk_start < to_date:
            chunk_end = min(chunk_start + dt.timedelta(days=_MAX_DAY_INTERVAL_SPAN),
                            to_date)
            candles += kite.historical_data(256265, chunk_start, chunk_end, "day")
            chunk_start = chunk_end + dt.timedelta(days=1)
            if chunk_start < to_date:
                time.sleep(0.35)

    df = pd.DataFrame(candles)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset="date").sort_values("date")
    return df.set_index("date")


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------

def get_positions() -> pd.DataFrame:
    kite = get_kite()
    pos = kite.positions().get("net", [])
    return pd.DataFrame(pos)


def get_holdings() -> pd.DataFrame:
    kite = get_kite()
    return pd.DataFrame(kite.holdings())


def get_margins() -> dict:
    kite = get_kite()
    return kite.margins()


def get_ltp(symbols: list[str]) -> dict[str, float]:
    kite = get_kite()
    keys = [f"NSE:{s}" for s in symbols]
    data = kite.ltp(keys)
    return {k.split(":")[1]: v["last_price"] for k, v in data.items()}


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def place_order(symbol: str, qty: int, side: str,
                product: str = "CNC", order_type: str = "MARKET",
                price: float | None = None) -> str:
    """Place an NSE equity order. Returns order_id.

    side: "BUY" | "SELL"
    product: "CNC" (delivery, right for 3-6 month holds) or "MIS" (intraday)
    """
    kite = get_kite()
    kwargs = dict(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NSE,
        tradingsymbol=symbol,
        transaction_type=side,
        quantity=int(qty),
        product=product,
        order_type=order_type,
    )
    if order_type == "LIMIT" and price:
        kwargs["price"] = price
    return kite.place_order(**kwargs)


def place_gtt_stoploss(symbol: str, qty: int, trigger_price: float,
                       last_price: float) -> int:
    """Place a GTT stop-loss (good for delivery positions held weeks/months —
    a plain SL order expires end of day, GTT persists)."""
    kite = get_kite()
    return kite.place_gtt(
        trigger_type=kite.GTT_TYPE_SINGLE,
        tradingsymbol=symbol,
        exchange=kite.EXCHANGE_NSE,
        trigger_values=[round(trigger_price, 1)],
        last_price=last_price,
        orders=[{
            "transaction_type": kite.TRANSACTION_TYPE_SELL,
            "quantity": int(qty),
            "product": kite.PRODUCT_CNC,
            "order_type": kite.ORDER_TYPE_LIMIT,
            "price": round(trigger_price * 0.995, 1),
        }],
    )["trigger_id"]


def square_off_position(symbol: str) -> str | None:
    """Close the net position in `symbol` at market. Handles both
    positions (MIS/NRML) and CNC holdings."""
    kite = get_kite()

    for p in kite.positions().get("net", []):
        if p["tradingsymbol"] == symbol and p["quantity"] != 0:
            side = "SELL" if p["quantity"] > 0 else "BUY"
            return place_order(symbol, abs(p["quantity"]), side,
                               product=p["product"])

    for h in kite.holdings():
        qty = h["quantity"] + h.get("t1_quantity", 0)
        if h["tradingsymbol"] == symbol and qty > 0:
            return place_order(symbol, qty, "SELL", product="CNC")

    return None


def get_orders() -> pd.DataFrame:
    kite = get_kite()
    return pd.DataFrame(kite.orders())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        print_login_url()
    elif len(sys.argv) > 2 and sys.argv[1] == "token":
        exchange_request_token(sys.argv[2])
    else:
        print("Usage: python kite_client.py login | token <request_token>")
