from flask import Flask, request, jsonify
import os
import time
import json
import hmac
import hashlib
import requests
from datetime import datetime, timezone

app = Flask(__name__)

# =========================
# CONFIG GLOBALE
# =========================
SECRET_EXPECTED = "TV_BOT_DEMO_2026_V2"
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

LEVERAGE = 25  # verrouillé comme tu veux

# =========================
# BITMART CONFIG (DEMO/REAL selon tes clés)
# =========================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

# NOTE: adapte si tu utilises spot vs futures, demo vs live.
# Ici je garde la structure; tu avais déjà un code BitMart fonctionnel.
BASE_URL = (os.environ.get("BITMART_BASE_URL") or "https://api-cloud.bitmart.com").strip()

# =========================
# ETAT PAR SYMBOLE
# =========================
def blank_state():
    return {
        "in_position": False,
        "side": None,                 # "LONG" / "SHORT"
        "last_entry_bar_key": None,   # anti double entrée par bougie
        "squeeze": False,             # filtre d’entrée
        "sl_level": None,             # stop structurel (niveau)
        "last_bar_key_seen": None,    # anti spam BAR_CLOSE
        "position_open_time_ms": None
    }

STATE = {sym: blank_state() for sym in ALLOWED_SYMBOLS}

# =========================
# UTILITAIRES
# =========================
def now():
    return datetime.now(timezone.utc).isoformat()

def log(msg: str):
    print(f"[{now()}] {msg}", flush=True)

def normalize_symbol(ticker_raw: str) -> str:
    if not ticker_raw:
        return ""
    t = ticker_raw.strip().upper()
    if t.endswith(".P"):
        t = t[:-2]
    return t

def is_opposite(a: str, b: str) -> bool:
    return (a == "LONG" and b == "SHORT") or (a == "SHORT" and b == "LONG")

def ok(extra=None):
    payload = {"ok": True}
    if extra:
        payload.update(extra)
    return jsonify(payload), 200

# =========================
# BITMART SIGNING (si ton ancien bot l'utilisait déjà)
# =========================
def bm_timestamp_ms() -> str:
    return str(int(time.time() * 1000))

