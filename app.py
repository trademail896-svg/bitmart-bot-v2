# app.py
from flask import Flask, request, jsonify
import os
import json
import time
import hmac
import hashlib
import requests
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

app = Flask(__name__)

# =========================
# CONFIG
# =========================
BOT_VERSION = os.environ.get(
    "BOT_VERSION",
    "TV_BOT_DEMO_2026_V2_B1_stoch_flip_safe_with_SL15_reverse_on_SL"
).strip()

SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# Real orders only if EXECUTION_ENABLED=1
EXECUTION_ENABLED = (os.environ.get("EXECUTION_ENABLED") or "0").strip() == "1"

# TradingView tickers
ALLOWED_TICKERS = {"BTCUSDT.P", "ETHUSDT.P", "SOLUSDT.P"}
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Stop loss %
SL_PCT = float((os.environ.get("SL_PCT") or "0.15").strip())  # 0.15 = 15%

# BitMart Futures V2
# Demo: https://demo-api-cloud-v2.bitmart.com
# Live: https://api-cloud-v2.bitmart.com
BITMART_BASE_URL = (os.environ.get("BITMART_BASE_URL") or "https://demo-api-cloud-v2.bitmart.com").strip()
BITMART_API_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_API_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_API_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()  # required for signature scheme used here

LEVERAGE = (os.environ.get("LEVERAGE") or "25").strip()
OPEN_TYPE = (os.environ.get("OPEN_TYPE") or "isolated").strip()  # isolated/cross
ACCOUNT = (os.environ.get("BITMART_ACCOUNT") or "futures").strip()

# Size (contracts). If not specified, default 1 (safer for demo; set explicitly for live).
BM_SIZE_DEFAULT = int((os.environ.get("BM_SIZE_DEFAULT") or "1").strip() or "1")

# Upstash (REST)
UPSTASH_REDIS_REST_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").strip()
UPSTASH_REDIS_REST_TOKEN = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()

TTL_STATE_SEC = 60 * 60 * 24 * 7   # 7 days
TTL_DEDUP_SEC = 60 * 60 * 6        # 6 hours

# Redis keys
K_POS = "tvbotv2:pos:{sym}"              # json {"in_position":bool,"side":"LONG/SHORT","entry_price":float}
K_DEDUP = "tvbotv2:dedup:{sym}:{k}"      # "1"


# =========================
# UPSTASH HELPERS (COMMAND-BODY SAFE)
# =========================
def _upstash_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }

def _upstash_cmd(cmd: list) -> Optional[Any]:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        r = requests.post(
            UPSTASH_REDIS_REST_URL,
            headers=_upstash_headers(),
            data=json.dumps(cmd),
            timeout=10
        )
        j = r.json()
        return j.get("result", None)
    except Exception:
        return None

def upstash_get(key: str) -> Optional[str]:
    res = _upstash_cmd(["GET", key])
    if res is None:
        return None
    return str(res)

def upstash_set(key: str, value: str, ex: int = TTL_STATE_SEC) -> bool:
    res = _upstash_cmd(["SET", key, value, "EX", int(ex)])
    return res is not None

def upstash_del(key: str) -> bool:
    res = _upstash_cmd(["DEL", key])
    return res is not None


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
    ticker = str(payload.get("ticker") or "").strip().upper()
    tf = str(payload.get("tf") or "").strip()

    if not ticker or not tf:
        return None, None, None, None

    sym = normalize_symbol(ticker)

    tf_ms = tf_to_ms(tf)
    t_ms = parse_time_ms(payload)
    if tf_ms is None or t_ms is None:
        return None, None, ticker, sym

    bar_time_ms = snap_to_bar(t_ms, tf_ms)
    bar_key_ms = f"{ticker}|{tf}|{bar_time_ms}"
    return bar_time_ms, bar_key_ms, ticker, sym


