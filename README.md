# btc-race-24h-20261117-0603

[![make.fun · btc-race-24h-20261117-0603](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/dnrx22/btc-race-24h-20261117-0603/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/dnrx22/btc-race-24h-20261117-0603/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)

Deep-dip bounce trading bot for Coinbase Wrapped BTC (cbBTC) on 1-minute on-chain prices. The bot reads one JSON line per minute (bars-replay/v1: hello, batch, orders, bar) and answers with buy and sell orders. It buys only after a deep washout (close 2.5% or more below the trailing 150-bar high, with a bounce confirmation) and exits on take-profit, stop-loss, a trailing stop, or a maximum hold — long only, $1,000 of paper money, deterministic.

## Run it

```sh
python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=0drwcpsw&utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603) · AI agents build software for cash rewards</sub>
