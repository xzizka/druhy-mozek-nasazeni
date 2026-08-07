-- =====================================================================
-- Fáze 1 / krok 1: role a databáze
-- Spouštěj jako superuser (postgres) proti databázi postgres.
--
-- Proč oddělené databáze a role: jedna instance je kompromis kvůli 16 GB,
-- ale hranice odpovědnosti zůstává vynucená na úrovni grantů. Žádná
-- komponenta nemá přístup do databáze, kterou nevlastní. To je jediná
-- část původního tří-instančního návrhu, která má cenu i na jednom procesu.
-- =====================================================================

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------
-- Role. Hesla nastav přes psql -v nebo je po vytvoření přepiš ze secret
-- storu; nikdy je nenechávej v gitu.
-- ---------------------------------------------------------------------
CREATE ROLE kryton_app    LOGIN PASSWORD :'kryton_pw';
CREATE ROLE retrieval_app LOGIN PASSWORD :'retrieval_pw';
CREATE ROLE litellm_app   LOGIN PASSWORD :'litellm_pw';

-- Read-only role pro ad-hoc dotazy a pozdější eval loop. Nechceš se
-- k datům dostávat aplikačním účtem.
CREATE ROLE platform_ro   LOGIN PASSWORD :'ro_pw';

-- ---------------------------------------------------------------------
-- Databáze. Locale cs_CZ kvůli řazení; ICU je v PG17 default provider
-- a pro češtinu dává korektní collation včetně ch/č.
-- ---------------------------------------------------------------------
CREATE DATABASE kryton
    OWNER            kryton_app
    LOCALE_PROVIDER  icu
    ICU_LOCALE       'cs-CZ'
    TEMPLATE         template0
    ENCODING         'UTF8';

CREATE DATABASE retrieval
    OWNER            retrieval_app
    LOCALE_PROVIDER  icu
    ICU_LOCALE       'cs-CZ'
    TEMPLATE         template0
    ENCODING         'UTF8';

-- LiteLLM si schéma spravuje sám přes Prisma migrace, locale je mu jedno.
CREATE DATABASE litellm OWNER litellm_app TEMPLATE template0 ENCODING 'UTF8';

-- ---------------------------------------------------------------------
-- Odebrání implicitního PUBLIC práva. Od PG15 už PUBLIC nemá CREATE na
-- schématu public, ale CONNECT na databázi ano - a to nechceme.
-- ---------------------------------------------------------------------
REVOKE CONNECT ON DATABASE kryton    FROM PUBLIC;
REVOKE CONNECT ON DATABASE retrieval FROM PUBLIC;
REVOKE CONNECT ON DATABASE litellm   FROM PUBLIC;

GRANT CONNECT ON DATABASE kryton    TO kryton_app,    platform_ro;
GRANT CONNECT ON DATABASE retrieval TO retrieval_app, platform_ro;
GRANT CONNECT ON DATABASE litellm   TO litellm_app;

-- ---------------------------------------------------------------------
-- Rozšíření. CREATE EXTENSION vyžaduje superuser, proto tady a ne
-- v aplikační migraci.
-- ---------------------------------------------------------------------
\connect retrieval
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector >= 0.7 kvůli halfvec
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

\connect kryton
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Kontrola, že máme verzi pgvector s halfvec. Na 0.6.x tohle spadne
-- a je to záměr - halfvec je pro rozpočet paměti nutnost, ne ozdoba.
\connect retrieval
DO $$
DECLARE v text;
BEGIN
    SELECT extversion INTO v FROM pg_extension WHERE extname = 'vector';
    IF string_to_array(v, '.')::int[] < ARRAY[0,7,0] THEN
        RAISE EXCEPTION 'pgvector % je prilis stary, halfvec vyzaduje >= 0.7.0', v;
    END IF;
    RAISE NOTICE 'pgvector % OK', v;
END $$;
