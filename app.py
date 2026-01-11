from flask import Flask, request, jsonify
import os
import time
import json
import hmac
import hashlib
import requests
from typing import Optional, Tuple, Dict, Any, List

app = Flask(__name__)

# ================= STRATEGIE =================
LONG_COLORS = {"green", "blue"}
SHORT_COLORS = {"red", "purple"}  # si tu veux inclure pink: {"red","pink","purple"}
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# 1 position par symbole
STATE: Dict[str, Dict[str, Any]] = {
    sym: {
        "in_position": False,
        "side": None,                 # "LONG" / "SHORT"
        "last_entry_bar_key": None,   # lock anti double entrée même bougie
    }
    for sym in ALLOWED_SYMBOLS
}

# Blocage entrées global (tous symboles)
SQUEEZE_ON = False

# IMPORTANT: doit matcher TradingView
SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# ================= BITMART CONFIG =================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

# DEMO
BASE_URL = (os.environ.get("BITMART_BASE_URL") or "https://demo-api-cloud-v2.bitmart.com").strip()

LEVERAGE = (os.environ.get("LEVERAGE") or "25").strip()
OPEN_TYPE = (os.environ.get("OPEN_TYPE") or "isolated").strip().lower()  # "isolated" ou "cross"

# Cache leverage
LEVERAGE_CACHE: Dict[str, Dict[str, Any]] = {}
LEVERAGE_CACHE_TTL_SEC = 600

BOT_VERSION = (os.environ.get("BOT_VERSION") or "v2-per-symbol-positions").strip()

# ================= DEBUG: derniers webhooks =================
LAST_ALERTS: List[Dict[str, Any]] = []
LAST_ALERTS_MAX = 30

# ================= UTILS =================
def normalize_symbol(s: str) -> str:
    sym = (s or "").upper().strip()
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym

def get_size(symbol: str) -> int:
    try:
        n = int(os.environ.get(f"SIZE_{symbol}", "1"))
        return max(1, n)
    except Exception:
        return 1

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

def make_bar_key(symbol: str, tf: Optional[str], t: Optional[str], side: Optional[str], source: Optional[str]) -> str:
    return f"{symbol}|{tf or ''}|{t or ''}|{side or ''}|{source or ''}"

def safe_float(v) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except Exception:
        return 0.0

def pick_last_low(data: Dict[str, Any]) -> float:
    # Priorité: last_low -> swing_low -> sl -> stop -> low
    for k in ["last_low", "swing_low", "sl", "stop", "low"]:
        val = safe_float(data.get(k))
        if val > 0:
            return val
    return 0.0

def pick_last_high(data: Dict[str, Any]) -> float:
    # Priorité: last_high -> swing_high -> sl -> stop -> high
    for k in ["last_high", "swing_high", "sl", "stop", "high"]:
        val = safe_float(data.get(k))
        if val > 0:
            return val
    return 0.0

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

# ================= BITMART ACTIONS =================
def submit_leverage(symbol: str) -> Dict[str, Any]:
    return bm_post("/contract/private/submit-leverage", {
        "symbol": symbol,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE
    })

def ensure_leverage_synced(symbol: str) -> bool:
    now = int(time.time())
    cached = LEVERAGE_CACHE.get(symbol)
    if cached:
        ok_same = (cached.get("leverage") == LEVERAGE and cached.get("open_type") == OPEN_TYPE)
        fresh = (now - int(cached.get("ts", 0)) < LEVERAGE_CACHE_TTL_SEC)
        if ok_same and fresh:
            return True

    res = submit_leverage(symbol)
    print("BITMART SUBMIT LEVERAGE:", symbol, res, flush=True)
    if extract_code(res) == 1000:
        LEVERAGE_CACHE[symbol] = {"leverage": LEVERAGE, "open_type": OPEN_TYPE, "ts": now}
        return True
    return False

def open_market(symbol: str, side: str) -> Dict[str, Any]:
    ensure_leverage_synced(symbol)
    return bm_post("/contract/private/submit-order", {
        "symbol": symbol,
        "type": "market",
        "side": 1 if side == "LONG" else 4,   # 1=buy open long, 4=sell open short
        "mode": 1,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "size": get_size(symbol)
    })

