"""Официальный Telegram-бот заявок VDS-Apl.

Направления: Контент мейкер (VoidSMP и/или Майнкилд), Хелпер,
Редактор Вики (Fandom). Заявки уходят ТОЛЬКО администраторам из config.ADMIN_IDS.

Запуск на bothost.ru: `python bot.py` (см. README.md и bothost.json).
"""
from __future__ import annotations

import html
import logging
import math
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import telebot
from telebot import types

import config as cfg
from storage import ApplicationStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("vds-apl")

# Плейсхолдер нужен только затем, чтобы модуль можно было импортировать в тестах
# без реального токена. В main() наличие настоящего токена проверяется жёстко.
_PLACEHOLDER_TOKEN = "123456789:" + "A" * 35
bot = telebot.TeleBot(
    cfg.BOT_TOKEN or _PLACEHOLDER_TOKEN,
    parse_mode="HTML",
    skip_pending=True,
    num_threads=4,
)
store = ApplicationStore(cfg.DB_PATH)

# Состояния анкет пользователей и отдельно — ожидание текста от админов.
_states: dict[int, dict] = {}
_admin_states: dict[int, dict] = {}
_state_lock = threading.RLock()


# =========================================================================
# Утилиты
# =========================================================================
def esc(value) -> str:
    """Экранирование пользовательского текста для parse_mode=HTML."""
    return html.escape(str(value if value is not None else ""), quote=False)


def _tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(cfg.BOT_TIMEZONE)
    except Exception:
        return timezone(timedelta(hours=3))


def now_iso() -> str:
    return datetime.now(_tz()).isoformat(timespec="seconds")


def fmt_dt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_tz()).strftime("%d.%m.%Y %H:%M %Z")
    except (ValueError, TypeError):
        return str(value)


# Проверяем НАЛИЧИЕ ссылки в тексте (не доступность сайта — бот не делает
# сетевых запросов). Для необязательной ссылки (профиль Fandom) подходит любая
# ссылка, для канала контент-мейкера — только YouTube / Twitch.
_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+\.[^\s]*"
    r"|(?:\b[\w-]+\.)+[a-zа-яё]{2,}(?:/[^\s]*)?",
    re.IGNORECASE,
)


def _link_hosts(text: str) -> list[tuple[str, str]]:
    """Достаёт из текста пары (хост, путь) для всех похожих на URL токенов."""
    hosts = []
    for token in re.findall(r"\S+", text or ""):
        candidate = token.strip(".,;:!?()[]{}<>\"'«»")
        if not candidate:
            continue
        if "://" not in candidate:
            candidate = "https://" + candidate
        try:
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower()
        except ValueError:
            continue
        if host and "." in host:
            hosts.append((host, parsed.path or ""))
    return hosts


