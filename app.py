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
BOT_VERSION = os.environ.get(
    "BOT_VERSION",
    "TV_BOT_DEMO_2026_V2_B1_stoch_flip_safe_with_SL15_reverse_on_SL"
).strip()

SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# Demo by default (no real orders)
EXECUTION_ENABLED = (os.environ.get("EXECUTION_ENABLED") or "0").strip() == "1"

# Stop loss percent (15% = 0.15). Not tied to leverage.
SL_PCT = float(os.environ.get("SL_PCT", "0.15").strip())

# Symbols allowed (normalized)
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Upstash (REST)
UPSTASH_REDIS_REST_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").strip()
UPSTASH_REDIS_REST_TOKEN = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()

# TTLs
TTL_STATE_SEC = 60 * 60 * 24 * 7     # 7 days (position)
TTL_DEDUP_SEC = 60 * 30              # 30 min (dedup per bar)
TTL_LATCH_SEC = 60 * 30              # 30 min (latch per bar)
TTL_LOCK_SEC = 12                    # 12 sec (critical section lock)
TTL_BAR_DONE_SEC = 60 * 30           # 30 min (prevent multiple actions in same bar)

# Redis keys
# Position stores entry_price and sl_price
K_POS = "tvbotv2:pos:{sym}"  # json {"in_position":bool,"side":"LONG/SHORT","entry_price":float,"sl_price":float}
K_LOCK = "tvbotv2:lock:{sym}"
K_STOCH_LATCH = "tvbotv2:stoch_latch:{sym}:{bar_key_ms}"   # "1" (NX)
K_DEDUP = "tvbotv2:dedup:{sym}:{dedup_id}:{bar_key_ms}"    # "1" (NX)
K_BAR_DONE = "tvbotv2:bar_done:{sym}:{bar_key_ms}"         # "1" (NX) -> only one trade action per bar


# =========================
# UPSTASH HELPERS (COMMAND-BODY SAFE)
# =========================
def _upstash_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }

def _upstash_cmd(cmd: list) -> Tuple[Optional[Any], Optional[str]]:
    """
    Executes a Redis command through Upstash REST by sending the command in the request body.
    Returns: (result, error_str)
    """
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None, "UPSTASH_NOT_CONFIGURED"
    try:
        r = requests.post(
            UPSTASH_REDIS_REST_URL,
            headers=_upstash_headers(),
            data=json.dumps(cmd),
            timeout=10
        )
        j = r.json()
        if "error" in j and j["error"]:
            return None, str(j["error"])
        return j.get("result", None), None
    except Exception as e:
        return None, f"UPSTASH_EXCEPTION:{type(e).__name__}"

def upstash_get(key: str) -> Tuple[Optional[str], Optional[str]]:
    res, err = _upstash_cmd(["GET", key])
    if err:
        return None, err
    if res is None:
        return None, None
    return str(res), None

def upstash_set_ex(key: str, value: str, ex: int) -> Tuple[bool, Optional[str]]:
    res, err = _upstash_cmd(["SET", key, value, "EX", int(ex)])
    if err:
        return False, err
    return res is not None, None

def upstash_set_nx_ex(key: str, value: str, ex: int) -> Tuple[bool, Optional[str]]:
    """
    Atomic SET NX EX.
    Returns True if key was set (NEW), False if key already existed.
    """
    res, err = _upstash_cmd(["SET", key, value, "NX", "EX", int(ex)])
    if err:
        return False, err
    return (res == "OK"), None

