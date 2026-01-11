from flask import Flask, request, jsonify
import os
import time
import json
import hmac
import hashlib
import math
import requests
from typing import Optional, Tuple, Dict, Any, List

app = Flask(__name__)

# ================= STRATEGIE =================
LONG_COLORS = {"green", "blue"}
SHORT_COLORS = {"red", "purple"}  # ajoute "pink" si nécessaire
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# 1 position par symbole (BTC/ETH/SOL peuvent coexister)
STATE: Dict[str, Dict[str, Any]] = {
    sym: {
        "in_position": False,
        "side": None,                 # "LONG" / "SHORT"
        "last_entry_bar_id": None,    # lock: une entrée max par bougie (peu importe LONG/SHORT)
        "last_seen_ts": 0
    }
    for sym in ALLOWED_SYMBOLS
}

SQUEEZE_ON = False
SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# ================= BITMART CONFIG =================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

BASE_URL = (os.environ.get("BITMART_BASE_URL") or "https://demo-api-cloud-v2.bitmart.com").strip()
LEVERAGE = (os.environ.get("LEVERAGE") or "25").strip()
OPEN_TYPE = (os.environ.get("OPEN_TYPE") or "isolated").strip().lower()

# 100$ à 25x => 2500$ notional
NOTIONAL_USD_PER_TRADE = float((os.environ.get("NOTIONAL_USD_PER_TRADE") or "2500").strip())

BOT_VERSION = (os.environ.get("BOT_VERSION") or "v2-anti-hedge-sl-retry").strip()

# caches
LEVERAGE_CACHE: Dict[str, Dict[str, Any]] = {}
LEVERAGE_CACHE_TTL_SEC = 600

DETAILS_CACHE: Dict[str, Dict[str, Any]] = {}
DETAILS_TTL_SEC = 600

# debug
LAST_ORDER: Dict[str, Any] = {}
LAST_ORDER_TS = 0

# ================= UTILS =================
def normalize_symbol(s: str) -> str:
    sym = (s or "").upper().strip()
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym

def extract_code(res: Dict[str, Any]):
    j = res.get("json") or {}
    return j.get("code")

def reason_is_ld(reason: str) -> bool:
    r = (reason or "").upper().strip()
    return r == "LD" or r.startswith("LD")

def reason_is_hd(reason: str) -> bool:
    r = (reason or "").upper().strip()
    return r == "HD" or r.startswith("HD")

def parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in {"true", "1", "yes", "y", "on"}:
        return True
    if s in {"false", "0", "no", "n", "off", ""}:
        return False
    return True

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

def pick_last_low(data: Dict[str, Any]) -> float:
    for k in ["last_low", "swing_low", "low"]:
        val = safe_float(data.get(k))
        if val > 0:
            return val
    return 0.0

def pick_last_high(data: Dict[str, Any]) -> float:
    for k in ["last_high", "swing_high", "high"]:
        val = safe_float(data.get(k))
        if val > 0:
            return val
    return 0.0

def bar_id(symbol: str, tf: str, t: str) -> str:
    # lock d’entrée par bougie, peu importe LONG/SHORT
    return f"{symbol}|{tf or ''}|{t or ''}"

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
        r = requests.post(
            BASE_URL + path,
            headers=headers,
            data=json.dumps(body, separators=(",", ":"), sort_keys=True),
            timeout=15
        )
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

# ================= DETAILS (contract_size + last_price) =================
def get_details(symbol: str) -> Tuple[bool, Dict[str, Any]]:
    now = int(time.time())
    cached = DETAILS_CACHE.get(symbol)
    if cached and (now - int(cached.get("_ts", 0)) < DETAILS_TTL_SEC):
        return True, cached

    res = bm_get_public("/contract/public/details", params={"symbol": symbol})
    j = res.get("json") or {}
    if j.get("code") != 1000:
        return False, {"error": "details_failed", "raw": res}

    data = j.get("data") or {}
    if isinstance(data, dict) and "symbols" in data:
        payload = data
    else:
        payload = {"symbols": data if isinstance(data, list) else [data]}
    payload["_ts"] = now
    DETAILS_CACHE[symbol] = payload
    return True, payload

