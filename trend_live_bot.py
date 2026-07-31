#!/usr/bin/env python3
"""
trend_live_bot.py — REAL-MONEY executor for the validated daily TREND basket (trail mode).

Mirrors trend_paper_bot's edge exactly: daily blended-momentum direction, vol-targeted leverage,
trailing-stop (breakeven -> profit-lock) + pyramid-UP. The difference is EXECUTION: real Binance
USDT-M Futures orders via binance_futures.py, with an EXCHANGE-RESIDENT STOP_MARKET so the stop
fires even if this process dies.

SAFETY (read before going live):
  * LIVE_ENABLED=false by default  -> the loop only observes/logs, sends nothing.
  * LIVE_DRY_RUN=true  by default   -> even when enabled, orders are LOGGED not sent (binance_futures).
  * Hard caps: max notional/position, max total exposure, max concurrent, min-notional enforced.
  * Daily-loss kill switch: if equity drops DAILY_LOSS_STOP below the day's start -> flatten + halt.
  * The EXCHANGE is the source of truth for positions; we reconcile every poll (no double-open).

Keys come only from env (BINANCE_API_KEY/SECRET) and are handled by binance_futures — never here.
Go-live path: run with DRY_RUN=true, read the [DRY] logs, confirm sizes, THEN set LIVE_DRY_RUN=false
and LIVE_ENABLED=true with tiny capital.
"""
import json, os, time

import binance_futures as bf
import trend_paper_bot as tp   # reuse the validated signal / leverage / trail params

# ---- which markets + sizing ----
ASSETS      = [a.strip().upper() for a in os.getenv("LIVE_ASSETS", "BTC,ETH").split(",") if a.strip()]
SYMBOL      = {a: f"{a}USDT" for a in ASSETS}
BET_USD     = float(os.getenv("LIVE_BET_USD", "1"))      # margin per position (your stake at risk)
LEV_MIN     = float(os.getenv("LIVE_LEV_MIN", "1"))
LEV_MAX     = float(os.getenv("LIVE_LEV_MAX", "5"))      # needed so $1 stake clears Binance min-notional (~$5)
POLL        = int(float(os.getenv("LIVE_POLL_SECS", "900")))

# ---- trailing / pyramid (reuse the validated defaults from the paper bot) ----
TRAIL_STOP0 = tp.TRAIL_STOP0
TRAIL_PCT   = tp.TRAIL_PCT
TRAIL_BE    = tp.TRAIL_BE
TRAIL_PYR   = tp.TRAIL_PYR
TRAIL_ADDS  = tp.TRAIL_ADDS
TRAIL_ADDF  = tp.TRAIL_ADDF
MAX_RISK    = tp.MAX_RISK

# ---- HARD safety rails ----
LIVE_ENABLED   = os.getenv("LIVE_ENABLED", "false").lower() == "true"
MAX_NOTL_POS   = float(os.getenv("LIVE_MAX_NOTIONAL_POS", "40"))    # per-position notional cap ($)
MAX_NOTL_TOTAL = float(os.getenv("LIVE_MAX_NOTIONAL_TOTAL", "80"))  # total exposure cap ($)
MAX_CONCURRENT = int(os.getenv("LIVE_MAX_CONCURRENT", "2"))
DAILY_LOSS_STOP = float(os.getenv("LIVE_DAILY_LOSS_STOP", "0.25"))  # halt if equity down 25% vs day start
POS_EPS        = float(os.getenv("LIVE_POS_EPS", "0"))             # treat |amt|<=eps as flat (0=exact)
FEE            = float(os.getenv("LIVE_FEE_RATE", "0.0005"))        # taker fee for the paper simulation
SIM_START_BAL  = float(os.getenv("LIVE_SIM_BAL", "0"))             # 0 = use the real balance at startup

TAB   = os.getenv("LIVE_SHEET_TAB", "Live Trades")
_HEAD = ["utc", "event", "asset", "side", "reason", "mark", "qty", "notional", "lev", "entry",
         "stop_px", "adds", "unreal", "realized_est", "free_usdt", "equity", "note"]


def _log(m): print(f"[TrendLive] {m}", flush=True)


def _public_ip():
    """Outbound IP of this service — the address to whitelist on the Binance key."""
    from urllib.request import urlopen
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://checkip.amazonaws.com"):
        try:
            return urlopen(url, timeout=8).read().decode().strip()
        except Exception:
            continue
    return "unknown"


