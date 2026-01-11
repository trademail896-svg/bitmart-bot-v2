from flask import Flask, request, jsonify
import os
import time
import json
import hmac
import hashlib
import threading
import requests
from typing import Optional, Dict, Any, Tuple

app = Flask(__name__)

# ================= CONFIG / STRATEGIE =================
ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

LONG_COLORS = {"green", "blue"}
SHORT_COLORS = {"red", "pink", "purple"}

# Secret V2 (Render env: TV_WEBHOOK_SECRET=TV_BOT_DEMO_2026_V2)
SECRET = (os.environ.get("TV_WEBHOOK_SECRET") or "TV_BOT_DEMO_2026_V2").strip()

# Levier global (Render env: LEVERAGE=25)
LEVERAGE = str(int(os.environ.get("LEVERAGE", "25")))

# ================= BITMART CONFIG =================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

# DEMO
BASE_URL = (os.environ.get("BITMART_BASE_URL") or "https://demo-api-cloud-v2.bitmart.com").strip()

# ================= THREAD SAFETY =================
STATE_LOCK = threading.Lock()

# ================= STATE (PAR SYMBOLE) =================
def new_symbol_state() -> Dict[str, Any]:
    return {
        "in_position": False,
        "side": None,                 # "LONG" / "SHORT"
        "pos_size": None,             # taille détectée (si possible)
        "last_entry_bar_key": None,   # anti-double entry
        "squeeze": False,             # mis à jour via event SQUEEZE_STATE
    }

STATE: Dict[str, Dict[str, Any]] = {s: new_symbol_state() for s in ALLOWED_SYMBOLS}


# ================= UTILS =================
def normalize_symbol(s: str) -> str:
    sym = (s or "").upper().strip()
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym

def safe_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, str) and x.strip() == "":
            return default
        return float(x)
    except Exception:
        return default

def safe_bool(x, default=False) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        v = x.strip().lower()
        if v in {"true", "1", "yes", "y"}:
            return True
        if v in {"false", "0", "no", "n"}:
            return False
    if isinstance(x, (int, float)):
        return bool(x)
    return default

def get_size(symbol: str) -> int:
    # Size Futures = int (BitMart). On force >= 1.
    try:
        n = int(os.environ.get(f"SIZE_{symbol}", "1"))
        return max(1, n)
    except Exception:
        return 1

def extract_code(res: dict) -> Optional[int]:
    j = res.get("json") if isinstance(res, dict) else None
    if not isinstance(j, dict):
        return None
    try:
        return int(j.get("code"))
    except Exception:
        return None

def make_bar_key(symbol: str, tf: str, t: str, side: str, signal: str) -> str:
    return f"{symbol}|{tf or ''}|{t or ''}|{side or ''}|{signal or ''}"

