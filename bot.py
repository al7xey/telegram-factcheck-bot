"""Telegram bot entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

from config import config
from factcheck_service import FactCheckResult, analyze_news, answer_question
from news_service import NewsItem, fetch_top_news
from storage import (
    clear_state,
    delete_subscription,
    get_last_news,
    get_subscription_expires_at,
    get_state,
    get_usage_count,
    increment_usage,
    init_db,
    set_last_news,
    set_state,
    set_subscription,
)


LOG_PATH = os.getenv("LOG_PATH", "bot.log")
logger = logging.getLogger("factcheck-bot")
logger.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
file_handler = RotatingFileHandler(
    LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logger.addHandler(file_handler)
BACKGROUND_TASKS: set[asyncio.Task] = set()

SUBSCRIPTION_PAYLOAD = "subscription_month_unlimited_v1"
STATE_AWAITING_QUESTION = "awaiting_question"
STATE_AWAITING_TOPIC = "awaiting_topic"
SECTION_TITLES = {
    STATE_AWAITING_QUESTION: "Вопрос по последней новости",
    STATE_AWAITING_TOPIC: "Топ-5 новостей по теме",
}
BUTTON_CHECK = "✅ Проверка"
BUTTON_SUBSCRIBE = "⭐ Подписка"
BUTTON_QUESTION = "❓ Вопрос"
BUTTON_TOP = "📰 Топ новости"
BUTTON_HELP = "🆘 Помощь"
BUTTON_ABOUT = "ℹ️ О боте"
BUTTON_START = "▶️ Старт"
BUTTON_TEXTS = {
    BUTTON_CHECK,
    BUTTON_SUBSCRIBE,
    BUTTON_QUESTION,
    BUTTON_TOP,
    BUTTON_HELP,
    BUTTON_ABOUT,
    BUTTON_START,
    "Проверка",
    "Подписка",
    "Помощь",
    "О боте",
    "Вопрос",
    "Топ новости",
    "Старт",
}

CHECK_TRIGGERS = {"проверка", "✅ проверка"}
SUBSCRIBE_TRIGGERS = {"подписка", "⭐ подписка"}
QUESTION_TRIGGERS = {"вопрос", "❓ вопрос"}
TOP_TRIGGERS = {"топ новости", "📰 топ новости", "топ"}
HELP_TRIGGERS = {"помощь", "🆘 помощь"}
ABOUT_TRIGGERS = {"о боте", "ℹ️ о боте"}
START_TRIGGERS = {"старт", "▶️ старт"}
MENU_TRIGGERS = {"меню", "📋 меню"}

GREETING_TRIGGERS = {
    "привет",
    "приветик",
    "здравствуйте",
    "здравствуй",
    "добрый день",
    "добрый вечер",
    "доброе утро",
    "хай",
    "хелло",
    "hello",
    "hi",
    "йо",
    "ку",
}

SMALLTALK_TRIGGERS = {
    "как дела",
    "как ты",
    "что делаешь",
    "спасибо",
    "благодарю",
    "понял",
    "понятно",
    "ок",
    "окей",
    "ясно",
}


def _is_too_short(text: str) -> bool:
    words = [word for word in text.split() if word.strip()]
    min_words = min(config.min_text_words, 4)
    return len(words) < min_words


def _format_result(result: FactCheckResult) -> str:
    reasoning = result.reasoning[:3]
    sources = result.sources[:3]

    if not sources:
        sources = ["(источники не указаны)"]

    reasoning_block = "\n".join(f"- {point}" for point in reasoning)
    sources_block = "\n".join(f"{idx + 1}. {src}" for idx, src in enumerate(sources))

    return (
        "✅ Проверка фактов\n\n"
        f"📌 Вердикт: {result.verdict}\n\n"
        f"🎯 Уверенность: {result.confidence}%\n\n"
        "🧾 Обоснование:\n"
        f"{reasoning_block}\n\n"
        "🔗 Источники:\n"
        f"{sources_block}"
    )


def _subscription_info() -> str:
    return (
        f"📊 Лимит без подписки: {config.daily_limit} новостей в день.\n"
        f"⭐ Подписка: {config.subscription_stars} ⭐️ на {config.subscription_days} дней безлимита "
        "(кнопка «Подписка»)."
    )


def _format_date(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y")


def _get_active_subscription(user_id: int, now: datetime) -> datetime | None:
    expires_at = get_subscription_expires_at(user_id)
    if not expires_at:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        delete_subscription(user_id)
        return None
    return expires_at


def _today_key(now: datetime) -> str:
    return now.date().isoformat()


def _normalize_topic(text: str) -> str | None:
    raw = text.strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in {"без темы", "без", "любые", "любой", "все новости", "топ", "топ новости"}:
        return None
    return raw


def _extract_question(text: str) -> str | None:
    raw = text.strip()
    if not raw:
        return None
    lowered = raw.lower()
    for prefix in ("вопрос:", "вопрос -", "вопрос "):
        if lowered.startswith(prefix):
            return raw[len(prefix) :].strip()
    if raw.startswith("?"):
        return raw.lstrip("?").strip()
    return None


def _normalize_trigger(text: str) -> str:
    return " ".join(text.strip().casefold().split())


def _normalize_free_text(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return " ".join(cleaned.casefold().split())


def _contains_url(text: str) -> bool:
    return bool(re.search(r"(https?://|www\.)", text, re.IGNORECASE))


def _is_short_message(text: str) -> bool:
    normalized = _normalize_free_text(text)
    if not normalized:
        return False
    if len(normalized) > 60:
        return False
    return len(normalized.split()) <= 3


def _looks_like_greeting(text: str) -> bool:
    normalized = _normalize_free_text(text)
    if not normalized:
        return False
    if normalized in GREETING_TRIGGERS:
        return True
    words = normalized.split()
    if len(words) > 2:
        return False
    return all(word in GREETING_TRIGGERS for word in words)


def _looks_like_smalltalk(text: str) -> bool:
    normalized = _normalize_free_text(text)
    if not normalized:
        return False
    if len(normalized.split()) > 4:
        return False
    return any(trigger in normalized for trigger in SMALLTALK_TRIGGERS)


def _section_banner(state_name: str | None) -> str:
    if not state_name:
        return ""
    title = SECTION_TITLES.get(state_name)
    if not title:
        return ""
    return f"Сейчас вы в разделе: {title}."


def _with_section_banner(text: str, state_name: str | None) -> str:
    banner = _section_banner(state_name)
    if not banner:
        return text
    return f"{banner}\n\n{text}"


def _menu_text() -> str:
    return (
        "📋 Меню разделов\n\n"
        "✅ /check — проверить новость\n"
        "❓ /question — вопрос по последней новости\n"
        "📰 /top — топ-5 новостей по теме\n"
        "⭐ /subscribe — подписка и лимиты\n"
        "🆘 /help — помощь\n"
        "ℹ️ /about — о боте\n"
        "▶️ /start — старт\n"
        "📋 /menu — показать это меню\n\n"
        "Можно использовать кнопки ниже."
    )


def _format_news_items(items: list[NewsItem], topic: str | None) -> str:
    header = "📰 Топ-5 новостей"
    if topic:
        header = f"{header} по теме: {topic}"
    lines = [header, ""]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item.title}")
        if item.summary:
            lines.append(f"📝 {item.summary}")
        if item.source:
            lines.append(f"🗞️ Источник: {item.source}")
        lines.append(f"🔗 {item.link}")
        lines.append("")
    return "\n".join(lines).strip()


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_CHECK), KeyboardButton(text=BUTTON_SUBSCRIBE)],
            [KeyboardButton(text=BUTTON_QUESTION), KeyboardButton(text=BUTTON_TOP)],
            [KeyboardButton(text=BUTTON_HELP), KeyboardButton(text=BUTTON_ABOUT)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Введите новость, вопрос или тему ✍️",
    )


def _extract_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def _is_owner(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in config.owner_ids


def _track_task(task: asyncio.Task) -> None:
    BACKGROUND_TASKS.add(task)

    def _done(done_task: asyncio.Task) -> None:
        BACKGROUND_TASKS.discard(done_task)
        try:
            done_task.result()
        except Exception:
            logger.exception("Background task failed")

    task.add_done_callback(_done)


async def _analyze_and_respond(
    bot: Bot,
    chat_id: int,
    status_message_id: int,
    news_text: str,
) -> None:
    try:
        result = await asyncio.to_thread(analyze_news, news_text)
        result_text = _format_result(result)
    except Exception:
        logger.exception("Fact-check failed")
        result_text = (
            "Извините, сейчас не удалось проанализировать новость. Попробуйте позже."
        )

    try:
        await bot.edit_message_text(
            text=result_text,
            chat_id=chat_id,
            message_id=status_message_id,
        )
    except TelegramBadRequest:
        await bot.send_message(chat_id, result_text)


async def handle_start(message: Message) -> None:
    if message.from_user:
        clear_state(message.from_user.id)
    await message.answer(
        "Привет! 👋\n\n"
        "Я бот для проверки новостей и сообщений 🔎\n\n"
        "Как это работает:\n"
        "1️⃣ Ты присылаешь текст или ссылку\n"
        "2️⃣ Я анализирую и даю вердикт\n"
        "3️⃣ Показываю краткое обоснование и источники\n\n"
        "🔗 Источники по возможности даю прямыми ссылками на первоисточники.\n"
        "🧠 Я не заменяю журналистские расследования — проверяю по доступным данным.\n\n"
        "Можно задать вопрос по последней новости (кнопка «Вопрос»).\n"
        "Также доступен «Топ новости» — отправь тему и получишь подборку.\n"
        "📋 /menu — список разделов и команд.\n\n"
        f"{_subscription_info()}\n\n"
        "⏳ Если бот долго не отвечает, подождите до 50 секунд — "
        "иногда требуется время на запуск.",
        reply_markup=_main_keyboard(),
    )


async def handle_help(message: Message) -> None:
    if message.from_user:
        clear_state(message.from_user.id)
    await message.answer(
        "🆘 Как пользоваться ботом:\n\n"
        "1️⃣ Перешли новость или сообщение\n"
        "2️⃣ Отправь ссылку на статью\n"
        "3️⃣ Напиши текст, который хочешь проверить\n\n"
        "✅ Я проанализирую информацию и покажу результат проверки.\n"
        "🔗 Источники по возможности будут прямыми ссылками на первоисточники.\n\n"
        "❓ Вопрос по последней новости — кнопка «Вопрос».\n"
        "📰 Подборка по теме — кнопка «Топ новости».\n"
        "📋 /menu — список команд и разделов.\n\n"
        f"{_subscription_info()}\n\n"
        "⏳ Если бот долго не отвечает, подожди до 50 секунд — "
        "иногда требуется время на запуск.",
        reply_markup=_main_keyboard(),
    )


async def handle_about(message: Message) -> None:
    if message.from_user:
        clear_state(message.from_user.id)
    await message.answer(
        "ℹ️ О боте\n\n"
        "Этот бот анализирует новости и сообщения, "
        "чтобы помочь понять их достоверность. 🔎\n\n"
        "✅ Отправь текст или ссылку — и бот проверит информацию.\n"
        "🔗 Источники по возможности даются прямыми ссылками на первоисточники.\n"
        "🧠 Бот не заменяет экспертную проверку — он помогает быстро сориентироваться.\n\n"
        "❓ Можно задать вопрос по последней новости.\n"
        "📰 Можно получить подборку «Топ новости» по интересующей теме.\n"
        "📋 /menu — список команд и разделов.\n\n"
        f"{_subscription_info()}\n\n"
        "⏳ Если бот долго не отвечает, подожди до 50 секунд — "
        "иногда требуется время на запуск.",
        reply_markup=_main_keyboard(),
    )


async def handle_menu(message: Message) -> None:
    if message.from_user:
        clear_state(message.from_user.id)
    await message.answer(_menu_text(), reply_markup=_main_keyboard())


async def handle_check(message: Message) -> None:
    if message.from_user:
        clear_state(message.from_user.id)
    await message.answer(
        "✅ Проверка новости\n\n"
        "Отправьте текст новости или ссылку на статью.\n"
        "Пример: «В городе N открыли новый завод…»\n\n"
        "❓ После проверки можно задать вопрос по этой новости.\n"
        "Для выхода напишите «меню».",
        reply_markup=_main_keyboard(),
    )


async def _answer_question_message(
    message: Message,
    question: str,
    state_name: str | None = None,
) -> None:
    user = message.from_user
    if not user:
        await message.answer("⚠️ Не удалось определить пользователя.")
        return

    cleaned = question.strip()
    if len(cleaned) < 3:
        set_state(user.id, STATE_AWAITING_QUESTION)
        text = _with_section_banner(
            "📝 Сформулируйте вопрос чуть подробнее.\n"
            "Пример: «Кто упомянут в новости и когда это произошло?»\n"
            "Для выхода напишите «меню».",
            STATE_AWAITING_QUESTION,
        )
        await message.answer(text, reply_markup=_main_keyboard())
        return

    last_news = get_last_news(user.id)
    if not last_news:
        text = (
            "⚠️ Сначала отправьте новость для проверки — "
            "тогда я смогу отвечать на вопросы по ней."
        )
        await message.answer(
            _with_section_banner(text, state_name),
            reply_markup=_main_keyboard(),
        )
        return

    status_message = await message.answer(
        _with_section_banner(
            "⏳ Отвечаю на вопрос по последней новости. Пожалуйста, подождите.",
            state_name,
        )
    )
    try:
        answer = await asyncio.to_thread(answer_question, last_news, cleaned)
    except Exception:
        logger.exception("Question answering failed")
        answer = "⚠️ Извините, сейчас не удалось ответить на вопрос. Попробуйте позже."

    answer = _with_section_banner(answer, state_name)
    try:
        await message.bot.edit_message_text(
            text=answer,
            chat_id=message.chat.id,
            message_id=status_message.message_id,
        )
    except TelegramBadRequest:
        await message.answer(answer, reply_markup=_main_keyboard())


async def _send_top_news_message(
    message: Message,
    topic: str | None,
    state_name: str | None = None,
) -> None:
    status_message = await message.answer(
        _with_section_banner(
            "📰 Собираю топ-5 новостей. Пожалуйста, подождите.",
            state_name,
        )
    )
    try:
        items = await asyncio.to_thread(fetch_top_news, topic, 5)
    except Exception:
        logger.exception("Top news fetch failed")
        items = []

    if not items:
        text = _with_section_banner(
            "⚠️ Не удалось получить новости по теме. Попробуйте позже.",
            state_name,
        )
        await message.bot.edit_message_text(
            text=text,
            chat_id=message.chat.id,
            message_id=status_message.message_id,
        )
        return

    text = _with_section_banner(_format_news_items(items, topic), state_name)
    try:
        await message.bot.edit_message_text(
            text=text,
            chat_id=message.chat.id,
            message_id=status_message.message_id,
        )
    except TelegramBadRequest:
        await message.answer(text, reply_markup=_main_keyboard())


async def handle_question(message: Message) -> None:
    user = message.from_user
    if not user:
        await message.answer("⚠️ Не удалось определить пользователя.")
        return

    clear_state(user.id)
    last_news = get_last_news(user.id)
    if not last_news:
        await message.answer(
            "⚠️ Сначала отправьте новость для проверки — "
            "тогда я смогу отвечать на вопросы по ней.",
            reply_markup=_main_keyboard(),
        )
        return

    set_state(user.id, STATE_AWAITING_QUESTION)
    text = _with_section_banner(
        "❓ Напишите вопрос по последней новости.\n"
        "Пример: «Кто упомянут в новости и когда это произошло?»\n"
        "Для выхода напишите «меню».",
        STATE_AWAITING_QUESTION,
    )
    await message.answer(text, reply_markup=_main_keyboard())


async def handle_top_news(message: Message) -> None:
    user = message.from_user
    if user:
        clear_state(user.id)
        set_state(user.id, STATE_AWAITING_TOPIC)
    text = _with_section_banner(
        "📰 Напишите тему для топ-5 новостей.\n"
        "Например: «технологии», «спорт», «экономика».\n"
        "Если нужна подборка без темы — отправьте «без темы».\n"
        "Для выхода напишите «меню».",
        STATE_AWAITING_TOPIC,
    )
    await message.answer(text, reply_markup=_main_keyboard())


async def handle_subscribe(message: Message) -> None:
    user = message.from_user
    if not user:
        await message.answer("⚠️ Не удалось определить пользователя.")
        return

    clear_state(user.id)
    now = datetime.now(timezone.utc)
    active_until = _get_active_subscription(user.id, now)
    status_line = (
        f"✅ Текущая подписка активна до {_format_date(active_until)}."
        if active_until
        else "💳 Подписка активируется сразу после оплаты."
    )

    await message.answer(
        "⭐ Подписка на месяц: безлимитная проверка новостей.\n"
        f"💰 Цена: {config.subscription_stars} ⭐️.\n"
        f"📆 Срок: {config.subscription_days} дней.\n"
        f"📊 Лимит без подписки: {config.daily_limit} новостей в день.\n"
        f"{status_line}",
        reply_markup=_main_keyboard(),
    )

    prices = [
        LabeledPrice(
            label="Подписка на месяц",
            amount=config.subscription_stars,
        )
    ]
    await message.answer_invoice(
        title="⭐ Подписка на месяц",
        description=f"Безлимитная проверка новостей на {config.subscription_days} дней.",
        payload=SUBSCRIPTION_PAYLOAD,
        currency="XTR",
        prices=prices,
        provider_token=config.payment_provider_token,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"💳 Оплатить {config.subscription_stars} ⭐️",
                        pay=True,
                    )
                ]
            ]
        ),
    )


async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    if query.invoice_payload != SUBSCRIPTION_PAYLOAD:
        await query.answer(ok=False, error_message="⚠️ Платеж не распознан.")
        return
    await query.answer(ok=True)


async def handle_successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if not payment:
        return

    if payment.invoice_payload != SUBSCRIPTION_PAYLOAD:
        logger.warning("Unexpected payment payload: %s", payment.invoice_payload)
        return

    if payment.currency != "XTR":
        logger.warning("Unexpected payment currency: %s", payment.currency)
        return

    user = message.from_user
    if not user:
        await message.answer("⚠️ Не удалось определить пользователя.")
        return

    now = datetime.now(timezone.utc)
    current = get_subscription_expires_at(user.id)
    base = current if current and current > now else now
    new_expires = base + timedelta(days=config.subscription_days)
    set_subscription(user.id, new_expires)

    await message.answer(
        f"✅ Оплата получена. Подписка активна до {_format_date(new_expires)}.",
        reply_markup=_main_keyboard(),
    )


async def handle_any(message: Message) -> None:
    text = _extract_text(message)

    if message.text:
        stripped = message.text.strip()
        if stripped in BUTTON_TEXTS:
            return
        normalized = _normalize_trigger(stripped)
        if normalized in QUESTION_TRIGGERS:
            await handle_question(message)
            return
        if normalized in TOP_TRIGGERS:
            await handle_top_news(message)
            return
        if normalized in SUBSCRIBE_TRIGGERS:
            await handle_subscribe(message)
            return
        if normalized in CHECK_TRIGGERS:
            await handle_check(message)
            return
        if normalized in HELP_TRIGGERS:
            await handle_help(message)
            return
        if normalized in ABOUT_TRIGGERS:
            await handle_about(message)
            return
        if normalized in START_TRIGGERS:
            await handle_start(message)
            return
        if normalized in MENU_TRIGGERS:
            await handle_menu(message)
            return

    user = message.from_user

    if message.text and message.text.startswith("/"):
        command = message.text.split()[0].lower()
        if command in {
            "/start",
            "/help",
            "/about",
            "/check",
            "/subscribe",
            "/top",
            "/question",
            "/menu",
        }:
            return
        if user:
            clear_state(user.id)
        await message.answer(
            "📝 Отправьте текст новости для проверки.",
            reply_markup=_main_keyboard(),
        )
        return

    if user:
        state = get_state(user.id)
        if state:
            state_name, _payload = state
            clear_state(user.id)
            if state_name == STATE_AWAITING_QUESTION:
                await _answer_question_message(message, text, state_name=state_name)
                return
            if state_name == STATE_AWAITING_TOPIC:
                topic = _normalize_topic(text)
                await _send_top_news_message(message, topic, state_name=state_name)
                return

    question = _extract_question(text)
    if question:
        await _answer_question_message(message, question)
        return

    if (
        user
        and _is_short_message(text)
        and not _contains_url(text)
        and (_looks_like_greeting(text) or _looks_like_smalltalk(text))
    ):
        await message.answer(
            "Привет! Чем могу помочь?\n"
            "✅ Проверка новости\n"
            "❓ Вопрос по последней новости\n"
            "📰 Топ-5 новостей по теме\n\n"
            "Выберите раздел кнопками или отправьте /menu.",
            reply_markup=_main_keyboard(),
        )
        return

    if _is_too_short(text):
        await message.answer(
            "Похоже, это короткое сообщение.\n"
            "Если хотите проверить новость — пришлите текст или ссылку.\n"
            "Можно выбрать раздел кнопками или отправить /menu.",
            reply_markup=_main_keyboard(),
        )
        return

    if not user:
        await message.answer(
            "⚠️ Не удалось определить пользователя. Попробуйте еще раз.",
            reply_markup=_main_keyboard(),
        )
        return

    now = datetime.now(timezone.utc)
    active_until = _get_active_subscription(user.id, now)
    if not active_until and not _is_owner(user.id):
        day_key = _today_key(now)
        used = get_usage_count(user.id, day_key)
        if used >= config.daily_limit:
            await message.answer(
                "⚠️ Достигнут дневной лимит проверок.\n\n"
                f"{_subscription_info()}",
                reply_markup=_main_keyboard(),
            )
            return
        increment_usage(user.id, day_key)

    set_last_news(user.id, text)
    status_message = await message.answer(
        "🔎 Проверяю новость. Пожалуйста, подождите, ответ формируется."
    )
    task = asyncio.create_task(
        _analyze_and_respond(
            message.bot,
            message.chat.id,
            status_message.message_id,
            text,
        )
    )
    _track_task(task)


def _build_webhook_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    path = "/" + path.lstrip("/")
    return f"{base}{path}"


async def _healthcheck(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="▶️ Старт и приветствие"),
        BotCommand(command="check", description="✅ Проверка новости"),
        BotCommand(command="question", description="❓ Вопрос по последней новости"),
        BotCommand(command="top", description="📰 Топ-5 новостей по теме"),
        BotCommand(command="subscribe", description="⭐ Подписка и лимиты"),
        BotCommand(command="help", description="🆘 Помощь"),
        BotCommand(command="about", description="ℹ️ О боте"),
        BotCommand(command="menu", description="📋 Меню разделов"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception:
        logger.exception("Failed to set bot commands")


async def _run_polling(bot: Bot, dp: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


async def _run_webhook(bot: Bot, dp: Dispatcher) -> None:
    if not config.webhook_base_url:
        raise RuntimeError("WEBHOOK_BASE_URL or RENDER_EXTERNAL_URL is required.")

    webhook_url = _build_webhook_url(config.webhook_base_url, config.webhook_path)

    app = web.Application()
    app.router.add_get("/", _healthcheck)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=config.webhook_secret,
    ).register(app, path=config.webhook_path)

    async def _on_startup(_: web.Application) -> None:
        await bot.set_webhook(
            webhook_url,
            secret_token=config.webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        logger.info("Webhook set to %s", webhook_url)

    async def _on_shutdown(_: web.Application) -> None:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.session.close()

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.webhook_host, port=config.webhook_port)
    await site.start()
    logger.info(
        "Webhook server started on %s:%s", config.webhook_host, config.webhook_port
    )

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


async def main() -> None:
    init_db()
    bot = Bot(token=config.telegram_bot_token)
    dp = Dispatcher()

    await _set_bot_commands(bot)

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_start, F.text == "Старт")
    dp.message.register(handle_start, F.text == BUTTON_START)
    dp.message.register(handle_menu, Command("menu"))
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_help, F.text == "Помощь")
    dp.message.register(handle_help, F.text == BUTTON_HELP)
    dp.message.register(handle_about, Command("about"))
    dp.message.register(handle_about, F.text == "О боте")
    dp.message.register(handle_about, F.text == BUTTON_ABOUT)
    dp.message.register(handle_check, Command("check"))
    dp.message.register(handle_check, F.text == "Проверка")
    dp.message.register(handle_check, F.text == BUTTON_CHECK)
    dp.message.register(handle_question, Command("question"))
    dp.message.register(handle_question, F.text == "Вопрос")
    dp.message.register(handle_question, F.text == BUTTON_QUESTION)
    dp.message.register(handle_top_news, Command("top"))
    dp.message.register(handle_top_news, F.text == "Топ новости")
    dp.message.register(handle_top_news, F.text == BUTTON_TOP)
    dp.message.register(handle_subscribe, Command("subscribe"))
    dp.message.register(handle_subscribe, F.text == "Подписка")
    dp.message.register(handle_subscribe, F.text == BUTTON_SUBSCRIBE)
    dp.pre_checkout_query.register(handle_pre_checkout)
    dp.message.register(handle_successful_payment, F.successful_payment)
    dp.message.register(handle_any)

    if config.webhook_base_url:
        logger.info("Starting in webhook mode")
        await _run_webhook(bot, dp)
    else:
        logger.info("Starting in polling mode")
        await _run_polling(bot, dp)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