def _tg(msg):
    tok = os.getenv("TELEGRAM_BOT_TOKEN", ""); chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        return
    try:
        from urllib.parse import quote
        from urllib.request import urlopen, Request
        urlopen(Request(f"https://api.telegram.org/bot{tok}/sendMessage?chat_id={chat}&text={quote(msg)}"),
                timeout=8).read()
    except Exception:
        pass


# ---- Google Sheets (same pattern as the paper bots) ----
def _open_ws():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
        sid = (os.getenv("LIVE_SPREADSHEET_ID") or os.getenv("TP_SPREADSHEET_ID")
               or os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "135W_9kEaifBQeRjH6hZOjBaZHIvngOQi-Fm4af_1EE0"))
        if not creds:
            return None
        info = json.loads(creds) if creds.strip().startswith("{") else json.load(open(creds))
        gc = gspread.authorize(Credentials.from_service_account_info(
            info, scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]))
        sh = gc.open_by_key(sid)
        try:
            w = sh.worksheet(TAB)
            if w.row_values(1) != _HEAD:
                w.update([_HEAD], "A1", value_input_option="USER_ENTERED")
            return w
        except Exception:
            w = sh.add_worksheet(TAB, rows=4000, cols=len(_HEAD)); w.append_row(_HEAD); return w
    except Exception as e:
        _log(f"sheets off: {e}"); return None


def _row(ws, event, asset, side, reason, mark, qty, notl, lev, entry, stop, adds, unreal, realized,
         free, equity, note=""):
    if ws is None:
        return
    try:
        ws.append_row([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), event, asset, side, reason,
                       round(mark, 6), round(qty, 6), round(notl, 2), round(lev, 2),
                       round(entry, 6) if entry else "", round(stop, 6) if stop else "", adds,
                       round(unreal, 4), round(realized, 4), round(free, 2), round(equity, 2), note],
                      value_input_option="USER_ENTERED")
    except Exception as e:
        _log(f"log warn: {e}")


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


def _stop_dist(lev):
    lev = lev or 1.0
    return min(TRAIL_STOP0, MAX_RISK / lev) if MAX_RISK > 0 else TRAIL_STOP0


def _clamp_lev(lev):
    return max(LEV_MIN, min(LEV_MAX, lev))


def _equity(balance, marks):
    eq = balance
    for a in ASSETS:
        pos = bf.position(SYMBOL[a])
        if abs(pos["amt"]) > POS_EPS:
            eq += pos["unreal"]
    return eq


# ================= PAPER SIMULATION (DRY mode) =================
# In DRY mode there are no real fills, so instead of re-reading the (empty) exchange position every
# poll, we keep a VIRTUAL account: real prices, same trailing-stop + pyramid logic, tracked P&L and
# projected equity -> so "Live Trades" shows realistic numbers of where the account would be.

def _blank_sim():
    return {"dir": 0, "qty": 0.0, "avg": 0.0, "margin": 0.0, "stop": None, "adds": 0, "block": 0}


def _sim_equity(acct, snap):
    eq = acct["cash"]
    for a in ASSETS:
        p = acct["pos"][a]
        if p["dir"] != 0 and a in snap:
            eq += p["margin"] + p["dir"] * p["qty"] * (snap[a]["mark"] - p["avg"])
    return eq


def _sim_open(acct, a, d, lev, mark, why, ws, snap):
    sym = SYMBOL[a]
    target = max(BET_USD * lev, bf.min_notional(sym))
    qty = bf.qty_for_notional(sym, target, mark); notl = qty * mark
    if notl <= 0 or notl < bf.min_notional(sym) or notl > MAX_NOTL_POS:
        _log(f"[SIM] {a} skip size ${notl:.2f}"); return
    margin = notl / lev; fee = notl * FEE
    if acct["cash"] < margin + fee:
        return
    acct["cash"] -= (margin + fee); acct["fees"] += fee
    sd = _stop_dist(lev); stop = mark * (1 - sd) if d > 0 else mark * (1 + sd)
    acct["pos"][a] = {"dir": d, "qty": qty, "avg": mark, "margin": margin, "stop": stop, "adds": 0, "block": 0}
    eq = _sim_equity(acct, snap)
    _row(ws, "OPEN", a, "LONG" if d > 0 else "SHORT", why, mark, qty, notl, lev, mark, stop, 0, 0.0,
         acct["realized"], acct["cash"], eq, "SIM")
    _log(f"[SIM] {a} OPEN {'LONG' if d>0 else 'SHORT'} qty={qty} notl=${notl:.2f} stake=${margin:.2f} "
         f"lev={lev:.1f}x stop={stop:.6f} | {why}")


