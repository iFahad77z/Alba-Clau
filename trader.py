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
VOL_MULT_THRESHOLD = 1.0  # default for strategies that don't override
VOL_MULT_PER_STRAT = {
    'A': 1.0,
    'B': 1.0,  # 20/50 EMA — back to standard threshold
    'D': 1.0,
    'G': 1.0,
    'H': 1.0,
    'J': 1.0,
    # K (50/200) intentionally absent — no volume filter (per spec)
}
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

# Filter variants: maps "X2" -> base strategy "X". X2 inherits all of X's logic
# but adds a "price > 200 EMA" filter on entry (long-term uptrend confirmation).
# Note: F2 omitted because Strategy N already serves that role.
FILTER_VARIANTS = {
    'A2': 'A', 'B2': 'B', 'C2': 'C', 'D2': 'D', 'E2': 'E',
    'G2': 'G', 'H2': 'H', 'I2': 'I', 'J2': 'J',
    'K2': 'K', 'L2': 'L', 'M2': 'M',
    'X-A2': 'X-A',  # Volume EMA cross + 200 EMA filter
    'X-K2': 'X-K',   # BB+RSI reversal + 200 EMA filter
}

# Trailing-stop variants ("copy 3"): maps "X3" -> base strategy "X". X3 has IDENTICAL
# entry/exit signal logic to its base, but uses a ratcheting trailing stop instead of a
# fixed ATR stop:
#   * +1×ATR profit -> stop moves to breakeven (entry price)
#   * +2×ATR profit -> stop trails at price - 1.5×ATR (locks in gains)
#   * stop never moves down
# A/B test: every base strategy has a "3" twin to measure trailing-stop impact.
TRAIL_VARIANTS = {
    'A3': 'A', 'B3': 'B', 'C3': 'C', 'D3': 'D', 'E3': 'E', 'F3': 'F', 'G3': 'G',
    'H3': 'H', 'I3': 'I', 'J3': 'J', 'K3': 'K', 'L3': 'L', 'M3': 'M', 'N3': 'N',
    'X-A3': 'X-A',  # Volume EMA cross + Trailing Stop
    'X-K3': 'X-K',   # BB+RSI reversal + Trailing Stop
}
TRAILING_STOP_STRATS = set(TRAIL_VARIANTS.keys())

# Swing-low-stop variants ("copy 4"): same entry as X2 (base + 200 EMA filter),
# but the stop loss is set at the lowest low of the past SWINGLOW_LOOKBACK bars
# instead of price - 1.5×ATR. Fires the same entries as X2; just a wider/tighter
# stop depending on recent price structure.
SWINGLOW_VARIANTS = {
    'A4': 'A', 'B4': 'B', 'C4': 'C', 'D4': 'D', 'E4': 'E', 'F4': 'F', 'G4': 'G',
    'H4': 'H', 'I4': 'I', 'J4': 'J', 'K4': 'K', 'L4': 'L', 'M4': 'M', 'N4': 'N',
    'X-A4': 'X-A',
    'X-K4': 'X-K',
}
SWINGLOW_STOP_STRATS = set(SWINGLOW_VARIANTS.keys())
SWINGLOW_LOOKBACK = 20  # bars (100 min on 5-min chart)

ALL_STRATS = (
    'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N',
    'A2', 'B2', 'C2', 'D2', 'E2', 'G2', 'H2', 'I2', 'J2', 'K2', 'L2', 'M2',
    'A3', 'B3', 'C3', 'D3', 'E3', 'F3', 'G3', 'H3', 'I3', 'J3', 'K3', 'L3', 'M3', 'N3',
    'X-A', 'X-A2', 'X-A3',
    'X-K', 'X-K2', 'X-K3',
    'A4', 'B4', 'C4', 'D4', 'E4', 'F4', 'G4', 'H4', 'I4', 'J4', 'K4', 'L4', 'M4', 'N4',
    'X-A4', 'X-K4',
)

STRAT_NAMES = {
    'A': 'Fast EMA Cross (9/21)',
    'B': 'Medium EMA Cross (20/50)',
    'C': 'Donchian Breakout (20/10)',
    'D': 'MACD (12/26/9)',
    'E': 'Bollinger Reversion (20, 2σ)',
    'F': 'RSI Bounce (14)',
    'G': 'SuperTrend (10, 3.0)',
    'H': 'Opening Range Breakout',
    'I': 'VWAP Reclaim',
    'J': 'Inside Bar Breakout',
    'K': 'Slow EMA Cross (50/200, no vol filter)',
    'L': 'Slow EMA + Take Profit (50/200 in, +1.5% TP or 50/200 bear out)',
    'M': 'Slow EMA No-Stop (50/200 in, exits ONLY on 50/200 bear cross)',
    'N': 'RSI Bounce + 200 EMA Trend Filter',
    'A2': 'Fast EMA Cross (9/21) + 200 EMA Filter',
    'B2': 'Medium EMA Cross (20/50) + 200 EMA Filter',
    'C2': 'Donchian Breakout (20/10) + 200 EMA Filter',
    'D2': 'MACD + 200 EMA Filter',
    'E2': 'Bollinger Reversion + 200 EMA Filter',
    'G2': 'SuperTrend + 200 EMA Filter',
    'H2': 'Opening Range Breakout + 200 EMA Filter',
    'I2': 'VWAP Reclaim + 200 EMA Filter',
    'J2': 'Inside Bar Breakout + 200 EMA Filter',
    'K2': 'Slow EMA (50/200) + 200 EMA Filter',
    'L2': 'Slow EMA + TP + 200 EMA Filter',
    'M2': 'Slow EMA No-Stop + 200 EMA Filter',
    'A3': 'Fast EMA Cross (9/21) + Trailing Stop',
    'B3': 'Medium EMA Cross (20/50) + Trailing Stop',
    'C3': 'Donchian Breakout (20/10) + Trailing Stop',
    'D3': 'MACD + Trailing Stop',
    'E3': 'Bollinger Reversion + Trailing Stop',
    'F3': 'RSI Bounce + Trailing Stop',
    'G3': 'SuperTrend + Trailing Stop',
    'H3': 'Opening Range Breakout + Trailing Stop',
    'I3': 'VWAP Reclaim + Trailing Stop',
    'J3': 'Inside Bar Breakout + Trailing Stop',
    'K3': 'Slow EMA (50/200) + Trailing Stop',
    'L3': 'Slow EMA + TP + Trailing Stop',
    'M3': 'Slow EMA No-Stop + Trailing Stop (no-op for M3 — no stop)',
    'N3': 'RSI Bounce + 200 EMA Trend Filter + Trailing Stop',
    'X-A':  'Volume-EMA Cross 9/21 (OI proxy)',
    'X-A2': 'Volume-EMA Cross 9/21 + 200 EMA Filter',
    'X-A3': 'Volume-EMA Cross 9/21 + Trailing Stop',
    'X-K':  'Bollinger + RSI Reversal (RSI cross up 20 + wick below lower BB; exit RSI cross down 80)',
    'X-K2': 'Bollinger + RSI Reversal + 200 EMA Filter',
    'X-K3': 'Bollinger + RSI Reversal + Trailing Stop',
    'A4':   'Fast EMA Cross (9/21) + 200 EMA Filter + Swing-Low Stop',
    'B4':   'Medium EMA Cross (20/50) + 200 EMA Filter + Swing-Low Stop',
    'C4':   'Donchian Breakout (20/10) + 200 EMA Filter + Swing-Low Stop',
    'D4':   'MACD + 200 EMA Filter + Swing-Low Stop',
    'E4':   'Bollinger Reversion + 200 EMA Filter + Swing-Low Stop',
    'F4':   'RSI Bounce + 200 EMA Filter + Swing-Low Stop',
    'G4':   'SuperTrend + 200 EMA Filter + Swing-Low Stop',
    'H4':   'Opening Range Breakout + 200 EMA Filter + Swing-Low Stop',
    'I4':   'VWAP Reclaim + 200 EMA Filter + Swing-Low Stop',
    'J4':   'Inside Bar Breakout + 200 EMA Filter + Swing-Low Stop',
    'K4':   'Slow EMA (50/200) + 200 EMA Filter + Swing-Low Stop',
    'L4':   'Slow EMA + TP + 200 EMA Filter + Swing-Low Stop',
    'M4':   'Slow EMA No-Stop + 200 EMA Filter (swing-low n/a since M skips stops)',
    'N4':   'RSI Bounce + 200 EMA Filter + Swing-Low Stop',
    'X-A4': 'Volume-EMA Cross + 200 EMA Filter + Swing-Low Stop',
    'X-K4': 'BB+RSI Reversal + 200 EMA Filter + Swing-Low Stop',
}


