from flask import Flask, request, jsonify
import os
import time
import json
import hmac
import hashlib
import requests
from typing import Optional, Tuple, Dict, Any

app = Flask(__name__)

# ================= STRATEGIE =================
LONG_COLORS = {"green", "blue"}
SHORT_COLORS = {"red", "purple"}  # si tu veux inclure pink: {"red","pink","purple"}

ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Mode B : une seule position globale à la fois
STATE: Dict[str, Any] = {
    "in_position": False,
    "side": None,                 # "LONG" / "SHORT"
    "symbol": None,               # "BTCUSDT" / ...
    "last_entry_bar_key": None,   # lock anti-double entrée même bougie
    "squeeze_on": False
}

SECRET = "TV_BOT_DEMO_2026_V2"

# ================= BITMART CONFIG =================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

# DEMO
BASE_URL = "https://demo-api-cloud-v2.bitmart.com"

# Leverage (tu veux 25x partout)
LEVERAGE = (os.environ.get("LEVERAGE") or "25").strip()
OPEN_TYPE = (os.environ.get("OPEN_TYPE") or "isolated").strip().lower()  # "isolated" ou "cross"

# Cache leverage pour éviter de spam submit-leverage
LEVERAGE_CACHE: Dict[str, Dict[str, Any]] = {}
LEVERAGE_CACHE_TTL_SEC = 600  # 10 minutes

BOT_VERSION = (os.environ.get("BOT_VERSION") or "v2-clean-debug").strip()

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
    # gère True/False, 1/0, "true"/"false", "on"/"off"
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
    # fallback (non vide)
    return True

def make_bar_key(symbol: str, tf: Optional[str], t: Optional[str], side: Optional[str], source: Optional[str]) -> str:
    return f"{symbol}|{tf or ''}|{t or ''}|{side or ''}|{source or ''}"

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
    print("BITMART SUBMIT LEVERAGE:", res, flush=True)
    if extract_code(res) == 1000:
        LEVERAGE_CACHE[symbol] = {"leverage": LEVERAGE, "open_type": OPEN_TYPE, "ts": now}
        return True
    return False

def open_market(symbol: str, side: str) -> Dict[str, Any]:
    # sync leverage (évite 40012)
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

# ================= POSITION RESYNC =================
def fetch_position(symbol: str) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    res = bm_get_keyed("/contract/private/position", params={"symbol": symbol})
    j = res.get("json") or {}
    if j.get("code") != 1000:
        return (False, None, res)

    data = j.get("data") or []
    if not isinstance(data, list) or len(data) == 0:
        return (False, None, res)

    row = None
    for it in data:
        if (it.get("symbol") or "").upper() == symbol:
            row = it
            break
    if row is None:
        row = data[0]

    try:
        amt = float(row.get("current_amount") or 0)
    except Exception:
        amt = 0.0

    if amt == 0:
        return (False, None, res)

    ptype = row.get("position_type")
    if str(ptype) == "1":
        return (True, "LONG", res)
    if str(ptype) == "2":
        return (True, "SHORT", res)

    if amt > 0:
        return (True, "LONG", res)
    if amt < 0:
        return (True, "SHORT", res)

    return (True, None, res)

def resync_global_state(preferred_symbol: Optional[str] = None) -> None:
    symbols = [preferred_symbol] if preferred_symbol else []
    symbols += [s for s in ALLOWED_SYMBOLS if s != preferred_symbol]

    for sym in symbols:
        has_pos, side, _raw = fetch_position(sym)
        if has_pos:
            STATE.update({"in_position": True, "symbol": sym, "side": side})
            print("RESYNC: FOUND OPEN POSITION ON BITMART", {"symbol": sym, "side": side}, flush=True)
            return

    STATE.update({"in_position": False, "symbol": None, "side": None})
    print("RESYNC: NO OPEN POSITION ON BITMART", flush=True)

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
        "open_type": OPEN_TYPE
    }), 200

