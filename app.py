from flask import Flask, request, jsonify
import os
import json
from datetime import datetime, timezone

app = Flask(__name__)

# =========================
# CONFIG
# =========================
SECRET = "TV_BOT_DEMO_2026_V2"

# Symbols allowed (normalized)
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Vector colors (IMPORTANT: purple = pourpre)
BULL_VECTORS = {"green", "blue"}
BEAR_VECTORS = {"red", "purple"}

# Global single-position mode
STATE = {
    "in_position": False,
    "side": None,          # "LONG" or "SHORT"
    "symbol": None,        # normalized
    "entry_bar_key": None,

    "squeeze_on": False,

    # last signals
    "last_stoch": None,    # {"reason":"LD/HD","bar_key":..., "ticker":...}
    "last_vector": None,   # {"color":..., "bar_key":..., "ticker":...}

    # lock
    "last_action_bar_key": None,  # prevent double action same bar
}

# =========================
# HELPERS
# =========================
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)

def normalize_symbol(tv_ticker: str) -> str:
    if not tv_ticker:
        return ""
    t = tv_ticker.strip().upper()
    # normalize BitMart perp format: BTCUSDT.P -> BTCUSDT
    if t.endswith(".P"):
        t = t[:-2]
    return t

def safe_float(x):
    try:
        if x is None:
            return None
        # handle strings like "90627.6"
        return float(str(x).strip())
    except Exception:
        return None

def parse_json_body():
    raw = request.get_data(as_text=True) or ""
    raw = raw.strip()
    if not raw:
        return None, raw, "empty body"
    try:
        return json.loads(raw), raw, None
    except Exception as e:
        return None, raw, f"json parse error: {e}"

def get_bar_key(payload: dict) -> str:
    # bar_key optional; if missing, create a deterministic fallback
    bk = payload.get("bar_key")
    if bk:
        return str(bk)
    t = payload.get("ticker") or ""
    tf = payload.get("tf") or payload.get("interval") or ""
    tm = payload.get("time_ms") or payload.get("time") or ""
    return f"{t}|{tf}|{tm}"

# =========================
# BITMART PLACEHOLDERS
# IMPORTANT: Replace these with your existing working BitMart DEMO functions.
# =========================
def bitmart_enter(symbol: str, side: str, leverage: int = 25):
    """
    side: 'LONG' or 'SHORT'
    leverage: your standard leverage (25x)
    Replace with your real BitMart API entry order.
    """
    log(f"BITMART ENTER (stub) symbol={symbol} side={side} lev={leverage}")
    return True, {"stub": True}

def bitmart_exit(symbol: str, side: str):
    """
    Replace with your real BitMart API close position.
    """
    log(f"BITMART EXIT (stub) symbol={symbol} side={side}")
    return True, {"stub": True}

# =========================
# STRATEGY LOGIC
# =========================
def can_trade_symbol(symbol: str) -> bool:
    return symbol in ALLOWED_SYMBOLS

def try_entry():
    """
    ENTRY RULES:
    LONG  = LD + (green/blue) + squeeze OFF
    SHORT = HD + (red/purple) + squeeze OFF
    """
    if STATE["in_position"]:
        return
    if STATE["squeeze_on"]:
        return

    st = STATE["last_stoch"]
    vx = STATE["last_vector"]
    if not st or not vx:
        return

    # strict match: same bar_key (cleanest)
    st_bk = st.get("bar_key")
    vx_bk = vx.get("bar_key")
    if not st_bk or not vx_bk:
        return
    if st_bk != vx_bk:
        return

    bar_key = st_bk
    if STATE["last_action_bar_key"] == bar_key:
        return

    reason = (st.get("reason") or "").upper()  # LD/HD
    color = (vx.get("color") or "").lower()

    symbol = normalize_symbol(vx.get("ticker") or st.get("ticker") or "")
    if not can_trade_symbol(symbol):
        return

    # LONG
    if reason == "LD" and color in BULL_VECTORS:
        ok, info = bitmart_enter(symbol, "LONG", leverage=25)
        if ok:
            STATE["in_position"] = True
            STATE["side"] = "LONG"
            STATE["symbol"] = symbol
            STATE["entry_bar_key"] = bar_key
            STATE["last_action_bar_key"] = bar_key
            log(f"ENTRY ✅ LONG symbol={symbol} bar_key={bar_key} info={info}")
        return

    # SHORT
    if reason == "HD" and color in BEAR_VECTORS:
        ok, info = bitmart_enter(symbol, "SHORT", leverage=25)
        if ok:
            STATE["in_position"] = True
            STATE["side"] = "SHORT"
            STATE["symbol"] = symbol
            STATE["entry_bar_key"] = bar_key
            STATE["last_action_bar_key"] = bar_key
            log(f"ENTRY ✅ SHORT symbol={symbol} bar_key={bar_key} info={info}")
        return

