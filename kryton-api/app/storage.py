"""Originály nahraných dokumentů na S3.

Záměrně jen S3-kompatibilní volání, žádná znalost konkrétního poskytovatele:
výměna endpointu a migrace jsou požadavek, ne možnost. Konkrétní úložiště
určuje profil (`S3_PROFILE`) a jeho jméno se ukládá ke každému souboru —
bez toho by po migraci nešlo poznat, kde který originál leží.

Klíč objektu je odvozený z obsahu (`<prefix><sha256><přípona>`), takže dvojí
nahrání téhož souboru nevytvoří duplicitu. Důsledek, který je potřeba mít
na paměti: jeden objekt může patřit víc dokumentům, takže mazat originál
při smazání dokumentu jde jen s počítáním odkazů. Proto se odsud nemaže.
"""
from __future__ import annotations

import logging
import re

from . import config

log = logging.getLogger("kryton")

_client = None

# Region je u Backblaze součástí hostitele: s3.<region>.backblazeb2.com.
# Stejný tvar má i řada dalších poskytovatelů, takže se to zkouší obecně.
_REGION_Z_HOSTU = re.compile(r"^(?:https?://)?s3[.-]([a-z0-9-]+)\.", re.I)


def enabled() -> bool:
    return bool(config.S3_ENDPOINT and config.S3_BUCKET
                and config.S3_ACCESS_KEY_ID and config.S3_SECRET_ACCESS_KEY)


def region() -> str:
    if config.S3_REGION:
        return config.S3_REGION
    m = _REGION_Z_HOSTU.match(config.S3_ENDPOINT)
    return m.group(1) if m else "us-east-1"


def client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config as BotoConfig
        _client = boto3.client(
            "s3", endpoint_url=config.S3_ENDPOINT, region_name=region(),
            aws_access_key_id=config.S3_ACCESS_KEY_ID,
            aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
            config=BotoConfig(signature_version="s3v4",
                              retries={"max_attempts": 3}))
    return _client


def object_key(sha256_hex: str, ext: str) -> str:
    ext = ext if ext.startswith(".") else ("." + ext if ext else "")
    return "%s%s%s" % (config.S3_KEY_PREFIX, sha256_hex, ext.lower())


def exists(key: str) -> bool:
    try:
        client().head_object(Bucket=config.S3_BUCKET, Key=key)
        return True
    except Exception:
        return False


def put_original(data: bytes, key: str, original_name: str, mime: str) -> str:
    """Uloží originál. Když už objekt se stejným klíčem je, nepřepisuje.

    Klíč je hash obsahu, takže shodný klíč znamená shodný obsah — přepis by
    přenesl stejné bajty znovu a jen zaplatil přenos.
    """
    if exists(key):
        log.info("originál %s uz na S3 je, preskakuji upload", key)
        return key
    client().put_object(
        Bucket=config.S3_BUCKET, Key=key, Body=data,
        ContentType=mime or "application/octet-stream",
        # Metadata drží původní název, aby šel objekt identifikovat i bez
        # databáze. Ověřeno, že je Backblaze zachovává.
        Metadata={"original-name": _ascii_metadata(original_name)})
    return key


def _ascii_metadata(value: str) -> str:
    """Hlavičky HTTP unesou jen ASCII, a český název je běžně mimo.

    Diakritika se přepíše, ne zahodí celý název — jde o vodítko pro člověka,
    který se dívá do bucketu, autoritativní název je v databázi.
    """
    import unicodedata
    out = (unicodedata.normalize("NFKD", value or "")
           .encode("ascii", "ignore").decode())
    return out[:200] or "bez-nazvu"


def _backup_bucket() -> str:
    return config.BACKUP_S3_BUCKET or config.S3_BUCKET


def put_backup(data: bytes, key: str) -> None:
    """Uloží zálohu (DB dump nebo šifrovaný balík secrets).

    Na rozdíl od `put_original` PŘEPISUJE bez podmínky — klíč nese
    timestamp, ne hash obsahu, takže žádná deduplikace nemá smysl a stejný
    klíč by v praxi nikdy nemělo vzniknout dvakrát.
    """
    client().put_object(Bucket=_backup_bucket(), Key=key, Body=data,
                        ContentType="application/octet-stream")


def get_backup(key: str) -> bytes:
    r = client().get_object(Bucket=_backup_bucket(), Key=key)
    return r["Body"].read()


def list_backups(prefix: str | None = None) -> list[dict]:
    """Zálohy pod prefixem (výchozí `BACKUP_S3_PREFIX`), od nejstarší.

    Paginuje explicitně — `list_objects_v2` vrací nejvýš 1000 klíčů na
    stránku, a s denními zálohami víc DB se to jednou za pár let stane.
    """
    out = []
    token = None
    while True:
        kw = {"Bucket": _backup_bucket(), "Prefix": prefix or config.BACKUP_S3_PREFIX}
        if token:
            kw["ContinuationToken"] = token
        r = client().list_objects_v2(**kw)
        out.extend({"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"]}
                   for o in r.get("Contents", []))
        if not r.get("IsTruncated"):
            break
        token = r["NextContinuationToken"]
    out.sort(key=lambda o: o["last_modified"])
    return out


def delete_backup(key: str) -> None:
    client().delete_object(Bucket=_backup_bucket(), Key=key)


def check() -> str:
    """Dostupnost úložiště. Vrací krátký popis, nebo vyhodí výjimku.

    Testuje se `list_objects_v2` nad bucketem, NE `list_buckets`
    ani `head_bucket`: klíč je omezený na jeden bucket a obojí zmíněné
    vrací `AccessDenied: not entitled`. Ověřeno 2026-08-09.
    """
    r = client().list_objects_v2(Bucket=config.S3_BUCKET, MaxKeys=1)
    return "profil %s, bucket %s, region %s, objektů aspoň %s" % (
        config.S3_PROFILE, config.S3_BUCKET, region(), r.get("KeyCount", 0))
