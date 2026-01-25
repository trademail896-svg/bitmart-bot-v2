from flask import Flask, request, jsonify
import ccxt
import sqlite3
from datetime import datetime
import logging
from dotenv import load_dotenv
import os
import pandas as pd
import math

load_dotenv()

app = Flask(__name__)

# === CONFIG ===
BITMART_API_KEY    = (os.getenv('BITMART_API_KEY') or "").strip()
BITMART_SECRET     = (os.getenv('BITMART_SECRET') or "").strip()
BITMART_UID        = (os.getenv('BITMART_UID') or "").strip()
WEBHOOK_SECRET     = (os.getenv('WEBHOOK_SECRET') or "").strip()
LEVERAGE           = float(os.getenv('LEVERAGE', 25))
RISK_PCT           = float(os.getenv('RISK_PCT', 1.0)) / 100.0
BUFFER_PCT         = float(os.getenv('BUFFER_PCT', 0.15)) / 100.0
DB_FILE            = 'bot_state.db'
ALLOWED_SYMBOLS    = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BTCUSDT.P', 'ETHUSDT.P', 'SOLUSDT.P']

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

logger.info("=== BOT V2.4 COMPLÈTE - USDX + QUANTITY + ORDER - 2026-01-25 ===")

# Bitmart CCXT (demo USDX)
exchange = ccxt.bitmart({
    'apiKey': BITMART_API_KEY,
    'secret': BITMART_SECRET,
    'uid': BITMART_UID,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
        'marginMode': 'isolated',
    },
})

# Load markets au démarrage
try:
    exchange.load_markets()
    logger.info("[STARTUP] CCXT markets loaded OK.")
except Exception as e:
    logger.error(f"[STARTUP] load_markets() FAILED: {e}")

# Debug balance au démarrage
try:
    balance_info = exchange.fetch_balance()
    logger.info(f"[STARTUP DEBUG] Balance complète : {balance_info}")
    logger.info(f"[STARTUP DEBUG] Clés top-level : {list(balance_info.keys())}")
    if 'free' in balance_info:
        logger.info(f"[STARTUP DEBUG] Clés dans 'free' : {list(balance_info['free'].keys())}")
except Exception as e:
    logger.error(f"[STARTUP DEBUG] fetch_balance failed: {str(e)}")

