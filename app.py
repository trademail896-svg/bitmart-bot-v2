# app.py
from flask import Flask, request, jsonify
import os
import json
import requests
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

app = Flask(__name__)

# =========================
# CONFIG
# =========================
BOT_VERSION = os.environ.get("BOT_VERSION", "TV_BOT_DEMO_2026_V2_time_normalization_dedup_fix").strip()

SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# Demo by default (no real orders)
EXECUTION_ENABLED = (os.environ.get("EXECUTION_ENABLED") or "0").strip() == "1"

# Entry trigger mode:
# - VECTOR (recommended with your “vector enters” setup)
# - STOCH  (entries immediately on LD/HD)
ENTRY_TRIGGER = (os.environ.get("ENTRY_TRIGGER") or "VECTOR").strip().upper()
if ENTRY_TRIGGER not in ("VECTOR", "STOCH"):
    ENTRY_TRIGGER = "VECTOR"

ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Vector normalization: BLUE==GREEN, PURPLE==RED
LONG_COLORS = {"GREEN", "BLUE"}
SHORT_COLORS = {"RED", "PURPLE"}

# Upstash (REST)
UPSTASH_REDIS_REST_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").strip()
UPSTASH_REDIS_REST_TOKEN = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()

# TTLs
TTL_STATE_SEC = 60 * 60 * 24 * 7     # 7 days
TTL_LOCK_SEC = 60 * 60 * 6           # 6 hours (latch/anti-dup)

# Redis keys
K_EMA50 = "tvbotv2:ema50_state:{sym}"        # "ABOVE"/"BELOW"
K_BIAS = "tvbotv2:bias:{sym}"               # "LONG"/"SHORT"/None
K_POS = "tvbotv2:pos:{sym}"                 # json {"in_position":bool,"side":"LONG/SHORT"}
K_STOCH_LATCH = "tvbotv2:stoch_latch:{sym}" # bar_key_ms string
K_DEDUP = "tvbotv2:dedup:{sym}:{event}:{bar_key_ms}"  # "1" (idempotency)


# =========================
# UPSTASH HELPERS
# =========================
def _upstash_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }

def upstash_get(key: str) -> Optional[str]:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        r = requests.get(f"{UPSTASH_REDIS_REST_URL}/get/{key}", headers=_upstash_headers(), timeout=10)
        j = r.json()
        return j.get("result", None)
    except Exception:
        return None

def upstash_set(key: str, value: str, ex: int = TTL_STATE_SEC) -> bool:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return False
    try:
        payload = [key, value, "EX", ex]
        r = requests.post(
            f"{UPSTASH_REDIS_REST_URL}/set",
            headers=_upstash_headers(),
            data=json.dumps(payload),
            timeout=10
        )
        return r.status_code == 200
    except Exception:
        return False

def upstash_del(key: str) -> bool:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return False
    try:
        r = requests.post(f"{UPSTASH_REDIS_REST_URL}/del", headers=_upstash_headers(), data=json.dumps([key]), timeout=10)
        return r.status_code == 200
    except Exception:
        return False


# =========================
# TIME NORMALIZATION
# =========================
def tf_to_ms(tf: str) -> Optional[int]:
    s = str(tf).strip().upper()
    if s.isdigit():
        return int(s) * 60_000
    if s == "D":
        return 24 * 60 * 60_000
    if s == "W":
        return 7 * 24 * 60 * 60_000
    return None

def parse_time_ms(payload: Dict[str, Any]) -> Optional[int]:
    """
    Accepts:
      - payload['time_ms'] numeric string/int
      - payload['time'] ISO 'YYYY-MM-DDTHH:MM:SSZ'
    Returns epoch ms int or None.
    """
    if payload.get("time_ms") is not None:
        try:
            v = str(payload["time_ms"]).strip()
            if v.isdigit():
                return int(v)
        except Exception:
            pass

    iso = payload.get("time")
    if isinstance(iso, str) and iso.strip():
        iso = iso.strip()
        try:
            if iso.endswith("Z"):
                dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None

    return None