def get_contract_size_and_price(symbol: str) -> Tuple[bool, float, float, Dict[str, Any]]:
    ok, payload = get_details(symbol)
    if not ok:
        return False, 0.0, 0.0, payload

    syms = payload.get("symbols") or []
    row = None
    for it in syms:
        if (it.get("symbol") or "").upper() == symbol:
            row = it
            break
    if row is None and syms:
        row = syms[0]
    if not row:
        return False, 0.0, 0.0, {"error": "details_no_row", "details": payload}

    cs = safe_float(row.get("contract_size"))
    px = safe_float(row.get("last_price"))

    if cs <= 0:
        return False, 0.0, 0.0, {"error": "contract_size_missing", "row": row}
    return True, cs, px, {"row": row}

def compute_size(symbol: str, price_hint: float) -> Tuple[bool, int, Dict[str, Any]]:
    ok, cs, px, dbg = get_contract_size_and_price(symbol)
    if not ok:
        return False, 0, dbg

    price = px if px > 0 else price_hint
    if price <= 0:
        return False, 0, {"error": "no_price", "symbol": symbol, "price_hint": price_hint, "last_price": px}

    est = NOTIONAL_USD_PER_TRADE / (price * cs)
    size = int(math.floor(est))
    if size < 1:
        size = 1

    lev = safe_float(LEVERAGE) if safe_float(LEVERAGE) > 0 else 1.0
    return True, size, {
        "symbol": symbol,
        "notional": NOTIONAL_USD_PER_TRADE,
        "leverage": lev,
        "est_margin": NOTIONAL_USD_PER_TRADE / lev,
        "contract_size": cs,
        "price_used": price,
        "computed_contracts": est,
        "size_int": size
    }

# ================= POSITIONS (hedge detection) =================
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
        return False, {}, raw
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
        else:
            # fallback selon signe
            if amt > 0:
                sides["LONG"] += abs(amt)
            elif amt < 0:
                sides["SHORT"] += abs(amt)
    return True, sides, {"rows": rows, "raw": raw}

def resync_symbol(symbol: str) -> None:
    ok, sides, _dbg = get_open_sides(symbol)
    if not ok:
        return
    if sides.get("LONG", 0) > 0 and sides.get("SHORT", 0) > 0:
        # hedge actif sur ce symbole
        STATE[symbol].update({"in_position": True, "side": "HEDGE"})
    elif sides.get("LONG", 0) > 0:
        STATE[symbol].update({"in_position": True, "side": "LONG"})
    elif sides.get("SHORT", 0) > 0:
        STATE[symbol].update({"in_position": True, "side": "SHORT"})
    else:
        STATE[symbol].update({"in_position": False, "side": None})

def fetch_position_size_for_close(symbol: str, side: str) -> int:
    ok, rows, _raw = fetch_positions(symbol)
    if not ok:
        return 1
    ptype_need = "1" if side == "LONG" else "2"
    for r in rows:
        amt = safe_int(r.get("current_amount") or 0)
        if amt == 0:
            continue
        if str(r.get("position_type") or "") == ptype_need:
            return abs(amt)
    return 1

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
        fresh = (now - int(c.get("ts", 0)) < LEVERAGE_CACHE_TTL_SEC)
        same = (c.get("leverage") == str(LEVERAGE) and c.get("open_type") == OPEN_TYPE)
        if fresh and same:
            return
    res = submit_leverage(symbol)
    if extract_code(res) == 1000:
        LEVERAGE_CACHE[symbol] = {"leverage": str(LEVERAGE), "open_type": OPEN_TYPE, "ts": now}

def open_market(symbol: str, side: str, price_hint: float, source: str) -> Dict[str, Any]:
    global LAST_ORDER, LAST_ORDER_TS
    ensure_leverage(symbol)

    ok, size, dbg = compute_size(symbol, price_hint)
    if not ok:
        LAST_ORDER = {"error": dbg, "symbol": symbol, "side": side, "source": source}
        LAST_ORDER_TS = int(time.time())
        return {"http": 0, "json": {"code": -1, "message": "sizing_failed", "data": dbg}}

    body = {
        "symbol": symbol,
        "type": "market",
        "side": 1 if side == "LONG" else 4,
        "mode": 1,
        "leverage": str(LEVERAGE),
        "open_type": OPEN_TYPE,
        "size": str(size)
    }

    LAST_ORDER = {"symbol": symbol, "side": side, "source": source, "sizing": dbg, "payload": body}
    LAST_ORDER_TS = int(time.time())

    res = bm_post("/contract/private/submit-order", body)
    LAST_ORDER["response"] = res
    return res

def close_market(symbol: str, side: str) -> Dict[str, Any]:
    ensure_leverage(symbol)
    size = fetch_position_size_for_close(symbol, side)
    body = {
        "symbol": symbol,
        "type": "market",
        "side": 3 if side == "LONG" else 2,
        "mode": 1,
        "leverage": str(LEVERAGE),
        "open_type": OPEN_TYPE,
        "size": str(size)
    }
    return bm_post("/contract/private/submit-order", body)

