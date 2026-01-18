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
BITMART_BASE_URL = (os.environ.get("BITMART_BASE_URL") or "").strip()  # demo/live base url
BITMART_API_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_API_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_MEMO") or "").strip()

# Risk / leverage
LEVERAGE = int(os.environ.get("BOT_LEVERAGE") or "25")
OPEN_TYPE = (os.environ.get("BOT_OPEN_TYPE") or "isolated").strip()  # isolated/cross
NOTIONAL_USD_PER_TRADE = float(os.environ.get("BOT_NOTIONAL_USD_PER_TRADE") or "2500.0")

# SL config
SL_PCT = float(os.environ.get("BOT_SL_PCT") or "0.0025")  # 0.25% default

# VECTOR entry debounce
VECTOR_ENTRY_DEBOUNCE_SEC = float(os.environ.get("BOT_VECTOR_ENTRY_DEBOUNCE_SEC") or "3.0")

# Resync toggle
RESYNC_BEFORE_TRADE = (os.environ.get("BOT_RESYNC_BEFORE_TRADE") or "1").strip() == "1"

# Upstash (optional)
UPSTASH_REDIS_REST_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").strip()
UPSTASH_REDIS_REST_TOKEN = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
UPSTASH_PREFIX = (os.environ.get("UPSTASH_PREFIX") or "tvbotv2").strip()

# Post-entry SL verify/fallback (NEW)
POST_ENTRY_VERIFY_SL = (os.environ.get("BOT_POST_ENTRY_VERIFY_SL") or "1").strip() == "1"
POST_ENTRY_VERIFY_LIMIT = int(os.environ.get("BOT_POST_ENTRY_VERIFY_LIMIT") or "10")

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
        "last_action_type": None,   # "ENTRY"/"EXIT" (debug)

        # Vector buffer (last vector seen for a bar_key)
        "last_vector_bar_key": None,
        "last_vector_color": None,   # "GREEN"/"RED"
        "last_vector_ts": None,      # epoch seconds

        # Pending entry vector debounce window (per bar_key)
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

def _mk_client_order_id(symbol: str) -> str:
    return f"TVBOT_{symbol}_{int(time.time()*1000)}"[:32]

# =========================
# BITMART SIGNING / REQUESTS
# =========================
def _bitmart_sign(ts_ms: str, memo: str, body_str: str) -> str:
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
        body_str = ""
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

def _calc_size_from_notional(symbol: str) -> int:
    # Placeholder sizing (must be int)
    return 1

def _calc_sl_price(side: str, ref_price: float) -> Optional[str]:
    """
    Returns SL price as string.
    Same % logic + safe min distance + rounding as before.
    """
    if ref_price <= 0:
        return None

    proposed = ref_price * (1.0 - SL_PCT) if side == "LONG" else ref_price * (1.0 + SL_PCT)
    min_dist = max(ref_price * 0.0005, 0.0)

    if side == "LONG":
        final = min(proposed, ref_price - min_dist)
        final_rounded = math.floor(final * 10) / 10.0
    else:
        final = max(proposed, ref_price + min_dist)
        final_rounded = math.ceil(final * 10) / 10.0

    print(f"SL SAFE: {{'side': '{side}', 'ref_price': {ref_price}, 'proposed': {round(proposed, 5)}, 'min_dist': {round(min_dist, 6)}, 'final': {final_rounded}}}")
    return str(final_rounded)

def bitmart_open_market(symbol: str, side: str, sl_price: Optional[str]) -> Dict[str, Any]:
    """
    submit-order:
      side int (one-way):
        1=open long (buy)
        4=open short (sell)
      size must be int
    preset SL included if provided.
    """
    path = "/contract/private/submit-order"

    side_int = 1 if side == "LONG" else 4
    size_int = int(_calc_size_from_notional(symbol))

    body: Dict[str, Any] = {
        "symbol": symbol,
        "client_order_id": _mk_client_order_id(symbol),
        "type": "market",
        "side": side_int,
        "mode": 1,
        "leverage": str(LEVERAGE),
        "open_type": OPEN_TYPE,
        "size": size_int,
    }

    if sl_price:
        body["preset_stop_loss_price"] = sl_price
        body["preset_stop_loss_price_type"] = 1  # last_price

    http, js = bitmart_request("POST", path, body)
    print(f"BITMART OPEN: {symbol} {side} {{'http': {http}, 'json': {js}}} body={body}")
    return {"http": http, "json": js, "body": body}

def bitmart_close_market(symbol: str, side: str) -> Dict[str, Any]:
    """
    Minimal close. If you want perfect close sizing later, we can query position size.
    """
    path = "/contract/private/submit-order"

    # one-way close:
    # 3 = sell reduce-only -> close long
    # 2 = buy reduce-only  -> close short
    side_int = 3 if side == "LONG" else 2
    size_int = 1

    body: Dict[str, Any] = {
        "symbol": symbol,
        "client_order_id": _mk_client_order_id(symbol),
        "type": "market",
        "side": side_int,
        "mode": 1,
        "leverage": str(LEVERAGE),
        "open_type": OPEN_TYPE,
        "size": int(size_int),
    }

    http, js = bitmart_request("POST", path, body)
    print(f"BITMART CLOSE: {symbol} {side} {{'http': {http}, 'json': {js}}} body={body}")
    return {"http": http, "json": js, "body": body}

