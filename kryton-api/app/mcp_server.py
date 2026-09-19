"""MCP server — core.search()+core.answer() a core.capture() jako nástroje
pro externí agenty (OpenWork a další MCP klienty), vedle webového UI.

MCP klient se nepřihlašuje cookie session jako prohlížeč, proto vlastní
autentizace: sdílený token v `Authorization: Bearer <token>` hlavičce,
ověřovaný konstantním časem (`hmac.compare_digest`), ne `==` — token je
secret a časování porovnání by ho jinak mohlo prozradit bit po bitu.

`TokenVerifier` je „Resource Server" ověřovač z `fastmcp` — kontroluje
hlavičku bez nutnosti stavět celý OAuth server (žádné `/authorize`,
`/token` cesty). Bez nastaveného `MCP_BEARER_TOKEN` je endpoint zavřený
pro každého, ne otevřený — `verify_token` vrátí `None` i na správný token,
když secret chybí.

Vrací hotovou odpověď z `core.answer()`, ne syrové úryvky — pro hlubší
zkoumání zdrojů slouží Kryton samotný, tenhle nástroj je pro rychlý dotaz
z jiných AI nástrojů (Claude Code, Cursor, ChatGPT přes OpenWork).
"""
from __future__ import annotations

import hmac
import logging

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier

from . import config, core

log = logging.getLogger("kryton")


class _SdilenyToken(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if not config.MCP_BEARER_TOKEN:
            return None
        if not hmac.compare_digest(token, config.MCP_BEARER_TOKEN):
            return None
        return AccessToken(token=token, client_id="mcp", scopes=[])


mcp = FastMCP(name="kryton", auth=_SdilenyToken())


@mcp.tool()
def hledat(dotaz: str) -> dict:
    """Zeptej se na osobní poznámky uživatele a dostaň odpověď s citacemi."""
    vysledky = core.search(dotaz)
    odp = core.answer(dotaz, vysledky["results"])
    core.zaznamenej("mcp", dotaz, odp, vysledky["results"])
    citace = [
        {"source_path": h["source_path"], "heading_path": h.get("heading_path")}
        for h in vysledky["results"]
    ]
    return {"odpoved": odp.text, "citace": citace, "model": odp.model}


@mcp.tool()
def zachytit(text: str, nadpis: str | None = None) -> dict:
    """Zapiš novou poznámku do Druhého mozku (bez nadpisu jde do denního zápisu)."""
    rel = core.capture(text, nadpis, kanal="mcp")
    return {"ulozeno_do": rel}
