#!/usr/bin/env python3
"""
trend_paper_bot.py — PAPER bot: MULTI-COIN long/short TREND basket with dynamic leverage 1x-5x.

Runs the SAME validated trend logic on several coins IN PARALLEL (BTC/ETH/SOL/BNB/XRP by default),
so there are always a few positions running -> more activity + diversification (smoother equity),
instead of one lonely slow position.

Per coin:
  * direction = sign of blended momentum (30/40/50 daily bars)   [WHY logged]
  * leverage  = clip(vol_target / realized_daily_vol, 1x, 5x)    [big only when calm]
  * bet       = $10 margin; notional = dir * leverage * $10
  * MONITOR intraday (~15 min): TAKE-PROFIT at +20% price move; LIQUIDATION if adverse >= 1/lev
    (loses only that bet's margin); otherwise HOLD the loser until the trend REVERSES (no tight stop).

Account: one shared balance. free_balance = cash not locked; equity = free + margin + unrealized P&L.
A new position only opens if there's free cash for its margin. PAPER only. Logs to Google Sheets, Redis-persisted.
"""
import json, math, os, time
from urllib.request import urlopen, Request

FUT  = "https://fapi.binance.com/fapi/v1"
OKX  = "https://www.okx.com/api/v5"

# ---- config ----
ASSETS      = [a.strip().upper() for a in
               os.getenv("TP_ASSETS", os.getenv("TP_ASSET", "BTC,ETH,SOL,BNB,XRP")).split(",") if a.strip()]
LOOKBACKS   = [int(x) for x in os.getenv("TP_LOOKBACKS", "40,60,90").split(",")]  # walk-forward best (robust)
TAKE_PROFIT = float(os.getenv("TP_TAKE_PROFIT", "0.20"))
# --- exit mode: "trail" = trailing-stop (breakeven->profit-lock) + pyramid-UP (validated +EV daily,
#     beats fixed TP+20%); "fixed" = legacy +20% take-profit / hold loser to reversal ---
TP_EXIT     = os.getenv("TP_EXIT_MODE", "trail").lower()
TRAIL_STOP0 = float(os.getenv("TP_TRAIL_STOP0", "0.10"))   # initial stop distance (price move vs entry)
TRAIL_PCT   = float(os.getenv("TP_TRAIL_PCT", "0.10"))     # trail width once in profit
TRAIL_BE    = float(os.getenv("TP_TRAIL_BE", "0.06"))      # ratchet stop to breakeven after +this fav move
TRAIL_PYR   = float(os.getenv("TP_TRAIL_PYR", "0.12"))     # pyramid add every +this favorable step
TRAIL_ADDS  = int(os.getenv("TP_TRAIL_ADDS", "4"))         # max pyramid adds
TRAIL_ADDF  = float(os.getenv("TP_TRAIL_ADDF", "0.5"))     # add margin = this * BET_USD
# max loss per trade as a fraction of the position's MARGIN (stake). 0 = off. If lev*TRAIL_STOP0
# exceeds this, the initial stop is tightened so (lev * stop_dist) <= MAX_RISK. Lets you control
# stake-risk explicitly while keeping price-based stop placement (prevents leverage whipsaw).
MAX_RISK    = float(os.getenv("TP_MAX_RISK", "0.5"))


def _stop_dist(lev):
    """Initial stop distance as a PRICE move; capped so risk-in-stake (lev*dist) <= MAX_RISK."""
    lev = lev or 1.0
    return min(TRAIL_STOP0, MAX_RISK / lev) if MAX_RISK > 0 else TRAIL_STOP0