def upstash_del(key: str) -> Tuple[bool, Optional[str]]:
    res, err = _upstash_cmd(["DEL", key])
    if err:
        return False, err
    return res is not None, None


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
      - payload['time_ms'] numeric string/int (epoch ms)
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
    Returns: (bar_time_ms, bar_key_ms, ticker_raw, sym)
    bar_key_ms is canonicalized on sym to avoid .P mismatch issues.
    """
    ticker_raw = str(payload.get("ticker") or "").strip().upper()
    tf = str(payload.get("tf") or "").strip()

    if not ticker_raw or not tf:
        return None, None, None, None

    sym = normalize_symbol(ticker_raw)

    tf_ms = tf_to_ms(tf)
    t_ms = parse_time_ms(payload)
    if tf_ms is None or t_ms is None:
        return None, None, ticker_raw, sym

    bar_time_ms = snap_to_bar(t_ms, tf_ms)
    bar_key_ms = f"{sym}|{tf}|{bar_time_ms}"
    return bar_time_ms, bar_key_ms, ticker_raw, sym


# =========================
# PRICE HELPERS
# =========================
def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        s = str(v).strip()
        if not s:
            return None
        return float(s)
    except Exception:
        return None

def calc_sl(side: str, entry_price: float) -> float:
    side = str(side).strip().upper()
    if side == "LONG":
        return entry_price * (1.0 - SL_PCT)
    return entry_price * (1.0 + SL_PCT)


# =========================
# STATE (POSITION)
# =========================
def get_pos(sym: str) -> Tuple[Dict[str, Any], Optional[str]]:
    raw, err = upstash_get(K_POS.format(sym=sym))
    if err:
        return {"in_position": False, "side": None, "entry_price": None, "sl_price": None}, err
    if not raw:
        return {"in_position": False, "side": None, "entry_price": None, "sl_price": None}, None
    try:
        j = json.loads(raw.strip())
        if isinstance(j, dict) and "in_position" in j:
            side = j.get("side")
            if isinstance(side, str):
                side = side.strip().upper()
            if side not in ("LONG", "SHORT"):
                side = None
            entry_price = _to_float(j.get("entry_price"))
            sl_price = _to_float(j.get("sl_price"))
            return {
                "in_position": bool(j.get("in_position")),
                "side": side,
                "entry_price": entry_price,
                "sl_price": sl_price,
            }, None
    except Exception:
        pass
    return {"in_position": False, "side": None, "entry_price": None, "sl_price": None}, None

def set_pos(sym: str, in_position: bool, side: Optional[str], entry_price: Optional[float], sl_price: Optional[float]) -> Tuple[bool, Optional[str]]:
    s = side.strip().upper() if isinstance(side, str) else None
    if s not in ("LONG", "SHORT"):
        s = None
    payload = {
        "in_position": bool(in_position),
        "side": s,
        "entry_price": float(entry_price) if entry_price is not None else None,
        "sl_price": float(sl_price) if sl_price is not None else None,
    }
    ok, err = upstash_set_ex(K_POS.format(sym=sym), json.dumps(payload), ex=TTL_STATE_SEC)
    return ok, err

def flat_pos(sym: str) -> Tuple[bool, Optional[str]]:
    return set_pos(sym, False, None, None, None)


# =========================
# SAFETY: LOCK / DEDUP / LATCH / BAR_DONE
# =========================
def acquire_lock(sym: str) -> Tuple[bool, Optional[str]]:
    return upstash_set_nx_ex(K_LOCK.format(sym=sym), "1", ex=TTL_LOCK_SEC)

def release_lock(sym: str) -> None:
    upstash_del(K_LOCK.format(sym=sym))

def dedup_event(sym: str, dedup_id: str, bar_key_ms: str) -> Tuple[bool, Optional[str]]:
    key = K_DEDUP.format(sym=sym, dedup_id=dedup_id, bar_key_ms=bar_key_ms)
    return upstash_set_nx_ex(key, "1", ex=TTL_DEDUP_SEC)

def stoch_latch(sym: str, bar_key_ms: str) -> Tuple[bool, Optional[str]]:
    key = K_STOCH_LATCH.format(sym=sym, bar_key_ms=bar_key_ms)
    return upstash_set_nx_ex(key, "1", ex=TTL_LATCH_SEC)

def claim_bar_done(sym: str, bar_key_ms: str) -> Tuple[bool, Optional[str]]:
    """
    Ensures only ONE trade action (enter/exit/flip) can happen per symbol per bar.
    Atomic: SET NX EX.
    """
    key = K_BAR_DONE.format(sym=sym, bar_key_ms=bar_key_ms)
    return upstash_set_nx_ex(key, "1", ex=TTL_BAR_DONE_SEC)


# =========================
# DEMO EXECUTION (no exchange calls)
# =========================
def demo_actions(actions: list, sym: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "demo": not EXECUTION_ENABLED,
        "actions": actions,
        "symbol": sym,
        "meta": meta,
        "bot_version": BOT_VERSION,
        "mode": "B1_STOCH_FLIP_SAFE_WITH_SL_REVERSE",
        "sl_pct": SL_PCT,
    }


# =========================
# CORE: STOP LOSS CHECK + REVERSE
# =========================
def sl_hit(side: str, sl_price: float, bar_high: float, bar_low: float) -> bool:
    side = str(side).strip().upper()
    if side == "LONG":
        return bar_low <= sl_price
    return bar_high >= sl_price  # SHORT

def opposite_side(side: str) -> str:
    return "SHORT" if str(side).strip().upper() == "LONG" else "LONG"

def handle_sl_reverse(sym: str, side: str, entry_price: Optional[float], sl_price: float,
                      bar_high: float, bar_low: float, bar_close: Optional[float],
                      bar_key_ms: str, ticker_raw: str) -> Tuple[Dict[str, Any], int]:
    """
    If SL hit: EXIT current side + ENTER opposite side immediately.
    Uses bar_close for new entry if available; otherwise falls back to sl_price.
    """
    new_side = opposite_side(side)
    new_entry = bar_close if bar_close is not None else sl_price
    new_sl = calc_sl(new_side, new_entry)

    # Only one action per bar
    ok, err = claim_bar_done(sym, bar_key_ms)
    if err:
        return ({"ok": False, "error": "Upstash error (bar_done)", "detail": err}, 503)
    if not ok:
        return ({
            "ok": True,
            "ignored": True,
            "reason": "BAR_ALREADY_ACTED",
            "symbol": sym,
            "bar_key_ms": bar_key_ms
        }, 200)

    # Persist new position (reversed)
    ok, err = set_pos(sym, True, new_side, new_entry, new_sl)
    if err:
        return ({"ok": False, "error": "Upstash error (set_pos)", "detail": err}, 503)

    actions = [f"EXIT_{side}", f"ENTER_{new_side}"]
    meta = {
        "trigger": "STOP_LOSS_REVERSE",
        "from": side,
        "to": new_side,
        "prev_entry_price": entry_price,
        "prev_sl_price": sl_price,
        "bar_high": bar_high,
        "bar_low": bar_low,
        "bar_close_used": new_entry,
        "new_entry_price": new_entry,
        "new_sl_price": new_sl,
        "bar_key_ms": bar_key_ms,
        "ticker": ticker_raw,
    }
    return (demo_actions(actions, sym, meta), 200)


# =========================
# ROUTES
# =========================
@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "bot_version": BOT_VERSION,
        "execution_enabled": EXECUTION_ENABLED,
        "mode": "B1_STOCH_FLIP_SAFE_WITH_SL_REVERSE",
        "allowed_symbols": sorted(list(ALLOWED_SYMBOLS)),
        "sl_pct": SL_PCT,
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

    bar_time_ms, bar_key_ms, ticker_raw, sym = normalize_bar(payload)
    if sym is None or ticker_raw is None:
        return jsonify({"ok": False, "error": "Missing ticker/tf"}), 400

    if sym not in ALLOWED_SYMBOLS:
        return jsonify({"ok": True, "ignored": True, "reason": "IGNORED_SYMBOL", "symbol": sym, "ticker": ticker_raw}), 200

    if bar_key_ms is None:
        return jsonify({"ok": False, "error": "Could not normalize time (need time_ms numeric or time ISO)"}), 400

    # Basic log
    print("ALERTE:", payload)
    print("NORM:", {"sym": sym, "ticker": ticker_raw, "bar_time_ms": bar_time_ms, "bar_key_ms": bar_key_ms, "event": event})

    # Allowed events
    if event not in ("STOCH_SIGNAL", "BAR_CLOSE"):
        return jsonify({"ok": True, "ignored": True, "reason": "UNKNOWN_EVENT", "event": event}), 200

    # =========================
    # DEDUP + (STOCH) LATCH
    # =========================
    if event == "STOCH_SIGNAL":
        reason = str(payload.get("reason") or "").strip().upper()
        if reason not in ("LD", "HD"):
            return jsonify({"ok": False, "error": "Invalid STOCH reason"}), 400

        is_new, err = dedup_event(sym, f"STOCH:{reason}", bar_key_ms)
        if err:
            return jsonify({"ok": False, "error": "Upstash error (dedup)", "detail": err}), 503
        if not is_new:
            return jsonify({"ok": True, "ignored": True, "reason": "DUPLICATE_EVENT", "dedup": f"STOCH:{reason}", "bar_key_ms": bar_key_ms}), 200

        first, err = stoch_latch(sym, bar_key_ms)
        if err:
            return jsonify({"ok": False, "error": "Upstash error (latch)", "detail": err}), 503
        if not first:
            return jsonify({"ok": True, "ignored": True, "reason": "STOCH_LATCHED_THIS_BAR", "bar_key_ms": bar_key_ms}), 200

    else:
        is_new, err = dedup_event(sym, "BAR_CLOSE", bar_key_ms)
        if err:
            return jsonify({"ok": False, "error": "Upstash error (dedup)", "detail": err}), 503
        if not is_new:
            return jsonify({"ok": True, "ignored": True, "reason": "DUPLICATE_EVENT", "dedup": "BAR_CLOSE", "bar_key_ms": bar_key_ms}), 200

    # =========================
    # LOCK per symbol
    # =========================
    locked, err = acquire_lock(sym)
    if err:
        return jsonify({"ok": False, "error": "Upstash error (lock)", "detail": err}), 503
    if not locked:
        return jsonify({"ok": True, "ignored": True, "reason": "SYMBOL_BUSY_LOCKED", "symbol": sym}), 200

    try:
        # Load position
        pos, err = get_pos(sym)
        if err:
            return jsonify({"ok": False, "error": "Upstash error (get_pos)", "detail": err}), 503

        in_pos = bool(pos.get("in_position"))
        side = str(pos.get("side") or "").strip().upper() if pos.get("side") else None
        entry_price = _to_float(pos.get("entry_price"))
        sl_price = _to_float(pos.get("sl_price"))

        # Read prices from payload (support both events)
        bar_high = _to_float(payload.get("high"))
        bar_low = _to_float(payload.get("low"))
        bar_close = _to_float(payload.get("close"))

        # ==========================================================
        # 1) STOP LOSS PRIORITY (ON ANY EVENT IF high/low PRESENT)
        #    - If SL hit: reverse immediately (exit + enter opposite).
        #    - This is critical for "once per bar" because order of webhooks is not guaranteed.
        # ==========================================================
        if in_pos and side in ("LONG", "SHORT") and sl_price is not None:
            if bar_high is not None and bar_low is not None:
                if sl_hit(side, sl_price, bar_high, bar_low):
                    resp, code = handle_sl_reverse(
                        sym=sym,
                        side=side,
                        entry_price=entry_price,
                        sl_price=sl_price,
                        bar_high=bar_high,
                        bar_low=bar_low,
                        bar_close=bar_close,
                        bar_key_ms=bar_key_ms,
                        ticker_raw=ticker_raw,
                    )
                    return jsonify(resp), code

        # If BAR_CLOSE but no SL hit (or missing high/low), do nothing else on BAR_CLOSE
        if event == "BAR_CLOSE":
            if not in_pos or side not in ("LONG", "SHORT") or sl_price is None:
                return jsonify({
                    "ok": True,
                    "ignored": True,
                    "reason": "NO_POSITION_OR_NO_SL",
                    "symbol": sym,
                    "bar_key_ms": bar_key_ms,
                }), 200
            if bar_high is None or bar_low is None:
                return jsonify({
                    "ok": True,
                    "ignored": True,
                    "reason": "MISSING_HIGH_LOW_FOR_SL",
                    "symbol": sym,
                    "bar_key_ms": bar_key_ms,
                }), 200
            return jsonify({
                "ok": True,
                "ignored": True,
                "reason": "SL_NOT_HIT",
                "symbol": sym,
                "pos": {"in_position": in_pos, "side": side, "entry_price": entry_price, "sl_price": sl_price},
                "bar": {"high": bar_high, "low": bar_low, "close": bar_close},
                "bar_key_ms": bar_key_ms
            }), 200

        # ==========================================================
        # 2) STOCH ENTRY + STOCH FLIP (only if bar not already acted)
        # ==========================================================
        reason = str(payload.get("reason") or "").strip().upper()  # LD/HD validated earlier

        # Need close for entries/flips (new entry price)
        if bar_close is None:
            return jsonify({
                "ok": True,
                "ignored": True,
                "reason": "MISSING_CLOSE_FOR_ENTRY",
                "symbol": sym,
                "bar_key_ms": bar_key_ms
            }), 200

        # If already acted in this bar (e.g., other event), ignore
        # Note: We do NOT claim bar_done here unless we actually take an action.
        # We read it by attempting to claim right before action.

        # FLAT entry
        if not in_pos or side not in ("LONG", "SHORT"):
            if reason == "LD":
                new_side = "LONG"
                new_entry = bar_close
                new_sl = calc_sl(new_side, new_entry)

                ok, err = claim_bar_done(sym, bar_key_ms)
                if err:
                    return jsonify({"ok": False, "error": "Upstash error (bar_done)", "detail": err}), 503
                if not ok:
                    return jsonify({"ok": True, "ignored": True, "reason": "BAR_ALREADY_ACTED", "symbol": sym, "bar_key_ms": bar_key_ms}), 200

                ok, err = set_pos(sym, True, new_side, new_entry, new_sl)
                if err:
                    return jsonify({"ok": False, "error": "Upstash error (set_pos)", "detail": err}), 503

                return jsonify(demo_actions(
                    ["ENTER_LONG"],
                    sym,
                    {
                        "trigger": "STOCH_LD_PRIMARY",
                        "entry_price": new_entry,
                        "sl_price": new_sl,
                        "bar_key_ms": bar_key_ms,
                        "ticker": ticker_raw
                    }
                )), 200

            # reason == "HD"
            new_side = "SHORT"
            new_entry = bar_close
            new_sl = calc_sl(new_side, new_entry)

            ok, err = claim_bar_done(sym, bar_key_ms)
            if err:
                return jsonify({"ok": False, "error": "Upstash error (bar_done)", "detail": err}), 503
            if not ok:
                return jsonify({"ok": True, "ignored": True, "reason": "BAR_ALREADY_ACTED", "symbol": sym, "bar_key_ms": bar_key_ms}), 200

            ok, err = set_pos(sym, True, new_side, new_entry, new_sl)
            if err:
                return jsonify({"ok": False, "error": "Upstash error (set_pos)", "detail": err}), 503

            return jsonify(demo_actions(
                ["ENTER_SHORT"],
                sym,
                {
                    "trigger": "STOCH_HD_PRIMARY",
                    "entry_price": new_entry,
                    "sl_price": new_sl,
                    "bar_key_ms": bar_key_ms,
                    "ticker": ticker_raw
                }
            )), 200

        # In-position flip on opposite stoch
        if side == "LONG" and reason == "HD":
            new_side = "SHORT"
            new_entry = bar_close
            new_sl = calc_sl(new_side, new_entry)

            ok, err = claim_bar_done(sym, bar_key_ms)
            if err:
                return jsonify({"ok": False, "error": "Upstash error (bar_done)", "detail": err}), 503
            if not ok:
                return jsonify({"ok": True, "ignored": True, "reason": "BAR_ALREADY_ACTED", "symbol": sym, "bar_key_ms": bar_key_ms}), 200

            ok, err = set_pos(sym, True, new_side, new_entry, new_sl)
            if err:
                return jsonify({"ok": False, "error": "Upstash error (set_pos)", "detail": err}), 503

            return jsonify(demo_actions(
                ["EXIT_LONG", "ENTER_SHORT"],
                sym,
                {
                    "trigger": "STOCH_HD_FLIP",
                    "from": "LONG",
                    "to": "SHORT",
                    "entry_price": new_entry,
                    "sl_price": new_sl,
                    "bar_key_ms": bar_key_ms,
                    "ticker": ticker_raw
                }
            )), 200

        if side == "SHORT" and reason == "LD":
            new_side = "LONG"
            new_entry = bar_close
            new_sl = calc_sl(new_side, new_entry)

            ok, err = claim_bar_done(sym, bar_key_ms)
            if err:
                return jsonify({"ok": False, "error": "Upstash error (bar_done)", "detail": err}), 503
            if not ok:
                return jsonify({"ok": True, "ignored": True, "reason": "BAR_ALREADY_ACTED", "symbol": sym, "bar_key_ms": bar_key_ms}), 200

            ok, err = set_pos(sym, True, new_side, new_entry, new_sl)
            if err:
                return jsonify({"ok": False, "error": "Upstash error (set_pos)", "detail": err}), 503

            return jsonify(demo_actions(
                ["EXIT_SHORT", "ENTER_LONG"],
                sym,
                {
                    "trigger": "STOCH_LD_FLIP",
                    "from": "SHORT",
                    "to": "LONG",
                    "entry_price": new_entry,
                    "sl_price": new_sl,
                    "bar_key_ms": bar_key_ms,
                    "ticker": ticker_raw
                }
            )), 200

        return jsonify({
            "ok": True,
            "ignored": True,
            "reason": "STOCH_NOT_OPPOSITE",
            "symbol": sym,
            "pos": {"in_position": in_pos, "side": side, "entry_price": entry_price, "sl_price": sl_price},
            "stoch_reason": reason,
            "bar_key_ms": bar_key_ms
        }), 200

    finally:
        release_lock(sym)


# =========================
# DEBUG ENDPOINTS
# =========================
@app.get("/debug/state/<symbol>")
def debug_state(symbol: str):
    sym = symbol.strip().upper().replace(".P", "")
    if sym not in ALLOWED_SYMBOLS:
        return jsonify({"ok": False, "error": "Unknown symbol"}), 400

    pos, err = get_pos(sym)
    if err:
        return jsonify({"ok": False, "error": "Upstash error (get_pos)", "detail": err}), 503

    return jsonify({
        "ok": True,
        "endpoint": f"/debug/state/{sym}",
        "symbol": sym,
        "pos": pos,
        "sl_pct": SL_PCT,
        "mode": "B1_STOCH_FLIP_SAFE_WITH_SL_REVERSE",
        "bot_version": BOT_VERSION,
    }), 200

@app.get("/debug/bitmart")
def debug_bitmart():
    return jsonify({
        "ok": True,
        "endpoint": "/debug/bitmart",
        "note": "This build does not execute real orders unless EXECUTION_ENABLED=1",
        "execution_enabled": EXECUTION_ENABLED,
        "sl_pct": SL_PCT,
        "mode": "B1_STOCH_FLIP_SAFE_WITH_SL_REVERSE",
        "bot_version": BOT_VERSION,
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
