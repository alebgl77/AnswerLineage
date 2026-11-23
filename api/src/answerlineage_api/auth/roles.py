"""Les rôles d'un appelant de l'API.

Six rôles de personnes, et un rôle technique : celui des émetteurs, qui
envoient les événements et les relevés de couverture. Un rôle de personne se
lit dans une revendication du jeton, à travers une table de correspondance
réglable ; le rôle d'émetteur ne se lit que de l'identifiant du client
(``azp`` ou ``client_id``), jamais d'une revendication de rôle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Role(StrEnum):
    """Un rôle : identifiant technique en anglais, nom métier dans ``LABELS``."""

    READER = "reader"
    DATA_STEWARD = "data_steward"
    AGENT_OWNER = "agent_owner"
    INCIDENT_MANAGER = "incident_manager"
    AUDITOR = "auditor"
    ADMINISTRATOR = "administrator"
    EMITTER = "emitter"


# Les rôles des personnes, ceux qu'une revendication du jeton peut donner.
PERSON_ROLES: Final = frozenset(role for role in Role if role is not Role.EMITTER)

LABELS: Final = {
    Role.READER: "lecteur",
    Role.DATA_STEWARD: "responsable de données",
    Role.AGENT_OWNER: "responsable d'agent",
    Role.INCIDENT_MANAGER: "gestionnaire d'incident",
    Role.AUDITOR: "auditeur",
    Role.ADMINISTRATOR: "administrateur",
    Role.EMITTER: "émetteur",
}


@dataclass(frozen=True)
class Caller:
    """L'appelant d'une route : ses rôles, et l'identité OIDC d'une personne quand le jeton
    en porte une admise. Une action tracée la garde comme auteur ; rien d'autre ne la lit."""

    roles: frozenset[Role]
    # L'émetteur OIDC qui a signé le jeton, et le sujet du jeton (« sub ») : ``None`` quand
    # l'authentification est désactivée, ou que le jeton ne porte pas de sujet admis.
    issuer: str | None = None
    subject: str | None = None