def set_stop_loss(symbol: str, side: str, price: float, size_hint: Optional[int] = None) -> Dict[str, Any]:
    ensure_leverage(symbol)
    size = size_hint if (isinstance(size_hint, int) and size_hint > 0) else fetch_position_size_for_close(symbol, side)
    return bm_post("/contract/private/submit-tp-sl-order", {
        "symbol": symbol,
        "type": "stop_loss",
        "side": 3 if side == "LONG" else 2,
        "trigger_price": f"{price:.2f}",
        "executive_price": f"{price:.2f}",
        "price_type": 1,
        "plan_category": 2,
        "category": "market",
        "size": str(size)
    })

def place_sl_retry(symbol: str, side: str, price: float) -> Dict[str, Any]:
    # attend que la position existe réellement (sinon SL non attaché)
    last = {"http": 0, "json": {"code": -1, "message": "sl_not_sent"}}
    for _ in range(6):
        size_now = fetch_position_size_for_close(symbol, side)
        if size_now > 0:
            last = set_stop_loss(symbol, side, price, size_hint=size_now)
            if extract_code(last) == 1000:
                return last
        time.sleep(0.7)
    return last

# ================= ROUTES =================
@app.get("/")
def home():
    return "Bot TradingView DEMO V2 actif"

@app.get("/version")
def version():
    lev = safe_float(LEVERAGE) if safe_float(LEVERAGE) > 0 else 1.0
    return jsonify({
        "bot_version": BOT_VERSION,
        "base_url": BASE_URL,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "allowed_symbols": sorted(list(ALLOWED_SYMBOLS)),
        "notional_usd_per_trade": NOTIONAL_USD_PER_TRADE,
        "est_margin_usd_per_trade": NOTIONAL_USD_PER_TRADE / lev,
        "secret_len": len(SECRET),
        "squeeze_on": SQUEEZE_ON
    }), 200

@app.get("/debug/bitmart")
def debug_bitmart():
    tests = {}
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        tests[sym] = bm_get_keyed("/contract/private/position", params={"symbol": sym})
    return jsonify({"position_tests": tests}), 200

@app.get("/debug/state")
def debug_state():
    return jsonify({"squeeze_on": SQUEEZE_ON, "state": STATE}), 200

@app.get("/debug/last_order")
def debug_last_order():
    return jsonify({"last_order": LAST_ORDER, "ts": LAST_ORDER_TS}), 200