VOL_TARGET  = float(os.getenv("TP_VOL_TARGET", "0.025"))
VOL_LB      = int(os.getenv("TP_VOL_LB", "20"))
LEV_MIN     = float(os.getenv("TP_LEV_MIN", "1.0"))
LEV_MAX     = float(os.getenv("TP_LEV_MAX", "5.0"))
START_BAL   = float(os.getenv("TP_START_BAL", "50"))
BET_USD     = float(os.getenv("TP_BET_USD", "10"))         # margin per coin position
FEE_RATE    = float(os.getenv("TP_FEE_RATE", "0.0005"))
FUNDING_HRS = float(os.getenv("TP_FUNDING_HRS", "8"))
TP_POLL     = int(float(os.getenv("TP_POLL_SECS", "900")))
TP_TAB      = os.getenv("TP_SHEET_TAB", "Trend Paper Sim")
TP_SIG_TAB  = os.getenv("TP_SIG_TAB", "Trend Signals")
TP_ACCT_TAB = os.getenv("TP_ACCT_TAB", "Trend Account")    # account-level equity curve (1 row/poll)
LOG_SIGNALS = os.getenv("TP_LOG_SIGNALS", "true").lower() == "true"

_THEAD = ["asset", "opened_utc", "closed_utc", "dir", "why", "leverage", "notional", "margin", "entry_px",
          "take_profit_px", "liq_price", "cur_px", "fav_ret", "unreal_pnl", "exit_reason",
          "gross_pnl", "funding", "fees", "free_balance", "equity", "peak", "drawdown"]
_SHEAD = ["utc", "asset", "price", "mom_1", "mom_2", "mom_3", "signal_sum", "direction", "why",
          "leverage", "in_position", "entry_px", "notional", "margin", "take_profit_px", "liq_price",
          "unreal_pnl", "fav_ret", "funding_rate", "free_balance", "equity", "liquidated"]
_AHEAD = ["utc", "open_positions", "free_balance", "margin_used", "unreal_pnl", "equity", "peak",
          "drawdown", "total_fees_paid", "liquidated", "positions"]


def _log(m): print(f"[TrendPaper] {m}", flush=True)


def _get(url, timeout=15):
    r = Request(url, headers={"User-Agent": "trendpaper/1.0"})
    with urlopen(r, timeout=timeout) as x:
        return json.loads(x.read())


def daily_closes(asset):
    try:
        data = _get(f"{FUT}/klines?symbol={asset}USDT&interval=1d&limit=200")
        if data and len(data) > 5:
            conf = data[:-1]
            return [float(k[4]) for k in conf], int(conf[-1][0])
    except Exception:
        pass
    d = _get(f"{OKX}/market/candles?instId={asset}-USDT-SWAP&bar=1Dutc&limit=200")["data"]
    conf = list(reversed([r for r in d if r[-1] == "1"]))
    return [float(r[4]) for r in conf], int(conf[-1][0])


def price_now(asset):
    try:
        return float(_get(f"{FUT}/ticker/price?symbol={asset}USDT")["price"])
    except Exception:
        pass
    try:
        return float(_get(f"{OKX}/market/ticker?instId={asset}-USDT-SWAP")["data"][0]["last"])
    except Exception:
        return None


def funding_rate(asset):
    try:
        d = _get(f"{FUT}/premiumIndex?symbol={asset}USDT")
        d = d[0] if isinstance(d, list) else d
        return float(d.get("lastFundingRate"))
    except Exception:
        pass
    try:
        return float(_get(f"{OKX}/public/funding-rate?instId={asset}-USDT-SWAP")["data"][0]["fundingRate"])
    except Exception:
        return 0.0


def signal_and_lev(closes):
    n = len(closes)
    if n < max(LOOKBACKS) + 1 + VOL_LB:
        return None
    s = 0; moms = {}; refs = {}
    for Lk in LOOKBACKS:
        ref = closes[-1 - Lk]
        v = 1 if closes[-1] > ref else -1
        moms[Lk] = v; refs[Lk] = ref; s += v
    direction = 1 if s > 0 else (-1 if s < 0 else 0)
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(n - VOL_LB, n)]
    m = sum(rets) / len(rets)
    rv = math.sqrt(sum((x - m) ** 2 for x in rets) / len(rets))
    lev = LEV_MIN if rv <= 0 else max(LEV_MIN, min(LEV_MAX, VOL_TARGET / rv))
    return {"dir": direction, "signal": s, "moms": moms, "refs": refs, "rv": rv, "lev": lev}


