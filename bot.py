"""Telegram bot entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message

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
        "Отправьте новость, и я проверю, насколько она похожа на правду или фейк."
    )


async def handle_any(message: Message) -> None:
    text = _extract_text(message)

    if message.text and message.text.startswith("/start"):
        return

    if message.text and message.text.startswith("/"):
        await message.answer("Отправьте текст новости для проверки.")
        return

    if _is_too_short(text):
        await message.answer(
            "Пожалуйста, отправьте текст новости или подпись к пересланному сообщению."
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


async def main() -> None:
    bot = Bot(token=config.telegram_bot_token)
    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_any)

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
