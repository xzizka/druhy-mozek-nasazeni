"""Přepis hlasovek (Telegram krok 3) přes Whisper-kompatibilní STT API.

Samostatný modul, ne přes LiteLLM: LiteLLM transkripci samo neumí, jen by
proxovalo k dalšímu poskytovateli (nový účet, nový secret) — stejná úvaha
jako u storage.py a S3. STT_BASE_URL/STT_MODEL nejsou pevně zadrátované,
aby výměna poskytovatele byla konfigurační změna, ne zásah do kódu.
Výchozí hodnoty cílí na OpenRouter (viz config.py) — STT_API_KEY se v
nasazení mountuje ze stejného secretu jako OPENROUTER_API_KEY, žádný nový
účet ani secret.

Klíč jde v Authorization hlavičce (OpenAI-kompatibilní `/audio/transcriptions`),
ne v URL jako u Telegram Bot API — past s `raise ... from None` uvnitř
except bloku (viz telegram.py) se sem proto nevztahuje.
"""
from __future__ import annotations

import httpx

from . import config


def enabled() -> bool:
    return bool(config.STT_API_KEY)


def transcribe(data: bytes, filename: str = "hlasovka.ogg") -> str:
    """Přepíše zvuk na text. Vyhodí RuntimeError se čitelnou zprávou při chybě.

    Response_format se NEPOSÍLÁ - necháváme OpenAI-kompatibilní výchozí JSON
    (`{"text": ...}`), protože textový režim je napříč poskytovateli míň
    spolehlivě podporovaný (Groq ho umí, u OpenRouteru neověřeno) a JSON je
    univerzálnější sázka bez ztráty funkčnosti.
    """
    form = {"model": config.STT_MODEL}
    if config.STT_LANGUAGE:
        form["language"] = config.STT_LANGUAGE
    try:
        r = httpx.post(
            config.STT_BASE_URL,
            headers={"Authorization": "Bearer %s" % config.STT_API_KEY},
            data=form,
            files={"file": (filename, data, "audio/ogg")},
            timeout=config.STT_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise RuntimeError("STT: chyba spojení") from e
    if r.status_code != 200:
        raise RuntimeError("STT: HTTP %d" % r.status_code)
    return r.json()["text"].strip()
