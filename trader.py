"""Multi-strategy paper trader on Alpaca (10 strategies, 10% equity each).

Strategies:
  A: 9/21 EMA crossover
  B: 50/200 EMA crossover
  C: 20-bar Donchian channel breakout (10-bar exit)
  D: MACD (12/26/9) bullish crossover
  E: Bollinger Bands (20, 2.0) mean reversion
  F: RSI(14) oversold bounce
  G: SuperTrend (10, 3.0) bullish flip
  H: Opening Range Breakout (first 30 min of US session) — stocks only
  I: VWAP Reclaim (intraday) — stocks only
  J: Inside Bar Breakout — pattern based

All: 1.5x ATR(14) hard stop, force-close stocks at 19:30 UTC, BTC trades 24/7.
Volume filter (1.0x) applied to: A, B, D, G, H, J on stocks always; BTC only during market hours.
Strategies E, F, I have no separate volume filter (mean-reversion / VWAP-implicit).
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
VOL_MULT_THRESHOLD = 1.0
VOL_LOOKBACK = 20

DONCHIAN_PERIOD = 20
DONCHIAN_EXIT_PERIOD = 10
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
ST_PERIOD = 10
ST_MULT = 3.0
ORB_BARS = 6  # first 6 x 5-min bars = first 30 min of session

ALL_STRATS = ('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J')

STRAT_NAMES = {
    'A': 'Fast EMA Cross (9/21)',
    'B': 'Slow EMA Cross (50/200)',
    'C': 'Donchian Breakout (20/10)',
    'D': 'MACD (12/26/9)',
    'E': 'Bollinger Reversion (20, 2σ)',
    'F': 'RSI Bounce (14)',
    'G': 'SuperTrend (10, 3.0)',
    'H': 'Opening Range Breakout',
    'I': 'VWAP Reclaim',
    'J': 'Inside Bar Breakout',
}

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


def close_position(symbol, qty=None):
    """Close part or all of a position. If qty is given, close only that quantity."""
    key = symbol.replace('/', '')
    try:
        if qty is not None:
            # Close partial via a sell order
            is_crypto = '/' in symbol
            body = {
                'symbol': symbol,
                'qty': str(qty),
                'side': 'sell',
                'type': 'market',
                'time_in_force': 'gtc' if is_crypto else 'day',
            }
            r = requests.post(f'{TRADE_BASE}/orders', headers=HEADERS, json=body, timeout=15)
            r.raise_for_status()
            d = r.json()
            log(f"ORDER PLACED PARTIAL SELL {symbol} qty={qty} id={d.get('id')} status={d.get('status')}")
            return d
        else:
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
    default = {k: None for k in ALL_STRATS}
    if not STATE_PATH.exists():
        return default
    try:
        data = json.loads(STATE_PATH.read_text() or '{}')
        if not isinstance(data, dict):
            return default
        # Migrate old single-symbol format
        if not any(k in data for k in ALL_STRATS):
            migrated = default.copy()
            for sym, pos in data.items():
                if isinstance(pos, dict):
                    new_pos = dict(pos)
                    new_pos['symbol'] = sym
                    migrated['A'] = new_pos
                    break
            return migrated
        # Ensure all strategy keys present
        for k in ALL_STRATS:
            if k not in data:
                data[k] = None
        return data
    except Exception as e:
        log(f'state load error, resetting: {e}')
        return default


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ===== Signal functions =====

def signal_ema_cross(bars, fast, slow):
    closes = [float(b['c']) for b in bars]
    n = len(closes) - 1
    if n < slow + 1:
        return None
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    if any(x is None for x in (ef[n], es[n], ef[n - 1], es[n - 1])):
        return None
    return {
        'bull': ef[n - 1] <= es[n - 1] and ef[n] > es[n],
        'bear': ef[n - 1] >= es[n - 1] and ef[n] < es[n],
        'fast': ef[n], 'slow': es[n], 'price': closes[n],
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
        'bull': closes[n] > hh,
        'bear': closes[n] < ll,
        'hh': hh, 'll': ll, 'price': closes[n],
    }


def signal_macd(bars, fast=MACD_FAST, slow=MACD_SLOW, sig_p=MACD_SIGNAL):
    closes = [float(b['c']) for b in bars]
    n = len(closes) - 1
    if n < slow + sig_p + 2:
        return None
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    macd_line = []
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None:
            macd_line.append(ef[i] - es[i])
        else:
            macd_line.append(None)
    valid_macd = [m for m in macd_line if m is not None]
    if len(valid_macd) < sig_p + 2:
        return None
    sig_ema = ema_series(valid_macd, sig_p)
    pad = len(macd_line) - len(sig_ema)
    sig_full = [None] * pad + sig_ema
    if any(x is None for x in (macd_line[n], sig_full[n], macd_line[n - 1], sig_full[n - 1])):
        return None
    return {
        'bull': macd_line[n - 1] <= sig_full[n - 1] and macd_line[n] > sig_full[n],
        'bear': macd_line[n - 1] >= sig_full[n - 1] and macd_line[n] < sig_full[n],
        'macd': macd_line[n], 'signal': sig_full[n], 'price': closes[n],
    }


def signal_bollinger(bars, period=BB_PERIOD, stdv=BB_STD):
    closes = [float(b['c']) for b in bars]
    n = len(closes) - 1
    if n < period + 1:
        return None

    def bb(end_idx):
        win = closes[end_idx - period + 1:end_idx + 1]
        m = sum(win) / period
        v = sum((c - m) ** 2 for c in win) / period
        s = v ** 0.5
        return m, m + stdv * s, m - stdv * s

    sma_now, upper_now, lower_now = bb(n)
    sma_prev, _, lower_prev = bb(n - 1)
    return {
        'bull': closes[n - 1] < lower_prev and closes[n] > lower_now,  # bounce off lower band
        'reach_mid': closes[n] >= sma_now,
        'sma': sma_now, 'upper': upper_now, 'lower': lower_now, 'price': closes[n],
    }


def signal_rsi(bars, period=RSI_PERIOD):
    closes = [float(b['c']) for b in bars]
    n = len(closes) - 1
    if n < period + 2:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [-min(d, 0) for d in deltas]
    if len(gains) < period + 1:
        return None

    def rsi_at(end_idx_in_deltas):
        ag = sum(gains[:period]) / period
        al = sum(losses[:period]) / period
        for i in range(period, end_idx_in_deltas + 1):
            ag = (ag * (period - 1) + gains[i]) / period
            al = (al * (period - 1) + losses[i]) / period
        if al == 0:
            return 100
        rs = ag / al
        return 100 - 100 / (1 + rs)

    rsi_now = rsi_at(len(gains) - 1)
    rsi_prev = rsi_at(len(gains) - 2)
    return {
        'bull': rsi_prev <= RSI_OVERSOLD and rsi_now > RSI_OVERSOLD,
        'overbought': rsi_now >= RSI_OVERBOUGHT,
        'rsi': rsi_now, 'rsi_prev': rsi_prev, 'price': closes[n],
    }


def signal_supertrend(bars, period=ST_PERIOD, mult=ST_MULT):
    highs = [float(b['h']) for b in bars]
    lows = [float(b['l']) for b in bars]
    closes = [float(b['c']) for b in bars]
    n = len(closes) - 1
    if n < period + 2:
        return None
    trs = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atrs = [None] * (period - 1)
    a = sum(trs[:period]) / period
    atrs.append(a)
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        atrs.append(a)
    hl2 = [(highs[i] + lows[i]) / 2 for i in range(len(closes))]
    upper_basic = [hl2[i] + mult * atrs[i] if atrs[i] else None for i in range(len(closes))]
    lower_basic = [hl2[i] - mult * atrs[i] if atrs[i] else None for i in range(len(closes))]
    upper_final = [None] * len(closes)
    lower_final = [None] * len(closes)
    direction = [None] * len(closes)
    first = period
    if first >= len(closes):
        return None
    upper_final[first] = upper_basic[first]
    lower_final[first] = lower_basic[first]
    direction[first] = 1 if closes[first] > upper_basic[first] else -1
    for i in range(first + 1, len(closes)):
        if upper_basic[i] is None:
            continue
        if upper_basic[i] < upper_final[i - 1] or closes[i - 1] > upper_final[i - 1]:
            upper_final[i] = upper_basic[i]
        else:
            upper_final[i] = upper_final[i - 1]
        if lower_basic[i] > lower_final[i - 1] or closes[i - 1] < lower_final[i - 1]:
            lower_final[i] = lower_basic[i]
        else:
            lower_final[i] = lower_final[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if closes[i] < lower_final[i] else 1
        else:
            direction[i] = 1 if closes[i] > upper_final[i] else -1
    if direction[n] is None or direction[n - 1] is None:
        return None
    return {
        'bull': direction[n - 1] == -1 and direction[n] == 1,
        'bear': direction[n - 1] == 1 and direction[n] == -1,
        'direction': direction[n], 'price': closes[n],
    }


def session_bars_today(bars):
    """Filter bars to today's US regular session (13:30–20:00 UTC)."""
    today = datetime.now(timezone.utc).date()
    out = []
    for b in bars:
        try:
            bt = datetime.fromisoformat(b['t'].replace('Z', '+00:00'))
        except Exception:
            continue
        if bt.date() == today and (13 * 60 + 30) <= (bt.hour * 60 + bt.minute) < (20 * 60):
            out.append(b)
    return out


