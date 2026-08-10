"""Telegram můstek: textové zprávy <-> core.capture / core.search+core.answer.

KROK 1 z „jedno po druhém" — jen text. Denní otázka a hlas jsou další kroky,
tenhle modul je jen kanál a routing.

Long polling, ne webhook — brain nemá (a nemusí mít) veřejně dosažitelný
HTTPS endpoint, který by Telegram potřeboval zavolat. Polling potřebuje jen
odchozí spojení z brainu na Telegram.

Běží na pozadí ve vlastním vlákně (spuštěném z main.py při startu), ne
v asyncio smyčce FastAPI — volání do core.py jsou synchronní, stejně jako
zbytek Krytona, a vlákno se stejně jako HTTP handlery bezpečně dělí
o `db`'s connection pool (psycopg_pool je na to stavěný).

BEZPEČNOST: odpovídá se VÝHRADNĚ `config.TELEGRAM_ALLOWED_USER_ID`, ověřeno
na KAŽDÉ příchozí zprávě, ne jen jednou při startu. Bez tohoto filtru je bot
otevřenými dveřmi do poznámek pro kohokoliv, kdo ho na Telegramu najde.

ROZLIŠENÍ PŘÍKAZ/ODPOVĚĎ beze klasifikace záměru z obsahu: zpráva, která je
Telegram-reply na zprávu OD BOTA, je „odpověď" -> `core.capture()`. Čerstvá
zpráva bez reply je „příkaz/dotaz" -> `core.search()` + `core.answer()`.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from . import config, core

log = logging.getLogger("kryton")

API = "https://api.telegram.org/bot%s/%s"


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ALLOWED_USER_ID)


def _call(method: str, http_timeout: float = 15, **params) -> dict:
    """`**params` jde jako JSON tělo requestu na Telegram (může obsahovat
    i klíč `timeout` — to je Telegramův vlastní parametr pro long polling,
    NEZAMĚŇOVAT s `http_timeout`, což je timeout HTTP klienta. Ten musí
    být delší, jinak httpx vyhodí chybu dřív, než Telegram stihne odpovědět.
    """
    r = httpx.post(API % (config.TELEGRAM_BOT_TOKEN, method), json=params,
                   timeout=http_timeout)
    r.raise_for_status()
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError("Telegram API %s: %s" % (method, d))
    return d["result"]


def _send(chat_id: int, text: str) -> None:
    # Telegram limit je 4096 znaků na zprávu; oříznutí na kusy je bezpečnější
    # než tvrdý pád na delší odpověď.
    for i in range(0, len(text), 4000):
        _call("sendMessage", chat_id=chat_id, text=text[i:i + 4000] or "(prázdná odpověď)")


def _je_odpoved_na_bota(msg: dict) -> bool:
    rep = msg.get("reply_to_message")
    return bool(rep and rep.get("from", {}).get("is_bot"))


def _handle_message(msg: dict) -> None:
    frm = msg.get("from", {})
    chat_id = msg.get("chat", {}).get("id")
    if frm.get("id") != config.TELEGRAM_ALLOWED_USER_ID:
        log.warning("telegram: zprava od neautorizovaneho uzivatele %r, ignoruji",
                    frm.get("id"))
        return

    text = (msg.get("text") or "").strip()
    if not text:
        # Hlas, foto, sticker... KROK 1 umi jen text, dalsi kroky pribudou.
        _send(chat_id, "Zatím umím jen text — hlas přibude v dalším kroku.")
        return

    if _je_odpoved_na_bota(msg):
        rel = core.capture(text)
        log.info("telegram: zaznamenano do %s (%d znaku)", rel, len(text))
        _send(chat_id, "Zaznamenáno: %s" % rel)
        return

    try:
        res = core.search(text)
        odpoved, _model, _ms = core.answer(text, res["results"])
    except Exception as e:
        log.exception("telegram: dotaz selhal")
        _send(chat_id, "Dotaz selhal: %s" % e)
        return
    _send(chat_id, odpoved)


def _pocatecni_offset() -> int:
    """Zahodí, co se nastřádalo, dokud bot neběžel — jinak by po každém
    restartu přišel najednou celý starý backlog. Offset žije jen v paměti,
    pro osobního jednouživatelského bota to stačí (stejná úvaha jako
    u in-memory cache v LiteLLM: ztráta při restartu nikoho nebolí)."""
    try:
        stare = _call("getUpdates", http_timeout=10, timeout=0)
    except Exception:
        log.exception("telegram: pocatecni getUpdates selhalo, zacinam od 0")
        return 0
    return max((u["update_id"] for u in stare), default=-1) + 1


def poll_loop() -> None:
    offset = _pocatecni_offset()
    log.info("telegram: poll loop start, offset=%d", offset)
    while True:
        try:
            updates = _call("getUpdates", http_timeout=config.TELEGRAM_POLL_TIMEOUT + 10,
                            offset=offset, timeout=config.TELEGRAM_POLL_TIMEOUT)
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message")
                if msg:
                    _handle_message(msg)
        except Exception:
            log.exception("telegram: poll loop chyba, zkousim znovu za 5 s")
            time.sleep(5)


def start_background() -> None:
    if not enabled():
        log.info("telegram: TELEGRAM_BOT_TOKEN nebo TELEGRAM_ALLOWED_USER_ID "
                 "chybí, můstek vypnutý")
        return
    threading.Thread(target=poll_loop, daemon=True, name="telegram-poll").start()
    log.info("telegram: poll loop spuštěn na pozadí")
