"""Telegram můstek: textové zprávy <-> core.capture / core.search+core.answer.

KROK 1 — jen text, kanál a routing. KROK 2 — denní otázka na plánovači
(`daily_loop`), aby nemusela chodit ručně. KROK 3 — hlasovky: STT přes
app/stt.py (Whisper-kompatibilní API), routing STEJNÝ jako u textu — hlasovka
je jen jiný zdroj textu, ne jiná větev rozhodování (reply/fresh se řeší až
po přepisu, viz `_je_odpoved_na_bota`).

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

P15 (2026-09-09) k tomu přidává dvě věci, obě navěšené na reply gesto
a na příkaz, ne na klasifikaci obsahu — tedy stejnou logikou jako výš:
`_otazka_dne_z_reply()` vytáhne z `reply_to_message` otázku dne a předá ji
`core.capture(otazka=)`, a `/denik [N] <dotaz>` vynutí odpověď nad celým
deníkem za N dnů.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from . import config, core, stt

log = logging.getLogger("kryton")

API = "https://api.telegram.org/bot%s/%s"


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ALLOWED_USER_ID)


def _call(method: str, http_timeout: float = 15, **params) -> dict:
    """`**params` jde jako JSON tělo requestu na Telegram (může obsahovat
    i klíč `timeout` — to je Telegramův vlastní parametr pro long polling,
    NEZAMĚŇOVAT s `http_timeout`, což je timeout HTTP klienta. Ten musí
    být delší, jinak httpx vyhodí chybu dřív, než Telegram stihne odpovědět.

    VŠECHNY výjimky z httpx se tady zachytávají a nahrazují vlastní zprávou.
    Telegram Bot API nese token PŘÍMO V URL (na rozdíl od LiteLLM, kde jde
    v Authorization hlavičce) — `r.raise_for_status()` i syrová httpx
    výjimka by ho vložily do textu chyby, a `log.exception()` v poll_loop()
    by ho pak zapsal do journalu při každém síťovém zádrhelu.

    RAISE MUSÍ BÝT MIMO `except` BLOK. `raise ... from None` sice potlačí
    VÝPIS chained výjimky, ale `__context__` v paměti pořád drží tu
    původní (s tokenem v URL) — ověřeno testem, který přesně tohle chytil.
    Jen po ÚPLNÉM opuštění except bloku (žádná aktivní obsluha výjimky)
    zůstane nová výjimka bez cizího __context__ i __cause__.
    """
    try:
        r = httpx.post(API % (config.TELEGRAM_BOT_TOKEN, method), json=params,
                       timeout=http_timeout)
    except httpx.HTTPError:
        r = None
    if r is None:
        raise RuntimeError("Telegram API %s: chyba spojeni" % method)
    if r.status_code != 200:
        raise RuntimeError("Telegram API %s: HTTP %d" % (method, r.status_code))
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError("Telegram API %s: %s" % (method, d))
    return d["result"]


def _zkrat(text: str, limit: int) -> str:
    """Usekne text tak, aby se i s poznámkou o zkrácení vešel do `limit`.

    Řeže na hranici slova, ale jen když ta hranice leží v poslední pětině
    povoleného úseku. U textu bez mezer (jedno dlouhé slovo, base64, kus
    JSONu) by hledání mezery uřízlo skoro všechno, a to je horší než
    useknout uprostřed slova.
    """
    poznamka = "\n\n⚠️ Zkráceno z %d znaků. Celá odpověď je v logu Krytona." % len(text)
    telo = limit - len(poznamka)
    if telo <= 0:
        # Limit menší než samotná poznámka — pak nemá smysl nic vysvětlovat.
        return text[:limit]
    usek = text[:telo]
    mezera = max(usek.rfind(" "), usek.rfind("\n"))
    if mezera >= telo * 0.8:
        usek = usek[:mezera]
    return usek.rstrip() + poznamka


def _send(chat_id: int, text: str) -> None:
    """Pošle PRÁVĚ JEDNU zprávu, nikdy seriál (P8 bod d).

    Dřív se text krájel po 4000 znacích ve `for` cyklu. Zacyklená odpověď
    z 2026-08-17 (~24 000 znaků) tak dorazila jako šest zpráv za sebou,
    mlčky — uživatel nedostal žádný signál, že je něco špatně. Je to tatáž
    kategorie jako „věrohodný nesmysl je horší než přiznané selhání", na
    které v tomhle projektu stojí rozhodnutí o fallbacích.

    **Při zkrácení se celá odpověď LOGUJE.** V Telegramu se nedohledá nic:
    `telegram.py` nepíše do `conversation` ani `message`, takže bez logu by
    useknutá část zmizela nadobro. Strop 1024 znaků je přísnější než
    pozorované maximum odpovědi (595), takže se to spustí i na delší
    legitimní odpovědi, ne jen na anomálie — o to víc na tom logu záleží.

    Prázdný text posílá `(prázdná odpověď)`. Dřív se pro něj neprovedl ani
    jeden průchod cyklem (`range(0, 0, 4000)` je prázdný rozsah), takže
    `or "(prázdná odpověď)"` uvnitř byl mrtvý kód a uživatel dostal ticho —
    nerozeznatelné od nefunkčního bota. Prázdná odpověď přitom reálně
    nastává, viz P1: model spotřebuje celý strop na uvažování a vrátí
    prázdný content.
    """
    if not text.strip():
        _call("sendMessage", chat_id=chat_id, text="(prázdná odpověď)")
        return
    limit = config.TELEGRAM_MAX_ZNAKU
    if len(text) <= limit:
        _call("sendMessage", chat_id=chat_id, text=text)
        return
    log.warning("telegram: odpoved %d znaku presahla limit %d, zkracuji; "
                "cela odpoved: %s", len(text), limit, text)
    _call("sendMessage", chat_id=chat_id, text=_zkrat(text, limit))


def _je_odpoved_na_bota(msg: dict) -> bool:
    rep = msg.get("reply_to_message")
    return bool(rep and rep.get("from", {}).get("is_bot"))


def _otazka_dne_z_reply(msg: dict) -> str | None:
    """Text otázky dne, na kterou tahle zpráva odpovídá. Jinak None. (P15)

    BERE SE Z GESTA, NE Z PAMĚTI PROCESU, a to je celý vtip: `reply_to_message`
    nese plné tělo zprávy, na kterou uživatel swipnul, takže otázku známe
    přesně — i když odpoví na otázku ze včerejška, i po restartu kontejneru.
    Pamatovat si „co jsem naposled poslal" (jako `_posledni_odeslano`) by
    obojí zkazilo, a náhodný výběr z třiceti variant znamená, že špatný
    odhad by přiřadil odpovědi CIZÍ otázku — horší než žádnou.

    Prefix se kontroluje proto, že reply na bota může být i reply na
    obyčejnou Krytonovu ODPOVĚĎ. Tam žádná otázka dne není a vrací se None,
    takže se do deníku nic nepřilepí.
    """
    rep = msg.get("reply_to_message") or {}
    t = (rep.get("text") or "").strip()
    if t.startswith(OTAZKA_DNE_PREFIX):
        return t[len(OTAZKA_DNE_PREFIX):].strip() or None
    return None


# `/denik`, `/denik 90`, `/denik@mujbot 90` — Telegram u příkazů v grupách
# připojuje `@jmeno_bota`, takže se to musí připustit i u soukromého chatu.
_DENIK_PRIKAZ = re.compile(r"^/denik(?:@\w+)?(?:\s+(\d{1,4}))?\b\s*", re.I)


def _rozborem_prikazu(text: str) -> tuple[str, int | None]:
    """`(dotaz, denik_dni)` z textu zprávy. (P15)

    `denik_dni` je `None`, když příkaz nepadl — tedy „rozhodni heuristikou",
    což je přesně ta trojice stavů, kterou popisuje `core.answer()`.
    Explicitní cesta existuje proto, že heuristika nad volným textem měří
    formulaci, ne chování; tady se nehádá nic.
    """
    m = _DENIK_PRIKAZ.match(text)
    if not m:
        return text, None
    dni = int(m.group(1)) if m.group(1) else config.DENIK_DEFAULT_DNI
    return text[m.end():].strip(), dni


def _stahni_soubor(file_id: str) -> bytes:
    """Stáhne soubor z Telegramu podle file_id.

    Odkaz na stažení NESE TOKEN V URL stejně jako volání API v `_call` —
    stejná past, stejná oprava: chyba se zabalí do RuntimeError MIMO except
    blok, aby v paměti nezůstal __context__ s URL obsahující token.
    """
    info = _call("getFile", file_id=file_id)
    url = "https://api.telegram.org/file/bot%s/%s" % (
        config.TELEGRAM_BOT_TOKEN, info["file_path"])
    try:
        r = httpx.get(url, timeout=30)
    except httpx.HTTPError:
        r = None
    if r is None:
        raise RuntimeError("Telegram file download: chyba spojeni")
    if r.status_code != 200:
        raise RuntimeError("Telegram file download: HTTP %d" % r.status_code)
    return r.content


def _prepis_hlasovky(chat_id: int, voice: dict) -> str | None:
    """Stáhne a přepíše hlasovku. Vrací `None`, když se má handler ukončit
    beze zpracování (chyba, nebo STT vypnuté) — chybová zpráva už uživateli
    odešla, volající se s tím dál nemá zdržovat."""
    if not stt.enabled():
        _send(chat_id, "Hlas zatím neumím přepsat — STT klíč není nastavený.")
        return None
    try:
        data = _stahni_soubor(voice["file_id"])
        text = stt.transcribe(data)
    except Exception:
        log.exception("telegram: prepis hlasovky selhal")
        _send(chat_id, "Přepis hlasovky selhal, zkus to prosím znovu nebo napiš text.")
        return None
    log.info("telegram: hlasovka prepsana (%d znaku)", len(text))
    return text


def _handle_message(msg: dict) -> None:
    frm = msg.get("from", {})
    chat_id = msg.get("chat", {}).get("id")
    if frm.get("id") != config.TELEGRAM_ALLOWED_USER_ID:
        log.warning("telegram: zprava od neautorizovaneho uzivatele %r, ignoruji",
                    frm.get("id"))
        return

    voice = msg.get("voice")
    if voice:
        text = _prepis_hlasovky(chat_id, voice)
        if text is None:
            return
    else:
        text = (msg.get("text") or "").strip()

    if not text:
        # Foto, sticker, dokument... zatim nepokryto.
        _send(chat_id, "Zatím umím jen text a hlas — foto/sticker/dokument zatím ne.")
        return

    if _je_odpoved_na_bota(msg):
        otazka = _otazka_dne_z_reply(msg)
        rel = core.capture(text, kanal="telegram", otazka=otazka)
        log.info("telegram: zaznamenano do %s (%d znaku, otazka dne: %s)",
                 rel, len(text), "ano" if otazka else "ne")
        _send(chat_id, "Zaznamenáno: %s" % rel)
        return

    # P15: `/denik [N] otázka` vynutí deníkovou cestu na N dnů. Bez příkazu
    # zůstává `denik_dni=None`, tedy „rozhodni heuristikou" — příkaz je
    # jistota pro případ, kdy heuristika netrefí, ne povinnost.
    dotaz, denik_dni = _rozborem_prikazu(text)
    if denik_dni is not None and not dotaz:
        _send(chat_id, "Napiš i otázku, třeba: /denik 90 jaký je sentiment "
                       "mých zápisů?")
        return

    try:
        res = core.search(dotaz)
        odp = core.answer(dotaz, res["results"], denik_dni=denik_dni)
    except Exception as e:
        # Text výjimky se uživateli NEPOSÍLÁ. Detail patří do logu (kam ho
        # dá `log.exception` i s tracebackem), do chatu patří srozumitelná
        # věta. Dřív se posílalo "Dotaz selhal: %s" % e, což je jednak
        # nesrozumitelné (uživatel dostal `timed out` nebo kus JSONu od
        # LiteLLM), jednak zbytečně vynáší vnitřnosti ven z brainu.
        log.exception("telegram: dotaz selhal")
        core.zaznamenej("telegram", text, None, chyba=repr(e))
        _send(chat_id, "Na tenhle dotaz se mi teď nepodařilo odpovědět. "
                       "Zkus to prosím za chvíli znovu.")
        return
    # Zápis je PŘED odesláním, aby se stopa uložila i tehdy, když spadne
    # Telegram API — a naopak `zaznamenej()` nikdy nevyhodí výjimku, takže
    # rozbitá databáze nezabrání odeslání. Ani jedna z těch dvou věcí nesmí
    # shodit tu druhou.
    #
    # Ukládá se `text`, ne `dotaz`: do auditní stopy patří to, co uživatel
    # SKUTEČNĚ napsal, včetně `/denik 90`. Použité období se neztratí, jde
    # do stopy jako `denik_dni`.
    core.zaznamenej("telegram", text, odp, res["results"])
    _send(chat_id, odp.text)


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



# --- KROK 2: denní otázka -----------------------------------------------
# Malá rotující sada, ne LLM generování - jednoduché, bez ceny a latence
# navíc. „Chytřejší" otázky (podle mezer v korpusu) jsou dalsí krok, az
# tohle overi, ze samotne planovani ma smysl.
#
# ROZŠÍŘENO 2026-09-09 ze šesti na 31. Šest otázek při jedné denně znamená
# každou pětkrát za měsíc; 31 dá přibližně měsíční rotaci.
#
# Co drží tvar téhle sady (ať se při dalším přidávání nerozpadne):
#
#   1. DVOJDÍLNÁ OTÁZKA. Za otázkou následuje pobídka, která tlačí za tu
#      první samozřejmou odpověď („— a co by sis o tom chtěl/a pamatovat
#      i za měsíc", „tobě, ne jen na papíře"). Bez ní vzniká jednořádková
#      odpověď, ze které se za měsíc nic nevyčte.
#   2. ODPOVĚĎ MÁ BÝT DOHLEDATELNÁ. Je to vstup do RAGu, ne nálada do
#      šuplíku — otázky proto míří na konkrétní věci, jména, čísla
#      a rozhodnutí, ne na obecné pocity. Několik otázek na stav a energii
#      tu je záměrně (deník se takhle reálně používá, viz dotaz na
#      sentiment z P15), ale i ty se ptají na „co se v tu chvíli dělo".
#   3. TYKÁNÍ a rodově neutrální tvary („chtěl/a") — stejně jako
#      v původní šestici.
#
# Sada je schválně tematicky pestrá, ale výběr je náhodný, takže rovnoměrné
# pokrytí témat NEZARUČUJE. Kdyby to někdy vadilo, správný krok je rotace
# po tématech, ne přidávání dalších otázek do jednoho pytle.
DENNI_OTAZKY = [
    # --- co se dnes povedlo, pokazilo, rozhodlo ------------------------
    "Co se ti dnes povedlo vyřešit nebo pochopit — a co by sis o tom "
    "chtěl/a pamatovat i za měsíc, až to vyprchá z hlavy?",
    "Co tě dnes nejvíc zaskočilo nebo tě přinutilo změnit názor?",
    "Na čem dnes pracuješ a proč je to důležité — tobě, ne jen na papíře?",
    "Jakou chybu jsi dnes udělal/a a co z ní plyne pro příště?",
    "Co jsi se dnes naučil/a nového — technicky, nebo o sobě?",
    "Co bys chtěl/a mít zapsané, kdyby sis zítra na dnešek nevzpomněl/a?",
    "Jaké rozhodnutí jsi dnes udělal/a a co tě k němu přesvědčilo? Napiš "
    "i to, co jsi zvažoval/a a nevybral/a.",
    "Co ti dnes nefungovalo tak, jak jsi čekal/a — a čím se to nakonec "
    "vysvětlilo?",
    "Co dnes fungovalo na první pokus? Napiš proč, ať to umíš zopakovat.",
    "Co bys dnes udělal/a jinak, kdybys ten den začínal/a znovu?",
    "Co ses dnes dozvěděl/a o něčem, o čem sis myslel/a, že to už znáš?",
    "Jaká otázka ti dnes zůstala nezodpovězená a koho nebo co by bylo "
    "potřeba, abys ji zodpověděl/a?",

    # --- čas, pozornost, odkládání -------------------------------------
    "Na čem jsi dnes strávil/a nejvíc času — a stálo to za to?",
    "Co jsi dnes odložil/a na jindy a proč zrovna tohle?",
    "Na čem ti dnes doopravdy záleželo a kolik času jsi tomu dal/a? "
    "Jestli se ta dvě čísla rozcházejí, napiš i to.",
    "Co děláš pořád stejně, i když víš, že to nefunguje?",
    "Co tě dnes zaujalo natolik, že jsi na to myslel/a i po práci?",
    "Co jsi dnes viděl/a, přečetl/a nebo slyšel/a a chceš se k tomu vrátit? "
    "Napiš i kde to najdeš.",

    # --- lidé ----------------------------------------------------------
    "S kým jsi dnes mluvil/a a co si z toho rozhovoru chceš pamatovat?",
    "Řekl ti dnes někdo něco, co stojí za zapamatování — i kdyby jen "
    "proto, že s tím nesouhlasíš?",
    "Komu jsi dnes něco slíbil/a a do kdy to chceš splnit?",
    "Udělal pro tebe dnes někdo něco, co si zaslouží nezapomenout?",
    "Naštval tě dnes někdo? Napiš i to, co ho k tomu podle tebe vedlo — "
    "za měsíc se to bude číst jinak.",
    "Viděl/a jsi dnes někoho, kdo něco umí lépe než ty? Co konkrétního "
    "sis z toho vzal/a?",

    # --- stav, energie, tělo -------------------------------------------
    "Co ti dnes sebralo nejvíc energie — byla to práce, nebo lidi kolem?",
    "Co ti dnes energii naopak dodalo, i kdyby to byla maličkost?",
    "Kdy ti bylo dnes nejlíp a co se v tu chvíli dělo?",
    "Jak jsi dnes spal/a a poznal/a jsi to na sobě během dne?",
    "Cítil/a jsi dnes něco, co se poslední dobou opakuje?",

    # --- delší horizont a doložitelnost --------------------------------
    "Co se od minulého měsíce změnilo tak, že by si toho tehdejší ty "
    "nevšiml/a?",
    "Stalo se dnes něco, co bys mohl/a později potřebovat doložit? Napiš "
    "i podrobnosti, které se teď zdají nepodstatné — datum, kdo tam byl, "
    "co přesně padlo.",
]

# Index naposled poslané otázky. Jen v paměti, viz docstring `_vyber_otazku`.
_posledni_otazka = [None]


def _vyber_otazku() -> str:
    """Náhodná otázka dne, ale nikdy dvakrát za sebou tatáž.

    `random.choice()` sám o sobě může poslat tutéž otázku dva dny po sobě.
    Při šesti otázkách to bylo 17 % dnů, což je jasně vidět; při třiceti
    jsou to 3 %, ale opakování hned druhý den nepůsobí jako náhoda, nýbrž
    jako porucha bota — a to je horší než ta nižší pravděpodobnost.

    Stav žije JEN V PAMĚTI, stejně jako `_posledni_odeslano`: po restartu
    se může jedno opakování protlačit, a to je u osobního bota levnější než
    perzistence. Otázky se od P15 ukládají do deníku, takže historie
    v markdownu existuje — čtení zpátky by ale svázalo plánovač s diskem
    kvůli kosmetice, a za to to nestojí.
    """
    if len(DENNI_OTAZKY) < 2:
        return DENNI_OTAZKY[0]
    i = random.choice([j for j in range(len(DENNI_OTAZKY))
                       if j != _posledni_otazka[0]])
    _posledni_otazka[0] = i
    return DENNI_OTAZKY[i]
# Prefix zprávy s otázkou dne. JE TO SOUČÁST KONTRAKTU, ne kosmetika:
# `_otazka_dne_z_reply()` podle něj pozná, že uživatel odpověděl na otázku
# dne, a ne na obyčejnou Krytonovu odpověď. Kdo ho změní, musí počítat s tím,
# že u zpráv odeslaných PŘED změnou se otázka k odpovědi už nepřipojí.
OTAZKA_DNE_PREFIX = "🗓️ Otázka dne: "

_posledni_odeslano = [None]  # datetime.date | None; jen v pameti, viz docstring poll_loop


def _mel_bych_poslat_otazku(now: datetime) -> bool:
    if now.hour < config.TELEGRAM_DAILY_QUESTION_HOUR_UTC:
        return False
    return _posledni_odeslano[0] != now.date()


def _posli_otazku_dne() -> None:
    text = OTAZKA_DNE_PREFIX + _vyber_otazku()
    _send(config.TELEGRAM_ALLOWED_USER_ID, text)
    _posledni_odeslano[0] = datetime.now(timezone.utc).date()
    log.info("telegram: otazka dne odeslana")


def daily_loop() -> None:
    """Kontroluje co 10 min, ne kazdou sekundu - staci, cas ve zprave neni
    kriticky presny. Restart uprostred dne muze v nejhorsim pripade poslat
    otazku podruhé - `_posledni_odeslano` zije jen v pameti, stejna uvaha
    jako u offsetu v poll_loop (osobni bot, nizka cena chyby)."""
    while True:
        try:
            if _mel_bych_poslat_otazku(datetime.now(timezone.utc)):
                _posli_otazku_dne()
        except Exception:
            log.exception("telegram: denni otazka selhala, zkousim znovu pozdeji")
        time.sleep(600)


def start_background() -> None:
    if not enabled():
        log.info("telegram: TELEGRAM_BOT_TOKEN nebo TELEGRAM_ALLOWED_USER_ID "
                 "chybí, můstek vypnutý")
        return
    threading.Thread(target=poll_loop, daemon=True, name="telegram-poll").start()
    threading.Thread(target=daily_loop, daemon=True, name="telegram-daily").start()
    log.info("telegram: poll loop i denni otazka spusteny na pozadi")