def signal_orb(bars, range_bars=ORB_BARS):
    sb = session_bars_today(bars)
    if len(sb) < range_bars + 2:
        return None
    or_bars = sb[:range_bars]
    or_high = max(float(b['h']) for b in or_bars)
    or_low = min(float(b['l']) for b in or_bars)
    cur_close = float(sb[-1]['c'])
    prev_close = float(sb[-2]['c'])
    return {
        'bull': prev_close <= or_high and cur_close > or_high,
        'bear': prev_close >= or_low and cur_close < or_low,
        'or_high': or_high, 'or_low': or_low, 'price': cur_close,
    }


def signal_vwap(bars):
    sb = session_bars_today(bars)
    if len(sb) < 5:
        return None
    closes = [float(b['c']) for b in sb]
    highs = [float(b['h']) for b in sb]
    lows = [float(b['l']) for b in sb]
    vols = [float(b['v']) for b in sb]
    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(sb))]
    cum_pv = 0
    cum_v = 0
    vwaps = []
    for i in range(len(sb)):
        cum_pv += typical[i] * vols[i]
        cum_v += vols[i]
        vwaps.append(cum_pv / cum_v if cum_v > 0 else typical[i])
    n = len(sb) - 1
    if n < 1:
        return None
    return {
        'bull': closes[n - 1] < vwaps[n - 1] and closes[n] > vwaps[n],
        'below': closes[n] < vwaps[n],
        'vwap': vwaps[n], 'price': closes[n],
    }


