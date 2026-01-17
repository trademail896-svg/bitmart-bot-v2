from flask import Flask, request, jsonify
import os, time, json, hmac, hashlib, math, requests
from typing import Optional, Dict, Any, Tuple, List

app = Flask(__name__)

# ================= STRATEGIE =================
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

LONG_COLORS = {"green", "blue"}
SHORT_COLORS = {"red", "purple"}

SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

STATE: Dict[str, Dict[str, Any]] = {
    s: {"in_position": False, "side": None, "last_entry_bar_id": None}
    for s in ALLOWED_SYMBOLS
}

# BIAS persistant via Upstash Redis
BIAS: Dict[str, str] = {s: "NONE" for s in ALLOWED_SYMBOLS}

# ================= UPSTASH REDIS (REST) =================
# Supporte plusieurs noms d'env vars (au cas où)
UPSTASH_REDIS_REST_URL = (
    (os.environ.get("UPSTASH_REDIS_REST_URL") or "").strip()
    or (os.environ.get("UPSTASH_REST_URL") or "").strip()
)
UPSTASH_REDIS_REST_TOKEN = (
    (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    or (os.environ.get("UPSTASH_REST_TOKEN") or "").strip()
)

UPSTASH_ENABLED = bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)
UPSTASH_TIMEOUT = float((os.environ.get("UPSTASH_TIMEOUT") or "5").strip())

# On évite les hits Upstash trop fréquents
BIAS_REDIS_REFRESH_TTL = int(float((os.environ.get("BIAS_REDIS_REFRESH_TTL") or "30").strip()))
LAST_BIAS_REDIS_FETCH_TS: Dict[str, int] = {s: 0 for s in ALLOWED_SYMBOLS}

REDIS_KEY_PREFIX = (os.environ.get("REDIS_KEY_PREFIX") or "tvbotv2").strip()

def redis_bias_key(symbol: str) -> str:
    return f"{REDIS_KEY_PREFIX}:bias:{symbol}"

def upstash_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}

def upstash_get(key: str) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """
    GET key via Upstash REST: /get/<key>
    Réponse JSON: {"result": "..."} ou {"result": null}
    """
    if not UPSTASH_ENABLED:
        return False, None, {"error": "upstash_not_configured"}

    url = f"{UPSTASH_REDIS_REST_URL}/get/{key}"
    try:
        r = requests.get(url, headers=upstash_headers(), timeout=UPSTASH_TIMEOUT)
        try:
            j = r.json()
        except Exception:
            return False, None, {"http": r.status_code, "text": r.text}

        if r.status_code != 200:
            return False, None, {"http": r.status_code, "json": j}

        val = j.get("result", None)
        if val is None:
            return True, None, {"http": r.status_code, "json": j}
        return True, str(val), {"http": r.status_code, "json": j}
    except Exception as e:
        return False, None, {"http": 0, "error": str(e)}

def upstash_set(key: str, value: str) -> Tuple[bool, Dict[str, Any]]:
    """
    SET key value via Upstash REST: /set/<key>/<value>
    Réponse JSON: {"result":"OK"}
    """
    if not UPSTASH_ENABLED:
        return False, {"error": "upstash_not_configured"}

    # value doit être URL-safe; on encode proprement via requests (en utilisant path directement c'est ok pour LONG/SHORT/NONE)
    url = f"{UPSTASH_REDIS_REST_URL}/set/{key}/{value}"
    try:
        r = requests.get(url, headers=upstash_headers(), timeout=UPSTASH_TIMEOUT)
        try:
            j = r.json()
        except Exception:
            return False, {"http": r.status_code, "text": r.text}

        if r.status_code != 200:
            return False, {"http": r.status_code, "json": j}

        return True, {"http": r.status_code, "json": j}
    except Exception as e:
        return False, {"http": 0, "error": str(e)}

def bias_set_local(symbol: str, bias: str) -> None:
    b = (bias or "").upper().strip()
    if b not in {"LONG", "SHORT", "NONE"}:
        return
    BIAS[symbol] = b

