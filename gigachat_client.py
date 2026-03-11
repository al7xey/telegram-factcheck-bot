"""Minimal GigaChat API client using requests."""

from __future__ import annotations

import time
import uuid
from typing import Any

import requests

from config import config


class GigaChatError(RuntimeError):
    """Raised when GigaChat API requests fail."""


auth_token: str | None = None
expires_at: float = 0.0
SESSION = requests.Session()


def _get_verify_setting() -> bool | str:
    if config.ca_bundle_path:
        return config.ca_bundle_path
    return config.verify_ssl


def _get_access_token() -> str:
    global auth_token, expires_at

    # Reuse the token while it's valid (leave 60s safety margin).
    if auth_token and time.time() < expires_at - 60:
        return auth_token

    headers = {
        "Authorization": f"Basic {config.gigachat_api_key}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {"scope": config.gigachat_scope}

    response = SESSION.post(
        config.gigachat_auth_url,
        headers=headers,
        data=data,
        timeout=config.request_timeout,
        verify=_get_verify_setting(),
    )

    if response.status_code != 200:
        raise GigaChatError(
            f"Auth failed ({response.status_code}): {response.text.strip()}"
        )

    payload = response.json()
    token = payload.get("access_token")
    token_expires = payload.get("expires_at")

    if not token:
        raise GigaChatError("Auth response did not include access_token")

    auth_token = token
    if isinstance(token_expires, (int, float)):
        expires_at = float(token_expires)
    else:
        # Fallback to 25 minutes if the API did not return expiration.
        expires_at = time.time() + 25 * 60

    return auth_token


def _extract_content(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices") or []
    if choices:
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if content:
            return content
    return payload.get("content") or payload.get("text")


def send_prompt_to_gigachat(text: str) -> str:
    """Send a prompt to GigaChat and return the assistant text response."""
    token = _get_access_token()
    endpoint = f"{config.gigachat_base_url.rstrip('/')}/chat/completions"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload: dict[str, Any] = {
        "model": config.gigachat_model,
        "temperature": config.gigachat_temperature,
        "top_p": config.gigachat_top_p,
        "messages": [
            {"role": "user", "content": text},
        ],
    }

    if config.gigachat_max_tokens > 0:
        payload["max_tokens"] = config.gigachat_max_tokens

    response = SESSION.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=config.request_timeout,
        verify=_get_verify_setting(),
    )

    if response.status_code != 200:
        raise GigaChatError(
            f"Chat request failed ({response.status_code}): {response.text.strip()}"
        )

    content = _extract_content(response.json())
    if not content:
        raise GigaChatError("GigaChat response did not include content")

    return content
