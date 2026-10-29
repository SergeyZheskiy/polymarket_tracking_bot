"""Тесты доступа: заявки, одобрение кнопкой, открытый режим, чужие кнопки."""
from panel import Chats, Panel, Users

ADMIN = 1


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": len(self.sent)}

    def edit(self, *args, **kwargs):
        pass

    def call(self, *args, **kwargs):
        pass


def make(tmp_path, open_access=False):
    tg = FakeTelegram()
    panel = Panel(tg, Chats(tmp_path / "chats.txt"), Users(tmp_path / "users.csv"),
                  {ADMIN}, open_access=open_access)
    return panel, tg


def message(chat_id, text):
    return {"message": {"chat": {"id": chat_id}, "from": {"id": chat_id, "first_name": "Bob"},
                        "text": text}}


def button(from_id, data):
    return {"callback_query": {"id": "q", "from": {"id": from_id}, "data": data,
                               "message": {"chat": {"id": from_id}, "message_id": 7}}}


def test_stranger_gets_request_and_admin_approves(tmp_path):
    panel, tg = make(tmp_path)
    panel.on_update(message(555, "/start"))
    assert panel.receivers() == []
    request = [s for s in tg.sent if s[0] == ADMIN][-1]
    assert request[2][0][0]["callback_data"] == "ok:555"

    panel.on_update(button(ADMIN, "ok:555"))
    assert panel.receivers() == [555]
    assert "555" in (tmp_path / "users.csv").read_text()


def test_only_admin_can_press_buttons(tmp_path):
    panel, _ = make(tmp_path)
    panel.on_update(message(555, "/start"))
    panel.on_update(button(555, "ok:555"))
    assert panel.receivers() == []


def test_removed_user_stops_receiving(tmp_path):
    panel, _ = make(tmp_path)
    panel.on_update(message(555, "/start"))
    panel.on_update(button(ADMIN, "ok:555"))
    panel.on_update(message(ADMIN, "/deluser 555"))
    assert panel.receivers() == []


def test_open_mode_lets_anyone_in(tmp_path):
    panel, _ = make(tmp_path, open_access=True)
    panel.on_update(message(555, "/start"))
    assert panel.receivers() == [555]


def test_stop_unsubscribes(tmp_path):
    panel, _ = make(tmp_path, open_access=True)
    panel.on_update(message(555, "/start"))
    panel.on_update(message(555, "/stop"))
    assert panel.receivers() == []


def test_open_mode_admin_sees_subscribers(tmp_path):
    panel, tg = make(tmp_path, open_access=True)
    tg.call = lambda method, **kw: {"first_name": "Bob", "username": "bob"} \
        if method == "getChat" and kw["chat_id"] == 555 else None
    panel.on_update(message(555, "/start"))
    panel.on_update(message(ADMIN, "/admin"))
    text = tg.sent[-1][1]
    assert "Подписчиков: 1" in text and "Bob (@bob)" in text and "555" in text