# === DB INIT ===
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS bot_state (
            symbol TEXT PRIMARY KEY,
            in_position INTEGER DEFAULT 0,
            side TEXT,
            last_entry_bar_id INTEGER DEFAULT 0,
            bias TEXT DEFAULT 'NONE',
            last_update TEXT DEFAULT (datetime('now'))
        )
    ''')
    for sym in ALLOWED_SYMBOLS:
        c.execute("INSERT OR IGNORE INTO bot_state (symbol) VALUES (?)", (sym,))
    conn.commit()
    conn.close()

init_db()

# === STATE HELPERS ===
def get_state(symbol: str) -> dict:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM bot_state WHERE symbol = ?", (symbol,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'in_position': bool(row[1]),
            'side': row[2],
            'last_entry_bar_id': int(row[3] or 0),
            'bias': row[4] or 'NONE',
        }
    return {'in_position': False, 'side': None, 'last_entry_bar_id': 0, 'bias': 'NONE'}

def update_state(symbol: str, **kwargs):
    updates = []
    values = []
    for key, value in kwargs.items():
        if key == 'in_position':
            updates.append("in_position = ?")
            values.append(1 if value else 0)
        elif key in ['side', 'bias']:
            updates.append(f"{key} = ?")
            values.append(value)
        elif key == 'last_entry_bar_id':
            updates.append("last_entry_bar_id = ?")
            values.append(int(value))
    if not updates:
        return
    values.append(datetime.utcnow().isoformat())
    values.append(symbol)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    query = f"UPDATE bot_state SET {', '.join(updates)}, last_update = ? WHERE symbol = ?"
    c.execute(query, values)
    conn.commit()
    conn.close()
    logger.info(f"[STATE] Updated {symbol}: {kwargs}")

# === SYMBOL RESOLUTION ===
def resolve_ccxt_symbol(tv_symbol: str) -> str:
    if not tv_symbol:
        return tv_symbol
    tv_clean = tv_symbol.upper().replace('.P', '')
    if tv_clean in exchange.markets:
        return tv_clean
    for sym, m in exchange.markets.items():
        mid = str(m.get("id", "")).upper()
        if mid == tv_clean:
            return sym
    base = tv_clean[:-4] if tv_clean.endswith("USDT") else tv_clean
    prefix = f"{base}/USDT:"
    for sym in exchange.markets.keys():
        if sym.upper().startswith(prefix):
            return sym
    spot_like = f"{base}/USDT"
    if spot_like in exchange.markets:
        return spot_like
    return tv_clean

# === SAFE BALANCE (priorité USDX) ===
def safe_fetch_balance() -> dict:
    try:
        bal = exchange.fetch_balance()
        if isinstance(bal, dict):
            return bal
        logger.warning(f"[BALANCE] fetch_balance returned {type(bal)}")
        return {}
    except Exception as e:
        logger.error(f"[BALANCE] fetch_balance failed: {e}")
        return {}

def extract_stable_free_balance(balance_info: dict) -> float:
    wanted = ("USDX", "USDT")
    try:
        free_map = balance_info.get("free", {})
        if isinstance(free_map, dict):
            for k, v in free_map.items():
                ku = str(k).upper()
                if any(w in ku for w in wanted):
                    try:
                        return float(v or 0.0)
                    except Exception:
                        pass
        for k, v in balance_info.items():
            ku = str(k).upper()
            if any(w in ku for w in wanted) and isinstance(v, dict) and "free" in v:
                try:
                    return float(v.get("free") or 0.0)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"[BALANCE] extract stable failed: {e}")
    return 1000.0

# === BIAS EMA50 ===
def calculate_bias(tv_symbol: str) -> str:
    ccxt_symbol = resolve_ccxt_symbol(tv_symbol)
    try:
        ohlcv = exchange.fetch_ohlcv(ccxt_symbol, timeframe='5m', limit=120)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
        last_close = float(df['close'].iloc[-1])
        last_ema50 = float(df['ema50'].iloc[-1])
        if last_close > last_ema50:
            return 'LONG'
        elif last_close < last_ema50:
            return 'SHORT'
        return 'NONE'
    except Exception as e:
        logger.error(f"[BIAS] EMA50 failed {tv_symbol} ({ccxt_symbol}): {e}")
        return 'NONE'

# === PRICE HELPERS ===
def safe_last_price(tv_symbol: str) -> float:
    ccxt_symbol = resolve_ccxt_symbol(tv_symbol)
    try:
        t = exchange.fetch_ticker(ccxt_symbol)
        last = t.get("last") or t.get("close") or 0.0
        return float(last or 0.0)
    except Exception as e:
        logger.error(f"[TICKER] fetch_ticker failed {tv_symbol} ({ccxt_symbol}): {e}")
        return 0.0

# === CALCUL QUANTITE ===
def calculate_quantity(tv_symbol: str, entry_price: float, sl_price: float, risk_pct=RISK_PCT, leverage=LEVERAGE) -> float:
    try:
        balance_info = safe_fetch_balance()
        logger.info(f"[DEBUG BALANCE] keys={list(balance_info.keys()) if isinstance(balance_info, dict) else 'N/A'}")
        logger.info(f"[DEBUG BALANCE] raw={balance_info}")
        balance = extract_stable_free_balance(balance_info)
        if balance == 1000.0:
            logger.warning("[SIZE] No USDX/USDT found -> fallback 1000 used.")
        entry_price = float(entry_price)
        sl_price = float(sl_price)
        if entry_price <= 0:
            logger.error("[SIZE] entry_price <= 0")
            return 0.001
        distance = abs(entry_price - sl_price) / entry_price
        distance = max(distance, 0.0005)
        risk_amount = float(balance) * float(risk_pct)
        quantity = (risk_amount * float(leverage)) / (entry_price * distance)
        ccxt_symbol = resolve_ccxt_symbol(tv_symbol)
        try:
            quantity = float(exchange.amount_to_precision(ccxt_symbol, quantity))
        except Exception as e:
            logger.warning(f"[PRECISION] amount_to_precision failed {tv_symbol} ({ccxt_symbol}): {e}")
            quantity = float(quantity)
        if not math.isfinite(quantity) or quantity <= 0:
            return 0.001
        logger.info(f"[SIZE] {tv_symbol} qty={quantity} (bal={balance}, risk={risk_amount}, dist={distance}, lev={leverage})")
        return quantity
    except Exception as e:
        logger.error(f"[SIZE] calculate_quantity failed {tv_symbol}: {e}")
        return 0.001

# === WEBHOOK ROUTE ===
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            logger.error("[WEBHOOK] No JSON or invalid JSON")
            return jsonify({'status': 'ERROR', 'error': 'No/Invalid JSON'}), 200
        if data.get('secret') != WEBHOOK_SECRET:
            logger.warning("[WEBHOOK] Invalid secret")
            return jsonify({'status': 'ERROR', 'error': 'Invalid secret'}), 200
        symbol = (data.get('symbol') or data.get('ticker') or "").strip()
        if symbol not in ALLOWED_SYMBOLS:
            logger.warning(f"[WEBHOOK] Invalid symbol: {symbol}")
            return jsonify({'status': 'ERROR', 'error': 'Invalid symbol'}), 200
        try:
            bar_id = int(data.get('bar_id', 0) or 0)
        except Exception:
            bar_id = 0
        state = get_state(symbol)
        if data.get('ema50_cross_long'):
            update_state(symbol, bias='LONG')
            state['bias'] = 'LONG'
        elif data.get('ema50_cross_short'):
            update_state(symbol, bias='SHORT')
            state['bias'] = 'SHORT'
        action = 'IGNORE'
        reason = ''
        if state['in_position']:
            if state['side'] == 'LONG':
                if data.get('hd') or data.get('ema5_cross_down'):
                    action = 'EXIT_LONG'
                    reason = 'HD or EMA5 cross down'
                    update_state(symbol, in_position=False, side=None)
            elif state['side'] == 'SHORT':
                if data.get('ld') or data.get('ema5_cross_up'):
                    action = 'EXIT_SHORT'
                    reason = 'LD or EMA5 cross up'
                    update_state(symbol, in_position=False, side=None)
        else:
            if data.get('ld') and state['bias'] == 'LONG' and bar_id != state['last_entry_bar_id']:
                entry_price = safe_last_price(symbol)
                if entry_price <= 0:
                    logger.error("entry_price invalid -> abort enter long")
                else:
                    try:
                        sl_price = float(data.get('last_swing_low', 0) or 0) * (1 - BUFFER_PCT)
                        if sl_price <= 0:
                            logger.error("sl_price invalid -> abort enter long")
                        else:
                            qty = calculate_quantity(symbol, entry_price, sl_price)
                            if qty > 0:
                                ccxt_symbol = resolve_ccxt_symbol(symbol)
                                exchange.create_order(
                                    ccxt_symbol, 'limit', 'buy', qty, entry_price,
                                    params={
                                        'leverage': LEVERAGE,
                                        'stopLossPrice': sl_price,
                                    }
                                )
                                update_state(symbol, in_position=True, side='LONG', last_entry_bar_id=bar_id)
                                action = 'ENTER_LONG'
                                reason = 'LD + bias LONG'
                    except Exception as e:
                        logger.error(f"Erreur enter LONG {symbol}: {e}")
            elif data.get('hd') and state['bias'] == 'SHORT' and bar_id != state['last_entry_bar_id']:
                entry_price = safe_last_price(symbol)
                if entry_price <= 0:
                    logger.error("entry_price invalid -> abort enter short")
                else:
                    try:
                        sl_price = float(data.get('last_swing_high', 0) or 0) * (1 + BUFFER_PCT)
                        if sl_price <= 0:
                            logger.error("sl_price invalid -> abort enter short")
                        else:
                            qty = calculate_quantity(symbol, entry_price, sl_price)
                            if qty > 0:
                                ccxt_symbol = resolve_ccxt_symbol(symbol)
                                exchange.create_order(
                                    ccxt_symbol, 'limit', 'sell', qty, entry_price,
                                    params={
                                        'leverage': LEVERAGE,
                                        'stopLossPrice': sl_price,
                                    }
                                )
                                update_state(symbol, in_position=True, side='SHORT', last_entry_bar_id=bar_id)
                                action = 'ENTER_SHORT'
                                reason = 'HD + bias SHORT'
                    except Exception as e:
                        logger.error(f"Erreur enter SHORT {symbol}: {e}")
        logger.info(f"{symbol}: {action} - {reason}")
        return jsonify({'status': action, 'reason': reason}), 200
    except Exception as e:
        logger.error(f"[WEBHOOK] Crash prevented: {str(e)}")
        return jsonify({'status': 'ERROR', 'error': str(e)}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
