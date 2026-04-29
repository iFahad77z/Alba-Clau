"""
Dual-strategy trading watcher for GitHub Actions.
v1: 50/200 SMA Golden Cross on 3-min bars (LIVE — places real Alpaca paper orders)
v2: 9/21 EMA + 1.2x 20-bar volume filter + 1.5x ATR(14) hard stop (SHADOW — simulated)
"""
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KEY = os.environ['ALPACA_KEY']
SECRET = os.environ['ALPACA_SECRET']

TRADE_BASE = 'https://paper-api.alpaca.markets/v2'
DATA_STOCKS = 'https://data.alpaca.markets/v2/stocks/bars'
DATA_CRYPTO = 'https://data.alpaca.markets/v1beta3/crypto/us/bars'

HEADERS = {
    'APCA-API-KEY-ID': KEY,
    'APCA-API-SECRET-KEY': SECRET,
}

V2_STATE_PATH = Path('v2_state.json')
V2_NOTIONAL = 1000.0

WATCHLIST = [
    ('GOOGL',   False),
    ('AMZN',    False),
    ('MSFT',    False),
    ('NVDA',    False),
    ('CF',      False),
    ('NVO',     False),
    ('AMD',     False),
    ('BTC/USD', True),
]


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
    print(f'[{ts}] {msg}', flush=True)


def get_bars(symbol: str, is_crypto: bool):
    start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')
    if is_crypto:
        url = DATA_CRYPTO
        params = {'symbols': symbol, 'timeframe': '3Min', 'limit': 1000, 'sort': 'asc', 'start': start}
    else:
        url = DATA_STOCKS
        params = {'symbols': symbol, 'timeframe': '3Min', 'limit': 1000, 'sort': 'asc', 'start': start, 'feed': 'iex'}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        r.raise_for_status()
        return r.json().get('bars', {}).get(symbol, [])
    except Exception as e:
        log(f'ERROR fetching {symbol}: {e}')
        return []


def sma(values, period: int, end_idx: int):
    if end_idx - period + 1 < 0:
        return None
    return sum(values[end_idx - period + 1:end_idx + 1]) / period


def ema_series(values, period: int):
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


def atr(highs, lows, closes, period: int):
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
        log(f'ERROR fetching account: {e}')
        return None


def buy_notional(symbol: str, cash: float, is_crypto: bool):
    notional = round(cash * 0.99, 2)
    if notional < 1:
        log(f'BUY SKIPPED {symbol}: insufficient cash (${cash})')
        return
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
        log(f"V1 ORDER PLACED BUY {symbol} notional=${notional} id={d['id']} status={d['status']}")
    except Exception as e:
        log(f'V1 ORDER FAILED BUY {symbol}: {e} — {getattr(e, "response", None) and e.response.text}')


def close_position(symbol: str):
    encoded = urllib.parse.quote(symbol, safe='')
    try:
        rg = requests.get(f'{TRADE_BASE}/positions/{encoded}', headers=HEADERS, timeout=15)
        if rg.status_code == 404:
            log(f'V1 CLOSE SKIPPED {symbol}: no open position')
            return
        rg.raise_for_status()
        pos_qty = rg.json().get('qty')
        rd = requests.delete(f'{TRADE_BASE}/positions/{encoded}', headers=HEADERS, timeout=15)
        rd.raise_for_status()
        d = rd.json()
        log(f"V1 ORDER PLACED SELL ALL {symbol} qty={pos_qty} id={d.get('id')} status={d.get('status')}")
    except Exception as e:
        log(f'V1 ORDER FAILED SELL {symbol}: {e}')


