"""Project configuration and environment loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv


ENV_PATH = Path(__file__).with_name(".env")
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    gigachat_api_key: str
    gigachat_scope: str
    gigachat_base_url: str
    gigachat_auth_url: str
    gigachat_model: str
    gigachat_max_tokens: int
    gigachat_temperature: float
    gigachat_top_p: float
    request_timeout: int
    min_text_chars: int
    min_text_words: int
    verify_ssl: bool
    ca_bundle_path: str | None


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_config() -> Config:
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    gigachat_api_key = os.getenv("GIGACHAT_API_KEY", "").strip()

    if not telegram_bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing in environment variables."
        )
    if not gigachat_api_key:
        raise RuntimeError(
            "GIGACHAT_API_KEY is missing in environment variables."
        )

    return Config(
        telegram_bot_token=telegram_bot_token,
        gigachat_api_key=gigachat_api_key,
        gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip(),
        gigachat_base_url=os.getenv(
            "GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru/api/v1"
        ).strip(),
        gigachat_auth_url=os.getenv(
            "GIGACHAT_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        ).strip(),
        gigachat_model=os.getenv("GIGACHAT_MODEL", "GigaChat-2").strip(),
        gigachat_max_tokens=_get_int("GIGACHAT_MAX_TOKENS", 350),
        gigachat_temperature=_get_float("GIGACHAT_TEMPERATURE", 0.1),
        gigachat_top_p=_get_float("GIGACHAT_TOP_P", 0.8),
        request_timeout=_get_int("REQUEST_TIMEOUT", 45),
        min_text_chars=_get_int("MIN_TEXT_CHARS", 30),
        min_text_words=_get_int("MIN_TEXT_WORDS", 6),
        verify_ssl=_get_bool("GIGACHAT_VERIFY_SSL", True),
        ca_bundle_path=os.getenv("GIGACHAT_CA_BUNDLE", "").strip() or None,
    )


config = load_config()
