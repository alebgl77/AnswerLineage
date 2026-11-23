"""Mettre un incident en quarantaine, la lever, et diffuser les périmètres en quarantaine.

La quarantaine est un objet à part, rattaché à l'incident (``incident_quarantine``) :
une ligne par activation, active puis levée. Son périmètre est celui de
l'incident, ses jeux de données. Les heures sont posées par la base, et la base
tient elle-même les états et les transitions.

- **Activer** est idempotent : un incident déjà en quarantaine la garde telle
  quelle, sans nouvelle annonce. Une activation prend un verrou sur la table, le
  temps de sa transaction : deux activations ne dépassent pas ensemble la borne
  des quarantaines actives (``MAX_ACTIVE_QUARANTINES``).
- **Lever** est idempotent aussi : une quarantaine déjà levée est rendue telle
  quelle. Une levée est définitive ; activer de nouveau ouvre une nouvelle
  quarantaine.
- **Annoncer.** Chaque activation et chaque levée met son annonce dans la file
  des webhooks, dans sa transaction, quand un webhook est en service
  (``webhooks.py``) ; une demande qui ne change rien n'annonce rien.
- **Diffuser.** Les proxys lisent les périmètres en quarantaine active, d'une
  lecture bornée, avec leur empreinte : une version qui ne change que si la
  liste change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.incidents.models import (
    MAX_DATASETS,
    ActiveQuarantine,
    ActiveQuarantines,
    Quarantine,
    QuarantineChange,
    QuarantinedDataset,
    in_utc,
)
from answerlineage_api.incidents.webhooks import announce
from answerlineage_api.lineage.reading import Reader
from answerlineage_api.store.schema import MAX_ACTIVE_QUARANTINES, QuarantineMode

# Une valeur d'If-None-Match plus longue n'est pas lue : la liste est alors rendue.
MAX_IF_NONE_MATCH: Final = 1024

_COLUMNS = "state, mode, activated_at, lifted_at"

# Les activations passent l'une après l'autre ; les lectures ne sont pas retenues.
_LOCK = text("LOCK TABLE incident_quarantine IN SHARE ROW EXCLUSIVE MODE")
_INCIDENT = text("SELECT 1 FROM incident WHERE id = :id")
_ACTIVE = text(
    f"SELECT {_COLUMNS} FROM incident_quarantine WHERE incident_id = :id AND state = 'active'"  # noqa: S608 — colonnes constantes.
)
_LAST = text(
    f"""
    SELECT {_COLUMNS} FROM incident_quarantine WHERE incident_id = :id
    ORDER BY id DESC LIMIT 1
    """  # noqa: S608 — seules des colonnes constantes sont interpolées.
)
_ACTIVE_COUNT = text("SELECT count(*) FROM incident_quarantine WHERE state = 'active'")
_ACTIVATE = text(
    f"""
    INSERT INTO incident_quarantine (incident_id, mode) VALUES (:id, :mode)
    RETURNING {_COLUMNS}
    """  # noqa: S608 — seules des colonnes constantes sont interpolées.
)
# L'heure de la levée suit l'activation, même si l'horloge a reculé entre les deux.
_LIFT = text(
    f"""
    UPDATE incident_quarantine
    SET state = 'lifted', lifted_at = GREATEST(clock_timestamp(), activated_at)
    WHERE incident_id = :id AND state = 'active'
    RETURNING {_COLUMNS}
    """  # noqa: S608 — seules des colonnes constantes sont interpolées.
)
_ACTIVE_LIST = text(
    """
    SELECT incident_id, mode, activated_at FROM incident_quarantine
    WHERE state = 'active'
    ORDER BY activated_at, incident_id
    LIMIT :limit
    """
)
_PERIMETERS = text(
    """
    SELECT incident_id, namespace, name FROM incident_dataset
    WHERE incident_id = ANY(:ids)
    ORDER BY incident_id, namespace COLLATE "C", name COLLATE "C"
    LIMIT :limit
    """
)


class QuarantineLimitError(Exception):
    """La borne des quarantaines actives est atteinte : en lever une d'abord."""


class NotQuarantinedError(Exception):
    """L'incident n'a jamais été mis en quarantaine : il n'y a rien à lever."""


