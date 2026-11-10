"""Настройки официального бота заявок VDS-Apl (VoidSMP / Майнкилд).

Все изменяемые тексты, вопросы и получатели заявок собраны здесь.
На bothost.ru значения задаются в разделе «Переменные окружения» панели.
"""
from __future__ import annotations

import os


def _load_dotenv() -> None:
    """Лёгкая поддержка .env для локального запуска без python-dotenv.

    Переменные, уже заданные в окружении (например, панелью Bothost),
    имеют приоритет и не перезаписываются.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return max(0, int(raw)) if raw else default
    except ValueError:
        return default


def _admin_ids() -> tuple[int, ...]:
    """По умолчанию заявки уходят ТОЛЬКО этим двум людям."""
    raw = _env("ADMIN_IDS") or "5992994003,1787475400"
    ids = []
    for part in raw.replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            value = int(part)
            if value not in ids:
                ids.append(value)
    return tuple(ids) or (5992994003, 1787475400)


# --- Токен ---------------------------------------------------------------
# Bothost обычно подставляет BOT_TOKEN сам; для локального запуска задайте
# переменную окружения BOT_TOKEN (например, через .env при локальном запуске).
BOT_TOKEN = _env("BOT_TOKEN") or _env("TELEGRAM_BOT_TOKEN") or _env("TOKEN")

# Telegram ID администраторов, которым приходят заявки (ровно два по ТЗ)
ADMIN_IDS = _admin_ids()

# --- Хранение данных -----------------------------------------------------
# На Bothost укажите DATA_DIR=/app/data, если подключено постоянное хранилище.
DATA_DIR = _env("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DATA_DIR, "applications.json")
BOT_TIMEZONE = _env("BOT_TIMEZONE") or "Europe/Moscow"

# --- Брендинг ------------------------------------------------------------
COMMUNITY_NAME = _env("COMMUNITY_NAME") or "VoidSMP · Майнкилд"

# --- Проекты: контент-мейкер отмечает их отдельными кнопками --------------
# (можно отметить и VoidSMP, и Майнкилд; отдельной кнопки «оба» нет)
PROJECTS: dict[str, str] = {
    "voidsmp": "VoidSMP",
    "minkild": "Майнкилд",
}

CONTENT_MAKER = "content_maker"
HELPER = "helper"
WIKI_EDITOR = "wiki_editor"

ROLES: dict[str, dict] = {
    CONTENT_MAKER: {
        "label": "🎬 Контент мейкер",
        "title": "Контент мейкер",
        "about": "Снимаете видео, стримите или делаете посты по VoidSMP и/или Майнкилд.",
        "questions": [
            "Ваш ник -",
            "Ваш канал -",
            "Сколько вы играете в майнкрафт -",
            "Ваш возраст -",
            'Ваша заявка (стримерам писать тут "стримы") -',
            "Какой уровень активности стримов и/или видеороликов вы планируете на ближайшие полгода -",
        ],
    },
    HELPER: {
        "label": "🛠 Хелпер",
        "title": "Хелпер",
        "about": "Помогаете игрокам, отвечаете на вопросы и следите за порядком на серверах.",
        "questions": [
            "Как к вам обращаться? (ник в игре / имя)",
            "Возраст и часовой пояс (UTC).",
            "Сколько времени в день готовы уделять проекту?",
            "Есть ли опыт хелпера/модератора? Где именно и что делали?",
            "Игрок нарушает правила, старших модераторов нет в сети. Что сделаете?",
            "Какие команды и плагины уже знаете? Что умеете проверять (логи, жалобы и т.д.)?",
        ],
    },
    WIKI_EDITOR: {
        "label": "📚 Редактор Вики (Fandom)",
        "title": "Редактор Вики (Fandom)",
        "about": "Ведёте Fandom-вики проектов: статьи, лор, персонажи, события и оформление.",
        "questions": [
            "Как к вам обращаться? (ник в игре / имя)",
            "Ссылка на профиль Fandom (или другой вики-аккаунт). Если нет — напишите «нет».",
            "Есть ли опыт редактирования вики? Примеры статей или правок.",
            "Какие разделы хотели бы вести? (лор, персонажи, события, предметы...)",
            "Умеете ли работать с шаблонами, категориями, таблицами и источниками?",
            "Сколько времени в неделю готовы уделять вики?",
        ],
    },
}

MAX_ANSWER_LEN = 450           # лимит одного ответа в анкете
MIN_ANSWER_LEN = 2
CARD_ANSWER_LIMIT = 300        # обрезка длинного ответа в карточке для админов

# --- Проверка ссылок ------------------------------------------------------
# Индексы вопросов (с нуля), где ссылка ОБЯЗАТЕЛЬНА: без неё ответ не примется.
LINK_REQUIRED: dict[str, list[int]] = {
    CONTENT_MAKER: [1],        # «Ваш канал -»
}
# Где ссылка ожидается, но разрешён честный ответ «нет».
LINK_OPTIONAL: dict[str, list[int]] = {
    WIKI_EDITOR: [1],          # «Ссылка на профиль Fandom ... Если нет — напишите «нет»»
}
NO_LINK_ANSWERS = (
    "нет", "нету", "нет ссылки", "нету ссылки", "ссылки нет", "ссылки нету",
    "без ссылки", "отсутствует", "не указана", "не имеется", "не имею",
    "не знаю", "пока нет", "—", "-",
)
# Для обязательной ссылки («Ваш канал» у контент-мейкера) принимаются ТОЛЬКО
# эти площадки: сам домен или его поддомены (www., m., clips. и т.д.).
# Ответ «https://youtube.com» без пути к каналу/видео тоже не считается ссылкой.
LINK_ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "twitch.tv",
)

# --- Антиспам: кулдаун на отправку заявок (по умолчанию 1 час) ------------
APPLICATION_COOLDOWN_SEC = _env_int("APPLICATION_COOLDOWN_SEC", 3600)

STATUS_LABELS = {
    "pending": "🕓 На рассмотрении",
    "approved": "✅ Принята",
    "rejected": "❌ Отклонена",
}