# =========================
# STATE (POSITION)
# =========================
def get_pos(sym: str) -> Dict[str, Any]:
    raw = upstash_get(K_POS.format(sym=sym))
    if not raw:
        return {"in_position": False, "side": None, "entry_price": None}
    try:
        j = json.loads(raw.strip())
        if isinstance(j, dict) and "in_position" in j:
            side = j.get("side")
            if isinstance(side, str):
                side = side.strip().upper()
            if side not in ("LONG", "SHORT"):
                side = None
            ep = j.get("entry_price")
            try:
                ep = float(ep) if ep is not None else None
            except Exception:
                ep = None
            return {"in_position": bool(j.get("in_position")), "side": side, "entry_price": ep}
    except Exception:
        pass
    return {"in_position": False, "side": None, "entry_price": None}

def set_pos(sym: str, in_position: bool, side: Optional[str], entry_price: Optional[float]) -> None:
    s = side.strip().upper() if isinstance(side, str) else None
    if s not in ("LONG", "SHORT"):
        s = None
    ep = None
    if entry_price is not None:
        try:
            ep = float(entry_price)
        except Exception:
            ep = None
    upstash_set(
        K_POS.format(sym=sym),
        json.dumps({"in_position": bool(in_position), "side": s, "entry_price": ep}),
        ex=TTL_STATE_SEC
    )

def clear_pos(sym: str) -> None:
    upstash_del(K_POS.format(sym=sym))


# =========================
# DEDUP (Upstash REST)
# =========================
def dedup(sym: str, k: str) -> bool:
    """
    Returns True if NEW (process), False if DUP (ignore).
    Safe against webhook retries.
    """
    key = K_DEDUP.format(sym=sym, k=k)
    existing = upstash_get(key)
    if existing is not None:
        return False
    upstash_set(key, "1", ex=TTL_DEDUP_SEC)
    return True


# =========================
# BITMART SIGNING + REQUESTS
# =========================
def bitmart_ready() -> bool:
    return bool(BITMART_BASE_URL and BITMART_API_KEY and BITMART_API_SECRET and BITMART_API_MEMO)

def _bm_ts() -> str:
    return str(int(time.time() * 1000))

def _bm_sign(ts: str, query_string: str) -> str:
    # HMAC_SHA256(secret, ts + "#" + memo + "#" + queryString)
    prehash = f"{ts}#{BITMART_API_MEMO}#{query_string}"
    return hmac.new(
        BITMART_API_SECRET.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def bm_request(method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
               body: Optional[Dict[str, Any]] = None, signed: bool = True) -> Dict[str, Any]:
    url = BITMART_BASE_URL.rstrip("/") + path
    headers = {"Content-Type": "application/json", "X-BM-KEY": BITMART_API_KEY}

    query_string = ""
    if method.upper() == "GET":
        if params:
            query_string = urlencode(list(params.items()))
            url = url + "?" + query_string
        else:
            query_string = ""
    else:
        if body is None:
            body = {}
        query_string = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    if signed:
        ts = _bm_ts()
        headers["X-BM-TIMESTAMP"] = ts
        headers["X-BM-SIGN"] = _bm_sign(ts, query_string)

    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, timeout=15)
        else:
            r = requests.post(url, headers=headers, data=query_string, timeout=15)
        j = r.json() if r.text else {}
        return {"http": r.status_code, "json": j, "text": r.text, "url": url}
    except Exception as e:
        return {"http": 0, "json": {"code": -1, "message": f"REQUEST_ERROR: {e}"}, "text": "", "url": url}

def bm_submit_order(sym: str, side_int: int, size: int) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "symbol": sym,
        "side": int(side_int),
        "type": "market",
        "leverage": str(LEVERAGE),
        "open_type": str(OPEN_TYPE),
        "size": int(size),
        "mode": 1
    }
    return bm_request("POST", "/contract/private/submit-order", body=body, signed=True)

def bm_get_position_v2(sym: str) -> Dict[str, Any]:
    # Some accounts require signing even for position queries; keep signed=True.
    params = {"symbol": sym, "account": ACCOUNT}
    return bm_request("GET", "/contract/private/position-v2", params=params, signed=True)

