from flask import Flask, request, jsonify
import os
import time
import json
import hmac
import hashlib
import requests

app = Flask(__name__)

# ================= STRATEGIE =================
LONG_COLORS = {"green", "blue"}
SHORT_COLORS = {"red", "purple"}   # IMPORTANT: pourpre = purple
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

STATE = {
    "in_position": False,
    "side": None,               # "LONG" ou "SHORT"
    "symbol": None,             # "BTCUSDT", etc.
    "last_entry_bar_key": None, # lock anti double entrée sur la même bougie
    "squeeze_on": False,        # bloque les entrées
}

SECRET = "TV_BOT_DEMO_2026_V2"

# ================= BITMART CONFIG =================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

# DEMO
BASE_URL = "https://demo-api-cloud-v2.bitmart.com"

# Leverage
LEVERAGE = (os.environ.get("LEVERAGE") or "25").strip()
OPEN_TYPE = (os.environ.get("OPEN_TYPE") or "isolated").strip().lower()  # "isolated" ou "cross"

# Cache simple: on évite d'appeler submit-leverage 200 fois
LEVERAGE_CACHE = {
    # "BTCUSDT": {"leverage":"25","open_type":"isolated","ts": 123}
}

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

def extract_code(res: dict):
    j = res.get("json") or {}
    return j.get("code")

