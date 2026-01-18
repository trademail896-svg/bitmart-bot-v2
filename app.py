# app.py
from flask import Flask, request, jsonify
import os
import time
import hmac
import hashlib
import json
import math
import requests
from typing import Dict, Any, Optional, Tuple, List

app = Flask(__name__)

# =========================
# CONFIG
# =========================
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Vector colors (normalized by your TV alerts)
LONG_COLOR = "GREEN"
SHORT_COLOR = "RED"

SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# BitMart (Futures)
BITMART_BASE_URL = (os.environ.get("BITMART_BASE_URL") or "").strip()  # e.g. https://api-cloud-v2.bitmart.com or demo-api-cloud-v2...
BITMART_API_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_API_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_MEMO") or "").strip()

# Risk / leverage
LEVERAGE = int(os.environ.get("BOT_LEVERAGE") or "25")
OPEN_TYPE = (os.environ.get("BOT_OPEN_TYPE") or "isolated").strip()  # isolated/cross
NOTIONAL_USD_PER_TRADE = float(os.environ.get("BOT_NOTIONAL_USD_PER_TRADE") or "2500.0")

# SL config (UNCHANGED)
SL_PCT = float(os.environ.get("BOT_SL_PCT") or "0.0025")  # 0.25% default (matches your logs)

# VECTOR entry debounce (NEW, for buffer stability)
# Purpose: avoid entering on the first intrabar color if it flips immediately.
VECTOR_ENTRY_DEBOUNCE_SEC = float(os.environ.get("BOT_VECTOR_ENTRY_DEBOUNCE_SEC") or "3.0")

# Resync toggle (NEW)
RESYNC_BEFORE_TRADE = (os.environ.get("BOT_RESYNC_BEFORE_TRADE") or "1").strip() == "1"

# Upstash (optional)
UPSTASH_REDIS_REST_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").strip()
UPSTASH_REDIS_REST_TOKEN = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
UPSTASH_PREFIX = (os.environ.get("UPSTASH_PREFIX") or "tvbotv2").strip()

# =========================
# STATE (per symbol)
# =========================
STATE: Dict[str, Dict[str, Any]] = {
    s: {
        "in_position": False,
        "side": None,               # "LONG"/"SHORT"
        "last_entry_bar_key": None,

        # Regime from EMA50_STATE
        "regime": None,             # "ABOVE"/"BELOW"/None

        # Bar-key gate: 1 ACTION max per bar_key
        "last_action_bar_key": None,
        "last_action_type": None,   # "ENTRY"/"EXIT"/"SL" (debug)

        # (2) Vector buffer (last vector seen for a bar_key)
        "last_vector_bar_key": None,
        "last_vector_color": None,   # "GREEN"/"RED"
        "last_vector_ts": None,      # epoch seconds

        # (2) Pending entry vector debounce window (per bar_key)
        "pending_entry_bar_key": None,
        "pending_entry_first_ts": None,
        "pending_entry_color": None,  # "GREEN"/"RED"
    }
    for s in ALLOWED_SYMBOLS
}

# =========================
# UTILS
# =========================
def now_ts() -> float:
    return time.time()

def normalize_symbol(ticker: str) -> Optional[str]:
    """
    TradingView tickers may arrive like BTCUSDT.P; normalize to BTCUSDT.
    """
    if not ticker:
        return None
    t = ticker.strip().upper()
    if t.endswith(".P"):
        t = t[:-2]
    if ":" in t:
        t = t.split(":")[-1]
    if t in ALLOWED_SYMBOLS:
        return t
    return None

def upstash_get_bias(symbol: str) -> Optional[str]:
    """
    Compatibility: prints existing bias key if Upstash enabled.
    """
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    key = f"{UPSTASH_PREFIX}:bias:{symbol}"
    try:
        url = f"{UPSTASH_REDIS_REST_URL}/get/{key}"
        headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
        r = requests.get(url, headers=headers, timeout=10)
        js = r.json()
        val = js.get("result")
        print(f"UPSTASH GET: {{'symbol': '{symbol}', 'key': '{key}', 'ok': True, 'val': {val!r}, 'dbg': {{'http': {r.status_code}, 'json': {js}}}}}")
        return val
    except Exception as e:
        print(f"UPSTASH GET ERROR: {symbol} -> {e}")
        return None

def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def bar_key_from_payload(p: Dict[str, Any]) -> Optional[str]:
    bk = p.get("bar_key")
    if isinstance(bk, str) and bk.strip():
        return bk.strip()
    return None

