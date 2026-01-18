# app.py
from flask import Flask, request, jsonify
import os
import time
import hmac
import hashlib
import json
import math
import requests
from typing import Dict, Any, Optional, Tuple

app = Flask(__name__)

# =========================
# CONFIG
# =========================
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Vector colors (normalized by your TV alerts)
LONG_COLOR = "GREEN"
SHORT_COLOR = "RED"

SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# BitMart (Futures) - expected env vars
BITMART_BASE_URL = (os.environ.get("BITMART_BASE_URL") or "").strip()  # e.g. https://api-cloud.bitmart.com
BITMART_API_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_API_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_MEMO") or "").strip()

# Risk / leverage (already discussed: 25x)
LEVERAGE = int(os.environ.get("BOT_LEVERAGE") or "25")
OPEN_TYPE = (os.environ.get("BOT_OPEN_TYPE") or "isolated").strip()  # isolated/cross
NOTIONAL_USD_PER_TRADE = float(os.environ.get("BOT_NOTIONAL_USD_PER_TRADE") or "2500.0")

# SL config (unchanged here; keep whatever you already had; simple example)
SL_PCT = float(os.environ.get("BOT_SL_PCT") or "0.0025")  # 0.25% default (matches your logs)

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

        # Optional: keep last vector seen in this bar_key (for later step #2 if you want)
        "last_vector_bar_key": None,
        "last_vector_color": None,  # "GREEN"/"RED"
        "last_vector_ts": None,
    }
    for s in ALLOWED_SYMBOLS
}

# =========================
# UTILS
# =========================
def now_ts() -> int:
    return int(time.time())

def normalize_symbol(ticker: str) -> Optional[str]:
    """
    TradingView tickers may arrive like BTCUSDT.P; normalize to BTCUSDT.
    """
    if not ticker:
        return None
    t = ticker.strip().upper()
    if t.endswith(".P"):
        t = t[:-2]
    # Some feeds include exchange prefix; keep last part if needed
    if ":" in t:
        t = t.split(":")[-1]
    if t in ALLOWED_SYMBOLS:
        return t
    return None

def upstash_get_bias(symbol: str) -> Optional[str]:
    """
    Bias storage from previous versions; keep for compatibility.
    Key example: tvbotv2:bias:BTCUSDT -> "LONG"/"SHORT"/"NONE"
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
# BITMART SIGNING / REQUESTS (minimal, enough for open/close/SL)
# =========================
def _bitmart_sign(ts_ms: str, method: str, path: str, body: str) -> str:
    """
    BitMart signature (common pattern): sign = HMAC_SHA256(secret, ts + method + path + body)
    NOTE: confirm with your existing implementation if you already have it.
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
        return r.status_code, r.json()
    except Exception as e:
        return 500, {"code": -2, "message": f"request error: {e}"}

def bitmart_set_leverage(symbol: str) -> Dict[str, Any]:
    # Path here is illustrative; use your proven endpoint if different.
    path = "/contract/private/submit-leverage"
    body = {"symbol": symbol, "leverage": str(LEVERAGE), "open_type": OPEN_TYPE}
    http, js = bitmart_request("POST", path, body)
    print(f"BITMART SUBMIT LEVERAGE: {symbol} {{'http': {http}, 'json': {js}}}")
    return {"http": http, "json": js}

def bitmart_open_market(symbol: str, side: str) -> Dict[str, Any]:
    # side: "LONG" or "SHORT"
    path = "/contract/private/submit-order"
    # BitMart often uses "side": 1=buy(open long), 2=sell(open short) or similar; adapt if needed.
    # We'll keep "side" field generic in payload; your existing code may differ.
    body = {
        "symbol": symbol,
        "type": "market",
        "open_type": OPEN_TYPE,
        "leverage": str(LEVERAGE),
        "size": str(_calc_size_from_notional(symbol)),  # simplistic placeholder
        "side": side,  # keep as string in this scaffold
    }
    http, js = bitmart_request("POST", path, body)
    print(f"BITMART OPEN: {symbol} {side} {{'http': {http}, 'json': {js}}}")
    return {"http": http, "json": js}

def bitmart_close_market(symbol: str, side: str) -> Dict[str, Any]:
    # side: "LONG" means close long; "SHORT" means close short
    path = "/contract/private/submit-order"
    body = {
        "symbol": symbol,
        "type": "market",
        "open_type": OPEN_TYPE,
        "leverage": str(LEVERAGE),
        "size": "0",  # often means close all; your API may require position size. Replace as needed.
        "side": f"CLOSE_{side}",
    }
    http, js = bitmart_request("POST", path, body)
    print(f"BITMART CLOSE: {symbol} {side} {{'http': {http}, 'json': {js}}}")
    return {"http": http, "json": js}

def bitmart_set_sl(symbol: str, side: str, entry_price: float) -> Dict[str, Any]:
    """
    Simple % SL, then "SL SAFE" min distance + rounding.
    This mirrors the log style you showed; keep as-is for now.
    """
    proposed = entry_price * (1.0 - SL_PCT) if side == "LONG" else entry_price * (1.0 + SL_PCT)

    # Placeholder min distance: if you already compute from exchange filters, keep your original method.
    # Here we emulate the behavior: enforce some minimum distance.
    min_dist = max(entry_price * 0.0005, 0.0)  # 0.05% placeholder
    if side == "LONG":
        final = min(proposed, entry_price - min_dist)
    else:
        final = max(proposed, entry_price + min_dist)

    # Round to 0.1 as a placeholder (tick size differs per symbol!)
    final_rounded = math.floor(final * 10) / 10.0 if side == "LONG" else math.ceil(final * 10) / 10.0

    print(f"SL SAFE: {symbol} {{'side': '{side}', 'entry_price': {entry_price}, 'proposed': {round(proposed, 5)}, 'min_dist': {round(min_dist, 6)}, 'final': {final_rounded}}}")

    # Place SL order (illustrative path)
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

