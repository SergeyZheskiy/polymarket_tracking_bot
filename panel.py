"""Telegram-часть бота: кто получает сигналы и админская панель.

Права (в открытом режиме, open: true, сигналы получает любой, кто нажал /start):
  админы       -- id из config.yaml (admins), меняются только в коде
  пользователи -- data/notifier_users.csv, ими управляет админ из Telegram
  остальные    -- на /start получают «доступ по приглашению», а админу
                  приходит заявка с кнопками «Разрешить» / «Отклонить»

Команды всех, у кого есть доступ: /start, /stop.
Команды админа: /admin (панель), /adduser ID [имя], /deluser ID.
"""
from __future__ import annotations

import csv
import html
import threading
import time
from pathlib import Path

import requests

e = html.escape
SHOW_SUBSCRIBERS = 50          # больше в одно сообщение Telegram не влезет


class Telegram:
    """Тонкая обёртка над Bot API: только то, что нужно боту."""

    def __init__(self, token: str):
        self.url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **params):
        for _ in range(3):
            try:
                r = requests.post(self.url + method, json=params, timeout=40)
                data = r.json()
            except (requests.RequestException, ValueError) as exc:
                print(f"telegram {method}: {exc}")
                time.sleep(3)
                continue
            if data.get("ok"):
                return data.get("result")
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry:                      # лимит Telegram: ждём, сколько просят
                time.sleep(retry + 1)
                continue
            print(f"telegram {method}: {data.get('description')}")
            return None
        return None

    def send(self, chat_id: int, text: str, buttons: list | None = None):
        params = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                      disable_web_page_preview=True)
        if buttons is not None:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self.call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str, buttons: list | None = None):
        self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                  parse_mode="HTML", disable_web_page_preview=True,
                  reply_markup={"inline_keyboard": buttons or []})


class Chats:
    """Чаты, которые включили сигналы (/start). Один id на строку."""

    def __init__(self, path: Path):
        self.path = path
        self.ids: set[int] = set()
        if path.exists():
            self.ids = {int(x) for x in path.read_text().split() if x.lstrip("-").isdigit()}

    def set(self, chat_id: int, on: bool) -> None:
        (self.ids.add if on else self.ids.discard)(chat_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(str(x) for x in sorted(self.ids)) + "\n")


class Users:
    """Разрешённые пользователи. CSV id,name -- можно открыть и поправить руками."""

    def __init__(self, path: Path):
        self.path = path
        self.names: dict[int, str] = {}
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if str(row.get("id") or "").lstrip("-").isdigit():
                        self.names[int(row["id"])] = row.get("name") or ""

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "name"])
            writer.writerows(self.names.items())

    def add(self, user_id: int, name: str) -> None:
        self.names[user_id] = name or self.names.get(user_id, "")
        self.save()

    def remove(self, user_id: int) -> bool:
        if self.names.pop(user_id, None) is None:
            return False
        self.save()
        return True


def who(user: dict) -> str:
    """'Имя Фамилия (@ник)' из поля from сообщения Telegram."""
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    if user.get("username"):
        name = f"{name} (@{user['username']})" if name else f"@{user['username']}"
    return name or str(user.get("id"))