def get_trade_size(sym: str) -> int:
    env_k = f"BM_SIZE_{sym}"
    v = (os.environ.get(env_k) or "").strip()
    if v.isdigit() and int(v) > 0:
        return int(v)
    return int(BM_SIZE_DEFAULT) if BM_SIZE_DEFAULT > 0 else 0


# =========================
# EXECUTION WRAPPERS
# =========================
def exec_response(actions: list, sym: str, meta: Dict[str, Any], bitmart: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = {
        "ok": True,
        "demo": (not EXECUTION_ENABLED),
        "actions": actions,
        "symbol": sym,
        "meta": meta,
        "bot_version": BOT_VERSION,
        "mode": "B1_STOCH_FLIP_SAFE_WITH_SL_REVERSE",
        "sl_pct": SL_PCT
    }
    if bitmart is not None:
        out["bitmart"] = bitmart
    return out

def open_side(sym: str, side: str, entry_price: float) -> Tuple[bool, Dict[str, Any]]:
    """
    side: LONG/SHORT
      open LONG  => side_int=1
      open SHORT => side_int=4
    """
    if not EXECUTION_ENABLED:
        set_pos(sym, True, side, entry_price)
        return True, {"demo": True}

    if not bitmart_ready():
        return False, {"error": "BITMART_NOT_CONFIGURED (need BITMART_API_KEY/SECRET/MEMO + BITMART_BASE_URL)"}

    size = get_trade_size(sym)
    if size <= 0:
        return False, {"error": f"MISSING_SIZE (set BM_SIZE_{sym} or BM_SIZE_DEFAULT)"}

    side_int = 1 if side == "LONG" else 4
    bm = bm_submit_order(sym, side_int=side_int, size=size)
    ok = (bm.get("json") or {}).get("code") == 1000
    if ok:
        set_pos(sym, True, side, entry_price)
    return ok, bm

def close_side(sym: str, side: str) -> Tuple[bool, Dict[str, Any]]:
    """
    side: LONG/SHORT currently open
      close LONG  => side_int=3
      close SHORT => side_int=2
    """
    if not EXECUTION_ENABLED:
        set_pos(sym, False, None, None)
        return True, {"demo": True}

    if not bitmart_ready():
        return False, {"error": "BITMART_NOT_CONFIGURED (need BITMART_API_KEY/SECRET/MEMO + BITMART_BASE_URL)"}

    # Try to close actual open amount from position-v2; fallback to configured size.
    size = 0
    pv2 = bm_get_position_v2(sym)
    if (pv2.get("json") or {}).get("code") == 1000:
        data = (pv2.get("json") or {}).get("data") or []
        if isinstance(data, list) and len(data) > 0:
            try:
                ca = float(data[0].get("current_amount", "0"))
                size = int(abs(ca))
            except Exception:
                size = 0

    if size <= 0:
        size = get_trade_size(sym)

    if size <= 0:
        return False, {"error": f"MISSING_SIZE (set BM_SIZE_{sym} or BM_SIZE_DEFAULT)", "position_v2": pv2}

    side_int = 3 if side == "LONG" else 2
    bm = bm_submit_order(sym, side_int=side_int, size=size)
    ok = (bm.get("json") or {}).get("code") == 1000
    if ok:
        set_pos(sym, False, None, None)
    return ok, bm


# =========================
# STRATEGY HELPERS
# =========================
def fnum(payload: Dict[str, Any], k: str) -> Optional[float]:
    v = payload.get(k)
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except Exception:
        return None

def sl_hit(side: str, entry_price: float, bar_high: float, bar_low: float) -> bool:
    if entry_price <= 0:
        return False
    if side == "LONG":
        stop = entry_price * (1.0 - SL_PCT)
        return bar_low <= stop
    else:
        stop = entry_price * (1.0 + SL_PCT)
        return bar_high >= stop


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
        "upstash_configured": bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN),
        "sl_pct": SL_PCT
    }), 200


