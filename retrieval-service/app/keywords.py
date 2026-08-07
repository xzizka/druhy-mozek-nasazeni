"""Převod dotazu na klíčová slova pro lexikální a fuzzy větev.

PROČ TO TU JE — změřeno na nasazeném systému:

`websearch_to_tsquery` spojuje termíny operátorem AND. Z otázky
„Proč záleží na pořadí slovníků při lemmatizaci?" vznikne

    'proč' & 'záležet' & 'na' & 'pořadí' & 'slovník' & ('při'|'pře'|'přít') & 'lemmatizace'

a nenajde NIC, protože žádný dokument neobsahuje všechny ty termíny.
Fuzzy větev zmlkne taky — word_similarity dlouhého dotazu je ~0,24 proti
prahu 0,5. Zůstane jen dense větev a hybridní hledání se tiše zdegeneruje
na čistě vektorové.

Ověřeno, že klíčová slova naopak rozsvítí všechny tři větve.

Dense větev naopak s celou otázkou pracuje výborně (ověřeno), takže se
posílá do `hybrid_search` embedding CELÉ otázky, ale `p_query` jen
z klíčových slov. Funkce to umožňuje, protože jsou to dva nezávislé
parametry.

Tohle je deterministická varianta bez LLM — fallback pro `rewrite.py`, když
je přepis přes alias `cheap` vypnutý nebo selže. Kryton může poslat rovnou
lepší přepis v položce `keywords` a tenhle kód se přeskočí.

VÍCEJAZYČNOST: stopword listy jsou per jazyk (cs, en, de, la). Bez toho by si
anglický dotaz „What is the database?" ponechal `what is the`, AND semantika
by z každého toho slova udělala podmínku a dotaz by nenašel nic — tedy přesně
ten režim, kvůli kterému tenhle modul vznikl, jen v jiném jazyce.
"""
import re
import unicodedata

# Jen jednoznačná funkční slova. Záměrně konzervativní: odstranit omylem
# obsahové slovo je horší než ponechat předložku, protože AND semantika
# udělá z každého zbytečného termínu podmínku, ale z chybějícího termínu
# nenávratně ztracenou informaci.
#
# Listy jsou PER JAZYK a nikdy se nesjednocují. Sjednocení by bylo lákavé
# („stejně nevíme, co přijde"), ale zabíjelo by obsahová slova přes hranice
# jazyků — německé `die` je člen, kdežto anglické `die` je sloveso.
STOPWORDS = {
    "cs": {
        # tázací
        "proč", "jak", "kde", "kdy", "co", "kdo", "kolik", "zda", "jestli",
        "který", "která", "které", "kterého", "kterou", "kterým", "kterých",
        "jaký", "jaká", "jaké", "jakého", "jakou", "jakým", "jakých", "čím", "čeho", "čemu",
        # předložky
        "na", "v", "ve", "z", "ze", "k", "ke", "ku", "s", "se", "o", "do", "od",
        "po", "pro", "při", "za", "u", "nad", "pod", "před", "mezi", "bez",
        "kolem", "podle", "vedle", "kvůli", "proti", "skrz", "díky",
        # spojky a částice
        "a", "i", "ale", "nebo", "anebo", "či", "že", "aby", "když", "protože",
        "takže", "však", "tedy", "také", "taky", "jen", "už", "ještě", "asi",
        "prý", "snad", "ani", "však", "aťsi", "jako",
        # zájmena a pomocná slova
        "to", "ta", "ten", "ty", "tu", "toho", "tom", "tím", "této", "tato",
        "tento", "této", "ono", "on", "ona", "oni", "my", "vy", "já", "ty",
        "si", "mi", "mě", "mne", "tě", "ho", "jí", "jich", "jim", "nám", "vám",
        "svůj", "svého", "své", "svá", "můj", "tvůj", "náš", "váš",
        "všechno", "všechny", "něco", "nic", "někdo", "nikdo",
        # nejběžnější sponová a modální slovesa
        "je", "jsou", "byl", "byla", "bylo", "byly", "být", "bude", "budou",
        "má", "mají", "mít", "měl", "měla", "musí", "může", "můžu", "lze",
        "znamená", "dá", "dělá",
    },
    "en": {
        # tázací
        "what", "how", "why", "where", "when", "who", "which", "whose", "whom",
        # členy, předložky, spojky
        "a", "an", "the", "of", "to", "in", "on", "at", "by", "for", "from",
        "with", "without", "about", "into", "onto", "over", "under", "between",
        "through", "during", "before", "after", "against", "and", "or", "but",
        "if", "than", "then", "because", "so", "as", "that", "this", "these",
        "those", "there", "here",
        # zájmena
        "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
        "them", "my", "your", "his", "its", "our", "their",
        # sponová a modální slovesa
        "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
        "did", "done", "have", "has", "had", "can", "could", "should", "would",
        "will", "shall", "may", "might", "must", "not", "no",
    },
    "de": {
        # tázací
        "was", "wie", "warum", "wieso", "weshalb", "wo", "wann", "wer", "wen",
        "wem", "wessen", "welche", "welcher", "welches", "welchen", "welchem",
        # členy
        "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
        "einem", "einer", "eines",
        # předložky a spojky
        "in", "an", "auf", "für", "mit", "von", "zu", "zur", "zum", "aus",
        "bei", "nach", "über", "unter", "zwischen", "ohne", "durch", "gegen",
        "um", "vor", "seit", "und", "oder", "aber", "dass", "wenn", "weil",
        "als", "wie", "damit", "obwohl",
        # zájmena a částice
        "ich", "du", "er", "sie", "es", "wir", "ihr", "man", "sich", "mein",
        "dein", "sein", "unser", "euer", "auch", "nur", "noch", "schon",
        "mehr", "sehr", "nicht", "kein", "keine",
        # sponová a modální slovesa
        "ist", "sind", "war", "waren", "sein", "wird", "werden", "wurde",
        "wurden", "hat", "haben", "hatte", "hatten", "kann", "können",
        "muss", "müssen", "soll", "sollen", "will", "wollen", "darf", "dürfen",
    },
    "la": {
        # U latiny má filtrování největší cenu — konfigurace `latin` nestemmuje
        # ani nefiltruje sama (jen lowercase + unaccent), takže tohle je jediné
        # místo, kde se funkční slova z dotazu odstraní.
        # spojky a částice
        "et", "ac", "atque", "aut", "vel", "nec", "neque", "sed", "autem",
        "enim", "igitur", "ergo", "tamen", "quoque", "etiam", "nam", "que",
        # předložky
        "in", "ad", "ex", "de", "cum", "sine", "per", "pro", "sub", "super",
        "inter", "ante", "post", "apud", "circa", "contra", "trans",
        # vztažná a ukazovací zájmena
        "qui", "quae", "quod", "quem", "quam", "quos", "quas", "quo", "cuius",
        "cui", "hic", "haec", "hoc", "ille", "illa", "illud", "ipse", "ipsa",
        "idem", "eius", "eorum", "sibi", "sui", "suus", "sua", "suum",
        # sloveso být a záporky
        "est", "sunt", "esse", "erat", "erant", "fuit", "fuerunt", "erit",
        "sit", "sint", "esset", "non", "ne", "nihil", "nullus",
        # tázací
        "quis", "quid", "cur", "ubi", "quando", "quomodo", "quantum", "num",
        "utrum", "ut", "si",
    },
}

