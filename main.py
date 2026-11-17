#!/usr/bin/env python3
"""Dip-bounce long-only bot for CBBTC on make.fun, protocol agent-fun/bars-replay/v1.

Strategy (exact integer math only, no floats anywhere):
  * 1-minute bars. Track the last closes of CBBTC.
  * BUY when the close is at least 1.5% below the trailing high of the
    previous 150 closes AND the bar closed up on the previous bar
    (a dip followed by a bounce). One open position at most.
  * SELL the full position on take-profit +0.6%, stop-loss -1.5%, a
    trailing exit (armed at +0.5% from entry, fires after a 0.5%
    giveback from the peak close), or a 180-bar max hold.
  * 5-bar cooldown after an exit; a buy whose fill never shows up
    within 2 bars is treated as missed and a fresh buy is allowed.

Wire rules this file implements:
  * INPUT u64 values may arrive as JSON ints OR as decimal strings
    (e.g. "1000000000"). A single helper, to_u64(), accepts both and
    rejects anything else as a fatal protocol error.
  * *** REPLY AMOUNTS MUST BE DECIMAL STRINGS ***: in every reply,
    notional_quote_units (buy) and qty (sell) are emitted as decimal
    strings, e.g. "1000000000", never as JSON numbers. The harness
    rejects an int amount as a protocol violation and fails the run.
    seq stays a JSON int and max_slippage_bps stays a JSON int.
  * Deterministic: no clock, no environment, no unseeded randomness,
    no floats. Two runs on the same input produce byte-identical
    replies. Standard library only, single file, no local imports.
"""

import json
import re
import sys

PROTOCOL = "agent-fun/bars-replay/v1"
SYMBOL = "CBBTC"

# Strategy constants.
LOOKBACK = 150
DIP_BPS = 9850            # buy at >= 1.5% below trailing high
TAKE_PROFIT_BPS = 10060   # +0.6%
STOP_LOSS_BPS = 9850      # -1.5%
TRAIL_ARM_BPS = 10050     # +0.5%
TRAIL_GIVEBACK_BPS = 9950  # 0.5% giveback from peak
MAX_HOLD_BARS = 180
COOLDOWN_BARS = 5
MISSED_FILL_BARS = 2
MAX_SLIPPAGE_BPS = 300

_U64_RE = re.compile(r"(?:0|[1-9][0-9]{0,19})")
_SYMBOL_RE = re.compile(r"[A-Z0-9]{1,20}")
_CLIENT_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_U64 = 2 ** 64 - 1


class Fatal(Exception):
    """A protocol violation: print to stderr and exit nonzero."""


def fail(message):
    raise Fatal(message)


def to_u64(value, field):
    """Accept a u64 as a JSON int or as a decimal string; else fatal.

    Accepts a real int (not bool) or a string matching 0 or
    [1-9][0-9]{0,19}. Rejects floats, bools, negative or over-long
    strings, leading-zero strings such as "007", and anything outside
    0..2**64-1. Never rejects a value merely for being a string.
    """
    if isinstance(value, bool):
        fail("field %s: bool is not a u64" % field)
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        if _U64_RE.fullmatch(value) is None:
            fail("field %s: not a decimal u64 string: %r" % (field, value))
        number = int(value)
    else:
        fail("field %s: not an int or decimal string" % field)
    if number < 0 or number > _MAX_U64:
        fail("field %s: u64 out of range" % field)
    return number


def to_plain_int(value, field, minimum=None, maximum=None):
    """Accept a plain JSON int (not bool, not string) within bounds."""
    if isinstance(value, bool) or not isinstance(value, int):
        fail("field %s: not a plain int" % field)
    if minimum is not None and value < minimum:
        fail("field %s: below minimum %d" % (field, minimum))
    if maximum is not None and value > maximum:
        fail("field %s: above maximum %d" % (field, maximum))
    return value


def check_symbol(value, field):
    if not isinstance(value, str) or _SYMBOL_RE.fullmatch(value) is None:
        fail("field %s: bad symbol %r" % (field, value))
    return value