# =========================
# BITMART SIGNING / REQUESTS
# =========================
def _bitmart_sign(ts_ms: str, method: str, path: str, body: str) -> str:
    """
    Common BitMart signature pattern: HMAC_SHA256(secret, ts + method + path + body)
    """
    payload = f"{ts_ms}{method.upper()}{path}{body}"
    return hmac.new(BITMART_API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

def bitmart_request(method: str, path: str, body: Optional[dict] = None) -> Tuple[int, dict]:
    if not BITMART_BASE_URL:
        return 500, {"code": -1, "message": "BITMART_BASE_URL missing"}
    if not BITMART_API_KEY or not BITMART_API_SECRET or not BITMART_MEMO:
        return 500, {"code": -1, "message": "BitMart API credentials missing"}

    url = BITMART_BASE_URL.rstrip("/") + path
    ts_ms = str(int(time.time() * 1000))
    data = body or {}
    body_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    sign = _bitmart_sign(ts_ms, method, path, body_str)
    headers = {
        "Content-Type": "application/json",
        "X-BM-KEY": BITMART_API_KEY,
        "X-BM-SIGN": sign,
        "X-BM-TIMESTAMP": ts_ms,
        "X-BM-MEMO": BITMART_MEMO,
    }

    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, timeout=15)
        else:
            r = requests.request(method.upper(), url, headers=headers, data=body_str, timeout=15)
        try:
            js = r.json()
        except Exception:
            js = {"code": -3, "message": "non-json response", "text": r.text[:500]}
        return r.status_code, js
    except Exception as e:
        return 500, {"code": -2, "message": f"request error: {e}"}

def bitmart_set_leverage(symbol: str) -> Dict[str, Any]:
    path = "/contract/private/submit-leverage"
    body = {"symbol": symbol, "leverage": str(LEVERAGE), "open_type": OPEN_TYPE}
    http, js = bitmart_request("POST", path, body)
    print(f"BITMART SUBMIT LEVERAGE: {symbol} {{'http': {http}, 'json': {js}}}")
    return {"http": http, "json": js}

def _calc_size_from_notional(symbol: str) -> float:
    # Placeholder sizing
    return 1.0

def bitmart_open_market(symbol: str, side: str) -> Dict[str, Any]:
    path = "/contract/private/submit-order"
    body = {
        "symbol": symbol,
        "type": "market",
        "open_type": OPEN_TYPE,
        "leverage": str(LEVERAGE),
        "size": str(_calc_size_from_notional(symbol)),
        "side": side,  # "LONG"/"SHORT" (keep consistent with your previous implementation)
    }
    http, js = bitmart_request("POST", path, body)
    print(f"BITMART OPEN: {symbol} {side} {{'http': {http}, 'json': {js}}}")
    return {"http": http, "json": js}

def bitmart_close_market(symbol: str, side: str) -> Dict[str, Any]:
    path = "/contract/private/submit-order"
    body = {
        "symbol": symbol,
        "type": "market",
        "open_type": OPEN_TYPE,
        "leverage": str(LEVERAGE),
        "size": "0",  # close-all placeholder; keep your proven method if different
        "side": f"CLOSE_{side}",
    }
    http, js = bitmart_request("POST", path, body)
    print(f"BITMART CLOSE: {symbol} {side} {{'http': {http}, 'json': {js}}}")
    return {"http": http, "json": js}

def bitmart_set_sl(symbol: str, side: str, entry_price: float) -> Dict[str, Any]:
    """
    UNCHANGED behavior (simple % SL + safe min distance + rounding)
    """
    proposed = entry_price * (1.0 - SL_PCT) if side == "LONG" else entry_price * (1.0 + SL_PCT)

    # Placeholder min distance (keep your original if you had exchange-derived values)
    min_dist = max(entry_price * 0.0005, 0.0)

    if side == "LONG":
        final = min(proposed, entry_price - min_dist)
        final_rounded = math.floor(final * 10) / 10.0
    else:
        final = max(proposed, entry_price + min_dist)
        final_rounded = math.ceil(final * 10) / 10.0

    print(f"SL SAFE: {symbol} {{'side': '{side}', 'entry_price': {entry_price}, 'proposed': {round(proposed, 5)}, 'min_dist': {round(min_dist, 6)}, 'final': {final_rounded}}}")

    path = "/contract/private/submit-plan-order"
    body = {
        "symbol": symbol,
        "trigger_price": str(final_rounded),
        "plan_type": "loss_plan",
        "side": side,
        "open_type": OPEN_TYPE,
    }
    http, js = bitmart_request("POST", path, body)
    print(f"BITMART SL: {symbol} {side} {{'http': {http}, 'json': {js}}}")
    return {"http": http, "json": js}