def bias_save_to_redis(symbol: str) -> None:
    if not UPSTASH_ENABLED:
        return
    key = redis_bias_key(symbol)
    val = BIAS.get(symbol, "NONE")
    ok, dbg = upstash_set(key, val)
    print("UPSTASH SET:", {"symbol": symbol, "key": key, "val": val, "ok": ok, "dbg": dbg}, flush=True)

def bias_load_from_redis(symbol: str, force: bool = False) -> None:
    """
    Recharge BIAS[symbol] depuis Redis.
    - On ne spam pas Upstash : TTL par symbole.
    - force=True ignore TTL.
    """
    if not UPSTASH_ENABLED:
        return
    now = int(time.time())
    if not force:
        last = int(LAST_BIAS_REDIS_FETCH_TS.get(symbol, 0))
        if (now - last) < BIAS_REDIS_REFRESH_TTL:
            return
    LAST_BIAS_REDIS_FETCH_TS[symbol] = now

    key = redis_bias_key(symbol)
    ok, val, dbg = upstash_get(key)
    print("UPSTASH GET:", {"symbol": symbol, "key": key, "ok": ok, "val": val, "dbg": dbg}, flush=True)

    if ok and val is not None:
        v = val.upper().strip()
        if v in {"LONG", "SHORT", "NONE"}:
            BIAS[symbol] = v

def bias_warmup_all() -> None:
    """
    Tente de charger les BIAS au boot (best effort).
    """
    if not UPSTASH_ENABLED:
        return
    for s in ALLOWED_SYMBOLS:
        bias_load_from_redis(s, force=True)

# Warmup au démarrage
bias_warmup_all()


# ================= BITMART CONFIG =================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

BASE_URL = (os.environ.get("BITMART_BASE_URL") or "https://demo-api-cloud-v2.bitmart.com").strip()
OPEN_TYPE = (os.environ.get("OPEN_TYPE") or "isolated").strip().lower()
LEVERAGE = int(float((os.environ.get("LEVERAGE") or "25").strip()))
NOTIONAL_USD_PER_TRADE = float((os.environ.get("NOTIONAL_USD_PER_TRADE") or "2500").strip())

# SL safety
MIN_SL_PCT = float((os.environ.get("MIN_SL_PCT") or "0.0005").strip())  # 0.05%

BOT_VERSION = (os.environ.get("BOT_VERSION") or "v2-qqe-exit+no-squeeze+conditional-close+sl-fix+upstash-bias").strip()

LEVERAGE_CACHE: Dict[str, Dict[str, Any]] = {}
LEVERAGE_CACHE_TTL = 600

DETAILS_CACHE: Dict[str, Dict[str, Any]] = {}
DETAILS_CACHE_TTL = 600

LAST_ALERT: Dict[str, Any] = {}
LAST_ALERT_TS = 0

LAST_ORDER: Dict[str, Any] = {}
LAST_ORDER_TS = 0


# ================= UTILS =================
def normalize_symbol(s: str) -> str:
    sym = (s or "").upper().strip()
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym

def normalize_color(c: str) -> str:
    c = (c or "").strip().lower()
    mapping = {
        "lime": "green",
        "aqua": "blue",
        "cyan": "blue",
        "violet": "purple",
        "magenta": "purple",
        "fuchsia": "purple",
        "pink": "purple",
        "rose": "purple",
        "maroon": "red",
    }
    return mapping.get(c, c)

def safe_float(v) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except Exception:
        return 0.0

def safe_int(v) -> int:
    try:
        if v is None:
            return 0
        return int(float(v))
    except Exception:
        return 0

def extract_code(res: Dict[str, Any]):
    j = res.get("json") or {}
    return j.get("code")

def bar_id(symbol: str, tf: str, t: str) -> str:
    return f"{symbol}|{tf or ''}|{t or ''}"

def record_last_order(action: str, symbol: str, side: str, payload: Dict[str, Any], response: Dict[str, Any], extra: Optional[Dict[str, Any]] = None):
    global LAST_ORDER, LAST_ORDER_TS
    LAST_ORDER = {
        "action": action, "symbol": symbol, "side": side,
        "payload": payload, "response": response,
        "extra": extra or {}
    }
    LAST_ORDER_TS = int(time.time())