def snap_to_bar(time_ms: int, tf_ms: int) -> int:
    return (time_ms // tf_ms) * tf_ms

def normalize_symbol(ticker: str) -> str:
    return str(ticker).strip().upper().replace(".P", "")

def normalize_bar(payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    """
    Returns: (bar_time_ms, bar_key_ms, ticker, sym)
    """
    ticker = str(payload.get("ticker") or "").strip()
    tf = str(payload.get("tf") or "").strip()

    if not ticker or not tf:
        return None, None, None, None

    sym = normalize_symbol(ticker)
    if sym not in ALLOWED_SYMBOLS:
        return None, None, ticker, sym

    tf_ms = tf_to_ms(tf)
    t_ms = parse_time_ms(payload)
    if tf_ms is None or t_ms is None:
        return None, None, ticker, sym

    bar_time_ms = snap_to_bar(t_ms, tf_ms)
    bar_key_ms = f"{ticker}|{tf}|{bar_time_ms}"
    return bar_time_ms, bar_key_ms, ticker, sym


# =========================
# STATE
# =========================
def get_pos(sym: str) -> Dict[str, Any]:
    raw = upstash_get(K_POS.format(sym=sym))
    if not raw:
        return {"in_position": False, "side": None}
    try:
        raw = raw.strip()
        j = json.loads(raw)
        if isinstance(j, dict) and "in_position" in j:
            side = j.get("side")
            if isinstance(side, str):
                side = side.strip().upper()
            return {"in_position": bool(j.get("in_position")), "side": side}
    except Exception:
        pass
    return {"in_position": False, "side": None}

def set_pos(sym: str, in_position: bool, side: Optional[str]) -> None:
    s = side.strip().upper() if isinstance(side, str) else None
    upstash_set(K_POS.format(sym=sym), json.dumps({"in_position": in_position, "side": s}), ex=TTL_STATE_SEC)

def get_ema50(sym: str) -> Optional[str]:
    v = upstash_get(K_EMA50.format(sym=sym))
    if isinstance(v, str):
        v = v.strip().upper()
    if v in ("ABOVE", "BELOW"):
        return v
    return None

def set_ema50(sym: str, state: str) -> None:
    state = str(state).strip().upper()
    if state in ("ABOVE", "BELOW"):
        upstash_set(K_EMA50.format(sym=sym), state, ex=TTL_STATE_SEC)

def get_bias(sym: str) -> Optional[str]:
    v = upstash_get(K_BIAS.format(sym=sym))
    if isinstance(v, str):
        v = v.strip().upper()
    if v in ("LONG", "SHORT"):
        return v
    return None

def set_bias(sym: str, bias: str) -> None:
    bias = str(bias).strip().upper()
    if bias in ("LONG", "SHORT"):
        upstash_set(K_BIAS.format(sym=sym), bias, ex=TTL_STATE_SEC)

def get_stoch_latch(sym: str) -> Optional[str]:
    v = upstash_get(K_STOCH_LATCH.format(sym=sym))
    if isinstance(v, str):
        return v.strip()
    return None

def set_stoch_latch(sym: str, bar_key_ms: str) -> None:
    upstash_set(K_STOCH_LATCH.format(sym=sym), str(bar_key_ms).strip(), ex=TTL_LOCK_SEC)

def clear_stoch_latch(sym: str) -> None:
    upstash_del(K_STOCH_LATCH.format(sym=sym))


# =========================
# DEDUP (Upstash REST SAFE)
# =========================
def dedup_safe(sym: str, event: str, bar_key_ms: str) -> bool:
    """
    Returns True if NEW and should be processed.
    Returns False if DUPLICATE (already seen).

    Implementation: GET then SET (no NX), safe with Upstash REST.
    """
    key = K_DEDUP.format(sym=sym, event=event, bar_key_ms=bar_key_ms)
    existing = upstash_get(key)
    if existing is not None:
        return False
    # Best-effort set; even if it fails, we still process to avoid blocking critical logic.
    upstash_set(key, "1", ex=TTL_LOCK_SEC)
    return True


# =========================
# DEMO EXECUTION (no exchange calls)
# =========================
def demo_action(action: str, sym: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "demo": not EXECUTION_ENABLED,
        "action": action,
        "symbol": sym,
        "meta": meta,
        "bot_version": BOT_VERSION,
        "entry_trigger": ENTRY_TRIGGER,
    }


# =========================
# ROUTES
# =========================
@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "bot_version": BOT_VERSION,
        "execution_enabled": EXECUTION_ENABLED,
        "entry_trigger": ENTRY_TRIGGER,
        "allowed_symbols": sorted(list(ALLOWED_SYMBOLS)),
        "upstash_configured": bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN),
    }), 200


