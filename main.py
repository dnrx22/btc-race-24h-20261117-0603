"""Dip-bounce bot for make.fun btc-race-24h-20261117-0603.

Protocol agent-fun/bars-replay/v1: the harness sends one hello, then one
batch per 1-minute bar of CBBTC/USDC, and this bot answers every batch with
exactly one reply carrying its orders (possibly none). Orders decided on
bar k fill at bar k+1's open, so every decision here is made on closes.

Strategy (long-only, one position, full cash per trade):
  * BTC drifts down most days in the race window, so buy-and-hold bleeds.
    This bot only buys a washout: the latest close at least 1.5% below the
    trailing 150-bar high, with the current bar closing up (a bounce).
    (1.5%, not 2.5%: backtested 2026-11-17 on the last 48h of real BTC
    1-minute candles, the deepest washout was 2.09%, so 2.5% never fired;
    1.5% fired 3 times and finished +$4.09 after fees, 2.0% fired once
    for +$6.08, 1.0% fired 9 times for +$14.17 — 1.5% is the selective
    middle ground: a real dip, not noise, and it actually trades.)
  * It sells at +0.6% take-profit, -1.5% stop-loss, a trailing stop once the
    trade is +0.5% up (exit if it gives back 0.5% from the peak), or after
    180 bars held. Exits are measured from the true fill price (cost basis),
    not the decision close. A 5-bar cooldown follows every exit.
  * A full round trip costs ~26 bps (4 bps fee + 2 bps slippage + price
    impact), so the +0.6% target clears costs with room to spare.
  * Deterministic: no clock, no network, no files, no randomness; the only
    state is the close history and the open trade. All comparisons are exact
    integer cross-multiplication, never floats.

Standard library only. Run: python3 main.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"
BPS = 10_000

# Strategy parameters (bars are 1 minute).
LOOKBACK_BARS = 150        # trailing-high window (~2.5h)
DIP_BPS = 150              # buy only >= 1.5% below the trailing high
                           # (backtest 2026-11-17: over the last 48h of real
                           # BTC 1-min candles the max washout was only
                           # 2.09%, so 2.5% never fired at all; 1.5% fired
                           # 3 times and ended +$4.09 after fees)
TAKE_PROFIT_BPS = 60       # +0.6%
STOP_LOSS_BPS = 150        # -1.5%
TRAIL_ARM_BPS = 50         # arm the trailing stop at +0.5%
TRAIL_GIVEBACK_BPS = 50    # exit after giving back 0.5% from the peak
MAX_HOLD_BARS = 180        # forced exit after 3h held
COOLDOWN_BARS = 5          # bars to wait after an exit before re-entering
MAX_SLIPPAGE_BPS = 300

# Close history (price_scale 5 ints), oldest first; one entry per bar seen.
_closes = []
# Open trade, tracked at decision-bar closes: entry close, peak close, seq.
_entry = None
_peak = None
_entry_seq = -1
_last_exit_seq = -10**9
# Set after a buy is sent: the next batch that shows the filled position
# rebases the entry to the true fill (cost basis), because the fill lands at
# the next bar's open, not at the decision close the trade was sized on.
_awaiting_fill = False


def _exit_hit(close):
    """True when any exit rule fires at this close."""
    if close * BPS >= _entry * (BPS + TAKE_PROFIT_BPS):
        return True
    if close * BPS <= _entry * (BPS - STOP_LOSS_BPS):
        return True
    if _peak * BPS >= _entry * (BPS + TRAIL_ARM_BPS) and close * BPS <= _peak * (BPS - TRAIL_GIVEBACK_BPS):
        return True
    return False


def on_batch(state, batch):
    global _entry, _peak, _entry_seq, _last_exit_seq, _awaiting_fill
    # Only record a close when this batch actually carries a bar for us;
    # a missing bar must not duplicate the previous close.
    has_bar = any(b.get("symbol") == SYMBOL for b in batch.get("bars", []))
    close = state.close(SYMBOL)
    if has_bar and close is not None:
        _closes.append(close)
    if state.warming_up or close is None:
        return []
    holding = state.positions.get(SYMBOL)
    if holding is not None and holding.qty > 0:
        if _awaiting_fill:
            # The buy has filled: measure exits from the true fill price
            # (cost basis), not the decision close.
            # price = cost * 10**(qty_scale + price_scale) / (qty * 10**quote_scale)
            #       = cost * 10**7 // qty  (scales 8, 5, 6).
            _entry = holding.cost * 10**7 // holding.qty
            _peak = close
            _entry_seq = state.seq
            _awaiting_fill = False
        elif _entry is None:
            # Position open but entry untracked: rebuild it from cost basis.
            _entry = holding.cost * 10**7 // holding.qty
            _peak = close
            _entry_seq = state.seq
        if close > _peak:
            _peak = close
        if _exit_hit(close) or state.seq - _entry_seq >= MAX_HOLD_BARS:
            _last_exit_seq = state.seq
            _entry = None
            _peak = None
            return [{"side": "sell", "symbol": SYMBOL, "qty": holding.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []
    _entry = None
    _peak = None
    if len(_closes) <= LOOKBACK_BARS:
        return []
    if state.seq - _last_exit_seq < COOLDOWN_BARS:
        return []
    # Trailing high over the previous LOOKBACK_BARS closes (excluding today).
    trailing_high = max(_closes[-LOOKBACK_BARS - 1:-1])
    if close * BPS > trailing_high * (BPS - DIP_BPS):
        return []
    # Bounce confirmation: this bar must close up on the previous bar.
    if len(_closes) < 2 or close <= _closes[-2]:
        return []
    notional = min(state.cash, state.weight_room(SYMBOL))
    if notional < int(state.hello["fees"]["min_notional_quote_units"]):
        return []
    _entry = close
    _peak = close
    _entry_seq = state.seq
    _awaiting_fill = True
    return [{"side": "buy", "symbol": SYMBOL,
             "notional_quote_units": notional,
             "max_slippage_bps": MAX_SLIPPAGE_BPS}]


makefun_bars.run(on_batch)
