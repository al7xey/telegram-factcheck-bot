"""Fact-checking service that orchestrates prompt creation and response parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from gigachat_client import GigaChatError, send_prompt_to_gigachat


VERDICT_MAP = {
    "Fake": "Фейк",
    "Likely Fake": "Скорее фейк",
    "Unverified": "Не подтверждено",
    "Likely True": "Скорее правда",
    "Фейк": "Фейк",
    "Скорее фейк": "Скорее фейк",
    "Не подтверждено": "Не подтверждено",
    "Скорее правда": "Скорее правда",
}
ALLOWED_VERDICTS = set(VERDICT_MAP.keys())


@dataclass
class FactCheckResult:
    verdict: str
    confidence: int
    reasoning: list[str]
    sources: list[str]
    raw_response: str | None = None


def _build_prompt(news_text: str) -> str:
    return (
        "Ты фактчекер. Верни только JSON по схеме ниже. "
        "Ответ должен быть кратким.\n"
        "Вердикт: Фейк, Скорее фейк, Не подтверждено, или Скорее правда.\n"
        "Reasoning: ровно 3 коротких пункта, первый — ключевые утверждения.\n"
        "Sources: ОБЯЗАТЕЛЬНО 2-3 URL. НЕЛЬЗЯ оставлять пустым, НЕЛЬЗЯ писать 'нет'. "
        "Если точных ссылок нет, укажи наиболее релевантные официальные страницы и/или крупные медиа.\n\n"
        "JSON: {\n"
        "  \"verdict\": \"Скорее правда\",\n"
        "  \"confidence\": 78,\n"
        "  \"reasoning\": [\"пункт 1\", \"пункт 2\", \"пункт 3\"],\n"
        "  \"sources\": [\"https://example.com\"]\n"
        "}\n\n"
        "Текст новости:\n"
        f"{news_text}"
    )


def _build_question_prompt(news_text: str, question: str) -> str:
    return (
        "Ты помощник по новостям. Ответь на вопрос пользователя, "
        "используя только текст новости ниже. Не выдумывай фактов.\n"
        "Если в новости нет ответа, так и скажи и уточни, чего не хватает.\n"
        "Ответ — кратко, 3-6 предложений.\n\n"
        "Текст новости:\n"
        f"{news_text}\n\n"
        "Вопрос пользователя:\n"
        f"{question}"
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None

    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _normalize_verdict(value: Any) -> str:
    if isinstance(value, str):
        value = value.strip()
        if value in ALLOWED_VERDICTS:
            return VERDICT_MAP[value]
        lowered = value.lower()
        for key in ALLOWED_VERDICTS:
            if key.lower() == lowered:
                return VERDICT_MAP[key]
    return "Не подтверждено"


def _normalize_confidence(value: Any) -> int:
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        score = 50
    return max(0, min(100, score))


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _ensure_min_items(items: list[str], minimum: int, fallback: list[str]) -> list[str]:
    if len(items) >= minimum:
        return items
    needed = minimum - len(items)
    return items + fallback[:needed]


def analyze_news(news_text: str) -> FactCheckResult:
    """Analyze a news text and return a structured fact-check result."""
    prompt = _build_prompt(news_text)

    try:
        response_text = send_prompt_to_gigachat(prompt)
    except GigaChatError as exc:
        raise RuntimeError(f"GigaChat request failed: {exc}") from exc

    payload = _extract_json(response_text)

    if not payload:
        return FactCheckResult(
            verdict="Не подтверждено",
            confidence=50,
            reasoning=[
                "Ответ модели не соответствовал ожидаемому JSON-формату.",
                "В тексте новости нет проверяемых доказательств.",
                "Проверьте информацию через надежные источники.",
            ],
            sources=[],
            raw_response=response_text,
        )

    verdict = _normalize_verdict(payload.get("verdict"))
    confidence = _normalize_confidence(payload.get("confidence"))
    reasoning = _normalize_list(payload.get("reasoning"))
    sources = _normalize_list(payload.get("sources"))

    reasoning = _ensure_min_items(
        reasoning,
        3,
        [
            "В сообщении нет надежных ссылок на источники.",
            "Часть утверждений нельзя проверить только по тексту.",
            "Сверьте ключевые факты с проверенными изданиями.",
        ],
    )

    return FactCheckResult(
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
        sources=sources,
        raw_response=response_text,
    )


def answer_question(news_text: str, question: str) -> str:
    """Answer a user's question about the provided news text."""
    prompt = _build_question_prompt(news_text, question)

    try:
        response_text = send_prompt_to_gigachat(prompt)
    except GigaChatError as exc:
        raise RuntimeError(f"GigaChat request failed: {exc}") from exc

    return response_text.strip()
