from flask import Flask, request, jsonify
import os
import time
import json
import hmac
import hashlib
import requests
from typing import Optional, Tuple, Dict, Any

app = Flask(__name__)

# ================= STRATEGIE =================
LONG_COLORS = {"green", "blue"}
SHORT_COLORS = {"red", "purple"}  # si tu veux inclure pink: {"red","pink","purple"}

ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# Mode B : une seule position globale à la fois
STATE: Dict[str, Any] = {
    "in_position": False,
    "side": None,                 # "LONG" / "SHORT"
    "symbol": None,               # "BTCUSDT" / ...
    "last_entry_bar_key": None,   # lock anti-double entrée même bougie
    "squeeze_on": False
}

SECRET = "TV_BOT_DEMO_2026_V2"

# ================= BITMART CONFIG =================
BITMART_KEY = (os.environ.get("BITMART_API_KEY") or "").strip()
BITMART_SECRET = (os.environ.get("BITMART_API_SECRET") or "").strip()
BITMART_MEMO = (os.environ.get("BITMART_API_MEMO") or "").strip()

# DEMO
BASE_URL = "https://demo-api-cloud-v2.bitmart.com"

# Leverage (tu veux 25x partout)
LEVERAGE = (os.environ.get("LEVERAGE") or "25").strip()
OPEN_TYPE = (os.environ.get("OPEN_TYPE") or "isolated").strip().lower()  # "isolated" ou "cross"

# Cache leverage pour éviter de spam submit-leverage
LEVERAGE_CACHE: Dict[str, Dict[str, Any]] = {}
LEVERAGE_CACHE_TTL_SEC = 600  # 10 minutes

BOT_VERSION = (os.environ.get("BOT_VERSION") or "v2-clean-debug").strip()

# ================= UTILS =================
def normalize_symbol(s: str) -> str:
    sym = (s or "").upper().strip()
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym

def get_size(symbol: str) -> int:
    try:
        n = int(os.environ.get(f"SIZE_{symbol}", "1"))
        return max(1, n)
    except Exception:
        return 1

def extract_code(res: Dict[str, Any]):
    j = res.get("json") or {}
    return j.get("code")

def reason_is_ld(reason: str) -> bool:
    r = (reason or "").upper().strip()
    return r == "LD" or r.startswith("LD")

def reason_is_hd(reason: str) -> bool:
    r = (reason or "").upper().strip()
    return r == "HD" or r.startswith("HD")

def parse_bool(v) -> bool:
    # gère True/False, 1/0, "true"/"false", "on"/"off"
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in {"true", "1", "yes", "y", "on"}:
        return True
    if s in {"false", "0", "no", "n", "off", ""}:
        return False
    # fallback (non vide)
    return True

def make_bar_key(symbol: str, tf: Optional[str], t: Optional[str], side: Optional[str], source: Optional[str]) -> str:
    return f"{symbol}|{tf or ''}|{t or ''}|{side or ''}|{source or ''}"

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

# ================= BITMART ACTIONS =================
def submit_leverage(symbol: str) -> Dict[str, Any]:
    return bm_post("/contract/private/submit-leverage", {
        "symbol": symbol,
        "leverage": LEVERAGE,
        "open_type": OPEN_TYPE
    })

def ensure_leverage_synced(symbol: str) -> bool:
    now = int(time.time())
    cached = LEVERAGE_CACHE.ge_
