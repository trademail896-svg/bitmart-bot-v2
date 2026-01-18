# app.py — TV_BOT_DEMO_2026_V2 (Render + BitMart Futures)
# Patch APPROUVÉ: BUFFER/REPLAY (regime None) + SL “Position TP/SL” (UI-visible) + logs priorité EXIT
#
# Contraintes respectées:
# - Aucun changement Pine
# - Stratégie V3.2 inchangée
# - SL > EXIT > ENTRY
# - 1 action max par bar_key (last_action_bar_key)
# - resync_state_with_exchange() AVANT décision (déjà approuvé)
# - BUFFER/REPLAY minimal (1 pending / symbole) + replay seulement si bar_key match EXACT

from flask import Flask, request, jsonify
import os, time, json, hmac, hashlib, math
from typing import Dict, Any, Optional, Tuple
import requests

app = Flask(__name__)

# =============================
# CONFIG — Trading strategy invariants
# =============================
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

LONG_COLORS = {"green", "blue"}      # VECTOR long
SHORT_COLORS = {"red", "purple"}     # VECTOR short

TV_SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# =============================
# CONFIG — BitMart
# =============================
BITMART_BASE_URL = (os.environ.get("BITMART_BASE_URL") or "https://api-cloud-v2.bitmart.com").strip()
BITMART_API_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_API_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_API_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

OPEN_TYPE = (os.environ.get("OPEN_TYPE") or "isolated").strip()  # isolated / cross
LEVERAGE = int(os.environ.get("LEVERAGE") or "25")

# Position sizing (NOTE: BitMart "size" is often CONTRACTS integer.
# If your existing bot already computes correct "size", plug it here.)
EST_MARGIN_USD_PER_TRADE = float(os.environ.get("EST_MARGIN_USD_PER_TRADE") or "100")
NOTIONAL_USD_PER_TRADE = float(os.environ.get("NOTIONAL_USD_PER_TRADE") or str(EST_MARGIN_USD_PER_TRADE * LEVERAGE))

# Stop-loss percentage (WITHOUT leverage) e.g. 0.5% -> 0.005
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT") or "0.005")

# Make SL UI-visible using Position TP/SL endpoint
USE_POSITION_TPSL = (os.environ.get("USE_POSITION_TPSL") or "true").lower() == "true"
TPSL_PRICE_TYPE = int(os.environ.get("TPSL_PRICE_TYPE") or "2")  # 1=last, 2=fair/mark (recommended)
TPSL_CATEGORY = (os.environ.get("TPSL_CATEGORY") or "market").strip()  # "market" recommended

# Contract sizing helper (optional)
# If BitMart requires contracts, you can set CONTRACTS_PER_1_UNIT, e.g. 100, 10, etc. (depends on product spec)
CONTRACTS_PER_1_UNIT = float(os.environ.get("CONTRACTS_PER_1_UNIT") or "1")

# =============================
# STATE
# =============================
def now_ms() -> int:
    return int(time.time() * 1000)

def log(msg: str, obj: Optional[dict] = None):
    if obj is None:
        print(msg, flush=True)
    else:
        print(f"{msg} {obj}", flush=True)

def normalize_symbol(tv_ticker: str) -> Optional[str]:
    # TradingView peut envoyer BTCUSDT.P
    if not tv_ticker:
        return None
    base = tv_ticker.replace(".P", "").strip().upper()
    return base if base in ALLOWED_SYMBOLS else None

