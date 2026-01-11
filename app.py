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
SHORT_COLORS = {"red", "pink", "purple"}
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Mode B : une seule position globale à la fois
STATE = {
    "in_position": False,
    "side": None,     # "LONG" ou "SHORT"
    "symbol": None
}

SECRET = "TV_BOT_DEMO_2026"

# ================= BITMART CONFIG =================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

BASE_URL = "https://demo-api-cloud-v2.bitmart.com"

# ================= UTILS =================
def normalize_symbol(s):
    """
    TradingView peut envoyer:
      - BTCUSDT
      - BTCUSDT.P  (perp)
    On normalise pour matcher ALLOWED_SYMBOLS et BitMart.
    """
    sym = (s or "").upper().strip()
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym

def get_size(symbol):
    # size Futures = int, on force >= 1
    try:
        n = int(os.environ.get(f"SIZE_{symbol}", "1"))
        return max(1, n)
    except Exception:
        return 1

def extract_code(res: dict):
    j = res.get("json") or {}
    return j.get("code")

def sign_request(timestamp, body):
    """
    BitMart signature (cloud v2):
      sign = HMAC_SHA256(secret, f"{timestamp}#{memo}#{body_json_sorted}")
    """
    body_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
    message = f"{timestamp}#{BITMART_MEMO}#{body_str}"
    return hmac.new(
        BITMART_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

def bm_post(path, body):
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

# ================= BITMART ACTIONS =================
def open_market(symbol, side):
    return bm_post("/contract/private/submit-order", {
        "symbol": symbol,
        "type": "market",
        "side": 1 if side == "LONG" else 4,   # 1=buy open, 4=sell open
        "mode": 1,
        "leverage": "1",
        "open_type": "isolated",
        "size": get_size(symbol)
    })

def close_market(symbol, side):
    return bm_post("/contract/private/submit-order", {
        "symbol": symbol,
        "type": "market",
        "side": 3 if side == "LONG" else 2,   # 3=sell close, 2=buy close
        "mode": 1,
        "leverage": "1",
        "open_type": "isolated",
        "size": get_size(symbol)
    })

def set_stop_loss(symbol, side, price):
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

# ================= ROUTES =================
@app.get("/")
def home():
    return "Bot TradingView DEMO actif"

@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}

    # Sécurité
    if data.get("secret") != SECRET:
        return jsonify({"status": "forbidden"}), 403

    print("ALERTE:", data)

    # ===== RESET MANUEL (TEST / DEMO) =====
    if data.get("event") == "RESET":
        STATE.update({"in_position": False, "side": None, "symbol": None})
        print("STATE RESET OK")
        return jsonify({"status": "state_reset"}), 200

    symbol = normalize_symbol(data.get("ticker"))
    event = data.get("event")
    color = data.get("color")
    action = data.get("action")

    if symbol not in ALLOWED_SYMBOLS:
        print("IGNORED SYMBOL:", symbol)
        return jsonify({"status": "ignored_symbol"}), 200

    # Mode B : une seule position globale
    if STATE["in_position"] and STATE["symbol"] != symbol:
        print(f"IGNORED OTHER SYMBOL open={STATE['symbol']} got={symbol}")
        return jsonify({"status": "ignored_other_symbol"}), 200

    # ========== SORTIES ==========
    if STATE["in_position"]:
        # Sortie via Stoch
        if action in {"EXIT_LONG", "EXIT_SHORT"}:
            print("EXIT POSITION (STOCH)", STATE)
            res = close_market(STATE["symbol"], STATE["side"])
            print("BITMART CLOSE:", res)

            if extract_code(res) == 1000:
                STATE.update({"in_position": False, "side": None, "symbol": None})
                return jsonify({"status": "exit"}), 200

            print("CLOSE FAILED - KEEPING STATE", STATE)
            return jsonify({"status": "close_failed", "bitmart": res}), 200

        # Sortie via vecteur opposé
        if event == "VECTOR":
            if STATE["side"] == "LONG" and color in SHORT_COLORS:
                print("EXIT LONG (VECTOR OPP)", STATE)
                res = close_market(symbol, "LONG")
                print("BITMART CLOSE:", res)

                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit"}), 200

                print("CLOSE FAILED - KEEPING STATE", STATE)
                return jsonify({"status": "close_failed", "bitmart": res}), 200

            if STATE["side"] == "SHORT" and color in LONG_COLORS:
                print("EXIT SHORT (VECTOR OPP)", STATE)
                res = close_market(symbol, "SHORT")
                print("BITMART CLOSE:", res)

                if extract_code(res) == 1000:
                    STATE.update({"in_position": False, "side": None, "symbol": None})
                    return jsonify({"status": "exit"}), 200

                print("CLOSE FAILED - KEEPING STATE", STATE)
                return jsonify({"status": "close_failed", "bitmart": res}), 200

        print("HOLDING POSITION", STATE)
        return jsonify({"status": "holding"}), 200

    # ========== ENTREES ==========
    if event == "VECTOR":
        if color in LONG_COLORS:
            print("ENTER LONG", symbol)
            res_entry = open_market(symbol, "LONG")
            print("BITMART ENTRY:", res_entry)

            if extract_code(res_entry) != 1000:
                print("ENTRY FAILED - NOT UPDATING STATE")
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            STATE.update({"in_position": True, "side": "LONG", "symbol": symbol})

            sl = float(data.get("low", 0) or 0)
            if sl > 0:
                res_sl = set_stop_loss(symbol, "LONG", sl)
                print("BITMART SL:", res_sl)
                if extract_code(res_sl) != 1000:
                    print("SL FAILED (position kept)")

            return jsonify({"status": "enter_long"}), 200

        if color in SHORT_COLORS:
            print("ENTER SHORT", symbol)
            res_entry = open_market(symbol, "SHORT")
            print("BITMART ENTRY:", res_entry)

            if extract_code(res_entry) != 1000:
                print("ENTRY FAILED - NOT UPDATING STATE")
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            STATE.update({"in_position": True, "side": "SHORT", "symbol": symbol})

            sl = float(data.get("high", 0) or 0)
            if sl > 0:
                res_sl = set_stop_loss(symbol, "SHORT", sl)
                print("BITMART SL:", res_sl)
                if extract_code(res_sl) != 1000:
                    print("SL FAILED (position kept)")

            return jsonify({"status": "enter_short"}), 200

    print("IGNORED (NO RULE MATCHED) state=", STATE, "event=", event, "color=", color, "action=", action)
    return jsonify({"status": "ignored"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