@app.post("/webhook")
def webhook():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    # Secret
    if str(payload.get("secret", "")).strip() != SECRET:
        return jsonify({"ok": False, "error": "Bad secret"}), 403

    event = str(payload.get("event") or "").strip().upper()
    if not event:
        return jsonify({"ok": False, "error": "Missing event"}), 400

    bar_time_ms, bar_key_ms, ticker, sym = normalize_bar(payload)
    if sym is None or ticker is None:
        return jsonify({"ok": False, "error": "Missing ticker/tf"}), 400

    if sym not in ALLOWED_SYMBOLS:
        return jsonify({"ok": True, "ignored": True, "reason": "IGNORED_SYMBOL", "symbol": sym}), 200

    if bar_key_ms is None:
        return jsonify({"ok": False, "error": "Could not normalize time (need time_ms numeric or time ISO)"}), 400

    # Basic log
    print("ALERTE:", payload)
    print("NORM:", {"sym": sym, "ticker": ticker, "bar_time_ms": bar_time_ms, "bar_key_ms": bar_key_ms})

    # EMA50_STATE must ALWAYS be processed (do not block it with dedup)
    if event != "EMA50_STATE":
        if not dedup_safe(sym, event, bar_key_ms):
            return jsonify({"ok": True, "ignored": True, "reason": "DUPLICATE_EVENT", "bar_key_ms": bar_key_ms, "event": event}), 200

    pos = get_pos(sym)
    ema50 = get_ema50(sym)
    bias = get_bias(sym)

    # -------------------------
    # EMA50_STATE
    # -------------------------
    if event == "EMA50_STATE":
        state = str(payload.get("state") or "").strip().upper()
        if state not in ("ABOVE", "BELOW"):
            return jsonify({"ok": False, "error": "Invalid EMA50 state"}), 400
        set_ema50(sym, state)
        # Optionally also dedup AFTER writing (not required)
        return jsonify({"ok": True, "event": "EMA50_STATE", "symbol": sym, "state": state, "bar_key_ms": bar_key_ms}), 200

    # -------------------------
    # STOCH_SIGNAL (LD/HD)
    # - latch per bar: first STOCH wins
    # - strict filter: ABOVE => accept LD (bias LONG); BELOW => accept HD (bias SHORT)
    # - entry depends on ENTRY_TRIGGER
    # -------------------------
    if event == "STOCH_SIGNAL":
        reason = str(payload.get("reason") or "").strip().upper()
        if reason not in ("LD", "HD"):
            return jsonify({"ok": False, "error": "Invalid STOCH reason"}), 400

        # Latch: only first STOCH per bar is taken
        latched = get_stoch_latch(sym)
        if latched == bar_key_ms:
            return jsonify({"ok": True, "ignored": True, "reason": "STOCH_LATCHED_THIS_BAR", "bar_key_ms": bar_key_ms}), 200
        set_stoch_latch(sym, bar_key_ms)

        ema50 = get_ema50(sym)
        if ema50 not in ("ABOVE", "BELOW"):
            return jsonify({"ok": True, "ignored": True, "reason": "EMA50_UNKNOWN"}), 200

        # Apply strict regime mapping
        if ema50 == "ABOVE" and reason == "LD":
            set_bias(sym, "LONG")
            bias = "LONG"
        elif ema50 == "BELOW" and reason == "HD":
            set_bias(sym, "SHORT")
            bias = "SHORT"
        else:
            return jsonify({"ok": True, "ignored": True, "reason": "STOCH_BLOCKED_BY_EMA50", "ema50": ema50, "reason_in": reason}), 200

        # Entry if configured to enter on STOCH
        if ENTRY_TRIGGER == "STOCH":
            pos = get_pos(sym)
            if pos.get("in_position"):
                return jsonify({"ok": True, "ignored": True, "reason": "IN_POSITION_NO_FLIP"}), 200

            if bias == "LONG":
                set_pos(sym, True, "LONG")
                return jsonify(demo_action("ENTER_LONG", sym, {"trigger": "STOCH_LD", "bar_key_ms": bar_key_ms})), 200

            if bias == "SHORT":
                set_pos(sym, True, "SHORT")
                return jsonify(demo_action("ENTER_SHORT", sym, {"trigger": "STOCH_HD", "bar_key_ms": bar_key_ms})), 200

        # Otherwise STOCH sets bias only
        return jsonify({"ok": True, "event": "STOCH_SIGNAL", "symbol": sym, "bias": bias, "bar_key_ms": bar_key_ms}), 200

    # -------------------------
    # VECTOR
    # - exit immediately on opposite vector
    # - entry if ENTRY_TRIGGER == VECTOR and (bias matches) and FLAT and EMA50 matches
    # -------------------------
    if event == "VECTOR":
        side = str(payload.get("side") or "").strip().upper()   # LONG/SHORT
        color = str(payload.get("color") or "").strip().upper()

        if side not in ("LONG", "SHORT"):
            return jsonify({"ok": False, "error": "Invalid VECTOR side"}), 400

        # Optional warnings for color mismatches
        if side == "LONG" and color and color not in LONG_COLORS:
            print("WARN: VECTOR LONG color unexpected:", color)
        if side == "SHORT" and color and color not in SHORT_COLORS:
            print("WARN: VECTOR SHORT color unexpected:", color)

        pos = get_pos(sym)

        # Exit first (immediate)
        if pos.get("in_position"):
            current_side = str(pos.get("side") or "").strip().upper()
            if current_side == "LONG" and side == "SHORT":
                set_pos(sym, False, None)
                clear_stoch_latch(sym)
                return jsonify(demo_action("EXIT_LONG", sym, {"trigger": "OPPOSITE_VECTOR", "bar_key_ms": bar_key_ms})), 200
            if current_side == "SHORT" and side == "LONG":
                set_pos(sym, False, None)
                clear_stoch_latch(sym)
                return jsonify(demo_action("EXIT_SHORT", sym, {"trigger": "OPPOSITE_VECTOR", "bar_key_ms": bar_key_ms})), 200
            return jsonify({"ok": True, "ignored": True, "reason": "VECTOR_NOT_OPPOSITE", "pos": pos, "vector_side": side}), 200

        # Entry when FLAT
        if ENTRY_TRIGGER == "VECTOR":
            ema50 = get_ema50(sym)
            bias = get_bias(sym)

            if ema50 not in ("ABOVE", "BELOW") or bias not in ("LONG", "SHORT"):
                return jsonify({"ok": True, "ignored": True, "reason": "MISSING_FILTERS", "ema50": ema50, "bias": bias}), 200

            # Enforce EMA50 regime
            if side == "LONG" and ema50 != "ABOVE":
                return jsonify({"ok": True, "ignored": True, "reason": "VECTOR_LONG_BLOCKED_BY_EMA50", "ema50": ema50}), 200
            if side == "SHORT" and ema50 != "BELOW":
                return jsonify({"ok": True, "ignored": True, "reason": "VECTOR_SHORT_BLOCKED_BY_EMA50", "ema50": ema50}), 200

            # Require bias match
            if side == "LONG" and bias != "LONG":
                return jsonify({"ok": True, "ignored": True, "reason": "NO_LONG_BIAS"}), 200
            if side == "SHORT" and bias != "SHORT":
                return jsonify({"ok": True, "ignored": True, "reason": "NO_SHORT_BIAS"}), 200

            if side == "LONG":
                set_pos(sym, True, "LONG")
                return jsonify(demo_action("ENTER_LONG", sym, {"trigger": "VECTOR_LONG", "bar_key_ms": bar_key_ms})), 200
            else:
                set_pos(sym, True, "SHORT")
                return jsonify(demo_action("ENTER_SHORT", sym, {"trigger": "VECTOR_SHORT", "bar_key_ms": bar_key_ms})), 200

        return jsonify({"ok": True, "ignored": True, "reason": "FLAT_VECTOR_NO_ACTION"}), 200

    return jsonify({"ok": True, "ignored": True, "reason": "UNKNOWN_EVENT", "event": event}), 200


# =========================
# DEBUG ENDPOINTS
# =========================
@app.get("/debug/state/<symbol>")
def debug_state(symbol: str):
    sym = symbol.strip().upper()
    if sym not in ALLOWED_SYMBOLS:
        return jsonify({"ok": False, "error": "Unknown symbol"}), 400
    return jsonify({
        "ok": True,
        "endpoint": f"/debug/state/{sym}",
        "symbol": sym,
        "ema50_state": get_ema50(sym),
        "bias": get_bias(sym),
        "pos": get_pos(sym),
        "stoch_latch": get_stoch_latch(sym),
    }), 200

@app.get("/debug/bitmart")
def debug_bitmart():
    return jsonify({
        "ok": True,
        "endpoint": "/debug/bitmart",
        "note": "This build does not execute real BitMart orders unless EXECUTION_ENABLED=1",
        "execution_enabled": EXECUTION_ENABLED,
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