def signal_inside_bar(bars):
    """Inside bar: prev bar high < bar before's high AND prev bar low > bar before's low.
       Bull entry: current bar closes above prev bar's high (mother bar)."""
    if len(bars) < 4:
        return None
    n = len(bars) - 1
    h0, l0 = float(bars[n - 2]['h']), float(bars[n - 2]['l'])  # mother bar
    h1, l1 = float(bars[n - 1]['h']), float(bars[n - 1]['l'])  # inside bar candidate
    is_inside = h1 < h0 and l1 > l0
    cur_close = float(bars[n]['c'])
    prev_close = float(bars[n - 1]['c'])
    return {
        'bull': is_inside and prev_close <= h0 and cur_close > h0,
        'bear': is_inside and prev_close >= l0 and cur_close < l0,
        'mother_high': h0, 'mother_low': l0, 'price': cur_close,
    }


def vol_mult(bars):
    vols = [float(b['v']) for b in bars]
    n = len(vols) - 1
    if n < VOL_LOOKBACK:
        return 0
    avg = sum(vols[n - VOL_LOOKBACK:n]) / VOL_LOOKBACK
    return vols[n] / avg if avg > 0 else 0


# ===== Strategy entry/exit dispatch =====

# Strategies that use the 1.0x volume filter on stocks (and BTC during market hours)
VOL_FILTER_STRATS = {'A', 'B', 'D', 'G', 'H', 'J'}
# Strategies that are stock-only (skip BTC)
STOCK_ONLY_STRATS = {'H', 'I'}


