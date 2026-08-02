#!/usr/bin/env python3
"""
binance_futures.py — minimal SIGNED Binance USDT-M Futures REST client (no external deps).

Real-money order layer for the live trend executor. Uses only urllib + hmac (same as the rest of
this project). Keys come ONLY from env vars — this module never takes them as arguments and never
logs them:
    BINANCE_API_KEY / BINANCE_API_SECRET      (trade-enabled key; set on Railway, not in code)
    BINANCE_FUT_BASE   (default https://fapi.binance.com; testnet: https://testnet.binancefuture.com)

Safety: every state-changing call (order, cancel, leverage) is a no-op that just LOGS when DRY_RUN
is on (default). Flip DRY_RUN=false only after you've read the logs and trust the sizing.

Covers exactly what the strategy needs: exchange filters (tick/step/minNotional), balance,
position risk, set-leverage, market entry, STOP_MARKET reduce-only (exchange-resident stop),
cancel / cancel-all, and correct qty/price rounding to the symbol's filters.
"""
import hashlib, hmac, json, math, os, time
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError

DRY_RUN   = os.getenv("LIVE_DRY_RUN", "true").lower() == "true"
BASE      = os.getenv("BINANCE_FUT_BASE",
                      "https://testnet.binancefuture.com" if os.getenv("BINANCE_TESTNET", "false").lower() == "true"
                      else "https://fapi.binance.com").rstrip("/")
RECV_WINDOW = int(os.getenv("BINANCE_RECV_WINDOW", "5000"))


def _log(m): print(f"[BinanceFut] {m}", flush=True)


class BinanceError(Exception):
    pass


def _keys():
    k = os.getenv("BINANCE_API_KEY", ""); s = os.getenv("BINANCE_API_SECRET", "")
    if not k or not s:
        raise BinanceError("BINANCE_API_KEY / BINANCE_API_SECRET not set (set them as env vars, never in code)")
    return k, s


def _sign(params):
    _, secret = _keys()
    q = urlencode(params)
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q + "&signature=" + sig


def _request(method, path, params=None, signed=False, timeout=15):
    params = dict(params or {})
    headers = {"User-Agent": "trendlive/1.0"}
    url = f"{BASE}{path}"
    data = None
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = RECV_WINDOW
        key, _ = _keys()
        headers["X-MBX-APIKEY"] = key
        body = _sign(params)
        if method == "GET":
            url = f"{url}?{body}"
        else:
            data = body.encode()
    elif params:
        url = f"{url}?{urlencode(params)}"
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as x:
            return json.loads(x.read() or "{}")
    except HTTPError as e:
        detail = e.read().decode(errors="ignore")
        raise BinanceError(f"{method} {path} -> HTTP {e.code}: {detail}")
    except Exception as e:
        raise BinanceError(f"{method} {path} -> {e}")


# ---------- public / market data ----------
_FILTERS = {}


def load_filters(symbols=None):
    """Cache tick/step/minNotional/minQty per symbol from exchangeInfo."""
    info = _request("GET", "/fapi/v1/exchangeInfo")
    for s in info.get("symbols", []):
        sym = s["symbol"]
        if symbols and sym not in symbols:
            continue
        f = {"pricePrecision": s.get("pricePrecision"), "quantityPrecision": s.get("quantityPrecision")}
        for flt in s.get("filters", []):
            t = flt["filterType"]
            if t == "PRICE_FILTER": f["tickSize"] = float(flt["tickSize"])
            elif t == "LOT_SIZE": f["stepSize"] = float(flt["stepSize"]); f["minQty"] = float(flt["minQty"])
            elif t == "MIN_NOTIONAL": f["minNotional"] = float(flt.get("notional", flt.get("minNotional", 0)))
        _FILTERS[sym] = f
    return _FILTERS


def filters(symbol):
    if symbol not in _FILTERS:
        load_filters([symbol])
    return _FILTERS.get(symbol, {})


def _floor_step(x, step):
    return math.floor(x / step + 1e-9) * step if step else x


def _ceil_step(x, step):
    return math.ceil(x / step - 1e-9) * step if step else x


def round_qty(symbol, qty):
    f = filters(symbol); step = f.get("stepSize", 0.0)
    return round(_floor_step(abs(qty), step), f.get("quantityPrecision", 8))


def round_price(symbol, price):
    f = filters(symbol); tick = f.get("tickSize", 0.0)
    return round(_floor_step(price, tick), f.get("pricePrecision", 8))


def qty_for_notional(symbol, notional, price):
    """Smallest step-aligned qty whose notional >= `notional` AND >= the symbol's minQty."""
    f = filters(symbol); step = f.get("stepSize", 0.0); minq = f.get("minQty", 0.0) or 0.0
    q = max(_ceil_step(notional / price, step), minq)
    return round(q, f.get("quantityPrecision", 8))


def min_notional(symbol):
    return filters(symbol).get("minNotional", 5.0)