# =========================
# (3) RESYNC HELPERS
# =========================
def _extract_positions_list(js: dict) -> List[dict]:
    """
    BitMart returns data as [] or list of position objects in many cases.
    Be defensive.
    """
    data = js.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # sometimes position list nested
        for k in ("positions", "position", "result"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []

def bitmart_get_position(symbol: str) -> Tuple[bool, Optional[str]]:
    """
    Returns: (in_position, side) where side is "LONG"/"SHORT" or None.
    Uses documented endpoints:
      - GET /contract/private/position-v2 (KEYED)
      - fallback GET /contract/private/position
    """
    # primary: position-v2 (KEYED)
    for path in ("/contract/private/position-v2", "/contract/private/position"):
        http, js = bitmart_request("GET", path, None)
        if http >= 400:
            continue
        if not isinstance(js, dict) or js.get("code") not in (1000, "1000"):
            continue

        plist = _extract_positions_list(js)

        # If empty => flat
        if not plist:
            return False, None

        # Find symbol entry
        target = None
        for p in plist:
            if not isinstance(p, dict):
                continue
            psym = (p.get("symbol") or p.get("contract_symbol") or "").upper()
            if psym == symbol.upper():
                target = p
                break

        if not target:
            # No matching symbol => flat for our symbol
            return False, None

        # Derive side from common fields
        # Examples seen in logs: current_amount; sometimes can be negative or separate fields.
        amt = safe_float(target.get("current_amount") or target.get("position_amt") or target.get("amount"))
        if amt is not None:
            if amt > 0:
                return True, "LONG"
            if amt < 0:
                return True, "SHORT"

        side_val = (target.get("side") or target.get("position_side") or "").upper()
        if side_val in ("LONG", "BUY", "1"):
            return True, "LONG"
        if side_val in ("SHORT", "SELL", "2"):
            return True, "SHORT"

        # Another pattern: "hold_side" or "position_type"
        hold_side = (target.get("hold_side") or target.get("position_type") or "").upper()
        if hold_side in ("LONG", "1"):
            return True, "LONG"
        if hold_side in ("SHORT", "2"):
            return True, "SHORT"

        # If we can't determine but entry exists, treat as "in position unknown"
        return True, None

    # If all failed, don't change state
    return False, None

def resync_state_with_exchange(symbol: str, s: Dict[str, Any]) -> None:
    if not RESYNC_BEFORE_TRADE:
        return
    try:
        in_pos, side = bitmart_get_position(symbol)
        before_in = bool(s.get("in_position"))
        before_side = s.get("side")

        if not in_pos:
            if before_in:
                print(f"RESYNC: {symbol} EXCHANGE=FLAT but STATE=IN({before_side}) -> set FLAT")
            s["in_position"] = False
            s["side"] = None
            return

        # in_pos True
        if side in ("LONG", "SHORT"):
            if (not before_in) or (before_side != side):
                print(f"RESYNC: {symbol} EXCHANGE={side} but STATE=({before_side}) -> set {side}")
            s["in_position"] = True
            s["side"] = side
            return

        # side unknown
        if not before_in:
            print(f"RESYNC: {symbol} EXCHANGE=IN_POSITION but side unknown; STATE was FLAT -> set in_position=True (side unchanged)")
            s["in_position"] = True
            # keep side as-is (None)
    except Exception as e:
        print(f"RESYNC ERROR: {symbol} -> {e}")

# =========================
# CORE DECISION HELPERS (V3.2)
# =========================
def should_exit(s: Dict[str, Any], event: str, payload: Dict[str, Any]) -> Optional[str]:
    if not s["in_position"] or not s["side"]:
        return None
    side = s["side"]

    if event == "VECTOR":
        color = (payload.get("color") or "").upper()
        if side == "LONG" and color == SHORT_COLOR:
            return "EXIT_LONG"
        if side == "SHORT" and color == LONG_COLOR:
            return "EXIT_SHORT"

    if event == "STOCH_SIGNAL":
        sig = (payload.get("signal") or "").upper()
        if side == "LONG" and sig == "HD":
            return "EXIT_LONG"
        if side == "SHORT" and sig == "LD":
            return "EXIT_SHORT"

    return None

def should_enter(s: Dict[str, Any], event: str, payload: Dict[str, Any]) -> Optional[str]:
    if s["in_position"]:
        return None

    regime = s.get("regime")
    if regime not in ("ABOVE", "BELOW"):
        return None

    if event == "VECTOR":
        color = (payload.get("color") or "").upper()
        if regime == "ABOVE" and color == LONG_COLOR:
            return "ENTER_LONG"
        if regime == "BELOW" and color == SHORT_COLOR:
            return "ENTER_SHORT"

    if event == "STOCH_SIGNAL":
        sig = (payload.get("signal") or "").upper()
        if regime == "ABOVE" and sig == "LD":
            return "ENTER_LONG"
        if regime == "BELOW" and sig == "HD":
            return "ENTER_SHORT"

    return None

# =========================
# (2) VECTOR BUFFER + DEBOUNCE (ENTRY ONLY)
# =========================
def update_vector_buffer(s: Dict[str, Any], bar_key: Optional[str], color: str) -> None:
    s["last_vector_bar_key"] = bar_key
    s["last_vector_color"] = color
    s["last_vector_ts"] = now_ts()

def entry_allowed_by_vector_debounce(s: Dict[str, Any], bar_key: Optional[str], color: str) -> bool:
    """
    Only used for ENTRY on VECTOR.
    EXIT stays immediate.
    """
    if not bar_key:
        # no bar_key => cannot debounce reliably; allow
        return True

    # new bar_key => start debounce window
    if s.get("pending_entry_bar_key") != bar_key:
        s["pending_entry_bar_key"] = bar_key
        s["pending_entry_first_ts"] = now_ts()
        s["pending_entry_color"] = color
        print(f"VECTOR ENTRY DEBOUNCE START: bar_key={bar_key} color={color}")
        return False

    # same bar_key => update last color and check elapsed
    s["pending_entry_color"] = color
    first_ts = float(s.get("pending_entry_first_ts") or now_ts())
    elapsed = now_ts() - first_ts

    if elapsed >= VECTOR_ENTRY_DEBOUNCE_SEC:
        print(f"VECTOR ENTRY DEBOUNCE PASS: bar_key={bar_key} elapsed={round(elapsed,2)}s final_color={color}")
        return True

    print(f"VECTOR ENTRY DEBOUNCE HOLD: bar_key={bar_key} elapsed={round(elapsed,2)}s color={color}")
    return False

# =========================
# WEBHOOK
# =========================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"ok": False, "error": "invalid json"}), 400

    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "payload must be object"}), 400

    print(f"ALERTE: {payload}")

    # Secret check
    if (payload.get("secret") or "").strip() != SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 403

    ticker = payload.get("ticker") or ""
    symbol = normalize_symbol(ticker)
    if not symbol:
        return jsonify({"ok": True, "ignored": "symbol"}), 200

    s = STATE[symbol]
    event = (payload.get("event") or "").strip().upper()
    bk = bar_key_from_payload(payload)

    # Compatibility debug
    upstash_get_bias(symbol)

    # -------------------------
    # EMA50_STATE updates regime only (no trade action / no gate)
    # -------------------------
    if event == "EMA50_STATE":
        st = (payload.get("state") or "").upper()
        if st in ("ABOVE", "BELOW"):
            s["regime"] = st
        return jsonify({"ok": True, "event": "EMA50_STATE", "regime": s["regime"]}), 200

    # -------------------------
    # (2) VECTOR BUFFER always updates on VECTOR
    # -------------------------
    if event == "VECTOR":
        color = (payload.get("color") or "").upper()
        if color in (LONG_COLOR, SHORT_COLOR):
            update_vector_buffer(s, bk, color)

    # -------------------------
    # Decide action based on priority: SL > EXIT > ENTRY
    # (SL events not wired here; keeping original behavior)
    # -------------------------
    action: Optional[str] = None

    # EXIT first
    action = should_exit(s, event, payload)

    # ENTRY second
    if action is None:
        action = should_enter(s, event, payload)

    # If no action => do nothing and do NOT set last_action_bar_key
    if action is None:
        return jsonify({"ok": True, "action": "NONE"}), 200

    # -------------------------
    # (3) RESYNC before executing ENTRY/EXIT
    # -------------------------
    resync_state_with_exchange(symbol, s)

    # Re-check validity after resync
    if action in ("EXIT_LONG", "EXIT_SHORT"):
        if not s["in_position"] or (action == "EXIT_LONG" and s["side"] != "LONG") or (action == "EXIT_SHORT" and s["side"] != "SHORT"):
            print(f"RESYNC BLOCK EXIT: {symbol} action={action} state_in={s['in_position']} state_side={s['side']}")
            return jsonify({"ok": True, "action": "NONE", "note": "resync_block_exit"}), 200

    if action in ("ENTER_LONG", "ENTER_SHORT"):
        if s["in_position"]:
            print(f"RESYNC BLOCK ENTRY: {symbol} action={action} exchange/state shows in_position")
            return jsonify({"ok": True, "action": "NONE", "note": "resync_block_entry"}), 200

    # -------------------------
    # BAR_KEY GATE: 1 ACTION max per bar_key (trade actions only)
    # -------------------------
    if bk and s["last_action_bar_key"] == bk:
        print(f"SKIP_DUPLICATE_BAR_KEY: {symbol} bar_key={bk} incoming_event={event} action={action}")
        return jsonify({"ok": True, "skipped": "duplicate_bar_key"}), 200

    # -------------------------
    # (2) ENTRY debounce ONLY for VECTOR-triggered ENTRY
    # -------------------------
    if event == "VECTOR" and action in ("ENTER_LONG", "ENTER_SHORT"):
        color = (payload.get("color") or "").upper()
        if color in (LONG_COLOR, SHORT_COLOR):
            if not entry_allowed_by_vector_debounce(s, bk, color):
                # no trade action executed => do NOT set last_action_bar_key
                return jsonify({"ok": True, "action": "NONE", "note": "vector_entry_debounce"}), 200

        # Safety: if during debounce the final color contradicts action, cancel
        final_color = s.get("pending_entry_color")
        if action == "ENTER_LONG" and final_color != LONG_COLOR:
            print(f"VECTOR ENTRY CANCEL: {symbol} bar_key={bk} action={action} final_color={final_color}")
            return jsonify({"ok": True, "action": "NONE", "note": "vector_entry_color_flip"}), 200
        if action == "ENTER_SHORT" and final_color != SHORT_COLOR:
            print(f"VECTOR ENTRY CANCEL: {symbol} bar_key={bk} action={action} final_color={final_color}")
            return jsonify({"ok": True, "action": "NONE", "note": "vector_entry_color_flip"}), 200

    # -------------------------
    # Execute action (ENTRY/EXIT)
    # -------------------------
    if action == "ENTER_LONG":
        bitmart_set_leverage(symbol)
        r = bitmart_open_market(symbol, "LONG")

        s["in_position"] = True
        s["side"] = "LONG"
        s["last_entry_bar_key"] = bk

        entry_px = safe_float(payload.get("close")) or safe_float(payload.get("open")) or 0.0
        if entry_px > 0:
            bitmart_set_sl(symbol, "LONG", entry_px)

        s["last_action_bar_key"] = bk
        s["last_action_type"] = "ENTRY"
        return jsonify({"ok": True, "action": "ENTER_LONG", "bitmart": r}), 200

    if action == "ENTER_SHORT":
        bitmart_set_leverage(symbol)
        r = bitmart_open_market(symbol, "SHORT")

        s["in_position"] = True
        s["side"] = "SHORT"
        s["last_entry_bar_key"] = bk

        entry_px = safe_float(payload.get("close")) or safe_float(payload.get("open")) or 0.0
        if entry_px > 0:
            bitmart_set_sl(symbol, "SHORT", entry_px)

        s["last_action_bar_key"] = bk
        s["last_action_type"] = "ENTRY"
        return jsonify({"ok": True, "action": "ENTER_SHORT", "bitmart": r}), 200

    if action == "EXIT_LONG":
        r = bitmart_close_market(symbol, "LONG")
        s["in_position"] = False
        s["side"] = None
        s["last_action_bar_key"] = bk
        s["last_action_type"] = "EXIT"
        return jsonify({"ok": True, "action": "EXIT_LONG", "bitmart": r}), 200

    if action == "EXIT_SHORT":
        r = bitmart_close_market(symbol, "SHORT")
        s["in_position"] = False
        s["side"] = None
        s["last_action_bar_key"] = bk
        s["last_action_type"] = "EXIT"
        return jsonify({"ok": True, "action": "EXIT_SHORT", "bitmart": r}), 200

    return jsonify({"ok": True, "action": "NONE"}), 200

# =========================
# HEALTH
# =========================
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "bot": "TV_BOT_DEMO_2026_V2",
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "vector_entry_debounce_sec": VECTOR_ENTRY_DEBOUNCE_SEC,
        "resync_before_trade": RESYNC_BEFORE_TRADE,
        "allowed_symbols": sorted(list(ALLOWED_SYMBOLS)),
        "state": {
            s: {
                "in_position": STATE[s]["in_position"],
                "side": STATE[s]["side"],
                "regime": STATE[s]["regime"],
                "last_action_bar_key": STATE[s]["last_action_bar_key"],
                "last_action_type": STATE[s]["last_action_type"],
                "last_vector_bar_key": STATE[s]["last_vector_bar_key"],
                "last_vector_color": STATE[s]["last_vector_color"],
            }
            for s in ALLOWED_SYMBOLS
        }
    }), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT") or "5000"))