def close_market(symbol: str, side: str) -> Dict[str, Any]:
    return bm_post("/contract/private/submit-order", {
        "symbol": symbol,
        "type": "market",
        "side": 3 if side == "LONG" else 2,   # 3=sell close long, 2=buy close short
        "mode": 1,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "size": get_size(symbol)
    })

def set_stop_loss(symbol: str, side: str, price: float) -> Dict[str, Any]:
    return bm_post("/contract/private/submit-tp-sl-order", {
        "symbol": symbol,
        "type": "stop_loss",
        "side": 3 if side == "LONG" else 2,   # reduce side
        "trigger_price": f"{price:.2f}",
        "executive_price": f"{price:.2f}",
        "price_type": 1,
        "plan_category": 2,
        "category": "market",
        "size": get_size(symbol)
    })

# ================= POSITION RESYNC (par symbole) =================
def fetch_position(symbol: str) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    res = bm_get_keyed("/contract/private/position", params={"symbol": symbol})
    j = res.get("json") or {}
    if j.get("code") != 1000:
        return (False, None, res)

    data = j.get("data") or []
    if not isinstance(data, list) or len(data) == 0:
        return (False, None, res)

    # Cherche la ligne correspondante
    row = None
    for it in data:
        if (it.get("symbol") or "").upper() == symbol:
            row = it
            break
    if row is None:
        row = data[0]

    amt = safe_float(row.get("current_amount") or 0)
    if amt == 0:
        return (False, None, res)

    ptype = str(row.get("position_type") or "")
    if ptype == "1":
        return (True, "LONG", res)
    if ptype == "2":
        return (True, "SHORT", res)

    if amt > 0:
        return (True, "LONG", res)
    if amt < 0:
        return (True, "SHORT", res)

    return (True, None, res)

def resync_symbol(symbol: str) -> None:
    has_pos, side, raw = fetch_position(symbol)
    if has_pos:
        STATE[symbol].update({"in_position": True, "side": side})
        print("RESYNC:", symbol, "FOUND POSITION", {"side": side}, raw, flush=True)
    else:
        STATE[symbol].update({"in_position": False, "side": None})
        print("RESYNC:", symbol, "NO POSITION", raw, flush=True)

# ================= ROUTES =================
@app.get("/")
def home():
    return "Bot TradingView DEMO V2 actif"

@app.get("/version")
def version():
    return jsonify({
        "bot_version": BOT_VERSION,
        "base_url": BASE_URL,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "allowed_symbols": sorted(list(ALLOWED_SYMBOLS)),
        "long_colors": sorted(list(LONG_COLORS)),
        "short_colors": sorted(list(SHORT_COLORS)),
        "secret_len": len(SECRET),
        "squeeze_on": SQUEEZE_ON,
    }), 200

@app.get("/debug/state")
def debug_state():
    return jsonify({"squeeze_on": SQUEEZE_ON, "state": STATE}), 200

@app.get("/debug/alerts")
def debug_alerts():
    return jsonify({"count": len(LAST_ALERTS), "alerts": LAST_ALERTS}), 200

@app.get("/debug/bitmart")
def debug_bitmart():
    env_ok = {
        "has_key": bool(BITMART_KEY),
        "has_secret": bool(BITMART_SECRET),
        "has_memo": bool(BITMART_MEMO),
        "base_url": BASE_URL,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE
    }

    tests = {}
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        tests[sym] = bm_get_keyed("/contract/private/position", params={"symbol": sym})

    return jsonify({"env_ok": env_ok, "position_tests": tests}), 200

