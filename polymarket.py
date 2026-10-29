"""Клиент к публичным API Polymarket и чтение config.yaml. Ключи не нужны.

  gamma-api.polymarket.com        маркеты и профили
  data-api.polymarket.com         сделки кошельков
  ws-live-data.polymarket.com     живой поток ВСЕХ сделок биржи
"""
from __future__ import annotations

import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import requests
import yaml

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
WS_LIVE = "wss://ws-live-data.polymarket.com"

RETRY_ON = {408, 425, 429, 500, 502, 503, 504}


class Api:
    """GET с ретраями + параллельные запросы."""

    def __init__(self, timeout: float = 15.0, retries: int = 4, workers: int = 8):
        self.timeout = timeout
        self.retries = retries
        self.workers = workers
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "polymarket_tracking_bot/1.0"

    def get(self, url: str, params: dict | None = None) -> Any:
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = exc
            else:
                if r.status_code not in RETRY_ON:
                    r.raise_for_status()
                    return r.json() if r.content else None
                last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                wait = float(r.headers.get("Retry-After") or 0)
                if wait:
                    time.sleep(min(wait, 15))
                    continue
            if attempt < self.retries:
                time.sleep(min(0.5 * 2 ** (attempt - 1), 8) * random.uniform(0.7, 1.3))
        raise RuntimeError(f"{url} не ответил после {self.retries} попыток: {last}")

    def many(self, func: Callable, items: list) -> list:
        """Параллельно применяет func к каждому элементу. Порядок сохраняется."""
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(func, items))


def stream_trades(on_trade: Callable[[dict], None]) -> None:
    """Живой поток ВСЕХ сделок Polymarket. Подписка одна, фильтрация -- у вызывающего.

    Сделка приходит через доли секунды после блока. Поля: proxyWallet, side,
    size, price, conditionId, asset, slug, eventSlug, title, outcome, name,
    pseudonym, timestamp, transactionHash. При обрыве -- переподключение.
    """
    import websocket  # пакет websocket-client

    subscribe = json.dumps({
        "action": "subscribe",
        "subscriptions": [{"topic": "activity", "type": "trades"}],
    })
    delay = 1.0
    while True:
        try:
            ws = websocket.create_connection(WS_LIVE, timeout=30)
            ws.send(subscribe)
            delay = 1.0
            print("поток сделок подключён")
            while True:
                raw = ws.recv()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                trade = message.get("payload")
                if isinstance(trade, dict) and trade.get("proxyWallet"):
                    on_trade(trade)
        except Exception as exc:
            print(f"поток оборвался ({type(exc).__name__}), переподключаюсь через {delay:.0f}с")
            time.sleep(delay)
            delay = min(delay * 2, 30)


class Conf:
    """config.yaml + доступ по пути 'a.b'. Пути в конфиге -- от папки конфига."""

    def __init__(self, data: dict, path: Path):
        self.data = data or {}
        self.path = path

    @classmethod
    def load(cls, path: Path) -> "Conf":
        if not path.exists():
            sys.exit(f"нет конфига {path}")
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def path_to(self, dotted: str, default: str = "") -> Path:
        value = str(self.get(dotted, default))
        return (self.path.parent / value).resolve() if value else Path()