def sign_request(timestamp: int, body: dict) -> str:
    body_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
    message = f"{timestamp}#{BITMART_MEMO}#{body_str}"
    return hmac.new(
        BITMART_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

def bm_post(path: str, body: dict) -> dict:
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

def bm_get_keyed(path: str, params: dict | None = None) -> dict:
    headers = {"X-BM-KEY": BITMART_KEY}
    try:
        r = requests.get(BASE_URL + path, headers=headers, params=params or {}, timeout=15)
        try:
            return {"http": r.status_code, "json": r.json()}
        except Exception:
            return {"http": r.status_code, "text": r.text}
    except Exception as e:
        return {"http": 0, "error": str(e)}

def make_bar_key(symbol: str, tf: str | None, t: str | None, side: str | None, source: str | None) -> str:
    return f"{symbol}|{tf or ''}|{t or ''}|{side or ''}|{source or ''}"

# ================= BITMART ACTIONS =================
def submit_leverage(symbol: str) -> dict:
    """
    Fix code 40012:
    BitMart demande que le leverage envoyé dans l'ordre = leverage configuré côté compte.
    On le définit explicitement avant d'entrer.
    Endpoint: /contract/private/submit-leverage
    """
    body = {
        "symbol": symbol,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE
    }
    return bm_post("/contract/private/submit-leverage", body)

def ensure_leverage_synced(symbol: str) -> bool:
    now = int(time.time())
    cached = LEVERAGE_CACHE.get(symbol)

    # refresh toutes les 10 minutes
    if cached and cached.get("leverage") == LEVERAGE and cached.get("open_type") == OPEN_TYPE and (now - cached.get("ts", 0) < 600):
        return True

    res = submit_leverage(symbol)
    print("BITMART SUBMIT LEVERAGE:", res)

    if extract_code(res) == 1000:
        LEVERAGE_CACHE[symbol] = {"leverage": LEVERAGE, "open_type": OPEN_TYPE, "ts": now}
        return True

    return False

def open_market(symbol: str, side: str) -> dict:
    # 1) set leverage first
    ensure_leverage_synced(symbol)

    # 2) place order
    return bm_post("/contract/private/submit-order", {
        "symbol": symbol,
        "type": "market",
        "side": 1 if side == "LONG" else 4,   # 1=buy open, 4=sell open
        "mode": 1,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "size": get_size(symbol)
    })

def close_market(symbol: str, side: str) -> dict:
    return bm_post("/contract/private/submit-order", {
        "symbol": symbol,
        "type": "market",
        "side": 3 if side == "LONG" else 2,   # 3=sell close, 2=buy close
        "mode": 1,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "size": get_size(symbol)
    })

def set_stop_loss(symbol: str, side: str, price: float) -> dict:
    return bm_post("/contract/private/submit-tp-sl-order", {
        "symbol": symbol,
        "type": "stop_loss",
        "side": 3 if side == "LONG" else 2,
        "trigger_price": f"{price:.2f}",
        "executive_price": f"{price:.2f}",
        "price_type": 1,
        "plan_category": 2,
        "category": "market",
        "size": get_size(symbol)
    })

# ================= POSITION RESYNC =================
def fetch_position(symbol: str) -> tuple[bool, str | None, dict]:
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

def resync_global_state(preferred_symbol: str | None = None) -> None:
    symbols = [preferred_symbol] if preferred_symbol else []
    symbols += [s for s in ALLOWED_SYMBOLS if s != preferred_symbol]

    for sym in symbols:
        has_pos, side, _raw = fetch_position(sym)
        if has_pos:
            STATE.update({"in_position": True, "symbol": sym, "side": side})
            print("RESYNC: FOUND OPEN POSITION ON BITMART", {"symbol": sym, "side": side})
            return

    STATE.update({"in_position": False, "symbol": None, "side": None})
    print("RESYNC: NO OPEN POSITION ON BITMART")

# ================= ROUTES =================
@app.get("/")
def home():
    return "Bot TradingView DEMO V2 actif"

@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}

    if data.get("secret") != SECRET:
        return jsonify({"status": "forbidden"}), 403

    print("ALERTE:", data)

    event = (data.get("event") or "").upper().strip()
    action = (data.get("action") or "").upper().strip()
    reason = (data.get("reason") or "").upper().strip()
    color = (data.get("color") or "").lower().strip()

    symbol = normalize_symbol(data.get("ticker"))
    tf = str(data.get("tf") or "")
    t = str(data.get("time") or data.get("time_ms") or "")

    if symbol not in ALLOWED_SYMBOLS and event not in {"RESET", "SQUEEZE"}:
        print("IGNORED SYMBOL:", symbol)
        return jsonify({"status": "ignored_symbol"}), 200

    if event == "RESET":
        resync_global_state(preferred_symbol=symbol if symbol in ALLOWED_SYMBOLS else None)
        STATE["last_entry_bar_key"] = None
        print("STATE RESET (SAFE) OK", STATE)
        return jsonify({"status": "state_resynced", "state": STATE}), 200

    if event == "SQUEEZE":
        STATE["squeeze_on"] = bool(data.get("on"))
        print("SQUEEZE UPDATE:", STATE["squeeze_on"])
        return jsonify({"status": "squeeze_set", "on": STATE["squeeze_on"]}), 200

    # Mode B : une seule position globale
    if STATE["in_position"] and STATE["symbol"] != symbol:
        print(f"IGNORED OTHER SYMBOL open={STATE['symbol']} got={symbol}")
        return jsonify({"status": "ignored_other_symbol"}), 200

    # ===== SORTIES =====
    if (event == "EMA_EXIT" or action in {"EXIT_LONG", "EXIT_SHORT"}) and not STATE["in_position"]:
        print("EXIT RECEIVED BUT STATE FLAT -> RESYNC")
        resync_global_state(preferred_symbol=symbol)

    if STATE["in_position"]:
        # EMA_EXIT (EMA5)
        if event == "EMA_EXIT":
            ema_side = (data.get("side") or "").upper().strip()
            if ema_side == STATE["side"]:
                print("EXIT POSITION (EMA_EXIT)", STATE)
                res = close_market(STATE["symbol"], STATE["side"])
                print("BITMART CLOSE:", res)

                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_ema"}), 200

                return jsonify({"status": "close_failed", "bitmart": res}), 200

            print("EMA_EXIT ignored (side mismatch)", {"ema_side": ema_side, "state_side": STATE["side"]})
            return jsonify({"status": "ignored_ema_side"}), 200

        # STOCH opposé
        if event == "STOCH_ENTRY":
            if STATE["side"] == "LONG" and reason == "HD":
                print("EXIT LONG (STOCH OPP HD)", STATE)
                res = close_market(STATE["symbol"], "LONG")
                print("BITMART CLOSE:", res)
                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_stoch_opp"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

            if STATE["side"] == "SHORT" and reason == "LD":
                print("EXIT SHORT (STOCH OPP LD)", STATE)
                res = close_market(STATE["symbol"], "SHORT")
                print("BITMART CLOSE:", res)
                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_stoch_opp"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

        # VECTOR opposé
        if event == "VECTOR":
            if STATE["side"] == "LONG" and color in SHORT_COLORS:
                print("EXIT LONG (VECTOR OPP)", STATE)
                res = close_market(symbol, "LONG")
                print("BITMART CLOSE:", res)
                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_vector_opp"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

            if STATE["side"] == "SHORT" and color in LONG_COLORS:
                print("EXIT SHORT (VECTOR OPP)", STATE)
                res = close_market(symbol, "SHORT")
                print("BITMART CLOSE:", res)
                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit_vector_opp"}), 200
                return jsonify({"status": "close_failed", "bitmart": res}), 200

        print("HOLDING POSITION", STATE)
        return jsonify({"status": "holding"}), 200

    # ===== ENTREES =====
    if STATE["squeeze_on"]:
        print("ENTRY BLOCKED (SQUEEZE ON)")
        return jsonify({"status": "blocked_squeeze"}), 200

    # STOCH_ENTRY = entrée indépendante
    if event == "STOCH_ENTRY":
        if reason == "LD":
            bar_key = make_bar_key(symbol, tf, t, "LONG", "STOCH")
            if STATE["last_entry_bar_key"] == bar_key:
                return jsonify({"status": "ignored_same_bar"}), 200

            print("ENTER LONG (STOCH LD)", symbol, {"bar_key": bar_key})
            res_entry = open_market(symbol, "LONG")
            print("BITMART ENTRY:", res_entry)

            # retry si 40012
            if extract_code(res_entry) == 40012:
                print("40012 -> resync leverage then retry")
                ensure_leverage_synced(symbol)
                time.sleep(1)
                res_entry = open_market(symbol, "LONG")
                print("BITMART ENTRY RETRY:", res_entry)

            if extract_code(res_entry) != 1000:
                print("ENTRY FAILED - NOT UPDATING STATE")
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            STATE.update({"in_position": True, "side": "LONG", "symbol": symbol, "last_entry_bar_key": bar_key})
            return jsonify({"status": "enter_long_stoch"}), 200

        if reason == "HD":
            bar_key = make_bar_key(symbol, tf, t, "SHORT", "STOCH")
            if STATE["last_entry_bar_key"] == bar_key:
                return jsonify({"status": "ignored_same_bar"}), 200

            print("ENTER SHORT (STOCH HD)", symbol, {"bar_key": bar_key})
            res_entry = open_market(symbol, "SHORT")
            print("BITMART ENTRY:", res_entry)

            if extract_code(res_entry) == 40012:
                print("40012 -> resync leverage then retry")
                ensure_leverage_synced(symbol)
                time.sleep(1)
                res_entry = open_market(symbol, "SHORT")
                print("BITMART ENTRY RETRY:", res_entry)

            if extract_code(res_entry) != 1000:
                print("ENTRY FAILED - NOT UPDATING STATE")
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            STATE.update({"in_position": True, "side": "SHORT", "symbol": symbol, "last_entry_bar_key": bar_key})
            return jsonify({"status": "enter_short_stoch"}), 200

        return jsonify({"status": "ignored_stoch_unknown_reason"}), 200

    # VECTOR = entrée indépendante
    if event == "VECTOR":
        inferred_side = None
        if color in LONG_COLORS:
            inferred_side = "LONG"
        elif color in SHORT_COLORS:
            inferred_side = "SHORT"

        if inferred_side is None:
            return jsonify({"status": "ignored_vector_unknown_color"}), 200

        bar_key = make_bar_key(symbol, tf, t, inferred_side, "VECTOR")
        if STATE["last_entry_bar_key"] == bar_key:
            return jsonify({"status": "ignored_same_bar"}), 200

        if inferred_side == "LONG":
            print("ENTER LONG (VECTOR)", symbol, {"color": color, "bar_key": bar_key})
            res_entry = open_market(symbol, "LONG")
            print("BITMART ENTRY:", res_entry)

            if extract_code(res_entry) == 40012:
                print("40012 -> resync leverage then retry")
                ensure_leverage_synced(symbol)
                time.sleep(1)
                res_entry = open_market(symbol, "LONG")
                print("BITMART ENTRY RETRY:", res_entry)

            if extract_code(res_entry) != 1000:
                print("ENTRY FAILED - NOT UPDATING STATE")
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            STATE.update({"in_position": True, "side": "LONG", "symbol": symbol, "last_entry_bar_key": bar_key})

            sl = float(data.get("low", 0) or 0)
            if sl > 0:
                res_sl = set_stop_loss(symbol, "LONG", sl)
                print("BITMART SL:", res_sl)

            return jsonify({"status": "enter_long_vector"}), 200

        if inferred_side == "SHORT":
            print("ENTER SHORT (VECTOR)", symbol, {"color": color, "bar_key": bar_key})
            res_entry = open_market(symbol, "SHORT")
            print("BITMART ENTRY:", res_entry)

            if extract_code(res_entry) == 40012:
                print("40012 -> resync leverage then retry")
                ensure_leverage_synced(symbol)
                time.sleep(1)
                res_entry = open_market(symbol, "SHORT")
                print("BITMART ENTRY RETRY:", res_entry)

            if extract_code(res_entry) != 1000:
                print("ENTRY FAILED - NOT UPDATING STATE")
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            STATE.update({"in_position": True, "side": "SHORT", "symbol": symbol, "last_entry_bar_key": bar_key})

            sl = float(data.get("high", 0) or 0)
            if sl > 0:
                res_sl = set_stop_loss(symbol, "SHORT", sl)
                print("BITMART SL:", res_sl)

            return jsonify({"status": "enter_short_vector"}), 200

    return jsonify({"status": "ignored"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