def _run_sim(ws, r):
    keys = bool(os.getenv("BINANCE_API_KEY"))
    try:
        start = SIM_START_BAL or (bf.usdt_balance() if keys else 30.0) or 30.0
    except Exception:
        start = SIM_START_BAL or 30.0
    acct = {"cash": start, "realized": 0.0, "fees": 0.0, "peak": start, "pos": {a: _blank_sim() for a in ASSETS}}
    if r:
        try:
            saved = json.loads(r.get("trendlive:sim") or "{}")
            if saved and "cash" in saved and "pos" in saved:
                acct = saved
                for a in ASSETS:
                    acct["pos"].setdefault(a, _blank_sim())
        except Exception:
            pass
    _log(f"SIM/paper mode (DRY_RUN) — projected account start ${acct['cash']:.2f}, {ASSETS}. "
         f"No real orders; 'Live Trades' shows the simulated forecast.")
    _tg(f"📊 TrendLive SIM (paper forecast) start ${acct['cash']:.2f} {ASSETS}")

    while True:
        try:
            snap = {}
            for a in ASSETS:
                try:
                    closes, _ = tp.daily_closes(a); sl = tp.signal_and_lev(closes); mark = bf.mark_price(SYMBOL[a])
                except Exception as e:
                    _log(f"{a} data error: {e}"); continue
                if sl is None or not mark:
                    continue
                snap[a] = {"mark": mark, "sl": sl}
                sig = sl["dir"]; lev = _clamp_lev(sl["lev"]); why = tp.reason_str(sl, mark)
                p = acct["pos"][a]

                # ---- manage an open sim position ----
                if p["dir"] != 0:
                    d = p["dir"]; fav = d * (mark / p["avg"] - 1.0)
                    if fav >= TRAIL_BE:
                        tr = mark * (1 - TRAIL_PCT) if d > 0 else mark * (1 + TRAIL_PCT)
                        p["stop"] = max(p["stop"], p["avg"], tr) if d > 0 else min(p["stop"], p["avg"], tr)
                    add_notl = BET_USD * TRAIL_ADDF * lev
                    if (p["adds"] < TRAIL_ADDS and fav >= TRAIL_PYR * (p["adds"] + 1) and (sig == 0 or sig == d)
                            and acct["cash"] >= BET_USD * TRAIL_ADDF * (1 + FEE)):
                        addq = add_notl / mark; fee = add_notl * FEE
                        tot = p["qty"] + addq
                        p["avg"] = (p["avg"] * p["qty"] + mark * addq) / tot; p["qty"] = tot
                        p["margin"] += BET_USD * TRAIL_ADDF; acct["cash"] -= (BET_USD * TRAIL_ADDF + fee)
                        acct["fees"] += fee; p["adds"] += 1
                        tr = mark * (1 - TRAIL_PCT) if d > 0 else mark * (1 + TRAIL_PCT)
                        p["stop"] = max(p["stop"], tr) if d > 0 else min(p["stop"], tr)
                        _log(f"[SIM] {a} PYRAMID add#{p['adds']} -> avg {p['avg']:.6f} stop {p['stop']:.6f}")
                    stop_hit = (d > 0 and mark <= p["stop"]) or (d < 0 and mark >= p["stop"])
                    flip = sig != 0 and sig != d
                    if stop_hit or flip:
                        exitp = p["stop"] if stop_hit else mark
                        gross = d * p["qty"] * (exitp - p["avg"]); fee = abs(p["qty"] * exitp) * FEE
                        pnl = gross - fee
                        acct["cash"] += p["margin"] + pnl; acct["realized"] += pnl; acct["fees"] += fee
                        reason = "trail_stop" if stop_hit else "trend_flip"
                        qty_c, avg_c, stop_c, adds_c = p["qty"], p["avg"], p["stop"], p["adds"]
                        blk = d if reason == "trail_stop" else 0
                        acct["pos"][a] = _blank_sim(); acct["pos"][a]["block"] = blk   # remove BEFORE equity calc
                        eq = _sim_equity(acct, snap)
                        _row(ws, "CLOSE", a, "LONG" if d > 0 else "SHORT", reason, exitp, qty_c,
                             abs(qty_c * exitp), lev, avg_c, stop_c, adds_c, pnl, acct["realized"],
                             acct["cash"], eq, "SIM")
                        _log(f"[SIM] {a} CLOSE {reason} pnl=${pnl:+.2f} -> cash=${acct['cash']:.2f}")
                        if flip and sig != 0 and acct["cash"] >= BET_USD:
                            _sim_open(acct, a, sig, lev, mark, why, ws, snap)
                        continue

                # ---- open when flat ----
                if p["dir"] == 0 and sig != 0 and sig != p["block"]:
                    _sim_open(acct, a, sig, lev, mark, why, ws, snap)
                elif p["dir"] == 0 and sig == p["block"]:
                    pass  # blocked until the trend flips

            equity = _sim_equity(acct, snap); acct["peak"] = max(acct["peak"], equity)
            dd = (acct["peak"] - equity) / acct["peak"] if acct["peak"] > 0 else 0.0
            openn = sum(1 for a in ASSETS if acct["pos"][a]["dir"] != 0)
            _row(ws, "FORECAST", "-", "-", "sim equity", 0, 0, 0, 0, 0, "", openn, 0.0, acct["realized"],
                 acct["cash"], equity, f"open {openn}/{len(ASSETS)} dd={dd:.1%} fees=${acct['fees']:.2f}")
            _log(f"[SIM] equity=${equity:.2f} realized=${acct['realized']:+.2f} free=${acct['cash']:.2f} "
                 f"open={openn}/{len(ASSETS)} dd={dd:.1%}")
            if r:
                try: r.set("trendlive:sim", json.dumps(acct))
                except Exception: pass
        except Exception as e:
            _log(f"sim loop error: {e}")
        time.sleep(max(60, POLL))


