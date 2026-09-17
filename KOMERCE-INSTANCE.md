# Komerční instance — provozní fakta

> Stav k 2026-09-17, větev `komerce`. Hodnoty tajemství v tomto souboru
> **nejsou** — žijí jen v podman secretech na LXC 202 a v heslech uložených
> mimo instance (viz README o nekrytých podman secretech).

## Infrastruktura (pve1)

- CTID 202, hostname `komerce`, IP `10.20.0.108/24`, unprivileged LXC, `nesting=1`.
- RAM 14 336 MB, cores 6 — **plus `cpulimit 3`** (`pct set 202 -cpulimit 3`):
  součet limitů brainu (18 GB/6 jader) a komerce je záměrně nad fyzikou
  hostitele; stroj brání komerci strhnout výkon brainu při špičkách
  (model-load Infinity, hromadné embeddingy). Při potřebách dočasně
  zvýšit, je to strop, ne rezervace.
- Rootfs 64 GB na `local` (directory storage — overlay nad ZFS nefunguje).
- `/dev/net/tun` (Tailscale) + `lxc.cgroup2.devices.allow: c 10:200 rwm`.
- `APP_ROOT=/srv/rag` (tenancy D7).

## Obrazy a verze (D1 — ověřeno 2026-09-17)

| služba | image / verze | poznámka |
|---|---|---|
| postgres | `localhost/postgres-cs:17` = PostgreSQL **17.11**, pgvector 0.8.6 | rebuild z `pgvector/pgvector:pg17` |
| litellm | `ghcr.io/berriai/litellm@sha256:a3715fa7…` = **1.100.1** | digest pinovaný v 03-quadlets |
| infinity | `docker.io/michaelf34/infinity:0.0.77-cpu` | CPU varianta (komerce běží `--engine torch --device cpu`); plná 0.0.77 je 9,4 GB, jen stahování |
| kryton | `localhost/kryton:latest`, Python **3.13.15** | python-multipart 0.0.32, pypdf 6.18.0, fastmcp 3.2.4, FastAPI 0.141.1, uvicorn 0.52.4, psycopg 3.3.5, boto3 1.43.92 |
| retrieval | `localhost/retrieval-service:latest`, Python **3.13.15** | FastAPI 0.141.1, psycopg 3.3.5 |

## S3 / zálohy (D2 — ověřeno 2026-09-17)

- Bucket **`kryton-commerce`** (Backblaze B2), region **`eu-central-003`**,
  endpoint `https://s3.eu-central-003.backblazeb2.com` — samostatný bucket
  od osobního brainu od počátku (D3 rozhodnutí).
- `BACKUP_S3_PREFIX=db-backups/`, `BACKUP_RETENTION_DAYS=30`.
- Zálohu a OVĚŘENÍ OBNOVY dělá `scripts/19-kryton-backup.sh` (dump →
  `pg_restore` do zahozitelné DB → porovnání řádků; secrets šifrovaně,
  ověří se jen sada jmen). Provedeno a ověřeno. Retrieval DB (derivovaný
  index) záměrně není — staví se z markdownu.
- Šifrovací klíč zálohy secrets a login heslo Krytona NESMÍ zůstat jen
  v LXC — kopie mimo instanci (password manager).

## LiteLLM virtual keys (pravidla dle NASAZENI)

| alias | smí volat | rozpočet | rpm |
|---|---|---|---|
| `kryton` | `reasoning`, `workhorse`, `cheap`, `cheap-fallback`, **`backstop`** | 20 USD / 30 d | 60 |
| `retrieval-service` | `cheap`, `cheap-fallback` | 5 USD / 30 d | 60 |

Hodnoty vrácené z `POST /key/generate` jsou v podman secretech
`litellm_kryton_key` / `litellm_retrieval_key`; z LiteLLM se zpět
přečíst nedají.

## Regenerace quadletů na komerci

    pct enter 202
    APP_ROOT=/srv/rag \
      S3_BUCKET=kryton-commerce \
      BACKUP_S3_PREFIX=db-backups/ \
      INFINITY_IMAGE=docker.io/michaelf34/infinity:0.0.77-cpu \
      /root/deploy/scripts/03-quadlets.sh
    systemctl daemon-reload

`02-guest-bootstrap.sh` instaloval na komerci navíc `passt` a `aardvark-dns`
(podmínka funkční DNS v podman síti; bez nich litellm padal v Prisma engine
na `gaierror` pro `postgres`).

## Tajnosti (jen SEZNAM, hodnoty jinde)

`litellm_master_key`, `litellm_salt_key`, `litellm_database_url`,
`openrouter_api_key` (litellm OPENROUTER_API_KEY i kryton STT_API_KEY),
`bigpickle_api_key`, `kryton_auth_password`, `kryton_session_secret`,
`kryton_database_url`, `litellm_kryton_key`, `litellm_retrieval_key`,
`pg_kryton_pw`, `pg_retrieval_pw`, `platform_ro_url`, `s3_access_key_id`,
`s3_secret_access_key`, `backup_encryption_key`, `mcp_bearer_token`.