def reason_str(sl, price):
    ups = sum(1 for v in sl["moms"].values() if v > 0)
    detail = " ".join(f"{Lk}d:{'up' if sl['moms'][Lk] > 0 else 'dn'}(${sl['refs'][Lk]:.0f})"
                      for Lk in sorted(sl["moms"]))
    side = "LONG" if sl["dir"] > 0 else ("SHORT" if sl["dir"] < 0 else "FLAT")
    return f"px ${price:.2f} vs {detail} = {ups}/{len(sl['moms'])} up -> {side}"


def exit_levels(d, entry, lev):
    if not entry or not d:
        return "", ""
    if d > 0:
        return entry * (1 + TAKE_PROFIT), entry * (1 - 1.0 / lev)
    return entry * (1 - TAKE_PROFIT), entry * (1 + 1.0 / lev)


def blank_pos():
    return {"dir": 0, "block_dir": 0, "lev": 1.0, "notional": 0.0, "entry_px": None, "entry_ts": 0.0,
            "open_fee": 0.0, "opened_utc": "", "reason": "", "margin": 0.0,
            "stop_px": None, "units": 0.0, "adds": 0}   # trailing-stop + pyramid state


# ---- Google Sheets ----
def _open_ws(tab, head):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
        sid = (os.getenv("TP_SPREADSHEET_ID")
               or os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "135W_9kEaifBQeRjH6hZOjBaZHIvngOQi-Fm4af_1EE0"))
        if not creds:
            return None
        info = json.loads(creds) if creds.strip().startswith("{") else json.load(open(creds))
        gc = gspread.authorize(Credentials.from_service_account_info(
            info, scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]))
        sh = gc.open_by_key(sid)
        try:
            w = sh.worksheet(tab)
            try:
                if w.row_values(1) != head:
                    w.update([head], "A1", value_input_option="USER_ENTERED")
            except Exception:
                pass
            return w
        except Exception:
            w = sh.add_worksheet(tab, rows=2000, cols=len(head)); w.append_row(head); return w
    except Exception as e:
        _log(f"sheets off ({tab}): {e}"); return None


def _append(ws, row):
    if ws is None:
        return
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        _log(f"log warn: {e}")


def _upsert_trade(ws, row):
    """One row per trade, keyed by asset (col A) + opened_utc (col B); updates live, finalizes on close."""
    if ws is None:
        return
    ka, kb = row[0], row[1]
    try:
        colA = ws.col_values(1); colB = ws.col_values(2)
        r = next((i + 1 for i in range(len(colA))
                  if colA[i] == ka and (colB[i] if i < len(colB) else "") == kb), None)
        if r:
            ws.update([row], f"A{r}", value_input_option="USER_ENTERED")
        else:
            ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        _log(f"trade-log warn: {e}")


def _trade_row(asset, p, price, exit_reason, closed_utc, gross, funding, fees, unreal, free_bal, equity, peak):
    lev = p["lev"] or 1.0
    tp_px, liq_px = exit_levels(p["dir"], p["entry_px"], lev)
    fav = p["dir"] * (price / p["entry_px"] - 1.0) if (p["dir"] and p["entry_px"]) else 0.0
    dd = (peak - equity) / peak if peak > 0 else 0.0
    return [asset, p["opened_utc"], closed_utc, "LONG" if p["dir"] > 0 else "SHORT", p.get("reason", ""),
            round(lev, 2), round(p["notional"], 2), round(p["margin"], 2),
            round(p["entry_px"], 2) if p["entry_px"] else "",
            round(tp_px, 2) if tp_px else "", round(liq_px, 2) if liq_px else "", round(price, 2),
            round(fav, 4), round(unreal, 3), exit_reason, round(gross, 3), round(funding, 4), round(fees, 4),
            round(free_bal, 2), round(equity, 2), round(peak, 2), round(dd, 4)]


def _redis():
    try:
        import redis
        url = os.getenv("REDIS_URL") or os.getenv("REDIS_PRIVATE_URL") or os.getenv("REDIS_PUBLIC_URL")
        if not url:
            return None
        r = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        r.ping(); return r
    except Exception:
        return None