# Slovo = písmena včetně diakritiky, číslice, podtržítko, tečka a pomlčka
# uvnitř slova. Podtržítko a tečka jsou tu úmyslně: identifikátory jako
# `maintenance_work_mem` nebo `pg_trgm.word_similarity_threshold` jsou
# přesně to, co má fuzzy větev chytat.
TOKEN = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿĀ-ž_]+(?:[._-][0-9A-Za-zÀ-ÖØ-öø-ÿĀ-ž_]+)*")


def _fold(word: str) -> str:
    """Odstraní diakritiku. Dotazy bez diakritiky jsou podle návrhu
    prvotřídní případ (proto trigramová větev), takže stopwords musí
    zabírat i na `musi`, `byt`, `pred` — ne jen na tvary s háčky."""
    return unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode()


STOPWORDS_FOLDED = {lang: {_fold(w) for w in words}
                    for lang, words in STOPWORDS.items()}


def extract(query: str, lang: str = "cs") -> str:
    """Z dotazu udělá řetězec klíčových slov pro p_query.

    `lang` vybírá stopword list. Neznámý jazyk znamená, že se nefiltruje nic —
    to je bezpečnější než sáhnout po českém listu, protože odstranit z cizího
    dotazu obsahové slovo je nevratná ztráta, kdežto ponechaný termín navíc
    dotaz jen zúží.

    Nikdy nevrátí prázdno — kdyby po odfiltrování nic nezbylo, vrátí
    původní dotaz, protože prázdný p_query by lexikální i fuzzy větev
    vypnul úplně.
    """
    stop = STOPWORDS.get(lang, frozenset())
    stop_folded = STOPWORDS_FOLDED.get(lang, frozenset())
    tokens = TOKEN.findall(query)
    kept = []
    for t in tokens:
        low = t.lower()
        if low in stop or _fold(low) in stop_folded:
            continue
        # Jednoznakové zbytky nesou šum, ale čísla a identifikátory ponech.
        if len(t) < 2 and not t.isdigit():
            continue
        kept.append(t)
    return " ".join(kept) if kept else query.strip()
