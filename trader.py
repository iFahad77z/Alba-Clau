"""
9/21 EMA + 1.2x volume + 1.5x ATR(14) hard stop scalper.
LIVE on Alpaca paper trading. 100% of cash per entry, one position at a time.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KEY = os.environ['ALPACA_KEY']
SECRET = os.environ['ALPACA_SECRET']
TG_TOKEN = os.environ.get('TG_TOKEN', '')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID', '')

TRADE_BASE = 'https://paper-api.alpaca.markets/v2'
DATA_STOCKS = 'https://data.alpaca.markets/v2/stocks/bars'
DATA_CRYPTO = 'https://data.alpaca.markets/v1beta3/crypto/us/bars'

HEADERS = {
    'APCA-API-KEY-ID': KEY,
    'APCA-API-SECRET-KEY': SECRET,
}

STATE_PATH = Path('scalper_state.json')

# Strategy parameters
EMA_FAST = 9
EMA_SLOW = 21
VOL_LOOKBACK = 20
VOL_MULT = 1.0
ATR_PERIOD = 14
ATR_MULT = 1.5
CASH_PCT = 0.99

WATCHLIST = [
    ('GOOGL',   False),
    ('AMZN',    False),
    ('MSFT',    False),
    ('NVDA',    False),
    ('CF',      False),
    ('NVO',     False),
    ('AMD',     False),
    ('AAPL',    False),
    ('TSLA',    False),
    ('AMAT',    False),
    ('MU',      False),
    ('NKE',     False),
    ('LLY',     False),
    ('SLB',     False),
    ('CLS',     False),
    ('STX',     False),
    ('LRCX',    False),
    ('QCOM',    False),
    ('KLAC',    False),
    ('TXN',     False),
    ('CVX',     False),
    ('XOM',     False),
    ('GLW',     False),
    ('BTC/USD', True),
]


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
    print(f'[{ts}] {msg}', flush=True)


def tg(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.get(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            params={'chat_id': TG_CHAT_ID, 'text': msg},
            timeout=10,
        )
    except Exception as e:
        log(f'TG send failed: {e}')


def get_bars(symbol: str, is_crypto: bool):
    start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')
    if is_crypto:
        url = DATA_CRYPTO
        params = {'symbols': symbol, 'timeframe': '5Min', 'limit': 500, 'sort': 'asc', 'start': start}
    else:
        url = DATA_STOCKS
        params = {'symbols': symbol, 'timeframe': '5Min', 'limit': 500, 'sort': 'asc', 'start': start, 'feed': 'iex'}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        r.raise_for_status()
        return r.json().get('bars', {}).get(symbol, [])
    except Exception as e:
        log(f'ERROR fetching {symbol}: {e}')
        return []


def ema_series(values, period):
    if len(values) < period:
        return [None] * len(values)
    alpha = 2.0 / (period + 1)
    out = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    e = seed
    for i in range(period, len(values)):
        e = alpha * values[i] + (1 - alpha) * e
        out.append(e)
    return out


def atr(highs, lows, closes, period):
    if len(highs) < period + 1:
        return None
    trs = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    a = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
    return a


def get_account():
    try:
        r = requests.get(f'{TRADE_BASE}/account', headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f'ERROR account: {e}')
        return None


def buy_notional(symbol, cash, is_crypto):
    notional = round(cash * CASH_PCT, 2)
    if notional < 1:
        log(f'BUY SKIPPED {symbol}: insufficient cash (${cash})')
        return None
    body = {
        'symbol': symbol,
        'notional': notional,
        'side': 'buy',
        'type': 'market',
        'time_in_force': 'gtc' if is_crypto else 'day',
    }
    try:
        r = requests.post(f'{TRADE_BASE}/orders', headers=HEADERS, json=body, timeout=15)
        r.raise_for_status()
        d = r.json()
        log(f"ORDER PLACED BUY {symbol} notional=${notional} id={d['id']} status={d['status']}")
        return d
    except Exception as e:
        log(f'ORDER FAILED BUY {symbol}: {e}')
        return None


def close_position(symbol):
    # Alpaca position endpoint takes BTCUSD (no slash) for crypto
    key = symbol.replace('/', '')
    try:
        r = requests.delete(f'{TRADE_BASE}/positions/{key}', headers=HEADERS, timeout=15)
        if r.status_code == 404:
            log(f'CLOSE SKIPPED {symbol}: no position')
            return None
        r.raise_for_status()
        d = r.json()
        log(f"ORDER PLACED SELL ALL {symbol} qty={d.get('qty')} id={d.get('id')} status={d.get('status')}")
        return d
    except Exception as e:
        log(f'ORDER FAILED SELL {symbol}: {e}')
        return None


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text() or '{}')
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def run():
    log('=== Scalper LIVE tick ===')
    # Time-based gating (UTC; US market hours during DST: 13:30-20:00 UTC)
    now_utc = datetime.now(timezone.utc)
    utc_min = now_utc.hour * 60 + now_utc.minute
    market_open_min = 13 * 60 + 30
    market_close_min = 20 * 60
    no_entry_after_min = market_close_min - 60
    force_close_at_min = market_close_min - 30
    is_weekend = now_utc.weekday() >= 5
    market_open = (not is_weekend) and market_open_min <= utc_min < market_close_min
    block_new_stock_entries = (not market_open) or utc_min >= no_entry_after_min
    force_close_stocks = (not is_weekend) and force_close_at_min <= utc_min < market_close_min
    log(f"Time: UTC={now_utc:%H:%M} marketOpen={market_open} blockStockEntries={block_new_stock_entries} forceCloseStocks={force_close_stocks}")

    state = load_state()
    open_symbol = next(iter(state), None)

    for sym, is_crypto in WATCHLIST:
        bars = get_bars(sym, is_crypto)
        if not bars or len(bars) < 50:
            log(f'SKIP {sym}: only {len(bars)} bars')
            continue
        closes = [float(b['c']) for b in bars]
        highs = [float(b['h']) for b in bars]
        lows = [float(b['l']) for b in bars]
        vols = [float(b['v']) for b in bars]
        n = len(closes) - 1
        e9 = ema_series(closes, EMA_FAST)
        e21 = ema_series(closes, EMA_SLOW)
        a14 = atr(highs, lows, closes, ATR_PERIOD)
        if e9[n] is None or e21[n] is None or a14 is None:
            continue
        price = closes[n]

        # Detect cross within last 2 bars (n-2 -> n-1 OR n-1 -> n) to handle cron-vs-bar-close timing
        bull = False
        bear = False
        cross_bar = n
        for k in (0, 1):
            cur, prv = n - k, n - k - 1
            if prv < 0:
                break
            if not bull and e9[prv] <= e21[prv] and e9[cur] > e21[cur]:
                bull, cross_bar = True, cur
            if not bear and e9[prv] >= e21[prv] and e9[cur] < e21[cur]:
                bear, cross_bar = True, cur
            if bull or bear:
                break

        vol_bar = cross_bar if (bull or bear) else n
        vol_start = max(0, vol_bar - VOL_LOOKBACK)
        vol_window = vols[vol_start:vol_bar]
        vol_avg = sum(vol_window) / len(vol_window) if vol_window else 0
        vol_mult = vols[vol_bar] / vol_avg if vol_avg > 0 else 0

        if sym in state:
            pos = state[sym]
            entry = pos['entry']
            stop = pos['stop']
            should_exit, reason = False, ''
            if (not is_crypto) and force_close_stocks:
                should_exit, reason = True, 'FORCE CLOSE (30 min before market close)'
            elif price <= stop:
                should_exit, reason = True, f'STOP HIT (price={price:.4f} <= stop={stop:.4f})'
            elif bear:
                should_exit, reason = True, f'EMA BEAR CROSS (9={e9[n]:.4f} <= 21={e21[n]:.4f})'
            if should_exit:
                pl_pct = (price - entry) / entry * 100
                log(f'EXIT {sym}: {reason} | entry={entry:.4f} exit~{price:.4f} estP/L={pl_pct:.2f}%')
                close_position(sym)
                tg(f"SELL {sym}\nReason: {reason}\nEntry: ${entry:.4f}\nExit (approx): ${price:.4f}\nEst P/L: {pl_pct:+.2f}%")
                del state[sym]
                open_symbol = None
            else:
                pl_pct = (price - entry) / entry * 100
                log(f'HOLD {sym}: price={price:.4f} entry={entry:.4f} stop={stop:.4f} estP/L={pl_pct:.2f}%')
        elif open_symbol is None and bull and (is_crypto or vol_mult >= VOL_MULT) and a14 > 0 and (is_crypto or not block_new_stock_entries):
            stop_price = price - ATR_MULT * a14
            acct = get_account()
            if not acct:
                continue
            cash = float(acct['cash'])
            log(f'ENTRY SIGNAL {sym} @ {price:.4f} | reason: 9EMA bull cross 21EMA on volume={vol_mult:.2f}x avg | 9EMA={e9[n]:.4f} 21EMA={e21[n]:.4f} ATR(14)={a14:.4f} stop={stop_price:.4f} | cash=${cash:.2f}')
            order = buy_notional(sym, cash, is_crypto)
            if order:
                state[sym] = {
                    'entry': price,
                    'stop': stop_price,
                    'atr': a14,
                    'entry_time': datetime.now(timezone.utc).isoformat(),
                    'order_id': order['id'],
                }
                open_symbol = sym
                notional = round(cash * CASH_PCT, 2)
                tg(f"BUY {sym} @ ${price:.4f}\nReason: 9/21 EMA bull cross + vol {vol_mult:.2f}x\n9 EMA: {e9[n]:.4f}\n21 EMA: {e21[n]:.4f}\nATR(14): {a14:.4f}\nStop: ${stop_price:.4f}\nNotional: ${notional}\nCash before: ${cash:.2f}")

    save_state(state)


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        log(f'fatal: {e}')
        raise