def run():
    exitdesc = (f"TRAIL-STOP {TRAIL_STOP0:.0%}/trail {TRAIL_PCT:.0%} (BE@+{TRAIL_BE:.0%}) + PYRAMID<= {TRAIL_ADDS} adds, "
                f"max-risk {MAX_RISK:.0%} of stake/trade" if TP_EXIT != "fixed"
                else f"FIXED TP +{TAKE_PROFIT:.0%}, loser held to reversal")
    _log(f"start — ${START_BAL:.0f} bal, bet ${BET_USD:.0f}/coin, BASKET {ASSETS}, trend {LOOKBACKS}, "
         f"exit={TP_EXIT} [{exitdesc}], lev {LEV_MIN:.0f}-{LEV_MAX:.0f}x, "
         f"intraday every {TP_POLL}s -> tab '{TP_TAB}'")
    ws = _open_ws(TP_TAB, _THEAD); sig_ws = _open_ws(TP_SIG_TAB, _SHEAD) if LOG_SIGNALS else None
    acct_ws = _open_ws(TP_ACCT_TAB, _AHEAD)
    r = _redis()
    acct = {"cash": START_BAL, "peak": START_BAL, "liquidated": False, "fees_paid": 0.0,
            "pos": {a: blank_pos() for a in ASSETS}}
    if r:
        try:
            saved = json.loads(r.get("trendpaper:state") or "{}")
            if saved and "cash" in saved and "pos" in saved:
                acct["cash"] = saved["cash"]; acct["peak"] = saved.get("peak", START_BAL)
                acct["liquidated"] = saved.get("liquidated", False)
                acct["fees_paid"] = saved.get("fees_paid", 0.0)
                for a in ASSETS:                      # keep saved position if present, else blank
                    acct["pos"][a] = saved["pos"].get(a, blank_pos())
        except Exception:
            pass

    def enter(a, d, lev, price, now, why):
        p = acct["pos"][a]
        p["dir"] = d; p["lev"] = lev; p["notional"] = round(d * lev * BET_USD, 2)
        p["entry_px"] = price; p["entry_ts"] = now; p["reason"] = why
        margin = abs(p["notional"]) / (lev or 1.0)
        p["open_fee"] = FEE_RATE * abs(p["notional"]); p["margin"] = round(margin, 4)
        p["units"] = p["notional"] / price; p["adds"] = 0    # trailing/pyramid state
        sd = _stop_dist(lev)
        p["stop_px"] = price * (1 - sd) if d > 0 else price * (1 + sd)
        acct["cash"] -= (margin + p["open_fee"]); acct["fees_paid"] += p["open_fee"]
        p["opened_utc"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))
        _log(f"{a} OPEN {'LONG' if d>0 else 'SHORT'} lev={lev:.2f}x notional=${p['notional']:+.2f} "
             f"margin=${margin:.2f} @ {price:.4f} | free=${acct['cash']:.2f} | {why}")

    def close(a, price, fr, now, reason):
        p = acct["pos"][a]; notl = p["notional"]; margin = p["margin"]
        unreal = notl * (price / p["entry_px"] - 1.0)
        if unreal <= -margin:
            unreal = -margin
            reason = "liquidated" if reason != "take_profit" else reason
        fsign = 1.0 if notl > 0 else -1.0
        funding = -fsign * fr * abs(notl) * ((now - p["entry_ts"]) / 3600.0 / FUNDING_HRS)
        close_fee = FEE_RATE * abs(notl); acct["fees_paid"] += close_fee
        acct["cash"] += margin + unreal + funding - close_fee
        closed_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))
        peak = max(acct["peak"], acct["cash"])
        _upsert_trade(ws, _trade_row(a, p, price, reason, closed_utc, unreal, funding,
                                     p["open_fee"] + close_fee, unreal, acct["cash"], acct["cash"], peak))
        _log(f"{a} CLOSE {reason} pnl=${unreal:+.2f} -> free=${acct['cash']:.2f}")
        d_closed = p["dir"]; blk = p["block_dir"]
        acct["pos"][a] = blank_pos(); acct["pos"][a]["block_dir"] = blk
        if acct["cash"] <= 0:
            acct["liquidated"] = True; acct["cash"] = 0.0
        return d_closed

    while True:
        try:
            snap = {}
            # ---- pass 1: fetch + manage each coin ----
            for a in ASSETS:
                try:
                    closes, _ = daily_closes(a)
                    sl = signal_and_lev(closes)
                    price = price_now(a) or (closes[-1] if closes else None)
                    fr = funding_rate(a)
                except Exception as e:
                    _log(f"{a} data error: {e}"); continue
                if sl is None or price is None:
                    continue
                now = time.time(); why = reason_str(sl, price)
                snap[a] = {"price": price, "sl": sl, "fr": fr, "why": why}
                p = acct["pos"][a]; sig_dir = sl["dir"]; lev = sl["lev"]

                if not acct["liquidated"] and p["dir"] != 0:
                    d = p["dir"]; lv = p["lev"] or 1.0
                    fav = d * (price / p["entry_px"] - 1.0)
                    reason = None
                    if TP_EXIT == "fixed":
                        if fav >= TAKE_PROFIT - 1e-9:
                            reason = "take_profit"
                        elif fav <= -1.0 / lv:
                            reason = "liquidated"
                        elif sig_dir != 0 and sig_dir != d:
                            reason = "trend_flip"
                    else:  # ---- trailing-stop + pyramid-UP (validated) ----
                        if p.get("stop_px") is None:            # heal legacy/fixed-mode positions
                            sd = _stop_dist(lv)
                            p["stop_px"] = p["entry_px"] * (1 - sd) if d > 0 else p["entry_px"] * (1 + sd)
                        if not p.get("units"):
                            p["units"] = p["notional"] / p["entry_px"]
                        p.setdefault("adds", 0)
                        # ratchet stop to breakeven then trail, once far enough in profit
                        if fav >= TRAIL_BE:
                            be = p["entry_px"]
                            tp = price * (1 - TRAIL_PCT) if d > 0 else price * (1 + TRAIL_PCT)
                            p["stop_px"] = max(p["stop_px"], be, tp) if d > 0 else min(p["stop_px"], be, tp)
                        # pyramid UP each further step (only WITH the trend), each add raises the stop
                        if (p["adds"] < TRAIL_ADDS and fav >= TRAIL_PYR * (p["adds"] + 1)
                                and (sig_dir == 0 or sig_dir == d)
                                and acct["cash"] >= BET_USD * TRAIL_ADDF * (1 + FEE_RATE)):
                            add_m = BET_USD * TRAIL_ADDF; add_notl = d * lv * add_m
                            fee = FEE_RATE * abs(add_notl); acct["fees_paid"] += fee
                            p["notional"] += add_notl; p["units"] += add_notl / price
                            p["entry_px"] = p["notional"] / p["units"]; p["margin"] += add_m
                            acct["cash"] -= (add_m + fee); p["open_fee"] += fee; p["adds"] += 1
                            tp = price * (1 - TRAIL_PCT) if d > 0 else price * (1 + TRAIL_PCT)
                            p["stop_px"] = max(p["stop_px"], tp) if d > 0 else min(p["stop_px"], tp)
                            _log(f"{a} PYRAMID add#{p['adds']} +${add_m:.1f} margin -> avg {p['entry_px']:.4f} "
                                 f"stop {p['stop_px']:.4f} | free=${acct['cash']:.2f}")
                        # exits: liquidation, trailing-stop hit, or trend flip (backstop)
                        if fav <= -1.0 / lv:
                            reason = "liquidated"
                        elif p["stop_px"] and ((d > 0 and price <= p["stop_px"]) or (d < 0 and price >= p["stop_px"])):
                            reason = "trail_stop"
                        elif sig_dir != 0 and sig_dir != d:
                            reason = "trend_flip"
                    if reason:
                        d_closed = close(a, price, fr, now, reason)
                        if reason == "trend_flip" and sig_dir != 0 and not acct["liquidated"] \
                                and acct["cash"] >= BET_USD * (1 + FEE_RATE):
                            enter(a, sig_dir, lev, price, now, why)
                        elif reason in ("take_profit", "liquidated", "trail_stop"):
                            acct["pos"][a]["block_dir"] = d_closed

                p = acct["pos"][a]
                if not acct["liquidated"] and p["dir"] == 0:
                    if sig_dir != p["block_dir"]:
                        p["block_dir"] = 0
                    if sig_dir != 0 and sig_dir != p["block_dir"] and acct["cash"] >= BET_USD * (1 + FEE_RATE):
                        enter(a, sig_dir, lev, price, now, why)

            # ---- equity across the whole basket ----
            equity = acct["cash"]
            for a in ASSETS:
                p = acct["pos"][a]
                if p["dir"] != 0 and p["entry_px"] and a in snap:
                    u = p["notional"] * (snap[a]["price"] / p["entry_px"] - 1.0)
                    if u < -p["margin"]: u = -p["margin"]
                    equity += p["margin"] + u
            acct["peak"] = max(acct["peak"], equity)
            dd = (acct["peak"] - equity) / acct["peak"] if acct["peak"] > 0 else 0.0

            # ---- pass 2: write rows ----
            nows = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            for a in ASSETS:
                if a not in snap:
                    continue
                p = acct["pos"][a]; price = snap[a]["price"]; sl = snap[a]["sl"]; fr = snap[a]["fr"]
                unreal = 0.0
                if p["dir"] != 0 and p["entry_px"]:
                    unreal = p["notional"] * (price / p["entry_px"] - 1.0)
                    if unreal < -p["margin"]: unreal = -p["margin"]
                fav = p["dir"] * (price / p["entry_px"] - 1.0) if (p["dir"] and p["entry_px"]) else 0.0
                if p["dir"] != 0 and p["entry_px"] and not acct["liquidated"]:
                    _upsert_trade(ws, _trade_row(a, p, price, "open", "", 0.0, 0.0, p["open_fee"],
                                                 unreal, acct["cash"], equity, acct["peak"]))
                if sig_ws is not None:
                    m = sl["moms"]; mk = sorted(m)
                    tp_px, liq_px = exit_levels(p["dir"], p["entry_px"], p["lev"] or 1.0)
                    _append(sig_ws, [nows, a, round(price, 4), m[mk[0]],
                                     m[mk[1]] if len(mk) > 1 else "", m[mk[2]] if len(mk) > 2 else "",
                                     sl["signal"], sl["dir"], snap[a]["why"], round(sl["lev"], 2),
                                     p["dir"] != 0, round(p["entry_px"], 4) if p["entry_px"] else "",
                                     round(p["notional"], 2), round(p["margin"], 2) if p["dir"] else "",
                                     round(tp_px, 2) if tp_px else "", round(liq_px, 2) if liq_px else "",
                                     round(unreal, 3), round(fav, 4), round(fr, 6),
                                     round(acct["cash"], 2), round(equity, 2), acct["liquidated"]])

            open_n = sum(1 for a in ASSETS if acct["pos"][a]["dir"] != 0)
            margin_used = sum(acct["pos"][a]["margin"] for a in ASSETS if acct["pos"][a]["dir"] != 0)
            positions_str = " ".join(
                f"{a}:{'L' if acct['pos'][a]['dir']>0 else 'S'}{acct['pos'][a]['lev']:.1f}x"
                for a in ASSETS if acct["pos"][a]["dir"] != 0) or "flat"
            _append(acct_ws, [nows, open_n, round(acct["cash"], 2), round(margin_used, 2),
                              round(equity - acct["cash"] - margin_used, 3), round(equity, 2),
                              round(acct["peak"], 2), round(dd, 4), round(acct["fees_paid"], 4),
                              acct["liquidated"], positions_str])
            _log(f"heartbeat — open {open_n}/{len(ASSETS)} | free=${acct['cash']:.2f} equity=${equity:.2f} dd={dd:.1%}")
            if r:
                try:
                    r.set("trendpaper:state", json.dumps(acct))
                except Exception:
                    pass
        except Exception as e:
            _log(f"loop error: {e}")
        time.sleep(max(300, TP_POLL))


if __name__ == "__main__":
    run()