def get_entry_signal(strat, bars, sym, is_crypto):
    """Returns (fired: bool, details: str, extra: dict) or (False, '', {})."""
    if strat == 'A':
        sig = signal_ema_cross(bars, 9, 21)
        if sig and sig['bull']:
            return True, f"9/21 EMA bull cross | 9EMA={sig['fast']:.4f} 21EMA={sig['slow']:.4f}", sig
    elif strat == 'B':
        sig = signal_ema_cross(bars, 50, 200)
        if sig and sig['bull']:
            return True, f"50/200 EMA bull cross | 50EMA={sig['fast']:.4f} 200EMA={sig['slow']:.4f}", sig
    elif strat == 'C':
        sig = signal_donchian(bars)
        if sig and sig['bull']:
            return True, f"{DONCHIAN_PERIOD}-bar Donchian breakout | price={sig['price']:.4f} > high={sig['hh']:.4f}", sig
    elif strat == 'D':
        sig = signal_macd(bars)
        if sig and sig['bull']:
            return True, f"MACD bull cross | MACD={sig['macd']:.4f} signal={sig['signal']:.4f}", sig
    elif strat == 'E':
        sig = signal_bollinger(bars)
        if sig and sig['bull']:
            return True, f"BB lower-band bounce | price={sig['price']:.4f} lower={sig['lower']:.4f} mid={sig['sma']:.4f}", sig
    elif strat == 'F':
        sig = signal_rsi(bars)
        if sig and sig['bull']:
            return True, f"RSI(14) oversold bounce | RSI {sig['rsi_prev']:.1f} -> {sig['rsi']:.1f}", sig
    elif strat == 'G':
        sig = signal_supertrend(bars)
        if sig and sig['bull']:
            return True, f"SuperTrend bull flip | price={sig['price']:.4f}", sig
    elif strat == 'H':
        sig = signal_orb(bars)
        if sig and sig['bull']:
            return True, f"ORB break above OR-high={sig['or_high']:.4f} | price={sig['price']:.4f}", sig
    elif strat == 'I':
        sig = signal_vwap(bars)
        if sig and sig['bull']:
            return True, f"VWAP reclaim | price={sig['price']:.4f} VWAP={sig['vwap']:.4f}", sig
    elif strat == 'J':
        sig = signal_inside_bar(bars)
        if sig and sig['bull']:
            return True, f"Inside bar break above mother high={sig['mother_high']:.4f} | price={sig['price']:.4f}", sig
    return False, '', {}


