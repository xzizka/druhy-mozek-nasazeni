# nexos.ai jako náhrada OpenRouteru — analýza a pokusné nasazení

Stav: **ZMĚŘENO, NENASAZENO.** Zapsáno 2026-08-18.

Cíl zadání: zjistit, jak je na tom nexos.ai cenově proti OpenRouteru,
najít náhrady za dnešní modely (a když model existuje i na nexos.ai,
nechat tentýž), a pokusně přepnout.

Referenční stav před změnou je v `conf/litellm-config.yaml` a shrnutý
v pěti aliasech níže. K měření slouží `scripts/22-eval-nexos-models.py` —
sesterský skript k `17-eval-cheap-models.py` se **stejným korpusem
a stejnými prompty**, aby čísla šla porovnat jedno k jednomu.

---

## 1. Jak se s nexos.ai mluví

| věc | hodnota |
|---|---|
| endpoint | `https://api.nexos.ai/v1` (OpenAI-kompatibilní) |
| autentizace | `Authorization: Bearer <klíč>` (nebo `X-Api-Key`) |
| katalog | `GET /v1/models` (zapnuté), `GET /v1/models/all` (celý) |
| jméno modelu | **zobrazovaný název**, např. `GPT 5.4 mini` — ne `provider/model` |
| pod kapotou | Portkey (`id` odpovědi začíná `portkey-`) |

Do LiteLLM se to zapojí přesně jako dnešní OpenCode Zen: prefix `openai/`
znamená „OpenAI-kompatibilní endpoint", takže na providera jde jméno bez
prefixu.

```yaml
- model_name: cheap-nexos
  litellm_params:
    model: openai/GPT 5.4 mini          # mezery v názvu jsou v pořádku
    api_base: os.environ/NEXOS_API_BASE  # https://api.nexos.ai/v1
    api_key: os.environ/NEXOS_API_KEY
```

### Tři pasti, na které jsem narazil při měření

**1. Cloudflare blokuje `Python-urllib`.** Před `api.nexos.ai` sedí
Cloudflare a výchozí user agent `Python-urllib/3.x` odmítá s
`HTTP 403 / error code: 1010`. Vypadá to jako odmítnutý klíč, ale není —
s **jakoukoliv** vlastní hodnotou `User-Agent` projde. Sežralo mi to jeden
celý běh měření (4 modely × 33 volání = 0/20 u všech). `httpx`
v `retrieval-service` posílá vlastní UA, takže samotné služby to netrápí;
trápí to nástroje psané nad `urllib`, tedy i `scripts/19`.

**2. Gateway má vlastní cache.** Odpověď nese hlavičku `x-nexos-cache:
hit|miss`. Je to **druhá** cache nad tou, kterou už má LiteLLM
(`cache: true`, TTL 3600). Stejná past jako u měření `cheap` v srpnu:
zopakovaný prompt se vrátí zadarmo a netestuje nic. `scripts/19` hlavičku
čte a počet zásahů hlásí; když není nula, výsledky jsou k ničemu.

**3. `nexos_credits_cost` v `usage`.** Každá odpověď nese svou cenu
v kreditech. To je proti OpenRouteru krok navíc k měřitelnosti — cena je
známá hned, ne až ze spend logu.

---

## 2. Katalog: 145 chat modelů, zapnutých 14

Tohle je hlavní zjištění celé analýzy a mění povahu úlohy.

`GET /v1/models/all` vrátí 164 modelů, z toho 145 umí `chat_completion`.
Pro tvůj klíč je jich ale **zapnutých 18** (z toho 14 chat). Model, který
zapnutý není, vrátí:

```
{"error":{"code":100110,"message":"Model not found"}}
```

Ověřeno 2026-08-18 na `GPT 5 nano`, `Gemma 4 31B` i `GPT-OSS 20b`, a to
přesným názvem, malými písmeny i bez pomlčky. Zapínají se v konzoli:
**Models → add a model**.

### Dvě jmenné soustavy UUID, jen jedna volatelná

Tohle je snadné splést a mě to spletlo. Oba katalogové endpointy vracejí
UUID, ale **jiná** — a volat jde jen jedno z nich:

| endpoint | pole | co to je | volatelné? |
|---|---|---|---|
| `/v1/models` | `nexos_model_id` | instance modelu v tvém workspace | **ano** |
| `/v1/models/all` | `id` | globální ID modelu v katalogu | **ne** |

Ověřeno na `Gemini 3 Flash Preview`: workspace UUID
`169d61e0-021c-4d30-99ba-41634c4767cb` odpoví, globální
`91da0459-6505-405a-a92d-e7e711f7970c` vrátí totéž `Model not found` jako
vypnutý model. Vypnutý model `nexos_model_id` prostě **nemá** — to pole
existuje jen v `/v1/models`.

Praktický dopad: v `litellm-config.yaml` je jméno čitelnější, ale
**jednoznačné je jen `nexos_model_id`** — viz past hned níže.

**Zapnutá čtrnáctka je shodou okolností ta drahá půlka katalogu.**
Nejlevnější zapnutý chat model stojí `0,500 $/M` vstup; nejlevnější
v katalogu `0,050 $/M`. Než se v konzoli něco zapne, je srovnání s
OpenRouterem předem prohrané — a ne kvůli nexos.ai, ale kvůli tomu,
co je zaškrtnuté.

### Zapnout model jde jen v konzoli, ne přes API

Ověřeno 2026-08-18 tvým klíčem:

| endpoint | výsledek |
|---|---|
| `GET /v1/management/models` | `403 {"code":110014,"message":"Action is not allowed"}` |
| `GET /v1/management/workspaces` | `403` totéž |
| `GET /v1/management/models/{uuid}/fallbacks` | **200** |

Klíč je tedy gateway klíč bez správcovských práv a **zapnutí modelu se
naskriptovat nedá** — dokumentace žádný „add/enable a model" endpoint
nemá a tenhle klíč by na něj stejně nedosáhl.

