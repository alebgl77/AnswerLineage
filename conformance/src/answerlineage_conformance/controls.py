"""Catalogue des contrôles du kit.

Chaque contrôle a un code stable, un groupe et un motif. Le motif est un texte
fixe : il ne cite jamais une valeur reçue. Un contrôle ne vaut que sous les
profils où l'émetteur le doit : un point de capture ne doit pas de parent, un
adaptateur ne doit pas de contexte de trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Group = Literal["shape", "context", "content", "projection"]
Profile = Literal["adapter", "capture"]

GROUPS: Final[tuple[Group, ...]] = ("shape", "context", "content", "projection")
PROFILES: Final[tuple[Profile, ...]] = ("adapter", "capture")

_BOTH: Final[tuple[Profile, ...]] = PROFILES
_ADAPTER: Final[tuple[Profile, ...]] = ("adapter",)
_CAPTURE: Final[tuple[Profile, ...]] = ("capture",)


@dataclass(frozen=True)
class Control:
    code: str
    group: Group
    message: str
    profiles: tuple[Profile, ...] = _BOTH


CONTROLS: Final[tuple[Control, ...]] = (
    # Forme.
    Control(
        "ingestion_rejected",
        "shape",
        "L'ingestion rejetterait cet événement ; le motif est le code qu'elle rendrait.",
    ),
    Control(
        "facet_schema_violation",
        "shape",
        "La facette ne respecte pas son schéma publié ; le motif est la règle violée.",
    ),
    Control(
        "facet_schema_unknown",
        "shape",
        "Le _schemaURL de la facette n'est l'adresse d'aucune version publiée de son schéma : "
        "elle est jugée contre la version la plus récente.",
    ),
    Control(
        "facet_unknown",
        "shape",
        "Facette au préfixe d'AnswerLineage, mais inconnue : le graphe ne la lirait pas, "
        "et le run serait celui d'un job ordinaire.",
    ),
    Control(
        "facet_misplaced",
        "shape",
        "Facette connue, posée ailleurs qu'à la place que son schéma lui donne.",
    ),
    # Contexte.
    Control(
        "parent_missing",
        "context",
        "Appel d'outil sans facette parent : rien ne le rattache au run de l'agent.",
        _ADAPTER,
    ),
    Control(
        "parent_run_id_missing",
        "context",
        "Facette parent sans run.runId : elle ne désigne aucun run.",
        _ADAPTER,
    ),
    Control(
        "parent_run_id_not_canonical",
        "context",
        "Le run.runId de la facette parent n'est pas un UUID sous sa forme canonique.",
        _ADAPTER,
    ),
    Control(
        "parent_not_in_capture",
        "context",
        "Le run que désigne la facette parent n'a aucun événement dans la capture.",
        _ADAPTER,
    ),
    Control(
        "not_a_tool_call",
        "context",
        "Un point de capture n'émet que des appels d'outil : la facette "
        "answerlineage_toolCall manque.",
        _CAPTURE,
    ),
    Control(
        "trace_context_missing",
        "context",
        "Appel d'outil sans contexte de trace : traceId et spanId sont attendus ensemble "
        "sur un événement au moins.",
        _CAPTURE,
    ),
    Control(
        "run_id_not_derived",
        "context",
        "Le run.runId n'est pas celui que la dérivation publiée donne pour le contexte "
        "de trace cité.",
    ),
    Control(
        "run_without_terminal_event",
        "context",
        "Run sans événement terminal (COMPLETE, FAIL ou ABORT).",
    ),
    Control(
        "run_with_several_terminal_events",
        "context",
        "Run avec plusieurs événements terminaux distincts.",
    ),
    Control(
        "no_complete_chain",
        "context",
        "Aucune chaîne complète dans la capture : un jeu de données lu par un appel d'outil, "
        "rattaché à un run d'agent qui diffuse une réponse. La capture ne prouverait rien.",
        _ADAPTER,
    ),
    Control(
        "no_tool_call",
        "context",
        "Aucun appel d'outil dans la capture : elle ne prouverait rien.",
        _CAPTURE,
    ),
    # Contenu.
    Control(
        "canary_missing_from_witness",
        "content",
        "Canari absent du témoin : rien ne prouve qu'il a traversé l'émetteur.",
    ),
    Control("canary_in_events", "content", "Canari retrouvé dans les événements émis."),
    Control("canary_in_logs", "content", "Canari retrouvé dans un journal."),
    Control("canary_in_metrics", "content", "Canari retrouvé dans les métriques."),
    # Projection.
    Control(
        "projection_failed",
        "projection",
        "La projection de l'événement échoue : il resterait dans la file de traitement ; "
        "le motif est la classe de l'erreur.",
    ),
    Control(
        "parent_dropped",
        "projection",
        "Facette parent déclarée, que le graphe ignorerait.",
    ),
    Control(
        "delivery_dropped",
        "projection",
        "Diffusion déclarée, que le graphe ignorerait en tout ou en partie : réponse comptée "
        "comme une écriture, réponse sans diffusion, ou destinataires perdus.",
    ),
    Control(
        "columns_dropped",
        "projection",
        "Colonnes déclarées, que le graphe ignorerait en tout ou en partie.",
    ),
)

BY_CODE: Final[dict[str, Control]] = {control.code: control for control in CONTROLS}
