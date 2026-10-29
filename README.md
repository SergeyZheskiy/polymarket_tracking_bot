<div align="center">

# Polymarket Signals Bot

**Real-time Telegram alerts when the wallets you follow trade on [Polymarket](https://polymarket.com).**

[![CI](https://github.com/SergeyZheskiy/polymarket-signals-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/SergeyZheskiy/polymarket-signals-bot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

English · [Русский](README.ru.md)

<img src="docs/preview.svg" alt="Signal in Telegram" width="640">

</div>

## Features

- **Fast.** Trades come from Polymarket's live trade stream, so a signal
  usually arrives 1–3 seconds after the fill. A backup poll of each wallet's
  history runs every 20 seconds, so nothing is lost if the stream drops.
- **No spam.** A large order is often filled by dozens of counterparties. The
  bot sends **one message per series** and then edits it in place to show the
  running total, average price and fill count, without new notifications.
- **Access control.** The bot is invite-only by default: a stranger presses
  `/start` and the admin gets an approve/decline request. An `/admin` panel
  manages users. Set `open: true` to let anyone subscribe.
- **No Polymarket keys.** The bot uses only public APIs.
- **Simple to run.** Three small Python files, plain-text config, and Docker
  Compose for 24/7 hosting.

> The bot's Telegram interface and code comments are in Russian.

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/SergeyZheskiy/polymarket-signals-bot.git
cd polymarket-signals-bot
pip install -r requirements.txt

cp .env.example .env                  # bot token + your Telegram id
cp wallets.example.txt wallets.txt    # wallets to follow
python notifier.py
```

1. Get a bot token from [@BotFather](https://t.me/BotFather) with `/newbot`.
2. To find your Telegram id, leave `ADMINS` empty, start the bot and send it
   `/start`. The console prints `заявка на доступ: …, id 123456789`. Put that id
   into `ADMINS` and restart.

## Wallets

`wallets.txt` holds one wallet per line, as an address or a profile link:

```
0x2005d16a84ceefa912d4e380cd32e7ff827875ea   # RN1
https://polymarket.com/@swisstony
https://polymarket.com/ru/@gopfan2?tab=activity
```

The file is re-read every 5 seconds, so there is no need to restart. By
design, the list can't be changed from Telegram.

## Telegram commands

| Who | Command | Action |
|---|---|---|
| anyone with access | `/start` | turn signals on |
| | `/stop` | turn signals off |
| admin | `/admin` | panel: users, pending requests, subscribers |
| | `/adduser ID name` | grant access manually |
| | `/deluser ID` | revoke access |

## Configuration

All settings live in [`config.yaml`](config.yaml), with comments.

| Key | Default | Meaning |
|---|---|---|
| `open` | `false` | `true` lets anyone subscribe without approval |
| `min_usdc` | `0` | skip series smaller than this amount, in $ |
| `merge_minutes` | `10` | fills on the same outcome closer than this go into one message |
| `group_seconds` | `3` | how long to batch fills before sending |
| `poll_seconds` | `20` | backup poll interval (`0` disables it) |

## Deploy (24/7)

The cheapest Ubuntu VPS is enough. Docker Compose restarts the bot after
crashes and reboots.

```bash
git clone https://github.com/SergeyZheskiy/polymarket-signals-bot.git
cd polymarket-signals-bot
cp .env.example .env && nano .env
cp wallets.example.txt wallets.txt && nano wallets.txt

docker compose up -d --build
docker compose logs -f
```

To update: `git pull && docker compose up -d --build`.

## How it works

```
Polymarket live trade stream ─┐
                              ├─► filter by wallets.txt ─► dedupe ─► group into series ─► Telegram
backup poll (every 20 s) ─────┘                                                         (send, then edit)
```

| File | Role |
|---|---|
| [`notifier.py`](notifier.py) | trades → signals: stream, polling, series grouping, entry point |
| [`panel.py`](panel.py) | Telegram: subscribers, access requests, admin panel |
| [`polymarket.py`](polymarket.py) | client for Polymarket public APIs |

## Development

```bash
pip install pytest ruff
pytest        # tests run offline, without network calls
ruff check .
```

## Disclaimer

This project is not affiliated with Polymarket. Signals are informational
only and are not financial advice. Past performance of any wallet doesn't
guarantee future results.

## License

[MIT](LICENSE)