Postup v konzoli (podle dokumentace, neviděl jsem to na vlastní oči):
**Models → add a model →** *Model configurations* (vyber model a pojmenuj
ho) **→** *Set timeouts* (výchozí 60 000 ms, stream 300 000 ms) **→**
volitelně *Fallback models* **→ Save and add more models**. Když
požadovaný model v nabídce není, rozhoduje nastavení organizace, ne
workspace.

Za pozornost stojí, že **`/fallbacks` funguje** — fallback řetěz by tedy
šel držet v gatewayi místo v LiteLLM. Pro tenhle projekt to smysl nedává
(LiteLLM fallbacky už má a `num_retries` gateway nenabízí), ale bere
to UUID, ne jméno: `…/models/GPT-OSS 120b/fallbacks` vrátí 404,
`…/models/6d8aec78-…/fallbacks` vrátí 200. Další potvrzení, že
**správcovské endpointy jménem neadresuješ**.

### Ceny nesou přirážku už v sobě

Katalog obsahuje týž model víckrát za různé ceny a **zapnutá varianta je
soustavně ta o 10 % dražší**:

| model | levnější varianta | zapnutá varianta |
|---|---|---|
| Claude Haiku 4.5 | 1,000 / 5,000 (Anthropic) | **1,100 / 5,500** |
| Claude Sonnet 5 | 2,000 / 10,000 | **2,200 / 11,000** |
| GPT 5.5 | 5,000 / 30,000 | **5,500 / 33,000** |
| Gemini 3.5 Flash | 1,500 / 9,000 | **1,650 / 9,900** |

