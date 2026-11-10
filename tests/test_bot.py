"""Тесты полного сценария бота на локальной имитации Telegram Bot API.

Реальный telebot работает с фейковым HTTP-сервером на 127.0.0.1,
поэтому тесты не требуют токена, интернета и живой сети Telegram.
"""
from __future__ import annotations

import atexit
import itertools
import json
import os
from datetime import datetime, timedelta
import shutil
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TMP = tempfile.mkdtemp(prefix="vds-apl-tests-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

os.environ["BOT_TOKEN"] = "123456789:" + "A" * 35
os.environ["ADMIN_IDS"] = "5992994003,1787475400"
os.environ["DATA_DIR"] = TMP
os.environ["COMMUNITY_NAME"] = "VoidSMP · Майнкилд"

import telebot  # noqa: E402
from telebot import types  # noqa: E402

import bot as botmod  # noqa: E402
import config as cfg  # noqa: E402
from storage import ApplicationStore  # noqa: E402

UPDATE_IDS = itertools.count(1)
BOT_USER = {"id": 999999, "is_bot": True, "first_name": "TestBot",
            "username": "vds_apl_test_bot"}


def _user(uid: int, name: str = "Тест", username: str = "tester") -> dict:
    return {"id": uid, "is_bot": False, "first_name": name,
            "last_name": "", "username": username}


def _message(uid: int, mid: int, text: str) -> dict:
    return {
        "message_id": mid,
        "from": _user(uid),
        "chat": {"id": uid, "type": "private", "first_name": "Тест"},
        "date": 1700000000,
        "text": text,
    }


def _message_update(uid: int, text: str, mid: int = 10) -> dict:
    return {"update_id": next(UPDATE_IDS), "message": _message(uid, mid, text)}


def _callback_update(uid: int, data: str, mid: int) -> dict:
    return {
        "update_id": next(UPDATE_IDS),
        "callback_query": {
            "id": f"cb{next(UPDATE_IDS)}",
            "from": _user(uid),
            "chat_instance": "ci-1",
            "data": data,
            "message": {
                "message_id": mid,
                "from": BOT_USER,
                "chat": {"id": uid, "type": "private"},
                "date": 1700000000,
                "text": "сообщение с меню",
            },
        },
    }


class FakeAPI:
    """Записывает все вызовы Bot API и возвращает правдоподобные ответы."""

    def __init__(self):
        self.calls: list[dict] = []
        self._next_message_id = 1000
        self.lock = threading.Lock()

    def reset(self):
        with self.lock:
            self.calls.clear()
            self._next_message_id = 1000

    def handle(self, method: str, params: dict):
        with self.lock:
            self._next_message_id += 1
            new_id = self._next_message_id
        result = True
        if method in ("sendMessage", "editMessageText"):
            mid = int(params["message_id"]) if method == "editMessageText" else new_id
            result = {
                "message_id": mid,
                "date": 1700000000,
                "chat": {"id": int(params["chat_id"]), "type": "private",
                         "first_name": "Тест"},
                "from": BOT_USER,
                "text": params.get("text", ""),
            }
        with self.lock:
            self.calls.append({"method": method, "params": params, "result": result})
        return result


def _make_handler(api: FakeAPI):
    class Handler(BaseHTTPRequestHandler):
        def _process(self):
            parsed = urlparse(self.path)
            method = parsed.path.rstrip("/").split("/")[-1]
            params = {
                k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
            }
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                body = self.rfile.read(length).decode("utf-8")
                ctype = self.headers.get("Content-Type", "")
                if "application/json" in ctype and body:
                    params.update(json.loads(body))
                elif "form" in ctype and body:
                    params.update({k: v[0] for k, v in parse_qs(body).items()})
            response = json.dumps({"ok": True, "result": api.handle(method, params)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        do_GET = _process
        do_POST = _process

        def log_message(self, *args):
            pass

    return Handler


class ErrorCollector(telebot.ExceptionHandler):
    def __init__(self):
        self.errors: list[Exception] = []

    def handle(self, exception):
        self.errors.append(exception)
        return True


class BotFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = FakeAPI()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cls.api))
        cls.port = cls.server.server_address[1]
        telebot.apihelper.API_URL = f"http://127.0.0.1:{cls.port}/bot{{0}}/{{1}}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        botmod.bot.threaded = False  # синхронная обработка для детерминизма тестов
        cls.errors = ErrorCollector()
        botmod.bot.exception_handler = cls.errors

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        telebot.apihelper.API_URL = None

    def setUp(self):
        self.api.reset()
        self.errors.errors.clear()
        with botmod._state_lock:
            botmod._states.clear()
            botmod._admin_states.clear()
        botmod.store = ApplicationStore(os.path.join(TMP, f"apps_{uuid.uuid4().hex}.json"))

    # --- helpers ---------------------------------------------------------
    def push(self, raw: dict):
        botmod.bot.process_new_updates([types.Update.de_json(raw)])
        if self.errors.errors:
            raise AssertionError(
                f"Обработчик упал: {self.errors.errors[-1]!r}"
            ) from self.errors.errors[-1]

    def say(self, uid: int, text: str):
        self.push(_message_update(uid, text))

    def click(self, uid: int, data: str, mid: int | None = None):
        self.push(_callback_update(uid, data, mid or self.last_msg_id(uid)))

    def calls_to(self, uid: int, methods=("sendMessage", "editMessageText")) -> list[dict]:
        return [
            c for c in self.api.calls
            if c["method"] in methods and str(c["params"].get("chat_id")) == str(uid)
        ]

    def last_call(self, uid: int, methods=("sendMessage", "editMessageText")) -> dict:
        calls = self.calls_to(uid, methods)
        self.assertTrue(calls, f"Нет сообщений для {uid}")
        return calls[-1]

    def last_text(self, uid: int) -> str:
        return self.last_call(uid)["params"].get("text", "")

    def last_msg_id(self, uid: int) -> int:
        return self.last_call(uid)["result"]["message_id"]

    def last_buttons(self, uid: int) -> list[dict]:
        for call in reversed(self.calls_to(uid)):
            raw = call["params"].get("reply_markup")
            if raw:
                kb = json.loads(raw) if isinstance(raw, str) else raw
                return [b for row in kb.get("inline_keyboard", []) for b in row]
        return []

    def card_calls(self) -> list[dict]:
        return [
            c for c in self.api.calls
            if c["method"] == "sendMessage"
            and "Заявка №" in c["params"].get("text", "")
            and c["params"].get("text", "").startswith("🆕")
        ]

    def fill_answers(self, uid: int, answers: list[str] | None = None):
        # Второй ответ содержит ссылку — она обязательна для «Ваш канал» у КМ
        default = ["Ответ %d" % i for i in range(1, 7)]
        default[1] = "https://youtube.com/@test_channel"
        for text in answers or default:
            self.say(uid, text)

    def submit_helper(self, uid: int) -> int:
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.click(uid, f"role:{cfg.HELPER}")
        self.fill_answers(uid)
        self.click(uid, "form:submit")
        return botmod.store.recent(1)[0]["id"]

    # --- tests -----------------------------------------------------------
    def test_admin_ids_and_roles_config(self):
        self.assertEqual(cfg.ADMIN_IDS, (5992994003, 1787475400))
        self.assertEqual(set(cfg.PROJECTS), {"voidsmp", "minkild"})
        self.assertEqual(
            set(cfg.ROLES), {cfg.CONTENT_MAKER, cfg.HELPER, cfg.WIKI_EDITOR}
        )
        for role in cfg.ROLES.values():
            self.assertTrue(role["questions"])
        self.assertEqual(cfg.ROLES[cfg.CONTENT_MAKER]["questions"], [
            "Ваш ник -",
            "Ваш канал -",
            "Сколько вы играете в майнкрафт -",
            "Ваш возраст -",
            'Ваша заявка (стримерам писать тут "стримы") -',
            "Какой уровень активности стримов и/или видеороликов вы планируете на ближайшие полгода -",
        ])

    def test_content_maker_both_projects_and_approval(self):
        uid = 111
        self.say(uid, "/start")
        self.assertIn("Официальный бот заявок", self.last_text(uid))
        self.assertIn("📝 Подать заявку", [b["text"] for b in self.last_buttons(uid)])

        self.click(uid, "menu:apply")
        role_labels = [b["text"] for b in self.last_buttons(uid)]
        self.assertTrue(any("Контент мейкер" in t for t in role_labels))
        self.assertTrue(any("Хелпер" in t for t in role_labels))
        self.assertTrue(any("Редактор Вики (Fandom)" in t for t in role_labels))

        self.click(uid, f"role:{cfg.CONTENT_MAKER}")
        self.assertIn("VoidSMP", self.last_text(uid))
        self.assertIn("Майнкилд", self.last_text(uid))
        buttons = self.last_buttons(uid)
        callbacks = [b["callback_data"] for b in buttons]
        labels = [b["text"] for b in buttons]
        self.assertIn("proj:toggle:voidsmp", callbacks)
        self.assertIn("proj:toggle:minkild", callbacks)
        # Кнопки «Оба варианта» больше нет: оба проекта отмечаются по отдельности
        self.assertNotIn("proj:both", callbacks)
        self.assertFalse(any("Оба варианта" in label for label in labels))

        # Кнопка без выбора проекта не пускает дальше
        self.click(uid, "proj:next")
        alerts = [
            c["params"] for c in self.api.calls
            if c["method"] == "answerCallbackQuery" and c["params"].get("show_alert")
        ]
        self.assertTrue(any("хотя бы один проект" in a.get("text", "") for a in alerts))

        self.click(uid, "proj:toggle:voidsmp")
        self.click(uid, "proj:toggle:minkild")
        self.assertIn("VoidSMP, Майнкилд", self.last_text(uid))
        self.click(uid, "proj:next")
        self.assertIn("Вопрос 1 из 6", self.last_text(uid))
        self.assertIn("Ваш ник", self.last_text(uid))

        self.fill_answers(uid)
        self.assertIn("Проверьте заявку", self.last_text(uid))
        self.click(uid, "form:submit")
        self.assertIn("отправлена", self.last_text(uid))

        apps = botmod.store.recent(1)
        self.assertEqual(len(apps), 1)
        app = apps[0]
        self.assertEqual(app["role"], cfg.CONTENT_MAKER)
        self.assertEqual(app["projects"], ["voidsmp", "minkild"])
        self.assertEqual(len(app["answers"]), 6)
        self.assertEqual(app["status"], "pending")

        # Заявка ушла ровно двум указанным людям и никому ещё
        cards = self.card_calls()
        self.assertEqual(len(cards), 2)
        self.assertEqual(
            {int(c["params"]["chat_id"]) for c in cards}, set(cfg.ADMIN_IDS)
        )
        for card in cards:
            buttons = [
                b["text"] for row in
                json.loads(card["params"]["reply_markup"])["inline_keyboard"]
                for b in row
            ]
            self.assertTrue(any("Принять" in b for b in buttons))
            self.assertTrue(any("Отклонить" in b for b in buttons))

        admin_id = cfg.ADMIN_IDS[0]
        self.click(admin_id, f"dec:accept:{app['id']}")
        self.assertEqual(botmod.store.get(app["id"])["status"], "approved")
        self.assertIn("одобрена", self.last_text(uid))
        # карточки обоих админов обновлены без кнопок
        for other in cfg.ADMIN_IDS:
            edited = [
                c for c in self.calls_to(other, methods=("editMessageText",))
                if "Статус" in c["params"].get("text", "")
            ]
            self.assertTrue(edited, f"Карточка админа {other} не обновлена")
            self.assertEqual(
                json.loads(edited[-1]["params"]["reply_markup"])["inline_keyboard"], []
            )

    def test_helper_and_wiki_forms(self):
        for index, role_key in enumerate((cfg.HELPER, cfg.WIKI_EDITOR)):
            with self.subTest(role=role_key):
                self.setUp()
                uid = 200 + index
                self.say(uid, "/start")
                self.click(uid, "menu:apply")
                self.click(uid, f"role:{role_key}")
                self.assertIn("Вопрос 1 из 6", self.last_text(uid))
                self.assertNotIn("Выберите проект", self.last_text(uid))
                self.fill_answers(uid)
                self.click(uid, "form:submit")
                app = botmod.store.recent(1)[0]
                self.assertEqual(app["role"], role_key)
                self.assertEqual(app["projects"], [])
                self.assertEqual(
                    {int(c["params"]["chat_id"]) for c in self.card_calls()},
                    set(cfg.ADMIN_IDS),
                )

    def test_toggle_single_project_and_back_navigation(self):
        uid = 333
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.click(uid, f"role:{cfg.CONTENT_MAKER}")
        self.click(uid, "proj:toggle:minkild")
        self.assertIn("Майнкилд", self.last_text(uid))
        self.assertNotIn("VoidSMP, Майнкилд", self.last_text(uid))
        self.click(uid, "proj:next")
        self.assertIn("Вопрос 1 из 6", self.last_text(uid))
        self.say(uid, "Мой ник")
        self.assertIn("Вопрос 2 из 6", self.last_text(uid))
        self.click(uid, "form:back")
        self.assertIn("Вопрос 1 из 6", self.last_text(uid))
        self.assertIn("Мой ник", self.last_text(uid))
        self.click(uid, "form:cancel")
        self.assertIn("Официальный бот заявок", self.last_text(uid))

    def test_non_admin_cannot_decide(self):
        uid = 444
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.click(uid, f"role:{cfg.HELPER}")
        self.fill_answers(uid)
        self.click(uid, "form:submit")
        app_id = botmod.store.recent(1)[0]["id"]
        card_mid = self.card_calls()[0]["result"]["message_id"]
        self.click(777777, f"dec:accept:{app_id}", mid=card_mid)
        self.assertEqual(botmod.store.get(app_id)["status"], "pending")
        alerts = [
            c["params"] for c in self.api.calls
            if c["method"] == "answerCallbackQuery" and c["params"].get("show_alert")
        ]
        self.assertTrue(any("только администраторам" in a.get("text", "") for a in alerts))

    def test_reject_with_reason(self):
        uid = 555
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.click(uid, f"role:{cfg.WIKI_EDITOR}")
        self.fill_answers(uid)
        self.click(uid, "form:submit")
        app_id = botmod.store.recent(1)[0]["id"]
        admin_id = cfg.ADMIN_IDS[1]
        self.click(admin_id, f"dec:reject:{app_id}")
        self.assertIn("Причина отказа", self.last_text(admin_id))
        self.say(admin_id, "Мало опыта в вики-разметке")
        self.assertEqual(botmod.store.get(app_id)["status"], "rejected")
        self.assertIn("отклонена", self.last_text(uid))
        self.assertIn("Мало опыта в вики-разметке", self.last_text(uid))

    def test_html_escaped_in_admin_card(self):
        uid = 666
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.click(uid, f"role:{cfg.HELPER}")
        self.say(uid, "<script>alert('x')</script> & <b>жирный</b>")
        self.fill_answers(uid, ["Ответ %d" % i for i in range(2, 7)])
        self.click(uid, "form:submit")
        card_text = self.card_calls()[0]["params"]["text"]
        self.assertIn("&lt;script&gt;", card_text)
        self.assertNotIn("<script>", card_text)
        self.assertIn("&amp;", card_text)

    def test_no_second_pending_application(self):
        uid = 888
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.click(uid, f"role:{cfg.HELPER}")
        self.fill_answers(uid)
        self.click(uid, "form:submit")
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.assertIn("уже есть заявка", self.last_text(uid))


    def test_link_required_for_content_maker_channel(self):
        uid = 1010
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.click(uid, f"role:{cfg.CONTENT_MAKER}")
        self.click(uid, "proj:toggle:voidsmp")
        self.click(uid, "proj:next")
        self.say(uid, "Darkz")
        self.assertEqual(botmod.get_state(uid)["step"], 1)

        self.say(uid, "просто название канала без адреса")
        self.assertEqual(botmod.get_state(uid)["step"], 1)  # ответ не принят
        errors = [
            c["params"].get("text", "") for c in self.api.calls
            if c["method"] == "sendMessage"
            and str(c["params"].get("chat_id")) == str(uid)
        ]
        self.assertTrue(any("Нужна ссылка" in t for t in errors))

        self.say(uid, "https://youtube.com/@darkz")
        self.assertEqual(botmod.get_state(uid)["step"], 2)
        self.assertIn("Вопрос 3 из 6", self.last_text(uid))
        self.say(uid, "5 лет")
        self.say(uid, "16")
        self.say(uid, "стримы")
        self.say(uid, "3 стрима в неделю")
        self.assertIn("Проверьте заявку", self.last_text(uid))
        self.click(uid, "form:submit")
        app = botmod.store.recent(1)[0]
        self.assertIn("youtube.com", app["answers"][1]["a"])

    def test_wiki_profile_link_optional(self):
        cases = [
            ("нет", True),
            ("мой профиль без адреса", False),
            ("https://voidsmp.fandom.com/wiki/User:Darkz", True),
        ]
        for index, (answer, should_pass) in enumerate(cases):
            with self.subTest(answer=answer):
                self.setUp()
                uid = 1100 + index
                self.say(uid, "/start")
                self.click(uid, "menu:apply")
                self.click(uid, f"role:{cfg.WIKI_EDITOR}")
                self.say(uid, "Тестер")
                self.assertEqual(botmod.get_state(uid)["step"], 1)
                self.say(uid, answer)
                self.assertEqual(
                    botmod.get_state(uid)["step"], 2 if should_pass else 1
                )

    def test_cooldown_one_hour(self):
        self.assertEqual(cfg.APPLICATION_COOLDOWN_SEC, 3600)
        uid = 1212
        app_id = self.submit_helper(uid)
        self.click(cfg.ADMIN_IDS[0], f"dec:accept:{app_id}")  # снимаем «на рассмотрении»

        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.assertIn("не чаще", self.last_text(uid))
        self.assertIn("мин.", self.last_text(uid))
        self.assertNotIn("Выберите направление", self.last_text(uid))

        # Имитируем, что с момента отправки прошёл час с лишним
        old = (datetime.now(botmod._tz()) - timedelta(seconds=3700)).isoformat(
            timespec="seconds"
        )
        botmod.store.update(app_id, created_at=old)
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.assertIn("Выберите направление", self.last_text(uid))

    def test_cooldown_checked_on_submit(self):
        uid = 1313
        self.say(uid, "/start")
        self.click(uid, "menu:apply")
        self.click(uid, f"role:{cfg.HELPER}")
        self.fill_answers(uid)
        self.assertIn("Проверьте заявку", self.last_text(uid))
        # Пока пользователь заполнял анкету, от него уже ушла другая заявка
        botmod.store.create(
            user_id=uid, role=cfg.HELPER, status="approved",
            projects=[], answers=[], created_at=botmod.now_iso(),
        )
        self.click(uid, "form:submit")
        self.assertIn("не чаще", self.last_text(uid))
        self.assertEqual(len(botmod.store.by_user(uid)), 1)

    def test_my_shows_cooldown(self):
        uid = 1414
        self.submit_helper(uid)
        self.say(uid, "/my")
        self.assertIn("Следующую заявку", self.last_text(uid))


class ValidationTests(unittest.TestCase):
    def test_has_link(self):
        for text in [
            "https://youtube.com/@darkz", "http://t.me/void", "www.example.com/c",
            "t.me/voidsmp", "youtube.com/watch?v=1", "канал.рф/void",
            "https://vk.com/id1", "discord.gg/abc",
        ]:
            with self.subTest(text=text):
                self.assertTrue(botmod.has_link(text))
        for text in [
            "нет", "просто название канала", "стримы", "1.20",
            "мой канал 5 раз", "https://x",
        ]:
            with self.subTest(text=text):
                self.assertFalse(botmod.has_link(text))

    def test_link_error_rules(self):
        self.assertIsNotNone(botmod.link_error(cfg.CONTENT_MAKER, 1, "нет канала"))
        self.assertIsNone(botmod.link_error(cfg.CONTENT_MAKER, 1, "t.me/voidsmp"))
        self.assertIsNone(botmod.link_error(cfg.CONTENT_MAKER, 0, "без ссылки"))
        self.assertIsNone(botmod.link_error(cfg.WIKI_EDITOR, 1, "нет"))
        self.assertIsNotNone(botmod.link_error(cfg.WIKI_EDITOR, 1, "просто текст"))
        self.assertIsNone(botmod.link_error(cfg.HELPER, 1, "просто текст"))


class StoreTests(unittest.TestCase):
    def test_create_update_and_persist(self):
        path = os.path.join(TMP, f"store_{uuid.uuid4().hex}.json")
        store = ApplicationStore(path)
        app = store.create(user_id=42, role="helper", projects=[],
                           answers=[{"q": "Q", "a": "A"}], created_at="2026-11-10T12:00:00+03:00")
        self.assertEqual(app["id"], 1)
        self.assertEqual(store.pending_for_user(42)[0]["id"], 1)
        store.record_admin_message(1, 5992994003, 555)
        updated = store.update(1, status="approved", decided_by=5992994003)
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(len(store.by_user(42)), 1)
        self.assertEqual(store.recent(1)[0]["id"], 1)

        reopened = ApplicationStore(path)
        self.assertEqual(reopened.get(1)["status"], "approved")
        self.assertEqual(reopened.get(1)["admin_messages"]["5992994003"], 555)
        self.assertEqual(reopened.create(user_id=43, role="helper")["id"], 2)
        self.assertFalse(reopened.pending_for_user(42))


if __name__ == "__main__":
    unittest.main(verbosity=2)
