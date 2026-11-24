"""L'identifiant de run d'un appel d'outil, porté par la requête qu'il fait à l'entrepôt.

Fonction de référence pour un serveur MCP qui exécute des requêtes dans un
entrepôt. Depuis le contexte de trace qu'il reçoit avec l'appel d'outil
(``params._meta.traceparent``, puis l'en-tête HTTP ``traceparent``), elle calcule
l'identifiant de run que le proxy d'AnswerLineage donne à cet appel, et ce que le
serveur pose sur sa requête pour le porter :

- un label de job BigQuery, ``answerlineage_run_id``, dont la valeur est
  l'identifiant ;
- le paramètre de session ``QUERY_TAG`` de Snowflake, un objet JSON qui n'a que
  la clé ``answerlineage_run_id`` ;
- un commentaire SQL, en tête de la requête, celui que le proxy ajoute lui-même
  sur option (``--sql-run-comment``).

Les journaux d'audit de l'entrepôt gardent le label, le tag et le texte de la
requête : les lectures s'y rapprochent ainsi de leur run.

Le module n'importe que la bibliothèque standard, et rien d'AnswerLineage : il
se recopie tel quel dans un serveur MCP. L'identifiant de run est celui que
décrit ``docs/proxy.md`` à l'octet près ; les vecteurs de
``spec/run-id/vectors-v1.json`` tiennent ce calcul d'accord avec le proxy.

Un contexte de trace mal formé est une entrée hostile : il est ignoré comme s'il
manquait, sans être recopié. Les valeurs rendues ne contiennent que
l'identifiant de run, sous sa forme canonique vérifiée, dans un gabarit fixe.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

# Clé du label BigQuery et du tag Snowflake, nom du champ du commentaire SQL.
LABEL_KEY: Final = "answerlineage_run_id"
# Étiquette de la dérivation de l'identifiant de run, première version.
RUN_ID_LABEL: Final = b"answerlineage/run-id/v1"
# Au-delà, un traceparent n'est pas lu : celui de la version 00 compte 55 caractères.
MAX_TRACEPARENT_LENGTH: Final = 512

_TRACEPARENT: Final = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace>[0-9a-f]{32})-(?P<parent>[0-9a-f]{16})"
    r"-[0-9a-f]{2}(?P<rest>-.*)?",
    re.DOTALL,
)
# Forme canonique d'un UUID : huit, quatre, quatre, quatre et douze hexadécimaux minuscules.
_RUN_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class WarehouseTags:
    """Ce qu'un serveur MCP pose sur la requête d'un appel d'outil pour porter son run."""

    # Identifiant de run, sous la forme canonique d'un UUID : 36 caractères, chiffres,
    # lettres de a à f et tirets. Toute autre valeur est refusée.
    run_id: str

    def __post_init__(self) -> None:
        if not (isinstance(self.run_id, str) and _RUN_ID.fullmatch(self.run_id)):
            raise ValueError("L'identifiant de run n'a pas la forme canonique d'un UUID.")

    @property
    def bigquery_labels(self) -> dict[str, str]:
        """Les labels du job BigQuery : une clé, une valeur, toutes deux admises par BigQuery."""
        return {LABEL_KEY: self.run_id}

    @property
    def snowflake_query_tag(self) -> str:
        """La valeur de ``QUERY_TAG`` : ``{"answerlineage_run_id":"<run>"}``, sans blanc."""
        return json.dumps({LABEL_KEY: self.run_id}, separators=(",", ":"))

    @property
    def sql_comment(self) -> str:
        """Le commentaire à écrire en tête de la requête : ``/* answerlineage_run_id=<run> */``,
        suivi d'une espace. Il ne contient ni ``*/`` ni ``/*`` hors de ses bornes."""
        return f"/* {LABEL_KEY}={self.run_id} */ "


def run_id(meta: object, traceparent_headers: Sequence[str] | str = ()) -> str | None:
    """L'identifiant de run d'un appel d'outil, ou ``None`` faute de contexte de trace lisible.

    ``meta`` est ``params._meta`` de la requête ``tools/call`` telle que reçue ;
    ``traceparent_headers``, en HTTP, les valeurs de tous les en-têtes
    ``traceparent`` reçus, rien en stdio. Comme le proxy, le contexte de
    ``_meta`` l'emporte ; sinon l'en-tête compte s'il est unique et bien formé.
    """
    if isinstance(meta, Mapping):
        found = _context(meta.get("traceparent"))
        if found is not None:
            return found
    headers = [traceparent_headers] if isinstance(traceparent_headers, str) else traceparent_headers
    if len(headers) == 1:
        return _context(headers[0])
    return None


def warehouse_tags(
    meta: object, traceparent_headers: Sequence[str] | str = ()
) -> WarehouseTags | None:
    """Label BigQuery, tag Snowflake et commentaire SQL d'un appel d'outil ; ``None`` sans
    contexte de trace lisible : le serveur ne pose alors rien."""
    found = run_id(meta, traceparent_headers)
    return None if found is None else WarehouseTags(found)


def _context(value: object) -> str | None:
    """L'identifiant de run d'un ``traceparent`` bien formé ; ``None`` pour tout autre valeur."""
    if not isinstance(value, str) or len(value) > MAX_TRACEPARENT_LENGTH:
        return None
    found = _TRACEPARENT.fullmatch(value)
    if found is None:
        return None
    version, trace_id, parent_id = found["version"], found["trace"], found["parent"]
    if version == "ff" or (version == "00" and found["rest"] is not None):
        return None
    if not trace_id.strip("0") or not parent_id.strip("0"):
        return None
    digest = hashlib.sha256(RUN_ID_LABEL + bytes.fromhex(trace_id) + bytes.fromhex(parent_id))
    octets = bytearray(digest.digest()[:16])
    octets[6] = (octets[6] & 0x0F) | 0x80
    octets[8] = (octets[8] & 0x3F) | 0x80
    text = octets.hex()
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"
