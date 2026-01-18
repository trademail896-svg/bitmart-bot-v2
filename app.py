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

# (BUFFER/REPLAY) minimal robustness for out-of-order alerts
PENDING_TTL_SEC = float(os.environ.get("BOT_PENDING_TTL_SEC") or "90")   # keep small: ~ up to 3x 5m bars is NOT needed; 90s is enough for order jitter
PENDING_MAX_PER_SYMBOL = int(os.environ.get("BOT_PENDING_MAX_PER_SYMBOL") or "3")  # minimal queue size

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

        # (BUFFER/REPLAY) pending signals keyed by bar_key (per symbol)
        # { "<bar_key>": {"event": "VECTOR"/"STOCH_SIGNAL", "payload": {...minimal...}, "ts": <epoch>} }
        "pending_by_bar_key": {},
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

def _pending_cleanup(s: Dict[str, Any]) -> None:
    pbk = s.get("pending_by_bar_key")
    if not isinstance(pbk, dict) or not pbk:
        return
    cutoff = now_ts() - float(PENDING_TTL_SEC)
    stale = []
    for k, v in pbk.items():
        ts = None
        if isinstance(v, dict):
            ts = safe_float(v.get("ts"))
        if ts is None or ts < cutoff:
            stale.append(k)
    for k in stale:
        try:
            del pbk[k]
        except Exception:
            pass

def _pending_put(s: Dict[str, Any], bk: str, event: str, payload_min: Dict[str, Any]) -> None:
    if not bk:
        return
    pbk = s.get("pending_by_bar_key")
    if not isinstance(pbk, dict):
        pbk = {}
        s["pending_by_bar_key"] = pbk

    _pending_cleanup(s)

    # enforce max size (drop oldest)
    if len(pbk) >= int(PENDING_MAX_PER_SYMBOL):
        # drop oldest by ts
        oldest_k = None
        oldest_ts = None
        for k, v in pbk.items():
            ts = None
            if isinstance(v, dict):
                ts = safe_float(v.get("ts"))
            if ts is None:
                ts = 0.0
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts
                oldest_k = k
        if oldest_k is not None:
            try:
                del pbk[oldest_k]
            except Exception:
                pass

    pbk[bk] = {"event": event, "payload": payload_min, "ts": now_ts()}
    print(f"PENDING STORED: bar_key={bk} event={event} pending_count={len(pbk)}")

def _pending_get(s: Dict[str, Any], bk: str) -> Optional[Dict[str, Any]]:
    pbk = s.get("pending_by_bar_key")
    if not isinstance(pbk, dict) or not bk:
        return None
    _pending_cleanup(s)
    v = pbk.get(bk)
    if isinstance(v, dict):
        return v
    return None

def _pending_del(s: Dict[str, Any], bk: str) -> None:
    pbk = s.get("pending_by_bar_key")
    if isinstance(pbk, dict) and bk in pbk:
        try:
            del pbk[bk]
        except Exception:
            pass

def _min_payload_for_pending(payload: Dict[str, Any], event: str) -> Dict[str, Any]:
    """
    Store only minimal fields needed for later decision + SL price.
    """
    p: Dict[str, Any] = {
        "event": event,
        "ticker": payload.get("ticker"),
        "tf": payload.get("tf"),
        "time": payload.get("time"),
        "bar_key": payload.get("bar_key"),
        "open": payload.get("open"),
        "close": payload.get("close"),
    }
    if event == "VECTOR":
        p["color"] = payload.get("color")
        p["side"] = payload.get("side")
    elif event == "STOCH_SIGNAL":
        p["signal"] = payload.get("signal")
    return p

