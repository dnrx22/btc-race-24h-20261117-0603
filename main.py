"""Long-only dip-bounce bot for CBBTC, protocol agent-fun/bars-replay/v1.

Strategy (exact integer math only, cross-multiplication, never floats):
  bars are 1 minute. Keep the closes. After 150 closes, buy when the
  close is at least 1.5% below the trailing 150-bar high AND this bar
  closed up on the previous bar (dip-bounce). Size is the smaller of
  cash and the weight room implied by max_weight_bps. Exit the whole
  position on take-profit +0.6%, stop-loss -1.5%, a trailing exit
  (armed at +0.5%, fires after a 0.5% giveback from the peak), or a
  180-bar max hold, with a 5-bar cooldown after each exit.

Wire rule that matters most: the harness sends every u64 amount as a
JSON int OR as a decimal string (for example "100000000"). Every
amount in the stream is therefore parsed through to_u64, which
accepts both forms and rejects anything else. A value is never
rejected merely for being a string. Bar depth fields (o_depth /
c_depth) are ignored entirely; the bot never uses pool depth.

Deterministic, standard library only: no clock, no environment, no
unseeded randomness. Two runs on the same input produce byte-identical
replies. stdin is JSON lines, stdout carries exactly one compact JSON
reply line per batch, diagnostics go to stderr.
"""

import json
import re
import sys

U64_MAX = 2 ** 64 - 1
U64_STR_RE = re.compile(r"(0|[1-9][0-9]{0,19})")
SYMBOL_RE = re.compile(r"[A-Z0-9]{1,20}")
CLIENT_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

SYMBOL = "CBBTC"
PROTOCOL = "agent-fun/bars-replay/v1"

LOOKBACK = 150
DIP_BPS = 9850          # buy when close <= high * 9850 / 10000 (-1.5%)
TAKE_PROFIT_BPS = 10060  # exit when close >= entry * 10060 / 10000 (+0.6%)
STOP_LOSS_BPS = 9850    # exit when close <= entry * 9850 / 10000 (-1.5%)
TRAIL_ARM_BPS = 10050    # trailing armed when peak >= entry * 10050 / 10000
TRAIL_GIVEBACK_BPS = 9950  # trailing fires when close <= peak * 9950 / 10000
MAX_HOLD_BARS = 180
COOLDOWN_BARS = 5
MAX_SLIPPAGE_BPS = 300


def fail(message):
    print("error: " + message, file=sys.stderr)
    raise SystemExit(1)


def to_u64(value):
    """Parse a u64 that may arrive as a JSON int or a decimal string.

    Accepts a real int (not a bool) or a string matching 0 or
    [1-9][0-9]{0,19}; the result must lie in 0..2**64-1. Anything else
    (float, bool, None, a malformed string, out of range) is fatal.
    """
    if isinstance(value, bool):
        fail("u64 value must not be a bool")
    if isinstance(value, int):
        if 0 <= value <= U64_MAX:
            return value
        fail("u64 value out of range")
    if isinstance(value, str):
        if U64_STR_RE.fullmatch(value) is None:
            fail("u64 string is not a canonical decimal integer")
        number = int(value)
        if 0 <= number <= U64_MAX:
            return number
        fail("u64 value out of range")
    fail("u64 value must be an int or a decimal string")


def to_scale(value, name):
    """Parse a small scale (quote/qty/price scale): int or digit string."""
    if isinstance(value, bool):
        fail(name + " must not be a bool")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        if value == "" or not value.isdigit():
            fail(name + " is not a decimal integer")
        number = int(value)
    else:
        fail(name + " must be an int or a decimal string")
    if 0 <= number <= 30:
        return number
    fail(name + " out of range")


def check_symbol(value):
    if not isinstance(value, str):
        fail("symbol must be a string")
    if SYMBOL_RE.fullmatch(value) is None:
        fail("symbol does not match [A-Z0-9]{1,20}")
    return value


def position_value(qty, price, quote_scale, qty_scale, price_scale):
    """Value of qty units at price, in quote units (integer math)."""
    return qty * price * 10 ** quote_scale // 10 ** (qty_scale + price_scale)


def parse_account(raw):
    """Parse an account object; returns (cash, equity, {symbol: (qty, cost)})."""
    if not isinstance(raw, dict):
        fail("account must be an object")
    if "cash_quote_units" not in raw or "equity_quote_units" not in raw:
        fail("account is missing cash or equity")
    cash = to_u64(raw["cash_quote_units"])
    equity = to_u64(raw["equity_quote_units"])
    positions = {}
    raw_positions = raw.get("positions", [])
    if not isinstance(raw_positions, list):
        fail("account positions must be a list")
    for item in raw_positions:
        if not isinstance(item, dict):
            fail("position must be an object")
        if "symbol" not in item or "qty" not in item:
            fail("position is missing symbol or qty")
        if "cost_quote_units" not in item:
            fail("position is missing cost_quote_units")
        symbol = check_symbol(item["symbol"])
        qty = to_u64(item["qty"])
        cost = to_u64(item["cost_quote_units"])
        positions[symbol] = (qty, cost)
    return cash, equity, positions


