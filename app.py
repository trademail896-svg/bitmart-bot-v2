from flask import Flask, request, jsonify
import os, time, json, hmac, hashlib, math, requests, threading
from typing import Optional, Dict, Any, Tuple, List

app = Flask(__name__)

# ================= STRATEGIE =================
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Couleurs (ENGLISH ONLY)
LONG_COLORS = {"green", "blue"}
SHORT_COLORS = {"red", "purple"}

SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# 1 position par symbole (BTC/ETH/SOL simultané OK)
STATE: Dict[str, Dict[str, Any]] = {
    s: {"in_position": False, "side": None, "last_entry_bar_id": None}
    for s in ALLOWED_SYMBOLS
}

# SQUEEZE semantics:
# on=True  => NO TRADE
# on=False => TRADE
SQUEEZE_ON = False

# ================= STOP LOSS POLICY (OPTION 1A) =================
# Fallback buffer: 1% (validated by user)
SL_FALLBACK_PCT = float((os.environ.get("SL_FALLBACK_PCT") or "0.01").strip())
# Guarantee window: 60 seconds (validated by user)
SL_GUARANTEE_WINDOW_SEC = int(float((os.environ.get("SL_GUARANTEE_WINDOW_SEC") or "60").strip()))
# Retry cadence
SL_RETRY_EVERY_SEC = float((os.environ.get("SL_RETRY_EVERY_SEC") or "2").strip())

# ================= BITMART CONFIG =================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

BASE_URL = (os.environ.get("BITMART_BASE_URL") or "https://demo-api-cloud-v2.bitmart.com").strip()
OPEN_TYPE = (os.environ.get("OPEN_TYPE") or "isolated").strip().lower()
LEVERAGE = int(float((os.environ.get("LEVERAGE") or "25").strip()))

# 100$ marge @25x => notional 2500$
NOTIONAL_USD_PER_TRADE = float((os.environ.get("NOTIONAL_USD_PER_TRADE") or "2500").strip())

BOT_VERSION = (os.environ.get("BOT_VERSION") or "v3.2-sl-fallback-1pct-60s").strip()

LEVERAGE_CACHE: Dict[str, Dict[str, Any]] = {}
LEVERAGE_CACHE_TTL = 600

DETAILS_CACHE: Dict[str, Dict[str, Any]] = {}
DETAILS_CACHE_TTL = 600

LAST_ALERT: Dict[str, Any] = {}
LAST_ALERT_TS = 0

LAST_ORDER: Dict[str, Any] = {}
LAST_ORDER_TS = 0

# SL tasks (per symbol)
SL_TASKS: Dict[str, Dict[str, Any]] = {}  # {symbol: {"thread": Thread, "started": ts, "active": bool}}
SL_LOCKS: Dict[str, threading.Lock] = {s: threading.Lock() for s in ALLOWED_SYMBOLS}


# ================= UTILS =================
def normalize_symbol(s: str) -> str:
    sym = (s or "").upper().strip()
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym

def normalize_color(c: str) -> str:
    c = (c or "").strip().lower()
    mapping = {
        "lime": "green",
        "aqua": "blue",
        "cyan": "blue",
        "violet": "purple",
        "magenta": "purple",
        "fuchsia": "purple",
        "maroon": "red",
    }
    return mapping.get(c, c)

def safe_float(v) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except Exception:
        return 0.0

def safe_int(v) -> int:
    try:
        if v is None:
            return 0
        return int(float(v))
    except Exception:
        return 0

def extract_code(res: Dict[str, Any]):
    j = res.get("json") or {}
    return j.get("code")

def parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in {"true", "1", "yes", "y", "on"}

def bar_id(symbol: str, tf: str, t: str) -> str:
    return f"{symbol}|{tf or ''}|{t or ''}"

