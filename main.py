"""Long-only dip-bounce bot for CBBTC on the agent-fun/bars-replay/v1 wire protocol.

Strategy (1-minute bars, exact integer math only, no floats, no clock, no
randomness, no environment reads -- two runs on the same input produce
byte-identical replies):

  * Keep every CBBTC close. Once more than 150 closes are recorded, look at
    the trailing high: the maximum of the previous 150 closes, excluding the
    current bar.
  * BUY when the current close is at least 1.5% below that trailing high AND
    this bar closed up on the previous bar (a dip followed by a bounce).
    Size is min(cash, weight room) and never below the hello minimum
    notional. The buy is a single order (max slippage 300 bps); it fills at
    the next bar's open, so on the following batch the entry is rebased to
    the true fill price derived from the position cost basis.
  * While holding, track the peak close. SELL the full position when any of
    these fires: take-profit +0.6% over entry, stop-loss -1.5% under entry,
    trailing stop (peak at least +0.5% over entry, then close gives back
    0.5% from the peak), or 180 bars held. After an exit, wait a 5-bar
    cooldown before the next buy.
  * Never trade during the warm-up bars announced in hello.

Wire protocol: stdin is JSON lines, stdout carries exactly one compact JSON
reply line per batch (flushed before reading the next line), diagnostics go
to stderr. Any protocol violation is fatal (nonzero exit).
"""

import json
import re
import sys

PROTOCOL = "agent-fun/bars-replay/v1"
SYMBOL = "CBBTC"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,20}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
U64_MAX = (1 << 64) - 1

# Strategy constants.
LOOKBACK = 150
DIP_BPS = 150            # buy when close is >= 1.5% below the trailing high
TP_BPS = 60              # take profit at +0.6% over entry
SL_BPS = 150             # stop loss at -1.5% under entry
TRAIL_ARM_BPS = 50       # trailing stop arms at +0.5% over entry
TRAIL_GIVEBACK_BPS = 50  # ... and fires after a 0.5% giveback from the peak
MAX_HOLD_BARS = 180
COOLDOWN_BARS = 5
MAX_SLIPPAGE_BPS = 300


def fail(message):
    """Fatal protocol error: report on stderr and exit nonzero."""
    print("error: " + message, file=sys.stderr)
    sys.exit(1)


def require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        fail("field %s must be an integer" % name)
    return value


def require_nonneg_u64(value, name):
    require_int(value, name)
    if value < 0 or value > U64_MAX:
        fail("field %s out of u64 range" % name)
    return value


def require_symbol(value, name):
    if not isinstance(value, str) or not SYMBOL_RE.match(value):
        fail("field %s is not a valid symbol" % name)
    return value