def check_client_id(value):
    if not isinstance(value, str):
        fail("client_id is not a string")
    if _CLIENT_RE.fullmatch(value) is None or value.startswith("_"):
        fail("bad client_id %r" % (value,))
    return value


def check_account(raw, field):
    """Validate an account block; return (cash, equity, positions)."""
    if not isinstance(raw, dict):
        fail("field %s: account is not an object" % field)
    cash = to_u64(raw.get("cash_quote_units"), field + ".cash_quote_units")
    equity = to_u64(raw.get("equity_quote_units"), field + ".equity_quote_units")
    positions = raw.get("positions")
    if not isinstance(positions, list):
        fail("field %s: positions is not a list" % field)
    parsed = []
    for position in positions:
        if not isinstance(position, dict):
            fail("field %s: position is not an object" % field)
        symbol = check_symbol(position.get("symbol"), field + ".positions.symbol")
        qty = to_u64(position.get("qty"), field + ".positions.qty")
        cost = to_u64(position.get("cost_quote_units"),
                      field + ".positions.cost_quote_units")
        parsed.append((symbol, qty, cost))
    return cash, equity, parsed


class Bot:
    def __init__(self, hello):
        capital = hello.get("capital")
        if not isinstance(capital, dict):
            fail("hello.capital is not an object")
        if not isinstance(capital.get("quote"), str):
            fail("hello.capital.quote is not a string")
        self.quote_scale = to_plain_int(capital.get("quote_scale"),
                                        "hello.capital.quote_scale", 0, 18)
        # Validated for shape; the bot sizes from per-batch account data.
        to_u64(capital.get("quote_units"), "hello.capital.quote_units")

        raw_symbols = hello.get("symbols")
        if not isinstance(raw_symbols, list) or not raw_symbols:
            fail("hello.symbols is not a non-empty list")
        self.scales = {}
        for entry in raw_symbols:
            if not isinstance(entry, dict):
                fail("hello.symbols entry is not an object")
            symbol = check_symbol(entry.get("symbol"), "hello.symbols.symbol")
            price_scale = to_plain_int(entry.get("price_scale"),
                                       "hello.symbols.price_scale", 0, 18)
            qty_scale = to_plain_int(entry.get("qty_scale"),
                                     "hello.symbols.qty_scale", 0, 18)
            self.scales[symbol] = (price_scale, qty_scale)
        # Fallbacks only if the harness ever omits CBBTC from symbols.
        self.price_scale, self.qty_scale = self.scales.get(SYMBOL, (5, 8))

        caps = hello.get("caps")
        if not isinstance(caps, dict):
            fail("hello.caps is not an object")
        self.max_open_positions = to_plain_int(
            caps.get("max_open_positions"), "hello.caps.max_open_positions", 0)
        self.max_orders_per_bar = to_plain_int(
            caps.get("max_orders_per_bar"), "hello.caps.max_orders_per_bar", 0)
        self.max_weight_bps = to_plain_int(
            caps.get("max_weight_bps"), "hello.caps.max_weight_bps", 0, 10000)
        if not isinstance(caps.get("shorts"), bool):
            fail("hello.caps.shorts is not a bool")
        if not isinstance(caps.get("leverage"), bool):
            fail("hello.caps.leverage is not a bool")

        fees = hello.get("fees")
        if not isinstance(fees, dict):
            fail("hello.fees is not an object")
        to_plain_int(fees.get("fee_bps"), "hello.fees.fee_bps", 0)
        to_plain_int(fees.get("slippage_bps"), "hello.fees.slippage_bps", 0)
        self.min_notional = to_u64(fees.get("min_notional_quote_units"),
                                   "hello.fees.min_notional_quote_units")

        if "warmup_bars" in hello and hello.get("warmup_bars") is not None:
            self.warmup_bars = to_u64(hello.get("warmup_bars"),
                                      "hello.warmup_bars")
        else:
            self.warmup_bars = 0

        # Strategy state.
        self.closes = []          # CBBTC closes, oldest first
        self.latest_close = {}    # symbol -> latest close int
        self.entry = None         # entry price (price_scale ints)
        self.peak = None          # highest close since entry
        self.entry_seq = None
        self.last_exit_seq = -(10 ** 9)
        self.awaiting_fill = False
        self.buy_sent_seq = None
        self.order_counter = 0

    def close(self, symbol):
        return self.latest_close.get(symbol)

    def value(self, symbol, qty, price):
        """Position value in quote units at the given price."""
        if qty <= 0 or price is None:
            return 0
        price_scale, qty_scale = self.scales.get(symbol, (5, 8))
        return (qty * price * (10 ** self.quote_scale)
                // (10 ** (qty_scale + price_scale)))

    def weight_room(self, symbol):
        """Remaining buy room in quote units under the weight cap."""
        held_qty = self.held_qty
        price = self.close(symbol)
        held_value = self.value(symbol, held_qty, price)
        room = self.max_weight_bps * self.equity // 10000 - held_value
        return room if room > 0 else 0

    def fill_price(self, qty, cost):
        """True per-unit fill price from cost basis, in price-scale ints."""
        return (cost * (10 ** (self.qty_scale + self.price_scale))
                // (qty * (10 ** self.quote_scale)))

    def next_client_id(self):
        self.order_counter += 1
        client_id = "o%d" % self.order_counter
        return check_client_id(client_id)

    def buy_order(self, notional):
        # *** ROUND-4 FIX: amounts are decimal STRINGS in replies. ***
        return {"side": "buy",
                "client_id": self.next_client_id(),
                "symbol": SYMBOL,
                "notional_quote_units": str(notional),
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def sell_order(self, qty):
        # *** ROUND-4 FIX: amounts are decimal STRINGS in replies. ***
        return {"side": "sell",
                "client_id": self.next_client_id(),
                "symbol": SYMBOL,
                "qty": str(qty),
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def decide(self, seq, close, held_qty, held_cost):
        """Return the order list for this batch (possibly empty)."""
        if self.warmup_bars > 0 and seq < self.warmup_bars:
            return []
        if close is None:
            return []

        if held_qty > 0:
            # Holding: track the open trade, exit on any trigger.
            if self.awaiting_fill or self.entry is None:
                self.entry = self.fill_price(held_qty, held_cost)
                self.peak = close
                self.entry_seq = seq
                self.awaiting_fill = False
                self.buy_sent_seq = None
            if close > self.peak:
                self.peak = close
            take_profit = close * 10000 >= self.entry * TAKE_PROFIT_BPS
            stop_loss = close * 10000 <= self.entry * STOP_LOSS_BPS
            trailing = (self.peak * 10000 >= self.entry * TRAIL_ARM_BPS
                        and close * 10000 <= self.peak * TRAIL_GIVEBACK_BPS)
            max_hold = seq - self.entry_seq >= MAX_HOLD_BARS
            if take_profit or stop_loss or trailing or max_hold:
                self.last_exit_seq = seq
                self.entry = None
                self.peak = None
                self.awaiting_fill = False
                self.buy_sent_seq = None
                return [self.sell_order(held_qty)]
            return []

        # Not holding: clear any stale trade tracking.
        self.entry = None
        self.peak = None
        if self.awaiting_fill:
            if (self.buy_sent_seq is not None
                    and seq - self.buy_sent_seq >= MISSED_FILL_BARS):
                # Fill never showed up: treat the buy as missed.
                self.awaiting_fill = False
                self.buy_sent_seq = None
            else:
                # Fill may still land next bar; wait, do not double-buy.
                return []
        if len(self.closes) <= LOOKBACK:
            return []
        if seq - self.last_exit_seq < COOLDOWN_BARS:
            return []
        trailing_high = max(self.closes[-LOOKBACK - 1:-1])
        previous_close = self.closes[-2]
        dipped = close * 10000 <= trailing_high * DIP_BPS
        bounced = close > previous_close
        if not (dipped and bounced):
            return []
        notional = self.cash
        room = self.weight_room(SYMBOL)
        if room < notional:
            notional = room
        if notional <= 0:
            # Zero cash or zero weight room: a zero-notional buy would be
            # a protocol violation, so stand aside instead of ordering.
            return []
        if notional < self.min_notional:
            return []
        self.entry = close
        self.peak = close
        self.entry_seq = seq
        self.awaiting_fill = True
        self.buy_sent_seq = seq
        return [self.buy_order(notional)]


def run():
    bot = None
    expected_seq = 0
    batches_seen = 0
    done_seen = False
    out = sys.stdout

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            fail("line is not valid JSON")
        if not isinstance(message, dict):
            fail("message is not a JSON object")
        kind = message.get("type")

        if kind == "hello":
            if bot is not None:
                fail("second hello")
            if message.get("protocol") != PROTOCOL:
                fail("bad protocol %r" % (message.get("protocol"),))
            bot = Bot(message)
            continue

        if bot is None:
            fail("first message is not hello")

        if kind == "batch":
            seq = to_u64(message.get("seq"), "batch.seq")
            if seq != expected_seq:
                fail("batch seq %d, expected %d" % (seq, expected_seq))
            to_u64(message.get("t"), "batch.t")
            bars = message.get("bars")
            if not isinstance(bars, list):
                fail("batch.bars is not a list")
            fills = message.get("fills")
            if fills is not None and not isinstance(fills, list):
                fail("batch.fills is not a list")
            cash, equity, positions = check_account(
                message.get("account"), "batch.account")
            bot.cash = cash
            bot.equity = equity
            bot.held_qty = 0
            bot.held_cost = 0
            for symbol, qty, cost in positions:
                if symbol == SYMBOL:
                    bot.held_qty = qty
                    bot.held_cost = cost

            close = None
            for bar in bars:
                if not isinstance(bar, dict):
                    fail("batch.bars entry is not an object")
                symbol = check_symbol(bar.get("symbol"), "batch.bars.symbol")
                to_u64(bar.get("t"), "batch.bars.t")
                to_u64(bar.get("o"), "batch.bars.o")
                to_u64(bar.get("h"), "batch.bars.h")
                to_u64(bar.get("l"), "batch.bars.l")
                bar_close = to_u64(bar.get("c"), "batch.bars.c")
                volume = bar.get("v")
                if volume is not None:
                    to_u64(volume, "batch.bars.v")
                # Depth fields (o_depth / c_depth) are ignored entirely.
                bot.latest_close[symbol] = bar_close
                if symbol == SYMBOL:
                    close = bar_close
            if close is not None:
                bot.closes.append(close)

            orders = bot.decide(seq, close, bot.held_qty, bot.held_cost)
            if len(orders) > bot.max_orders_per_bar:
                fail("too many orders for one bar")
            for order in orders:
                check_symbol(order["symbol"], "reply order symbol")
                check_client_id(order["client_id"])
                to_plain_int(order["max_slippage_bps"],
                             "reply max_slippage_bps", 0, 10000)
                amount = (order.get("notional_quote_units")
                          if order["side"] == "buy" else order.get("qty"))
                if not isinstance(amount, str):
                    fail("reply amount is not a decimal string")
                to_u64(amount, "reply order amount")
            reply = json.dumps({"type": "reply", "seq": seq, "orders": orders},
                               separators=(",", ":"))
            if len(reply.encode("utf-8")) >= 65536:
                fail("reply exceeds 64 KiB")
            out.write(reply + "\n")
            out.flush()
            expected_seq += 1
            batches_seen += 1
            continue

        if kind == "done":
            seq = to_u64(message.get("seq"), "done.seq")
            if seq != batches_seen:
                fail("done seq %d, batches seen %d" % (seq, batches_seen))
            if not isinstance(message.get("reason"), str):
                fail("done.reason is not a string")
            check_account(message.get("account"), "done.account")
            done_seen = True
            break

        fail("unknown message type %r" % (kind,))

    if not done_seen:
        fail("EOF without done")


def main():
    try:
        run()
    except Fatal as exc:
        sys.stderr.write("fatal: %s\n" % (exc,))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