def run():
    _log(f"start — LIVE_ENABLED={LIVE_ENABLED} DRY_RUN={bf.DRY_RUN} base={bf.BASE} | {ASSETS} "
         f"bet ${BET_USD}/coin lev {LEV_MIN:.0f}-{LEV_MAX:.0f}x | trail {TRAIL_STOP0:.0%}/BE{TRAIL_BE:.0%} "
         f"pyr<= {TRAIL_ADDS} | caps: pos<=${MAX_NOTL_POS} total<=${MAX_NOTL_TOTAL} "
         f"conc<={MAX_CONCURRENT} dayloss {DAILY_LOSS_STOP:.0%}")
    ip = _public_ip()
    _log(f"OUTBOUND IP = {ip}  <- whitelist THIS on your Binance API key (IP restriction)")
    _tg(f"🟢 TrendLive up — enabled={LIVE_ENABLED} dry={bf.DRY_RUN} {ASSETS} (bet ${BET_USD}, lev<= {LEV_MAX:.0f}x)\n"
        f"Outbound IP to whitelist: {ip}")
    ws = _open_ws()
    r = _redis()
    try:
        bf.load_filters([SYMBOL[a] for a in ASSETS])
    except Exception as e:
        _log(f"filters load failed (will retry in loop): {e}")

    if bf.DRY_RUN:                       # DRY = run the paper simulation (real numbers, no orders)
        _run_sim(ws, r)
        return

    # per-symbol intended state we own: {dir, adds, stop_px}. Exchange is source of truth for size/entry.
    state = {a: {"dir": 0, "adds": 0, "stop_px": None, "block_dir": 0} for a in ASSETS}
    if r:
        try:
            saved = json.loads(r.get("trendlive:state") or "{}")
            for a in ASSETS:
                if a in saved:
                    state[a].update(saved[a])
        except Exception:
            pass

    day = time.strftime("%Y-%m-%d", time.gmtime()); day_start_eq = None; halted = False

    while True:
        try:
            balance = bf.usdt_balance() if os.getenv("BINANCE_API_KEY") else 0.0
            equity = _equity(balance, {})
            if day_start_eq is None:
                day_start_eq = equity or None
            # reset the daily anchor at UTC midnight
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if today != day:
                day = today; day_start_eq = equity; halted = False

            # ---- daily-loss kill switch ----
            if (day_start_eq and equity <= day_start_eq * (1 - DAILY_LOSS_STOP)) and not halted:
                halted = True
                _log(f"DAILY-LOSS STOP hit: equity ${equity:.2f} <= {(1-DAILY_LOSS_STOP):.0%} of day start ${day_start_eq:.2f} — FLATTEN + HALT")
                _tg(f"🛑 TrendLive DAILY-LOSS STOP — flattening. equity ${equity:.2f}")
                for a in ASSETS:
                    _flatten(a, ws, balance, equity, "daily_loss")
                    state[a] = {"dir": 0, "adds": 0, "stop_px": None, "block_dir": 0}

            open_syms = 0
            for a in ASSETS:
                sym = SYMBOL[a]
                try:
                    closes, _ = tp.daily_closes(a)
                    sl = tp.signal_and_lev(closes)
                    mark = bf.mark_price(sym)
                except Exception as e:
                    _log(f"{a} data error: {e}"); continue
                if sl is None or not mark:
                    continue
                sig = sl["dir"]; lev = _clamp_lev(sl["lev"]); why = tp.reason_str(sl, mark)
                pos = bf.position(sym)
                amt = pos["amt"]; live_dir = (1 if amt > POS_EPS else (-1 if amt < -POS_EPS else 0))
                st = state[a]

                # ---------- reconcile: exchange is truth ----------
                if live_dir == 0 and st["dir"] != 0:            # stop/flip already closed it on the exchange
                    _log(f"{a} detected FLAT (stop/exit filled) — reconciling")
                    _row(ws, "CLOSE", a, "LONG" if st["dir"] > 0 else "SHORT", "exchange_exit", mark,
                         0, 0, lev, pos["entry"], st["stop_px"], st["adds"], 0, 0, balance, equity, "reconciled flat")
                    bf.cancel_all(sym)
                    st.update({"dir": 0, "adds": 0, "stop_px": None, "block_dir": st["dir"]})
                if live_dir != 0:
                    open_syms += 1
                    if st["dir"] == 0:                          # adopt a position we didn't record (restart)
                        st["dir"] = live_dir; st["adds"] = 0
                        if st["stop_px"] is None:
                            sd = _stop_dist(lev)
                            st["stop_px"] = pos["entry"] * (1 - sd) if live_dir > 0 else pos["entry"] * (1 + sd)
                            _ensure_stop(sym, live_dir, st["stop_px"], ws, a, mark)

                if not LIVE_ENABLED:
                    _row(ws, "OBSERVE", a, "LONG" if sig > 0 else ("SHORT" if sig < 0 else "FLAT"),
                         why, mark, abs(amt), abs(amt) * mark, lev, pos["entry"], st["stop_px"],
                         st["adds"], pos["unreal"], 0, balance, equity, "LIVE_ENABLED=false")
                    continue
                if halted:
                    continue

                # ---------- manage an open position ----------
                if live_dir != 0:
                    d = live_dir; entry = pos["entry"] or mark
                    fav = d * (mark / entry - 1.0)
                    # ratchet trailing stop
                    if fav >= TRAIL_BE:
                        be = entry
                        trail = mark * (1 - TRAIL_PCT) if d > 0 else mark * (1 + TRAIL_PCT)
                        new_stop = max(st["stop_px"], be, trail) if d > 0 else min(st["stop_px"], be, trail)
                        if new_stop != st["stop_px"]:
                            st["stop_px"] = new_stop
                            _ensure_stop(sym, d, new_stop, ws, a, mark, note="ratchet")
                    # pyramid UP (with the trend), respecting caps
                    cur_notl = abs(amt) * mark
                    add_notl = BET_USD * TRAIL_ADDF * lev
                    if (st["adds"] < TRAIL_ADDS and fav >= TRAIL_PYR * (st["adds"] + 1)
                            and (sig == 0 or sig == d)
                            and cur_notl + add_notl <= MAX_NOTL_POS
                            and _total_notional() + add_notl <= MAX_NOTL_TOTAL
                            and balance >= BET_USD * TRAIL_ADDF):
                        qty = bf.round_qty(sym, add_notl / mark)
                        if qty * mark >= bf.min_notional(sym):
                            bf.market_order(sym, "BUY" if d > 0 else "SELL", qty)
                            st["adds"] += 1
                            trail = mark * (1 - TRAIL_PCT) if d > 0 else mark * (1 + TRAIL_PCT)
                            st["stop_px"] = max(st["stop_px"], trail) if d > 0 else min(st["stop_px"], trail)
                            _ensure_stop(sym, d, st["stop_px"], ws, a, mark, note=f"pyramid add#{st['adds']}")
                            _tg(f"➕ {a} pyramid add#{st['adds']} @ {mark} stop {st['stop_px']:.6f}")
                    # trend flip -> close now (stop is the normal exit; this is a backstop)
                    if sig != 0 and sig != d:
                        _flatten(a, ws, balance, equity, "trend_flip")
                        st.update({"dir": 0, "adds": 0, "stop_px": None, "block_dir": d})

                # ---------- open a new position when flat ----------
                elif sig != 0 and sig != st["block_dir"]:
                    if open_syms >= MAX_CONCURRENT:
                        continue
                    lev_c = lev
                    target = max(BET_USD * lev_c, bf.min_notional(sym))   # stake*lev, floored to exchange min
                    qty = bf.qty_for_notional(sym, target, mark)          # ceil-to-step, guaranteed >= min
                    notl = qty * mark
                    if notl <= 0 or notl < bf.min_notional(sym):
                        _log(f"{a} skip: cannot build a valid min-notional order (notl ${notl:.2f})"); continue
                    if notl > MAX_NOTL_POS:                               # never blow the per-pos cap
                        _log(f"{a} skip: smallest legal order ${notl:.0f} > per-pos cap ${MAX_NOTL_POS:.0f} "
                             f"(coin's min-notional too high for a ${BET_USD:.0f} stake — use a lower-min coin)")
                        continue
                    if _total_notional() + notl > MAX_NOTL_TOTAL:
                        continue
                    margin_used = notl / lev_c
                    if balance < BET_USD:
                        continue
                    bf.set_leverage(sym, lev_c)
                    bf.market_order(sym, "BUY" if sig > 0 else "SELL", qty)
                    sd = _stop_dist(lev_c)
                    stop_px = mark * (1 - sd) if sig > 0 else mark * (1 + sd)
                    _ensure_stop(sym, sig, stop_px, ws, a, mark, note="entry")
                    st.update({"dir": sig, "adds": 0, "stop_px": stop_px})
                    open_syms += 1
                    _row(ws, "OPEN", a, "LONG" if sig > 0 else "SHORT", why, mark, qty, qty * mark,
                         lev_c, mark, stop_px, 0, 0, 0, balance, equity)
                    _log(f"{a} OPEN {'LONG' if sig>0 else 'SHORT'} qty={qty} notl=${qty*mark:.2f} "
                         f"margin(stake)=${margin_used:.2f} lev={lev_c:.1f}x stop={stop_px:.6f} | {why}")
                    _tg(f"🟩 {a} OPEN {'LONG' if sig>0 else 'SHORT'} ${qty*mark:.0f} notl "
                        f"(stake ${margin_used:.2f}) @ {mark} stop {stop_px:.6f}")

                elif not halted:
                    _row(ws, "FLAT", a, "FLAT", why, mark, 0, 0, lev, 0, "", 0, 0, 0, balance, equity)

            if r:
                try: r.set("trendlive:state", json.dumps(state))
                except Exception: pass
            _log(f"heartbeat — open {open_syms}/{len(ASSETS)} free=${balance:.2f} equity=${equity:.2f} "
                 f"halted={halted}")
        except Exception as e:
            _log(f"loop error: {e}")
        time.sleep(max(60, POLL))