def base_strat(strat):
    """Map a variant ('A2', 'A3', 'A4') to its base strategy ('A'). Identity for non-variants."""
    if strat in FILTER_VARIANTS:
        return FILTER_VARIANTS[strat]
    if strat in TRAIL_VARIANTS:
        return TRAIL_VARIANTS[strat]
    if strat in SWINGLOW_VARIANTS:
        return SWINGLOW_VARIANTS[strat]
    return strat

# Take-profit thresholds (% gain that triggers exit)
TAKE_PROFIT_PER_STRAT = {
    'L': 1.5,  # Strategy L: take profit at +1.5%
}

# Strategies that skip the global ATR stop loss check
NO_ATR_STOP_STRATS = {'M'}
# Strategies that skip the 19:30 UTC force-close (empty — all strategies now force-close)
NO_FORCE_CLOSE_STRATS = set()

WATCHLIST = [
    ('GOOGL', False), ('AMZN', False), ('MSFT', False), ('NVDA', False),
    ('CF', False), ('NVO', False), ('AMD', False), ('AAPL', False),
    ('TSLA', False), ('AMAT', False), ('MU', False), ('NKE', False),
    ('LLY', False), ('SLB', False), ('CLS', False), ('STX', False),
    ('LRCX', False), ('QCOM', False), ('KLAC', False), ('TXN', False),
    ('CVX', False), ('XOM', False), ('GLW', False), ('BTC/USD', True),
]

# Symbols blocked from new entries based on consistent-loser data:
#   CVX: 0-6 W-L (-$17.84)   SLB: 0-9 W-L (-$10.33)   CF: 4-12 W-L (-$25.81)
# Existing positions in these symbols still get exit-managed; only NEW entries are blocked.
BLACKLIST = {'CVX', 'SLB', 'CF'}


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


def tg_error_once(state, key, msg, cooldown_min=60):
    """Send a Telegram error notification, deduped per (key, cooldown window).
    Prevents spam when a recurring error fires every 5-min cron tick."""
    if state is None:
        tg(msg)
        return
    notified = state.setdefault('_notified', {})
    last = notified.get(key, 0)
    now = datetime.now(timezone.utc).timestamp()
    if last and (now - last) < cooldown_min * 60:
        return
    notified[key] = now
    tg(msg)