def get_exit_signal(strat, bars, pos):
    """Returns (should_exit: bool, reason: str)."""
    closes = [float(b['c']) for b in bars]
    price = closes[-1]
    if strat == 'A':
        sig = signal_ema_cross(bars, 9, 21)
        if sig and sig['bear']:
            return True, f"9/21 EMA bear cross (9={sig['fast']:.4f} <= 21={sig['slow']:.4f})"
    elif strat == 'B':
        sig = signal_ema_cross(bars, 50, 200)
        if sig and sig['bear']:
            return True, f"50/200 EMA bear cross"
    elif strat == 'C':
        sig = signal_donchian(bars)
        if sig and sig['bear']:
            return True, f"Donchian breakdown (price={price:.4f} < {DONCHIAN_EXIT_PERIOD}-bar low={sig['ll']:.4f})"
    elif strat == 'D':
        sig = signal_macd(bars)
        if sig and sig['bear']:
            return True, f"MACD bear cross"
    elif strat == 'E':
        sig = signal_bollinger(bars)
        if sig and sig['reach_mid']:
            return True, f"BB reached middle (target reached)"
    elif strat == 'F':
        sig = signal_rsi(bars)
        if sig and sig['overbought']:
            return True, f"RSI overbought ({sig['rsi']:.1f})"
    elif strat == 'G':
        sig = signal_supertrend(bars)
        if sig and sig['bear']:
            return True, f"SuperTrend bear flip"
    elif strat == 'H':
        sig = signal_orb(bars)
        if sig and sig['bear']:
            return True, f"ORB break below OR-low={sig['or_low']:.4f}"
    elif strat == 'I':
        sig = signal_vwap(bars)
        if sig and sig['below']:
            return True, f"VWAP lost (price={price:.4f} < VWAP={sig['vwap']:.4f})"
    elif strat == 'J':
        sig = signal_inside_bar(bars)
        if sig and sig['bear']:
            return True, f"Inside bar break below mother low={sig['mother_low']:.4f}"
    return False, ''


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
    else:
        ex, r = get_exit_signal(strat, bars, pos)
        if ex:
            should_exit, reason = True, r

    if should_exit:
        pl_pct = (price - entry) / entry * 100
        log(f'[{strat}] EXIT {sym}: {reason} | entry={entry:.4f} exit~{price:.4f} estP/L={pl_pct:+.2f}%')
        # Use partial close if multiple strategies hold the same symbol; otherwise full close
        same_sym_count = sum(1 for s, p in state.items() if p and p.get('symbol') == sym)
        if same_sym_count > 1 and pos.get('qty'):
            close_position(sym, qty=pos['qty'])
        else:
            close_position(sym)
        tg(f"SELL {sym}\nStrategy [{strat}]: {STRAT_NAMES[strat]}\nReason: {reason}\nEntry: ${entry:.4f}\nExit (approx): ${price:.4f}\nEst P/L: {pl_pct:+.2f}%")
        state[strat] = None
    else:
        pl_pct = (price - entry) / entry * 100
        log(f'[{strat}] HOLD {sym}: price={price:.4f} entry={entry:.4f} stop={stop:.4f} estP/L={pl_pct:+.2f}%')


def process_entry(strat, state, bars_dict, taken_syms, per_strategy_target,
                  block_new_stock_entries, market_open):
    if state.get(strat):
        return  # already holding

    for sym, is_crypto in WATCHLIST:
        if sym in taken_syms:
            continue
        if (not is_crypto) and block_new_stock_entries:
            continue
        if is_crypto and strat in STOCK_ONLY_STRATS:
            continue  # ORB and VWAP don't apply to crypto
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

        fired, details, _ = get_entry_signal(strat, bars, sym, is_crypto)
        if not fired:
            continue

        # Volume filter (where applicable)
        if strat in VOL_FILTER_STRATS:
            apply_vol = (not is_crypto) or market_open
            if apply_vol:
                vm = vol_mult(bars)
                if vm < VOL_MULT_THRESHOLD:
                    continue
                details += f" + vol {vm:.2f}x"

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
                'qty': order.get('qty') or order.get('filled_qty'),
            }
            taken_syms.add(sym)
            tg(f"BUY {sym} @ ${price:.4f}\nStrategy [{strat}]: {STRAT_NAMES[strat]}\nReason: {details}\nATR(14): {a14:.4f}\nStop: ${stop_price:.4f}\nNotional: ${notional:.2f}")
            return  # one entry per strategy per tick


def run():
    log('=== Multi-strategy LIVE tick (10 strategies) ===')

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
    per_strategy_target = round(equity / 10 * CASH_PCT, 2)
    log(f'Equity: ${equity:.2f} | per-strategy target: ${per_strategy_target:.2f} (10% each)')
    summary = ' | '.join(f"{s}={state[s] and state[s]['symbol'] or 'flat'}" for s in ALL_STRATS)
    log(f'State: {summary}')

    bars_dict = {}
    for sym, is_crypto in WATCHLIST:
        bars = get_bars(sym, is_crypto)
        if bars and len(bars) >= 250:
            bars_dict[sym] = bars

    # Exits first
    for s in ALL_STRATS:
        process_exit(s, state, bars_dict, force_close_stocks)

    # Recompute taken symbols
    taken_syms = {state[s]['symbol'] for s in ALL_STRATS if state.get(s)}

    # Entries
    for s in ALL_STRATS:
        process_entry(s, state, bars_dict, taken_syms, per_strategy_target,
                      block_new_stock_entries, market_open)

    save_state(state)


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        log(f'fatal: {e}')
        raise