@app.post("/webhook")
def webhook():
    global SQUEEZE_ON

    data = request.get_json(silent=True) or {}
    if data.get("secret") != SECRET:
        return jsonify({"status": "forbidden"}), 403

    event = (data.get("event") or "").upper().strip()
    action = (data.get("action") or "").upper().strip()
    reason = (data.get("reason") or "").upper().strip()
    color = (data.get("color") or "").lower().strip()

    symbol = normalize_symbol(data.get("ticker"))
    tf = str(data.get("tf") or "")
    t = str(data.get("time") or data.get("time_ms") or "")

    price_hint = safe_float(data.get("close")) or safe_float(data.get("open")) or safe_float(data.get("high")) or safe_float(data.get("low"))

    if symbol not in ALLOWED_SYMBOLS and event not in {"RESET", "SQUEEZE"}:
        return jsonify({"status": "ignored_symbol"}), 200

    if event == "SQUEEZE":
        SQUEEZE_ON = parse_bool(data.get("on"))
        return jsonify({"status": "squeeze_set", "on": SQUEEZE_ON}), 200

    if event == "RESET":
        if symbol in ALLOWED_SYMBOLS:
            resync_symbol(symbol)
            STATE[symbol]["last_entry_bar_id"] = None
            return jsonify({"status": "state_resynced", "symbol": symbol, "state": STATE[symbol]}), 200
        return jsonify({"status": "reset_ignored_no_symbol"}), 200

    # Ignore heartbeats or unknown events quickly
    if event in {"BAR_CLOSE"} or action == "NA":
        return jsonify({"status": "ok"}), 200

    # refresh local state from BitMart (important in hedge_mode)
    resync_symbol(symbol)
    st = STATE[symbol]

    # ================= SORTIES =================
    is_any_exit = (event in {"EMA_EXIT", "STOCH_EXIT"} or action in {"EXIT_LONG", "EXIT_SHORT"})
    if is_any_exit and not st["in_position"]:
        return jsonify({"status": "flat"}), 200

    if st["in_position"]:
        # si hedge détecté: on ne fait rien automatiquement ici (sécurité),
        # mais on empêche les nouvelles entrées.
        if st["side"] == "HEDGE":
            return jsonify({"status": "blocked_hedge_mode_open_both_sides"}), 200

        if event == "EMA_EXIT":
            ema_side = (data.get("side") or "").upper().strip()
            if ema_side == st["side"]:
                res = close_market(symbol, st["side"])
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_ema"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200
            return jsonify({"status": "ignored_ema_side"}), 200

        if event == "VECTOR":
            if st["side"] == "LONG" and color in SHORT_COLORS:
                res = close_market(symbol, "LONG")
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_vector_opp"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

            if st["side"] == "SHORT" and color in LONG_COLORS:
                res = close_market(symbol, "SHORT")
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_vector_opp"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

        if event == "STOCH_EXIT":
            # exit opposé
            if st["side"] == "LONG" and reason_is_hd(reason):
                res = close_market(symbol, "LONG")
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_stoch"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

            if st["side"] == "SHORT" and reason_is_ld(reason):
                res = close_market(symbol, "SHORT")
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_stoch"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

        return jsonify({"status": "holding"}), 200

    # ================= ENTREES =================
    if SQUEEZE_ON:
        return jsonify({"status": "blocked_squeeze"}), 200

    # LOCK: une seule entrée par bougie / symbole, même si alerts multiples
    bid = bar_id(symbol, tf, t)
    if st["last_entry_bar_id"] == bid:
        return jsonify({"status": "ignored_same_bar"}), 200

    # SAFETY: si BitMart a déjà une position (resync_symbol) on refuse toute nouvelle entrée
    # Cela empêche LONG + SHORT simultané même en hedge_mode.
    if st["in_position"]:
        return jsonify({"status": "ignored_already_in_position"}), 200

    if event == "STOCH_ENTRY":
        if reason_is_ld(reason):
            res_entry = open_market(symbol, "LONG", price_hint, "STOCH_LD")
            if extract_code(res_entry) != 1000:
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            st.update({"in_position": True, "side": "LONG", "last_entry_bar_id": bid})

            sl = pick_last_low(data)
            if sl > 0:
                res_sl = place_sl_retry(symbol, "LONG", sl)
                return jsonify({"status": "enter_long_stoch", "sl": sl, "sl_res": res_sl}), 200
            return jsonify({"status": "enter_long_stoch", "sl": None}), 200

        if reason_is_hd(reason):
            res_entry = open_market(symbol, "SHORT", price_hint, "STOCH_HD")
            if extract_code(res_entry) != 1000:
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            st.update({"in_position": True, "side": "SHORT", "last_entry_bar_id": bid})

            sl = pick_last_high(data)
            if sl > 0:
                res_sl = place_sl_retry(symbol, "SHORT", sl)
                return jsonify({"status": "enter_short_stoch", "sl": sl, "sl_res": res_sl}), 200
            return jsonify({"status": "enter_short_stoch", "sl": None}), 200

        return jsonify({"status": "ignored_stoch_unknown_reason", "reason": reason}), 200

    if event == "VECTOR":
        inferred_side = None
        if color in LONG_COLORS:
            inferred_side = "LONG"
        elif color in SHORT_COLORS:
            inferred_side = "SHORT"
        else:
            return jsonify({"status": "ignored_vector_unknown_color", "color": color}), 200

        res_entry = open_market(symbol, inferred_side, price_hint, "VECTOR")
        if extract_code(res_entry) != 1000:
            return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

        st.update({"in_position": True, "side": inferred_side, "last_entry_bar_id": bid})

        # SL structurel vector
        if inferred_side == "LONG":
            sl = safe_float(data.get("low", 0) or 0)
            if sl > 0:
                res_sl = place_sl_retry(symbol, "LONG", sl)
                return jsonify({"status": "enter_vector_long", "sl": sl, "sl_res": res_sl}), 200
            return jsonify({"status": "enter_vector_long", "sl": None}), 200
        else:
            sl = safe_float(data.get("high", 0) or 0)
            if sl > 0:
                res_sl = place_sl_retry(symbol, "SHORT", sl)
                return jsonify({"status": "enter_vector_short", "sl": sl, "sl_res": res_sl}), 200
            return jsonify({"status": "enter_vector_short", "sl": None}), 200

    return jsonify({"status": "ignored"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