def _quarantine(row: Row[Any]) -> Quarantine:
    lifted_at: datetime | None = row.lifted_at
    return Quarantine(
        state=row.state,
        mode=row.mode,
        activated_at=in_utc(row.activated_at),
        lifted_at=None if lifted_at is None else in_utc(lifted_at),
    )


async def activate(
    conn: AsyncConnection, incident_id: UUID, mode: QuarantineMode
) -> QuarantineChange | None:
    """Met l'incident en quarantaine, dans la transaction de ``conn`` ; ``None`` s'il est inconnu.

    Lève ``QuarantineLimitError`` quand la borne des quarantaines actives est atteinte.
    """
    await conn.execute(_LOCK)
    if (await conn.execute(_INCIDENT, {"id": incident_id})).first() is None:
        return None
    active = (await conn.execute(_ACTIVE, {"id": incident_id})).first()
    if active is not None:
        return QuarantineChange(
            incident_id=incident_id, changed=False, quarantine=_quarantine(active)
        )
    if (await conn.execute(_ACTIVE_COUNT)).scalar_one() >= MAX_ACTIVE_QUARANTINES:
        raise QuarantineLimitError
    created = (await conn.execute(_ACTIVATE, {"id": incident_id, "mode": mode})).one()
    await announce(conn, incident_id, "incident.quarantined")
    return QuarantineChange(incident_id=incident_id, changed=True, quarantine=_quarantine(created))


async def lift(conn: AsyncConnection, incident_id: UUID) -> QuarantineChange | None:
    """Lève la quarantaine active de l'incident, dans la transaction de ``conn`` ; ``None``
    s'il est inconnu.

    Lève ``NotQuarantinedError`` quand l'incident n'a jamais été mis en quarantaine.
    """
    lifted = (await conn.execute(_LIFT, {"id": incident_id})).first()
    if lifted is not None:
        await announce(conn, incident_id, "incident.quarantine_lifted")
        return QuarantineChange(
            incident_id=incident_id, changed=True, quarantine=_quarantine(lifted)
        )
    last = (await conn.execute(_LAST, {"id": incident_id})).first()
    if last is not None:
        return QuarantineChange(
            incident_id=incident_id, changed=False, quarantine=_quarantine(last)
        )
    if (await conn.execute(_INCIDENT, {"id": incident_id})).first() is None:
        return None
    raise NotQuarantinedError


def version_of(quarantines: list[ActiveQuarantine]) -> str:
    """L'empreinte de la liste : SHA-256 de sa forme JSON canonique, en hexadécimal."""
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in quarantines],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Diffusion:
    """Les périmètres en quarantaine active et leur version, l'ETag de la réponse."""

    quarantines: ActiveQuarantines

    @property
    def etag(self) -> str:
        return f'"{self.quarantines.version}"'

    def matches(self, if_none_match: str | None) -> bool:
        """Vrai quand l'appelant tient déjà cette version (comparaison faible, RFC 9110)."""
        if if_none_match is None or len(if_none_match) > MAX_IF_NONE_MATCH:
            return False
        for candidate in if_none_match.split(","):
            tag = candidate.strip()
            if tag == "*" or tag.removeprefix("W/") == self.etag:
                return True
        return False


async def active_quarantines(reader: Reader) -> Diffusion:
    """Les quarantaines actives, chacune avec le périmètre de son incident, et leur version."""
    found = await reader.fetch(_ACTIVE_LIST, {}, limit=MAX_ACTIVE_QUARANTINES)
    ids = [row.incident_id for row in found.rows]
    datasets: dict[UUID, list[QuarantinedDataset]] = {incident_id: [] for incident_id in ids}
    if ids:
        perimeters = await reader.fetch(_PERIMETERS, {"ids": ids}, limit=len(ids) * MAX_DATASETS)
        for row in perimeters.rows:
            datasets[row.incident_id].append(
                QuarantinedDataset(namespace=row.namespace, name=row.name)
            )
    quarantines = [
        ActiveQuarantine(
            incident_id=row.incident_id,
            mode=row.mode,
            activated_at=in_utc(row.activated_at),
            datasets=datasets[row.incident_id],
        )
        for row in found.rows
    ]
    return Diffusion(ActiveQuarantines(version=version_of(quarantines), quarantines=quarantines))