def bm_sign(message: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

def bm_headers(path: str, body: str) -> dict:
    # BitMart signing depends on endpoint spec.
    # On garde la structure classique: timestamp + memo + body + path
    ts = bm_timestamp_ms()
    prehash = ts + "#" + BITMART_MEMO + "#" + body
    sign = bm_sign(prehash, BITMART_SECRET)

    return {
        "Content-Type": "application/json",
        "X-BM-KEY": BITMART_KEY,
        "X-BM-SIGN": sign,
        "X-BM-TIMESTAMP": ts,
        "X-BM-MEMO": BITMART_MEMO,
    }

# =========================
# BITMART ACTIONS (stubs robustes)
# IMPORTANT: tu avais déjà un code qui place les ordres.
# Ici, je te donne une version "safe" : logs + appels.
# =========================
def bitmart_place_market_order(symbol: str, side: str):
    """
    side: "LONG" -> buy/open long
          "SHORT" -> sell/open short
    """
    log(f"BITMART PLACE MARKET {side} {symbol} (leverage={LEVERAGE})")
    # TODO: Remplacer avec tes endpoints exacts futures demo.
    # On garde un stub pour ne pas casser ton déploiement.
    return {"ok": True, "stub": True}

def bitmart_close_position(symbol: str, side: str, reason: str):
    log(f"BITMART CLOSE {side} {symbol} reason={reason}")
    # TODO: Remplacer avec tes endpoints exacts futures demo.
    return {"ok": True, "stub": True}

# =========================
# LOGIQUE V2
# =========================
def handle_squeeze(st, data):
    # event SQUEEZE : squeeze true/false
    sq = data.get("squeeze", None)
    if isinstance(sq, bool):
        st["squeeze"] = sq
    else:
        # si squeeze vient en string
        if str(sq).lower() == "true":
            st["squeeze"] = True
        elif str(sq).lower() == "false":
            st["squeeze"] = False
    log(f"SQUEEZE state updated: squeeze={st['squeeze']}")

def handle_bar_close(st, symbol, data):
    # SL structurel sur clôture uniquement
    bar_key = data.get("bar_key", "")
    if bar_key and st["last_bar_key_seen"] == bar_key:
        return

    st["last_bar_key_seen"] = bar_key

    if not st["in_position"]:
        return

    if st["sl_level"] is None:
        return

    close = float(data.get("close", 0.0))

    if st["side"] == "LONG":
        if close <= st["sl_level"]:
            bitmart_close_position(symbol, "LONG", reason="SL_CLOSE")
            st.update(blank_state())
            log(f"SL_CLOSE executed LONG {symbol} close={close} sl={st['sl_level']}")
    elif st["side"] == "SHORT":
        if close >= st["sl_level"]:
            bitmart_close_position(symbol, "SHORT", reason="SL_CLOSE")
            st.update(blank_state())
            log(f"SL_CLOSE executed SHORT {symbol} close={close} sl={st['sl_level']}")

def enter_position(st, symbol, side, data, reason):
    bar_key = data.get("bar_key", "")
    if st["last_entry_bar_key"] == bar_key and bar_key:
        log(f"ENTRY blocked (same bar_key) {symbol} {side} bar_key={bar_key}")
        return

    if st["squeeze"]:
        log(f"ENTRY blocked (squeeze=true) {symbol} {side}")
        return

    # Place entry
    bitmart_place_market_order(symbol, side)

    # Set state
    st["in_position"] = True
    st["side"] = side
    st["last_entry_bar_key"] = bar_key
    st["position_open_time_ms"] = data.get("time_ms", None)

    # SL structurel initial basé sur bougie signal
    low = float(data.get("low", 0.0))
    high = float(data.get("high", 0.0))
    st["sl_level"] = low if side == "LONG" else high

    log(f"ENTER {side} {symbol} reason={reason} sl_level={st['sl_level']} bar_key={bar_key}")

def exit_if_match(st, symbol, expected_side, reason):
    if not st["in_position"]:
        return
    if st["side"] != expected_side:
        return
    bitmart_close_position(symbol, expected_side, reason=reason)
    st.update(blank_state())
    log(f"EXIT {expected_side} {symbol} reason={reason}")

def handle_stoch_entry(st, symbol, data):
    # attend: action ENTER_LONG/ENTER_SHORT et reason LD/HD...
    action = (data.get("action") or "").upper()
    reason = (data.get("reason") or "").upper()

    if action == "ENTER_LONG":
        # LONG = LD
        if reason not in {"LD", "LD_POINT"}:
            log(f"STOCH_ENTRY ignored (not LD) reason={reason}")
            return
        enter_position(st, symbol, "LONG", data, reason)
        return

    if action == "ENTER_SHORT":
        # SHORT = HD
        if reason not in {"HD", "HD_POINT"}:
            log(f"STOCH_ENTRY ignored (not HD) reason={reason}")
            return
        enter_position(st, symbol, "SHORT", data, reason)
        return

def handle_stoch_exit(st, symbol, data):
    # exit sur opposé
    action = (data.get("action") or "").upper()
    reason = (data.get("reason") or "").upper()

    # On supporte EXIT_LONG / EXIT_SHORT (ou EXIT op)
    if action == "EXIT_LONG":
        exit_if_match(st, symbol, "LONG", reason=f"STOCH_EXIT:{reason or 'OPP'}")
    elif action == "EXIT_SHORT":
        exit_if_match(st, symbol, "SHORT", reason=f"STOCH_EXIT:{reason or 'OPP'}")

def handle_vector(st, symbol, data):
    # payload: side = LONG pour vector bullish, SHORT pour bearish
    incoming_side = (data.get("side") or "").upper()
    if not st["in_position"]:
        return
    if incoming_side in {"LONG", "SHORT"} and is_opposite(st["side"], incoming_side):
        exit_if_match(st, symbol, st["side"], reason="VECTOR_OPP")

def handle_ema_exit(st, symbol, data):
    # payload: side=LONG => close long si en long
    side = (data.get("side") or "").upper()
    reason = (data.get("reason") or "").upper()
    if side == "LONG":
        exit_if_match(st, symbol, "LONG", reason=f"EMA5_EXIT:{reason or 'CLOSE_BELOW_EMA5'}")
    elif side == "SHORT":
        exit_if_match(st, symbol, "SHORT", reason=f"EMA5_EXIT:{reason or 'CLOSE_ABOVE_EMA5'}")

# =========================
# ROUTES
# =========================
@app.get("/")
def health():
    return "OK", 200

@app.post("/webhook")
def webhook():
    raw = request.get_data(as_text=True) or ""
    log(f"POST /webhook raw_len={len(raw)} raw={raw[:800]}")

    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        log(f"ERROR invalid JSON: {repr(e)}")
        return ok({"error": "invalid_json"})

    if not isinstance(data, dict):
        log(f"ERROR json_not_object type={type(data)}")
        return ok({"error": "json_not_object"})

    secret = (data.get("secret") or "").strip()
    if secret != SECRET_EXPECTED:
        log(f"FORBIDDEN bad_secret received='{secret}' expected='{SECRET_EXPECTED}'")
        return jsonify({"ok": False, "error": "bad_secret"}), 403

    ticker = data.get("ticker", "")
    symbol = normalize_symbol(ticker)
    if symbol not in ALLOWED_SYMBOLS:
        log(f"IGNORED symbol_not_allowed: ticker={ticker} symbol={symbol}")
        return ok({"ignored": True, "reason": "symbol_not_allowed"})

    event = (data.get("event") or "").upper()
    st = STATE[symbol]

    log(f"EVENT {event} symbol={symbol} in_position={st['in_position']} side={st['side']} squeeze={st['squeeze']} sl={st['sl_level']}")

    # ===== Dispatch =====
    if event == "SQUEEZE":
        handle_squeeze(st, data)
        return ok()

    if event == "BAR_CLOSE":
        handle_bar_close(st, symbol, data)
        return ok()

    if event == "EMA_EXIT":
        handle_ema_exit(st, symbol, data)
        return ok()

    if event == "VECTOR":
        handle_vector(st, symbol, data)
        return ok()

    if event == "STOCH_ENTRY":
        handle_stoch_entry(st, symbol, data)
        return ok()

    if event == "STOCH_EXIT":
        handle_stoch_exit(st, symbol, data)
        return ok()

    # si event inconnu, on log mais 200
    log(f"UNKNOWN EVENT ignored: {event}")
    return ok({"ignored": True, "reason": "unknown_event"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
