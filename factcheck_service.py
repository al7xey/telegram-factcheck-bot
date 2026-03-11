"""Fact-checking service that orchestrates prompt creation and response parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List

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


@dataclass
class FactCheckResult:
    verdict: str
    confidence: int
    reasoning: List[str]
    sources: List[str]
    raw_response: str | None = None


@dataclass
class QuestionAnswer:
    found: bool
    answer: str
    missing: str
    sources: List[str]
    raw_response: str | None = None


# =========================
# PROMPTS
# =========================

def _build_prompt(news_text: str) -> str:
    return (
        "Ты фактчекер. Верни только JSON.\n"
        "Вердикт: Фейк, Скорее фейк, Не подтверждено, Скорее правда.\n\n"
        "{\n"
        "  \"verdict\": \"Скорее правда\",\n"
        "  \"confidence\": 78,\n"
        "  \"reasoning\": [\"пункт 1\",\"пункт 2\",\"пункт 3\"],\n"
        "  \"sources\": [\"https://example.com\"]\n"
        "}\n\n"
        f"Текст новости:\n{news_text}"
    )


def _build_question_prompt(news_text: str, question: str) -> str:
    return (
        "Ответь на вопрос используя только текст новости.\n"
        "Верни JSON:\n"
        "{\n"
        "  \"answer_found\": true,\n"
        "  \"answer\": \"текст\",\n"
        "  \"missing\": \"\"\n"
        "}\n\n"
        f"Новость:\n{news_text}\n\n"
        f"Вопрос:\n{question}"
    )


def _build_search_prompt(question: str, items: list[NewsItem]) -> str:

    search_items = _format_search_items(items)

    return (
        "Ответь на вопрос используя только результаты поиска.\n"
        "Верни JSON:\n"
        "{\n"
        "  \"answer_found\": true,\n"
        "  \"answer\": \"текст\",\n"
        "  \"missing\": \"\",\n"
        "  \"sources\": [\"https://site.com\"]\n"
        "}\n\n"
        f"Результаты:\n{search_items}\n\n"
        f"Вопрос:\n{question}"
    )


def _build_assumption_prompt(news_text: str, question: str) -> str:
    return (
        "Ответ не найден. Сделай осторожное предположение.\n"
        "Начни со слова 'Предположение:'.\n\n"
        f"Новость:\n{news_text}\n\n"
        f"Вопрос:\n{question}"
    )


# =========================
# JSON PARSING
# =========================

def _strip_code_fence(text: str) -> str:
    if "```" not in text:
        return text
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


def _try_decode_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            payload, _end = decoder.raw_decode(text[match.start() :])
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _extract_json(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    for candidate in (cleaned, _strip_code_fence(cleaned)):
        payload = _try_decode_json(candidate)
        if payload is not None:
            return payload
    return None


# =========================
# NORMALIZATION
# =========================

def _normalize_verdict(value: Any) -> str:

    if isinstance(value, str):

        value = value.strip()

        if value in VERDICT_MAP:
            return VERDICT_MAP[value]

        lower = value.lower()

        for k in VERDICT_MAP:
            if k.lower() == lower:
                return VERDICT_MAP[k]

    return "Не подтверждено"


def _normalize_confidence(value: Any) -> int:

    try:
        v = int(float(value))
    except Exception:
        v = 50

    return max(0, min(100, v))


def _normalize_bool(value: Any) -> bool:

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "да"}

    if isinstance(value, (int, float)):
        return value != 0

    return False


def _normalize_list(value: Any) -> list[str]:

    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]

    if isinstance(value, str) and value.strip():
        return [value.strip()]

    return []


# =========================
# HELPERS
# =========================

def _format_search_items(items: list[NewsItem]) -> str:

    lines = []

    for i, item in enumerate(items, 1):

        title = getattr(item, "title", "")
        summary = getattr(item, "summary", "")
        source = getattr(item, "source", "")
        link = getattr(item, "link", "")

        lines.append(f"{i}. Заголовок: {title}")

        if summary:
            lines.append(f"Описание: {summary}")

        if source:
            lines.append(f"Источник: {source}")

        if link:
            lines.append(f"Ссылка: {link}")

        lines.append("")

    return "\n".join(lines)


def _looks_like_no_answer(text: str) -> bool:

    patterns = [
        "нет информации",
        "не указано",
        "не сообщается",
        "неизвестно",
    ]

    lower = text.lower()

    return any(p in lower for p in patterns)


def _parse_question_payload(text: str) -> QuestionAnswer | None:

    payload = _extract_json(text)

    if not payload:
        return None

    found = _normalize_bool(payload.get("answer_found"))
    answer = str(payload.get("answer") or "").strip()
    missing = str(payload.get("missing") or "").strip()

    sources = normalize_direct_links(
        _normalize_list(payload.get("sources"))
    )

    if found and (not answer or _looks_like_no_answer(answer)):
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

    out = ["Источники:"]

    for i, s in enumerate(sources, 1):
        out.append(f"{i}. {s}")

    return "\n".join(out)


def _ensure_assumption_prefix(text: str) -> str:

    text = text.strip()

    if not text:
        return "Предположение: недостаточно информации."

    if not text.lower().startswith("предположение"):
        return f"Предположение: {text}"

    return text


# =========================
# MAIN LOGIC
# =========================

def analyze_news(news_text: str) -> FactCheckResult:

    prompt = _build_prompt(news_text)

    try:
        response = send_prompt_to_gigachat(prompt)
    except GigaChatError as e:
        raise RuntimeError(f"GigaChat error: {e}")

    payload = _extract_json(response)

    if not payload:

        return FactCheckResult(
            verdict="Не подтверждено",
            confidence=50,
            reasoning=[
                "Ответ модели не JSON",
                "Факты не подтверждены",
                "Проверьте источники"
            ],
            sources=[],
            raw_response=response
        )

    verdict = _normalize_verdict(payload.get("verdict"))
    confidence = _normalize_confidence(payload.get("confidence"))
    reasoning = _normalize_list(payload.get("reasoning"))
    sources = normalize_direct_links(_normalize_list(payload.get("sources")))

    return FactCheckResult(
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
        sources=sources,
        raw_response=response
    )


def answer_question(news_text: str, question: str) -> str:

    prompt = _build_question_prompt(news_text, question)

    try:
        response = send_prompt_to_gigachat(prompt)
    except GigaChatError as e:
        raise RuntimeError(f"GigaChat error: {e}")

    payload = _parse_question_payload(response)

    if payload and payload.found and payload.answer:
        return payload.answer

    # =====================
    # NEWS SEARCH
    # =====================

    cleaned_question = question.strip()
    search_query = (
        cleaned_question if len(cleaned_question) >= 8 else news_text[:200]
    )

    try:
        items = fetch_top_news(search_query, limit=3)
    except Exception as e:
        print("News fetch error:", e)
        items = []

    if items:

        search_prompt = _build_search_prompt(question, items)

        try:
            search_response = send_prompt_to_gigachat(search_prompt)
        except GigaChatError as e:
            raise RuntimeError(f"GigaChat error: {e}")

        search_payload = _parse_question_payload(search_response)

        if (
            search_payload
            and search_payload.found
            and search_payload.answer
        ):

            sources = search_payload.sources

            if not sources:
                sources = normalize_direct_links(
                    [i.link for i in items if getattr(i, "link", None)]
                )

            sources_text = _format_sources(sources)

            if sources_text:
                return f"{search_payload.answer}\n\n{sources_text}"

            return search_payload.answer

    # =====================
    # ASSUMPTION
    # =====================

    prompt = _build_assumption_prompt(news_text, question)

    try:
        response = send_prompt_to_gigachat(prompt)
    except GigaChatError as e:
        raise RuntimeError(f"GigaChat error: {e}")

    return _ensure_assumption_prefix(response)