# =========================
# POST-ENTRY SL VERIFY / FALLBACK (NEW)
# =========================
def bitmart_get_profit_loss_plans(symbol: str, limit: int = 10) -> Dict[str, Any]:
    # plan_type=profit_loss is key for preset TP/SL plans
    path = f"/contract/private/current-plan-order?symbol={symbol}&plan_type=profit_loss&limit={int(limit)}"
    http, js = bitmart_request("GET", path, None)
    print(f"BITMART GET PLANS: {symbol} {{'http': {http}, 'json': {js}}}")
    return {"http": http, "json": js}

def bitmart_modify_preset_plan(symbol: str, order_id: Any, sl_price: str) -> Dict[str, Any]:
    """
    Force-attach preset SL to an existing order via modify-preset-plan-order.
    """
    path = "/contract/private/modify-preset-plan-order"
    body = {
        "symbol": symbol,
        "order_id": str(order_id),
        "open_type": OPEN_TYPE,
        "preset_stop_loss_price": str(sl_price),
        "preset_stop_loss_price_type": 1,  # last_price
    }
    http, js = bitmart_request("POST", path, body)
    print(f"BITMART MODIFY PRESET SL: {symbol} {{'http': {http}, 'json': {js}}} body={body}")
    return {"http": http, "json": js, "body": body}

def _extract_plan_list(js: dict) -> List[dict]:
    data = js.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # common patterns
        for k in ("result", "orders", "plan_orders", "plan_order", "records"):
            v = data.get(k)
            if isinstance(v, list):
                return v
        # if dict represents a single plan
        if "order_id" in data or "plan_order_id" in data:
            return [data]
    return []

def _plan_matches(plan: dict, order_id: str, sl_price: str) -> bool:
    """
    Try multiple fields because BitMart schemas vary slightly.
    """
    if not isinstance(plan, dict):
        return False

    oid = str(plan.get("order_id") or plan.get("entrust_id") or plan.get("plan_order_id") or plan.get("trigger_order_id") or "")
    if oid and oid == str(order_id):
        return True

    # try match by trigger/stop price
    trig = str(plan.get("trigger_price") or plan.get("triggerPrice") or plan.get("stop_loss_price") or plan.get("stopLossPrice") or "")
    if trig and sl_price and trig == str(sl_price):
        return True

    return False

def ensure_sl_present(symbol: str, order_id: Any, sl_price: Optional[str]) -> None:
    if not POST_ENTRY_VERIFY_SL:
        return
    if not order_id or not sl_price:
        return

    try:
        check = bitmart_get_profit_loss_plans(symbol, limit=POST_ENTRY_VERIFY_LIMIT)
        http = int(check.get("http") or 0)
        js = check.get("json") if isinstance(check.get("json"), dict) else {}

        found = False
        if http == 200 and isinstance(js, dict) and js.get("code") in (1000, "1000"):
            plans = _extract_plan_list(js)
            for p in plans:
                if _plan_matches(p, str(order_id), str(sl_price)):
                    found = True
                    break

        if found:
            print(f"SL VERIFY OK: {symbol} order_id={order_id} sl={sl_price}")
            return

        print(f"SL VERIFY MISS -> fallback modify-preset: {symbol} order_id={order_id} sl={sl_price}")
        bitmart_modify_preset_plan(symbol, order_id, sl_price)

    except Exception as e:
        print(f"SL VERIFY ERROR: {symbol} order_id={order_id} sl={sl_price} err={e}")

# =========================
# RESYNC HELPERS
# =========================
def _extract_positions_list(js: dict) -> List[dict]:
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
# VECTOR BUFFER + DEBOUNCE (ENTRY ONLY)
# =========================
def update_vector_buffer(s: Dict[str, Any], bar_key: Optional[str], color: str) -> None:
    s["last_vector_bar_key"] = bar_key
    s["last_vector_color"] = color
    s["last_vector_ts"] = now_ts()

