#!/usr/bin/env bash
# =====================================================================
# Integrační kontrola analytiky (P1b) proti SKUTEČNÉ databázi.
#
# Doplněk k `13-smoke-kryton.py`, ne náhrada. Smoke test stubuje databázi,
# takže neodhalí nic, co se pokazí až na spojení — a přesně to se stalo:
# `SET LOCAL statement_timeout = %s` prošlo všemi 89 kontrolami a na živém
# Postgresu spadlo na `syntax error at or near "$1"`, protože SET LOCAL je
# utility příkaz a placeholder do něj nepatří.
#
# Spouštěj na brainu, když se sáhne na analytics.py:
#     /root/deploy/scripts/14-analytics-check.sh
# =====================================================================
set -uo pipefail

podman exec -i kryton python - <<'PY'
import sys

from app import analytics

FAIL = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("OK   " if ok else "CHYBA", name,
                         ("" if ok else " — " + str(detail))))
    if not ok:
        FAIL.append(name)


analytics.init()
check("analytika je zapnutá (DSN platform_ro)", analytics.enabled())

d = analytics.schema_description()
check("schéma se introspektuje", "retrieval.document(" in d and "retrieval.chunk(" in d,
      d[:120])
check("schéma zná sloupec lang z migrace 03-multilang", "lang " in d)

cols, rows = analytics.run_sql(
    "SELECT lang, count(*) AS pocet FROM retrieval.document "
    "GROUP BY lang ORDER BY 2 DESC")
check("agregační dotaz projde", cols == ["lang", "pocet"] and len(rows) >= 1,
      "%s %s" % (cols, rows[:4]))
print("     -> %s" % rows)

total = analytics.run_sql("SELECT count(*) AS n FROM retrieval.document")[1][0][0]
check("součet přes jazyky sedí na počet dokumentů",
      sum(r[1] for r in rows) == total, "%s vs %s" % (sum(r[1] for r in rows), total))

fp = analytics.fingerprint()
check("otisk korpusu má tvar pocet:cas", ":" in fp and fp.split(":")[0].isdigit(), fp)

check("strop řádků se vynutí i bez LIMIT v dotazu",
      len(analytics.run_sql("SELECT id FROM retrieval.chunk")[1])
      <= analytics.config.ANALYTICS_MAX_ROWS)

for bad in ["SELECT 1; DROP TABLE x", "DELETE FROM retrieval.document",
            "UPDATE retrieval.document SET title='x'"]:
    try:
        analytics.run_sql(bad)
        check("odmítne %r" % bad[:34], False, "prošlo")
    except analytics.AnalyticsError:
        check("odmítne %r" % bad[:34], True)

# Poslední vrstva: i kdyby kontrola řetězce selhala, role zapsat nesmí.
try:
    with analytics._pool.connection() as c:
        c.execute("UPDATE retrieval.document SET title = 'hacked'")
        c.commit()
    check("role platform_ro NEUMÍ zapisovat", False, "zápis prošel!")
except Exception as e:
    check("role platform_ro neumí zapisovat", "permission denied" in str(e).lower(),
          str(e)[:80])

print()
if FAIL:
    print("SELHALO %d: %s" % (len(FAIL), ", ".join(FAIL)))
    sys.exit(1)
print("VŠE PROŠLO")
PY