def _total_notional():
    tot = 0.0
    for a in ASSETS:
        try:
            p = bf.position(SYMBOL[a]); m = bf.mark_price(SYMBOL[a])
            tot += abs(p["amt"]) * m
        except Exception:
            pass
    return tot


def _ensure_stop(sym, d, stop_px, ws, asset, mark, note=""):
    """Replace the exchange-resident stop: cancel existing, place a fresh STOP_MARKET closePosition."""
    close_side = "SELL" if d > 0 else "BUY"
    try:
        bf.cancel_all(sym)
        bf.stop_market(sym, close_side, stop_px, close_position=True)
        _log(f"{asset} stop -> {bf.round_price(sym, stop_px)} ({note})")
    except Exception as e:
        _log(f"{asset} stop set failed: {e}")


def _flatten(asset, ws, balance, equity, reason):
    sym = SYMBOL[asset]
    try:
        pos = bf.position(sym); amt = pos["amt"]
        if abs(amt) > POS_EPS:
            bf.market_order(sym, "SELL" if amt > 0 else "BUY", abs(amt), reduce_only=True)
        bf.cancel_all(sym)
        mark = bf.mark_price(sym)
        _row(ws, "CLOSE", asset, "LONG" if amt > 0 else "SHORT", reason, mark, abs(amt),
             abs(amt) * mark, pos.get("leverage", 1), pos["entry"], "", 0, pos["unreal"], pos["unreal"],
             balance, equity)
        _log(f"{asset} FLATTEN ({reason}) amt={amt} unreal=${pos['unreal']:+.2f}")
        _tg(f"🟥 {asset} CLOSE ({reason}) unreal=${pos['unreal']:+.2f}")
    except Exception as e:
        _log(f"{asset} flatten failed: {e}")


if __name__ == "__main__":
    run()