def parse_incoming_data() -> Dict[str, Any]:
    """
    TradingView envoie parfois du JSON en text/plain.
    Cette fonction lit:
      - application/json (request.get_json)
      - ou du texte JSON brut (request.data)
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    # Fallback text/plain
    try:
        raw = request.data.decode("utf-8", errors="ignore").strip()
        if raw:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass

    return {}

# ================= SIGNATURE / HTTP =================
def sign_request(timestamp: int, body: dict) -> str:
    """
    BitMart signature (cloud v2):
      sign = HMAC_SHA256(secret, f"{timestamp}#{memo}#{body_json_sorted}")
    """
    body_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
    message = f"{timestamp}#{BITMART_MEMO}#{body_str}"
    return hmac.new(
        BITMART_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

def bm_post(path: str, body: dict) -> dict:
    ts = int(time.time() * 1000)
    signature = sign_request(ts, body)

    headers = {
        "Content-Type": "application/json",
        "X-BM-KEY": BITMART_KEY,
        "X-BM-TIMESTAMP": str(ts),
        "X-BM-SIGN": signature,
    }

    try:
        r = requests.post(
            BASE_URL + path,
            headers=headers,
            data=json.dumps(body, separators=(",", ":"), sort_keys=True),
            timeout=15
        )
        try:
            return {"http": r.status_code, "json": r.json()}
        except Exception:
            return {"http": r.status_code, "text": r.text}
    except Exception as e:
        return {"http": 0, "error": str(e)}

def bm_get_keyed(path: str, params: Optional[dict] = None) -> dict:
    """
    GET "keyed" (comme ton V1). Note: selon permissions, BitMart peut exiger GET signé.
    On garde la version keyed pour compat, mais on gère les erreurs proprement.
    """
    headers = {"X-BM-KEY": BITMART_KEY}
    try:
        r = requests.get(BASE_URL + path, headers=headers, params=params or {}, timeout=15)
        try:
            return {"http": r.status_code, "json": r.json()}
        except Exception:
            return {"http": r.status_code, "text": r.text}
    except Exception as e:
        return {"http": 0, "error": str(e)}

# ================= BITMART ACTIONS =================
def open_market(symbol: str, side: str) -> dict:
    return bm_post("/contract/private/submit-order", {
        "symbol": symbol,
        "type": "market",
        "side": 1 if side == "LONG" else 4,   # 1=buy open, 4=sell open
        "mode": 1,
        "leverage": LEVERAGE,
        "open_type": "isolated",
        "size": get_size(symbol)
    })

def close_market(symbol: str, side: str, size: Optional[int] = None) -> dict:
    return bm_post("/contract/private/submit-order", {
        "symbol": symbol,
        "type": "market",
        "side": 3 if side == "LONG" else 2,   # 3=sell close, 2=buy close
        "mode": 1,
        "leverage": LEVERAGE,
        "open_type": "isolated",
        "size": int(size) if size is not None else get_size(symbol)
    })

def set_stop_loss(symbol: str, side: str, price: float, size: Optional[int] = None) -> dict:
    """
    IMPORTANT: le tick size peut rendre certains prix invalides.
    Ici on envoie le prix tel quel (string) sans forcer .2f.
    """
    p = str(price)
    return bm_post("/contract/private/submit-tp-sl-order", {
        "symbol": symbol,
        "type": "stop_loss",
        "side": 3 if side == "LONG" else 2,   # close side
        "trigger_price": p,
        "executive_price": p,
        "price_type": 1,
        "plan_category": 2,
        "category": "market",
        "size": int(size) if size is not None else get_size(symbol)
    })

# ================= POSITION RESYNC =================
def fetch_position(symbol: str) -> Tuple[Optional[bool], Optional[str], Optional[int], dict]:
    """
    Retourne: (has_position, side, abs_size_int, raw)
    has_position=None => inconnu (API fail). On ne conclut pas flat.
    """
    res = bm_get_keyed("/contract/private/position", params={"symbol": symbol})
    j = res.get("json") if isinstance(res, dict) else None
    if not isinstance(j, dict) or j.get("code") != 1000:
        return (None, None, None, res)

    data = j.get("data") or []
    if not isinstance(data, list) or len(data) == 0:
        return (False, None, None, res)

    row = None
    for it in data:
        if (it.get("symbol") or "").upper() == symbol:
            row = it
            break
    if row is None:
        row = data[0]

    amt = safe_float(row.get("current_amount"), 0.0)
    if amt == 0:
        return (False, None, None, res)

    ptype = row.get("position_type")
    side = None
    if str(ptype) == "1":
        side = "LONG"
    elif str(ptype) == "2":
        side = "SHORT"
    else:
        side = "LONG" if amt > 0 else "SHORT"

    abs_size = int(abs(round(amt)))
    abs_size = max(1, abs_size)
    return (True, side, abs_size, res)

def resync_symbol(symbol: str) -> None:
    st = STATE[symbol]
    has_pos, side, abs_size, raw = fetch_position(symbol)
    if has_pos is None:
        print("RESYNC UNKNOWN (API FAIL)", {"symbol": symbol, "raw": raw})
        return
    if has_pos is False:
        st.update({"in_position": False, "side": None, "pos_size": None})
        print("RESYNC FLAT", {"symbol": symbol})
        return
    st.update({"in_position": True, "side": side, "pos_size": abs_size})
    print("RESYNC FOUND POSITION", {"symbol": symbol, "side": side, "size": abs_size})

def resync_all() -> None:
    for s in ALLOWED_SYMBOLS:
        resync_symbol(s)

# ================= ROUTES =================
@app.get("/")
def home():
    return "BitMart Bot V2 actif (multi-symbol, squeeze feed, 25x)"

@app.post("/webhook")
def webhook():
    data = parse_incoming_data()

    # Sécurité
    if data.get("secret") != SECRET:
        print("FORBIDDEN: bad/missing secret. received=", data.get("secret"))
        return jsonify({"status": "forbidden"}), 403

    event = (data.get("event") or "").upper().strip()
    action = (data.get("action") or "").upper().strip()
    color = (data.get("color") or "").lower().strip()

    symbol = normalize_symbol(data.get("ticker"))
    tf = str(data.get("tf") or "")
    t = str(data.get("time") or "")

    # RESET: autorisé même sans symbole valide
    if event != "RESET" and symbol not in ALLOWED_SYMBOLS:
        print("IGNORED SYMBOL:", symbol, "event:", event)
        return jsonify({"status": "ignored_symbol"}), 200

    with STATE_LOCK:
        # ========== RESET ==========
        if event == "RESET":
            # si ticker fourni et autorisé => resync seulement celui-là, sinon tous
            if symbol in ALLOWED_SYMBOLS:
                resync_symbol(symbol)
                STATE[symbol]["last_entry_bar_key"] = None
            else:
                resync_all()
                for s in ALLOWED_SYMBOLS:
                    STATE[s]["last_entry_bar_key"] = None
            return jsonify({"status": "state_resynced", "state": STATE}), 200

        st = STATE[symbol]

        # ========== SQUEEZE STATE FEED ==========
        if event == "SQUEEZE_STATE":
            sq = safe_bool(data.get("squeeze"), st["squeeze"])
            st["squeeze"] = sq
            print("SQUEEZE UPDATE:", {"symbol": symbol, "squeeze": sq})
            return jsonify({"status": "squeeze_updated", "symbol": symbol, "squeeze": sq}), 200

        # ========== SORTIES (compat V1) ==========
        # Si on reçoit EXIT_* mais état local flat => resync symbole (cas reboot)
        if action in {"EXIT_LONG", "EXIT_SHORT"} and not st["in_position"]:
            print("EXIT RECEIVED BUT STATE FLAT -> RESYNC", {"symbol": symbol})
            resync_symbol(symbol)

        if st["in_position"]:
            # Sortie explicite (Stoch / autre)
            if action in {"EXIT_LONG", "EXIT_SHORT"}:
                print("EXIT POSITION (ACTION)", {"symbol": symbol, "state": st, "action": action})
                size_to_close = st.get("pos_size") or get_size(symbol)
                res = close_market(symbol, st["side"], size=size_to_close)
                print("BITMART CLOSE:", res)

                if extract_code(res) == 1000:
                    st.update({"in_position": False, "side": None, "pos_size": None})
                    return jsonify({"status": "exit"}), 200

                return jsonify({"status": "close_failed", "bitmart": res}), 200

            # Sortie via vecteur opposé (si event VECTOR)
            if event == "VECTOR":
                if st["side"] == "LONG" and color in SHORT_COLORS:
                    size_to_close = st.get("pos_size") or get_size(symbol)
                    res = close_market(symbol, "LONG", size=size_to_close)
                    print("EXIT LONG (VECTOR OPP)", res)
                    if extract_code(res) == 1000:
                        st.update({"in_position": False, "side": None, "pos_size": None})
                        return jsonify({"status": "exit"}), 200
                    return jsonify({"status": "close_failed", "bitmart": res}), 200

                if st["side"] == "SHORT" and color in LONG_COLORS:
                    size_to_close = st.get("pos_size") or get_size(symbol)
                    res = close_market(symbol, "SHORT", size=size_to_close)
                    print("EXIT SHORT (VECTOR OPP)", res)
                    if extract_code(res) == 1000:
                        st.update({"in_position": False, "side": None, "pos_size": None})
                        return jsonify({"status": "exit"}), 200
                    return jsonify({"status": "close_failed", "bitmart": res}), 200

            return jsonify({"status": "holding"}), 200

        # ========== ENTREES (compat V1: VECTOR = entrée) ==========
        if event == "VECTOR":
            # Filtre squeeze: pas d'entrée si squeeze actif
            if st.get("squeeze") is True:
                print("ENTRY BLOCKED (SQUEEZE)", {"symbol": symbol})
                return jsonify({"status": "blocked_squeeze"}), 200

            inferred_side = None
            if color in LONG_COLORS:
                inferred_side = "LONG"
            elif color in SHORT_COLORS:
                inferred_side = "SHORT"

            if inferred_side is None:
                return jsonify({"status": "ignored_vector_unknown_color"}), 200

            # Anti-double entrée même bougie/direction
            bar_key = make_bar_key(symbol, tf, t, inferred_side, "VECTOR")
            if st["last_entry_bar_key"] == bar_key:
                return jsonify({"status": "ignored_same_bar"}), 200

            res_entry = open_market(symbol, inferred_side)
            print("BITMART ENTRY:", {"symbol": symbol, "side": inferred_side, "res": res_entry})

            if extract_code(res_entry) != 1000:
                return jsonify({"status": "entry_failed", "bitmart": res_entry}), 200

            st.update({
                "in_position": True,
                "side": inferred_side,
                "pos_size": get_size(symbol),
                "last_entry_bar_key": bar_key
            })

            # SL (compat V1)
            if inferred_side == "LONG":
                sl = safe_float(data.get("low"), 0.0)
                if sl > 0:
                    res_sl = set_stop_loss(symbol, "LONG", sl, size=st["pos_size"])
                    print("BITMART SL:", res_sl)

            if inferred_side == "SHORT":
                sl = safe_float(data.get("high"), 0.0)
                if sl > 0:
                    res_sl = set_stop_loss(symbol, "SHORT", sl, size=st["pos_size"])
                    print("BITMART SL:", res_sl)

            return jsonify({"status": "entered", "symbol": symbol, "side": inferred_side}), 200

        # Aucun match
        return jsonify({"status": "ignored", "event": event, "action": action}), 200


if __name__ == "__main__":
    # Render fournit PORT
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
