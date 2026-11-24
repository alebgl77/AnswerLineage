"""Relevés de couverture de l'entreprise synthétique.

Chaque serveur MCP de l'entreprise est un émetteur, ``mcp://<serveur>``,
comme le proxy placé devant lui. Ses relevés couvrent tout l'intervalle des
demandes, jour par jour (UTC), y compris les jours où il n'a servi aucun
appel : le serveur n'est jamais muet. L'entreprise ne perd rien : chaque
appel vu a son événement, et aucune requête n'échappe à l'inspection. La
couverture lue après chargement est donc mesurée, sans trou.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from answerlineage_corpus.enterprise import Enterprise

DAY = timedelta(days=1)


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _midnight(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def coverage_reports(enterprise: Enterprise) -> list[dict[str, Any]]:
    """Un relevé par serveur et par jour, du premier jour des demandes au lendemain du dernier."""
    calls = [call for request in enterprise.requests for call in request.calls]
    moments = [
        moment
        for request in enterprise.requests
        for moment in (request.started_at, request.ended_at)
    ]
    if not moments:
        return []
    servers = sorted({tool.server for tool in enterprise.tools})
    seen = Counter((call.tool.server, _midnight(call.time)) for call in calls)
    first, last = _midnight(min(moments)), _midnight(max(moments))
    reports = []
    for server in servers:
        day = first
        while day <= last:
            count = seen[server, day]
            reports.append(
                {
                    "emitter": f"mcp://{server}",
                    "window": {"from": _instant(day), "to": _instant(day + DAY)},
                    "calls_seen": count,
                    "events_emitted": count,
                    "events_lost": 0,
                    "uninspected": 0,
                }
            )
            day += DAY
    return reports