# ----------------------------------------------------------------------
class Panel:
    def __init__(self, tg: Telegram, chats: Chats, users: Users, admins: set[int],
                 open_access: bool = False):
        self.tg, self.chats, self.users, self.admins = tg, chats, users, admins
        self.open = open_access
        self.requests: dict[int, str] = {}       # id -> имя, ждут решения админа
        self.lock = threading.Lock()

    def allowed(self, user_id: int) -> bool:
        return self.open or user_id in self.admins or user_id in self.users.names

    def receivers(self) -> list[int]:
        """Кому слать сигнал: включили /start и всё ещё имеют доступ."""
        return [c for c in list(self.chats.ids) if self.allowed(c)]

    def setup_commands(self) -> None:
        self.tg.call("setMyCommands", commands=[
            {"command": "start", "description": "включить сигналы"},
            {"command": "stop", "description": "выключить сигналы"},
        ])
        for admin in self.admins:
            self.admin_menu(admin)

    def admin_menu(self, admin: int) -> None:
        """Админу -- расширенное меню. Telegram разрешает это, только если
        админ уже писал боту, поэтому повторяем на каждый его /start."""
        self.tg.call("setMyCommands", scope={"type": "chat", "chat_id": admin}, commands=[
            {"command": "admin", "description": "панель: пользователи и заявки"},
            {"command": "adduser", "description": "/adduser ID имя -- дать доступ"},
            {"command": "deluser", "description": "/deluser ID -- забрать доступ"},
            {"command": "start", "description": "включить сигналы"},
            {"command": "stop", "description": "выключить сигналы"},
        ])

    def on_update(self, update: dict) -> None:
        with self.lock:
            if update.get("callback_query"):
                self.on_button(update["callback_query"])
            elif (update.get("message") or {}).get("text"):
                self.on_message(update["message"])

    # -- сообщения -------------------------------------------------------
    def on_message(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        user = msg.get("from") or {}
        text = msg["text"].strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        args = text.split()[1:]

        if not self.allowed(chat_id):
            self.request_access(chat_id, who(user))
        elif chat_id in self.admins and command == "/admin":
            self.tg.send(chat_id, *self.panel())
        elif chat_id in self.admins and command == "/adduser":
            self.add_user(chat_id, args)
        elif chat_id in self.admins and command == "/deluser":
            self.del_user(chat_id, args)
        elif command == "/start":
            self.chats.set(chat_id, True)
            if chat_id in self.admins:
                self.admin_menu(chat_id)
            self.tg.send(chat_id, "Сигналы включены: пришлю каждую сделку отслеживаемых "
                                  "кошельков Polymarket.\n/stop — выключить.")
        elif command == "/stop":
            self.chats.set(chat_id, False)
            self.tg.send(chat_id, "Сигналы выключены. /start — включить снова.")
        elif chat_id in self.admins:
            self.tg.send(chat_id, "/admin — панель, /adduser ID имя, /deluser ID, "
                                  "/start и /stop — сигналы.")
        else:
            self.tg.send(chat_id, "Я только присылаю сигналы. /start — включить, "
                                  "/stop — выключить.")

    def request_access(self, chat_id: int, name: str) -> None:
        if chat_id in self.requests:
            self.tg.send(chat_id, "Заявка уже у администратора, жди ответа.")
            return
        self.requests[chat_id] = name
        print(f"заявка на доступ: {name}, id {chat_id}")
        self.tg.send(chat_id, "Бот работает по приглашению. Заявка отправлена "
                              "администратору — когда он одобрит, сигналы начнут приходить.")
        for admin in self.admins:
            self.tg.send(admin, f"🔔 Заявка на доступ\n{e(name)}\nid: <code>{chat_id}</code>",
                         [[{"text": "✅ Разрешить", "callback_data": f"ok:{chat_id}"},
                           {"text": "🚫 Отклонить", "callback_data": f"no:{chat_id}"}]])

    def add_user(self, admin: int, args: list[str]) -> None:
        if not args or not args[0].lstrip("-").isdigit():
            self.tg.send(admin, "Пример: /adduser 123456789 Вася\n"
                                "id человек может узнать, написав боту /start — "
                                "тогда тебе просто придёт заявка с кнопкой.")
            return
        user_id = int(args[0])
        name = " ".join(args[1:]) or self.requests.get(user_id, "")
        self.approve(user_id, name)
        self.tg.send(admin, f"✅ Доступ открыт: {e(name or str(user_id))}")

    def del_user(self, admin: int, args: list[str]) -> None:
        if not args or not args[0].lstrip("-").isdigit():
            self.tg.send(admin, "Пример: /deluser 123456789")
            return
        name = self.users.names.get(int(args[0])) or args[0]
        if self.revoke(int(args[0])):
            self.tg.send(admin, f"Доступ закрыт: {e(name)}")
        else:
            self.tg.send(admin, "Такого пользователя нет в списке.")

    # -- действия ---------------------------------------------------------
    def approve(self, user_id: int, name: str) -> None:
        self.users.add(user_id, name)
        if self.requests.pop(user_id, None) is not None:
            self.chats.set(user_id, True)        # он уже жал /start -- сразу включаем
            self.tg.send(user_id, "✅ Доступ открыт, сигналы включены.\n/stop — выключить.")
        else:
            self.tg.send(user_id, "✅ Тебе открыли доступ к сигналам. Нажми /start.")

    def revoke(self, user_id: int) -> bool:
        if not self.users.remove(user_id):
            return False
        self.chats.set(user_id, False)
        self.tg.send(user_id, "Доступ к сигналам закрыт.")
        return True

    # -- кнопки -----------------------------------------------------------
    def on_button(self, cb: dict) -> None:
        admin = (cb.get("from") or {}).get("id")
        msg = cb.get("message") or {}
        action, _, raw = str(cb.get("data") or "").partition(":")
        self.tg.call("answerCallbackQuery", callback_query_id=cb["id"])
        if admin not in self.admins or not raw.lstrip("-").isdigit():
            return
        user_id = int(raw)
        chat_id, message_id = msg["chat"]["id"], msg["message_id"]

        if action == "ok":
            name = self.requests.get(user_id, "")
            self.approve(user_id, name)
            self.tg.edit(chat_id, message_id, f"✅ Разрешено: {e(name or str(user_id))}")
        elif action == "no":
            name = self.requests.pop(user_id, "")
            self.tg.send(user_id, "Администратор отклонил заявку.")
            self.tg.edit(chat_id, message_id, f"🚫 Отклонено: {e(name or str(user_id))}")
        elif action in ("del", "pok", "pno"):         # кнопки внутри /admin
            if action == "del":
                self.revoke(user_id)
            elif action == "pok":
                self.approve(user_id, self.requests.get(user_id, ""))
            else:
                self.requests.pop(user_id, None)
                self.tg.send(user_id, "Администратор отклонил заявку.")
            self.tg.edit(chat_id, message_id, *self.panel())

    def describe(self, chat_id: int) -> str:
        """Имя и @ник подписчика -- спрашиваем у Telegram по id."""
        chat = self.tg.call("getChat", chat_id=chat_id) or {}
        return who(chat) if chat.get("first_name") or chat.get("username") else str(chat_id)

    def panel(self) -> tuple[str, list]:
        if self.open:
            subs = self.receivers()
            lines = [f"<b>Админ-панель</b>\n\nБот открытый: сигналы получает любой, "
                     f"кто нажал /start, заявок нет.\n\n<b>Подписчиков: {len(subs)}</b>"]
            for chat_id in subs[:SHOW_SUBSCRIBERS]:
                lines.append(f"🔔 {e(self.describe(chat_id))} <code>{chat_id}</code>")
            if len(subs) > SHOW_SUBSCRIBERS:
                lines.append(f"… и ещё {len(subs) - SHOW_SUBSCRIBERS}")
            return "\n".join(lines), []
        lines = [f"<b>Админ-панель</b>\n\nПользователей: {len(self.users.names)}, "
                 f"получают сигналы: {len(self.receivers())}"]
        buttons = []
        for user_id, name in self.users.names.items():
            on = "🔔" if user_id in self.chats.ids else "🔕"
            lines.append(f"{on} {e(name or '—')} <code>{user_id}</code>")
            buttons.append([{"text": f"❌ Убрать {name or user_id}"[:60],
                             "callback_data": f"del:{user_id}"}])
        if self.requests:
            lines.append("\n<b>Заявки:</b>")
        for user_id, name in self.requests.items():
            lines.append(f"⏳ {e(name)} <code>{user_id}</code>")
            buttons.append([{"text": f"✅ {name}"[:40], "callback_data": f"pok:{user_id}"},
                            {"text": "🚫", "callback_data": f"pno:{user_id}"}])
        lines.append("\nДобавить вручную: /adduser ID имя\n"
                     "Или пусть человек напишет боту /start — придёт заявка.\n"
                     "🔔 — сигналы включены, 🔕 — выключены (/stop).")
        return "\n".join(lines), buttons