def mark_price(symbol):
    d = _request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
    d = d[0] if isinstance(d, list) else d
    return float(d["markPrice"])


def book_spread(symbol):
    """Return (spread_fraction, mid) from best bid/ask, or (None, None). Liquidity/friction gauge."""
    try:
        d = _request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        d = d[0] if isinstance(d, list) else d
        bid = float(d["bidPrice"]); ask = float(d["askPrice"])
        if bid <= 0 or ask <= 0:
            return None, None
        mid = (bid + ask) / 2.0
        return (ask - bid) / mid, mid
    except Exception:
        return None, None


# ---------- account (signed, read-only) ----------
def usdt_balance():
    d = _request("GET", "/fapi/v2/balance", signed=True)
    for a in d:
        if a.get("asset") == "USDT":
            return float(a.get("availableBalance", a.get("balance", 0)))
    return 0.0


def wallet_equity():
    """True account equity = wallet balance + unrealized PnL (locked margin still counts).
    Use this for risk checks; usdt_balance() (available) drops when margin is locked and must NOT
    be used as equity."""
    d = _request("GET", "/fapi/v2/account", signed=True)
    return float(d.get("totalMarginBalance") or d.get("totalWalletBalance") or 0.0)


def position(symbol):
    """Return {'amt','entry','unreal','leverage'} for a symbol (amt signed: + long / - short)."""
    d = _request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
    d = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    amt = float(d.get("positionAmt", 0) or 0)
    return {"amt": amt, "entry": float(d.get("entryPrice", 0) or 0),
            "unreal": float(d.get("unRealizedProfit", 0) or 0),
            "leverage": float(d.get("leverage", 1) or 1)}


def open_orders(symbol):
    return _request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)


# ---------- state-changing (guarded by DRY_RUN) ----------
def set_leverage(symbol, lev):
    lev = int(max(1, min(125, round(lev))))
    if DRY_RUN:
        _log(f"[DRY] set_leverage {symbol} {lev}x"); return {"leverage": lev, "dry": True}
    return _request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev}, signed=True)


def market_order(symbol, side, qty, reduce_only=False, client_id=None):
    """side = BUY / SELL. qty already rounded & sign-less."""
    qty = round_qty(symbol, qty)
    if qty <= 0:
        raise BinanceError(f"market_order {symbol}: qty rounds to 0 (below step/minQty)")
    p = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}
    if reduce_only: p["reduceOnly"] = "true"
    if client_id: p["newClientOrderId"] = client_id
    if DRY_RUN:
        _log(f"[DRY] MARKET {side} {qty} {symbol}{' reduceOnly' if reduce_only else ''}")
        return {"dry": True, "symbol": symbol, "side": side, "qty": qty, "type": "MARKET"}
    _log(f"MARKET {side} {qty} {symbol}{' reduceOnly' if reduce_only else ''}")
    return _request("POST", "/fapi/v1/order", p, signed=True)


def stop_market(symbol, side, stop_price, close_position=True, qty=None, client_id=None):
    """Exchange-resident stop that fires even if the bot dies. side = closing side (SELL for long)."""
    stop_price = round_price(symbol, stop_price)
    p = {"symbol": symbol, "side": side, "type": "STOP_MARKET", "stopPrice": stop_price,
         "workingType": "MARK_PRICE"}
    if close_position:
        p["closePosition"] = "true"
    else:
        p["quantity"] = round_qty(symbol, qty or 0); p["reduceOnly"] = "true"
    if client_id: p["newClientOrderId"] = client_id
    if DRY_RUN:
        _log(f"[DRY] STOP_MARKET {side} {symbol} stop={stop_price} closePos={close_position}")
        return {"dry": True, "symbol": symbol, "stopPrice": stop_price}
    _log(f"STOP_MARKET {side} {symbol} stop={stop_price} closePos={close_position}")
    return _request("POST", "/fapi/v1/order", p, signed=True)


def cancel_order(symbol, order_id=None, client_id=None):
    if DRY_RUN:
        _log(f"[DRY] cancel {symbol} id={order_id or client_id}"); return {"dry": True}
    p = {"symbol": symbol}
    if order_id: p["orderId"] = order_id
    if client_id: p["origClientOrderId"] = client_id
    return _request("DELETE", "/fapi/v1/order", p, signed=True)


def cancel_all(symbol):
    if DRY_RUN:
        _log(f"[DRY] cancel_all {symbol}"); return {"dry": True}
    return _request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)


if __name__ == "__main__":
    # quick connectivity/read check (no orders). Safe: only public + read-only signed calls.
    print("BASE:", BASE, "DRY_RUN:", DRY_RUN)
    try:
        load_filters(["BTCUSDT"]); print("BTCUSDT filters:", filters("BTCUSDT"))
        print("mark BTCUSDT:", mark_price("BTCUSDT"))
        if os.getenv("BINANCE_API_KEY"):
            print("USDT balance:", usdt_balance())
    except Exception as e:
        print("check failed:", e)