def do_exit(reason: str):
    if not STATE["in_position"]:
        return
    symbol = STATE["symbol"]
    side = STATE["side"]
    ok, info = bitmart_exit(symbol, side)
    if ok:
        log(f"EXIT ✅ {side} symbol={symbol} reason={reason} info={info}")
        STATE["in_position"] = False
        STATE["side"] = None
        STATE["symbol"] = None
        STATE["entry_bar_key"] = None
    else:
        log(f"EXIT ❌ {side} symbol={symbol} reason={reason} info={info}")

def evaluate_exit(event: str, payload: dict):
    """
    EXIT RULES:
    LONG exits if:
      - EMA_EXIT side=LONG  (close below EMA5)
      - STOCH_ENTRY reason=HD (opposite)
      - VECTOR color in (red/purple)

    SHORT exits if:
      - EMA_EXIT side=SHORT (close above EMA5)
      - STOCH_ENTRY reason=LD (opposite)
      - VECTOR color in (green/blue)
    """
    if not STATE["in_position"]:
        return

    pos_side = STATE["side"]

    if event == "EMA_EXIT":
        side = (payload.get("side") or "").upper()
        if side == pos_side:
            do_exit(payload.get("reason") or "EMA_EXIT")
        return

    if event == "STOCH_ENTRY":
        r = (payload.get("reason") or "").upper()
        if pos_side == "LONG" and r == "HD":
            do_exit("STOCH_OPPOSITE_HD")
        elif pos_side == "SHORT" and r == "LD":
            do_exit("STOCH_OPPOSITE_LD")
        return

    if event == "VECTOR":
        c = (payload.get("color") or "").lower()
        if pos_side == "LONG" and c in BEAR_VECTORS:
            do_exit(f"VECTOR_OPPOSITE_{c}")
        elif pos_side == "SHORT" and c in BULL_VECTORS:
            do_exit(f"VECTOR_OPPOSITE_{c}")
        return

# =========================
# ROUTES
# =========================
@app.get("/")
def health():
    return jsonify({"ok": True, "time": now_iso(), "state": STATE})

@app.post("/webhook")
def webhook():
    data, raw, err = parse_json_body()
    log(f"POST /webhook raw_len={len(raw)} raw={raw[:2000]}")

    # Keep 200 for TV stability when message is bad
    if err:
        log(f"ERROR {err}")
        return jsonify({"ok": False, "error": err}), 200

    # Secret validation
    if (data.get("secret") or "").strip() != SECRET:
        log("403 invalid secret")
        return jsonify({"ok": False, "error": "invalid secret"}), 403

    event = (data.get("event") or "").strip().upper()
    ticker = data.get("ticker") or ""
    symbol = normalize_symbol(ticker)
    bar_key = get_bar_key(data)

    # parse ohlc (optional)
    o = safe_float(data.get("open"))
    h = safe_float(data.get("high"))
    l = safe_float(data.get("low"))
    c = safe_float(data.get("close"))

    log(f"EVENT {event} ticker={ticker} symbol={symbol} tf={data.get('tf')} bar_key={bar_key} in_position={STATE['in_position']} side={STATE['side']} squeeze={STATE['squeeze_on']}")

    # --- SQUEEZE ---
    if event == "SQUEEZE":
        # expects payload: {"on": true/false}
        STATE["squeeze_on"] = bool(data.get("on"))
        return jsonify({"ok": True, "event": "SQUEEZE", "on": STATE["squeeze_on"]}), 200

    # --- VECTOR ---
    if event == "VECTOR":
        color = (data.get("color") or "").lower().strip()
        STATE["last_vector"] = {
            "color": color,
            "ticker": ticker,
            "symbol": symbol,
            "bar_key": bar_key,
            "open": o, "high": h, "low": l, "close": c,
        }

        # exits first
        evaluate_exit("VECTOR", data)
        # then entry attempt
        try_entry()

        return jsonify({"ok": True, "event": "VECTOR", "color": color}), 200

    # --- STOCH ENTRY (LD/HD) ---
    if event == "STOCH_ENTRY":
        reason = (data.get("reason") or "").upper().strip()  # LD/HD
        STATE["last_stoch"] = {
            "reason": reason,
            "ticker": ticker,
            "symbol": symbol,
            "bar_key": bar_key,
            "close": c,
        }

        # exits first
        evaluate_exit("STOCH_ENTRY", data)
        # then entry attempt
        try_entry()

        return jsonify({"ok": True, "event": "STOCH_ENTRY", "reason": reason}), 200

    # --- EMA EXIT (already working in your setup) ---
    if event == "EMA_EXIT":
        evaluate_exit("EMA_EXIT", data)
        return jsonify({"ok": True, "event": "EMA_EXIT"}), 200

    # --- BAR_CLOSE heartbeat (optional) ---
    if event == "BAR_CLOSE":
        return jsonify({"ok": True, "event": "BAR_CLOSE"}), 200

    log(f"IGNORED unknown event={event}")
    return jsonify({"ok": True, "ignored": True, "event": event}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    log(f"Starting server on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