@app.post("/webhook")
def webhook():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    if str(payload.get("secret", "")).strip() != SECRET:
        return jsonify({"ok": False, "error": "Bad secret"}), 403

    event = str(payload.get("event") or "").strip().upper()
    if not event:
        return jsonify({"ok": False, "error": "Missing event"}), 400

    bar_time_ms, bar_key_ms, ticker, sym = normalize_bar(payload)
    if sym is None or ticker is None:
        return jsonify({"ok": False, "error": "Missing ticker/tf"}), 400

    if str(ticker).strip().upper() not in ALLOWED_TICKERS:
        return jsonify({"ok": True, "ignored": True, "reason": "IGNORED_TICKER", "ticker": ticker}), 200

    if sym not in ALLOWED_SYMBOLS:
        return jsonify({"ok": True, "ignored": True, "reason": "IGNORED_SYMBOL", "symbol": sym}), 200

    if bar_key_ms is None:
        return jsonify({"ok": False, "error": "Could not normalize time (need time_ms numeric or time ISO)"}), 400

    print("ALERTE:", payload)
    print("NORM:", {"sym": sym, "ticker": ticker, "bar_time_ms": bar_time_ms, "bar_key_ms": bar_key_ms, "event": event})

    # =========================
    # STOCH_SIGNAL (LD/HD): ENTRY or FLIP
    # =========================
    if event == "STOCH_SIGNAL":
        reason = str(payload.get("reason") or "").strip().upper()
        if reason not in ("LD", "HD"):
            return jsonify({"ok": False, "error": "Invalid STOCH reason"}), 400

        # Dedup per bar+reason (prevents webhook retry duplicates)
        if not dedup(sym, f"STOCH:{reason}:{bar_key_ms}"):
            return jsonify({"ok": True, "ignored": True, "reason": "DUPLICATE_RETRY", "bar_key_ms": bar_key_ms}), 200

        close_p = fnum(payload, "close")
        if close_p is None or close_p <= 0:
            return jsonify({"ok": False, "error": "Missing/invalid close"}), 400

        desired = "LONG" if reason == "LD" else "SHORT"
        pos = get_pos(sym)
        in_pos = bool(pos.get("in_position"))
        side = pos.get("side")

        # FLAT => enter
        if (not in_pos) or (side not in ("LONG", "SHORT")):
            ok, bm = open_side(sym, desired, close_p)
            if ok:
                return jsonify(exec_response(
                    [f"ENTER_{desired}"],
                    sym,
                    {"trigger": f"STOCH_{reason}_ENTRY", "bar_key_ms": bar_key_ms, "entry_price": close_p},
                    bitmart=bm
                )), 200
            return jsonify({"ok": False, "error": "OPEN_FAILED", "symbol": sym, "desired": desired, "bitmart": bm}), 500

        # Same side => ignore
        if side == desired:
            return jsonify({"ok": True, "ignored": True, "reason": "ALREADY_IN_SIDE", "pos": pos, "bar_key_ms": bar_key_ms}), 200

        # Opposite => FLIP (close then open)
        okc, bmc = close_side(sym, side)
        if not okc:
            return jsonify({"ok": False, "error": "CLOSE_FAILED", "symbol": sym, "bitmart": bmc}), 500

        oko, bmo = open_side(sym, desired, close_p)
        if not oko:
            return jsonify({"ok": False, "error": "OPEN_FAILED_AFTER_CLOSE", "symbol": sym, "bitmart": bmo}), 500

        return jsonify(exec_response(
            [f"EXIT_{side}", f"ENTER_{desired}"],
            sym,
            {"trigger": f"STOCH_{reason}_FLIP", "from": side, "to": desired, "bar_key_ms": bar_key_ms, "entry_price": close_p},
            bitmart={"close": bmc, "open": bmo}
        )), 200

    # =========================
    # BAR_CLOSE: SL check + reverse on SL
    # =========================
    if event == "BAR_CLOSE":
        if not dedup(sym, f"BAR_CLOSE:{bar_key_ms}"):
            return jsonify({"ok": True, "ignored": True, "reason": "DUPLICATE_RETRY", "bar_key_ms": bar_key_ms}), 200

        pos = get_pos(sym)
        in_pos = bool(pos.get("in_position"))
        side = pos.get("side")
        entry_price = pos.get("entry_price")

        if (not in_pos) or (side not in ("LONG", "SHORT")) or (entry_price is None):
            return jsonify({"ok": True, "noop": True, "reason": "NO_POSITION", "bar_key_ms": bar_key_ms}), 200

        hi = fnum(payload, "high")
        lo = fnum(payload, "low")
        close_p = fnum(payload, "close")

        if hi is None or lo is None or close_p is None:
            return jsonify({"ok": False, "error": "BAR_CLOSE missing high/low/close"}), 400

        if not sl_hit(side, float(entry_price), float(hi), float(lo)):
            return jsonify({"ok": True, "noop": True, "reason": "SL_NOT_HIT", "pos": pos, "bar_key_ms": bar_key_ms}), 200

        reverse = "SHORT" if side == "LONG" else "LONG"

        okc, bmc = close_side(sym, side)
        if not okc:
            return jsonify({"ok": False, "error": "SL_CLOSE_FAILED", "symbol": sym, "bitmart": bmc}), 500

        oko, bmo = open_side(sym, reverse, float(close_p))
        if not oko:
            return jsonify({"ok": False, "error": "SL_REVERSE_OPEN_FAILED", "symbol": sym, "bitmart": bmo}), 500

        return jsonify(exec_response(
            [f"EXIT_{side}_SL", f"ENTER_{reverse}_REVERSE"],
            sym,
            {
                "trigger": "STOP_LOSS_15PCT_REVERSE",
                "from": side,
                "to": reverse,
                "entry_price_before": entry_price,
                "bar_high": hi,
                "bar_low": lo,
                "bar_close": close_p,
                "bar_key_ms": bar_key_ms
            },
            bitmart={"close": bmc, "open": bmo}
        )), 200

    return jsonify({"ok": True, "ignored": True, "reason": "UNKNOWN_EVENT", "event": event}), 200


