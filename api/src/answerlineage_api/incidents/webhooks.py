"""Annonce des changements d'état d'un incident au récepteur de webhooks.

Chaque changement d'état met son message dans la file des webhooks
(``webhook_queue``), dans la transaction qui le fait : la déclaration de
l'incident par l'API, la fin de son analyse par un worker, faite ou en échec
pour de bon, l'activation et la levée de sa quarantaine par l'API. Un
changement annulé n'annonce donc rien, et un changement validé est annoncé.
Les workers qui ont un webhook vident la file (``answerlineage_workers.webhooks``).

- **Rien sans webhook en service.** Un worker qui a un webhook dit jusqu'à quand
  il est en service (``webhook_stats.in_service_until``) ; au-delà, ou sans
  webhook réglé, rien n'est mis en file.
- **Figé à l'heure du changement.** Le message reprend l'état de l'incident, de
  son analyse et ses décomptes, et sa dernière quarantaine, tels que la
  transaction vient de les écrire : un envoi tardif annonce ce qui était vrai alors.
- **Aucun texte libre.** Des identifiants, des codes de listes fermées, des
  heures et des nombres ; les pseudonymes des destinataires ne sont que comptés.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.store.schema import WebhookEvent

_ANNOUNCE = text(
    """
    INSERT INTO webhook_queue (
        event_type, incident_id, occurred_at, incident_state, severity,
        analysis_state, analysis_error, answers, recipients, lower_bound,
        quarantine_state, quarantine_mode
    )
    SELECT CAST(:event AS text), i.id,
           CASE CAST(:event AS text)
                WHEN 'incident.declared' THEN i.declared_at
                WHEN 'incident.quarantined' THEN q.activated_at
                WHEN 'incident.quarantine_lifted' THEN q.lifted_at
                ELSE s.finished_at END,
           i.state, i.severity, s.state, s.error,
           CASE WHEN s.state = 'done' THEN s.answers END,
           CASE WHEN s.state = 'done' THEN s.recipients END,
           CASE WHEN s.state = 'done' THEN s.truncated END,
           q.state, q.mode
    FROM incident i JOIN incident_analysis s ON s.incident_id = i.id
    LEFT JOIN LATERAL (
        SELECT state, mode, activated_at, lifted_at FROM incident_quarantine
        WHERE incident_id = i.id ORDER BY id DESC LIMIT 1
    ) q ON true
    WHERE i.id = :incident_id
      AND EXISTS (SELECT 1 FROM webhook_stats WHERE in_service_until > now())
    """
)


async def announce(conn: AsyncConnection, incident_id: UUID, event: WebhookEvent) -> bool:
    """Met en file le message de ``event`` pour l'incident, dans la transaction de ``conn``.

    Rend vrai quand le message est mis en file : un webhook est en service.
    """
    result = await conn.execute(_ANNOUNCE, {"incident_id": incident_id, "event": event})
    return bool(result.rowcount)