def main():
    hello_seen = False
    done_seen = False
    quote_scale = 0
    qty_scale = 0
    price_scale = 0
    max_orders_per_bar = 0
    max_weight_bps = 0
    min_notional = 0
    warmup_bars = 0

    expected_seq = 0
    batches_seen = 0
    order_counter = 0

    cash = 0
    equity = 0
    positions = {}
    closes = []

    entry = None
    peak = None
    entry_seq = None
    last_exit_seq = -1000000000
    awaiting_fill = False

    out = sys.stdout

    for raw_line in sys.stdin:
        if raw_line.strip() == "":
            continue
        try:
            message = json.loads(raw_line)
        except ValueError:
            fail("input line is not valid JSON")
        if not isinstance(message, dict):
            fail("input line must be a JSON object")
        msg_type = message.get("type")

        if not hello_seen:
            if msg_type != "hello":
                fail("first message must be hello")
            if message.get("protocol") != PROTOCOL:
                fail("protocol must be agent-fun/bars-replay/v1")
            capital = message.get("capital")
            if not isinstance(capital, dict):
                fail("hello capital must be an object")
            if "quote_units" not in capital or "quote_scale" not in capital:
                fail("hello capital is missing quote_units or quote_scale")
            to_u64(capital["quote_units"])
            quote_scale = to_scale(capital["quote_scale"], "quote_scale")
            raw_symbols = message.get("symbols")
            if not isinstance(raw_symbols, list) or not raw_symbols:
                fail("hello symbols must be a non-empty list")
            scales = {}
            for item in raw_symbols:
                if not isinstance(item, dict):
                    fail("hello symbol must be an object")
                if "symbol" not in item:
                    fail("hello symbol is missing symbol")
                if "qty_scale" not in item or "price_scale" not in item:
                    fail("hello symbol is missing a scale")
                symbol = check_symbol(item["symbol"])
                scales[symbol] = (
                    to_scale(item["qty_scale"], "qty_scale"),
                    to_scale(item["price_scale"], "price_scale"),
                )
            if SYMBOL not in scales:
                fail("hello symbols must include CBBTC")
            qty_scale, price_scale = scales[SYMBOL]
            caps = message.get("caps")
            if not isinstance(caps, dict):
                fail("hello caps must be an object")
            if "max_orders_per_bar" not in caps or "max_weight_bps" not in caps:
                fail("hello caps is missing a field")
            max_orders_per_bar = to_u64(caps["max_orders_per_bar"])
            max_weight_bps = to_u64(caps["max_weight_bps"])
            fees = message.get("fees")
            if not isinstance(fees, dict):
                fail("hello fees must be an object")
            if "min_notional_quote_units" not in fees:
                fail("hello fees is missing min_notional_quote_units")
            min_notional = to_u64(fees["min_notional_quote_units"])
            if "warmup_bars" in message and message["warmup_bars"] is not None:
                warmup_bars = to_u64(message["warmup_bars"])
            else:
                warmup_bars = 0
            hello_seen = True
            continue

        if msg_type == "hello":
            fail("second hello is a protocol error")
        if msg_type == "done":
            if "seq" not in message:
                fail("done is missing seq")
            done_seq = to_u64(message["seq"])
            if done_seq != batches_seen:
                fail("done seq must equal the number of batches seen")
            if "account" not in message:
                fail("done is missing account")
            parse_account(message["account"])
            done_seen = True
            break
        if msg_type != "batch":
            fail("unknown message type")

        if "seq" not in message or "t" not in message:
            fail("batch is missing seq or t")
        seq = to_u64(message["seq"])
        to_u64(message["t"])
        if seq != expected_seq:
            fail("batch seq is not the expected next seq")
        raw_bars = message.get("bars")
        if not isinstance(raw_bars, list):
            fail("batch bars must be a list")
        fills = message.get("fills", [])
        if not isinstance(fills, list):
            fail("batch fills must be a list")
        if "account" not in message:
            fail("batch is missing account")
        cash, equity, positions = parse_account(message["account"])

        close = None
        for bar in raw_bars:
            if not isinstance(bar, dict):
                fail("bar must be an object")
            if "symbol" not in bar:
                fail("bar is missing symbol")
            if "o" not in bar or "h" not in bar:
                fail("bar is missing o or h")
            if "l" not in bar or "c" not in bar:
                fail("bar is missing l or c")
            bar_symbol = check_symbol(bar["symbol"])
            to_u64(bar["o"])
            to_u64(bar["h"])
            to_u64(bar["l"])
            bar_close = to_u64(bar["c"])
            # Depth fields (o_depth / c_depth) are ignored entirely.
            if bar_symbol == SYMBOL:
                # If a batch ever carried two bars for CBBTC, the
                # last one is the latest close.
                close = bar_close

        if close is not None:
            closes.append(close)

        warming_up = warmup_bars > 0 and seq < warmup_bars
        orders = []
        # A zero order cap means no order may ever be sent; skip the
        # trading logic entirely so no exit or buy is recorded for an
        # order that can never leave this process.
        if not warming_up and close is not None and max_orders_per_bar > 0:
            held = positions.get(SYMBOL, (0, 0))
            held_qty, held_cost = held
            if held_qty > 0:
                if awaiting_fill or entry is None:
                    if held_cost > 0:
                        fill_price = (
                            held_cost
                            * 10 ** (qty_scale + price_scale)
                            // (held_qty * 10 ** quote_scale)
                        )
                    else:
                        fill_price = close
                    entry = fill_price
                    peak = close
                    entry_seq = seq
                    awaiting_fill = False
                if entry is None:
                    entry = close
                    peak = close
                    entry_seq = seq
                if peak is None:
                    peak = close
                elif close > peak:
                    peak = close
                if entry_seq is None:
                    entry_seq = seq
                take_profit = close * 10000 >= entry * TAKE_PROFIT_BPS
                stop_loss = close * 10000 <= entry * STOP_LOSS_BPS
                trailing = (
                    peak * 10000 >= entry * TRAIL_ARM_BPS
                    and close * 10000 <= peak * TRAIL_GIVEBACK_BPS
                )
                max_hold = seq - entry_seq >= MAX_HOLD_BARS
                if take_profit or stop_loss or trailing or max_hold:
                    order_counter += 1
                    client_id = "o" + str(order_counter)
                    if CLIENT_RE.fullmatch(client_id) is None:
                        fail("generated client_id is invalid")
                    orders = [
                        {
                            "side": "sell",
                            "client_id": client_id,
                            "symbol": SYMBOL,
                            "qty": held_qty,
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        }
                    ]
                    last_exit_seq = seq
                    entry = None
                    peak = None
                    entry_seq = None
                    awaiting_fill = False
            else:
                entry = None
                peak = None
                if awaiting_fill:
                    # The buy was sent but no fill has shown up yet; do
                    # not stack a second buy on the next bar. If the
                    # fill still has not appeared two bars after the
                    # buy, treat it as missed and allow a fresh buy.
                    if entry_seq is not None and seq - entry_seq >= 2:
                        awaiting_fill = False
                    else:
                        orders = []
                        awaiting_fill = True
                if not awaiting_fill:
                    if len(closes) > LOOKBACK and seq - last_exit_seq >= COOLDOWN_BARS:
                        previous_close = closes[-2]
                        trailing_high = max(closes[-151:-1])
                        dip = close * 10000 <= trailing_high * DIP_BPS
                        bounce = close > previous_close
                        if dip and bounce:
                            # Held qty is 0 on this branch, so this is 0;
                            # use the shared formula instead of a magic
                            # constant so the weight room stays correct
                            # if this branch ever sees a residual position.
                            held_value = position_value(
                                held_qty, close, quote_scale, qty_scale, price_scale
                            )
                            weight_room = (
                                max_weight_bps * equity // 10000 - held_value
                            )
                            if weight_room < 0:
                                weight_room = 0
                            notional = cash if cash < weight_room else weight_room
                            if notional >= min_notional and notional > 0:
                                order_counter += 1
                                client_id = "o" + str(order_counter)
                                if CLIENT_RE.fullmatch(client_id) is None:
                                    fail("generated client_id is invalid")
                                orders = [
                                    {
                                        "side": "buy",
                                        "client_id": client_id,
                                        "symbol": SYMBOL,
                                        "notional_quote_units": notional,
                                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                                    }
                                ]
                                entry = close
                                peak = close
                                entry_seq = seq
                                awaiting_fill = True
                # Keep entry_seq from the buy (used by the missed-fill
                # timeout above); entry/peak are rebuilt from the true
                # fill price once the position appears.

        if len(orders) > max_orders_per_bar:
            orders = []
        reply = {"type": "reply", "seq": seq, "orders": orders}
        out.write(json.dumps(reply, separators=(",", ":")) + "\n")
        out.flush()
        expected_seq += 1
        batches_seen += 1

    if not hello_seen:
        fail("no hello received")
    if not done_seen:
        fail("EOF without done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
