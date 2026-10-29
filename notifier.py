#!/usr/bin/env python3
"""Telegram-бот: как только кошелёк из списка совершает сделку -- сигнал.

    python notifier.py               настройки из config.yaml
    python notifier.py other.yaml    config.yaml + переопределения из other.yaml

В сообщении: ник со ссылкой на профиль, покупка/продажа, сумма, исход,
название маркета со ссылкой. Кто получает сигналы и админская панель --
в panel.py.

Список кошельков -- wallets.txt, правится только руками: адрес
или ссылка на профиль, по одной на строку. Бот перечитывает файл сам,
перезапуск не нужен.

Источники сделок: живой поток всех сделок Polymarket (polymarket.stream_trades)
плюс страховочный опрос истории каждого кошелька раз в poll_seconds.
Дубли между ними отсекаются по транзакции.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import yaml

from panel import Chats, Panel, Telegram, Users
from polymarket import DATA, GAMMA, Api, Conf, stream_trades

ROOT = Path(__file__).resolve().parent
ADDRESS = re.compile(r"0x[a-fA-F0-9]{40}")
USERNAME = re.compile(r"polymarket\.com/(?:[a-z]{2}/)?@([^/?#\s]+)")
SITE = "https://polymarket.com"


# ----------------------------------------------------------------------
class Watchlist:
    """Кошельки из wallets.txt. Файл перечитывается, когда его поменяли."""

    def __init__(self, path: Path, api: Api):
        self.path = path
        self.api = api
        self.mtime = 0.0
        self.resolved: dict[str, str] = {}   # ник -> адрес, чтобы не искать дважды
        self.since: dict[str, float] = {}    # адрес -> с какого момента слать сделки

    def wallets(self) -> list[str]:
        return list(self.since)

    def reload(self) -> None:
        if not self.path.exists():
            sys.exit(f"нет файла {self.path} -- скопируй wallets.example.txt в wallets.txt "
                     f"и впиши свои кошельки")
        mtime = self.path.stat().st_mtime
        if mtime == self.mtime:
            return
        self.mtime = mtime
        wallets = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            wallet = self.parse(line)
            if wallet:
                wallets.append(wallet)
            else:
                print(f"wallets.txt: не понял строку «{line}»")
        now = time.time()
        old = self.since
        self.since = {w: old.get(w, now) for w in wallets}
        print(f"кошельков под наблюдением: {len(self.since)}")

    def parse(self, line: str) -> str | None:
        """Адрес 0x..., ссылка /profile/0x... или ссылка polymarket.com/@ник."""
        match = USERNAME.search(line)
        if match:
            return self.find(match.group(1))
        match = ADDRESS.search(line)
        return match.group(0).lower() if match else None

    def find(self, username: str) -> str | None:
        key = username.lower()
        if key not in self.resolved:
            try:
                found = self.api.get(f"{GAMMA}/public-search", {
                    "q": username, "search_profiles": "true", "limit_per_type": 10,
                }) or {}
            except Exception as exc:
                print(f"не нашёл профиль {username}: {exc}")
                return None
            for p in found.get("profiles") or []:
                if (p.get("name") or "").lower() == key and p.get("proxyWallet"):
                    self.resolved[key] = p["proxyWallet"].lower()
                    break
            else:
                return None
        return self.resolved[key]

    def watch_forever(self) -> None:
        while True:
            time.sleep(5)
            try:
                self.reload()
            except Exception as exc:
                print(f"wallets.txt: {exc}")


# ----------------------------------------------------------------------
def money(x: float) -> str:
    return f"${x:,.2f}"


def market_url(trade: dict) -> str:
    event, slug = trade.get("eventSlug") or "", trade.get("slug") or ""
    if event and slug and event != slug:
        return f"{SITE}/event/{event}/{slug}"
    return f"{SITE}/event/{event or slug}"


def nickname(trade: dict) -> str:
    """Ник из профиля. Если ник не задан, Polymarket подставляет 'адрес-число'."""
    name = str(trade.get("name") or "")
    if not name or re.fullmatch(r"(0x)?[0-9a-fA-F]{40}(-\d+)?", name):
        return str(trade.get("pseudonym") or "")
    return name


def fill_market(api: Api, trade: dict) -> None:
    """Живой поток иногда присылает сделку без названия, слага и даже conditionId.

    Ищем маркет по очереди: в Gamma по conditionId, в Gamma по id токена,
    в истории сделок кошелька (там эти поля есть всегда).
    """
    if trade.get("title") and trade.get("slug") and trade.get("outcome"):
        return
    asset = str(trade.get("asset") or "")
    queries = []
    if trade.get("conditionId"):
        queries.append({"condition_ids": trade["conditionId"]})
    if asset:
        queries.append({"clob_token_ids": asset})
    for query in queries:
        try:
            markets = api.get(f"{GAMMA}/markets", query) or []
        except Exception:
            continue
        if markets:
            m = markets[0]
            events = m.get("events") or [{}]
            trade["title"] = trade.get("title") or m.get("question")
            trade["slug"] = trade.get("slug") or m.get("slug")
            trade["eventSlug"] = trade.get("eventSlug") or events[0].get("slug")
            if not trade.get("outcome"):
                try:
                    tokens = json.loads(m.get("clobTokenIds") or "[]")
                    outcomes = json.loads(m.get("outcomes") or "[]")
                    trade["outcome"] = outcomes[tokens.index(asset)]
                except (ValueError, IndexError):
                    pass
            return
    try:
        history = api.get(f"{DATA}/trades", {"user": trade["proxyWallet"], "limit": 50,
                                             "takerOnly": "false"}) or []
    except Exception:
        return
    for t in history:
        if str(t.get("asset")) == asset:
            for field in ("title", "slug", "eventSlug", "outcome", "conditionId"):
                trade[field] = trade.get(field) or t.get(field)
            return


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def format_trade(s: dict) -> str:
    """s -- серия: trade (первый fill), shares, usdc, fills, first, last (unix-время)."""
    trade = s["trade"]
    wallet = str(trade["proxyWallet"]).lower()
    nick = nickname(trade) or wallet[:10]
    buy = str(trade.get("side")).upper() == "BUY"
    avg = s["usdc"] / s["shares"] if s["shares"] else 0
    e = html.escape
    if trade.get("title") and (trade.get("slug") or trade.get("eventSlug")):
        market = f"<a href=\"{market_url(trade)}\">{e(str(trade['title']))}</a>"
    else:
        market = "маркет не удалось определить"
    text = (
        f"{'🟢 ПОКУПКА' if buy else '🔴 ПРОДАЖА'}\n"
        f"👤 <a href=\"{SITE}/profile/{wallet}\">{e(nick)}</a>\n"
        f"💵 <b>{money(s['usdc'])}</b> — {s['shares']:,.2f} шт. × {avg * 100:.1f}¢ "
        f"на «{e(str(trade.get('outcome') or '?'))}»\n"
        f"📊 {market}"
    )
    if s["fills"] > 1:
        span = max(0, int(s["last"] - s["first"]))
        took = f"{span} с" if span < 120 else f"{span // 60} мин"
        text += (f"\n🧩 {s['fills']} {plural(s['fills'], 'сделка', 'сделки', 'сделок')} "
                 f"за {took}, последняя в {time.strftime('%H:%M:%S', time.localtime(s['last']))}")
    return text


class Notifier:
    """Собирает fill'ы в серии и рассылает сигналы.

    Серия -- все сделки одного кошелька в один исход одного маркета, пока
    между ними меньше merge_minutes. На серию -- одно сообщение: первое
    приходит сразу, дальше его сумма обновляется правкой (без нового
    уведомления в телефоне).
    """

    def __init__(self, tg: Telegram, api: Api, watch: Watchlist, panel: Panel,
                 min_usdc: float, group_seconds: float, merge_minutes: float,
                 delay_seconds: float = 0):
        self.tg, self.api, self.watch, self.panel = tg, api, watch, panel
        self.min_usdc = min_usdc
        self.group_seconds = group_seconds
        self.delay_seconds = delay_seconds       # пауза перед отправкой, по умолчанию 0
        self.merge_seconds = merge_minutes * 60
        self.lock = threading.Lock()
        self.seen: dict[tuple, float] = {}      # fill -> когда увидели
        self.pending: dict[tuple, dict] = {}    # (кошелёк, исход, сторона) -> новые fill'ы
        self.series: dict[tuple, dict] = {}     # то же -> серия и её сообщения

    def on_trade(self, trade: dict) -> None:
        """Вызывается на каждую сделку биржи -- поэтому первым делом фильтр."""
        wallet = str(trade.get("proxyWallet") or "").lower()
        since = self.watch.since.get(wallet)
        ts = float(trade.get("timestamp") or 0)
        if since is None or ts < since - 5:
            return
        size, price = float(trade.get("size") or 0), float(trade.get("price") or 0)
        key = (wallet, trade.get("asset"), trade.get("side"))
        fill = key + (trade.get("transactionHash"), round(size, 4), round(price, 4))
        with self.lock:
            if fill in self.seen:
                return
            self.seen[fill] = time.time()
            batch = self.pending.setdefault(key, {
                "trade": trade, "shares": 0.0, "usdc": 0.0, "fills": 0,
                "first": ts, "last": ts, "at": time.time()})
            batch["shares"] += size
            batch["usdc"] += size * price
            batch["fills"] += 1
            batch["first"], batch["last"] = min(batch["first"], ts), max(batch["last"], ts)

    def flush_forever(self) -> None:
        while True:
            time.sleep(0.25)
            self.flush_once(time.time())

    def flush_once(self, now: float) -> None:
        """Отправляет накопленные сделки, которые ждали group_seconds + delay_seconds."""
        wait = self.group_seconds + self.delay_seconds
        with self.lock:
            ready = [k for k, b in self.pending.items() if now - b["at"] >= wait]
            batches = [(k, self.pending.pop(k)) for k in ready]
            self.seen = {f: t for f, t in self.seen.items() if now - t < 3600}
        self.series = {k: s for k, s in self.series.items()
                       if now - s["seen_at"] < self.merge_seconds}
        for key, batch in batches:
            try:
                self.publish(key, batch, now)
            except Exception as exc:
                print(f"не отправил сигнал: {exc}")

    def publish(self, key: tuple, batch: dict, now: float) -> None:
        s = self.series.get(key)
        if s is None:
            s = self.series[key] = {"trade": batch["trade"], "shares": 0.0, "usdc": 0.0,
                                    "fills": 0, "first": batch["first"],
                                    "last": batch["last"], "messages": {}}
            fill_market(self.api, s["trade"])
        s["shares"] += batch["shares"]
        s["usdc"] += batch["usdc"]
        s["fills"] += batch["fills"]
        s["first"], s["last"] = min(s["first"], batch["first"]), max(s["last"], batch["last"])
        s["seen_at"] = now
        if not s["trade"].get("title"):
            s["trade"].update({k: v for k, v in batch["trade"].items() if v})
            fill_market(self.api, s["trade"])
        if s["usdc"] < self.min_usdc:
            return
        text = format_trade(s)
        print(text.replace("\n", " | "))
        if not s["messages"]:                     # первое сообщение серии
            for chat_id in self.panel.receivers():
                sent = self.tg.send(chat_id, text)
                if sent:
                    s["messages"][chat_id] = sent["message_id"]
        else:                                     # докупка -- правим то же сообщение
            for chat_id, message_id in s["messages"].items():
                self.tg.edit(chat_id, message_id, text)


def poll_forever(api: Api, watch: Watchlist, notifier: Notifier, every: float) -> None:
    """Страховка к потоку: последние сделки каждого кошелька раз в every секунд."""
    def last_trades(wallet: str) -> list[dict]:
        try:
            return api.get(f"{DATA}/trades", {"user": wallet, "limit": 20,
                                              "takerOnly": "false"}) or []
        except Exception as exc:
            print(f"опрос {wallet[:10]}: {exc}")
            return []

    while True:
        time.sleep(every)
        for trades in api.many(last_trades, watch.wallets()):
            for trade in reversed(trades):           # от старых к новым
                notifier.on_trade(trade)


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                value = value.split(" #")[0].strip().strip('"')
                os.environ.setdefault(key.strip(), value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-сигналы о сделках кошельков")
    parser.add_argument("extra", nargs="?",
                        help="файл, переопределяющий часть настроек config.yaml")
    args = parser.parse_args()

    conf = Conf.load(ROOT / "config.yaml")
    if args.extra:                     # поверх config.yaml -- переопределения
        extra = Path(args.extra)
        if not extra.exists():
            extra = conf.path.parent / args.extra
        conf.data.update(yaml.safe_load(extra.read_text(encoding="utf-8")) or {})
    load_env()
    token_env = conf.get("token_env", "TELEGRAM_BOT_TOKEN")
    token = os.getenv(token_env, "").strip()
    if not token:
        sys.exit(f"впиши {token_env} в .env (токен от @BotFather), пример -- .env.example")

    tg = Telegram(token)
    me = tg.call("getMe")
    if not me:
        sys.exit(f"Telegram не принял токен -- проверь {token_env}")

    api = Api()
    watch = Watchlist(conf.path_to("wallets", "wallets.txt"), api)
    watch.reload()
    # админы -- id в Telegram через запятую в .env (ADMINS=...) и/или в config.yaml
    admins = {int(x) for x in os.getenv("ADMINS", "").replace(" ", "").split(",") if x}
    admins |= {int(x) for x in conf.get("admins", [])}
    if not admins:
        print("ВНИМАНИЕ: не задан ADMINS в .env -- некому одобрять заявки и открыть /admin")
    panel = Panel(tg, Chats(conf.path_to("chats_file", "data/chats.txt")),
                  Users(conf.path_to("users_file", "data/users.csv")), admins,
                  open_access=bool(conf.get("open", False)))
    panel.setup_commands()
    notifier = Notifier(tg, api, watch, panel, float(conf.get("min_usdc", 0)),
                        float(conf.get("group_seconds", 3)),
                        float(conf.get("merge_minutes", 10)),
                        float(conf.get("delay_seconds", 0)))

    threading.Thread(target=stream_trades, args=(notifier.on_trade,), daemon=True).start()
    threading.Thread(target=notifier.flush_forever, daemon=True).start()
    threading.Thread(target=watch.watch_forever, daemon=True).start()
    poll = float(conf.get("poll_seconds", 20))
    if poll > 0:
        threading.Thread(target=poll_forever, args=(api, watch, notifier, poll),
                         daemon=True).start()

    print(f"@{me['username']} запущен, получают сигналы: {len(panel.receivers())}, "
          f"задержка +{conf.get('delay_seconds', 0)} с, "
          f"{'открытый' if conf.get('open') else 'по приглашению'}. "
          f"Ctrl+C -- остановить.")

    offset = 0
    try:
        while True:
            updates = tg.call("getUpdates", offset=offset, timeout=30,
                              allowed_updates=["message", "callback_query"]) or []
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    panel.on_update(update)
                except Exception as exc:
                    print(f"ошибка команды: {exc}")
    except KeyboardInterrupt:
        print("остановлен")


if __name__ == "__main__":
    main()