Marketing mluví o 5 % („model za $10/M → platíš $10,50/M"), katalog ukazuje
10 %. Neber prosím moje čtení jako výklad tvé smlouvy — je to poměr dvou
čísel z API, ne citace ceníku. Ale **počítej s 10 %, ne s 5 %.**

Výjimka, která do vzorce nesedí: `GPT-OSS 120b` je zapnutý ve variantě
hostované samotným nexos.ai za **0,800 / 1,600**, zatímco varianta přes
Azure stojí **0,150 / 0,600** — tedy 5,3× méně. Tady nejde o přirážku,
ale o jiné hostování.

**A právě tady je past, kterou musíš vyřešit dřív, než přepneš:** obě
varianty mají **týž zobrazovaný název** `GPT-OSS 120b`. Dokud je zapnutá
jen jedna, `model: openai/GPT-OSS 120b` míří jednoznačně na ni (dnes na
tu drahou, workspace UUID `6d8aec78-893f-4c89-b4e0-40c20751cf0a`). Jakmile
zapneš i tu od Azure, **jméno přestane být jednoznačné** a nemáš jak říct,
kterou chceš. Jediné spolehlivé adresování je pak `nexos_model_id`.

Takže: po zapnutí Azure varianty si vytáhni její `nexos_model_id`
(vypíše `scripts/23-nexos-pripravenost.py`) a dej do configu **UUID**,
ne jméno. Totéž platí pro každý model, který je v katalogu vícekrát —
`GPT 5 nano` má tři varianty, `Gemma 4 31B` jednu, `GPT-OSS 20b` jednu.

---

## 3. Náhrady za dnešní modely

Zadání říká: když model existuje i na nexos.ai, nech tentýž. Prověřeno
jmenným hledáním v celém katalogu:

| dnešní model | na nexos.ai | poznámka |
|---|---|---|
| `inclusionai/ling-2.6-flash` | **není** | žádný InclusionAI v katalogu |
| `google/gemma-4-26b-a4b-it` | **není** | je `Gemma 4 31B`, jiný model |
| `openai/gpt-oss-20b` | **JE** (Groq, 0,075 / 0,300) | nutno zapnout |
| `openai/big-pickle` | **není** | stealth model exkluzivní pro OpenCode Zen |
| `mistralai/ministral-*` | není | (zamítnutí kandidáti, jen pro pořádek) |

Takže z pěti aliasů se **tentýž model dá zachovat u jednoho** — `backstop`.
Zbytek je náhrada za jiný model, tedy věc, kterou je nutné přeměřit,
ne odhadnout.

### Navržené mapování

Sloupec „dnes" je z `conf/litellm-config.yaml`, sloupec „nexos" je návrh
**po zapnutí modelů v konzoli**.

| alias | dnes | $/M dnes | nexos.ai | $/M nexos | proč |
|---|---|---|---|---|---|
| `cheap` | ling-2.6-flash | 0,010 / 0,030 | **GPT 5 nano** | 0,050 / 0,400 | nejlevnější chat v katalogu |
| `cheap-fallback` | gemma-4-26b-a4b-it | 0,070 | **Gemma 4 31B** | 0,130 / 0,380 | nejbližší příbuzný dnešní gemmy |
| `workhorse` | gemma-4-26b:free | 0 | **Gemma 4 31B** | 0,130 / 0,380 | tentýž rodokmen jako dnes |
| `backstop` | gpt-oss-20b:free | 0 | **GPT-OSS 20b (Groq)** | 0,075 / 0,300 | **týž model**, jen placený |
| `reasoning` | big-pickle (Zen) | 0 | **GPT-OSS 120b (Azure)** | 0,150 / 0,600 | otevřený reasoning model, 5,3× levněji než zapnutá varianta |

---

## 3b. OPRAVA: rozhoduje poskytovatel, ne katalog

Původní návrh v kapitole 3 byl **z velké části nepoužitelný** a přišlo se
na to až z konzole: uživatel hledal `Gemm` a nenašel nic.

Důvod: `/v1/models/all` vrací **globální katalog nexos.ai**, ne to, co si
tvoje organizace může přidat. Zapnuto je dnes jen od tří poskytovatelů:

| připojeno | počet zapnutých |
|---|---|
| Agent Platform (Vertex AI) | 9 |
| Azure | 8 |
| nexos.ai | 1 |

A z těchhle není zapnuto **nic**: OpenAI (15 variant), Fireworks AI (14),
SpaceXAI (9), Bedrock (7), Mistral AI (6), Anthropic (2), DeepInfra (2),
Alibaba Cloud (2), Groq (1), Nebius (1).

Tím padají tři z pěti původních doporučení:

| původní návrh | poskytovatel | proč nejde |
|---|---|---|
| Gemma 4 31B | DeepInfra | jiná varianta v katalogu není |
| GPT-OSS 20b | Groq | jiná varianta v katalogu není |
| GPT 5 nano *(varianta OpenAI)* | OpenAI | ale **Azure variantu má**, viz níže |

### A ještě jedna oprava: `region: EU` neplatí plošně

Napsal jsem výše, že katalog hlásí `region: EU` u všeho. **Není to
pravda** — zobecnil jsem to z prvního záznamu. Skutečné rozložení:

| region | variant | z toho zapnutých |
|---|---|---|
| EU | 48 | 13 |
| OTHER | 74 | 5 |
| US | 42 | 0 |

Pět tvých zapnutých modelů je `OTHER`, ne EU — **včetně obou, které
v měření uspěly**: `GPT 5.4 mini` i `Gemini 3 Flash Preview`. Region tedy
není něco, co nexos.ai zařídí samo; je to volba na úrovni každé varianty.

### Revidovaný návrh — jen Azure a Vertex, vše EU

| alias | model | kdo | $/M | stav |
|---|---|---|---|---|
| `cheap` | **GPT 5 nano** | Azure EU | 0,060 / 0,440 | zapnout |
| `cheap-fallback` | **Gemini 2.5 Flash Lite** | Vertex EU | 0,100 / 0,400 | zapnout |
| `workhorse` | **Gemini 2.5 Flash Lite** | Vertex EU | 0,100 / 0,400 | zapnout |
| `backstop` | **GPT 4.1 nano** | Azure EU | 0,110 / 0,440 | zapnout |
| `reasoning` | **GPT-OSS 120b** | nexos.ai EU | 0,800 / 1,600 | **už zapnutý** |

Proti původnímu návrhu je to o kousek dražší u `cheap` (0,060 místo 0,050 —
Azure EU proti OpenAI mimo EU) a citelně dražší u `backstop` a `reasoning`.
Zato je celé v EU a celé z poskytovatelů, které tvoje org opravdu má.

**`reasoning-nexos` nepotřebuje v konzoli vůbec nic** — `GPT-OSS 120b`
je zapnutý. Je to reasoning model, takže na roli `cheap` by neuspěl
(0/20 v měření), ale Kryton posílá 8000 tokenů, kde na úvahu místo je.

### Co to dělá s cenou

`reasoning` je 95 % celého účtu, protože Kryton posílá ~2800 tokenů na dotaz:

| varianta | $/měsíc | poměr |
|---|---|---|
| OpenRouter dnes | 0,0086 | 1× |
| nexos.ai, vše EU (`reasoning` = GPT-OSS 120b od nexos.ai) | **1,50** | 173× |
| nexos.ai, ale `reasoning` = GPT-OSS 120b **od Azure, region OTHER** | **0,36** | 42× |
| nexos.ai, vše EU, `reasoning` = Claude Haiku 4.5 | 2,19 | 253× |

Ta prostřední řádka je rozhodnutí, ne technikálie: `GPT-OSS 120b` od Azure
stojí 0,150/0,600 místo 0,800/1,600, tedy **5,3× méně za týž model**, ale
běží mimo EU. Rozdíl je 1,14 $ měsíčně.

## 3c. DRUHÁ OPRAVA: katalog workspace je uzavřený na osmnácti

Návrh z 3b padl taky, a tentokrát celý. Uživatel v konzoli hledal `gpt`
a dostal **přesně sedm položek** — `gpt-image-1.5`, `gpt-5.5-eu`,
`gpt-oss-120b`, `gpt-5.4-mini`, `gpt-image-2`, `gpt-5.6-sol-eu`,
`gpt-5.6-terra-eu`.

To je do jednoho **týž seznam, jaký vrací `/v1/models` jako už zapnutý.**
Žádný `gpt-5-nano`, žádný `gpt-4.1-nano`, žádný `gpt-oss-20b`.

**Picker „Add model" tedy nenabízí katalog nexos.ai, ale katalog povolený
tvojí organizací — a ten je vyčerpaný.** Ve workspace není co přidávat.
Rozšířit ho může jen správce organizace; dokumentace to říká rovnou:
*„If your desired model isn't available, contact your workspace
administrator, as organization settings control the model list."*

Odtud plyne pravidlo, které jsem měl uplatnit hned na začátku:

> **`/v1/models/all` je katalog nexos.ai, ne tvoje nabídka.** Jediný
> spolehlivý seznam toho, co lze použít, je `/v1/models` — a ten je
> dnes osmnáctipoložkový. Doporučovat cokoliv mimo něj je plácnutí
> do vody.

### Co z osmnáctky opravdu jde použít

Chat modelů je z nich čtrnáct. Změřené jsou čtyři nejlevnější a výsledek
je jednoznačný: **použitelné jsou dva.**

| alias | model | $/M | naměřeno |
|---|---|---|---|
| `cheap` | GPT 5.4 mini | 0,750 / 4,500 | 20/20 detekce, 8/10 přepis |
| `cheap-fallback` | Claude Haiku 4.5 | 1,100 / 5,500 | 20/20 detekce, 9/10 přepis |
| `workhorse` | GPT 5.4 mini | 0,750 / 4,500 | dtto |
| `backstop` | Claude Haiku 4.5 | 1,100 / 5,500 | dtto |
| `reasoning` | GPT-OSS 120b | 0,800 / 1,600 | reasoning model, na 8000 tokenů v pořádku |

`backstop` na Haiku je přitom **obrat o 180 stupňů proti původnímu záměru**:
dnešní `gpt-oss-20b:free` byl vybrán jako „poslední záchrana, která aspoň
něco vrátí", a tady by to byl nejkvalitnější model celého řetězu. Za
1,100/5,500 $/M. To není návrh, to je důsledek toho, že levněji nic není.

## 4. Cena

Modelový měsíc na změřených profilech volání (50 hledání/den, indexace
10 dokumentů/den, 20 dotazů Krytona/den; velikosti promptů jsou z měření
zapsaných v paměti projektu):

| varianta | $/měsíc | poměr |
|---|---|---|
| OpenRouter, dnešní stav (vč. 5,5 % při nákupu kreditů) | **0,0086** | 1× |
| nexos.ai po zapnutí levných modelů | **0,3556** | 41× |
| nexos.ai jen z toho, co je dnes zapnuté | **1,9869** | 230× |

Čtyřicetinásobek vypadá drasticky, ale **absolutní čísla jsou obě
zanedbatelná**: rozdíl je devět centů proti šestatřiceti. Poměr je tak
vysoký hlavně proto, že dnešní sestava jede ze tří pětin na `:free`
a na big-pickle ve free preview — tedy na nule, kterou nelze podlézt.

Cenu tedy tahle úvaha **nerozhoduje**. Rozhodují vlastnosti, které se za
těch třicet centů kupují:

| co dnes bolí | vyřeší nexos.ai? |
|---|---|
| denní strop 50/den na free tieru ([cheap-alias-denni-limit]) | ano — placené modely strop nemají |
| `upstream_provider_shared_pool` 429 (P6) | ano — jiná infrastruktura, ne sdílený free fond |
| vyčerpávající se kvóta big-pickle (P8) | ano — placený model |
| big-pickle trénuje na tvých datech (P8) | ano — odpadá tím, že se big-pickle opustí |
| data mimo EU | **částečně** — viz oprava níže, EU je 48 ze 164 variant |

A co se naopak ztratí:

- **Katalog je uzavřený a kurátorovaný.** Dnešní volnost „najdi si
  nejlevnější placený model ze 378" na nexos.ai neexistuje; vybíráš ze
  145 a zapínáš přes konzoli.
- **Fallbacky umí gateway sama**, ale retry na úrovni gatewaye
  dokumentace nezmiňuje. `num_retries` v LiteLLM tedy zůstává jediná
  ochrana proti krátkému zahlcení — což je přesně to, co P6 řešilo.
- **Rozpočty jsou per tým/uživatel, ne per klíč.** Dnešní vzorec
  „každá komponenta má virtual key s vlastním rozpočtem" musí zůstat
  v LiteLLM; nexos.ai ho nenahradí.

---

## 5. Hlas (STT) — jediná část, kterou lze zkusit hned

Přepis hlasu z Telegramu jde **mimo LiteLLM**, přímo na OpenRouter:
quadlet předává `openrouter_api_key` jako `STT_API_KEY`
(`scripts/03-quadlets.sh:449`). `kryton/app/stt.py` má ale poskytovatele
parametrizovaného, ne zadrátovaného — takže přesun je čistě změna
prostředí, bez zásahu do kódu:

```
STT_BASE_URL=https://api.nexos.ai/v1/audio/transcriptions
STT_MODEL=Whisper 1
STT_API_KEY=<klíč nexos.ai>
```

**A hlavně: `Whisper 1` (Azure) je jeden ze čtrnácti modelů, které máš
zapnuté už teď.** Je to tedy jediný kus migrace, který nečeká na zásah
v konzoli. Krok 3 telegramového můstku podle poznámek stejně čeká na
nasazení a živé ověření, takže se to potkává.

Nezměřeno: kvalita přepisu češtiny ani cena (u `Whisper 1` katalog
per-token cenu nevrací, účtuje se nejspíš po minutách).

---

## 6. Měření kvality — co jde zavolat dnes

`scripts/22-eval-nexos-models.py`, 132 volání, 2026-08-18, ze stanice.
Korpus i prompty jsou doslova z `17-eval-cheap-models.py`, takže poslední
řádek (OpenRouter) je přímo srovnatelný.

| model | $/M | detekce jazyka | přepis dotazu | medián |
|---|---|---|---|---|
| Gemini 3 Flash Preview | 0,500 / 3,000 | **0/20** | **0/10** | 1,48 s |
| GPT 5.4 mini | 0,750 / 4,500 | 20/20 | 8/10 | 1,32 s |
| GPT-OSS 120b | 0,800 / 1,600 | **0/20** | **0/10** | 0,63 s |
| Claude Haiku 4.5 | 1,100 / 5,500 | 20/20 | 9/10 | 0,86 s |
| — *ling-2.6-flash / OpenRouter, 2026-08-09* | *0,010 / 0,030* | *20/20* | *10/10* | *0,88 s* |

Celé měření stálo 0,031459 kreditu.

### Dvě nuly nejsou špatná kvalita, je to známá past

`Gemini 3 Flash Preview` i `GPT-OSS 120b` jsou **reasoning modely**:
tokeny padnou na `reasoning_content` dřív, než začnou psát odpověď.
Při `max_tokens=20` (detekce jazyka) tedy vrátí prázdný `content`
s `finish_reason=length` — Gemini vykázalo přesně `reasoning_tokens=17`
z dvaceti. Při `max_tokens=120` (přepis) doběhne JSON jen do `{"lang":`
a rozparsovat se nedá.

**Je to přesně tatáž past, která už v tomhle projektu chytila
`big-pickle`, `ling-3.0-flash` a `qwen3.7-flash`.** Nová informace není
past sama, ale to, že **tři ze čtyř zapnutých modelů na ni doplácejí** —
a že tím pádem je z dnešní zapnuté nabídky pro roli `cheap` použitelný
jediný, `GPT 5.4 mini`. Pro roli `reasoning` jsou naopak v pořádku:
Kryton posílá `ANSWER_MAX_TOKENS=8000`, kde na úvahu místo je.

### Obě kvalitní modely spadly na stejném dotazu

`GPT 5.4 mini` i `Claude Haiku 4.5` určily
„Proč jsou databases running faster po zvýšení paměti?" jako **cs místo en**.
Haiku k tomu navíc ztratilo `faster` (přepsalo na `běh`), což je právě ta
normalizace, kterou `REWRITE_PROMPT` výslovně zakazuje, protože zabíjí
shodu v lexikální větvi.

Je poctivé dodat, že ten dotaz je česká věta s anglickými slovy, takže
`en` je sporná pravda; ale je to **týž test, na kterém 2026-08-10
propadly oba Ministraly a který `ling` prošel** — jako srovnání tedy platí.

Stylistický tik na okraj: `Claude Haiku 4.5` začalo dvě ze tří odpovědí
na volné psaní markdownovým nadpisem `# …`, ačkoliv SYSTEM prompt Krytona
chce plynulou češtinu. Stejná kategorie drobnosti jako „Viedeň" u `linga`.

---

## 7. Závěr

**Cenově nexos.ai nevyhraje a vyhrát nemůže.** Tři z pěti dnešních aliasů
jedou na nule (`:free`, resp. free preview u big-pickle) a nulu nelze
podlézt. Nejlevnější chat model v celém katalogu nexos.ai je 5× dražší
na vstupu a 13× na výstupu než dnešní `ling-2.6-flash`. V absolutních
číslech je to ale devět centů proti šestatřiceti měsíčně — tedy částka,
která o ničem rozhodovat nemá.

**Co za tu částku dostaneš** je věc, kterou dnešní sestava nemá a která
tenhle projekt už čtyřikrát pokousala: konec denního stropu 50/den,
konec `upstream_provider_shared_pool` (P6), konec vyčerpávající se kvóty
big-pickle (P8), a konec toho, že obsah tvých poznámek podle dokumentace
OpenCode Zen smí sloužit k trénování modelu. K tomu `region: EU`
u všeho a cena každého volání přímo v odpovědi.

**Jenže to zatím nejde vyzkoušet.** Ze čtrnácti zapnutých chat modelů je
pro roli `cheap` použitelný jediný a stojí 75× víc než dnešek. Čtyři
z pěti navržených modelů jsou v konzoli vypnuté.

### Co udělat dál, v tomhle pořadí

1. **Zapnout v konzoli** (Models → add a model) pět modelů z návrhu.
   U `GPT-OSS 120b` si pohlídat variantu **Azure** (0,150/0,600), ne tu
   od nexos.ai (0,800/1,600) — je 5,3× dražší za týž model.
2. `python3 scripts/23-nexos-pripravenost.py` — musí projít všech pět.
3. Do `MODELS` v `scripts/22-eval-nexos-models.py` doplnit `GPT 5 nano`
   a `Gemma 4 31B` a přeměřit. Bez toho je `cheap-nexos` jen odhad.
4. Teprve pak secret, quadlet a `systemctl restart litellm` — aliasy
   `*-nexos` už v configu jsou a ostrý provoz se jimi nedotkne.
5. Hlas (STT) jde zkusit kdykoliv, `Whisper 1` je zapnutý — viz kapitola 5.

Než tohle proběhne, **ostrý provoz zůstává na OpenRouteru**. Souběžné
aliasy jsou v configu právě proto, aby přepnutí bylo rozhodnutí, ne
vedlejší efekt nasazení.

---

## 8. Doporučení po dvou opravách: stěhovat jen to, co jede na free tieru

Tři varianty, všechny proveditelné **dnes a bez zásahu do konzole**:

| | co kam | $/měsíc | poměr |
|---|---|---|---|
| **A** | dnešek beze změny | 0,0086 | 1× |
| **B** | všech pět aliasů na nexos.ai | 2,2700 | 263× |
| **C** | na nexos.ai jen `reasoning` a `workhorse` | ~1,59 | 185× |

**Doporučuji C.** Důvod není cena — všechny tři varianty jsou v řádu
korun. Důvod je, že C přesune právě ty aliasy, u kterých dnešní řešení
skutečně selhává, a nechá na pokoji ty, kde funguje:

* **`reasoning`** je jediná věc, kterou nexos.ai řeší doopravdy. Dnešní
  big-pickle má vyčerpávající se kvótu s neznámým rozvrhem (P8), a podle
  dokumentace OpenCode Zen smí obsah tvých poznámek včetně deníku sloužit
  k trénování modelu. `GPT-OSS 120b` je za 0,800/1,600 placený, v EU,
  a v řetězu je stejně nejdražší položkou (95 % účtu), takže hybrid
  nic neušetří — ale to nebyl cíl.
* **`workhorse`** jede na `:free`, tedy pod denním stropem 50/den. Přesně
  ten strop 2026-08-08 zaindexoval pět dokumentů se špatným jazykem.
  Za 0,16 $ měsíčně je to pryč.
* **`cheap` a `cheap-fallback` nechat na OpenRouteru.** Jsou už placené,
  strop nemají, a `ling-2.6-flash` je proti nejlepšímu dosažitelnému
  modelu na nexos.ai **75× levnější a zároveň lepší** (10/10 proti 8/10).
  Stěhovat je by byla čistá ztráta na obou stranách.
* **`backstop` nechat taky.** Je na `:free`, takže strop má — ale jeho
  role je „poslední záchrana", ne kvalita, a nahradit ho modelem za
  1,100/5,500 $/M popírá, proč tam je.

### Kdyby se katalog otevřel

Jestli se dá u správce organizace vyjednat rozšíření, stojí za to
`gpt-5-nano-eu` (0,060/0,440) a `gemini-2.5-flash-lite` (0,100/0,400).
S nimi by celá migrace vyšla na ~1,50 $/měsíc místo 2,27 a role `cheap`
by přestala být nesmysl. Bez nich je varianta B průchozí, ale platíš
75násobek za horší přepis dotazu.

### Co zůstává platné bez ohledu na volbu

Hlas (STT) — `Whisper 1` je zapnutý a `kryton/app/stt.py` má
poskytovatele parametrizovaného, takže je to změna tří proměnných
prostředí. Viz kapitola 5.

---

## 9. Kontrola, kterou jsem měl udělat dřív: co stojí tytéž modely na OpenRouteru

Doporučení v kapitole 8 mlčky předpokládalo, že „placený model" znamená
„jít na nexos.ai". Nezname. Katalog OpenRouteru je veřejný (`GET
https://openrouter.ai/api/v1/models`, bez klíče) a po jeho stažení vyšlo
tohle:

| model | OpenRouter $/M | nexos.ai $/M | poměr |
|---|---|---|---|
| `openai/gpt-oss-120b` | **0,030 / 0,170** | 0,800 / 1,600 | **27× / 9,4×** |
| `anthropic/claude-haiku-4.5` | 1,000 / 5,000 | 1,100 / 5,500 | 1,1× |
| `google/gemma-4-26b-a4b-it` | 0,070 / 0,340 | — | v katalogu není |

U Claude je rozdíl přesně ta platformní přirážka. **U `gpt-oss-120b` je
to 27×** — a je to týž model. nexos.ai si ho hostuje sám (`owned_by:
nexos.ai`) a ocenil ho vysoko; OpenRouter ho routuje na levné
poskytovatele (CoreWeave, DeepInfra, Novita, AkashML a další).

To znamená, že **odchod z free tieru nevyžaduje měnit poskytovatele.**
Obě věci, které dnes bolí, se dají vyřešit uvnitř OpenRouteru:

| alias | dnes | placená náhrada na OpenRouteru | proč je bezpečná |
|---|---|---|---|
| `workhorse` | `gemma-4-26b-a4b-it:free` | **`google/gemma-4-26b-a4b-it`** 0,070/0,340 | **týž model**, jen bez `:free`. Změřený 2026-08-09 na 20/20 detekce a 10/10 přepis a už nasazený jako `cheap-fallback`. Nulové riziko. |
| `backstop` | `gpt-oss-20b:free` | **`openai/gpt-oss-20b`** 0,030/0,130 | **týž model**, jen bez `:free`. |
| `reasoning` | `big-pickle` (Zen) | **`openai/gpt-oss-120b`** 0,030/0,170 | větší sourozenec dnešního `backstopu`, ale **NEZMĚŘENÝ** — viz níže |

### Čtyři varianty vedle sebe

| | co | $/měsíc | proti dnešku |
|---|---|---|---|
| A | dnešek (tři aliasy na free tieru) | 0,0086 | 1× |
| **D** | **OpenRouter, všechno placené** | **0,0887** | **10×** |
| C | hybrid s nexos.ai (`reasoning`+`workhorse`) | 1,5896 | 184× |
| B | všech pět aliasů na nexos.ai | 2,3756 | 275× |

**Varianta D je 18× levnější než C a 27× levnější než B**, a přitom dělá
totéž: ruší závislost na free tieru i na big-pickle. Doporučení
z kapitoly 8 tím padá — **nexos.ai pro tenhle projekt nepřináší nic,
co by OpenRouter neuměl levněji.**

Jediné, co má nexos.ai navíc, je `region: EU` u části modelů a cena
volání přímo v odpovědi. Za rozdíl 1,5 $ měsíčně to koupit lze, ale je
to jiná úvaha než ta, se kterou se začínalo (cena).

### Co je nutné doměřit, než se cokoliv přepne

`gpt-oss-120b` v roli `reasoning` **není změřený** a je důvod k opatrnosti:
jeho menší sourozenec `gpt-oss-20b` v testu 2026-08-09 odpověděl česky
správně, ale obsahově SMYŠLENĚ — a věrohodný nesmysl je u Krytona horší
než chyba, protože si ho nikdo nevšimne. Přesně proto má
`scripts/24-eval-reasoning.py` dvě ze čtyř otázek jako pasti, kde
odpověď v úryvcích není a model musí říct, že ji nemá.

Skript měří obě cesty vedle sebe. Potřebuje ale klíč k OpenRouteru —
je dnes jen jako podman secret na brainu, takže pro běh ze stanice ho
dej do `~/.config/openrouter/key.env` jako řádek `OPENROUTER_API_KEY=...`
(stejný vzorec jako u nexos.ai), nebo skript spusť na brainu.

---

## 10. Varianta D — přesná změna v configu

Dvě z těch tří změn jsou **beze změny modelu**: jde jen o odebrání
přípony `:free`, tedy o týž model bez denního stropu. Ty se dají nasadit
bez měření, protože měřené jsou už dávno.

```diff
   - model_name: workhorse
     litellm_params:
-      model: openrouter/google/gemma-4-26b-a4b-it:free
+      model: openrouter/google/gemma-4-26b-a4b-it
       api_key: os.environ/OPENROUTER_API_KEY
       timeout: 90
       num_retries: 3

   - model_name: backstop
     litellm_params:
-      model: openrouter/openai/gpt-oss-20b:free
+      model: openrouter/openai/gpt-oss-20b
       api_key: os.environ/OPENROUTER_API_KEY
       timeout: 150
       max_tokens: 2500
       num_retries: 3
```

Třetí změna — `reasoning` z `big-pickle` na `openrouter/openai/gpt-oss-120b`
— **čeká na `scripts/24-eval-reasoning.py`**. U ní se mění model, ne jen
tarif.

### Co si u toho ohlídat

**`workhorse` a `cheap-fallback` budou mířit na týž model.** Dnes to
platí pro `workhorse`(free) a `cheap-fallback`(placená) jen zdánlivě —
jsou to různé tarify téhož modelu. Po změně budou identické. Nevadí to
(dělají jinou práci a fallback mezi nimi nevede), ale komentář
v configu o tom, že „hop workhorse → cheap byl no-op", tím dostává
druhý život: **nepřidávej mezi ně fallback**, byl by k ničemu ze stejného
důvodu jako tehdy.

**Zmizí důvod pro `cheap-fallback` jako samostatný alias?** Ne. `cheap`
padá na `cheap-fallback` a ten musí zůstat oddělený kvůli virtual key
oprávněním — klíč `retrieval-service` smí `{cheap, cheap-fallback}`,
`workhorse` ne.

**Rozpočet.** Tři aliasy přestanou být zdarma. Dnešní virtual keys mají
`max_budget` nastavený na 5 a 20 USD / 30 dní, takže odhadovaných
0,09 $/měsíc se do nich vejde s obrovskou rezervou — ale stojí za to se
po prvním měsíci podívat do spend logu, protože **odhad stojí na
předpokládaném objemu** (50 hledání a 20 dotazů denně), ne na měření.

**Co se tím NEVYŘEŠÍ.** P6 (`upstream_provider_shared_pool`) mířil na
sdílený fond zdarma — ten placené modely obchází. Ale krátkodobé 429 od
poskytovatele existují i u placených; `num_retries: 3` proto zůstává.

---

## 11. Měření role `reasoning` — pět kandidátů

`scripts/24-eval-reasoning.py`, 2026-08-18, ze stanice. Čtyři otázky nad
týmiž úryvky, `max_tokens=8000` a SYSTEM prompt doslova z Krytona. Dvě
otázky jsou **pasti**: odpověď v úryvcích není a model má říct, že ji nemá.

| model | kvalita | medián | max | $/M | $/měsíc |
|---|---|---|---|---|---|
| `openai/gpt-oss-120b` @ OpenRouter | 4/4 | **11,0 s** | 33,6 s | 0,030/0,170 | 0,059 |
| `openai/gpt-oss-20b` @ OpenRouter | 4/4 | 5,1 s | — | 0,030/0,130 | 0,057 |
| `google/gemma-4-26b-a4b-it` @ OpenRouter | 4/4 | 2,9 s | — | 0,070/0,340 | 0,134 |
| `GPT-OSS 120b` @ nexos.ai | 4/4 | **1,8 s** | — | 0,800/1,600 | 1,421 |
| `Claude Haiku 4.5` @ nexos.ai | 4/4 | 1,9 s | — | 1,100/5,500 | 1,920 |

**Kvalitativně neprošel nikdo hůř než ostatní.** Všech pět odpovědělo
na obě zodpověditelné otázky správně a s citacemi, a u obou pastí
přiznalo, že informaci nemá — několik z nich navíc samo odkázalo na
`/korpus`, jak jim SYSTEM prompt ukládá.

### Skóre v tabulce je z ručního přečtení, ne z metriky

Automatický detektor přiznání se spletl **třikrát** a pokaždé v neprospěch
modelu. Nejdřív neznal „není uvedena" a „nemohu odpovědět" (tři modely
tím spadly na 2/4), po doplnění dvanácti tvarů propadla gemma na větě:

> „V poskytnutých poznámkách **není informace o tom**, kolik stojí měsíční
> provoz serveru brain."

Což je samozřejmě přesně to správné chování. Závěr, zapsaný i ve skriptu:
**klíčová slova nad volným textem měří formulaci, ne chování, a doplňovat
další tvary to neřeší** — čeština jich má víc, než kdo vypíše. Skript
proto u pastí vždycky tiskne celou odpověď a skóre označuje jen jako
upozornění „na tohle se podívej".

### Rozdíl NENÍ v kvalitě, ale v latenci

`gpt-oss-120b` přes OpenRouter má medián **11,0 s** a maximum 33,6 s;
týž model na nexos.ai **1,8 s**. To je šestinásobek a u interaktivního
dotazu z Telegramu se to pozná.

Příčinu ukázalo opakované měření: OpenRouter během šesti volání routoval
na **čtyři různé poskytovatele** (DeepInfra, CoreWeave, AkashML,
DigitalOcean). Kvalitu to nezhoršilo — 12/12 na pastech — ale rychlost
kolísá podle toho, kdo zrovna vyhraje. nexos.ai si model hostuje sám,
takže je pomalejší už z principu nemá kde vzniknout.

Pozor při čtení: měřeno **ze stanice, ne z brainu**, a jde o jednotky
volání. Latence je orientační.

## 12. Doporučení

**Varianta D s `reasoning` na `openai/gpt-oss-120b`**, ale latenci
ošetřit. Za 0,089 $/měsíc (10× dnešek, 18× méně než hybrid s nexos.ai)
zmizí free tier i big-pickle a kvalita podle měření neklesne.

Tři věci, které k tomu patří:

1. **`workhorse` a `backstop` přepnout hned** — je to týž model bez
   `:free`, měřený a nasazený. Diff je v kapitole 10.
2. **`reasoning` přepnout až po ošetření latence.** OpenRouter umí
   omezit routing na konkrétní poskytovatele (`provider.only`, případně
   `provider.sort: "throughput"`). Doměřit, jestli to medián 11 s srazí
   k těm třem sekundám, které dává nexos.ai. **Neověřeno** — tohle je
   jediný otevřený bod celé analýzy.
3. **Kdyby to nešlo srazit**, je `gpt-oss-20b` (5,1 s, 4/4, 0,057 $/měs)
   rozumný kompromis, nebo `gemma-4-26b` (2,9 s, 4/4, 0,134 $/měs) —
   a to je model, který v tomhle projektu běží od začátku a je změřený
   nejlíp ze všech.

**nexos.ai zůstává neopodstatněné.** Za 1,4 $ měsíčně navíc kupuje
latenci 1,8 s místo 11 s u téhož modelu — což je jediná věc, kterou
umí líp, a která jde nejspíš vyřešit i na OpenRouteru zadarmo.

---

## 13. Současný stav proti navrhovanému

| alias | | model | poskytovatel | tarif | $/M | $/měsíc |
|---|---|---|---|---|---|---|
| **`reasoning`** | dnes | `openai/big-pickle` | **OpenCode Zen** | free preview | 0 / 0 | 0,0000 |
| | návrh | `openai/gpt-oss-120b` | OpenRouter | placený | 0,030 / 0,170 | 0,0586 |
| **`workhorse`** | dnes | `gemma-4-26b-a4b-it:free` | OpenRouter | **free tier** | 0 / 0 | 0,0000 |
| | návrh | `gemma-4-26b-a4b-it` | OpenRouter | placený | 0,070 / 0,340 | 0,0145 |
| **`cheap`** | obojí | `inclusionai/ling-2.6-flash` | OpenRouter | placený | 0,010 / 0,030 | 0,0082 |
| **`cheap-fallback`** | obojí | `gemma-4-26b-a4b-it` | OpenRouter | placený | 0,070 / 0,340 | 0,0000 |
| **`backstop`** | dnes | `gpt-oss-20b:free` | OpenRouter | **free tier** | 0 / 0 | 0,0000 |
| | návrh | `gpt-oss-20b` | OpenRouter | placený | 0,030 / 0,130 | 0,0028 |
| | | | | **celkem dnes** | | **0,0086** |
| | | | | **celkem návrh** | | **0,0887** |

Celkem včetně 5,5 % poplatku při nákupu kreditů. Objem je odhad —
50 hledání a 20 dotazů Krytona denně; skutečnost ukáže spend log.

Mění se **tři aliasy z pěti**, ale model se doopravdy mění jen u jednoho.
U `workhorse` a `backstop` jde o **týž model bez přípony `:free`**.

### Co se tím mění věcně

| | dnes | návrh |
|---|---|---|
| poskytovatelů | **dva** (OpenRouter + OpenCode Zen) | **jeden** |
| aliasů na free tieru | 3 z 5 | **0** |
| denní strop 50/den | `workhorse`, `backstop` | odpadá |
| kvóta big-pickle (P8) | vyčerpává se, rozvrh neznámý | odpadá |
| trénování na poznámkách | ano, dle dokumentace Zenu | odpadá |
| podman secrets | `openrouter_api_key`, `bigpickle_api_key` | **jen `openrouter_api_key`** |
| latence `reasoning` | neměřeno | **11,0 s medián — otevřené** |


---

## 14. OPRAVA odhadu: `workhorse` nedělá to, co o něm config tvrdil

Všechny odhady výše (kapitoly 4, 8, 9, 13) počítaly `workhorse` jako
300 volání měsíčně za „titulek a tagy při indexaci". **Taková úloha
v kódu neexistuje.**

Ověřeno grepem přes repozitář: jediné aliasy, které kód nastavuje jako
`model`, jsou `cheap` (`REWRITE_MODEL` v retrievalu) a `reasoning`
(`ANSWER_MODEL`, `ANALYTICS_MODEL` v Krytonovi). Indexer sahá na LLM jen
kvůli detekci jazyka a jde přes `cheap` — viz docstring `indexer.py`,
krok 3. `workhorse` se v kódu vyskytuje jedině v seznamu modelů, které
smí volat virtual key Krytona (`scripts/03-quadlets.sh:406`), a to právě
proto, aby fungoval fallback.

**Skutečná role `workhorse` je první záchyt fallbacku za `reasoning`.**
Za normálního provozu se nezavolá ani jednou.

### Opravená cena po kroku 1

| položka | volání/měs | $/měsíc |
|---|---|---|
| `cheap` — přepis dotazu + detekce jazyka | 1800 | 0,0082 |
| `reasoning` — big-pickle | 600 | 0,0000 |
| `workhorse` — při ~1 % propadu | 6 | 0,0013 |
| `backstop` — při ~0,2 % propadu | 1 | 0,0001 |
| **celkem** (vč. 5,5 %) | | **0,0102** |

Dřív uváděno 0,0887. Rozdíl je skoro celý ten neexistující úkol.
**Krok 1 tím nedává menší smysl — je jen levnější, než jsem tvrdil.**

Pro představu, co stojí porucha: při výpadku jako P8, kdy byl big-pickle
mrtvý čtyři dny a všechen provoz Krytona šel na `workhorse`, by celý
měsíc na gemmě vyšel na **0,134 $**. I havarijní režim je tedy v haléřích.

### Co z toho plyne pro výběr modelu

Kritérium u `workhorse` bylo napsané jako „mechanické úlohy, cena za
token rozhoduje víc než kvalita". Skutečné kritérium je **„umí odpovědět
na RAG dotaz s citacemi, když spadne `reasoning`"** — a přesně to se
dělo 2026-08-13 až 17, kdy tenhle alias tiše obsloužil všechen provoz
Krytona (P8).

Shodou okolností to dopadlo dobře: `gemma-4-26b-a4b-it` dala v měření
2026-08-18 **4/4 s mediánem 2,9 s**, tedy rychleji než `gpt-oss-120b`
(4/4 za 11,0 s). Zůstává proto beze změny — nově ale ze změřeného
důvodu, ne z omylu.

### Otevřená vada P8(b) bydlí právě tady

Kryton posílá `ANSWER_MAX_TOKENS=8000`, hodnotu zvolenou kvůli
big-pickle, a ta se aplikuje **i na fallbacky**. Gemma na tolik volnosti
stavěná není — 2026-08-17 se zacyklila a vyrobila 8000 tokenů za 743 s.

**Past: `max_tokens` v tomhle configu to nevyřeší.** Podle poznámky
v `kryton/app/config.py` má hodnota z requestu přednost před
konfigurační, takže strop určuje Kryton. Oprava musí být tam.

**Krok 2 ji umožní.** Naměřeno, že `gpt-oss-120b` spotřebuje na uvažování
medián 109 a maximum 184 tokenů, proti big-pickle, který jich sám utratil
402 a víc. Jakmile `reasoning` opustí big-pickle, `ANSWER_MAX_TOKENS`
může spadnout z 8000 na ~2000 a strop pro zacyklení se uzavře i pro
fallbacky.

Pořadí je tedy: **krok 2 → snížit `ANSWER_MAX_TOKENS` → zavřít P8(b).**
Dělat to dřív nejde, dokud `reasoning` jede na big-pickle, který 8000
potřebuje.
