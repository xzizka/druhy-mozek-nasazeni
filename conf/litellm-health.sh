#!/bin/sh
# Healthcheck pro LiteLLM — v ZIPu nebyl, přidáno při nasazení.
#
# Dva důvody, proč to není inline v quadletu:
#
# 1) Image ghcr.io/berriai/litellm neobsahuje curl ani wget, takže původní
#    HealthCmd=curl -fsS http://localhost:4000/health/liveliness nemohl nikdy
#    projít a kontejner byl trvale "unhealthy".
#
# 2) Quadlet při parsování HealthCmd spolkne koncovou dvojitou uvozovku.
#    Z HealthCmd=python3 -c "import ..." se v podmanu stane
#      ["CMD-SHELL", "python3 -c \"import ..."]
#    tedy bez uzavírací uvozovky, a healthcheck spadne na
#      /bin/sh: syntax error: unterminated quoted string
#    Proto quadlet volá jen `sh /health.sh` — žádné uvozovky, žádný problém.
exec python3 -c "import urllib.request as u; u.urlopen('http://localhost:4000/health/liveliness', timeout=5)"
