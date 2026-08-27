#!/usr/bin/env python3
"""Smoke test převodu Keep -> markdown (P13). Bez sítě, bez Keepu, bez DB.

Testuje to, co může tiše ublížit:

  * **Determinismus.** Detekce změn v indexeru je sha256 celého souboru.
    Kdyby render nad nezměněnou poznámkou vrátil pokaždé jiný bajt, hodinový
    sync by přeembeddovával celý Keep dokola a commitoval do gitu 24× denně.
  * **Frontmatter čte SKUTEČNÝ parser z retrieval-service**, ne domněnka
    o tom, jak vypadá. `split_frontmatter` záměrně není plný YAML — kdyby se
    tvar rozešel, `lang:` a `trust:` by se ignorovaly a poznalo by se to až
    podle toho, že lexikální větev hledání nic nevrací.
  * **Stabilita cesty.** `document.source_path` je UNIQUE klíč; přejmenování
    souboru = smazání dokumentu a nové embeddování celé poznámky.
  * **Pojistka na mazání.** Jediná věc v celém P13, která je nevratná.

Tři prostředí a jen PRVNÍ umí všechny kontroly: `gkeepapi` a retrieval-service
nikde v provozu na jednom místě nejsou (do obrazu Krytona jde `COPY app ./app`
a nic víc). Chybějící sekce se přeskočí s poznámkou, nespadne.

    # VŠECH 29 kontrol — celý repozitář i gkeepapi, odhoditelný kontejner.
    # Tohle spouštěj, když se mění render nebo pojistka. Overeno 2026-08-21.
    podman run --rm -v "$PWD":/repo:ro,Z -w /repo docker.io/library/python:3.13-slim \
      sh -c "pip install -q gkeepapi==0.17.1 && python3 /repo/scripts/30-smoke-keep.py"

    ./scripts/30-smoke-keep.py              # stanice: 24, bez gkeepapi
    podman cp scripts/30-smoke-keep.py kryton:/srv/ && \
      podman exec -w /srv kryton python3 /srv/30-smoke-keep.py
                                            # Kryton: 23, bez frontmatteru

Návratový kód 0 = vše prošlo, 1 = něco selhalo.
"""
import importlib
import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent
os.environ.setdefault("DATABASE_URL", "postgresql://stub/stub")

# Kryton i retrieval-service mají oba balíček jménem `app`, takže je nelze
# naimportovat oba přes sys.path — druhý by dostal ten první. Retrieval se
# proto registruje pod jiným jménem; `from . import config` uvnitř nej pak
# míří správně, protože relativní importy jdou podle jména balíčku.
sys.path.insert(0, str(KOREN / "kryton"))
from app import keep                      # noqa: E402  (kryton/app)


