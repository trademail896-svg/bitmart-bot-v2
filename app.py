from flask import Flask, request, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)

# ======================
# CONFIG
# ======================
SECRET_EXPECTED = "TV_BOT_DEMO_2026_V2"

ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

def now():
    return datetime.utcnow().isoformat() + "Z"

def normalize_symbol(ticker_raw: str) -> str:
    if not ticker_raw:
        return ""
    t = ticker_raw.strip().upper()
    # TradingView futures/perp sometimes adds .P
    if t.endswith(".P"):
        t = t[:-2]
    return t

def log(msg: str):
    print(f"[{now()}] {msg}", flush=True)

@app.get("/")
def health():
    return "OK", 200

@app.post("/webhook")
def webhook():
    # 1) Log raw request
    raw = request.get_data(as_text=True)  # body brut
    log(f"POST /webhook raw_len={len(raw)} raw={raw[:500]}")  # limite à 500 chars

    # 2) Parse JSON if possible
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        log(f"ERROR JSON parse: {repr(e)}")
        # IMPORTANT: pour debug, on renvoie 200 mais on indique l'erreur
        return jsonify({"ok": False, "error": "invalid_json", "detail": str(e)}), 200

    if not isinstance(data, dict):
        log(f"ERROR JSON not an object: type={type(data)}")
        return jsonify({"ok": False, "error": "json_not_object"}), 200

    # 3) Validate secret
    secret = (data.get("secret") or "").strip()
    if secret != SECRET_EXPECTED:
        log(f"FORBIDDEN secret mismatch received='{secret}' expected='{SECRET_EXPECTED}'")
        # Pour debug, on renvoie 403 (comme maintenant) + un message clair
        return jsonify({"ok": False, "error": "forbidden", "reason": "bad_secret"}), 403

    # 4) Normalize symbol + basic checks
    ticker = data.get("ticker", "")
    symbol = normalize_symbol(ticker)
    event = data.get("event", "")
    tf = data.get("tf", "")
    bar_key = data.get("bar_key", "")

    log(f"OK secret ✅ event={event} ticker={ticker} symbol={symbol} tf={tf} bar_key={bar_key}")

    if symbol and symbol not in ALLOWED_SYMBOLS:
        log(f"IGNORED symbol not allowed: {symbol}")
        return jsonify({"ok": True, "ignored": True, "reason": "symbol_not_allowed"}), 200

    # 5) Always return 200 if secret OK
    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