class Bot:
    def __init__(self, hello):
        capital = hello.get("capital")
        if not isinstance(capital, dict):
            fail("hello missing capital")
        self.quote_scale = require_int(capital.get("quote_scale"), "capital.quote_scale")

        symbols = hello.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            fail("hello missing symbols")
        self.scales = {}
        for entry in symbols:
            if not isinstance(entry, dict):
                fail("hello symbols entry must be an object")
            symbol = require_symbol(entry.get("symbol"), "symbols[].symbol")
            qty_scale = require_int(entry.get("qty_scale"), "symbols[].qty_scale")
            price_scale = require_int(entry.get("price_scale"), "symbols[].price_scale")
            self.scales[symbol] = (qty_scale, price_scale)
        if SYMBOL not in self.scales:
            fail("hello symbols do not include " + SYMBOL)

        caps = hello.get("caps")
        if not isinstance(caps, dict):
            fail("hello missing caps")
        self.max_orders_per_bar = require_int(caps.get("max_orders_per_bar"), "caps.max_orders_per_bar")
        self.max_weight_bps = require_int(caps.get("max_weight_bps"), "caps.max_weight_bps")
        if self.max_orders_per_bar < 0 or self.max_weight_bps < 0 or self.max_weight_bps > 10000:
            fail("hello caps out of range")

        fees = hello.get("fees")
        if not isinstance(fees, dict):
            fail("hello missing fees")
        self.min_notional = require_nonneg_u64(
            fees.get("min_notional_quote_units"), "fees.min_notional_quote_units")

        warmup = hello.get("warmup_bars", 0)
        self.warmup_bars = require_int(warmup, "warmup_bars")
        if self.warmup_bars < 0:
            fail("warmup_bars must be non-negative")

        # Market state.
        self.cash = 0
        self.equity = 0
        self.positions = {}
        self.latest_close = {}
        self.closes = []

        # Trade state.
        self.entry = None
        self.peak = None
        self.entry_seq = None
        self.last_exit_seq = -(10 ** 9)
        self.awaiting_fill = False

        # Order numbering: o1, o2, o3, ... never reused.
        self.order_counter = 0

    def value(self, symbol, qty, price):
        qty_scale, price_scale = self.scales[symbol]
        return qty * price * (10 ** self.quote_scale) // (10 ** (qty_scale + price_scale))

    def weight_room(self, symbol):
        held = self.positions.get(symbol)
        held_qty = held["qty"] if held else 0
        held_price = self.latest_close.get(symbol)
        held_value = 0
        if held_qty and held_price is not None:
            held_value = self.value(symbol, held_qty, held_price)
        room = self.max_weight_bps * self.equity // 10000 - held_value
        return room if room > 0 else 0

    def next_client_id(self):
        self.order_counter += 1
        client_id = "o%d" % self.order_counter
        if not CLIENT_ID_RE.match(client_id):
            fail("generated client_id invalid")
        return client_id

    def decide(self, seq):
        """Return the list of orders for this batch (possibly empty)."""
        warming_up = self.warmup_bars > 0 and seq < self.warmup_bars
        close = self.latest_close.get(SYMBOL)
        if warming_up or close is None:
            return []

        held = self.positions.get(SYMBOL)
        held_qty = held["qty"] if held else 0

        if held_qty > 0:
            # Rebase the entry to the true fill price when a fill just
            # landed (or when the entry was never tracked, e.g. the bot
            # started mid-position). Derived from the hello scales so the
            # inversion of value() stays exact if they ever differ from
            # 8 / 5 / 6: price = cost * 10**(qty+price) // (qty * 10**quote).
            if self.awaiting_fill or self.entry is None:
                qty_scale, price_scale = self.scales[SYMBOL]
                fill_price = (
                    held["cost_quote_units"] * (10 ** (qty_scale + price_scale))
                    // (held_qty * (10 ** self.quote_scale))
                )
                self.entry = fill_price
                self.peak = close
                self.entry_seq = seq
                self.awaiting_fill = False
            if close > self.peak:
                self.peak = close
            entry = self.entry
            peak = self.peak
            take_profit = close * 10000 >= entry * (10000 + TP_BPS)
            stop_loss = close * 10000 <= entry * (10000 - SL_BPS)
            trailing = (
                peak * 10000 >= entry * (10000 + TRAIL_ARM_BPS)
                and close * 10000 <= peak * (10000 - TRAIL_GIVEBACK_BPS)
            )
            max_hold = seq - self.entry_seq >= MAX_HOLD_BARS
            if take_profit or stop_loss or trailing or max_hold:
                self.last_exit_seq = seq
                self.entry = None
                self.peak = None
                self.entry_seq = None
                self.awaiting_fill = False
                return [{
                    "side": "sell",
                    "client_id": self.next_client_id(),
                    "symbol": SYMBOL,
                    "qty": held_qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            return []

        # Flat: forget any stale trade state, then look for a dip-bounce.
        self.entry = None
        self.peak = None
        self.entry_seq = None
        self.awaiting_fill = False
        if len(self.closes) <= LOOKBACK:
            return []
        if seq - self.last_exit_seq < COOLDOWN_BARS:
            return []
        previous_closes = self.closes[-LOOKBACK - 1:-1]
        trailing_high = max(previous_closes)
        previous_close = self.closes[-2]
        dipped = close * 10000 <= trailing_high * (10000 - DIP_BPS)
        bounced = close > previous_close
        if not (dipped and bounced):
            return []
        notional = self.cash
        room = self.weight_room(SYMBOL)
        if room < notional:
            notional = room
        if notional < self.min_notional:
            return []
        self.entry = close
        self.peak = close
        self.entry_seq = seq
        self.awaiting_fill = True
        return [{
            "side": "buy",
            "client_id": self.next_client_id(),
            "symbol": SYMBOL,
            "notional_quote_units": notional,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]


def validate_account(account):
    if not isinstance(account, dict):
        fail("account must be an object")
    cash = require_nonneg_u64(account.get("cash_quote_units"), "account.cash_quote_units")
    equity = require_nonneg_u64(account.get("equity_quote_units"), "account.equity_quote_units")
    positions = account.get("positions")
    if not isinstance(positions, list):
        fail("account.positions must be a list")
    parsed = {}
    for position in positions:
        if not isinstance(position, dict):
            fail("account.positions entry must be an object")
        symbol = require_symbol(position.get("symbol"), "positions[].symbol")
        qty = require_nonneg_u64(position.get("qty"), "positions[].qty")
        cost = require_nonneg_u64(position.get("cost_quote_units"), "positions[].cost_quote_units")
        parsed[symbol] = {"qty": qty, "cost_quote_units": cost}
    return cash, equity, parsed


def validate_bars(bars):
    if not isinstance(bars, list):
        fail("batch.bars must be a list")
    parsed = {}
    for bar in bars:
        if not isinstance(bar, dict):
            fail("batch.bars entry must be an object")
        symbol = require_symbol(bar.get("symbol"), "bars[].symbol")
        for field in ("o", "h", "l", "c"):
            require_nonneg_u64(bar.get(field), "bars[]." + field)
        for field in ("o_depth", "c_depth"):
            depth = bar.get(field)
            if not isinstance(depth, list):
                fail("bars[]." + field + " must be a list")
            for item in depth:
                require_nonneg_u64(item, "bars[]." + field + " item")
        parsed[symbol] = bar
    return parsed


def main():
    bot = None
    batches_seen = 0
    out = sys.stdout

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            fail("input line is not valid JSON")
        if not isinstance(message, dict):
            fail("input line must be a JSON object")
        msg_type = message.get("type")

        if msg_type == "hello":
            if bot is not None:
                fail("second hello received")
            if message.get("protocol") != PROTOCOL:
                fail("unsupported protocol: %r" % (message.get("protocol"),))
            bot = Bot(message)
            continue

        if bot is None:
            fail("message before hello: %r" % (msg_type,))

        if msg_type == "batch":
            seq = require_int(message.get("seq"), "batch.seq")
            if seq != batches_seen:
                fail("batch seq out of order: got %d, expected %d" % (seq, batches_seen))
            require_int(message.get("t"), "batch.t")
            if not isinstance(message.get("fills"), list):
                fail("batch.fills must be a list")
            bars = validate_bars(message.get("bars"))
            cash, equity, positions = validate_account(message.get("account"))
            bot.cash = cash
            bot.equity = equity
            bot.positions = positions
            bar = bars.get(SYMBOL)
            if bar is not None:
                bot.latest_close[SYMBOL] = bar["c"]
                bot.closes.append(bar["c"])
            orders = bot.decide(seq)
            if len(orders) > bot.max_orders_per_bar:
                fail("too many orders for one bar")
            for order in orders:
                if order["side"] == "buy":
                    require_nonneg_u64(order["notional_quote_units"], "order.notional_quote_units")
                else:
                    require_nonneg_u64(order["qty"], "order.qty")
                slippage = order["max_slippage_bps"]
                require_int(slippage, "order.max_slippage_bps")
                if slippage < 0 or slippage > 10000:
                    fail("order.max_slippage_bps out of range")
                if not CLIENT_ID_RE.match(order["client_id"]):
                    fail("order.client_id invalid")
            reply = {"type": "reply", "seq": seq, "orders": orders}
            reply_line = json.dumps(reply, separators=(",", ":"), sort_keys=True)
            if len(reply_line.encode("utf-8")) >= 65536:
                fail("reply line exceeds 64 KiB")
            out.write(reply_line + "\n")
            out.flush()
            batches_seen += 1
            continue

        if msg_type == "done":
            seq = require_int(message.get("seq"), "done.seq")
            if seq != batches_seen:
                fail("done seq mismatch: got %d, expected %d" % (seq, batches_seen))
            validate_account(message.get("account"))
            return 0

        fail("unknown message type: %r" % (msg_type,))

    # EOF without a done message is a protocol violation.
    fail("input ended without done")


if __name__ == "__main__":
    sys.exit(main())