def record_last_order(action: str, symbol: str, side: str, payload: Dict[str, Any], response: Dict[str, Any], extra: Optional[Dict[str, Any]] = None):
    global LAST_ORDER, LAST_ORDER_TS
    LAST_ORDER = {
        "action": action, "symbol": symbol, "side": side,
        "payload": payload, "response": response,
        "extra": extra or {}
    }
    LAST_ORDER_TS = int(time.time())

def fmt_price(p: float) -> str:
    # Use high precision to reduce rounding-caused invalid SL.
    # BitMart typically accepts string decimals; 8 is safe for crypto.
    return f"{p:.8f}"

def now_ts() -> int:
    return int(time.time())

def should_be_long_price(sl: float, entry: float) -> bool:
    # LONG stop loss must be strictly < entry
    return sl > 0 and entry > 0 and sl < entry

def should_be_short_price(sl: float, entry: float) -> bool:
    # SHORT stop loss must be strictly > entry
    return sl > 0 and entry > 0 and sl > entry


# ================= SIGN / HTTP =================
def sign_request(timestamp: int, body: Dict[str, Any]) -> str:
    body_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
    message = f"{timestamp}#{BITMART_MEMO}#{body_str}"
    return hmac.new(BITMART_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

def bm_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    ts = int(time.time() * 1000)
    signature = sign_request(ts, body)
    headers = {
        "Content-Type": "application/json",
        "X-BM-KEY": BITMART_KEY,
        "X-BM-TIMESTAMP": str(ts),
        "X-BM-SIGN": signature,
    }
    try:
        r = requests.post(BASE_URL + path, headers=headers,
                          data=json.dumps(body, separators=(",", ":"), sort_keys=True),
                          timeout=15)
        try:
            return {"http": r.status_code, "json": r.json()}
        except Exception:
            return {"http": r.status_code, "text": r.text}
    except Exception as e:
        return {"http": 0, "error": str(e)}

def bm_get_keyed(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    headers = {"X-BM-KEY": BITMART_KEY}
    try:
        r = requests.get(BASE_URL + path, headers=headers, params=params or {}, timeout=15)
        try:
            return {"http": r.status_code, "json": r.json()}
        except Exception:
            return {"http": r.status_code, "text": r.text}
    except Exception as e:
        return {"http": 0, "error": str(e)}

def bm_get_public(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        r = requests.get(BASE_URL + path, params=params or {}, timeout=15)
        try:
            return {"http": r.status_code, "json": r.json()}
        except Exception:
            return {"http": r.status_code, "text": r.text}
    except Exception as e:
        return {"http": 0, "error": str(e)}


# ================= DETAILS (contract_size + last_price) =================
def get_details(symbol: str) -> Tuple[bool, Dict[str, Any]]:
    now = int(time.time())
    c = DETAILS_CACHE.get(symbol)
    if c and (now - int(c.get("_ts", 0)) < DETAILS_CACHE_TTL):
        return True, c

    res = bm_get_public("/contract/public/details", params={"symbol": symbol})
    j = res.get("json") or {}
    if j.get("code") != 1000:
        return False, {"error": "details_failed", "raw": res}

    data = j.get("data") or {}
    payload = data if isinstance(data, dict) else {"symbols": data if isinstance(data, list) else [data]}
    if "symbols" not in payload:
        payload = {"symbols": [payload]}
    payload["_ts"] = now
    DETAILS_CACHE[symbol] = payload
    return True, payload

def get_contract_size_and_last_price(symbol: str) -> Tuple[bool, float, float, Dict[str, Any]]:
    ok, payload = get_details(symbol)
    if not ok:
        return False, 0.0, 0.0, payload

    row = None
    for it in payload.get("symbols") or []:
        if (it.get("symbol") or "").upper() == symbol:
            row = it
            break
    if row is None:
        syms = payload.get("symbols") or []
        row = syms[0] if syms else None

    if not row:
        return False, 0.0, 0.0, {"error": "details_no_row", "details": payload}

    cs = safe_float(row.get("contract_size"))
    px = safe_float(row.get("last_price"))
    if cs <= 0:
        return False, 0.0, 0.0, {"error": "contract_size_missing", "row": row}
    return True, cs, px, {"row": row}

def compute_size(symbol: str, price_hint: float) -> Tuple[bool, int, Dict[str, Any]]:
    ok, cs, px, dbg = get_contract_size_and_last_price(symbol)
    if not ok:
        return False, 0, dbg

    price = px if px > 0 else price_hint
    if price <= 0:
        return False, 0, {"error": "no_price", "symbol": symbol, "price_hint": price_hint, "last_price": px}

    contracts = NOTIONAL_USD_PER_TRADE / (price * cs)
    size_int = int(math.floor(contracts))
    if size_int < 1:
        size_int = 1

    return True, size_int, {
        "symbol": symbol,
        "notional_usd": NOTIONAL_USD_PER_TRADE,
        "leverage": float(LEVERAGE),
        "est_margin_usd": NOTIONAL_USD_PER_TRADE / float(LEVERAGE),
        "contract_size": cs,
        "price_used": price,
        "computed_contracts": contracts,
        "size_int": size_int
    }


# ================= POSITIONS =================
def fetch_positions(symbol: str) -> Tuple[bool, List[Dict[str, Any]], Dict[str, Any]]:
    res = bm_get_keyed("/contract/private/position", params={"symbol": symbol})
    j = res.get("json") or {}
    if j.get("code") != 1000:
        return False, [], res
    data = j.get("data") or []
    if not isinstance(data, list):
        data = []
    return True, data, res

def get_open_sides(symbol: str) -> Tuple[bool, Dict[str, int], Dict[str, Any]]:
    ok, rows, raw = fetch_positions(symbol)
    if not ok:
        return False, {"LONG": 0, "SHORT": 0}, raw

    sides = {"LONG": 0, "SHORT": 0}
    for r in rows:
        amt = safe_int(r.get("current_amount") or 0)
        if amt == 0:
            continue
        ptype = str(r.get("position_type") or "")
        if ptype == "1":
            sides["LONG"] += abs(amt)
        elif ptype == "2":
            sides["SHORT"] += abs(amt)

    return True, sides, {"rows": rows, "raw": raw}

def fetch_position_size(symbol: str, side: str) -> int:
    ok, rows, _raw = fetch_positions(symbol)
    if not ok:
        return 1
    want = "1" if side == "LONG" else "2"
    for r in rows:
        amt = safe_int(r.get("current_amount") or 0)
        if amt == 0:
            continue
        if str(r.get("position_type") or "") == want:
            return abs(amt)
    return 1

def fetch_position_entry_price(symbol: str, side: str) -> float:
    """
    Try to get the average entry price from BitMart position data.
    If missing, fallback to public last_price.
    """
    ok, rows, _raw = fetch_positions(symbol)
    want = "1" if side == "LONG" else "2"
    if ok:
        for r in rows:
            amt = safe_int(r.get("current_amount") or 0)
            if amt == 0:
                continue
            if str(r.get("position_type") or "") == want:
                # commonly 'open_avg_price' exists; if not, try alternative fields
                p = safe_float(r.get("open_avg_price")) or safe_float(r.get("avg_price")) or safe_float(r.get("entry_price"))
                if p > 0:
                    return p

    ok2, _cs, last_px, _dbg = get_contract_size_and_last_price(symbol)
    if ok2 and last_px > 0:
        return last_px
    return 0.0

def resync_symbol(symbol: str) -> None:
    ok, sides, _dbg = get_open_sides(symbol)
    if not ok:
        return
    if sides["LONG"] > 0 and sides["SHORT"] > 0:
        STATE[symbol].update({"in_position": True, "side": "HEDGE"})
    elif sides["LONG"] > 0:
        STATE[symbol].update({"in_position": True, "side": "LONG"})
    elif sides["SHORT"] > 0:
        STATE[symbol].update({"in_position": True, "side": "SHORT"})
    else:
        STATE[symbol].update({"in_position": False, "side": None})


# ================= BITMART ACTIONS =================
def submit_leverage(symbol: str) -> Dict[str, Any]:
    return bm_post("/contract/private/submit-leverage", {
        "symbol": symbol,
        "leverage": str(LEVERAGE),
        "open_type": OPEN_TYPE
    })

def ensure_leverage(symbol: str) -> None:
    now = int(time.time())
    c = LEVERAGE_CACHE.get(symbol)
    if c:
        fresh = (now - int(c.get("ts", 0)) < LEVERAGE_CACHE_TTL)
        same = (c.get("leverage") == LEVERAGE and c.get("open_type") == OPEN_TYPE)
        if fresh and same:
            return

    res = submit_leverage(symbol)
    print("BITMART SUBMIT LEVERAGE:", symbol, res, flush=True)
    if extract_code(res) == 1000:
        LEVERAGE_CACHE[symbol] = {"leverage": LEVERAGE, "open_type": OPEN_TYPE, "ts": now}

def open_market(symbol: str, side: str, price_hint: float, source: str) -> Dict[str, Any]:
    ensure_leverage(symbol)

    ok, size_int, sizing_dbg = compute_size(symbol, price_hint)
    if not ok:
        res = {"http": 0, "json": {"code": -1, "message": "sizing_failed", "data": sizing_dbg}}
        record_last_order("OPEN", symbol, side, {"error": "sizing_failed"}, res, {"source": source, "sizing": sizing_dbg})
        print("SIZING ERROR:", symbol, sizing_dbg, flush=True)
        return res

    payload = {
        "symbol": symbol,
        "type": "market",
        "side": 1 if side == "LONG" else 4,  # 1 buy open long, 4 sell open short
        "mode": 1,
        "size": size_int
    }
    res = bm_post("/contract/private/submit-order", payload)
    record_last_order("OPEN", symbol, side, payload, res, {"source": source, "sizing": sizing_dbg})
    print("BITMART OPEN:", symbol, side, res, flush=True)
    return res

def close_market(symbol: str, side: str, source: str) -> Dict[str, Any]:
    ensure_leverage(symbol)
    size_int = fetch_position_size(symbol, side)

    payload = {
        "symbol": symbol,
        "type": "market",
        "side": 3 if side == "LONG" else 2,  # 3 sell close long, 2 buy close short
        "mode": 1,
        "size": size_int
    }
    res = bm_post("/contract/private/submit-order", payload)
    record_last_order("CLOSE", symbol, side, payload, res, {"source": source, "close_size": size_int})
    print("BITMART CLOSE:", symbol, side, res, flush=True)
    return res

def set_stop_loss(symbol: str, position_side: str, trigger_price: float, source: str) -> Dict[str, Any]:
    ensure_leverage(symbol)
    size_int = fetch_position_size(symbol, position_side)

    payload = {
        "symbol": symbol,
        "type": "stop_loss",
        "side": 3 if position_side == "LONG" else 2,  # reduce side
        "trigger_price": fmt_price(trigger_price),
        "executive_price": fmt_price(trigger_price),
        "price_type": 1,
        "plan_category": 2,
        "category": "market",
        "size": size_int
    }
    res = bm_post("/contract/private/submit-tp-sl-order", payload)
    record_last_order("SET_SL", symbol, position_side, payload, res, {"source": source, "sl": trigger_price})
    print("BITMART SL:", symbol, position_side, res, flush=True)
    return res


# ================= SL TASK (OPTION 1A) =================
def compute_fallback_sl(side: str, entry_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    if side == "LONG":
        return entry_price * (1.0 - SL_FALLBACK_PCT)
    return entry_price * (1.0 + SL_FALLBACK_PCT)

def try_place_sl(symbol: str, side: str, sl_price: float, tag: str) -> Tuple[bool, Dict[str, Any]]:
    if sl_price <= 0:
        return False, {"error": "sl_price_invalid", "sl": sl_price}

    entry = fetch_position_entry_price(symbol, side)
    if entry > 0:
        if side == "LONG" and not should_be_long_price(sl_price, entry):
            return False, {"error": "sl_not_below_entry", "sl": sl_price, "entry": entry, "side": side}
        if side == "SHORT" and not should_be_short_price(sl_price, entry):
            return False, {"error": "sl_not_above_entry", "sl": sl_price, "entry": entry, "side": side}

    res = set_stop_loss(symbol, side, sl_price, source=tag)
    return (extract_code(res) == 1000), res

def sl_guard_task(symbol: str, side: str, sl_structural: float, task_label: str):
    """
    Guarantee: within SL_GUARANTEE_WINDOW_SEC, the position must have a valid SL.
    If not possible, CLOSE market (no stop = no trade).
    """
    start = time.time()
    deadline = start + float(SL_GUARANTEE_WINDOW_SEC)

    print(f"SL_TASK START {symbol} {side} label={task_label} window={SL_GUARANTEE_WINDOW_SEC}s fallback_pct={SL_FALLBACK_PCT}", flush=True)

    while time.time() < deadline:
        # Confirm position still exists (avoid useless actions)
        ok, sides, _dbg = get_open_sides(symbol)
        if not ok:
            time.sleep(SL_RETRY_EVERY_SEC)
            continue

        if side == "LONG" and sides["LONG"] <= 0:
            print(f"SL_TASK END {symbol} {side}: no LONG position anymore", flush=True)
            return
        if side == "SHORT" and sides["SHORT"] <= 0:
            print(f"SL_TASK END {symbol} {side}: no SHORT position anymore", flush=True)
            return

        # Get best entry price we can
        entry = fetch_position_entry_price(symbol, side)

        # 1) Try structural SL first (only if it is on the correct side of entry)
        if sl_structural > 0 and entry > 0:
            if (side == "LONG" and should_be_long_price(sl_structural, entry)) or (side == "SHORT" and should_be_short_price(sl_structural, entry)):
                ok_sl, res = try_place_sl(symbol, side, sl_structural, tag=f"{task_label}_STRUCT")
                if ok_sl:
                    print(f"SL_TASK OK {symbol} {side}: STRUCT placed sl={sl_structural} entry={entry}", flush=True)
                    return
                else:
                    # If BitMart rejects, we move to fallback (do not stop trying)
                    print(f"SL_TASK WARN {symbol} {side}: STRUCT rejected sl={sl_structural} entry={entry} res={res}", flush=True)

        # 2) Fallback SL at 1% from entry
        if entry > 0:
            sl_fb = compute_fallback_sl(side, entry)

            # Ensure strict inequality if rounding causes equality
            # Add a tiny epsilon away from entry
            eps = max(entry * 0.000001, 0.00000001)  # ~0.0001% or tiny
            if side == "LONG" and sl_fb >= entry:
                sl_fb = entry - eps
            if side == "SHORT" and sl_fb <= entry:
                sl_fb = entry + eps

            ok_fb, res2 = try_place_sl(symbol, side, sl_fb, tag=f"{task_label}_FALLBACK")
            if ok_fb:
                print(f"SL_TASK OK {symbol} {side}: FALLBACK placed sl={sl_fb} entry={entry}", flush=True)
                return
            else:
                print(f"SL_TASK WARN {symbol} {side}: FALLBACK rejected sl={sl_fb} entry={entry} res={res2}", flush=True)

        time.sleep(SL_RETRY_EVERY_SEC)

    # Deadline reached: CLOSE (no stop = no trade)
    print(f"SL_TASK TIMEOUT {symbol} {side}: closing position (no SL within {SL_GUARANTEE_WINDOW_SEC}s)", flush=True)
    close_market(symbol, side, source=f"{task_label}_NO_SL_TIMEOUT")

def start_sl_task(symbol: str, side: str, sl_structural: float, label: str):
    """
    Start (or replace) SL task for a symbol. Non-blocking for webhook.
    """
    if symbol not in ALLOWED_SYMBOLS:
        return

    with SL_LOCKS[symbol]:
        # Mark existing task inactive (best-effort)
        existing = SL_TASKS.get(symbol)
        if existing and existing.get("active"):
            existing["active"] = False

        t = threading.Thread(target=sl_guard_task, args=(symbol, side, sl_structural, label), daemon=True)
        SL_TASKS[symbol] = {"thread": t, "started": now_ts(), "active": True, "side": side, "label": label, "sl_structural": sl_structural}
        t.start()


# ================= ROUTES =================
@app.get("/")
def home():
    return "Bot TradingView DEMO V3.2 actif"

@app.get("/version")
def version():
    return jsonify({
        "bot_version": BOT_VERSION,
        "base_url": BASE_URL,
        "allowed_symbols": sorted(list(ALLOWED_SYMBOLS)),
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "notional_usd_per_trade": NOTIONAL_USD_PER_TRADE,
        "est_margin_usd_per_trade": NOTIONAL_USD_PER_TRADE / float(LEVERAGE),
        "secret_len": len(SECRET),
        "squeeze_on": SQUEEZE_ON,
        "sl_fallback_pct": SL_FALLBACK_PCT,
        "sl_window_sec": SL_GUARANTEE_WINDOW_SEC,
        "sl_retry_sec": SL_RETRY_EVERY_SEC
    }), 200

@app.get("/debug/bitmart")
def debug_bitmart():
    tests = {}
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        tests[sym] = bm_get_keyed("/contract/private/position", params={"symbol": sym})
    return jsonify({"position_tests": tests}), 200

@app.get("/debug/last_alert")
def debug_last_alert():
    return jsonify({"ts": LAST_ALERT_TS, "last_alert": LAST_ALERT}), 200

@app.get("/debug/last_order")
def debug_last_order():
    return jsonify({"ts": LAST_ORDER_TS, "last_order": LAST_ORDER}), 200

@app.get("/debug/state")
def debug_state():
    return jsonify({"squeeze_on": SQUEEZE_ON, "state": STATE, "sl_tasks": SL_TASKS}), 200


# ================= WEBHOOK =================
@app.post("/webhook")
def webhook():
    global SQUEEZE_ON, LAST_ALERT, LAST_ALERT_TS

    data = request.get_json(silent=True) or {}
    if data.get("secret") != SECRET:
        return jsonify({"status": "forbidden"}), 403

    LAST_ALERT = data
    LAST_ALERT_TS = int(time.time())
    print("ALERTE:", data, flush=True)

    event = (data.get("event") or "").upper().strip()
    action = (data.get("action") or "").upper().strip()
    reason = (data.get("reason") or "").upper().strip()

    symbol = normalize_symbol(data.get("ticker"))
    color = normalize_color(data.get("color") or "")

    tf = str(data.get("tf") or "")
    t = str(data.get("time") or data.get("time_ms") or "")

    # prix pour sizing
    price_hint = (
        safe_float(data.get("close")) or safe_float(data.get("open"))
        or safe_float(data.get("high")) or safe_float(data.get("low"))
    )

    # SQUEEZE: on=True => NO TRADE; on=False => TRADE
    if event == "SQUEEZE":
        SQUEEZE_ON = parse_bool(data.get("on"))
        return jsonify({"status": "squeeze_set", "on": SQUEEZE_ON}), 200

    if event == "RESET":
        if symbol in ALLOWED_SYMBOLS:
            resync_symbol(symbol)
            STATE[symbol]["last_entry_bar_id"] = None
            return jsonify({"status": "state_resynced", "symbol": symbol, "state": STATE[symbol]}), 200
        return jsonify({"status": "reset_ignored"}), 200

    if symbol not in ALLOWED_SYMBOLS:
        return jsonify({"status": "ignored_symbol", "symbol": symbol}), 200

    # sync BitMart
    resync_symbol(symbol)
    st = STATE[symbol]

    # ============= SORTIES =============
    # 1) EMA_EXIT ferme la position du côté envoyé
    if event == "EMA_EXIT":
        ema_side = (data.get("side") or "").upper().strip()
        if ema_side not in {"LONG", "SHORT"}:
            return jsonify({"status": "ema_exit_missing_side"}), 200

        ok, sides, _dbg = get_open_sides(symbol)
        if not ok:
            return jsonify({"status": "bitmart_position_fetch_failed"}), 200

        if ema_side == "LONG" and sides["LONG"] > 0:
            res = close_market(symbol, "LONG", source="EMA_EXIT")
            if extract_code(res) == 1000:
                resync_symbol(symbol)
                return jsonify({"status": "exit_ema_long"}), 200
            return jsonify({"status": "close_failed", "bitmart": res}), 200

        if ema_side == "SHORT" and sides["SHORT"] > 0:
            res = close_market(symbol, "SHORT", source="EMA_EXIT")
            if extract_code(res) == 1000:
                resync_symbol(symbol)
                return jsonify({"status": "exit_ema_short"}), 200
            return jsonify({"status": "close_failed", "bitmart": res}), 200

        return jsonify({"status": "ema_exit_no_position", "state": st}), 200

    # 2) STOCH_EXIT ou action EXIT_LONG/EXIT_SHORT
    if event == "STOCH_EXIT" or action in {"EXIT_LONG", "EXIT_SHORT"}:
        # convention: si tu es LONG, tu sors sur HD; si tu es SHORT, tu sors sur LD
        ok, sides, _dbg = get_open_sides(symbol)
        if not ok:
            return jsonify({"status": "bitmart_position_fetch_failed"}), 200

        if sides["LONG"] > 0 and (reason.startswith("HD")):
            res = close_market(symbol, "LONG", source="STOCH_EXIT_HD")
            if extract_code(res) == 1000:
                resync_symbol(symbol)
                return jsonify({"status": "exit_stoch_long"}), 200
            return jsonify({"status": "close_failed", "bitmart": res}), 200

        if sides["SHORT"] > 0 and (reason.startswith("LD")):
            res = close_market(symbol, "SHORT", source="STOCH_EXIT_LD")
            if extract_code(res) == 1000:
                resync_symbol(symbol)
                return jsonify({"status": "exit_stoch_short"}), 200
            return jsonify({"status": "close_failed", "bitmart": res}), 200

        return jsonify({"status": "stoch_exit_no_match", "reason": reason, "open": sides}), 200

    # 3) VECTOR opposé ferme (EXIT RULE)
    if event == "VECTOR":
        ok, sides, _dbg = get_open_sides(symbol)
        if not ok:
            return jsonify({"status": "bitmart_position_fetch_failed"}), 200

        # If SHORT open and a LONG vector appears => exit SHORT
        if sides["SHORT"] > 0 and color in LONG_COLORS:
            res = close_market(symbol, "SHORT", source=f"VECTOR_EXIT_{color}")
            if extract_code(res) == 1000:
                resync_symbol(symbol)
                return jsonify({"status": "exit_short_on_long_vector", "color": color}), 200
            return jsonify({"status": "close_failed", "bitmart": res}), 200

        # If LONG open and a SHORT vector appears => exit LONG
        if sides["LONG"] > 0 and color in SHORT_COLORS:
            res = close_market(symbol, "LONG", source=f"VECTOR_EXIT_{color}")
            if extract_code(res) == 1000:
                resync_symbol(symbol)
                return jsonify({"status": "exit_long_on_short_vector", "color": color}), 200
            return jsonify({"status": "close_failed", "bitmart": res}), 200

        # Otherwise, we might still use VECTOR as entry later
        # (entry logic below)
        # do not return here

    # ============= ENTREES =============
    bid = bar_id(symbol, tf, t)
    if st["last_entry_bar_id"] == bid:
        return jsonify({"status": "ignored_same_bar"}), 200

    # bloque entrées si squeeze ON (on=True => no trade)
    if SQUEEZE_ON:
        return jsonify({"status": "blocked_squeeze"}), 200

    # IMPORTANT: empêcher hedge involontaire (pas de LONG+SHORT sur même symbole)
    ok, sides, _dbg = get_open_sides(symbol)
    if ok and (sides["LONG"] > 0 or sides["SHORT"] > 0):
        st.update({"in_position": True, "side": "LONG" if sides["LONG"] > 0 else "SHORT"})
        return jsonify({"status": "ignored_already_in_position", "open": sides}), 200

    # STOCH_ENTRY : LD => LONG, HD => SHORT
    if event == "STOCH_ENTRY":
        if reason == "LD":
            res = open_market(symbol, "LONG", price_hint, source="STOCH_LD")
            if extract_code(res) != 1000:
                return jsonify({"status": "entry_failed", "bitmart": res}), 200

            st.update({"in_position": True, "side": "LONG", "last_entry_bar_id": bid})

            # SL structural from signal (low preferred; fallback task will handle invalidity)
            sl_struct = safe_float(data.get("sl_price")) or safe_float(data.get("low"))
            start_sl_task(symbol, "LONG", sl_structural=sl_struct, label="SL_STOCH_LD")

            return jsonify({"status": "enter_long_stoch", "sl_task": "started"}), 200

        if reason == "HD":
            res = open_market(symbol, "SHORT", price_hint, source="STOCH_HD")
            if extract_code(res) != 1000:
                return jsonify({"status": "entry_failed", "bitmart": res}), 200

            st.update({"in_position": True, "side": "SHORT", "last_entry_bar_id": bid})

            # SL structural from signal (high preferred; fallback task will handle invalidity)
            sl_struct = safe_float(data.get("sl_price")) or safe_float(data.get("high"))
            start_sl_task(symbol, "SHORT", sl_structural=sl_struct, label="SL_STOCH_HD")

            return jsonify({"status": "enter_short_stoch", "sl_task": "started"}), 200

        return jsonify({"status": "ignored_stoch_reason", "reason": reason}), 200

    # VECTOR ENTRY (optionnel) - colors only (green/blue long, red/purple short)
    # Note: If you later want "only if LD/HD active", we must add a TradingView state signal.
    if event == "VECTOR":
        inferred = None
        if color in LONG_COLORS:
            inferred = "LONG"
        elif color in SHORT_COLORS:
            inferred = "SHORT"
        else:
            return jsonify({"status": "ignored_vector_unknown_color", "color": color}), 200

        res = open_market(symbol, inferred, price_hint, source="VECTOR_ENTRY")
        if extract_code(res) != 1000:
            return jsonify({"status": "entry_failed", "bitmart": res}), 200

        st.update({"in_position": True, "side": inferred, "last_entry_bar_id": bid})

        # SL structural from vector candle
        if inferred == "LONG":
            sl_struct = safe_float(data.get("low"))
            start_sl_task(symbol, "LONG", sl_structural=sl_struct, label="SL_VECTOR_LONG")
        else:
            sl_struct = safe_float(data.get("high"))
            start_sl_task(symbol, "SHORT", sl_structural=sl_struct, label="SL_VECTOR_SHORT")

        return jsonify({"status": "enter_vector", "side": inferred, "sl_task": "started"}), 200

    return jsonify({"status": "ignored"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