# =========================
# DEBUG ENDPOINTS
# =========================
@app.get("/debug/state/<symbol>")
def debug_state(symbol: str):
    sym = symbol.strip().upper().replace(".P", "")
    if sym not in ALLOWED_SYMBOLS:
        return jsonify({"ok": False, "error": "Unknown symbol"}), 400
    return jsonify({
        "ok": True,
        "endpoint": f"/debug/state/{sym}",
        "symbol": sym,
        "pos": get_pos(sym),
        "execution_enabled": EXECUTION_ENABLED,
        "sl_pct": SL_PCT,
        "mode": "B1_STOCH_FLIP_SAFE_WITH_SL_REVERSE",
        "bot_version": BOT_VERSION
    }), 200

@app.get("/debug/bitmart")
def debug_bitmart():
    return jsonify({
        "ok": True,
        "endpoint": "/debug/bitmart",
        "execution_enabled": EXECUTION_ENABLED,
        "bitmart_configured": bitmart_ready(),
        "bitmart_base_url": BITMART_BASE_URL,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "account": ACCOUNT,
        "bm_size_default": BM_SIZE_DEFAULT,
        "note": "Real orders require EXECUTION_ENABLED=1 + BITMART_API_KEY/SECRET/MEMO configured.",
        "mode": "B1_STOCH_FLIP_SAFE_WITH_SL_REVERSE",
        "bot_version": BOT_VERSION
    }), 200

@app.get("/debug/bitmart/position/<symbol>")
def debug_bitmart_position(symbol: str):
    sym = symbol.strip().upper().replace(".P", "")
    if sym not in ALLOWED_SYMBOLS:
        return jsonify({"ok": False, "error": "Unknown symbol"}), 400
    if not bitmart_ready():
        return jsonify({"ok": False, "error": "BITMART_NOT_CONFIGURED"}), 400
    pv2 = bm_get_position_v2(sym)
    return jsonify({"ok": True, "symbol": sym, "position_v2": pv2}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