def run_v1():
    log('=== V1 GOLDEN CROSS (LIVE) ===')
    for sym, is_crypto in WATCHLIST:
        bars = get_bars(sym, is_crypto)
        if not bars or len(bars) < 201:
            log(f'V1 SKIP {sym}: only {len(bars)} bars (need 201)')
            continue
        closes = [float(b['c']) for b in bars]
        n = len(closes) - 1
        s50_now = sma(closes, 50, n)
        s200_now = sma(closes, 200, n)
        s50_prev = sma(closes, 50, n - 1)
        s200_prev = sma(closes, 200, n - 1)
        golden = s50_prev <= s200_prev and s50_now > s200_now
        death = s50_prev >= s200_prev and s50_now < s200_now
        state = 'above' if s50_now > s200_now else 'below'
        tag = ''
        if golden:
            tag = '  <<< GOLDEN CROSS'
        elif death:
            tag = '  <<< DEATH CROSS'
        log(f'V1 {sym}: 50={s50_now:.4f} 200={s200_now:.4f} ({state}){tag}')
        if golden:
            acct = get_account()
            if acct:
                cash = float(acct['cash'])
                log(f'V1 Account cash: ${cash}')
                buy_notional(sym, cash, is_crypto)
        elif death:
            close_position(sym)


def load_v2_state():
    if V2_STATE_PATH.exists():
        try:
            return json.loads(V2_STATE_PATH.read_text() or '{}')
        except Exception:
            return {}
    return {}


def save_v2_state(state):
    V2_STATE_PATH.write_text(json.dumps(state, indent=2))


def run_v2():
    log('=== V2 EMA SCALPER (SHADOW) ===')
    state = load_v2_state()
    for sym, is_crypto in WATCHLIST:
        bars = get_bars(sym, is_crypto)
        if not bars or len(bars) < 50:
            log(f'V2 SKIP {sym}: only {len(bars)} bars')
            continue
        closes = [float(b['c']) for b in bars]
        highs = [float(b['h']) for b in bars]
        lows = [float(b['l']) for b in bars]
        vols = [float(b['v']) for b in bars]
        n = len(closes) - 1
        e9 = ema_series(closes, 9)
        e21 = ema_series(closes, 21)
        a14 = atr(highs, lows, closes, 14)
        if e9[n] is None or e21[n] is None or a14 is None:
            continue
        vol_avg = sum(vols[n - 20:n]) / 20 if n >= 20 else 0
        vol_mult = vols[n] / vol_avg if vol_avg > 0 else 0
        bull = e9[n - 1] <= e21[n - 1] and e9[n] > e21[n]
        bear = e9[n - 1] >= e21[n - 1] and e9[n] < e21[n]
        price = closes[n]
        if sym in state:
            pos = state[sym]
            entry = pos['entry']
            stop = pos['stop']
            qty = pos['qty']
            pl = (price - entry) * qty
            pl_pct = (price - entry) / entry * 100
            if price <= stop:
                log(f'V2 EXIT (STOP HIT) {sym}: entry={entry:.4f} stop={stop:.4f} exit={price:.4f} qty={qty:.6f} P/L=${pl:.2f} ({pl_pct:.2f}%)')
                del state[sym]
            elif bear:
                log(f'V2 EXIT (BEAR CROSS) {sym}: entry={entry:.4f} exit={price:.4f} qty={qty:.6f} P/L=${pl:.2f} ({pl_pct:.2f}%) [9={e9[n]:.4f} 21={e21[n]:.4f}]')
                del state[sym]
            else:
                log(f'V2 HOLD {sym}: price={price:.4f} entry={entry:.4f} stop={stop:.4f} unrealized=${pl:.2f} ({pl_pct:.2f}%)')
        else:
            if bull and vol_mult >= 1.2 and a14 > 0:
                stop_price = price - 1.5 * a14
                qty = V2_NOTIONAL / price
                state[sym] = {
                    'entry': price,
                    'stop': stop_price,
                    'qty': qty,
                    'atr': a14,
                    'entry_time': datetime.now(timezone.utc).isoformat(),
                }
                log(f'V2 ENTER LONG {sym} @ {price:.4f} | reason: 9EMA bull cross 21EMA on volume={vol_mult:.2f}x avg | 9={e9[n]:.4f} 21={e21[n]:.4f} ATR={a14:.4f} stop={stop_price:.4f} qty={qty:.6f} (${V2_NOTIONAL} notional)')
    save_v2_state(state)


if __name__ == '__main__':
    try:
        run_v1()
    except Exception as e:
        log(f'V1 fatal: {e}')
    try:
        run_v2()
    except Exception as e:
        log(f'V2 fatal: {e}')