def _nacti_balicek(jmeno: str, cesta: Path):
    spec = importlib.util.spec_from_file_location(
        jmeno, cesta / "__init__.py", submodule_search_locations=[str(cesta)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[jmeno] = mod
    spec.loader.exec_module(mod)
    return mod


# Retrieval-service je NEPOVINNA. V obrazu Krytona neni (Containerfile dela
# jen `COPY app ./app`), a prave uvnitr Krytona je zase `gkeepapi` — takze
# kdyby se tenhle import vyzadoval, test by se v kontejneru rozpadl driv,
# nez by se dostal k sekci proti skutecne knihovne. Overeno 2026-08-21:
# padalo to na FileNotFoundError u rapp/__init__.py.
chunker = None
if (KOREN / "retrieval-service" / "app" / "__init__.py").exists():
    _nacti_balicek("rapp", KOREN / "retrieval-service" / "app")
    chunker = importlib.import_module("rapp.chunker")

FAIL = []


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'CHYBA'} {name}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        FAIL.append(name)


class Timestamps:
    def __init__(self, created, updated):
        self.created, self.updated = created, updated


class Label:
    def __init__(self, name):
        self.name = name


class Labels:
    def __init__(self, names):
        self._n = [Label(x) for x in names]

    def all(self):
        return self._n


class Polozka:
    def __init__(self, text, checked):
        self.text, self.checked = text, checked


class FakeNote:
    """Tvar, který vrací gkeepapi. Jen atributy, které keep.py čte."""

    def __init__(self, id, title="", text="", items=None, labels=(),
                 pinned=False, archived=False, trashed=False, blobs=()):
        self.id, self.title, self.text = id, title, text
        self.items = list(items) if items else []
        self.labels = Labels(labels)
        self.pinned, self.archived, self.trashed = pinned, archived, trashed
        self.blobs = list(blobs)
        self.timestamps = Timestamps(datetime(2026, 3, 1, 10, 12, 0),
                                     datetime(2026, 8, 19, 8, 3, 0))


print("== převod poznámky ==")
n = FakeNote("1a2b3c.4d5e", title="Nákup", labels=["nakupy", "dum"],
             items=[Polozka("mléko", False), Polozka("chleba", True)])
md = keep.render(n)
check("checklist se převede na markdown", "- [ ] mléko" in md and "- [x] chleba" in md)
check("titulek je H1", "\n# Nákup\n" in md)
check("štítky jsou ve frontmatteru", "stitky: [dum, nakupy]" in md, md)
check("štítky jsou seřazené (Keep pořadí negarantuje)",
      md.index("dum") < md.index("nakupy"))
check("text checklistu se needuplikuje", md.count("mléko") == 1)

print("\n== determinismus ==")
check("dvojí render dá bajt po bajtu totéž", keep.render(n) == md)
n2 = FakeNote("1a2b3c.4d5e", title="Nákup", labels=["dum", "nakupy"],
              items=[Polozka("mléko", False), Polozka("chleba", True)])
check("jiné pořadí štítků z API nezmění výsledek", keep.render(n2) == md)
check("nikde není čas běhu", "20" + datetime.now().strftime("%y-%m-%d") not in md
      or datetime.now().strftime("%Y-%m-%d") == "2026-03-01")

print("\n== frontmatter proti skutečnému parseru z retrieval-service ==")
if chunker is None:
    print("  --   retrieval-service není po ruce, přeskakuji")
    print("       (tuhle sekci umí jen běh nad celým repozitářem, ne v Krytonovi)")
else:
    meta, body = chunker.split_frontmatter(md)
    check("parser frontmatter rozpozná", meta != {}, str(meta))
    check("lang projde jako skalár", meta.get("lang") == "cs", str(meta))
    check("trust projde jako skalár", meta.get("trust") == "2", str(meta))
    check("frontmatter se nedostane do těla", "keep_id" not in body, body[:80])
    check("tělo začíná nadpisem", body.lstrip().startswith("# Nákup"))

print("\n== cesta k souboru ==")
p1 = keep.rel_path(n)
check("tvar keep/RRRR-MM-DD-<id>.md", p1 == "keep/2026-03-01-1a2b3c-4d5e.md", p1)
n3 = FakeNote("1a2b3c.4d5e", title="Úplně jiný titulek", text="x")
check("změna titulku cestu NEZMĚNÍ", keep.rel_path(n3) == p1, keep.rel_path(n3))

print("\n== okrajové případy ==")
check("poznámka jen s přílohou se přeskočí",
      keep.render(FakeNote("x.1", blobs=[object()])) is None)
bez = keep.render(FakeNote("x.2", text="jenom text bez nadpisu"))
check("bez titulku se nadpis odvodí z prvního řádku",
      "# jenom text bez nadpisu" in bez, bez)
sp = keep.render(FakeNote("x.3", title="S fotkou", text="popis", blobs=[object()]))
check("příloha se zmíní v těle", "1 příloh" in sp, sp)
check("archivovaná poznámka se vyfiltruje",
      keep._k_indexaci([FakeNote("a.1", text="a", archived=True)]) == [])
check("poznámka v koši se vyfiltruje",
      keep._k_indexaci([FakeNote("a.2", text="a", trashed=True)]) == [])

# Nalezeno 2026-08-27 na dotazu "co víme o drenáži" — poznámka "Drenáž"
# s tělem jen z YouTube odkazu se nikdy nenašla. chunker.py nedává nadpis
# do `content` (jen do `heading_path`), takže `content_tsv` byl složený
# výhradně z URL. Titulek musí být i v těle, ne jen v H1.
jen_odkaz = keep.render(FakeNote("d.1", title="Drenáž",
                                 text="https://youtube.com/shorts/x"))
check("titulek poznámky jen s odkazem je i v těle (ne jen v H1)",
      jen_odkaz.count("Drenáž") >= 2, jen_odkaz)

# Tentýž nález, horší varianta: poznámka jen s titulkem a PRÁZDNÝM tělem
# ("Objednat", nic pod tím) — chunker.py bez těla pod nadpisem nevytvoří
# žádný chunk, dokument je v indexu, ale nedohledatelný (0 chunků). Ověřeno
# 2026-08-27 přímo v DB: 4 takové poznámky ("Objednat", "iPhone SE červený"
# mezi nimi) měly `count(chunk.id) = 0`.
jen_titulek = keep.render(FakeNote("d.2", title="Objednat"))
check("poznámka jen s titulkem (bez těla) se dá vykreslit",
      jen_titulek is not None, jen_titulek)

if chunker is None:
    print("  --   retrieval-service není po ruce, přeskakuji ověření chunkerem")
else:
    # Autoritativní verze obou nálezů výše — přes SKUTEČNÝ chunker.py, ne
    # jen počítání výskytů v markdownu. `heading_path` do `content` nejde,
    # takže tohle je jediný způsob, jak ověřit, co se opravdu zaembeduje.
    _, telo_odkaz = chunker.split_frontmatter(jen_odkaz)
    chunky_odkaz = chunker.chunk_markdown(telo_odkaz)
    check("poznámka jen s odkazem dá chunk obsahující titulek",
          any("Drenáž" in ch.content for ch in chunky_odkaz),
          " | ".join(ch.content for ch in chunky_odkaz))
    _, telo_titulek = chunker.split_frontmatter(jen_titulek)
    chunky_titulek = chunker.chunk_markdown(telo_titulek)
    check("poznámka jen s titulkem (bez těla) dá ALESPOŇ JEDEN chunk",
          len(chunky_titulek) >= 1, repr(telo_titulek))

print("\n== pojistka na mazání ==")
res = {"k_indexaci": 0}
check("nula poznámek z API mazání zastaví",
      keep._pojistka(["keep/a.md"], {"keep/a.md", "keep/b.md"}, res) is not None)
res = {"k_indexaci": 100}
check("běžné smazání dvou z sta projde",
      keep._pojistka(["keep/a.md", "keep/b.md"],
                     {f"keep/{i}.md" for i in range(100)}, res) is None)
check("smazání poloviny se zastaví",
      keep._pojistka([f"keep/{i}.md" for i in range(50)],
                     {f"keep/{i}.md" for i in range(100)}, res) is not None)
check("malá sbírka: 2 ze 4 projdou (absolutní mez drží)",
      keep._pojistka(["keep/a.md", "keep/b.md"],
                     {"keep/a.md", "keep/b.md", "keep/c.md", "keep/d.md"},
                     res) is None)

print("\n== proti skutečné knihovně gkeepapi (když je k dispozici) ==")
try:
    from gkeepapi import node
except ImportError:
    print("  --   gkeepapi není nainstalované, přeskakuji")
    # Soubor v obrazu Krytona NENÍ, musí se tam nejdřív zkopírovat.
    print("       (uvnitř Krytona: podman cp scripts/30-smoke-keep.py kryton:/srv/ &&")
    print("        podman exec -w /srv kryton python3 /srv/30-smoke-keep.py)")
else:
    # Atrapy výše popisují tvar, ve který věříme. Tohle ověřuje ten skutečný.
    real = node.List()
    real.title = "Úkoly"
    real.add("koupit lepidlo", False)
    real.add("zavolat elektrikáři", True)
    out = keep.render(real)
    check("checklist ze skutečného node.List", "- [ ] koupit lepidlo" in out
          and "- [x] zavolat elektrikáři" in out, out)
    # POTVRZENO 2026-08-20: List.text je serializace s ☐/☑. Kdyby se u
    # checklistu použil `text` místo položek, byla by v indexu tahle
    # unicode zaškrtávátka, ne markdown.
    check("List.text jsou ☐/☑, proto se u checklistu ignoruje",
          "\u2610" in real.text and "\u2610" not in out, repr(real.text))
    check("determinismus i nad skutečným objektem", keep.render(real) == out)
    realn = node.Note()
    realn.title = "Nákup"
    realn.text = "mléko"
    check("cesta ze skutečného node.Note",
          keep.rel_path(realn).startswith("keep/") and
          keep.rel_path(realn).endswith(".md"), keep.rel_path(realn))
    if chunker is not None:
        meta2, _ = chunker.split_frontmatter(keep.render(realn))
        check("frontmatter ze skutečného objektu parser přečte",
              meta2.get("lang") == "cs" and meta2.get("trust") == "2", str(meta2))

print()
if FAIL:
    print(f"SELHALO {len(FAIL)}: " + ", ".join(FAIL))
    sys.exit(1)
print("Všechno prošlo.")