# =========================
# BITMART SIGNING / REQUESTS
# =========================
def _bitmart_sign(ts_ms: str, memo: str, body_str: str) -> str:
    """
    BitMart Futures signature (works for demo + live):
      sign = HMAC_SHA256(secret, ts + "#" + memo + "#" + body)
    Notes:
      - body is "" for GET
      - body is compact JSON for POST
    """
    payload = f"{ts_ms}#{memo}#{body_str}"
    return hmac.new(BITMART_API_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

def bitmart_request(method: str, path: str, body: Optional[dict] = None) -> Tuple[int, dict]:
    if not BITMART_BASE_URL:
        return 500, {"code": -1, "message": "BITMART_BASE_URL missing"}
    if not BITMART_API_KEY or not BITMART_API_SECRET or not BITMART_MEMO:
        return 500, {"code": -1, "message": "BitMart API credentials missing (need BITMART_API_KEY, BITMART_API_SECRET, BITMART_MEMO)"}

    url = BITMART_BASE_URL.rstrip("/") + path
    ts_ms = str(int(time.time() * 1000))

    m = method.upper()
    if m == "GET":
        body_str = ""  # IMPORTANT for BitMart sign
    else:
        data = body or {}
        body_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    sign = _bitmart_sign(ts_ms, BITMART_MEMO, body_str)

    headers = {
        "Content-Type": "application/json",
        "X-BM-KEY": BITMART_API_KEY,
        "X-BM-SIGN": sign,
        "X-BM-TIMESTAMP": ts_ms,
        "X-BM-MEMO": BITMART_MEMO,
    }

    try:
        if m == "GET":
            r = requests.get(url, headers=headers, timeout=15)
        else:
            r = requests.request(m, url, headers=headers, data=body_str, timeout=15)

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
        "side": side,  # "LONG"/"SHORT"
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
        for k in ("positions", "position", "result"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []

def bitmart_get_position(symbol: str) -> Tuple[bool, Optional[str]]:
    """
    Returns: (in_position, side) where side is "LONG"/"SHORT" or None.
    Uses:
      - GET /contract/private/position-v2
      - fallback GET /contract/private/position
    """
    for path in ("/contract/private/position-v2", "/contract/private/position"):
        http, js = bitmart_request("GET", path, None)
        if http >= 400:
            continue
        if not isinstance(js, dict) or js.get("code") not in (1000, "1000"):
            continue

        plist = _extract_positions_list(js)
        if not plist:
            return False, None

        target = None
        for p in plist:
            if not isinstance(p, dict):
                continue
            psym = (p.get("symbol") or p.get("contract_symbol") or "").upper()
            if psym == symbol.upper():
                target = p
                break

        if not target:
            return False, None

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

        hold_side = (target.get("hold_side") or target.get("position_type") or "").upper()
        if hold_side in ("LONG", "1"):
            return True, "LONG"
        if hold_side in ("SHORT", "2"):
            return True, "SHORT"

        return True, None

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

        if side in ("LONG", "SHORT"):
            if (not before_in) or (before_side != side):
                print(f"RESYNC: {symbol} EXCHANGE={side} but STATE=({before_side}) -> set {side}")
            s["in_position"] = True
            s["side"] = side
            return

        if not before_in:
            print(f"RESYNC: {symbol} EXCHANGE=IN_POSITION but side unknown; STATE was FLAT -> set in_position=True (side unchanged)")
            s["in_position"] = True
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
        return True

    if s.get("pending_entry_bar_key") != bar_key:
        s["pending_entry_bar_key"] = bar_key
        s["pending_entry_first_ts"] = now_ts()
        s["pending_entry_color"] = color
        print(f"VECTOR ENTRY DEBOUNCE START: bar_key={bar_key} color={color}")
        return False

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

    if (payload.get("secret") or "").strip() != SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 403

    ticker = payload.get("ticker") or ""
    symbol = normalize_symbol(ticker)
    if not symbol:
        return jsonify({"ok": True, "ignored": "symbol"}), 200

    s = STATE[symbol]
    event = (payload.get("event") or "").strip().upper()
    bk = bar_key_from_payload(payload)

    upstash_get_bias(symbol)

    def _handle_non_ema_event(inner_payload: Dict[str, Any], from_replay: bool = False) -> Tuple[Dict[str, Any], int]:
        """
        Runs the exact same decision/execution path for non-EMA50_STATE events.
        from_replay=True prevents re-buffering loops.
        """
        inner_event = (inner_payload.get("event") or "").strip().upper()
        inner_bk = bar_key_from_payload(inner_payload)

        # VECTOR buffer updates
        if inner_event == "VECTOR":
            color = (inner_payload.get("color") or "").upper()
            if color in (LONG_COLOR, SHORT_COLOR):
                update_vector_buffer(s, inner_bk, color)

        # Decide action: EXIT > ENTRY
        action: Optional[str] = None
        action = should_exit(s, inner_event, inner_payload)
        if action is None:
            action = should_enter(s, inner_event, inner_payload)

        # If no action and regime missing: buffer/replay (only for entry triggers, not for replay calls)
        if action is None and (not from_replay) and s.get("regime") is None and inner_event in ("VECTOR", "STOCH_SIGNAL") and inner_bk:
            _pending_put(s, inner_bk, inner_event, _min_payload_for_pending(inner_payload, inner_event))
            return {"ok": True, "action": "NONE", "note": "buffered_pending_regime", "bar_key": inner_bk}, 200

        if action is None:
            return {"ok": True, "action": "NONE"}, 200

        # RESYNC before executing
        resync_state_with_exchange(symbol, s)

        if action in ("EXIT_LONG", "EXIT_SHORT"):
            if not s["in_position"] or (action == "EXIT_LONG" and s["side"] != "LONG") or (action == "EXIT_SHORT" and s["side"] != "SHORT"):
                print(f"RESYNC BLOCK EXIT: {symbol} action={action} state_in={s['in_position']} state_side={s['side']}")
                return {"ok": True, "action": "NONE", "note": "resync_block_exit"}, 200

        if action in ("ENTER_LONG", "ENTER_SHORT"):
            if s["in_position"]:
                print(f"RESYNC BLOCK ENTRY: {symbol} action={action} exchange/state shows in_position")
                return {"ok": True, "action": "NONE", "note": "resync_block_entry"}, 200

        # BAR_KEY GATE
        if inner_bk and s["last_action_bar_key"] == inner_bk:
            print(f"SKIP_DUPLICATE_BAR_KEY: {symbol} bar_key={inner_bk} incoming_event={inner_event} action={action}")
            return {"ok": True, "skipped": "duplicate_bar_key"}, 200

        # ENTRY debounce ONLY for VECTOR-triggered ENTRY
        if inner_event == "VECTOR" and action in ("ENTER_LONG", "ENTER_SHORT"):
            color = (inner_payload.get("color") or "").upper()
            if color in (LONG_COLOR, SHORT_COLOR):
                if not entry_allowed_by_vector_debounce(s, inner_bk, color):
                    return {"ok": True, "action": "NONE", "note": "vector_entry_debounce"}, 200

            final_color = s.get("pending_entry_color")
            if action == "ENTER_LONG" and final_color != LONG_COLOR:
                print(f"VECTOR ENTRY CANCEL: {symbol} bar_key={inner_bk} action={action} final_color={final_color}")
                return {"ok": True, "action": "NONE", "note": "vector_entry_color_flip"}, 200
            if action == "ENTER_SHORT" and final_color != SHORT_COLOR:
                print(f"VECTOR ENTRY CANCEL: {symbol} bar_key={inner_bk} action={action} final_color={final_color}")
                return {"ok": True, "action": "NONE", "note": "vector_entry_color_flip"}, 200

        # Execute action
        if action == "ENTER_LONG":
            bitmart_set_leverage(symbol)
            r = bitmart_open_market(symbol, "LONG")

            s["in_position"] = True
            s["side"] = "LONG"
            s["last_entry_bar_key"] = inner_bk

            entry_px = safe_float(inner_payload.get("close")) or safe_float(inner_payload.get("open")) or 0.0
            if entry_px > 0:
                bitmart_set_sl(symbol, "LONG", entry_px)

            s["last_action_bar_key"] = inner_bk
            s["last_action_type"] = "ENTRY"
            return {"ok": True, "action": "ENTER_LONG", "bitmart": r}, 200

        if action == "ENTER_SHORT":
            bitmart_set_leverage(symbol)
            r = bitmart_open_market(symbol, "SHORT")

            s["in_position"] = True
            s["side"] = "SHORT"
            s["last_entry_bar_key"] = inner_bk

            entry_px = safe_float(inner_payload.get("close")) or safe_float(inner_payload.get("open")) or 0.0
            if entry_px > 0:
                bitmart_set_sl(symbol, "SHORT", entry_px)

            s["last_action_bar_key"] = inner_bk
            s["last_action_type"] = "ENTRY"
            return {"ok": True, "action": "ENTER_SHORT", "bitmart": r}, 200

        if action == "EXIT_LONG":
            r = bitmart_close_market(symbol, "LONG")
            s["in_position"] = False
            s["side"] = None
            s["last_action_bar_key"] = inner_bk
            s["last_action_type"] = "EXIT"
            return {"ok": True, "action": "EXIT_LONG", "bitmart": r}, 200

        if action == "EXIT_SHORT":
            r = bitmart_close_market(symbol, "SHORT")
            s["in_position"] = False
            s["side"] = None
            s["last_action_bar_key"] = inner_bk
            s["last_action_type"] = "EXIT"
            return {"ok": True, "action": "EXIT_SHORT", "bitmart": r}, 200

        return {"ok": True, "action": "NONE"}, 200

    # EMA50_STATE updates regime only + (BUFFER/REPLAY) optional replay of same bar_key
    if event == "EMA50_STATE":
        st = (payload.get("state") or "").upper()
        if st in ("ABOVE", "BELOW"):
            s["regime"] = st

        # Try replay for SAME bar_key (safe)
        if bk:
            pending = _pending_get(s, bk)
            if pending and isinstance(pending.get("payload"), dict):
                replay_payload = dict(pending["payload"])
                _pending_del(s, bk)
                print(f"REPLAY TRIGGER: symbol={symbol} bar_key={bk} event={pending.get('event')}")
                replay_js, replay_code = _handle_non_ema_event(replay_payload, from_replay=True)
                return jsonify({
                    "ok": True,
                    "event": "EMA50_STATE",
                    "regime": s["regime"],
                    "replay": replay_js,
                }), 200

        return jsonify({"ok": True, "event": "EMA50_STATE", "regime": s["regime"]}), 200

    # All other events go through the normal path (with buffering if regime missing)
    js, code = _handle_non_ema_event(payload, from_replay=False)
    return jsonify(js), code

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
        "pending_ttl_sec": PENDING_TTL_SEC,
        "pending_max_per_symbol": PENDING_MAX_PER_SYMBOL,
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
                "pending_count": len(STATE[s].get("pending_by_bar_key") or {}),
            }
            for s in ALLOWED_SYMBOLS
        }
    }), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT") or "5000"))