def get_bars(symbol, is_crypto):
    """Fetch the most recent ~500 5-min bars for a symbol.

    NOTE on the previous (broken) behavior:
        With sort=asc, limit=1000, start=10d-ago, Alpaca returns the FIRST 1000
        bars in the window. For BTC (288 bars/day), 1000 bars = 3.5 days, so the
        bot was reading data from 6-10 days ago and missing the last week. Stocks
        weren't affected (market hours -> only ~390 bars/10d, fits in 1000).
        Now we use sort=desc (newest first) and reverse to ascending — guarantees
        the last bar is the current 5-min slot.
    """
    # Different lookback per asset class:
    # - Crypto: 4 days back at 288 bars/day = ~1152 bars total, fetch newest 500 (~42h).
    # - Stocks: market is only open 6.5h/day, and the IEX free feed is sparse (~160 bars
    #   in 4 trading days). Need 10 calendar days to comfortably get >250 bars for 200 EMA.
    if is_crypto:
        start = (datetime.now(timezone.utc) - timedelta(days=4)).strftime('%Y-%m-%dT%H:%M:%SZ')
        url = DATA_CRYPTO
        params = {'symbols': symbol, 'timeframe': '5Min', 'limit': 500, 'sort': 'desc', 'start': start}
    else:
        start = (datetime.now(timezone.utc) - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
        url = DATA_STOCKS
        params = {'symbols': symbol, 'timeframe': '5Min', 'limit': 1000, 'sort': 'desc', 'start': start, 'feed': 'iex'}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        r.raise_for_status()
        bars = r.json().get('bars', {}).get(symbol, [])
        # API returned newest-first; reverse to ascending so [-1] is the current bar.
        return list(reversed(bars))
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


def get_alpaca_positions():
    """Returns dict of {symbol: position_dict}. BTCUSD normalized to BTC/USD.
    Returns None on API failure (timeout, network error) so callers can distinguish
    'no positions' from 'unknown — cannot rely on this result'. Returning {} on error
    used to cause sync_state_with_alpaca to wipe all state slots."""
    try:
        r = requests.get(f'{TRADE_BASE}/positions', headers=HEADERS, timeout=15)
        r.raise_for_status()
        out = {}
        for p in r.json():
            sym = p.get('symbol', '')
            if sym == 'BTCUSD':
                sym = 'BTC/USD'
            out[sym] = p
        return out
    except Exception as e:
        log(f'ERROR positions: {e}')
        return None  # sentinel: API failed, caller must NOT treat as "no positions"


def sync_state_with_alpaca(state):
    """Drop any state entries for positions that no longer exist on Alpaca (manual closes etc.).

    SAFETY: if the Alpaca positions API fails (timeout / network), get_alpaca_positions()
    returns None. We must NOT clear any state in that case — a transient API failure
    previously wiped multiple strategy slots and triggered an orphan-claim collapse that
    consolidated multi-strategy positions into a single slot."""
    positions = get_alpaca_positions()
    if positions is None:
        log('STATE SYNC SKIPPED: positions API unavailable, preserving state for retry next tick')
        return
    alpaca_syms = set(positions.keys())
    for s in ALL_STRATS:
        pos = state.get(s)
        if pos and pos.get('symbol') not in alpaca_syms:
            log(f'[{s}] STATE SYNC: {pos["symbol"]} no longer on Alpaca, clearing slot')
            state[s] = None


def claim_orphan_positions(state, bars_dict, alpaca_positions):
    """Detect Alpaca positions with no strategy claim and assign them to flat strategy slots.
    Uses Alpaca's avg_entry_price as the recorded entry, but sets a fresh ATR stop based
    on current price so the claimed position has room to breathe instead of immediate stop-out."""
    claimed_syms = {state[s]['symbol'] for s in ALL_STRATS if state.get(s)}
    orphans = [(sym, p) for sym, p in alpaca_positions.items() if sym not in claimed_syms]
    if not orphans:
        return
    flat_slots = [s for s in ALL_STRATS if not state.get(s)]
    for sym, ap in orphans:
        if not flat_slots:
            log(f'ORPHAN UNCLAIMED {sym}: no flat strategy slots available')
            continue
        bars = bars_dict.get(sym)
        if not bars:
            log(f'ORPHAN UNCLAIMED {sym}: no bars data, cannot compute stop')
            continue
        closes = [float(b['c']) for b in bars]
        highs = [float(b['h']) for b in bars]
        lows = [float(b['l']) for b in bars]
        a14 = atr(highs, lows, closes, ATR_PERIOD)
        if not a14 or a14 <= 0:
            log(f'ORPHAN UNCLAIMED {sym}: no valid ATR')
            continue
        slot = flat_slots.pop(0)
        current_price = closes[-1]
        stop_price = current_price - ATR_MULT * a14
        actual_entry = float(ap.get('avg_entry_price', current_price))
        try:
            market_value = float(ap.get('market_value', 0))
        except Exception:
            market_value = 0
        state[slot] = {
            'symbol': sym,
            'entry': actual_entry,
            'stop': stop_price,
            'atr': a14,
            'notional': round(market_value, 2),
            'entry_time': datetime.now(timezone.utc).isoformat(),
            'order_id': 'orphan-claimed',
            'qty': ap.get('qty'),
        }
        log(f'[{slot}] CLAIMED ORPHAN {sym} @ entry={actual_entry:.4f} (current={current_price:.4f}) | stop={stop_price:.4f} qty={ap.get("qty")} mv=${market_value:.2f}')
        tg(f"CLAIMED ORPHAN {sym}\nStrategy [{slot}]: {STRAT_NAMES[slot]}\nEntry (actual): ${actual_entry:.4f}\nCurrent: ${current_price:.4f}\nFresh stop: ${stop_price:.4f}\nNotional: ${market_value:.2f}")


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
    except requests.HTTPError as e:
        body_text = ''
        try:
            body_text = e.response.text[:300] if e.response is not None else ''
        except Exception:
            pass
        log(f'ORDER FAILED BUY {symbol}: {e} body={body_text}')
        return None
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
            # If Alpaca says insufficient qty available (some qty held for other pending
            # orders), fall back to a partial close for just the available portion.
            if r.status_code == 403:
                try:
                    err = r.json()
                except Exception:
                    err = {}
                if err.get('code') == 40310000 and err.get('available'):
                    avail = float(err['available'])
                    if avail > 1e-9:
                        log(f"FULL CLOSE blocked for {symbol} (qty held by pending orders); falling back to partial close avail={avail}")
                        is_crypto = '/' in symbol
                        body = {
                            'symbol': symbol,
                            'qty': str(round(avail * 0.999, 9)),
                            'side': 'sell',
                            'type': 'market',
                            'time_in_force': 'gtc' if is_crypto else 'day',
                        }
                        r2 = requests.post(f'{TRADE_BASE}/orders', headers=HEADERS, json=body, timeout=15)
                        r2.raise_for_status()
                        d = r2.json()
                        log(f"ORDER PLACED PARTIAL SELL (fallback) {symbol} qty={body['qty']} id={d.get('id')} status={d.get('status')}")
                        return d
            r.raise_for_status()
            d = r.json()
            log(f"ORDER PLACED SELL ALL {symbol} qty={d.get('qty')} id={d.get('id')} status={d.get('status')}")
            return d
    except requests.HTTPError as e:
        # Capture the response body — Alpaca's 403 body has the actual reason
        # ('insufficient qty', 'pdt block', 'pending order conflict', etc.).
        body = ''
        try:
            body = e.response.text[:300] if e.response is not None else ''
        except Exception:
            pass
        log(f'ORDER FAILED SELL {symbol}: {e} body={body}')
        return None
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


def signal_bb_rsi_reversal(bars, bb_period=BB_PERIOD, bb_std=BB_STD,
                           rsi_period=RSI_PERIOD, rsi_low=20, rsi_high=80):
    """Strategy O: Deep Bollinger + RSI reversal.

    ENTRY (bull) — both must be true on the trigger bar:
      1) RSI crossed UP through 20: rsi_prev < 20 AND rsi_now >= 20
      2) The bar's low pierced below the lower Bollinger Band but the close
         came back above it (wick rejection): low <= lower_bb AND close > lower_bb

    EXIT (bear): RSI crossed DOWN through 80: rsi_prev > 80 AND rsi_now <= 80
    """
    closes = [float(b['c']) for b in bars]
    lows   = [float(b['l']) for b in bars]
    n = len(closes) - 1
    if n < max(bb_period, rsi_period) + 2:
        return None

    # Bollinger lower band at the current bar
    win = closes[n - bb_period + 1:n + 1]
    m = sum(win) / bb_period
    v = sum((c - m) ** 2 for c in win) / bb_period
    s = v ** 0.5
    lower_now = m - bb_std * s

    # RSI now and prev
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [-min(d, 0) for d in deltas]
    if len(gains) < rsi_period + 1:
        return None

    def rsi_at(end_idx_in_deltas):
        ag = sum(gains[:rsi_period]) / rsi_period
        al = sum(losses[:rsi_period]) / rsi_period
        for i in range(rsi_period, end_idx_in_deltas + 1):
            ag = (ag * (rsi_period - 1) + gains[i]) / rsi_period
            al = (al * (rsi_period - 1) + losses[i]) / rsi_period
        if al == 0:
            return 100
        rs = ag / al
        return 100 - 100 / (1 + rs)

    rsi_now  = rsi_at(len(gains) - 1)
    rsi_prev = rsi_at(len(gains) - 2)

    rsi_cross_up_20   = rsi_prev < rsi_low and rsi_now >= rsi_low
    rsi_cross_down_80 = rsi_prev > rsi_high and rsi_now <= rsi_high
    wick_below_bb = lows[n] <= lower_now and closes[n] > lower_now

    return {
        'bull': rsi_cross_up_20 and wick_below_bb,
        'bear': rsi_cross_down_80,
        'rsi': rsi_now, 'rsi_prev': rsi_prev,
        'lower_bb': lower_now, 'low': lows[n], 'close': closes[n],
        'price': closes[n],
    }


def signal_volume_ema_cross(bars, fast=9, slow=21):
    """Strategy X-A: 9/21 EMA crossover on VOLUME (proxy for Open Interest momentum).
    Bull when fast EMA crosses above slow EMA AND price has risen on the trigger bar.
    The price-rise check filters out volume spikes during sell-offs (defensive flow)."""
    vols = [float(b.get('v', 0)) for b in bars]
    closes = [float(b['c']) for b in bars]
    n = len(closes) - 1
    if n < slow + 1:
        return None
    vf = ema_series(vols, fast)
    vs = ema_series(vols, slow)
    if any(x is None for x in (vf[n], vs[n], vf[n - 1], vs[n - 1])):
        return None
    price_up = closes[n] > closes[n - 1]
    price_dn = closes[n] < closes[n - 1]
    return {
        'bull': vf[n - 1] <= vs[n - 1] and vf[n] > vs[n] and price_up,
        'bear': vf[n - 1] >= vs[n - 1] and vf[n] < vs[n] and price_dn,
        'fast_v': vf[n], 'slow_v': vs[n], 'price': closes[n],
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
    """Returns (fired: bool, details: str, extra: dict) or (False, '', {}).
    Filter variants (X2) delegate to the base then add a 200 EMA filter.
    Trailing variants (X3) delegate to the base with no signal change (only stop differs).
    Swing-low variants (X4) behave like X2 on entry (200 EMA filter); the stop differs."""
    if strat in FILTER_VARIANTS or strat in SWINGLOW_VARIANTS:
        if strat in FILTER_VARIANTS:
            base = FILTER_VARIANTS[strat]
        else:
            base = SWINGLOW_VARIANTS[strat]
        fired, details, extra = _get_entry_signal_base(base, bars, sym, is_crypto)
        if not fired:
            return False, '', {}
        # Apply 200 EMA filter: current price must be above 200 EMA
        closes = [float(b['c']) for b in bars]
        e200 = ema_series(closes, 200)
        n = len(closes) - 1
        if e200[n] is None or closes[n] <= e200[n]:
            return False, '', {}
        details += f" + price>200EMA ({closes[n]:.4f}>{e200[n]:.4f})"
        return True, details, extra
    if strat in TRAIL_VARIANTS:
        # Trailing-stop twin: same entry as base. Stop ratchet is applied in process_exit.
        return _get_entry_signal_base(TRAIL_VARIANTS[strat], bars, sym, is_crypto)
    return _get_entry_signal_base(strat, bars, sym, is_crypto)


def _get_entry_signal_base(strat, bars, sym, is_crypto):
    """Original per-strategy entry signal logic (for the 14 base strategies)."""
    if strat == 'A':
        sig = signal_ema_cross(bars, 9, 21)
        if sig and sig['bull']:
            return True, f"9/21 EMA bull cross | 9EMA={sig['fast']:.4f} 21EMA={sig['slow']:.4f}", sig
    elif strat == 'B':
        sig = signal_ema_cross(bars, 20, 50)
        if sig and sig['bull']:
            return True, f"20/50 EMA bull cross | 20EMA={sig['fast']:.4f} 50EMA={sig['slow']:.4f}", sig
    elif strat == 'K':
        sig = signal_ema_cross(bars, 50, 200)
        if sig and sig['bull']:
            return True, f"50/200 EMA bull cross | 50EMA={sig['fast']:.4f} 200EMA={sig['slow']:.4f}", sig
    elif strat == 'L':
        sig = signal_ema_cross(bars, 50, 200)
        if sig and sig['bull']:
            return True, f"50/200 EMA bull cross (with +1.5% TP) | 50EMA={sig['fast']:.4f} 200EMA={sig['slow']:.4f}", sig
    elif strat == 'M':
        sig = signal_ema_cross(bars, 50, 200)
        if sig and sig['bull']:
            return True, f"50/200 EMA bull cross (no-stop, ride till bear) | 50EMA={sig['fast']:.4f} 200EMA={sig['slow']:.4f}", sig
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
    elif strat == 'N':
        # RSI bounce + 200 EMA trend filter: only fire when current price is above the 200 EMA
        sig_rsi = signal_rsi(bars)
        if sig_rsi and sig_rsi['bull']:
            closes = [float(b['c']) for b in bars]
            e200 = ema_series(closes, 200)
            n = len(closes) - 1
            if e200[n] is not None and closes[n] > e200[n]:
                return True, f"RSI bounce + price>200EMA | RSI {sig_rsi['rsi_prev']:.1f}->{sig_rsi['rsi']:.1f} | price {closes[n]:.4f} > 200EMA {e200[n]:.4f}", sig_rsi
    elif strat == 'X-A':
        # Volume-EMA cross 9/21 (Open Interest proxy). Bull when 9-EMA of volume crosses
        # above 21-EMA of volume AND price is up on the trigger bar — captures money-flow
        # acceleration confirmed by price direction.
        sig = signal_volume_ema_cross(bars, 9, 21)
        if sig and sig['bull']:
            return True, f"Vol-EMA bull cross (OI proxy) | 9volEMA={sig['fast_v']:.0f} 21volEMA={sig['slow_v']:.0f} | price={sig['price']:.4f}", sig
    elif strat == 'X-K':
        # Bollinger + RSI deep-reversal: RSI just crossed up through 20 AND the bar wicked
        # below the lower BB but closed back above it. Catches capitulation bounces.
        sig = signal_bb_rsi_reversal(bars)
        if sig and sig['bull']:
            return True, (f"BB+RSI reversal | RSI {sig['rsi_prev']:.1f}->{sig['rsi']:.1f} crossed up 20 "
                          f"| low={sig['low']:.4f} <= lowerBB={sig['lower_bb']:.4f} < close={sig['close']:.4f}"), sig
    return False, '', {}


def get_exit_signal(strat, bars, pos):
    """Returns (should_exit: bool, reason: str). Variants share exit logic with their base."""
    if strat in FILTER_VARIANTS:
        return _get_exit_signal_base(FILTER_VARIANTS[strat], bars, pos)
    if strat in TRAIL_VARIANTS:
        return _get_exit_signal_base(TRAIL_VARIANTS[strat], bars, pos)
    if strat in SWINGLOW_VARIANTS:
        return _get_exit_signal_base(SWINGLOW_VARIANTS[strat], bars, pos)
    return _get_exit_signal_base(strat, bars, pos)


def _get_exit_signal_base(strat, bars, pos):
    closes = [float(b['c']) for b in bars]
    price = closes[-1]
    if strat == 'A':
        sig = signal_ema_cross(bars, 9, 21)
        if sig and sig['bear']:
            return True, f"9/21 EMA bear cross (9={sig['fast']:.4f} <= 21={sig['slow']:.4f})"
    elif strat == 'B':
        sig = signal_ema_cross(bars, 20, 50)
        if sig and sig['bear']:
            return True, f"20/50 EMA bear cross"
    elif strat == 'K':
        sig = signal_ema_cross(bars, 50, 200)
        if sig and sig['bear']:
            return True, f"50/200 EMA bear cross"
    elif strat == 'L':
        # Exit logic: 1) take profit at +1.5%, else 2) 50/200 EMA bear cross (ATR stop is handled globally)
        entry = pos['entry']
        tp_pct = TAKE_PROFIT_PER_STRAT.get('L', None)
        if tp_pct and entry > 0:
            gain_pct = (price - entry) / entry * 100
            if gain_pct >= tp_pct:
                return True, f"TAKE PROFIT (+{gain_pct:.2f}% >= +{tp_pct}%)"
        sig = signal_ema_cross(bars, 50, 200)
        if sig and sig['bear']:
            return True, f"50/200 EMA bear cross (after no TP)"
    elif strat == 'M':
        # M's only exit signal is 50/200 bear cross. ATR stop and force-close also disabled (see process_exit).
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
    elif strat == 'N':
        # Same exit logic as F: RSI overbought (≥70). ATR stop and force-close are global.
        sig = signal_rsi(bars)
        if sig and sig['overbought']:
            return True, f"RSI overbought ({sig['rsi']:.1f})"
    elif strat == 'X-A':
        # Exit on bearish volume-EMA cross (9 below 21) — money flow fading.
        sig = signal_volume_ema_cross(bars, 9, 21)
        if sig and sig['bear']:
            return True, f"Vol-EMA bear cross (9volEMA={sig['fast_v']:.0f} <= 21volEMA={sig['slow_v']:.0f})"
    elif strat == 'X-K':
        # Exit only on RSI crossing down through 80 (deep overbought reversal).
        sig = signal_bb_rsi_reversal(bars)
        if sig and sig['bear']:
            return True, f"RSI cross-down 80 (RSI {sig['rsi_prev']:.1f}->{sig['rsi']:.1f})"
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

    bs = base_strat(strat)

    # Trailing stop ratchet (X3 variants only): never lowers, only raises.
    #   * +1×ATR profit -> stop = max(stop, entry)        (move to breakeven)
    #   * +2×ATR profit -> stop = max(stop, price - 1.5×ATR)  (trail)
    if strat in TRAILING_STOP_STRATS and bs not in NO_ATR_STOP_STRATS:
        a14 = pos.get('atr')
        if a14 and a14 > 0:
            profit = price - entry
            new_stop = stop
            if profit >= 2.0 * a14:
                new_stop = max(new_stop, price - ATR_MULT * a14)
            elif profit >= 1.0 * a14:
                new_stop = max(new_stop, entry)  # breakeven
            if new_stop > stop:
                log(f'[{strat}] TRAIL stop {stop:.4f} -> {new_stop:.4f} (price={price:.4f} entry={entry:.4f} ATR={a14:.4f})')
                stop = new_stop
                pos['stop'] = new_stop

    should_exit, reason = False, ''
    skip_force_close = bs in NO_FORCE_CLOSE_STRATS
    skip_atr_stop = bs in NO_ATR_STOP_STRATS
    # Force-close applies to STOCKS ONLY at 19:30 UTC weekdays. BTC trades 24/7.
    if (not is_crypto) and force_close_stocks and not skip_force_close:
        should_exit, reason = True, 'FORCE CLOSE (19:30 UTC, 30 min before market close)'
    elif price <= stop and not skip_atr_stop:
        should_exit, reason = True, f'STOP HIT (price={price:.4f} <= stop={stop:.4f})'
    else:
        ex, r = get_exit_signal(strat, bars, pos)
        if ex:
            should_exit, reason = True, r

    if should_exit:
        pl_pct = (price - entry) / entry * 100
        log(f'[{strat}] EXIT {sym}: {reason} | entry={entry:.4f} exit~{price:.4f} estP/L={pl_pct:+.2f}%')
        # Use partial close if multiple strategies hold the same symbol; otherwise full close.
        same_sym_count = sum(1 for s, p in state.items() if p and p.get('symbol') == sym)
        if same_sym_count > 1 and pos.get('qty'):
            # Query Alpaca for live qty_available — never request more than is actually
            # sellable RIGHT NOW (other strategies' pending sells reduce qty_available).
            positions = get_alpaca_positions()
            if positions is None:
                log(f'[{strat}] EXIT close skipped for {sym}: positions API unavailable, keeping state')
                return
            apos = positions.get(sym)
            if not apos:
                # Position vanished from Alpaca (manual close, prior force close, etc.).
                # Clear state and don't send an order — there's nothing to sell.
                log(f'[{strat}] EXIT cleared {sym}: no Alpaca position to sell against')
                state[strat] = None
                return
            qty_avail = float(apos.get('qty_available', 0))
            if qty_avail <= 1e-9:
                log(f'[{strat}] EXIT close skipped for {sym}: qty_available=0 (other strategy pending), retry next tick')
                return
            # Cap: never exceed our stored share AND never exceed currently-available qty.
            requested = float(pos['qty']) if pos.get('qty') else qty_avail / same_sym_count
            # Take 99% of the cap as a safety margin against float / fee dust
            sell_qty = min(requested, qty_avail) * 0.999
            sell_qty = round(sell_qty, 9)
            if sell_qty < 1e-8:
                log(f'[{strat}] EXIT cleared {sym}: sell qty too small ({sell_qty})')
                state[strat] = None
                return
            log(f'[{strat}] partial close {sym}: requested={requested:.9f} avail={qty_avail:.9f} -> sending {sell_qty:.9f}')
            result = close_position(sym, qty=sell_qty)
        else:
            result = close_position(sym)
        if result is None:
            # Close failed (e.g. pending order already exists, market closed, PDT). Keep state to retry next tick.
            log(f'[{strat}] EXIT close failed for {sym}; keeping state to retry on next tick')
            # Notify Telegram (deduped per symbol per 60 min so we don't spam every 5 min)
            tg_error_once(
                state,
                key=f'close_fail:{sym}',
                msg=(f"⚠️ Close FAILED for {sym} (strategy [{strat}])\n"
                     f"Reason: {reason}\n"
                     f"Likely cause: PDT block, market closed, or duplicate pending order.\n"
                     f"Bot kept state and will retry every 5 min until success."),
                cooldown_min=60,
            )
            return
        notional_in = pos.get('notional')
        est_pl_dollars = (notional_in * pl_pct / 100) if notional_in else None
        if notional_in is not None and est_pl_dollars is not None:
            tg(f"SELL {sym}\nStrategy [{strat}]: {STRAT_NAMES[strat]}\nReason: {reason}\nEntry: ${entry:.4f}\nExit (approx): ${price:.4f}\nNotional: ${notional_in:.2f}\nEst P/L: {pl_pct:+.2f}% (~${est_pl_dollars:+.2f})")
        else:
            tg(f"SELL {sym}\nStrategy [{strat}]: {STRAT_NAMES[strat]}\nReason: {reason}\nEntry: ${entry:.4f}\nExit (approx): ${price:.4f}\nEst P/L: {pl_pct:+.2f}%")
        # Record trade for daily, weekly, and lifetime stats
        daily = state.setdefault('_daily', {'date': '', 'sent': False, 'trades': [], 'start_equity': None})
        trade_rec = {
            'strat': strat, 'sym': sym, 'pl_pct': round(pl_pct, 2),
            'pl_usd': round(est_pl_dollars, 2) if est_pl_dollars is not None else None,
            'reason': reason,
        }
        daily.setdefault('trades', []).append(trade_rec)
        # Weekly trades — rolling Monday-to-Sunday window in UTC
        weekly = state.setdefault('_weekly', {'week_start': '', 'trades': []})
        now_dt = datetime.now(timezone.utc).date()
        monday = (now_dt - timedelta(days=now_dt.weekday())).isoformat()
        if weekly.get('week_start') != monday:
            weekly['week_start'] = monday
            weekly['trades'] = []
        weekly.setdefault('trades', []).append(trade_rec)
        # Lifetime cumulative stats per strategy (never resets)
        lifetime = state.setdefault('_lifetime', {})
        d = lifetime.setdefault(strat, {'n': 0, 'w': 0, 'l': 0, 'usd': 0.0, 'pct': 0.0})
        d['n'] += 1
        if pl_pct > 0: d['w'] += 1
        else: d['l'] += 1
        d['usd'] += (est_pl_dollars or 0.0)
        d['pct'] += pl_pct
        state[strat] = None
    else:
        pl_pct = (price - entry) / entry * 100
        log(f'[{strat}] HOLD {sym}: price={price:.4f} entry={entry:.4f} stop={stop:.4f} estP/L={pl_pct:+.2f}%')


def process_entry(strat, state, bars_dict, taken_syms, per_strategy_target,
                  block_new_stock_entries, market_open, all_in_mode=False,
                  block_new_btc_entries=False, tick_ctx=None):
    """
    Returns True if an entry was placed for this strategy this tick, False otherwise.
    If all_in_mode is True, the entry uses ALL available cash (capped to ~95% of the
    free buying power to allow for slippage), instead of per_strategy_target. The caller
    is responsible for stopping the loop after the first all-in entry fires.
    """
    if state.get(strat):
        return False  # already holding

    for sym, is_crypto in WATCHLIST:
        # No single-symbol lock — multiple strategies may hold the same symbol
        # concurrently (each with its own per-strategy cash slice). Allows full
        # head-to-head comparison: A, A2, and A3 can all trade the same AAPL bar.
        # Partial-close logic in process_exit handles multi-strategy ownership.
        # Skip blacklisted chronic losers (existing positions still exit normally).
        if sym in BLACKLIST:
            continue
        # Stocks: blocked outside 13:30–15:30 UTC normal trading. The all-in path bypasses
        # this block during the 15:30–16:00 UTC last-entry window.
        if (not is_crypto) and block_new_stock_entries and not all_in_mode:
            continue
        # BTC: time-of-day rule (user policy). Blocked during stock-trading hours so it
        # only trades AFTER force-close (19:30 UTC weekdays) through the next stock open.
        # All-in window does not unblock BTC (it sits inside the blocked period).
        if is_crypto and block_new_btc_entries:
            continue
        if is_crypto and base_strat(strat) in STOCK_ONLY_STRATS:
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

        # Volume filter (where applicable) — use base strategy's properties
        if base_strat(strat) in VOL_FILTER_STRATS:
            apply_vol = (not is_crypto) or market_open
            if apply_vol:
                vm = vol_mult(bars)
                threshold = VOL_MULT_PER_STRAT.get(base_strat(strat), VOL_MULT_THRESHOLD)
                if vm < threshold:
                    continue
                details += f" + vol {vm:.2f}x"

        acct = get_account()
        if not acct:
            continue
        cash = float(acct['cash'])
        # Use non_marginable_buying_power to prevent the bot from using margin.
        # Falls back to cash if not present (some account types).
        nmbp = float(acct.get('non_marginable_buying_power', cash))
        available = min(cash, nmbp)
        # Subtract cash already committed this tick by earlier strategies — Alpaca's
        # account endpoint doesn't update fast enough between rapid back-to-back orders,
        # so we maintain a local running total to prevent over-commitment 403s.
        if tick_ctx is not None:
            available -= tick_ctx.get('committed_cash', 0.0)
        if all_in_mode:
            # Last-entry window: deploy ALL available cash on the first signal that fires.
            # Floor at $50 to avoid placing dust orders if cash is essentially zero.
            if available < 50:
                log(f'[{strat}] ALL-IN SKIPPED {sym}: only ${available:.2f} available')
                continue
            notional = available * CASH_PCT
        else:
            # All-or-nothing sizing: only enter if at least 95% of the full per-strategy target
            # is available. Prevents tiny noise trades from rationing capital across strategies.
            if available < per_strategy_target * 0.95:
                log(f'[{strat}] BUY SKIPPED {sym}: cash ${available:.2f} below 95% of target ${per_strategy_target:.2f}')
                continue
            notional = per_strategy_target * CASH_PCT

        # Stop calculation: X4 variants use the lowest low of the past SWINGLOW_LOOKBACK
        # bars (chart structure stop). All others use 1.5×ATR (X3 also starts here and
        # ratchets up in process_exit).
        if strat in SWINGLOW_STOP_STRATS:
            recent_lows = [float(b['l']) for b in bars[-SWINGLOW_LOOKBACK:]]
            swing_low = min(recent_lows) if recent_lows else price
            # Safety: if swing-low is at or above current price (very rare — only on a brand
            # new high after a fast rip), fall back to ATR stop so the stop is meaningfully
            # below entry. Otherwise use the swing-low as-is.
            if swing_low < price:
                stop_price = swing_low
                stop_kind = f"swing-low({SWINGLOW_LOOKBACK}b)"
            else:
                stop_price = price - ATR_MULT * a14
                stop_kind = f"ATR fallback (swing-low {swing_low:.4f} >= price)"
        else:
            stop_price = price - ATR_MULT * a14
            stop_kind = f"ATR(14)×{ATR_MULT}"
        log(f'[{strat}] ENTRY SIGNAL {sym} @ {price:.4f} | {details} | {stop_kind} stop={stop_price:.4f} notional=${notional:.2f}')
        # Compute the planned qty up front so partial-close (used when multiple strategies
        # share a symbol) has a valid number to send. Notional-based crypto buys come back
        # from Alpaca with qty=null and filled_qty=0 at placement time, so we couldn't rely
        # on the order response alone.
        planned_qty = round(notional / price, 9) if price > 0 else 0
        order = buy_notional(sym, notional, is_crypto)
        if not order:
            tg_error_once(
                state,
                key=f'buy_fail:{sym}',
                msg=(f"⚠️ Buy FAILED for {sym} (strategy [{strat}])\n"
                     f"Notional: ${notional:.2f}\nCheck logs for details."),
                cooldown_min=60,
            )
        if order:
            # Track committed cash so subsequent strategies in the same tick see reduced
            # available cash and don't try to over-spend (Alpaca's account API lags).
            if tick_ctx is not None:
                tick_ctx['committed_cash'] = tick_ctx.get('committed_cash', 0.0) + notional
            state[strat] = {
                'symbol': sym,
                'entry': price,
                'stop': stop_price,
                'atr': a14,
                'notional': round(notional, 2),
                'entry_time': datetime.now(timezone.utc).isoformat(),
                'order_id': order['id'],
                # Prefer the order's reported qty (when it's already filled), else fall
                # back to our computed planned_qty. Never store 0 — that breaks partial close.
                'qty': (float(order.get('qty')) if order.get('qty') else None) or
                       (float(order.get('filled_qty')) if order.get('filled_qty') and float(order.get('filled_qty')) > 0 else None) or
                       planned_qty,
            }
            # taken_syms retained as a no-op for now (no symbol is locked). Kept for
            # backward compat in case we want to re-enable per-symbol concurrency caps later.
            taken_syms.add(sym)
            tag = '[ALL-IN] ' if all_in_mode else ''
            tg(f"{tag}BUY {sym} @ ${price:.4f}\nStrategy [{strat}]: {STRAT_NAMES[strat]}\nReason: {details}\nATR(14): {a14:.4f}\nStop: ${stop_price:.4f}\nNotional: ${notional:.2f}")
            return True  # one entry per strategy per tick
    return False


def send_daily_summary(state, equity_now, is_weekend=False, morning=False):
    daily = state.get('_daily') or {}
    trades = daily.get('trades', [])
    start_eq = daily.get('start_equity')
    eq_change_usd = (equity_now - start_eq) if (start_eq is not None) else None
    eq_change_pct = (eq_change_usd / start_eq * 100) if (start_eq and eq_change_usd is not None) else None

    # On weekends, only BTC trades exist. Filter to BTC-only and label the summary.
    if is_weekend:
        trades = [t for t in trades if t.get('sym') == 'BTC/USD']
        title = f"📊 Weekend BTC Summary — {daily.get('date','?')}"
    elif morning:
        title = f"🌅 Morning Summary — {daily.get('date','?')} 07:00 UTC"
    else:
        title = f"📊 Daily Summary — {daily.get('date','?')}"

    n = len(trades)
    wins = sum(1 for t in trades if (t.get('pl_pct') or 0) > 0)
    losses = n - wins
    realized_usd = sum((t.get('pl_usd') or 0) for t in trades)

    # Per-strategy aggregation (n trades, wins, losses, $ realized, total % return)
    by_strat = {}
    for t in trades:
        s = t['strat']
        d = by_strat.setdefault(s, {'n': 0, 'w': 0, 'l': 0, 'usd': 0.0, 'pct': 0.0})
        d['n'] += 1
        if (t.get('pl_pct') or 0) > 0: d['w'] += 1
        else: d['l'] += 1
        d['usd'] += (t.get('pl_usd') or 0)
        d['pct'] += (t.get('pl_pct') or 0)

    # Weekly realized — sum from state['_weekly']['trades'] (rolling Mon-Sun)
    weekly = state.get('_weekly') or {}
    weekly_trades = weekly.get('trades', [])
    if is_weekend:
        weekly_trades = [t for t in weekly_trades if t.get('sym') == 'BTC/USD']
    weekly_n = len(weekly_trades)
    weekly_wins = sum(1 for t in weekly_trades if (t.get('pl_pct') or 0) > 0)
    weekly_realized = sum((t.get('pl_usd') or 0) for t in weekly_trades)
    week_start_str = weekly.get('week_start', '?')

    no_trade_msg = "No BTC trades closed today." if is_weekend else "No trades closed today."
    lines = [
        title,
        f"Equity: ${equity_now:,.2f}" + (f" ({eq_change_pct:+.2f}%, ${eq_change_usd:+,.2f})" if eq_change_pct is not None else ""),
        f"Trades: {n}  W-L: {wins}-{losses}" + (f" ({wins/n*100:.0f}% win)" if n else ""),
        f"Realized today: ${realized_usd:+,.2f}",
        f"Realized this week: ${weekly_realized:+,.2f} ({weekly_n} trade{'s' if weekly_n!=1 else ''}, W-L: {weekly_wins}-{weekly_n-weekly_wins}, since {week_start_str})",
        "",
        "Today's per-strategy:" if by_strat else no_trade_msg,
    ]
    for s in sorted(by_strat.keys(), key=lambda x: -by_strat[x]['usd']):
        d = by_strat[s]
        lines.append(f"[{s}] {d['n']} trade{'s' if d['n']!=1 else ''}, W:{d['w']} L:{d['l']}, ${d['usd']:+,.2f} ({d['pct']:+.2f}%)")

    # ===== Lifetime block: all 43 strategies, cumulative stats since deployment =====
    lifetime = state.get('_lifetime', {}) or {}
    # Status of each slot right now (flat / holding sym)
    lines.append("")
    lines.append("Lifetime (since deploy):")
    # Sort: strategies with trades first (by $ desc), then untraded ones
    traded = [s for s in ALL_STRATS if s in lifetime and lifetime[s].get('n', 0) > 0]
    untraded = [s for s in ALL_STRATS if s not in lifetime or lifetime[s].get('n', 0) == 0]
    traded.sort(key=lambda x: -lifetime[x]['usd'])
    for s in traded:
        d = lifetime[s]
        pos = state.get(s)
        held = f" 🟢{pos['symbol']}" if pos and pos.get('symbol') else ""
        lines.append(f"[{s}] {d['n']}t W:{d['w']} L:{d['l']} ${d['usd']:+,.0f} ({d['pct']:+.1f}%){held}")
    if untraded:
        # Just list untraded strategies on one or two lines (no zeros to crunch through)
        held_among_untraded = []
        truly_flat = []
        for s in untraded:
            pos = state.get(s)
            if pos and pos.get('symbol'):
                held_among_untraded.append(f"[{s}]🟢{pos['symbol']}")
            else:
                truly_flat.append(s)
        if held_among_untraded:
            lines.append("Holding (no closed trades yet): " + " ".join(held_among_untraded))
        if truly_flat:
            lines.append(f"Never fired ({len(truly_flat)}): " + ", ".join(truly_flat))

    msg = "\n".join(lines)
    log("DAILY SUMMARY:\n" + msg)
    # Telegram has a 4096-char limit per message — split if oversized
    if len(msg) <= 4000:
        tg(msg)
    else:
        # Split on the lifetime divider
        parts = msg.split("Lifetime (since deploy):")
        tg(parts[0].rstrip())
        if len(parts) > 1:
            tg("Lifetime (since deploy):" + parts[1])


def process_halt(state, bars_dict):
    """News/event kill switch. Reads state['_halt'] which the user (or scheduled
    blackouts) can set to force-close all positions and block new entries.

    state['_halt'] format:
      {
        'active': bool,                       # is halt active?
        'reason': 'CPI release' / 'FOMC' etc.,
        'since': ISO timestamp,
        'symbols': [] or ['AAPL','NVDA',...]   # empty list = halt everything;
                                               # populated = halt only these symbols
                                               # (and BTC if 'BTC/USD' included)
        'force_close': bool                   # close existing positions immediately?
      }
    Returns True if any halt is active and entries should be blocked tick-wide
    (used by the entry loop to short-circuit)."""
    halt = state.get('_halt') or {}
    if not halt.get('active'):
        return False
    reason = halt.get('reason', 'manual halt')
    since  = halt.get('since', '?')
    sym_filter = halt.get('symbols') or []   # empty = all
    force_close = bool(halt.get('force_close', True))
    # One-shot Telegram alert when entering halt (dedupe via _halt['notified'])
    if not halt.get('notified'):
        scope = ', '.join(sym_filter) if sym_filter else 'ALL SYMBOLS'
        tg(f"🔴🚨 TRADING HALT ACTIVATED\nReason: {reason}\nScope: {scope}\nForce close: {force_close}\nSince: {since}\n\nAll new entries BLOCKED. Existing positions {'being force-closed' if force_close else 'left open'}.")
        halt['notified'] = True
    if force_close:
        # Force-close every state slot whose symbol matches the scope
        for s in ALL_STRATS:
            pos = state.get(s)
            if not pos:
                continue
            sym = pos.get('symbol')
            if sym_filter and sym not in sym_filter:
                continue
            bars = bars_dict.get(sym)
            price = float(bars[-1]['c']) if bars else pos.get('entry', 0)
            entry = pos.get('entry', price)
            pl_pct = (price - entry) / entry * 100 if entry else 0
            log(f'[{s}] HALT FORCE EXIT {sym}: {reason} | entry={entry:.4f} exit~{price:.4f} estP/L={pl_pct:+.2f}%')
            same_sym_count = sum(1 for x, p in state.items() if isinstance(p, dict) and p.get('symbol') == sym)
            if same_sym_count > 1 and pos.get('qty'):
                # Re-use the safe partial-close logic from process_exit's improved path
                positions = get_alpaca_positions()
                if positions is None:
                    log(f'[{s}] HALT close skipped {sym}: positions API down')
                    continue
                apos = positions.get(sym)
                if not apos:
                    log(f'[{s}] HALT cleared {sym}: no Alpaca position')
                    state[s] = None
                    continue
                qty_avail = float(apos.get('qty_available', 0))
                if qty_avail <= 1e-9:
                    log(f'[{s}] HALT skipped {sym}: qty_available=0, retry next tick')
                    continue
                sell_qty = round(min(float(pos['qty']), qty_avail) * 0.999, 9)
                if sell_qty < 1e-8:
                    state[s] = None
                    continue
                result = close_position(sym, qty=sell_qty)
            else:
                result = close_position(sym)
            if result is not None:
                state[s] = None
                tg(f"🔴 HALT SELL [{s}] {sym}\nReason: {reason}\nEntry: ${entry:.4f}\nExit: ${price:.4f}\nP/L: {pl_pct:+.2f}%")
    return True   # halt active — caller should block new entries


def set_halt(state, reason, symbols=None, force_close=True):
    """Helper to activate the halt. Call from CLI script or manual edit."""
    state['_halt'] = {
        'active': True,
        'reason': reason,
        'since': datetime.now(timezone.utc).isoformat(),
        'symbols': list(symbols) if symbols else [],
        'force_close': bool(force_close),
        'notified': False,
    }
    log(f'HALT ACTIVATED: reason={reason} symbols={symbols or "ALL"} force_close={force_close}')


def clear_halt(state):
    """Helper to release the halt. Logs and TGs the resume."""
    halt = state.get('_halt') or {}
    if halt.get('active'):
        reason = halt.get('reason', '?')
        tg(f"🟢 TRADING RESUMED\nPrevious halt: {reason}\nBot is back to normal operation.")
    state['_halt'] = {'active': False}
    log('HALT CLEARED — trading resumed')


def maybe_send_morning_summary(state, utc_min, utc_hour, equity_now, is_weekend):
    """Send a morning recap at 07:00–07:04 UTC every day (weekday and weekend).
    Uses today's daily['trades'] which at 7 AM contains only overnight BTC activity
    (since daily resets at midnight UTC). Independent of the end-of-day summary —
    has its own 'sent' tracker so both fire in the same UTC date."""
    if utc_hour != 7 or utc_min >= 5:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get('_morning_sent_date') == today:
        return
    state['_morning_sent_date'] = today
    send_daily_summary(state, equity_now, is_weekend=is_weekend, morning=True)


def maybe_send_daily_summary(state, utc_min, is_weekend, equity_now):
    """Send the EVENING daily summary once per UTC date.
    * Weekdays: 19:40 UTC if all stocks are flat, else fallback at 20:00 UTC.
    * Weekends: 20:00 UTC, BTC-only trades.
    The morning summary (07:00 UTC) is handled separately by maybe_send_morning_summary."""
    daily = state.setdefault('_daily', {'date': '', 'sent': False, 'trades': [], 'start_equity': None})
    today = datetime.now(timezone.utc).date().isoformat()
    if daily.get('date') != today:
        # New day — reset
        daily['date'] = today
        daily['sent'] = False
        daily['trades'] = []
        daily['start_equity'] = equity_now
    if daily.get('sent'):
        return

    if is_weekend:
        if (20*60) <= utc_min < (20*60 + 5):
            send_daily_summary(state, equity_now, is_weekend=True)
            daily['sent'] = True
        return

    all_stocks_flat = True
    for s in ALL_STRATS:
        p = state.get(s)
        if p and p.get('symbol') and p['symbol'] != 'BTC/USD':
            all_stocks_flat = False
            break
    early_window  = (19*60 + 40) <= utc_min < (19*60 + 45)
    late_fallback = (20*60)      <= utc_min < (20*60 + 5)
    if (early_window and all_stocks_flat) or late_fallback:
        send_daily_summary(state, equity_now, is_weekend=False)
        daily['sent'] = True
        daily['sent_evening'] = True


def run():
    log(f'=== Multi-strategy LIVE tick ({len(ALL_STRATS)} strategies) ===')

    now_utc = datetime.now(timezone.utc)
    utc_min = now_utc.hour * 60 + now_utc.minute
    market_open_min = 13 * 60 + 30
    market_close_min = 20 * 60
    # Force-close at 19:30 UTC (30 min before market close) — gives orders ~30 min margin
    # to actually fill in the regular session, avoiding the 403 wall we hit at 20:00.
    force_close_at_min = market_close_min - 30   # 19:30 UTC
    # Last-entry window: 15:30–16:00 UTC. Normal stock entries fire 13:30–15:30 UTC. From
    # 15:30 UTC, only the all-in path can place a stock entry, and only one strategy total
    # fires inside this 30-min window — it takes all available cash. After 16:00 UTC, no
    # new STOCK entries until the next stock-market open at 13:30 UTC. (BTC stays 24/7.)
    last_entry_window_start = 15 * 60 + 30        # 15:30 UTC
    last_entry_window_end   = 16 * 60             # 16:00 UTC
    is_weekend = now_utc.weekday() >= 5
    market_open = (not is_weekend) and market_open_min <= utc_min < market_close_min
    in_last_entry_window = (not is_weekend) and last_entry_window_start <= utc_min < last_entry_window_end
    # Block normal stock entries when market is closed OR once the last-entry window has begun
    # (only the all-in entry path can fire during the window). After 16:00 UTC, stock entries
    # remain blocked all the way until the next 13:30 UTC.
    block_new_stock_entries = (not market_open) or utc_min >= last_entry_window_start
    # BTC time-of-day rule: blocked during stock-trading hours (13:30–19:30 UTC weekdays).
    # Allowed from 19:30 UTC (force-close start) through the next 13:30 UTC, plus weekends.
    btc_active_window = is_weekend or utc_min >= force_close_at_min or utc_min < market_open_min
    block_new_btc_entries = not btc_active_window
    # Force-close fires from 19:30 UTC for 30 min — covers 19:30–20:00 UTC, all in regular hours.
    force_close_stocks = (not is_weekend) and force_close_at_min <= utc_min < (force_close_at_min + 30)
    log(f'Time: UTC={now_utc:%H:%M} marketOpen={market_open} blockStockEntries={block_new_stock_entries} blockBtcEntries={block_new_btc_entries} forceCloseStocks={force_close_stocks} lastEntryWindow={in_last_entry_window}')

    state = load_state()
    sync_state_with_alpaca(state)

    acct = get_account()
    if not acct:
        log('Could not fetch account; aborting tick')
        save_state(state)
        return
    equity = float(acct['equity'])
    per_strategy_target = round(equity / len(ALL_STRATS) * CASH_PCT, 2)
    pct_each = round(100 / len(ALL_STRATS), 2)
    log(f'Equity: ${equity:.2f} | per-strategy target: ${per_strategy_target:.2f} ({pct_each}% each, {len(ALL_STRATS)} strategies)')
    summary = ' | '.join(f"{s}={state[s] and state[s]['symbol'] or 'flat'}" for s in ALL_STRATS)
    log(f'State: {summary}')

    bars_dict = {}
    for sym, is_crypto in WATCHLIST:
        bars = get_bars(sym, is_crypto)
        # Need enough history for 200-EMA based filters/strategies. 220 gives
        # 200 EMA convergence + room for cross detection on the trigger bar.
        if bars and len(bars) >= 220:
            bars_dict[sym] = bars
        elif bars:
            log(f'SKIP {sym}: only {len(bars)} bars available (need >=220)')

    # Claim any orphan Alpaca positions (existing positions that no strategy is tracking)
    alpaca_positions_now = get_alpaca_positions()
    if alpaca_positions_now is None:
        log('ORPHAN CLAIM SKIPPED: positions API unavailable')
    else:
        claim_orphan_positions(state, bars_dict, alpaca_positions_now)

    # Exits first
    for s in ALL_STRATS:
        process_exit(s, state, bars_dict, force_close_stocks)

    # News/event halt — if active, force-close affected positions and short-circuit entries
    halt_active = process_halt(state, bars_dict)

    # Recompute taken symbols
    taken_syms = {state[s]['symbol'] for s in ALL_STRATS if state.get(s)}

    # Entries (skipped entirely when a halt is active)
    # tick_ctx tracks cash committed in this tick so back-to-back buys don't over-spend
    # (Alpaca's account endpoint cash field lags behind rapid order submissions).
    tick_ctx = {'committed_cash': 0.0}
    if halt_active:
        log('Entries SKIPPED: trading halt active')
        save_state(state)
        return
    if in_last_entry_window:
        # All-in mode: try strategies in order until ONE fires, deploys all cash, and we stop.
        log(f'[ALL-IN WINDOW] 15:30-16:00 UTC: scanning for first signal to deploy all available cash')
        for s in ALL_STRATS:
            placed = process_entry(s, state, bars_dict, taken_syms, per_strategy_target,
                                   block_new_stock_entries, market_open, all_in_mode=True,
                                   block_new_btc_entries=block_new_btc_entries,
                                   tick_ctx=tick_ctx)
            if placed:
                log(f'[ALL-IN WINDOW] [{s}] consumed available cash; halting further entries this tick')
                break
    else:
        for s in ALL_STRATS:
            process_entry(s, state, bars_dict, taken_syms, per_strategy_target,
                          block_new_stock_entries, market_open,
                          block_new_btc_entries=block_new_btc_entries,
                          tick_ctx=tick_ctx)

    # Morning summary: 07:00 UTC every day (overnight recap, focuses on BTC since stocks closed)
    maybe_send_morning_summary(state, utc_min, now_utc.hour, equity, is_weekend)
    # Daily summary: 19:40 UTC if all stocks flat, else fallback at 20:00 UTC. Weekend: 20:00 UTC BTC-only.
    maybe_send_daily_summary(state, utc_min, is_weekend, equity)

    save_state(state)


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        log(f'fatal: {e}')
        # Best-effort Telegram notification of the crash. Always send (no dedupe)
        # because fatal errors halt this tick entirely — we want to see every one.
        try:
            import traceback
            tb = traceback.format_exc().splitlines()
            tail = '\n'.join(tb[-6:])  # last few frames only — keep msg short
            tg(f"🚨 BOT CRASHED on tick\n{type(e).__name__}: {e}\n\n{tail}")
        except Exception:
            pass
        raise
