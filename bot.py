"""Telegram bot entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

from config import config
from factcheck_service import FactCheckResult, analyze_news


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


def _is_too_short(text: str) -> bool:
    words = [word for word in text.split() if word.strip()]
    return len(text.strip()) < config.min_text_chars or len(words) < config.min_text_words


def _format_result(result: FactCheckResult) -> str:
    reasoning = result.reasoning[:3]
    sources = result.sources[:3]

    if not sources:
        sources = ["(источники не указаны)"]

    reasoning_block = "\n".join(f"- {point}" for point in reasoning)
    sources_block = "\n".join(f"{idx + 1}. {src}" for idx, src in enumerate(sources))

    return (
        "Проверка фактов\n\n"
        f"Вердикт: {result.verdict}\n\n"
        f"Уверенность: {result.confidence}%\n\n"
        "Обоснование:\n"
        f"{reasoning_block}\n\n"
        "Источники:\n"
        f"{sources_block}"
    )


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/check"), KeyboardButton(text="/help")],
            [KeyboardButton(text="/about"), KeyboardButton(text="/start")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Введите новость или выберите команду",
    )


def _extract_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


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
    await message.answer(
        "Привет! 👋\n\n"
        "Я бот для проверки новостей.\n\n"
        "Просто перешли мне сообщение, новость или ссылку — "
        "я проанализирую её и покажу результат проверки и источники.",
        reply_markup=_main_keyboard(),
    )


async def handle_help(message: Message) -> None:
    await message.answer(
        "Как пользоваться ботом:\n\n"
        "1️⃣ Перешли новость или сообщение\n"
        "2️⃣ Отправь ссылку на статью\n"
        "3️⃣ Напиши текст, который хочешь проверить\n\n"
        "Бот проанализирует информацию и покажет результат проверки.",
        reply_markup=_main_keyboard(),
    )


async def handle_about(message: Message) -> None:
    await message.answer(
        "Этот бот анализирует новости и сообщения, "
        "чтобы определить их достоверность.\n\n"
        "Отправь текст или ссылку — и бот проверит информацию.",
        reply_markup=_main_keyboard(),
    )


async def handle_check(message: Message) -> None:
    await message.answer(
        "Отправь новость, сообщение или ссылку — "
        "я попробую проверить её достоверность.",
        reply_markup=_main_keyboard(),
    )


async def handle_any(message: Message) -> None:
    text = _extract_text(message)

    if message.text and message.text.startswith("/"):
        command = message.text.split()[0].lower()
        if command in {"/start", "/help", "/about", "/check"}:
            return
        await message.answer(
            "Отправьте текст новости для проверки.",
            reply_markup=_main_keyboard(),
        )
        return

    if _is_too_short(text):
        await message.answer(
            "Пожалуйста, отправьте текст новости или подпись к пересланному сообщению.",
            reply_markup=_main_keyboard(),
        )
        return

    status_message = await message.answer(
        "Проверяю новость. Пожалуйста, подождите, ответ формируется."
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
            drop_pending_updates=True,
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
    bot = Bot(token=config.telegram_bot_token)
    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_about, Command("about"))
    dp.message.register(handle_check, Command("check"))
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
