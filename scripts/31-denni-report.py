#!/usr/bin/env python3
"""Denní report provozu + alerty na invarianty, které už jednou selhaly.

PROČ TENHLE SKRIPT EXISTUJE
===========================
P8 byla čtyřdenní tichá degradace: big-pickle měl od 13. 8. vyčerpanou
kvótu, všechno odpovídala `gemma:free` a nikdo to neviděl. Zásadní na tom
je, že **data k odhalení byla celou dobu v databázi.** Jeden `GROUP BY` nad
`LiteLLM_SpendLogs` ukazoval 160 volání aliasu, který se za normálního
provozu nemá zavolat ani jednou, s průměrnou latencí 56 sekund. Nechybělo
úložiště ani nástroj — chyběl pohled na to, co už bylo zapsané.

Tenhle skript je ten pohled. Nesbírá nová data; čte, co Kryton a LiteLLM
ukládají tak jako tak, a hlásí šest věcí, o kterých z incidentů víme, že
znamenají potíž.

CO SE HLÍDÁ A PROČ PRÁVĚ TOHLE
==============================
A1 FALLBACK   Kryton dostal odpověď od jiného modelu, než chtěl (P8), nebo
              se v spend logu objevil `workhorse`/`backstop` pod klíčem
              `kryton`. **Kontroluje se PER KLÍČ, ne per alias**, a to je
              podstatné: 2026-08-24 se ukázalo, že `workhorse` volá i klíč
              `n8n` (User-Agent langchainjs-openai), tedy úplně legitimně
              a mimo fallback řetěz. Alert na alias bez ohledu na klíč by
              pípal každý den a za týden by se přestal číst.

A2 ODMÍTNUTÍ  Podíl odpovědí "v poznámkách jsem nic nenašel". Tohle je
              tripwire na P7-B: práh `ANSWER_MIN_RERANK` z P4 je o řád výš
              než skóre, které reranker dává temporálním dotazům, takže
              projde nula chunků. Kdyby tenhle report běžel 19. 8., ukázal
              by to týž den místo za den.

A3 FREE TIER  Volání modelu, jehož jméno končí na `:free`. Krok 1 z
              2026-08-18 odstranil `:free` ze VŠECH aliasů, protože denní
              strop 50 požadavků na účet způsobil jak špatně zaindexované
              jazyky (2026-08-08), tak P6. Jakýkoliv výskyt `:free` proto
              znamená, že se konfigurace vrátila, kam neměla.

A4 LATENCE    Volání nad prahem. Podpis P8 je `workhorse` s průměrem 56 s
              (zacyklená gemma na max_tokens=8000), podpis P6 je `backstop`
              se 41 s. Obojí je vidět dřív na latenci než na kvalitě.

A5 KLÍČE      Virtual key bez `max_budget` nebo bez `rpm_limit`, nebo klíč
              nad 80 % rozpočtu. Celý smysl virtual keys podle komentáře
              v `litellm-config.yaml` je, "aby rozbitá smyčka v jedné
              komponentě nevyčerpala kredit celé platformy" — klíč bez
              stropu tuhle ochranu nemá. (Nález z 2026-08-24: `n8n` je
              přesně takový.)

A6 ODSEKNUTÍ  Odpověď narazila na strop tokenů (`finish_reason=length`),
              seskupeno podle deníkového období. Jedno odseknutí uživatel
              vidí sám — je označené v odpovědi. Opakované znamená špatně
              nastavený strop nebo nefunkční pokyn k formátu v deníkovém
              bloku, a to se pozná jedině souhrnem. Doplněno s P15
              (2026-09-09), tedy později než A1-A5.

SPOUŠTĚNÍ
=========
Na brainu, jako root (potřebuje `podman exec` na kontejner postgresu):

    /root/deploy/scripts/31-denni-report.py              # posledních 24 h
    /root/deploy/scripts/31-denni-report.py --hodin 96   # od začátku P8

Návratový kód 0 = žádný alert, 1 = aspoň jeden. Díky tomu se to dá pověsit
na systemd timer s `OnFailure=`, nebo pustit ručně po nasazení.

VĚDOMÉ OMEZENÍ: report NEVIDÍ obsah promptů ani odpovědí od LiteLLM —
`turn_off_message_logging: true` je zapnuté schválně a tenhle skript ho
nemá důvod obcházet. Obsah odpovědí Krytona v `message` ale k dispozici je,
protože to je vlastní databáze na témž stroji, ne telemetrie ven.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

SEP = "\x1f"  # unit separator; v textech poznámek se nevyskytuje


def dotaz(db: str, sql: str) -> list[list[str]]:
    """Spustí SQL v kontejneru postgresu a vrátí řádky jako seznamy textů.

    Přes `podman exec`, ne přes psycopg: skript běží na hostiteli, kde
    žádné Python závislosti nejsou a instalovat je kvůli reportu by byla
    další pohyblivá součástka. Stejnou cestou jde `14-analytics-check.sh`.
    """
    r = subprocess.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-tA", "-F", SEP, "-c", sql, db],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("SQL selhalo (%s): %s" % (db, r.stderr.strip()[:300]))
    return [radek.split(SEP) for radek in r.stdout.strip().splitlines() if radek]


def cislo(s: str, jinak=0):
    if s == "":
        return jinak
    try:
        return float(s) if "." in s or "e" in s.lower() else int(s)
    except ValueError:
        return jinak


def tabulka(nadpis: str, hlavicka: list[str], radky: list[list[str]]) -> None:
    print("\n%s" % nadpis)
    if not radky:
        print("  (nic)")
        return
    sirky = [max(len(str(h)), *(len(str(r[i])) for r in radky))
             for i, h in enumerate(hlavicka)]
    print("  " + "  ".join(str(h).ljust(sirky[i]) for i, h in enumerate(hlavicka)))
    print("  " + "  ".join("-" * s for s in sirky))
    for r in radky:
        print("  " + "  ".join(str(c).ljust(sirky[i]) for i, c in enumerate(r)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hodin", type=int, default=24, help="okno reportu (24)")
    ap.add_argument("--prah-odmitnuti", type=float, default=0.30,
                    help="podíl odmítnutých odpovědí, nad kterým se hlásí (0.30)")
    ap.add_argument("--min-odpovedi", type=int, default=3,
                    help="pod tímhle počtem se podíl nehlásí, je to šum (3)")
    ap.add_argument("--prah-latence", type=int, default=30000,
                    help="ms, nad kterými se volání hlásí (30000)")
    a = ap.parse_args()
    okno = "%d hours" % a.hodin

    print("=" * 70)
    print("Druhý mozek — provozní report za posledních %d h" % a.hodin)
    print("=" * 70)

    alerty: list[str] = []

    # -----------------------------------------------------------------
    # Odpovědi Krytona
    # -----------------------------------------------------------------
    souhrn = dotaz("kryton", """
        SELECT count(*),
               count(*) FILTER (WHERE odmitnuto),
               count(*) FILTER (WHERE fallback),
               coalesce(round(avg(latency_ms))::text, ''),
               coalesce(round(max(latency_ms))::text, ''),
               coalesce(round(avg(max_rerank)::numeric, 4)::text, ''),
               count(*) FILTER (WHERE odmitnuto IS NULL),
               count(*) FILTER (WHERE denik_dni IS NOT NULL),
               count(*) FILTER (WHERE odseknuto)
        FROM message
        WHERE role = 'assistant' AND created_at > now() - interval '%s'
    """ % okno)[0]
    (celkem, odmitnuto, fallbacku, lat_avg, lat_max, rer_avg, bez_stopy,
     denikovych, odseknutych) = (
        cislo(souhrn[0]), cislo(souhrn[1]), cislo(souhrn[2]),
        souhrn[3] or "-", souhrn[4] or "-", souhrn[5] or "-", cislo(souhrn[6]),
        cislo(souhrn[7]), cislo(souhrn[8]))

    print("\nOdpovědi Krytona (web + Telegram + MCP)")
    print("  celkem            %d" % celkem)
    print("  odmítnutí         %d%s" % (
        odmitnuto, "  (%.0f %%)" % (100.0 * odmitnuto / celkem) if celkem else ""))
    print("  propad na fallback %d" % fallbacku)
    print("  latence ø / max   %s / %s ms" % (lat_avg, lat_max))
    print("  ø max_rerank      %s" % rer_avg)
    # P15. Podíl deníkových je zároveň kontrola falešných pozitivů heuristiky:
    # když deníkovou cestou jde většina dotazů, chytá `je_denikovy_prehled()`
    # i běžné faktografické dotazy a ředí je zápisy z deníku.
    print("  deníkovou cestou  %d%s" % (
        denikovych, "  (%.0f %%)" % (100.0 * denikovych / celkem) if celkem else ""))
    print("  odseknuto na stropu %d" % odseknutych)
    if bez_stopy:
        print("  bez stopy         %d  (zápis z doby před 2026-08-24 nebo selhaná odpověď)"
              % bez_stopy)

    tabulka("Podle kanálu", ["kanál", "odpovědí", "odmítnutí", "ø ms"],
            dotaz("kryton", """
                SELECT c.kanal, count(*),
                       count(*) FILTER (WHERE m.odmitnuto),
                       coalesce(round(avg(m.latency_ms))::text, '-')
                FROM message m JOIN conversation c ON c.id = m.conversation_id
                WHERE m.role = 'assistant' AND m.created_at > now() - interval '%s'
                GROUP BY 1 ORDER BY 2 DESC
            """ % okno))

    slabe = dotaz("kryton", """
        SELECT to_char(created_at, 'MM-DD HH24:MI'),
               coalesce(n_kandidatu::text, '-'),
               coalesce(n_nad_prahem::text, '-'),
               coalesce(round(max_rerank::numeric, 4)::text, '-'),
               left(content, 48)
        FROM message
        WHERE role = 'assistant' AND odmitnuto
          AND created_at > now() - interval '%s'
        ORDER BY created_at DESC LIMIT 10
    """ % okno)
    if slabe:
        tabulka("Odmítnuté odpovědi — na co se reranker nechytil",
                ["kdy", "kandid.", "nad prahem", "max_rerank", "odpověď"], slabe)

    # -----------------------------------------------------------------
    # LLM volání
    # -----------------------------------------------------------------
    volani = dotaz("litellm", """
        SELECT coalesce(k.key_alias, '(bez aliasu)'),
               coalesce(s.model_group, '-'), coalesce(s.model, '-'), s.status,
               count(*), coalesce(round(avg(s.request_duration_ms))::text, '-'),
               coalesce(round(max(s.request_duration_ms))::text, '-'),
               round(sum(s.spend)::numeric, 5)::text
        FROM "LiteLLM_SpendLogs" s
        LEFT JOIN "LiteLLM_VerificationToken" k ON k.token = s.api_key
        WHERE s."startTime" > now() - interval '%s'
        GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC
    """ % okno)
    tabulka("LLM volání", ["klíč", "alias", "model", "stav", "ks", "ø ms",
                           "max ms", "$"], volani)

    # -----------------------------------------------------------------
    # Alerty
    # -----------------------------------------------------------------
    # A1 — propad na fallback. Dva nezávislé zdroje: co si všiml Kryton sám
    # (`zkontroluj_fallback` porovná alias proti vrácenému modelu), a co je
    # vidět v spend logu. Ten druhý zachytí i propad při volání, které do
    # `message` nedojde — třeba z analytiky.
    if fallbacku:
        alerty.append("A1 FALLBACK: %d odpovědí přišlo od jiného modelu, než "
                      "měl `reasoning` vrátit (P8). Zkontroluj kvótu "
                      "primárního poskytovatele." % fallbacku)
    fb = dotaz("litellm", """
        SELECT s.model_group, s.model, count(*)
        FROM "LiteLLM_SpendLogs" s
        JOIN "LiteLLM_VerificationToken" k ON k.token = s.api_key
        WHERE k.key_alias = 'kryton'
          AND s.model_group IN ('workhorse', 'backstop')
          AND s."startTime" > now() - interval '%s'
        GROUP BY 1, 2
    """ % okno)
    for g, m, n in fb:
        alerty.append("A1 FALLBACK: klíč `kryton` volal %sx alias `%s` (%s) — "
                      "ten se za normálního provozu nemá zavolat ani jednou."
                      % (n, g, m))

    # A2 — tripwire na P7-B.
    if celkem >= a.min_odpovedi and odmitnuto / celkem > a.prah_odmitnuti:
        alerty.append("A2 ODMÍTNUTÍ: %d z %d odpovědí (%.0f %%) skončilo na "
                      "„v poznámkách jsem nic nenašel\u201c, práh je %.0f %%. "
                      "Podívej se na sloupec max_rerank v tabulce výš: když je "
                      "řádově pod ANSWER_MIN_RERANK, je to P7-B, ne prázdný "
                      "korpus."
                      % (odmitnuto, celkem, 100.0 * odmitnuto / celkem,
                         100.0 * a.prah_odmitnuti))

    # A3 — návrat free tieru.
    free = dotaz("litellm", """
        SELECT coalesce(model_group, '-'), model, count(*)
        FROM "LiteLLM_SpendLogs"
        WHERE model LIKE '%%:free' AND "startTime" > now() - interval '%s'
        GROUP BY 1, 2
    """ % okno)
    for g, m, n in free:
        alerty.append("A3 FREE TIER: alias `%s` volal %sx model `%s`. Přípona "
                      "`:free` byla 2026-08-18 odstraněna ze všech aliasů "
                      "(denní strop 50/účet, viz P6 a chybné jazyky 08-08)."
                      % (g, n, m))

    # A4 — latenční podpis P8 a P6.
    pomala = dotaz("litellm", """
        SELECT coalesce(model_group, '-'), coalesce(model, '-'), count(*),
               max(request_duration_ms)
        FROM "LiteLLM_SpendLogs"
        WHERE request_duration_ms > %d AND "startTime" > now() - interval '%s'
        GROUP BY 1, 2
    """ % (a.prah_latence, okno))
    for g, m, n, mx in pomala:
        alerty.append("A4 LATENCE: `%s` (%s) %sx nad %d ms, maximum %s ms. "
                      "Podpis P8 je 56 000 ms, podpis P6 41 000 ms."
                      % (g, m, n, a.prah_latence, mx))

    # A5 — klíč bez stropu. Není vázaný na okno: je to stav konfigurace.
    klice = dotaz("litellm", """
        SELECT key_alias, coalesce(max_budget::text, ''),
               coalesce(rpm_limit::text, ''), round(spend::numeric, 5)::text
        FROM "LiteLLM_VerificationToken"
        WHERE key_alias IS NOT NULL
    """)
    for alias, budget, rpm, spend in klice:
        chybi = [n for n, v in (("max_budget", budget), ("rpm_limit", rpm)) if not v]
        if chybi:
            alerty.append("A5 KLÍČE: `%s` nemá %s. Rozbitá smyčka pod tímhle "
                          "klíčem může vyčerpat kredit celé platformy."
                          % (alias, " ani ".join(chybi)))
        elif cislo(spend) > 0.8 * cislo(budget, 1e9):
            alerty.append("A5 KLÍČE: `%s` utratil %s z rozpočtu %s (nad 80 %%)."
                          % (alias, spend, budget))

    # A6 — odseknutá odpověď (P15). Uživatel varování v textu odpovědi vidí,
    # ale jen u toho jednoho dotazu; opakované odsekávání znamená, že je
    # špatně nastavený strop nebo že pokyn k formátu v deníkovém bloku
    # nefunguje, a to se pozná jedině souhrnem.
    odseknute = dotaz("kryton", """
        SELECT coalesce(denik_dni::text, '-'), count(*), max(length(content))
        FROM message
        WHERE odseknuto AND created_at > now() - interval '%s'
        GROUP BY 1
        ORDER BY 2 DESC
    """ % okno)
    for dni, n, znaku in odseknute:
        alerty.append("A6 ODSEKNUTÍ: %sx odpověď narazila na strop tokenů "
                      "(deníkové období %s dnů, nejdelší %s znaků). Zužte "
                      "období, nebo zvyšte DENIK_ANSWER_MAX_TOKENS — ale "
                      "POZOR, strop platí i pro gemmu ve fallbacku (P8)."
                      % (n, dni, znaku))

    print("\n" + "=" * 70)
    if not alerty:
        print("ALERTY: žádné.")
        return
    print("ALERTY (%d):" % len(alerty))
    for x in alerty:
        print("  * " + x)
    sys.exit(1)


if __name__ == "__main__":
    main()