def verify_tv_secret(payload: Dict[str, Any]) -> bool:
    return (payload.get("secret") or "").strip() == TV_SECRET

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def safe_int(x, default=0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default

def compute_bar_key(payload: Dict[str, Any]) -> str:
    # On utilise le champ bar_key si fourni par Pine, sinon on reconstruit (ticker|tf|time)
    if payload.get("bar_key"):
        return str(payload["bar_key"])
    t = payload.get("ticker") or payload.get("symbol") or "NA"
    tf = payload.get("tf") or payload.get("interval") or "NA"
    tm = payload.get("time") or payload.get("time_ms") or "NA"
    return f"{t}|{tf}|{tm}"

def compute_size_contracts(symbol: str, close_price: float) -> int:
    # Minimal and deterministic.
    # If your existing bot has exact BitMart sizing, replace this function body with your known-good logic.
    if close_price <= 0:
        return 0
    qty_units = NOTIONAL_USD_PER_TRADE / close_price  # units (coin)
    contracts = int(math.floor(qty_units * CONTRACTS_PER_1_UNIT))
    return max(0, contracts)

def round_price(p: float) -> str:
    # Simple rounding; adjust per symbol tick if needed.
    return f"{p:.2f}"

STATE: Dict[str, Dict[str, Any]] = {
    s: {
        "in_position": False,
        "side": None,                 # "LONG" / "SHORT"
        "regime": None,               # "ABOVE" / "BELOW"
        "last_action_bar_key": None,  # gate 1 action / bar_key
        "pending": None,              # BUFFER/REPLAY (1 pending / symbole)
        "last_resync_ts": 0,
        "entry_price": None,          # best-effort from alert
    }
    for s in ALLOWED_SYMBOLS
}

# =============================
# BITMART — signing + request
# =============================
def _sorted_kv_string(params: Dict[str, Any]) -> str:
    # For GET signing: "k=v&k2=v2" with stable order
    items = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        items.append(f"{k}={v}")
    return "&".join(items)

def _compact_json(params: Dict[str, Any]) -> str:
    # For POST signing: compact json string
    return json.dumps(params, separators=(",", ":"), sort_keys=True)

def _bm_sign(ts_ms: int, qs: str) -> str:
    # Signature prehash: timestamp + "#" + memo + "#" + queryString
    prehash = f"{ts_ms}#{BITMART_API_MEMO}#{qs}"
    return hmac.new(
        BITMART_API_SECRET.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def bm_request(method: str, path: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Tuple[int, Dict[str, Any]]:
    params = params or {}
    url = BITMART_BASE_URL.rstrip("/") + path
    ts = now_ms()

    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-BM-KEY"] = BITMART_API_KEY
        headers["X-BM-TIMESTAMP"] = str(ts)

    if method.upper() == "GET":
        qs = _sorted_kv_string(params)
        if signed:
            headers["X-BM-SIGN"] = _bm_sign(ts, qs)
        try:
            r = requests.get(url, headers=headers, params=params, timeout=12)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"raw": r.text}
        except Exception as e:
            return 599, {"error": str(e), "path": path}

    else:
        body = params
        qs = _compact_json(body)
        if signed:
            headers["X-BM-SIGN"] = _bm_sign(ts, qs)
        try:
            r = requests.post(url, headers=headers, data=qs, timeout=12)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"raw": r.text}
        except Exception as e:
            return 599, {"error": str(e), "path": path}

# =============================
# BITMART — primitives
# =============================
def bm_get_position(symbol: str) -> Tuple[bool, Optional[str], float]:
    """
    Returns (in_position, side, position_size)
    side may be None if API doesn't include a clean side field.
    """
    http, js = bm_request("GET", "/contract/private/position-v2", {"symbol": symbol}, signed=True)
    if http != 200:
        log("BITMART position-v2 HTTP error:", {"symbol": symbol, "http": http, "json": js})
        return False, None, 0.0
    if js.get("code") != 1000:
        log("BITMART position-v2 API error:", {"symbol": symbol, "json": js})
        return False, None, 0.0

    data = js.get("data") or {}
    # Try common fields
    amt = safe_float(data.get("position_amount", data.get("current_amount", 0.0)), 0.0)
    in_pos = abs(amt) > 0

    # Some payloads include something like hold_side/position_side
    side = None
    for k in ("hold_side", "position_side", "position_type"):
        if k in data and data[k] is not None:
            try:
                v = int(data[k])
                if v == 1:
                    side = "LONG"
                elif v == 2:
                    side = "SHORT"
            except Exception:
                pass

    return in_pos, side, abs(amt)

def resync_state_with_exchange(symbol: str):
    st = STATE[symbol]
    prev = {"in_position": st["in_position"], "side": st["side"]}

    in_pos, exch_side, size = bm_get_position(symbol)
    st["in_position"] = in_pos
    if not in_pos:
        st["side"] = None
        st["entry_price"] = None
    else:
        if exch_side in ("LONG", "SHORT"):
            st["side"] = exch_side

    st["last_resync_ts"] = now_ms()

    # Important log when mismatch matters for exits
    if prev["in_position"] != st["in_position"] or prev["side"] != st["side"]:
        log("RESYNC:", {"symbol": symbol, "prev": prev, "now": {"in_position": st["in_position"], "side": st["side"], "size": size}})

def bm_set_leverage(symbol: str):
    http, js = bm_request("POST", "/contract/private/submit-leverage", {
        "symbol": symbol,
        "leverage": str(LEVERAGE),
        "open_type": OPEN_TYPE
    }, signed=True)
    log("BITMART leverage:", {"symbol": symbol, "http": http, "json": js})

def bm_submit_order(symbol: str, side_code: int, size: int) -> Tuple[bool, Dict[str, Any]]:
    """
    side_code (BitMart contract submit-order) common mapping:
      1 buy_open_long
      2 sell_close_long
      3 buy_close_short
      4 sell_open_short
    If your exchange/account uses different, adjust side_code here ONLY.
    """
    payload = {
        "symbol": symbol,
        "side": side_code,
        "type": "market",
        "size": str(size),
        "open_type": OPEN_TYPE,
        "leverage": str(LEVERAGE),
    }
    http, js = bm_request("POST", "/contract/private/submit-order", payload, signed=True)
    ok = (http == 200 and js.get("code") == 1000)
    log("BITMART submit-order:", {"symbol": symbol, "side_code": side_code, "size": size, "http": http, "json": js})
    return ok, {"http": http, "json": js}

def bm_submit_position_sl(symbol: str, trigger_price: float) -> Tuple[bool, Dict[str, Any]]:
    """
    Position TP/SL — endpoint designed to attach TP/SL at position-level (often UI-visible).
    plan_category: 2 = Position TP/SL (default)
    category: market (recommended)
    type: stop_loss
    """
    payload = {
        "symbol": symbol,
        "type": "stop_loss",
        "trigger_price": round_price(trigger_price),
        "plan_category": 2,
        "price_type": TPSL_PRICE_TYPE,
        "category": TPSL_CATEGORY,
    }
    http, js = bm_request("POST", "/contract/private/submit-tp-sl-order", payload, signed=True)
    ok = (http == 200 and js.get("code") == 1000)
    log("SL_SUBMIT (Position TP/SL):", {"symbol": symbol, "trigger_price": payload["trigger_price"], "price_type": TPSL_PRICE_TYPE, "category": TPSL_CATEGORY, "http": http, "json": js})
    return ok, {"http": http, "json": js}

def bm_get_current_plan_orders(symbol: str) -> Tuple[int, Dict[str, Any]]:
    # Best-effort verify (endpoint name can vary between versions; keep non-fatal)
    http, js = bm_request("GET", "/contract/private/current-plan-order", {"symbol": symbol}, signed=True)
    return http, js

# =============================
# STRATEGY V3.2 — decision core (NO CHANGE)
# =============================
def gate_already_acted(symbol: str, bar_key: str) -> bool:
    st = STATE[symbol]
    return st["last_action_bar_key"] == bar_key

def set_acted(symbol: str, bar_key: str):
    STATE[symbol]["last_action_bar_key"] = bar_key

def regime_allows_entry(regime: str, signal_or_color: str, is_stoch: bool) -> Optional[str]:
    """
    Returns desired entry side "LONG"/"SHORT" or None.
    V3.2:
      ABOVE+LD or VECTOR green/blue => LONG
      BELOW+HD or VECTOR red/purple => SHORT
    """
    if not regime:
        return None

    if is_stoch:
        if regime == "ABOVE" and signal_or_color == "LD":
            return "LONG"
        if regime == "BELOW" and signal_or_color == "HD":
            return "SHORT"
        return None

    # VECTOR
    if regime == "ABOVE" and signal_or_color in LONG_COLORS:
        return "LONG"
    if regime == "BELOW" and signal_or_color in SHORT_COLORS:
        return "SHORT"
    return None

def stoch_exit_for_side(side: str, stoch_sig: str) -> bool:
    # V3.2 exits:
    # LONG exits on HD; SHORT exits on LD
    return (side == "LONG" and stoch_sig == "HD") or (side == "SHORT" and stoch_sig == "LD")

def vector_exit_for_side(side: str, color: str) -> bool:
    # LONG exits on red/purple; SHORT exits on green/blue
    if side == "LONG":
        return color in SHORT_COLORS
    if side == "SHORT":
        return color in LONG_COLORS
    return False

def place_entry(symbol: str, side: str, close_price: float, bar_key: str, reason: str) -> Dict[str, Any]:
    """
    ENTRY: sets leverage (best-effort), submits market order, then attaches SL if configured.
    """
    st = STATE[symbol]
    if st["in_position"]:
        return {"ok": False, "msg": "ENTRY blocked: already in position"}

    size = compute_size_contracts(symbol, close_price)
    if size <= 0:
        return {"ok": False, "msg": "ENTRY blocked: computed size <= 0", "close": close_price}

    # Set leverage (non-fatal)
    bm_set_leverage(symbol)

    if side == "LONG":
        side_code = 1
    else:
        side_code = 4

    ok, resp = bm_submit_order(symbol, side_code, size)
    if not ok:
        return {"ok": False, "msg": "ENTRY failed", "resp": resp}

    # Local optimistic update (exchange resync will confirm)
    st["in_position"] = True
    st["side"] = side
    st["entry_price"] = close_price

    set_acted(symbol, bar_key)
    log("ENTRY_APPLY:", {"symbol": symbol, "side": side, "bar_key": bar_key, "reason": reason, "size": size, "close": close_price})

    # Attach SL (Position TP/SL) — UI-visible
    if USE_POSITION_TPSL:
        # best-effort resync before placing SL (position must exist)
        resync_state_with_exchange(symbol)
        if STATE[symbol]["in_position"]:
            if side == "LONG":
                trigger = close_price * (1.0 - STOP_LOSS_PCT)
            else:
                trigger = close_price * (1.0 + STOP_LOSS_PCT)

            ok_sl, sl_resp = bm_submit_position_sl(symbol, trigger)
            # Best-effort verify
            http_po, js_po = bm_get_current_plan_orders(symbol)
            log("SL_VERIFY (current-plan-order):", {"symbol": symbol, "http": http_po, "json": js_po})

            return {"ok": True, "msg": "ENTRY ok + SL attempted", "entry": resp, "sl": sl_resp}
        else:
            log("SL_SKIP:", {"symbol": symbol, "reason": "position_not_confirmed_after_entry"})
            return {"ok": True, "msg": "ENTRY ok (SL skipped: position not confirmed yet)", "entry": resp}

    return {"ok": True, "msg": "ENTRY ok (SL disabled)", "entry": resp}

def place_exit(symbol: str, bar_key: str, reason: str) -> Dict[str, Any]:
    """
    EXIT: submit market close based on local side (or exchange side if resync populated it).
    """
    st = STATE[symbol]
    if not st["in_position"] or st["side"] not in ("LONG", "SHORT"):
        return {"ok": False, "msg": "EXIT blocked: state flat"}

    side = st["side"]
    # Close size: Using "size" close-all is not always possible; often you close by size.
    # Minimal approach: use a large size is dangerous. Better: rely on your existing close size logic.
    # Here: we attempt "close by position amount" using position-v2 size if available.
    in_pos, exch_side, pos_size = bm_get_position(symbol)
    if not in_pos or pos_size <= 0:
        # If exchange says flat, align local
        st["in_position"] = False
        st["side"] = None
        st["entry_price"] = None
        return {"ok": False, "msg": "EXIT skipped: exchange flat (local state corrected)"}

    size = int(math.floor(pos_size))
    if size <= 0:
        return {"ok": False, "msg": "EXIT blocked: computed close size <= 0", "pos_size": pos_size}

    if side == "LONG":
        side_code = 2  # sell_close_long
    else:
        side_code = 3  # buy_close_short

    ok, resp = bm_submit_order(symbol, side_code, size)
    if not ok:
        return {"ok": False, "msg": "EXIT failed", "resp": resp}

    # Local update
    st["in_position"] = False
    st["side"] = None
    st["entry_price"] = None

    set_acted(symbol, bar_key)
    log("EXIT_APPLY:", {"symbol": symbol, "bar_key": bar_key, "reason": reason, "closed_side": side, "size": size})
    return {"ok": True, "msg": "EXIT ok", "resp": resp}

def process_signal(symbol: str, payload: Dict[str, Any], source: str) -> Dict[str, Any]:
    """
    source: "VECTOR" | "STOCH_SIGNAL" | "EMA50_STATE" | "REPLAY"
    Must respect: SL > EXIT > ENTRY, 1 action max per bar_key
    """
    st = STATE[symbol]
    bar_key = compute_bar_key(payload)

    # Resync BEFORE decision (approved)
    resync_state_with_exchange(symbol)

    # Gate
    if gate_already_acted(symbol, bar_key):
        log("GATE_SKIP:", {"symbol": symbol, "bar_key": bar_key, "source": source})
        return {"ok": True, "msg": "SKIP: already acted on bar_key", "bar_key": bar_key}

    event = (payload.get("event") or "").upper()

    # ============ SL priority ============
    # (SL management is exchange-side; bot doesn't trigger it here.
    # Priority mention maintained: if in future you add explicit SL event, place it above exits.)

    # ============ EXIT priority ============
    if st["in_position"] and st["side"] in ("LONG", "SHORT"):
        side = st["side"]

        if event == "STOCH_SIGNAL":
            sig = (payload.get("signal") or payload.get("stoch") or payload.get("value") or payload.get("reason") or "").upper()
            # Normalize to LD/HD if sent as "LD"/"HD"
            if sig in ("LD", "HD"):
                if stoch_exit_for_side(side, sig):
                    return place_exit(symbol, bar_key, reason=f"STOCH_EXIT_{sig}")
                else:
                    log("EXIT_NOOP (stoch):", {"symbol": symbol, "side": side, "sig": sig, "bar_key": bar_key})

        if event == "VECTOR":
            color = (payload.get("color") or "").lower()
            if color:
                if vector_exit_for_side(side, color):
                    return place_exit(symbol, bar_key, reason=f"VECTOR_EXIT_{color}")
                else:
                    log("EXIT_NOOP (vector):", {"symbol": symbol, "side": side, "color": color, "bar_key": bar_key})

    # ============ ENTRY ============
    if not st["in_position"]:
        regime = st.get("regime")
        close_price = safe_float(payload.get("close"), 0.0)

        if event == "STOCH_SIGNAL":
            sig = (payload.get("signal") or payload.get("stoch") or payload.get("value") or payload.get("reason") or "").upper()
            if sig in ("LD", "HD"):
                side = regime_allows_entry(regime, sig, is_stoch=True)
                if side and close_price > 0:
                    return place_entry(symbol, side, close_price, bar_key, reason=f"STOCH_ENTRY_{sig}_{regime}")

        if event == "VECTOR":
            color = (payload.get("color") or "").lower()
            # Debounce ENTRY on VECTOR only: "once per bar" already at TV, we still keep bar_key gate + this check
            if color:
                side = regime_allows_entry(regime, color, is_stoch=False)
                if side and close_price > 0:
                    return place_entry(symbol, side, close_price, bar_key, reason=f"VECTOR_ENTRY_{color}_{regime}")

    return {"ok": True, "msg": "NO_ACTION", "bar_key": bar_key, "source": source}

# =============================
# BUFFER / REPLAY (approved)
# =============================
def buffer_pending(symbol: str, payload: Dict[str, Any], why: str) -> Dict[str, Any]:
    bar_key = compute_bar_key(payload)
    evt = (payload.get("event") or "").upper()
    STATE[symbol]["pending"] = {
        "bar_key": bar_key,
        "event": evt,
        "payload": payload,
        "ts": now_ms(),
    }
    log("BUFFER:", {"symbol": symbol, "event": evt, "bar_key": bar_key, "reason": why})
    return {"ok": True, "msg": "BUFFERED", "bar_key": bar_key, "event": evt}

def try_replay_pending(symbol: str, ema_payload: Dict[str, Any]) -> Dict[str, Any]:
    st = STATE[symbol]
    pending = st.get("pending")
    ema_bar_key = compute_bar_key(ema_payload)

    if not pending:
        return {"ok": True, "msg": "REPLAY: no pending", "bar_key": ema_bar_key}

    if pending.get("bar_key") != ema_bar_key:
        log("REPLAY_SKIP:", {"symbol": symbol, "reason": "bar_key_mismatch", "pending_bar_key": pending.get("bar_key"), "ema_bar_key": ema_bar_key})
        st["pending"] = None
        return {"ok": True, "msg": "REPLAY_SKIP: bar_key mismatch", "ema_bar_key": ema_bar_key}

    # Respect gate: if already acted on this bar_key, do NOT replay
    if gate_already_acted(symbol, ema_bar_key):
        log("REPLAY_SKIP:", {"symbol": symbol, "reason": "already_acted_on_bar_key", "bar_key": ema_bar_key})
        st["pending"] = None
        return {"ok": True, "msg": "REPLAY_SKIP: already acted", "bar_key": ema_bar_key}

    log("REPLAY:", {"symbol": symbol, "event": pending.get("event"), "bar_key": ema_bar_key})
    st["pending"] = None
    # Replay by routing through the SAME decision pipeline
    return process_signal(symbol, pending["payload"], source="REPLAY")

# =============================
# WEBHOOK
# =============================
@app.route("/", methods=["GET"])
def home():
    return "ok", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"ok": False, "error": "invalid json"}), 400

    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "payload must be object"}), 400

    if not verify_tv_secret(payload):
        return jsonify({"ok": False, "error": "bad secret"}), 403

    # Normalize symbol
    tv_ticker = payload.get("ticker") or payload.get("symbol") or ""
    symbol = normalize_symbol(tv_ticker)
    if not symbol:
        log("IGNORED SYMBOL:", {"ticker": tv_ticker})
        return jsonify({"ok": True, "msg": "ignored symbol"}), 200

    event = (payload.get("event") or "").upper()
    bar_key = compute_bar_key(payload)

    # Basic trace log
    log("ALERTE:", {"event": event, "symbol": symbol, "bar_key": bar_key})

    st = STATE[symbol]

    # EMA50_STATE updates regime + replay pending if exact bar_key match
    if event == "EMA50_STATE":
        state = (payload.get("state") or payload.get("regime") or payload.get("value") or "").upper()
        if state in ("ABOVE", "BELOW"):
            prev = st.get("regime")
            st["regime"] = state
            log("REGIME_SET:", {"symbol": symbol, "prev": prev, "now": state, "bar_key": bar_key})

            # Replay pending (approved) — only if bar_key match exact
            replay_res = try_replay_pending(symbol, payload)
            return jsonify({"ok": True, "msg": "EMA50_STATE processed", "replay": replay_res}), 200

        return jsonify({"ok": True, "msg": "EMA50_STATE ignored: invalid state"}), 200

    # BUFFER logic: if event is actionable but regime is None, buffer and exit
    if event in ("VECTOR", "STOCH_SIGNAL"):
        if st.get("regime") is None:
            res = buffer_pending(symbol, payload, why="regime_none")
            return jsonify(res), 200

        # Normal processing
        res = process_signal(symbol, payload, source=event)
        return jsonify(res), 200

    # Unknown events: ignore safely
    return jsonify({"ok": True, "msg": "ignored event", "event": event}), 200


# =============================
# DEBUG ENDPOINTS (optional)
# =============================
@app.route("/debug/state", methods=["GET"])
def debug_state():
    # For quick inspection in Render logs / browser
    return jsonify({"ok": True, "state": STATE}), 200


if __name__ == "__main__":
    # Local run
    port = int(os.environ.get("PORT") or "5000")
    app.run(host="0.0.0.0", port=port, debug=False)