def entry_allowed_by_vector_debounce(s: Dict[str, Any], bar_key: Optional[str], color: str) -> bool:
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

    # EMA50_STATE updates regime only
    if event == "EMA50_STATE":
        st = (payload.get("state") or "").upper()
        if st in ("ABOVE", "BELOW"):
            s["regime"] = st
        return jsonify({"ok": True, "event": "EMA50_STATE", "regime": s["regime"]}), 200

    # VECTOR buffer updates
    if event == "VECTOR":
        color = (payload.get("color") or "").upper()
        if color in (LONG_COLOR, SHORT_COLOR):
            update_vector_buffer(s, bk, color)

    # Decide action: EXIT > ENTRY
    action: Optional[str] = should_exit(s, event, payload)
    if action is None:
        action = should_enter(s, event, payload)

    if action is None:
        return jsonify({"ok": True, "action": "NONE"}), 200

    # RESYNC before executing
    resync_state_with_exchange(symbol, s)

    if action in ("EXIT_LONG", "EXIT_SHORT"):
        if not s["in_position"] or (action == "EXIT_LONG" and s["side"] != "LONG") or (action == "EXIT_SHORT" and s["side"] != "SHORT"):
            print(f"RESYNC BLOCK EXIT: {symbol} action={action} state_in={s['in_position']} state_side={s['side']}")
            return jsonify({"ok": True, "action": "NONE", "note": "resync_block_exit"}), 200

    if action in ("ENTER_LONG", "ENTER_SHORT"):
        if s["in_position"]:
            print(f"RESYNC BLOCK ENTRY: {symbol} action={action} exchange/state shows in_position")
            return jsonify({"ok": True, "action": "NONE", "note": "resync_block_entry"}), 200

    # BAR_KEY GATE
    if bk and s["last_action_bar_key"] == bk:
        print(f"SKIP_DUPLICATE_BAR_KEY: {symbol} bar_key={bk} incoming_event={event} action={action}")
        return jsonify({"ok": True, "skipped": "duplicate_bar_key"}), 200

    # ENTRY debounce ONLY for VECTOR-triggered ENTRY
    if event == "VECTOR" and action in ("ENTER_LONG", "ENTER_SHORT"):
        color = (payload.get("color") or "").upper()
        if color in (LONG_COLOR, SHORT_COLOR):
            if not entry_allowed_by_vector_debounce(s, bk, color):
                return jsonify({"ok": True, "action": "NONE", "note": "vector_entry_debounce"}), 200

        final_color = s.get("pending_entry_color")
        if action == "ENTER_LONG" and final_color != LONG_COLOR:
            print(f"VECTOR ENTRY CANCEL: {symbol} bar_key={bk} action={action} final_color={final_color}")
            return jsonify({"ok": True, "action": "NONE", "note": "vector_entry_color_flip"}), 200
        if action == "ENTER_SHORT" and final_color != SHORT_COLOR:
            print(f"VECTOR ENTRY CANCEL: {symbol} bar_key={bk} action={action} final_color={final_color}")
            return jsonify({"ok": True, "action": "NONE", "note": "vector_entry_color_flip"}), 200

    # Execute action
    if action == "ENTER_LONG":
        bitmart_set_leverage(symbol)

        ref_px = safe_float(payload.get("close")) or safe_float(payload.get("open")) or 0.0
        sl_price = _calc_sl_price("LONG", ref_px) if ref_px > 0 else None

        r = bitmart_open_market(symbol, "LONG", sl_price)

        # Only update state if entry succeeded
        ok = int(r.get("http") or 0) == 200 and isinstance(r.get("json"), dict) and r["json"].get("code") in (1000, "1000")
        if ok:
            s["in_position"] = True
            s["side"] = "LONG"
            s["last_entry_bar_key"] = bk
            s["last_action_bar_key"] = bk
            s["last_action_type"] = "ENTRY"

            # Post-entry verify/fallback SL (NEW)
            try:
                order_id = (r.get("json") or {}).get("data", {}).get("order_id")
                ensure_sl_present(symbol, order_id, sl_price)
            except Exception as e:
                print(f"POST-ENTRY SL ERROR: {symbol} -> {e}")
        else:
            print(f"ENTRY FAILED -> STATE NOT UPDATED: {symbol} action={action} resp={r}")

        return jsonify({"ok": True, "action": "ENTER_LONG", "bitmart": r}), 200

    if action == "ENTER_SHORT":
        bitmart_set_leverage(symbol)

        ref_px = safe_float(payload.get("close")) or safe_float(payload.get("open")) or 0.0
        sl_price = _calc_sl_price("SHORT", ref_px) if ref_px > 0 else None

        r = bitmart_open_market(symbol, "SHORT", sl_price)

        ok = int(r.get("http") or 0) == 200 and isinstance(r.get("json"), dict) and r["json"].get("code") in (1000, "1000")
        if ok:
            s["in_position"] = True
            s["side"] = "SHORT"
            s["last_entry_bar_key"] = bk
            s["last	tf	"
            s["last_action_bar_key"] = bk
            s["last_action_type"] = "ENTRY"

            # Post-entry verify/fallback SL (NEW)
            try:
                order_id = (r.get("json") or {}).get("data", {}).get("order_id")
                ensure_sl_present(symbol, order_id, sl_price)
            except Exception as e:
                print(f"POST-ENTRY SL ERROR: {symbol} -> {e}")
        else:
            print(f"ENTRY FAILED -> STATE NOT UPDATED: {symbol} action={action} resp={r}")

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
        "post_entry_verify_sl": POST_ENTRY_VERIFY_SL,
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
