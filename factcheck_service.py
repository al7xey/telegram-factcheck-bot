"""Fact-checking service that orchestrates prompt creation and response parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from gigachat_client import GigaChatError, send_prompt_to_gigachat
from news_service import NewsItem, fetch_top_news, normalize_direct_links


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


@dataclass
class QuestionAnswer:
    found: bool
    answer: str
    missing: str
    sources: list[str]
    raw_response: str | None = None


def _build_prompt(news_text: str) -> str:
    return (
        "Ты фактчекер. Верни только JSON по схеме ниже. "
        "Ответ должен быть кратким.\n"
        "Вердикт: Фейк, Скорее фейк, Не подтверждено, или Скорее правда.\n"
        "Reasoning: ровно 3 коротких пункта, первый — ключевые утверждения.\n"
        "Sources: ОБЯЗАТЕЛЬНО 2-3 прямых URL на первоисточники (без Google/Google News). "
        "НЕЛЬЗЯ оставлять пустым, НЕЛЬЗЯ писать 'нет'. "
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
        "используя ТОЛЬКО текст новости ниже. Не выдумывай фактов.\n"
        "Верни ТОЛЬКО JSON по схеме:\n"
        "{\n"
        "  \"answer_found\": true,\n"
        "  \"answer\": \"краткий ответ (3-6 предложений)\",\n"
        "  \"missing\": \"что не хватает, если ответа нет\"\n"
        "}\n"
        "Правила:\n"
        "- Если ответ есть в тексте, answer_found=true, answer заполнен, missing пустая строка.\n"
        "- Если ответа нет, answer_found=false, answer пустая строка, missing заполнен.\n"
        "- Не добавляй внешние источники.\n\n"
        "Текст новости:\n"
        f"{news_text}\n\n"
        "Вопрос пользователя:\n"
        f"{question}"
    )


def _build_search_prompt(question: str, items: list[NewsItem]) -> str:
    return (
        "Ты помощник по новостям. Ответь на вопрос пользователя, "
        "используя ТОЛЬКО результаты поиска ниже (заголовки и краткие описания). "
        "Не выдумывай фактов.\n"
        "Верни ТОЛЬКО JSON по схеме:\n"
        "{\n"
        "  \"answer_found\": true,\n"
        "  \"answer\": \"краткий ответ (3-6 предложений)\",\n"
        "  \"missing\": \"что не хватает, если ответа нет\",\n"
        "  \"sources\": [\"https://example.com\"]\n"
        "}\n"
        "Правила:\n"
        "- Если ответ есть, answer_found=true и sources содержит 1-3 URL ТОЛЬКО из списка ниже "
        "(прямые ссылки, не Google/Google News).\n"
        "- Если ответа нет, answer_found=false, answer пустая строка, missing заполнен, sources пустой.\n"
        "- Не добавляй другие источники и не делай выводов сверх текста результатов.\n\n"
        "Результаты поиска:\n"
        f"{_format_search_items(items)}\n\n"
        "Вопрос пользователя:\n"
        f"{question}"
    )


def _build_assumption_prompt(news_text: str, question: str) -> str:
    return (
        "Прямого ответа в тексте новости и в дополнительных источниках нет.\n"
        "Сформулируй обоснованное предположение по вопросу пользователя, "
        "опираясь на контекст новости и здравый смысл.\n"
        "Правила:\n"
        "- Начни ответ со слов \"Предположение:\".\n"
        "- Явно укажи, что это не подтверждено источниками.\n"
        "- 2-4 предложения, без категоричных утверждений.\n\n"
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


def _normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {
            "true",
            "yes",
            "да",
            "истина",
            "верно",
            "1",
        }
    return False


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


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _format_search_items(items: list[NewsItem]) -> str:
    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. Заголовок: {item.title}")
        if item.summary:
            lines.append(f"Описание: {item.summary}")
        if item.source:
            lines.append(f"Источник: {item.source}")
        lines.append(f"Ссылка: {item.link}")
        lines.append("")
    return "\n".join(lines).strip()


def _looks_like_no_answer(text: str) -> bool:
    lowered = text.lower()
    patterns = [
        "нет ответа",
        "не сказано",
        "не указано",
        "не упоминается",
        "не говорится",
        "не сообщается",
        "нет информации",
        "нет данных",
        "отсутствует информация",
        "неизвестно",
    ]
    return any(pattern in lowered for pattern in patterns)


def _parse_question_payload(text: str) -> QuestionAnswer | None:
    payload = _extract_json(text)
    if not payload:
        return None

    found = _normalize_bool(payload.get("answer_found"))
    answer = str(payload.get("answer") or "").strip()
    missing = str(payload.get("missing") or "").strip()
    sources = normalize_direct_links(_normalize_list(payload.get("sources")))

    if found and not answer:
        found = False

    return QuestionAnswer(
        found=found,
        answer=answer,
        missing=missing,
        sources=sources,
        raw_response=text,
    )


def _format_sources(sources: list[str]) -> str:
    if not sources:
        return ""
    lines = ["Источники:"]
    for idx, source in enumerate(sources, start=1):
        lines.append(f"{idx}. {source}")
    return "\n".join(lines)


def _ensure_assumption_prefix(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return "Предположение: ответ зависит от деталей, которых нет в доступных источниках."
    lowered = cleaned.lower()
    if lowered.startswith("предположение"):
        return cleaned
    return f"Предположение: {cleaned}"


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
    sources = normalize_direct_links(_normalize_list(payload.get("sources")))

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

    payload = _parse_question_payload(response_text)
    if payload and payload.found and payload.answer:
        return payload.answer

    if not payload and not _looks_like_no_answer(response_text):
        return response_text.strip()

    query = question.strip()
    if len(query) > 160:
        query = query[:160].rstrip()

    try:
        items = fetch_top_news(query, limit=3)
    except Exception:
        items = []

    if items:
        search_prompt = _build_search_prompt(question, items)
        try:
            search_response = send_prompt_to_gigachat(search_prompt)
        except GigaChatError as exc:
            raise RuntimeError(f"GigaChat request failed: {exc}") from exc

        search_payload = _parse_question_payload(search_response)
        if search_payload and search_payload.found and search_payload.answer:
            sources = search_payload.sources or normalize_direct_links(
                [item.link for item in items]
            )
            formatted_sources = _format_sources(sources)
            if formatted_sources:
                return f"{search_payload.answer}\n\n{formatted_sources}"
            return search_payload.answer

        if not search_payload and not _looks_like_no_answer(search_response):
            sources = normalize_direct_links([item.link for item in items])
            formatted_sources = _format_sources(sources)
            if formatted_sources:
                return f"{search_response.strip()}\n\n{formatted_sources}"
            return search_response.strip()

    assumption_prompt = _build_assumption_prompt(news_text, question)
    try:
        assumption_response = send_prompt_to_gigachat(assumption_prompt)
    except GigaChatError as exc:
        raise RuntimeError(f"GigaChat request failed: {exc}") from exc

    assumption_text = _ensure_assumption_prefix(assumption_response)
    if payload and payload.missing:
        return (
            "Прямого ответа в новости и дополнительных источниках не найдено.\n"
            f"Не хватает: {payload.missing}\n"
            f"{assumption_text}"
        )
    return (
        "Прямого ответа в новости и дополнительных источниках не найдено.\n"
        f"{assumption_text}"
    )