@app.get("/debug/state")
def debug_state():
    return jsonify({"state": STATE}), 200

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
    data = request.get_json(silent=True) or {}

    if data.get("secret") != SECRET:
        return jsonify({"status": "forbidden"}), 403

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

    if event == "RESET":
        resync_global_state(preferred_symbol=symbol if symbol in ALLOWED_SYMBOLS else None)
        STATE["last_entry_bar_key"] = None
        return jsonify({"status": "state_resynced", "state": STATE}), 200

    if event == "SQUEEZE":
        STATE["squeeze_on"] = parse_bool(data.get("on"))
        print("SQUEEZE UPDATE:", STATE["squeeze_on"], flush=True)
        return jsonify({"status": "squeeze_set", "on": STATE["squeeze_on"]}), 200

    # Mode B : une seule position globale
    if STATE["in_position"] and STATE["symbol"] != symbol:
        print("IGNORED OTHER SYMBOL", {"open": STATE["symbol"], "got": symbol}, flush=True)
        return jsonify({"status": "ignored_other_symbol"}), 200

    # ================= SORTIES =================
    is_any_exit = (event in {"EMA_EXIT", "STOCH_EXIT"} or action in {"EXIT_LONG", "EXIT_SHORT"})
    if is_any_exit and not STATE["in_position"]:
        print("EXIT RECEIVED BUT STATE FLAT -> RESYNC", flush=True)
        resync_global_state(preferred_symbol=symbol)

    if STATE["in_position"]:
        # EMA exit
        if event == "EMA_EXIT":
            ema_side = (data.get("side") or "").upper().strip()
            if ema_side == STATE["side"]:
                res = close_market(STATE["symbol"], STATE["side"])
                print("BITMART CLOSE (EMA):", res, flush=True)
                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_ema"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200
            return jsonify({"status": "ignored_ema_side"}), 200

        # STOCH exit (ou action exit)
        if event == "STOCH_EXIT" or action in {"EXIT_LONG", "EXIT_SHORT"}:
            if STATE["side"] == "LONG" and reason_is_hd(reason):
                res = close_market(STATE["symbol"], "LONG")
                print("BITMART CLOSE (STOCH):", res, flush=True)
                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_stoch"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

            if STATE["side"] == "SHORT" and reason_is_ld(reason):
                res = close_market(STATE["symbol"], "SHORT")
                print("BITMART CLOSE (STOCH):", res, flush=True)
                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_stoch"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

        # Vector opposé
        if event == "VECTOR":
            if STATE["side"] == "LONG" and color in SHORT_COLORS:
                res = close_market(symbol, "LONG")
                print("BITMART CLOSE (VECTOR OPP):", res, flush=True)
                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_vector_opp"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

            if STATE["side"] == "SHORT" and color in LONG_COLORS:
                res = close_market(symbol, "SHORT")
                print("BITMART CLOSE (VECTOR OPP):", res, flush=True)
                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_vector_opp"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

        return jsonify({"status": "holding"}), 200

    # ================= ENTREES =================
    if STATE["squeeze_on"]:
        return jsonify({"status": "blocked_squeeze"}), 200

    if event == "STOCH_ENTRY":
        if reason_is_ld(reason):
            bar_key = make_bar_key(symbol, tf, t, "LONG", "STOCH")
            if STATE["last_entry_bar_key"] == bar_key:
                return jsonify({"status": "ignored_same_bar"}), 200

            res_entry = open_market(symbol, "LONG")
            print("BITMART ENTRY (STOCH LONG):", res_entry, flush=True)
            if extract_code(res_entry) != 1000:
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            STATE.update({"in_position": True, "side": "LONG", "symbol": symbol, "last_entry_bar_key": bar_key})
            return jsonify({"status": "enter_long_stoch"}), 200

        if reason_is_hd(reason):
            bar_key = make_bar_key(symbol, tf, t, "SHORT", "STOCH")
            if STATE["last_entry_bar_key"] == bar_key:
                return jsonify({"status": "ignored_same_bar"}), 200

            res_entry = open_market(symbol, "SHORT")
            print("BITMART ENTRY (STOCH SHORT):", res_entry, flush=True)
            if extract_code(res_entry) != 1000:
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            STATE.update({"in_position": True, "side": "SHORT", "symbol": symbol, "last_entry_bar_key": bar_key})
            return jsonify({"status": "enter_short_stoch"}), 200

        return jsonify({"status": "ignored_stoch_unknown_reason", "reason": reason}), 200

    if event == "VECTOR":
        inferred_side = None
        if color in LONG_COLORS:
            inferred_side = "LONG"
        elif color in SHORT_COLORS:
            inferred_side = "SHORT"
        else:
            return jsonify({"status": "ignored_vector_unknown_color", "color": color}), 200

        bar_key = make_bar_key(symbol, tf, t, inferred_side, "VECTOR")
        if STATE["last_entry_bar_key"] == bar_key:
            return jsonify({"status": "ignored_same_bar"}), 200

        res_entry = open_market(symbol, inferred_side)
        print("BITMART ENTRY (VECTOR):", res_entry, flush=True)
        if extract_code(res_entry) != 1000:
            return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

        STATE.update({"in_position": True, "side": inferred_side, "symbol": symbol, "last_entry_bar_key": bar_key})

        # SL structurel
        try:
            if inferred_side == "LONG":
                sl = float(data.get("low", 0) or 0)
                if sl > 0:
                    res_sl = set_stop_loss(symbol, "LONG", sl)
                    print("BITMART SL:", res_sl, flush=True)
            else:
                sl = float(data.get("high", 0) or 0)
                if sl > 0:
                    res_sl = set_stop_loss(symbol, "SHORT", sl)
                    print("BITMART SL:", res_sl, flush=True)
        except Exception as e:
            print("SL ERROR:", str(e), flush=True)

        return jsonify({"status": "enter_vector"}), 200

    return jsonify({"status": "ignored"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