def _calc_size_from_notional(symbol: str) -> float:
    """
    Placeholder: replace with your existing sizing logic.
    In your current bot you likely size by notional USDT.
    """
    # Without mark price access here, return a dummy.
    return 1.0

# =========================
# CORE DECISION HELPERS (V3.2)
# =========================
def should_exit(s: Dict[str, Any], event: str, payload: Dict[str, Any]) -> Optional[str]:
    """
    Return "EXIT_LONG"/"EXIT_SHORT" or None
    """
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
    """
    Return "ENTER_LONG"/"ENTER_SHORT" or None
    """
    if s["in_position"]:
        return None

    regime = s.get("regime")  # "ABOVE"/"BELOW"/None
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

    # Keep compatibility with your Upstash bias reads
    upstash_get_bias(symbol)

    # -------------------------
    # Update regime (EMA50_STATE) - NO ACTION GATE HERE because it's not a trade action
    # -------------------------
    if event == "EMA50_STATE":
        st = (payload.get("state") or "").upper()
        if st in ("ABOVE", "BELOW"):
            s["regime"] = st
        return jsonify({"ok": True, "event": "EMA50_STATE", "regime": s["regime"]}), 200

    # -------------------------
    # BAR_KEY GATE (THIS IS THE ONLY MOD YOU ASKED FOR)
    # We only gate TRADE ACTIONS (ENTRY/EXIT/SL). Non-action messages should not lock.
    # Here VECTOR and STOCH_SIGNAL can lead to actions, so we gate on execution.
    # -------------------------

    # 1) Decide action based on priority: SL > EXIT > ENTRY
    # NOTE: SL triggers would normally come from exchange fill/stop events; not implemented here.
    action: Optional[str] = None

    # EXIT
    action = should_exit(s, event, payload)
    if action is None:
        # ENTRY
        action = should_enter(s, event, payload)

    # 2) If no action, do nothing (and DO NOT set last_action_bar_key)
    if action is None:
        return jsonify({"ok": True, "action": "NONE"}), 200

    # 3) If we are about to execute an action, enforce 1 action max per bar_key
    if bk and s["last_action_bar_key"] == bk:
        print(f"SKIP_DUPLICATE_BAR_KEY: {symbol} bar_key={bk} incoming_event={event} action={action}")
        return jsonify({"ok": True, "skipped": "duplicate_bar_key"}), 200

    # 4) Execute action (ENTRY/EXIT)
    if action == "ENTER_LONG":
        # Final guards
        if s["in_position"]:
            return jsonify({"ok": True, "action": "NONE", "note": "already_in_position"}), 200

        bitmart_set_leverage(symbol)
        r = bitmart_open_market(symbol, "LONG")

        # Mark state optimistically; in production you might confirm fill/position
        s["in_position"] = True
        s["side"] = "LONG"
        s["last_entry_bar_key"] = bk

        # SL (unchanged behavior)
        entry_px = safe_float(payload.get("close")) or safe_float(payload.get("open")) or 0.0
        if entry_px > 0:
            bitmart_set_sl(symbol, "LONG", entry_px)

        # Commit bar_key gate ONLY after action executed
        s["last_action_bar_key"] = bk
        s["last_action_type"] = "ENTRY"
        return jsonify({"ok": True, "action": "ENTER_LONG", "bitmart": r}), 200

    if action == "ENTER_SHORT":
        if s["in_position"]:
            return jsonify({"ok": True, "action": "NONE", "note": "already_in_position"}), 200

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
        if not s["in_position"] or s["side"] != "LONG":
            return jsonify({"ok": True, "action": "NONE", "note": "not_in_long"}), 200

        r = bitmart_close_market(symbol, "LONG")
        s["in_position"] = False
        s["side"] = None
        s["last_action_bar_key"] = bk
        s["last_action_type"] = "EXIT"
        return jsonify({"ok": True, "action": "EXIT_LONG", "bitmart": r}), 200

    if action == "EXIT_SHORT":
        if not s["in_position"] or s["side"] != "SHORT":
            return jsonify({"ok": True, "action": "NONE", "note": "not_in_short"}), 200

        r = bitmart_close_market(symbol, "SHORT")
        s["in_position"] = False
        s["side"] = None
        s["last_action_bar_key"] = bk
        s["last_action_type"] = "EXIT"
        return jsonify({"ok": True, "action": "EXIT_SHORT", "bitmart": r}), 200

    # Fallback
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
        "allowed_symbols": sorted(list(ALLOWED_SYMBOLS)),
        "state": {
            s: {
                "in_position": STATE[s]["in_position"],
                "side": STATE[s]["side"],
                "regime": STATE[s]["regime"],
                "last_action_bar_key": STATE[s]["last_action_bar_key"],
                "last_action_type": STATE[s]["last_action_type"],
            }
            for s in ALLOWED_SYMBOLS
        }
    }), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT") or "5000"))