def _host_allowed(host: str, allowed: tuple[str, ...] | list[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def has_link(text: str, allowed_domains=None, require_path: bool = False) -> bool:
    """Есть ли в тексте ссылка.

    allowed_domains=None — любая ссылка; иначе только указанные домены
    (и их поддомены). require_path требует путь к каналу/видео, а не голый домен.
    """
    if allowed_domains is None:
        return bool(_URL_RE.search(text or ""))
    for host, path in _link_hosts(text):
        if _host_allowed(host, allowed_domains):
            if not require_path or path.strip("/"):
                return True
    return False


def link_mode(role_key: str, question_index: int) -> str | None:
    if question_index in cfg.LINK_REQUIRED.get(role_key, []):
        return "required"
    if question_index in cfg.LINK_OPTIONAL.get(role_key, []):
        return "optional"
    return None


def link_error(role_key: str, question_index: int, text: str) -> str | None:
    """Возвращает текст ошибки, если ссылка обязательна/ожидается, но её нет."""
    mode = link_mode(role_key, question_index)
    if not mode:
        return None
    cleaned = text.strip().lower().rstrip(".! ")
    if mode == "optional":
        if cleaned in cfg.NO_LINK_ANSWERS or has_link(text):
            return None
        return (
            "📎 Похоже, в ответе нет ссылки. Пришлите ссылку на профиль "
            "(https://..., fandom.com/...) или напишите «нет»."
        )
    # Обязательная ссылка на канал: только YouTube и Twitch, с путём к каналу.
    if has_link(
        text,
        allowed_domains=cfg.LINK_ALLOWED_DOMAINS,
        require_path=True,
    ):
        return None
    return (
        "🔗 <b>Нужна ссылка на канал: только YouTube или Twitch</b>.\n\n"
        "Например:\n"
        "<code>https://youtube.com/@ваш_канал</code>\n"
        "<code>https://youtu.be/...</code>\n"
        "<code>https://twitch.tv/ваш_канал</code>\n\n"
        "Ссылки на Telegram, VK, TikTok и другие площадки не принимаются. "
        "Отправьте ответ ещё раз."
    )


def last_submission_time(uid: int):
    """Время последней отправленной заявки пользователя (или None)."""
    latest = None
    for app in store.by_user(uid):
        raw = app.get("created_at")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if latest is None or dt > latest:
            latest = dt
    return latest


def cooldown_left(uid: int) -> int:
    """Сколько секунд осталось до следующей заявки (0 = можно отправлять)."""
    limit = cfg.APPLICATION_COOLDOWN_SEC
    if limit <= 0:
        return 0
    last = last_submission_time(uid)
    if not last:
        return 0
    elapsed = (datetime.now(_tz()) - last.astimezone(_tz())).total_seconds()
    left = limit - elapsed
    return min(limit, int(left)) if left > 0 else 0


def cooldown_text(left: int) -> str:
    minutes = max(1, math.ceil(left / 60))
    until = (datetime.now(_tz()) + timedelta(seconds=left)).strftime("%H:%M %Z")
    return (
        "⏳ <b>Отправлять заявки можно не чаще одного раза в час.</b>\n\n"
        f"До следующей заявки осталось ≈{minutes} мин. "
        f"(можно после {esc(until)}).\n\n"
        "Это защита от спама. 📂 /my — статус предыдущей заявки."
    )


def show_blocked(uid: int, st: dict, text: str) -> None:
    show_screen(uid, st, text, markup([
        [("📂 Мои заявки", "menu:my")],
        [("🏠 Главное меню", "menu:main")],
    ]), "menu")


def user_info(user) -> dict[str, str]:
    full_name = getattr(user, "full_name", "") or " ".join(
        filter(None, [getattr(user, "first_name", ""), getattr(user, "last_name", "")])
    )
    return {
        "username": (getattr(user, "username", "") or "").lstrip("@"),
        "full_name": full_name or "Без имени",
    }


def project_labels(keys: list[str]) -> str:
    return esc(", ".join(cfg.PROJECTS.get(k, k) for k in keys)) or "—"


def markup(rows: list[list[tuple[str, str]]]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        kb.row(*[types.InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return kb


def get_state(uid: int) -> dict | None:
    with _state_lock:
        st = _states.get(uid)
        return dict(st) if st else None


def set_state(uid: int, state: dict) -> None:
    with _state_lock:
        _states[uid] = state


def clear_state(uid: int) -> None:
    with _state_lock:
        _states.pop(uid, None)


def get_admin_state(uid: int) -> dict | None:
    with _state_lock:
        st = _admin_states.get(uid)
        return dict(st) if st else None


def set_admin_state(uid: int, state: dict) -> None:
    with _state_lock:
        _admin_states[uid] = state


def clear_admin_state(uid: int) -> None:
    with _state_lock:
        _admin_states.pop(uid, None)


def is_admin(uid: int) -> bool:
    return uid in cfg.ADMIN_IDS


def _edit_or_send(chat_id: int, message_id: int | None, text: str, kb) -> int:
    """Одно «живое» сообщение-меню на пользователя."""
    if message_id:
        try:
            bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            return message_id
        except Exception as exc:  # noqa: BLE001 — сетевые/API ошибки не должны ломать сценарий
            if "message is not modified" in str(exc).lower():
                return message_id
            log.warning("Не удалось отредактировать сообщение %s: %s", message_id, exc)
    msg = bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
    return msg.message_id


def show_screen(uid: int, st: dict, text: str, kb, flow: str) -> None:
    st["flow"] = flow
    st["message_id"] = _edit_or_send(uid, st.get("message_id"), text, kb)
    set_state(uid, st)


# =========================================================================
# Тексты и клавиатуры
# =========================================================================
def main_menu_text() -> str:
    return (
        "👋 <b>Официальный бот заявок</b> | " + esc(cfg.COMMUNITY_NAME) + "\n\n"
        "Набор в команду проектов открыт. Доступные направления:\n"
        "🎬 <b>Контент мейкер</b> — VoidSMP и/или Майнкилд\n"
        "🛠 <b>Хелпер</b> — помощь игрокам и порядок на сервере\n"
        "📚 <b>Редактор Вики (Fandom)</b> — статьи, лор и оформление\n\n"
        "Заявку рассматривают только два администратора. "
        "Решение придёт сюда же, в этот чат.\n\n"
        "Нажмите «Подать заявку», чтобы начать."
    )


def main_menu_kb():
    return markup([
        [("📝 Подать заявку", "menu:apply")],
        [("ℹ️ О проектах", "menu:about"), ("❓ Как это работает", "menu:help")],
        [("📂 Мои заявки", "menu:my")],
    ])


def show_main(uid: int, st: dict) -> None:
    show_screen(uid, st, main_menu_text(), main_menu_kb(), "menu")


def show_roles(uid: int, st: dict) -> None:
    st.pop("role", None)
    st.pop("projects", None)
    st.pop("answers", None)
    st.pop("step", None)
    lines = ["📝 <b>Выберите направление</b>", ""]
    for role in cfg.ROLES.values():
        lines.append(f"{esc(role['label'])} — {esc(role['about'])}")
    lines += ["", "<i>После выбора ответьте на несколько вопросов — это займёт 2–3 минуты.</i>"]
    kb = markup([
        [("🎬 Контент мейкер", f"role:{cfg.CONTENT_MAKER}")],
        [("🛠 Хелпер", f"role:{cfg.HELPER}")],
        [("📚 Редактор Вики (Fandom)", f"role:{cfg.WIKI_EDITOR}")],
        [("◀️ Назад", "menu:main")],
    ])
    show_screen(uid, st, "\n".join(lines), kb, "roles")


def show_projects(uid: int, st: dict, selected: list[str]) -> None:
    chosen = project_labels(selected) if selected else "ничего"
    text = (
        "🎬 <b>Контент мейкер</b>\n\n"
        "Отметьте проект(ы), для которых хотите создавать контент: "
        "VoidSMP и Майнкилд можно выбрать вместе, нажав на оба.\n\n"
        f"<b>Выбрано:</b> {chosen}\n\n"
        "<i>Нажмите на проект, чтобы отметить/снять его, затем «Продолжить».</i>"
    )
    rows = []
    for key, label in cfg.PROJECTS.items():
        mark = "✅ " if key in selected else "▫️ "
        rows.append([(mark + label, f"proj:toggle:{key}")])
    rows += [
        [("▶️ Продолжить", "proj:next")],
        [("◀️ К направлениям", "nav:roles")],
    ]
    st["projects"] = list(selected)
    show_screen(uid, st, text, markup(rows), "projects")


def show_help(uid: int, st: dict) -> None:
    text = (
        "❓ <b>Как подать заявку</b>\n\n"
        "1️⃣ Нажмите «Подать заявку» и выберите направление.\n"
        "2️⃣ Для <b>Контент мейкера</b> отметьте VoidSMP и/или Майнкилд (можно оба).\n"
        "3️⃣ Ответьте на вопросы анкеты текстовыми сообщениями.\n"
        "4️⃣ Проверьте ответы и нажмите «Отправить».\n"
        "5️⃣ Заявка уйдёт двум администраторам; решение придёт в этот чат.\n\n"
        "🔗 В вопросе про канал нужна ссылка только на <b>YouTube или Twitch</b>.\n"
        "⏳ Отправлять заявки можно <b>не чаще раза в час</b> — защита от спама.\n\n"
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/my — мои заявки и их статусы\n"
        "/cancel — отменить заполнение\n"
        "/help — эта справка\n\n"
        "<i>Пока заявка на рассмотрении, подать вторую нельзя.</i>"
    )
    show_screen(uid, st, text, markup([[("◀️ В меню", "menu:main")]]), "menu")


def show_about(uid: int, st: dict) -> None:
    text = (
        f"ℹ️ <b>О проектах {esc(cfg.COMMUNITY_NAME)}</b>\n\n"
        "Это официальный бот для набора в команду. Мы ищем:\n\n"
        "🎬 <b>Контент мейкеров</b> — видео, стримы и посты по VoidSMP и Майнкилд "
        "(можно выбрать оба проекта).\n"
        "🛠 <b>Хелперов</b> — помощь игрокам, ответы на вопросы, порядок на сервере.\n"
        "📚 <b>Редакторов Вики</b> — ведение Fandom-вики: лор, персонажи, события, оформление.\n\n"
        "<i>Персональные данные не публикуются: анкета видна только администраторам.</i>"
    )
    show_screen(uid, st, text, markup([[("◀️ В меню", "menu:main")]]), "menu")


def show_my(uid: int, st: dict) -> None:
    apps = store.by_user(uid)
    if not apps:
        text = "📂 <b>Мои заявки</b>\n\nУ вас пока нет заявок. Нажмите «Подать заявку»."
    else:
        lines = ["📂 <b>Мои заявки</b>", ""]
        for app in sorted(apps, key=lambda a: a["id"], reverse=True)[:10]:
            role = cfg.ROLES.get(app["role"], {}).get("label", app["role"])
            lines.append(
                f"№{app['id']:04d} · {esc(role)} · "
                f"{esc(cfg.STATUS_LABELS.get(app['status'], app['status']))} · "
                f"{esc(fmt_dt(app.get('created_at')))}"
            )
        left = cooldown_left(uid)
        if left:
            lines += ["", f"⏳ Следующую заявку можно отправить примерно через "
                          f"{max(1, math.ceil(left / 60))} мин."]
        lines += ["", "<i>Решение по заявке приходит в этот чат автоматически.</i>"]
        text = "\n".join(lines)
    show_screen(uid, st, text, markup([
        [("📝 Подать заявку", "menu:apply")],
        [("◀️ В меню", "menu:main")],
    ]), "menu")


# =========================================================================
# Анкета
# =========================================================================
def start_form(uid: int, st: dict, role_key: str, projects: list[str]) -> None:
    role = cfg.ROLES[role_key]
    st.update({
        "role": role_key,
        "projects": list(projects),
        "step": 0,
        "answers": [None] * len(role["questions"]),
    })
    set_state(uid, st)
    show_question(uid, st)


def show_question(uid: int, st: dict) -> None:
    role = cfg.ROLES[st["role"]]
    idx = st["step"]
    total = len(role["questions"])
    lines = [f"📝 <b>Заявка: {esc(role['title'])}</b>"]
    if st.get("projects"):
        lines.append(f"<b>Проекты:</b> {project_labels(st['projects'])}")
    lines += ["", f"<b>Вопрос {idx + 1} из {total}</b>", esc(role["questions"][idx]), ""]
    previous = st["answers"][idx]
    if previous:
        lines += [f"<i>Ваш предыдущий ответ:</i> {esc(previous[:200])}", ""]
    lines.append(
        f"<i>Ответьте одним текстовым сообщением (до {cfg.MAX_ANSWER_LEN} символов). "
        "/cancel — отменить.</i>"
    )
    kb = markup([
        [("◀️ Назад", "form:back"), ("❌ Отмена", "form:cancel")],
    ])
    show_screen(uid, st, "\n".join(lines), kb, "form")


def summary(st: dict) -> str:
    role = cfg.ROLES[st["role"]]
    lines = [f"<b>Направление:</b> {esc(role['label'])}"]
    if st.get("projects"):
        lines.append(f"<b>Проекты:</b> {project_labels(st['projects'])}")
    lines.append("")
    for idx, question in enumerate(role["questions"]):
        answer = st["answers"][idx] or "—"
        lines += [f"<b>{idx + 1}. {esc(question)}</b>", esc(answer), ""]
    return "\n".join(lines).strip()


def show_preview(uid: int, st: dict) -> None:
    text = (
        "🔍 <b>Проверьте заявку перед отправкой</b>\n\n"
        + summary(st)
        + "\n\n<i>Если всё верно — отправляйте. Заявку увидят только администраторы.</i>"
    )
    kb = markup([
        [("📨 Отправить заявку", "form:submit")],
        [("✏️ Заполнить заново", "form:restart"), ("◀️ Последний вопрос", "form:back")],
        [("❌ Отмена", "form:cancel")],
    ])
    show_screen(uid, st, text, kb, "preview")


def handle_form_answer(message, st: dict, text: str) -> None:
    uid = message.from_user.id
    if len(text) < cfg.MIN_ANSWER_LEN:
        bot.send_message(uid, "Ответ слишком короткий — напишите чуть подробнее 🙂")
        return
    if len(text) > cfg.MAX_ANSWER_LEN:
        bot.send_message(
            uid,
            f"Слишком длинный ответ. Сократите до {cfg.MAX_ANSWER_LEN} символов "
            "и отправьте ещё раз.",
        )
        return
    link_problem = link_error(st["role"], st["step"], text)
    if link_problem:
        bot.send_message(uid, link_problem)
        return
    total = len(cfg.ROLES[st["role"]]["questions"])
    st["answers"][st["step"]] = text
    st["step"] += 1
    if st["step"] >= total:
        show_preview(uid, st)
    else:
        show_question(uid, st)


def submit_application(uid: int, st: dict) -> None:
    if store.pending_for_user(uid):
        show_screen(
            uid, st,
            "⏳ У вас уже есть заявка на рассмотрении. Дождитесь решения — "
            "оно придёт в этот чат.\n\n📂 /my — статус заявки",
            markup([[("◀️ В меню", "menu:main")]]),
            "menu",
        )
        return
    left = cooldown_left(uid)
    if left:
        show_blocked(uid, st, cooldown_text(left))
        return
    role = cfg.ROLES[st["role"]]
    person = st.get("user") or {"username": "", "full_name": "Без имени"}
    app = store.create(
        user_id=uid,
        username=person.get("username", ""),
        full_name=person.get("full_name", "Без имени"),
        role=st["role"],
        projects=list(st.get("projects") or []),
        answers=[
            {"q": q, "a": st["answers"][i] or ""}
            for i, q in enumerate(role["questions"])
        ],
        created_at=now_iso(),
    )
    notify_admins(app)
    number = f"№{app['id']:04d}"
    text = (
        f"✅ <b>Заявка {number} отправлена!</b>\n\n"
        f"Направление: {esc(role['label'])}\n"
        + (f"Проекты: {project_labels(app['projects'])}\n" if app["projects"] else "")
        + "\nЗаявку рассматривают два администратора. Как только будет решение, "
        "бот напишет сюда.\n\n📂 /my — статус заявки"
    )
    # Редактируем то же сообщение в подтверждение — меню остаётся «живым».
    message_id = st.get("message_id")
    st.clear()
    st.update({"flow": "menu", "message_id": message_id, "user": person})
    show_screen(uid, st, text, markup([
        [("🏠 Главное меню", "menu:main")],
        [("📂 Мои заявки", "menu:my")],
    ]), "menu")
    log.info("Новая заявка %s от %s (%s)", number, uid, app["role"])


# =========================================================================
# Рассылка администраторам и решения
# =========================================================================
def format_application(app: dict) -> str:
    role = cfg.ROLES.get(app["role"], {"label": app["role"]})
    lines = [f"🆕 <b>Заявка №{app['id']:04d}</b>", ""]
    lines.append(f"<b>Направление:</b> {esc(role['label'])}")
    if app.get("projects"):
        lines.append(f"<b>Проекты:</b> {project_labels(app['projects'])}")
    username = f" (@{esc(app['username'])})" if app.get("username") else ""
    lines += [
        f"<b>От:</b> <a href=\"tg://user?id={app['user_id']}\">"
        f"{esc(app.get('full_name'))}</a>{username} · <code>{app['user_id']}</code>",
        f"<b>Подана:</b> {esc(fmt_dt(app.get('created_at')))}",
        "",
    ]
    for idx, item in enumerate(app.get("answers", []), 1):
        answer = str(item.get("a", ""))
        if len(answer) > cfg.CARD_ANSWER_LIMIT:
            answer = answer[: cfg.CARD_ANSWER_LIMIT] + "…"
        lines += [f"<b>{idx}. {esc(item.get('q', ''))}</b>", esc(answer), ""]
    status = cfg.STATUS_LABELS.get(app.get("status"), app.get("status", ""))
    lines.append(f"<b>Статус:</b> {esc(status)}")
    if app.get("decided_at"):
        who = app.get("decided_by_name") or app.get("decided_by") or "администратор"
        lines.append(f"<b>Решение:</b> {esc(who)} · {esc(fmt_dt(app['decided_at']))}")
    if app.get("reason"):
        lines.append(f"<b>Причина отказа:</b> {esc(app['reason'])}")
    return "\n".join(lines)


def admin_keyboard(app_id: int):
    return markup([
        [("✅ Принять", f"dec:accept:{app_id}"), ("❌ Отклонить", f"dec:reject:{app_id}")],
        [("💬 Написать заявителю", f"dec:reply:{app_id}")],
    ])


def notify_admins(app: dict) -> None:
    """Заявка уходит ТОЛЬКО в лички ADMIN_IDS (два человека)."""
    text = format_application(app)
    kb = admin_keyboard(app["id"])
    for admin_id in cfg.ADMIN_IDS:
        try:
            msg = bot.send_message(
                admin_id, text, reply_markup=kb, disable_web_page_preview=True
            )
            store.record_admin_message(app["id"], admin_id, msg.message_id)
        except Exception:  # noqa: BLE001 — один недоступный админ не должен блокировать второго
            log.exception(
                "Не удалось доставить заявку №%s администратору %s",
                app["id"], admin_id,
            )


def user_decision_text(app: dict) -> str:
    number = f"№{app['id']:04d}"
    role = cfg.ROLES.get(app["role"], {"label": app["role"]})
    head = (
        f"Направление: {esc(role['label'])}\n"
        + (f"Проекты: {project_labels(app['projects'])}\n" if app.get("projects") else "")
    )
    if app["status"] == "approved":
        return (
            f"✅ <b>Заявка {number} одобрена!</b>\n\n" + head +
            "\nПоздравляем! Администрация свяжется с вами в ближайшее время "
            "для дальнейших шагов. Спасибо, что хотите быть частью "
            + esc(cfg.COMMUNITY_NAME) + "!"
        )
    reason = app.get("reason") or ""
    tail = f"\n\n<b>Причина:</b> {esc(reason)}" if reason else ""
    return (
        f"❌ <b>Заявка {number} отклонена</b>\n\n" + head + tail +
        "\n\nВы можете подать новую заявку позже: /start"
    )


def finalize(app_id: int, status: str, admin_id: int, admin_name: str,
             reason: str = "") -> dict | None:
    app = store.get(app_id)
    if not app or app["status"] != "pending":
        return None
    app = store.update(
        app_id,
        status=status,
        decided_by=admin_id,
        decided_by_name=admin_name,
        decided_at=now_iso(),
        reason=reason[:1000],
    )
    if not app:
        return None
    try:
        bot.send_message(
            app["user_id"], user_decision_text(app), disable_web_page_preview=True
        )
    except Exception:  # noqa: BLE001 — заявитель мог заблокировать бота
        log.exception("Не удалось уведомить заявителя %s", app["user_id"])
    card = format_application(app)
    for admin_key, message_id in app.get("admin_messages", {}).items():
        try:
            bot.edit_message_text(
                card,
                chat_id=int(admin_key),
                message_id=int(message_id),
                reply_markup=types.InlineKeyboardMarkup(),  # убираем кнопки решения
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            log.exception("Не удалось обновить карточку у %s", admin_key)
    log.info("Заявка №%s: %s (админ %s)", app_id, status, admin_id)
    return app


def handle_admin_callback(call, data: str) -> None:
    uid = call.from_user.id
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if not is_admin(uid):
        bot.answer_callback_query(
            call.id, "⛔ Эта кнопка доступна только администраторам.", show_alert=True
        )
        return
    if action == "canceladmin":
        st = get_admin_state(uid)
        clear_admin_state(uid)
        if st and st.get("message_id"):
            try:
                bot.edit_message_text(
                    "❌ Действие отменено.", chat_id=uid,
                    message_id=st["message_id"],
                    reply_markup=types.InlineKeyboardMarkup(),
                )
            except Exception:  # noqa: BLE001
                pass
        bot.answer_callback_query(call.id, "Отменено")
        return
    try:
        app_id = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        app_id = 0
    app = store.get(app_id) if app_id else None
    if not app:
        bot.answer_callback_query(call.id, "Заявка не найдена", show_alert=True)
        return
    if app["status"] != "pending":
        bot.answer_callback_query(
            call.id,
            f"Заявка уже обработана: {cfg.STATUS_LABELS.get(app['status'], app['status'])}",
            show_alert=True,
        )
        return

    if action == "accept":
        finalize(app_id, "approved", uid, user_info(call.from_user)["full_name"])
        bot.answer_callback_query(call.id, f"✅ Заявка №{app_id:04d} принята")
        return

    if action == "reject":
        prompt = (
            f"✍️ <b>Причина отказа по заявке №{app_id:04d}</b>\n\n"
            "Напишите одним сообщением, что передать заявителю, "
            "или нажмите «Без причины»."
        )
        msg = bot.send_message(uid, prompt, reply_markup=markup([
            [("Без причины", f"dec:skipreason:{app_id}")],
            [("Отмена", "dec:canceladmin")],
        ]))
        set_admin_state(uid, {"flow": "reject_reason", "app_id": app_id,
                              "message_id": msg.message_id})
        bot.answer_callback_query(call.id)
        return

    if action == "skipreason":
        st = get_admin_state(uid)
        clear_admin_state(uid)
        finalize(app_id, "rejected", uid, user_info(call.from_user)["full_name"])
        if st:
            close_admin_prompt(uid, st, f"❌ Заявка №{app_id:04d} отклонена.")
        bot.answer_callback_query(call.id, f"❌ Заявка №{app_id:04d} отклонена")
        return

    if action == "reply":
        prompt = (
            f"💬 <b>Сообщение заявителю по заявке №{app_id:04d}</b>\n\n"
            "Напишите текст одним сообщением — бот перешлёт его в личку заявителя."
        )
        msg = bot.send_message(uid, prompt, reply_markup=markup([
            [("Отмена", "dec:canceladmin")],
        ]))
        set_admin_state(uid, {"flow": "admin_reply", "app_id": app_id,
                              "message_id": msg.message_id})
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)


def close_admin_prompt(uid: int, st: dict, text: str) -> None:
    """Убираем кнопки у служебного сообщения администратора."""
    if st.get("message_id"):
        try:
            bot.edit_message_text(
                text, chat_id=uid, message_id=st["message_id"],
                reply_markup=types.InlineKeyboardMarkup(),
            )
        except Exception:  # noqa: BLE001
            pass


def handle_admin_text(message, text: str) -> None:
    uid = message.from_user.id
    st = get_admin_state(uid)
    app_id = st["app_id"]
    app = store.get(app_id)
    if not app:
        clear_admin_state(uid)
        bot.send_message(uid, "Заявка не найдена.")
        return
    if app["status"] != "pending":
        clear_admin_state(uid)
        bot.send_message(uid, "Заявка уже обработана другим администратором.")
        return
    name = user_info(message.from_user)["full_name"]
    if st["flow"] == "reject_reason":
        clear_admin_state(uid)
        finalize(app_id, "rejected", uid, name, reason=text)
        close_admin_prompt(uid, st, f"❌ Отказ по заявке №{app_id:04d} отправлен заявителю.")
    elif st["flow"] == "admin_reply":
        clear_admin_state(uid)
        body = (
            f"💬 <b>Сообщение от администрации</b> (заявка №{app_id:04d})\n\n"
            + esc(text[:1500])
            + "\n\n<i>Это служебное сообщение. Новая заявка — /start.</i>"
        )
        try:
            bot.send_message(app["user_id"], body, disable_web_page_preview=True)
            close_admin_prompt(uid, st, "✅ Сообщение доставлено заявителю.")
        except Exception:  # noqa: BLE001
            log.exception("Не удалось доставить сообщение заявителю %s", app["user_id"])
            close_admin_prompt(
                uid, st,
                "⚠️ Не удалось доставить сообщение (возможно, бот заблокирован).",
            )


# =========================================================================
# Обработчики Telegram
# =========================================================================
def _private(message) -> bool:
    if message.chat.type == "private":
        return True
    bot.send_message(message.chat.id, "ℹ️ Бот работает только в личных сообщениях.")
    return False


@bot.message_handler(commands=["start"])
def cmd_start(message):
    if not _private(message):
        return
    uid = message.from_user.id
    clear_admin_state(uid)
    msg = bot.send_message(
        uid, main_menu_text(), reply_markup=main_menu_kb(), disable_web_page_preview=True
    )
    set_state(uid, {
        "flow": "menu",
        "message_id": msg.message_id,
        "user": user_info(message.from_user),
    })
    log.info("/start от %s", uid)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if not _private(message):
        return
    uid = message.from_user.id
    clear_state(uid)
    clear_admin_state(uid)
    msg = bot.send_message(
        uid,
        "❌ Действие отменено.\n\n" + main_menu_text(),
        reply_markup=main_menu_kb(),
        disable_web_page_preview=True,
    )
    set_state(uid, {
        "flow": "menu", "message_id": msg.message_id,
        "user": user_info(message.from_user),
    })


@bot.message_handler(commands=["help"])
def cmd_help(message):
    if not _private(message):
        return
    st = get_state(message.from_user.id) or {"user": user_info(message.from_user)}
    show_help(message.from_user.id, st)


@bot.message_handler(commands=["about"])
def cmd_about(message):
    if not _private(message):
        return
    st = get_state(message.from_user.id) or {"user": user_info(message.from_user)}
    show_about(message.from_user.id, st)


@bot.message_handler(commands=["my"])
def cmd_my(message):
    if not _private(message):
        return
    st = get_state(message.from_user.id) or {"user": user_info(message.from_user)}
    show_my(message.from_user.id, st)


@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.send_message(
        message.chat.id,
        f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>",
    )


@bot.message_handler(commands=["apps"])
def cmd_apps(message):
    uid = message.from_user.id
    if not is_admin(uid):
        bot.send_message(message.chat.id, "⛔ Команда доступна только администраторам.")
        return
    apps = store.recent(10)
    if not apps:
        bot.send_message(message.chat.id, "📋 Заявок пока нет.")
        return
    lines = ["📋 <b>Последние заявки</b>", ""]
    for app in apps:
        role = cfg.ROLES.get(app["role"], {}).get("label", app["role"])
        lines.append(
            f"№{app['id']:04d} · {esc(role)} · "
            f"{esc(cfg.STATUS_LABELS.get(app['status'], app['status']))} · "
            f"<code>{app['user_id']}</code>"
        )
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    data = call.data or ""
    uid = call.from_user.id

    if data.startswith("dec:"):
        handle_admin_callback(call, data)
        return

    answered = [False]

    def ack(text: str | None = None, alert: bool = False) -> None:
        """Отвечаем на callback ровно один раз."""
        if not answered[0]:
            bot.answer_callback_query(call.id, text=text, show_alert=alert)
            answered[0] = True

    st = get_state(uid)
    if not st or st.get("message_id") != call.message.message_id:
        ack("⚠️ Это меню устарело. Нажмите /start, чтобы открыть актуальное.", True)
        return
    st.setdefault("user", user_info(call.from_user))
    parts = data.split(":")
    action = parts[0]

    try:
        # --- навигация по меню --------------------------------------------
        if data == "menu:main":
            show_main(uid, st)
        elif data == "menu:apply":
            left = cooldown_left(uid)
            if store.pending_for_user(uid):
                show_screen(
                    uid, st,
                    "⏳ У вас уже есть заявка на рассмотрении. Дождитесь решения.\n\n"
                    "📂 /my — статус",
                    markup([[("◀️ В меню", "menu:main")]]), "menu",
                )
            elif left:
                show_blocked(uid, st, cooldown_text(left))
            else:
                show_roles(uid, st)
        elif data == "menu:about":
            show_about(uid, st)
        elif data == "menu:help":
            show_help(uid, st)
        elif data == "menu:my":
            show_my(uid, st)
        elif data == "nav:roles":
            show_roles(uid, st)

        # --- выбор направления ---------------------------------------------
        elif action == "role" and len(parts) > 1:
            role_key = parts[1]
            if role_key not in cfg.ROLES:
                return
            if role_key == cfg.CONTENT_MAKER:
                show_projects(uid, st, [])
            else:
                start_form(uid, st, role_key, [])

        # --- выбор проектов контент-мейкера (оба отмечаются отдельно) -----
        elif action == "proj" and len(parts) > 2 and parts[1] == "toggle":
            key = parts[2]
            selected = list(st.get("projects") or [])
            if key in cfg.PROJECTS:
                if key in selected:
                    selected.remove(key)
                else:
                    selected.append(key)
            show_projects(uid, st, selected)
        elif data == "proj:next":
            selected = list(st.get("projects") or [])
            if not selected:
                ack("Выберите хотя бы один проект: VoidSMP или Майнкилд.", True)
                return
            start_form(uid, st, cfg.CONTENT_MAKER, selected)

        # --- анкета --------------------------------------------------------
        elif data == "form:back":
            if st.get("flow") == "preview":
                st["step"] = len(cfg.ROLES[st["role"]]["questions"]) - 1
                show_question(uid, st)
            elif st.get("flow") == "form" and st.get("step", 0) > 0:
                st["step"] -= 1
                show_question(uid, st)
            elif st.get("role") == cfg.CONTENT_MAKER:
                show_projects(uid, st, list(st.get("projects") or []))
            else:
                show_roles(uid, st)
        elif data == "form:restart":
            start_form(uid, st, st["role"], list(st.get("projects") or []))
        elif data == "form:cancel":
            show_main(uid, {"user": st.get("user"), "message_id": st.get("message_id")})
        elif data == "form:submit" and st.get("flow") == "preview":
            submit_application(uid, st)
    finally:
        ack()


@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(message):
    if not _private(message):
        return
    uid = message.from_user.id
    text = (message.text or "").strip()

    if is_admin(uid) and get_admin_state(uid):
        if text.startswith("/"):
            bot.send_message(uid, "Неизвестная команда. /help — список команд.")
            return
        handle_admin_text(message, text)
        return

    if text.startswith("/"):
        bot.send_message(uid, "Неизвестная команда. /help — список команд, /start — меню.")
        return

    st = get_state(uid)
    if not st:
        bot.send_message(uid, "Нажмите /start, чтобы открыть главное меню.")
        return
    if st.get("flow") == "form":
        handle_form_answer(message, st, text)
    else:
        bot.send_message(
            uid, "Сейчас бот ждёт нажатия кнопки в меню. /start — главное меню."
        )


@bot.message_handler(
    content_types=["photo", "video", "document", "sticker", "voice", "video_note",
                   "audio", "animation", "location", "contact", "poll"],
    func=lambda m: m.chat.type == "private",
)
def on_media(message):
    bot.send_message(
        message.chat.id,
        "📝 Анкета принимает только текстовые ответы. Отправьте ответ сообщением "
        "или нажмите /cancel, чтобы прервать заполнение.",
    )


# =========================================================================
# Точка входа
# =========================================================================
def main() -> None:
    if not cfg.BOT_TOKEN:
        print(
            "❌ Не задан BOT_TOKEN. Укажите токен от @BotFather в переменных "
            "окружения панели Bothost (или экспортируйте BOT_TOKEN локально).",
            file=sys.stderr,
        )
        sys.exit(1)
    log.info("Запуск бота. Администраторы заявок: %s", ", ".join(map(str, cfg.ADMIN_IDS)))
    log.info("Хранилище: %s", cfg.DB_PATH)
    try:
        me = bot.get_me()
        log.info("Авторизован как @%s (id %s)", me.username, me.id)
        # Работаем через long polling: вебхук (если был) мешает getUpdates.
        bot.delete_webhook()
        bot.set_my_commands([
            types.BotCommand("start", "Главное меню и подача заявки"),
            types.BotCommand("my", "Мои заявки и их статусы"),
            types.BotCommand("help", "Как подать заявку"),
            types.BotCommand("about", "О проектах и ролях"),
            types.BotCommand("cancel", "Отменить заполнение анкеты"),
            types.BotCommand("id", "Показать мой Telegram ID"),
            types.BotCommand("apps", "Список заявок (админам)"),
        ])
    except Exception as exc:  # noqa: BLE001 — сеть может подняться позже
        log.warning("Не удалось обновить настройки бота при старте: %s", exc)
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=25,
        skip_pending=True,
        logger_level=logging.INFO,
    )


if __name__ == "__main__":
    main()
