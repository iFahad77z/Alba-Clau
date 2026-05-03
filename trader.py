"""Multi-strategy paper trader on Alpaca.

A: 9/21 EMA crossover (5-min bars)
B: 50/200 EMA crossover (5-min bars)
C: 20-bar Donchian channel breakout (10-bar exit)

Each strategy: 1/3 of total equity per entry, max one position at a time.
All: 1.5x ATR(14) hard stop, force-close stocks at 19:30 UTC, BTC trades 24/7.
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
HEADERS = {'APCA-API-KEY-ID': KEY, 'APCA-API-SECRET-KEY': SECRET}

STATE_PATH = Path('scalper_state.json')

ATR_PERIOD = 14
ATR_MULT = 1.5
CASH_PCT = 0.99
VOL_MULT_STOCKS = 1.0
VOL_LOOKBACK = 20
DONCHIAN_PERIOD = 20
DONCHIAN_EXIT_PERIOD = 10

WATCHLIST = [
    ('GOOGL', False), ('AMZN', False), ('MSFT', False), ('NVDA', False),
    ('CF', False), ('NVO', False), ('AMD', False), ('AAPL', False),
    ('TSLA', False), ('AMAT', False), ('MU', False), ('NKE', False),
    ('LLY', False), ('SLB', False), ('CLS', False), ('STX', False),
    ('LRCX', False), ('QCOM', False), ('KLAC', False), ('TXN', False),
    ('CVX', False), ('XOM', False), ('GLW', False), ('BTC/USD', True),
]


def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
    print(f'[{ts}] {msg}', flush=True)


def tg(msg):
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


def get_bars(symbol, is_crypto):
    start = (datetime.now(timezone.utc) - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    if is_crypto:
        url = DATA_CRYPTO
        params = {'symbols': symbol, 'timeframe': '5Min', 'limit': 1000, 'sort': 'asc', 'start': start}
    else:
        url = DATA_STOCKS
        params = {'symbols': symbol, 'timeframe': '5Min', 'limit': 1000, 'sort': 'asc', 'start': start, 'feed': 'iex'}
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


def buy_notional(symbol, notional, is_crypto):
    body = {
        'symbol': symbol,
        'notional': round(notional, 2),
        'side': 'buy',
        'type': 'market',
        'time_in_force': 'gtc' if is_crypto else 'day',
    }
    try:
        r = requests.post(f'{TRADE_BASE}/orders', headers=HEADERS, json=body, timeout=15)
        r.raise_for_status()
        d = r.json()
        log(f"ORDER PLACED BUY {symbol} notional=${round(notional, 2)} id={d['id']} status={d['status']}")
        return d
    except Exception as e:
        log(f'ORDER FAILED BUY {symbol}: {e}')
        return None


def close_position(symbol):
    key = symbol.replace('/', '')
    try:
        r = requests.delete(f'{TRADE_BASE}/positions/{key}', headers=HEADERS, timeout=15)
        if r.status_code == 404:
            log(f'CLOSE SKIPPED {symbol}: no position on Alpaca')
            return None
        r.raise_for_status()
        d = r.json()
        log(f"ORDER PLACED SELL ALL {symbol} qty={d.get('qty')} id={d.get('id')} status={d.get('status')}")
        return d
    except Exception as e:
        log(f'ORDER FAILED SELL {symbol}: {e}')
        return None


def load_state():
    default = {'A': None, 'B': None, 'C': None}
    if not STATE_PATH.exists():
        return default
    try:
        data = json.loads(STATE_PATH.read_text() or '{}')
        if not isinstance(data, dict):
            return default
        # Migration: old single-symbol format -> assign to strategy A
        if 'A' not in data and 'B' not in data and 'C' not in data:
            migrated = default.copy()
            for sym, pos in data.items():
                if isinstance(pos, dict):
                    new_pos = dict(pos)
                    new_pos['symbol'] = sym
                    migrated['A'] = new_pos
                    break
            return migrated
        # Ensure all three keys present
        for k in ('A', 'B', 'C'):
            if k not in data:
                data[k] = None
        return data
    except Exception as e:
        log(f'state load error, resetting: {e}')
        return default


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


# === Signal detection ===

def signal_ema(bars, fast, slow):
    closes = [float(b['c']) for b in bars]
    n = len(closes) - 1
    if n < slow + 1:
        return None
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    if ef[n] is None or es[n] is None or ef[n - 1] is None or es[n - 1] is None:
        return None
    return {
        'bull_cross': ef[n - 1] <= es[n - 1] and ef[n] > es[n],
        'bear_cross': ef[n - 1] >= es[n - 1] and ef[n] < es[n],
        'fast_now': ef[n], 'slow_now': es[n], 'price': closes[n],
    }


def signal_donchian(bars, period=DONCHIAN_PERIOD, exit_period=DONCHIAN_EXIT_PERIOD):
    highs = [float(b['h']) for b in bars]
    lows = [float(b['l']) for b in bars]
    closes = [float(b['c']) for b in bars]
    n = len(closes) - 1
    if n < max(period, exit_period) + 1:
        return None
    hh = max(highs[n - period:n])
    ll = min(lows[n - exit_period:n])
    return {
        'bull_breakout': closes[n] > hh,
        'bear_breakdown': closes[n] < ll,
        'hh': hh, 'll': ll, 'price': closes[n],
    }


def vol_mult(bars):
    vols = [float(b['v']) for b in bars]
    n = len(vols) - 1
    if n < VOL_LOOKBACK:
        return 0
    avg = sum(vols[n - VOL_LOOKBACK:n]) / VOL_LOOKBACK
    return vols[n] / avg if avg > 0 else 0


# === Per-strategy processing ===

def process_exit(strat, state, bars_dict, force_close_stocks):
    pos = state.get(strat)
    if not pos:
        return
    sym = pos['symbol']
    is_crypto = sym == 'BTC/USD'
    bars = bars_dict.get(sym)
    if not bars:
        return
    closes = [float(b['c']) for b in bars]
    price = closes[-1]
    entry = pos['entry']
    stop = pos['stop']

    should_exit, reason = False, ''
    if (not is_crypto) and force_close_stocks:
        should_exit, reason = True, 'FORCE CLOSE (30 min before market close)'
    elif price <= stop:
        should_exit, reason = True, f'STOP HIT (price={price:.4f} <= stop={stop:.4f})'
    elif strat in ('A', 'B'):
        f, s = (9, 21) if strat == 'A' else (50, 200)
        sig = signal_ema(bars, f, s)
        if sig and sig['bear_cross']:
            should_exit, reason = True, f'EMA BEAR CROSS ({f}={sig["fast_now"]:.4f} <= {s}={sig["slow_now"]:.4f})'
    elif strat == 'C':
        sig = signal_donchian(bars)
        if sig and sig['bear_breakdown']:
            should_exit, reason = True, f'DONCHIAN BREAKDOWN (price={price:.4f} < {DONCHIAN_EXIT_PERIOD}-bar low={sig["ll"]:.4f})'

    if should_exit:
        pl_pct = (price - entry) / entry * 100
        log(f'[{strat}] EXIT {sym}: {reason} | entry={entry:.4f} exit~{price:.4f} estP/L={pl_pct:+.2f}%')
        close_position(sym)
        tg(f"[{strat}] SELL {sym}\nReason: {reason}\nEntry: ${entry:.4f}\nExit (approx): ${price:.4f}\nEst P/L: {pl_pct:+.2f}%")
        state[strat] = None
    else:
        pl_pct = (price - entry) / entry * 100
        log(f'[{strat}] HOLD {sym}: price={price:.4f} entry={entry:.4f} stop={stop:.4f} estP/L={pl_pct:+.2f}%')


def process_entry(strat, state, bars_dict, taken_syms, per_strategy_target, block_new_stock_entries):
    if state.get(strat):
        return  # already holding

    for sym, is_crypto in WATCHLIST:
        if sym in taken_syms:
            continue
        if (not is_crypto) and block_new_stock_entries:
            continue
        bars = bars_dict.get(sym)
        if not bars:
            continue

        closes = [float(b['c']) for b in bars]
        highs = [float(b['h']) for b in bars]
        lows = [float(b['l']) for b in bars]
        a14 = atr(highs, lows, closes, ATR_PERIOD)
        if not a14 or a14 <= 0:
            continue
        price = closes[-1]

        fired = False
        details = ''

        if strat in ('A', 'B'):
            f, s = (9, 21) if strat == 'A' else (50, 200)
            sig = signal_ema(bars, f, s)
            if not sig or not sig['bull_cross']:
                continue
            # Volume filter for stocks (not crypto)
            if not is_crypto:
                vm = vol_mult(bars)
                if vm < VOL_MULT_STOCKS:
                    continue
                details = f"{f}/{s} EMA bull cross + vol {vm:.2f}x | {f}EMA={sig['fast_now']:.4f} {s}EMA={sig['slow_now']:.4f}"
            else:
                details = f"{f}/{s} EMA bull cross (BTC, no vol filter) | {f}EMA={sig['fast_now']:.4f} {s}EMA={sig['slow_now']:.4f}"
            fired = True

        elif strat == 'C':
            sig = signal_donchian(bars)
            if not sig or not sig['bull_breakout']:
                continue
            details = f"{DONCHIAN_PERIOD}-bar Donchian breakout | price={price:.4f} broke {DONCHIAN_PERIOD}-bar high={sig['hh']:.4f}"
            fired = True

        if not fired:
            continue

        acct = get_account()
        if not acct:
            continue
        cash = float(acct['cash'])
        notional = min(per_strategy_target, cash * CASH_PCT)
        if notional < 1:
            log(f'[{strat}] BUY SKIPPED {sym}: insufficient cash (${cash:.2f})')
            continue

        stop_price = price - ATR_MULT * a14
        log(f'[{strat}] ENTRY SIGNAL {sym} @ {price:.4f} | {details} | ATR(14)={a14:.4f} stop={stop_price:.4f} notional=${notional:.2f}')
        order = buy_notional(sym, notional, is_crypto)
        if order:
            state[strat] = {
                'symbol': sym,
                'entry': price,
                'stop': stop_price,
                'atr': a14,
                'entry_time': datetime.now(timezone.utc).isoformat(),
                'order_id': order['id'],
            }
            taken_syms.add(sym)
            tg(f"[{strat}] BUY {sym} @ ${price:.4f}\nReason: {details}\nATR(14): {a14:.4f}\nStop: ${stop_price:.4f}\nNotional: ${notional:.2f}")
            return  # one entry per strategy per tick


def run():
    log('=== Multi-strategy LIVE tick ===')

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
    log(f'Time: UTC={now_utc:%H:%M} marketOpen={market_open} blockStockEntries={block_new_stock_entries} forceCloseStocks={force_close_stocks}')

    state = load_state()

    acct = get_account()
    if not acct:
        log('Could not fetch account; aborting tick')
        save_state(state)
        return
    equity = float(acct['equity'])
    per_strategy_target = round(equity / 3 * CASH_PCT, 2)
    log(f'Equity: ${equity:.2f} | per-strategy target: ${per_strategy_target:.2f}')
    log(f'State: A={state["A"] and state["A"]["symbol"] or "flat"} | B={state["B"] and state["B"]["symbol"] or "flat"} | C={state["C"] and state["C"]["symbol"] or "flat"}')

    bars_dict = {}
    for sym, is_crypto in WATCHLIST:
        bars = get_bars(sym, is_crypto)
        if bars and len(bars) >= 250:
            bars_dict[sym] = bars

    # Exits first
    for s in ('A', 'B', 'C'):
        process_exit(s, state, bars_dict, force_close_stocks)

    # Recompute taken symbols after exits
    taken_syms = {state[s]['symbol'] for s in ('A', 'B', 'C') if state.get(s)}

    # Entries
    for s in ('A', 'B', 'C'):
        process_entry(s, state, bars_dict, taken_syms, per_strategy_target, block_new_stock_entries)

    save_state(state)


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        log(f'fatal: {e}')
        raise