# ================= SIGN / HTTP =================
def sign_request(timestamp: int, body: Dict[str, Any]) -> str:
    body_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
    message = f"{timestamp}#{BITMART_MEMO}#{body_str}"
    return hmac.new(BITMART_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

def bm_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    ts = int(time.time() * 1000)
    signature = sign_request(ts, body)
    headers = {
        "Content-Type": "application/json",
        "X-BM-KEY": BITMART_KEY,
        "X-BM-TIMESTAMP": str(ts),
        "X-BM-SIGN": signature,
    }
    try:
        r = requests.post(BASE_URL + path, headers=headers,
                          data=json.dumps(body, separators=(",", ":"), sort_keys=True),
                          timeout=15)
        try:
            return {"http": r.status_code, "json": r.json()}
        except Exception:
            return {"http": r.status_code, "text": r.text}
    except Exception as e:
        return {"http": 0, "error": str(e)}

def bm_get_keyed(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    headers = {"X-BM-KEY": BITMART_KEY}
    try:
        r = requests.get(BASE_URL + path, headers=headers, params=params or {}, timeout=15)
        try:
            return {"http": r.status_code, "json": r.json()}
        except Exception:
            return {"http": r.status_code, "text": r.text}
    except Exception as e:
        return {"http": 0, "error": str(e)}

def bm_get_public(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        r = requests.get(BASE_URL + path, params=params or {}, timeout=15)
        try:
            return {"http": r.status_code, "json": r.json()}
        except Exception:
            return {"http": r.status_code, "text": r.text}
    except Exception as e:
        return {"http": 0, "error": str(e)}


# ================= DETAILS (contract_size + last_price + precision) =================
def get_details(symbol: str) -> Tuple[bool, Dict[str, Any]]:
    now = int(time.time())
    c = DETAILS_CACHE.get(symbol)
    if c and (now - int(c.get("_ts", 0)) < DETAILS_CACHE_TTL):
        return True, c

    res = bm_get_public("/contract/public/details", params={"symbol": symbol})
    j = res.get("json") or {}
    if j.get("code") != 1000:
        return False, {"error": "details_failed", "raw": res}

    data = j.get("data") or {}
    payload = data if isinstance(data, dict) else {"symbols": data if isinstance(data, list) else [data]}
    if "symbols" not in payload:
        payload = {"symbols": [payload]}
    payload["_ts"] = now
    DETAILS_CACHE[symbol] = payload
    return True, payload

def _pick_details_row(payload: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    row = None
    for it in payload.get("symbols") or []:
        if (it.get("symbol") or "").upper() == symbol:
            row = it
            break
    if row is None:
        syms = payload.get("symbols") or []
        row = syms[0] if syms else None
    return row

def get_contract_size_and_price(symbol: str) -> Tuple[bool, float, float, Dict[str, Any]]:
    ok, payload = get_details(symbol)
    if not ok:
        return False, 0.0, 0.0, payload

    row = _pick_details_row(payload, symbol)
    if not row:
        return False, 0.0, 0.0, {"error": "details_no_row", "details": payload}

    cs = safe_float(row.get("contract_size"))
    px = safe_float(row.get("last_price"))
    if cs <= 0:
        return False, 0.0, 0.0, {"error": "contract_size_missing", "row": row}
    return True, cs, px, {"row": row}

def get_price_precision(symbol: str) -> int:
    ok, payload = get_details(symbol)
    if not ok:
        return 2
    row = _pick_details_row(payload, symbol) or {}
    for k in ("price_precision", "price_scale", "price_decimals", "price_decimal"):
        v = row.get(k)
        if v is None:
            continue
        try:
            p = int(float(v))
            if 0 <= p <= 10:
                return p
        except Exception:
            pass
    return 2

def round_price(symbol: str, price: float) -> float:
    p = get_price_precision(symbol)
    try:
        return float(f"{price:.{p}f}")
    except Exception:
        return float(price)

def compute_size(symbol: str, price_hint: float) -> Tuple[bool, int, Dict[str, Any]]:
    ok, cs, px, dbg = get_contract_size_and_price(symbol)
    if not ok:
        return False, 0, dbg

    price = px if px > 0 else price_hint
    if price <= 0:
        return False, 0, {"error": "no_price", "symbol": symbol, "price_hint": price_hint, "last_price": px}

    contracts = NOTIONAL_USD_PER_TRADE / (price * cs)
    size_int = int(math.floor(contracts))
    if size_int < 1:
        size_int = 1

    return True, size_int, {
        "symbol": symbol,
        "notional_usd": NOTIONAL_USD_PER_TRADE,
        "leverage": float(LEVERAGE),
        "est_margin_usd": NOTIONAL_USD_PER_TRADE / float(LEVERAGE),
        "contract_size": cs,
        "price_used": price,
        "computed_contracts": contracts,
        "size_int": size_int
    }


# ================= POSITIONS =================
def fetch_positions(symbol: str) -> Tuple[bool, List[Dict[str, Any]], Dict[str, Any]]:
    res = bm_get_keyed("/contract/private/position", params={"symbol": symbol})
    j = res.get("json") or {}
    if j.get("code") != 1000:
        return False, [], res
    data = j.get("data") or []
    if not isinstance(data, list):
        data = []
    return True, data, res

def get_open_sides(symbol: str) -> Tuple[bool, Dict[str, int], Dict[str, Any]]:
    ok, rows, raw = fetch_positions(symbol)
    if not ok:
        return False, {"LONG": 0, "SHORT": 0}, raw

    sides = {"LONG": 0, "SHORT": 0}
    for r in rows:
        amt = safe_int(r.get("current_amount") or 0)
        if amt == 0:
            continue
        ptype = str(r.get("position_type") or "")
        if ptype == "1":
            sides["LONG"] += abs(amt)
        elif ptype == "2":
            sides["SHORT"] += abs(amt)

    return True, sides, {"rows": rows, "raw": raw}

def fetch_position_size(symbol: str, side: str) -> int:
    ok, rows, _raw = fetch_positions(symbol)
    if not ok:
        return 1

    want = "1" if side == "LONG" else "2"
    for r in rows:
        amt = safe_int(r.get("current_amount") or 0)
        if amt == 0:
            continue
        if str(r.get("position_type") or "") == want:
            return abs(amt)

    for r in rows:
        amt = safe_int(r.get("current_amount") or 0)
        if amt != 0:
            return abs(amt)

    return 1

def fetch_entry_price(symbol: str, side: str) -> float:
    ok, rows, _raw = fetch_positions(symbol)
    if not ok:
        return 0.0
    want = "1" if side == "LONG" else "2"
    for r in rows:
        if str(r.get("position_type") or "") != want:
            continue
        amt = safe_int(r.get("current_amount") or 0)
        if amt == 0:
            continue
        ep = safe_float(r.get("entry_price") or r.get("open_avg_price"))
        if ep > 0:
            return ep
    return 0.0

def fetch_entry_price_retry(symbol: str, side: str, tries: int = 5, sleep_s: float = 0.25) -> float:
    for _ in range(max(1, tries)):
        ep = fetch_entry_price(symbol, side)
        if ep > 0:
            return ep
        time.sleep(sleep_s)
    return 0.0

def resync_symbol(symbol: str) -> None:
    ok, sides, _dbg = get_open_sides(symbol)
    if not ok:
        return
    if sides["LONG"] > 0 and sides["SHORT"] > 0:
        STATE[symbol].update({"in_position": True, "side": "HEDGE"})
    elif sides["LONG"] > 0:
        STATE[symbol].update({"in_position": True, "side": "LONG"})
    elif sides["SHORT"] > 0:
        STATE[symbol].update({"in_position": True, "side": "SHORT"})
    else:
        STATE[symbol].update({"in_position": False, "side": None})


# ================= BITMART ACTIONS =================
def submit_leverage(symbol: str) -> Dict[str, Any]:
    return bm_post("/contract/private/submit-leverage", {
        "symbol": symbol,
        "leverage": str(LEVERAGE),
        "open_type": OPEN_TYPE
    })

def ensure_leverage(symbol: str) -> None:
    now = int(time.time())
    c = LEVERAGE_CACHE.get(symbol)
    if c:
        fresh = (now - int(c.get("ts", 0)) < LEVERAGE_CACHE_TTL)
        same = (c.get("leverage") == LEVERAGE and c.get("open_type") == OPEN_TYPE)
        if fresh and same:
            return

    res = submit_leverage(symbol)
    print("BITMART SUBMIT LEVERAGE:", symbol, res, flush=True)
    if extract_code(res) == 1000:
        LEVERAGE_CACHE[symbol] = {"leverage": LEVERAGE, "open_type": OPEN_TYPE, "ts": now}

def open_market(symbol: str, side: str, price_hint: float, source: str) -> Dict[str, Any]:
    ensure_leverage(symbol)

    ok, size_int, sizing_dbg = compute_size(symbol, price_hint)
    if not ok:
        res = {"http": 0, "json": {"code": -1, "message": "sizing_failed", "data": sizing_dbg}}
        record_last_order("OPEN", symbol, side, {"error": "sizing_failed"}, res, {"source": source, "sizing": sizing_dbg})
        print("SIZING ERROR:", symbol, sizing_dbg, flush=True)
        return res

    payload = {
        "symbol": symbol,
        "type": "market",
        "side": 1 if side == "LONG" else 4,
        "mode": 1,
        "size": size_int
    }
    res = bm_post("/contract/private/submit-order", payload)
    record_last_order("OPEN", symbol, side, payload, res, {"source": source, "sizing": sizing_dbg})
    print("BITMART OPEN:", symbol, side, res, flush=True)
    return res

def close_market(symbol: str, side: str, source: str) -> Dict[str, Any]:
    ensure_leverage(symbol)
    size_int = fetch_position_size(symbol, side)

    payload = {
        "symbol": symbol,
        "type": "market",
        "side": 3 if side == "LONG" else 2,
        "mode": 1,
        "size": size_int
    }
    res = bm_post("/contract/private/submit-order", payload)
    record_last_order("CLOSE", symbol, side, payload, res, {"source": source, "close_size": size_int})
    print("BITMART CLOSE:", symbol, side, res, flush=True)
    return res

def set_stop_loss(symbol: str, position_side: str, trigger_price: float, source: str) -> Dict[str, Any]:
    ensure_leverage(symbol)
    size_int = fetch_position_size(symbol, position_side)

    payload = {
        "symbol": symbol,
        "type": "stop_loss",
        "side": 3 if position_side == "LONG" else 2,
        "trigger_price": f"{trigger_price:.{get_price_precision(symbol)}f}",
        "executive_price": f"{trigger_price:.{get_price_precision(symbol)}f}",
        "price_type": 1,
        "plan_category": 2,
        "category": "market",
        "size": size_int
    }
    res = bm_post("/contract/private/submit-tp-sl-order", payload)
    record_last_order("SET_SL", symbol, position_side, payload, res, {"source": source, "sl": trigger_price})
    print("BITMART SL:", symbol, position_side, res, flush=True)
    return res

def compute_safe_sl(symbol: str, side: str, proposed_sl: float, entry_price: float) -> Tuple[bool, float, Dict[str, Any]]:
    """
    Retourne (ok, sl_final, debug)
    ok=False => pas de SL envoyé
    """
    if proposed_sl <= 0 or entry_price <= 0:
        return False, 0.0, {"error": "missing_prices", "proposed_sl": proposed_sl, "entry_price": entry_price}

    min_dist = entry_price * float(MIN_SL_PCT)

    if side == "LONG":
        target = proposed_sl
        if target >= entry_price or (entry_price - target) < min_dist:
            target = entry_price - min_dist
        target = round_price(symbol, target)
        if target >= entry_price:
            target = round_price(symbol, entry_price - (entry_price * 0.001))
        return True, target, {"side": side, "entry_price": entry_price, "proposed": proposed_sl, "min_dist": min_dist, "final": target}

    else:
        target = proposed_sl
        if target <= entry_price or (target - entry_price) < min_dist:
            target = entry_price + min_dist
        target = round_price(symbol, target)
        if target <= entry_price:
            target = round_price(symbol, entry_price + (entry_price * 0.001))
        return True, target, {"side": side, "entry_price": entry_price, "proposed": proposed_sl, "min_dist": min_dist, "final": target}


# ================= ROUTES =================
@app.get("/")
def home():
    return "Bot TradingView DEMO V2 actif"

@app.get("/version")
def version():
    return jsonify({
        "bot_version": BOT_VERSION,
        "base_url": BASE_URL,
        "allowed_symbols": sorted(list(ALLOWED_SYMBOLS)),
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE,
        "notional_usd_per_trade": NOTIONAL_USD_PER_TRADE,
        "est_margin_usd_per_trade": NOTIONAL_USD_PER_TRADE / float(LEVERAGE),
        "min_sl_pct": MIN_SL_PCT,
        "secret_len": len(SECRET),
        "bias": BIAS,
        "upstash_enabled": UPSTASH_ENABLED,
        "redis_key_prefix": REDIS_KEY_PREFIX,
        "bias_redis_refresh_ttl_s": BIAS_REDIS_REFRESH_TTL
    }), 200

@app.get("/debug/bitmart")
def debug_bitmart():
    tests = {}
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        tests[sym] = bm_get_keyed("/contract/private/position", params={"symbol": sym})
    return jsonify({"position_tests": tests}), 200

@app.get("/debug/last_alert")
def debug_last_alert():
    return jsonify({"ts": LAST_ALERT_TS, "last_alert": LAST_ALERT}), 200

@app.get("/debug/last_order")
def debug_last_order():
    return jsonify({"ts": LAST_ORDER_TS, "last_order": LAST_ORDER}), 200

@app.get("/debug/state")
def debug_state():
    return jsonify({"bias": BIAS, "state": STATE}), 200

@app.get("/debug/redis")
def debug_redis():
    if not UPSTASH_ENABLED:
        return jsonify({"status": "upstash_not_configured"}), 200
    out = {}
    for sym in sorted(list(ALLOWED_SYMBOLS)):
        key = redis_bias_key(sym)
        ok, val, dbg = upstash_get(key)
        out[sym] = {"key": key, "ok": ok, "val": val, "dbg": dbg}
    return jsonify({"status": "ok", "redis_bias": out}), 200


# ================= WEBHOOK =================
@app.post("/webhook")
def webhook():
    global LAST_ALERT, LAST_ALERT_TS

    data = request.get_json(silent=True) or {}
    if data.get("secret") != SECRET:
        return jsonify({"status": "forbidden"}), 403

    LAST_ALERT = data
    LAST_ALERT_TS = int(time.time())
    print("ALERTE:", data, flush=True)

    event = (data.get("event") or "").upper().strip()
    action = (data.get("action") or "").upper().strip()
    reason = (data.get("reason") or "").upper().strip()

    symbol = normalize_symbol(data.get("ticker"))
    color = normalize_color(data.get("color") or "")

    tf = str(data.get("tf") or "")
    t = str(data.get("time") or data.get("time_ms") or "")

    price_hint = (
        safe_float(data.get("close")) or safe_float(data.get("open"))
        or safe_float(data.get("high")) or safe_float(data.get("low"))
    )

    # symbol gate
    if symbol not in ALLOWED_SYMBOLS and event != "RESET":
        return jsonify({"status": "ignored_symbol", "symbol": symbol}), 200

    # Toujours essayer de recharger le BIAS depuis Redis (best-effort)
    # - utile après restart
    # - ne change pas ta stratégie
    if symbol in ALLOWED_SYMBOLS:
        bias_load_from_redis(symbol, force=False)

    # RESET
    if event == "RESET":
        if symbol in ALLOWED_SYMBOLS:
            resync_symbol(symbol)
            STATE[symbol]["last_entry_bar_id"] = None
            bias_set_local(symbol, "NONE")
            bias_save_to_redis(symbol)
            return jsonify({"status": "state_resynced", "symbol": symbol, "state": STATE[symbol], "bias": BIAS[symbol]}), 200
        return jsonify({"status": "reset_ignored"}), 200

    # sync
    resync_symbol(symbol)
    st = STATE[symbol]

    # BIAS update stoch only (fondation)
    if event == "STOCH_ENTRY":
        if reason == "LD":
            bias_set_local(symbol, "LONG")
            bias_save_to_redis(symbol)
        elif reason == "HD":
            bias_set_local(symbol, "SHORT")
            bias_save_to_redis(symbol)

    # =========================
    # EXITS (QQE / ACTION)
    # =========================
    if action in {"EXIT_LONG", "EXIT_SHORT"} or event == "QQE_EXIT":
        ok, sides, _dbg = get_open_sides(symbol)
        print("EXIT CHECK:", {"symbol": symbol, "event": event, "action": action, "reason": reason, "ok": ok, "sides": sides}, flush=True)

        if action == "EXIT_LONG":
            if ok and sides.get("LONG", 0) > 0:
                res = close_market(symbol, "LONG", source=f"{event or 'EXIT'}_{reason or action}")
                return jsonify({"status": "exit_long_sent", "event": event, "reason": reason, "bitmart": res}), 200
            return jsonify({"status": "exit_long_ignored_no_position", "event": event, "reason": reason, "open": sides}), 200

        if action == "EXIT_SHORT":
            if ok and sides.get("SHORT", 0) > 0:
                res = close_market(symbol, "SHORT", source=f"{event or 'EXIT'}_{reason or action}")
                return jsonify({"status": "exit_short_sent", "event": event, "reason": reason, "bitmart": res}), 200
            return jsonify({"status": "exit_short_ignored_no_position", "event": event, "reason": reason, "open": sides}), 200

        return jsonify({"status": "exit_missing_action", "event": event, "reason": reason}), 200

    # SORTIE STOCH (si encore utilisé)
    if event == "STOCH_EXIT":
        ok, sides, _dbg = get_open_sides(symbol)
        if not ok:
            return jsonify({"status": "bitmart_position_fetch_failed"}), 200

        if sides["LONG"] > 0 and reason.startswith("HD"):
            res = close_market(symbol, "LONG", source="STOCH_EXIT_HD")
            return jsonify({"status": "exit_stoch_long", "bitmart": res}), 200

        if sides["SHORT"] > 0 and reason.startswith("LD"):
            res = close_market(symbol, "SHORT", source="STOCH_EXIT_LD")
            return jsonify({"status": "exit_stoch_short", "bitmart": res}), 200

        return jsonify({"status": "stoch_exit_no_match", "reason": reason, "open": sides}), 200

    # VECTOR opposé ferme
    if event == "VECTOR":
        ok, sides, _dbg = get_open_sides(symbol)
        if not ok:
            return jsonify({"status": "bitmart_position_fetch_failed"}), 200

        if sides["SHORT"] > 0 and color in LONG_COLORS:
            res = close_market(symbol, "SHORT", source=f"VECTOR_{color}")
            return jsonify({"status": "exit_short_on_long_vector", "color": color, "bitmart": res}), 200

        if sides["LONG"] > 0 and color in SHORT_COLORS:
            res = close_market(symbol, "LONG", source=f"VECTOR_{color}")
            return jsonify({"status": "exit_long_on_short_vector", "color": color, "bitmart": res}), 200

        # sinon: continuer vers entrées

    # =========================
    # ENTRIES
    # =========================
    # Priorité au bar_key si envoyé par TradingView (plus robuste)
    bid = (data.get("bar_key") or "").strip() or bar_id(symbol, tf, t)

    if st["last_entry_bar_id"] == bid:
        return jsonify({"status": "ignored_same_bar"}), 200

    ok, sides, _dbg = get_open_sides(symbol)
    if ok and (sides["LONG"] > 0 or sides["SHORT"] > 0):
        st.update({"in_position": True, "side": "LONG" if sides["LONG"] > 0 else "SHORT"})
        return jsonify({"status": "ignored_already_in_position", "open": sides, "bias": BIAS[symbol]}), 200

    # STOCH ENTRY (fondation)
    if event == "STOCH_ENTRY":
        if reason == "LD":
            res = open_market(symbol, "LONG", price_hint, source="STOCH_LD")
            if extract_code(res) != 1000:
                return jsonify({"status": "entry_failed", "bitmart": res, "bias": BIAS[symbol]}), 200

            st.update({"in_position": True, "side": "LONG", "last_entry_bar_id": bid})

            proposed_sl = safe_float(data.get("sl_price")) or safe_float(data.get("low"))
            entry = fetch_entry_price_retry(symbol, "LONG", tries=5, sleep_s=0.25) or price_hint
            ok_sl, sl_final, sl_dbg = compute_safe_sl(symbol, "LONG", proposed_sl, entry)
            if ok_sl:
                set_stop_loss(symbol, "LONG", sl_final, source="SL_STOCH_LD_SAFE")
                print("SL SAFE:", symbol, sl_dbg, flush=True)
            else:
                print("SL SKIP:", symbol, sl_dbg, flush=True)

            return jsonify({"status": "enter_long_stoch", "bias": BIAS[symbol]}), 200

        if reason == "HD":
            res = open_market(symbol, "SHORT", price_hint, source="STOCH_HD")
            if extract_code(res) != 1000:
                return jsonify({"status": "entry_failed", "bitmart": res, "bias": BIAS[symbol]}), 200

            st.update({"in_position": True, "side": "SHORT", "last_entry_bar_id": bid})

            proposed_sl = safe_float(data.get("sl_price")) or safe_float(data.get("high"))
            entry = fetch_entry_price_retry(symbol, "SHORT", tries=5, sleep_s=0.25) or price_hint
            ok_sl, sl_final, sl_dbg = compute_safe_sl(symbol, "SHORT", proposed_sl, entry)
            if ok_sl:
                set_stop_loss(symbol, "SHORT", sl_final, source="SL_STOCH_HD_SAFE")
                print("SL SAFE:", symbol, sl_dbg, flush=True)
            else:
                print("SL SKIP:", symbol, sl_dbg, flush=True)

            return jsonify({"status": "enter_short_stoch", "bias": BIAS[symbol]}), 200

        return jsonify({"status": "ignored_stoch_reason", "reason": reason, "bias": BIAS[symbol]}), 200

    # VECTOR ENTRY (conditionné par le BIAS: LD ON / HD ON)
    if event == "VECTOR":
        inferred = None
        if color in LONG_COLORS:
            inferred = "LONG"
        elif color in SHORT_COLORS:
            inferred = "SHORT"
        else:
            return jsonify({"status": "ignored_vector_unknown_color", "color": color, "bias": BIAS[symbol]}), 200

        if inferred == "LONG" and BIAS[symbol] != "LONG":
            return jsonify({"status": "ignored_vector_bias_mismatch", "wanted": "LONG", "bias": BIAS[symbol], "color": color}), 200
        if inferred == "SHORT" and BIAS[symbol] != "SHORT":
            return jsonify({"status": "ignored_vector_bias_mismatch", "wanted": "SHORT", "bias": BIAS[symbol], "color": color}), 200

        res = open_market(symbol, inferred, price_hint, source="VECTOR_ENTRY")
        if extract_code(res) != 1000:
            return jsonify({"status": "entry_failed", "bitmart": res, "bias": BIAS[symbol]}), 200

        st.update({"in_position": True, "side": inferred, "last_entry_bar_id": bid})

        if inferred == "LONG":
            proposed_sl = safe_float(data.get("low"))
            entry = fetch_entry_price_retry(symbol, "LONG", tries=5, sleep_s=0.25) or price_hint
            ok_sl, sl_final, sl_dbg = compute_safe_sl(symbol, "LONG", proposed_sl, entry)
            if ok_sl:
                set_stop_loss(symbol, "LONG", sl_final, source="SL_VECTOR_LONG_SAFE")
                print("SL SAFE:", symbol, sl_dbg, flush=True)
        else:
            proposed_sl = safe_float(data.get("high"))
            entry = fetch_entry_price_retry(symbol, "SHORT", tries=5, sleep_s=0.25) or price_hint
            ok_sl, sl_final, sl_dbg = compute_safe_sl(symbol, "SHORT", proposed_sl, entry)
            if ok_sl:
                set_stop_loss(symbol, "SHORT", sl_final, source="SL_VECTOR_SHORT_SAFE")
                print("SL SAFE:", symbol, sl_dbg, flush=True)

        return jsonify({"status": "enter_vector", "side": inferred, "bias": BIAS[symbol]}), 200

    return jsonify({"status": "ignored", "bias": BIAS[symbol]}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