@app.post("/webhook")
def webhook():
    global SQUEEZE_ON

    data = request.get_json(silent=True) or {}

    if data.get("secret") != SECRET:
        return jsonify({"status": "forbidden"}), 403

    # debug store
    LAST_ALERTS.append(data)
    if len(LAST_ALERTS) > LAST_ALERTS_MAX:
        LAST_ALERTS.pop(0)

    print("ALERTE:", data, flush=True)

    event = (data.get("event") or "").upper().strip()
    action = (data.get("action") or "").upper().strip()
    reason = (data.get("reason") or "").upper().strip()
    color = (data.get("color") or "").lower().strip()

    symbol = normalize_symbol(data.get("ticker"))
    tf = str(data.get("tf") or "")
    t = str(data.get("time") or data.get("time_ms") or "")

    if symbol not in ALLOWED_SYMBOLS and event not in {"RESET", "SQUEEZE"}:
        print("IGNORED SYMBOL:", symbol, flush=True)
        return jsonify({"status": "ignored_symbol"}), 200

    # RESET = resync pour ce symbole
    if event == "RESET":
        if symbol in ALLOWED_SYMBOLS:
            resync_symbol(symbol)
            STATE[symbol]["last_entry_bar_key"] = None
            return jsonify({"status": "state_resynced", "symbol": symbol, "state": STATE[symbol]}), 200
        return jsonify({"status": "reset_ignored_no_symbol"}), 200

    # SQUEEZE global
    if event == "SQUEEZE":
        SQUEEZE_ON = parse_bool(data.get("on"))
        print("SQUEEZE UPDATE:", SQUEEZE_ON, flush=True)
        return jsonify({"status": "squeeze_set", "on": SQUEEZE_ON}), 200

    st = STATE[symbol]

    # ================= SORTIES =================
    is_any_exit = (event in {"EMA_EXIT", "STOCH_EXIT"} or action in {"EXIT_LONG", "EXIT_SHORT"})
    if is_any_exit and not st["in_position"]:
        print("EXIT RECEIVED BUT STATE FLAT -> RESYNC", symbol, flush=True)
        resync_symbol(symbol)
        st = STATE[symbol]

    if st["in_position"]:
        # EMA exit
        if event == "EMA_EXIT":
            ema_side = (data.get("side") or "").upper().strip()
            if ema_side == st["side"]:
                res = close_market(symbol, st["side"])
                print("BITMART CLOSE (EMA):", symbol, res, flush=True)
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_ema", "symbol": symbol}), 200
                return jsonify({"status": "close_failed", "symbol": symbol, "bitmart": res}), 200
            return jsonify({"status": "ignored_ema_side", "symbol": symbol}), 200

        # STOCH exit
        if event == "STOCH_EXIT" or action in {"EXIT_LONG", "EXIT_SHORT"}:
            if st["side"] == "LONG" and reason_is_hd(reason):
                res = close_market(symbol, "LONG")
                print("BITMART CLOSE (STOCH):", symbol, res, flush=True)
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_stoch", "symbol": symbol}), 200
                return jsonify({"status": "close_failed", "symbol": symbol, "bitmart": res}), 200

            if st["side"] == "SHORT" and reason_is_ld(reason):
                res = close_market(symbol, "SHORT")
                print("BITMART CLOSE (STOCH):", symbol, res, flush=True)
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_stoch", "symbol": symbol}), 200
                return jsonify({"status": "close_failed", "symbol": symbol, "bitmart": res}), 200

        # VECTOR opposé
        if event == "VECTOR":
            if st["side"] == "LONG" and color in SHORT_COLORS:
                res = close_market(symbol, "LONG")
                print("BITMART CLOSE (VECTOR OPP):", symbol, res, flush=True)
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_vector_opp", "symbol": symbol}), 200
                return jsonify({"status": "close_failed", "symbol": symbol, "bitmart": res}), 200

            if st["side"] == "SHORT" and color in LONG_COLORS:
                res = close_market(symbol, "SHORT")
                print("BITMART CLOSE (VECTOR OPP):", symbol, res, flush=True)
                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None})
                    return jsonify({"status": "exit_vector_opp", "symbol": symbol}), 200
                return jsonify({"status": "close_failed", "symbol": symbol, "bitmart": res}), 200

        return jsonify({"status": "holding", "symbol": symbol, "side": st["side"]}), 200

    # ================= ENTREES =================
    if SQUEEZE_ON:
        return jsonify({"status": "blocked_squeeze", "symbol": symbol}), 200

    # STOCH_ENTRY
    if event == "STOCH_ENTRY":
        if reason_is_ld(reason):
            bar_key = make_bar_key(symbol, tf, t, "LONG", "STOCH")
            if st["last_entry_bar_key"] == bar_key:
                return jsonify({"status": "ignored_same_bar", "symbol": symbol}), 200

            res_entry = open_market(symbol, "LONG")
            print("BITMART ENTRY (STOCH LONG):", symbol, res_entry, flush=True)
            if extract_code(res_entry) != 1000:
                return jsonify({"status": "entry_failed", "symbol": symbol, "bitmart": res_entry}), 200

            st.update({"in_position": True, "side": "LONG", "last_entry_bar_key": bar_key})

            # SL sous le dernier low (last_low prioritaire)
            sl = pick_last_low(data)
            if sl > 0:
                res_sl = set_stop_loss(symbol, "LONG", sl)
                print("BITMART SL (STOCH LONG):", symbol, sl, res_sl, flush=True)

            return jsonify({"status": "enter_long_stoch", "symbol": symbol, "sl": sl}), 200

        if reason_is_hd(reason):
            bar_key = make_bar_key(symbol, tf, t, "SHORT", "STOCH")
            if st["last_entry_bar_key"] == bar_key:
                return jsonify({"status": "ignored_same_bar", "symbol": symbol}), 200

            res_entry = open_market(symbol, "SHORT")
            print("BITMART ENTRY (STOCH SHORT):", symbol, res_entry, flush=True)
            if extract_code(res_entry) != 1000:
                return jsonify({"status": "entry_failed", "symbol": symbol, "bitmart": res_entry}), 200

            st.update({"in_position": True, "side": "SHORT", "last_entry_bar_key": bar_key})

            # SL au-dessus du dernier high (last_high prioritaire)
            sl = pick_last_high(data)
            if sl > 0:
                res_sl = set_stop_loss(symbol, "SHORT", sl)
                print("BITMART SL (STOCH SHORT):", symbol, sl, res_sl, flush=True)

            return jsonify({"status": "enter_short_stoch", "symbol": symbol, "sl": sl}), 200

        return jsonify({"status": "ignored_stoch_unknown_reason", "symbol": symbol, "reason": reason}), 200

    # VECTOR entry
    if event == "VECTOR":
        inferred_side = None
        if color in LONG_COLORS:
            inferred_side = "LONG"
        elif color in SHORT_COLORS:
            inferred_side = "SHORT"
        else:
            return jsonify({"status": "ignored_vector_unknown_color", "symbol": symbol, "color": color}), 200

        bar_key = make_bar_key(symbol, tf, t, inferred_side, "VECTOR")
        if st["last_entry_bar_key"] == bar_key:
            return jsonify({"status": "ignored_same_bar", "symbol": symbol}), 200

        res_entry = open_market(symbol, inferred_side)
        print("BITMART ENTRY (VECTOR):", symbol, inferred_side, res_entry, flush=True)
        if extract_code(res_entry) != 1000:
            return jsonify({"status": "entry_failed", "symbol": symbol, "bitmart": res_entry}), 200

        st.update({"in_position": True, "side": inferred_side, "last_entry_bar_key": bar_key})

        # SL structurel sur vector
        if inferred_side == "LONG":
            sl = safe_float(data.get("low", 0) or 0)
            if sl > 0:
                res_sl = set_stop_loss(symbol, "LONG", sl)
                print("BITMART SL (VECTOR LONG):", symbol, sl, res_sl, flush=True)
        else:
            sl = safe_float(data.get("high", 0) or 0)
            if sl > 0:
                res_sl = set_stop_loss(symbol, "SHORT", sl)
                print("BITMART SL (VECTOR SHORT):", symbol, sl, res_sl, flush=True)

        return jsonify({"status": "enter_vector", "symbol": symbol, "side": inferred_side}), 200

    return jsonify({"status": "ignored", "symbol": symbol}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
