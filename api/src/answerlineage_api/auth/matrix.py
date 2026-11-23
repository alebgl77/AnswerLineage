"""La matrice des droits : pour chaque route, les rôles qui l'ouvrent.

C'est la seule source. La dépendance d'accès (``access.py``) l'applique avant
toute route ; l'OpenAPI la publie, opération par opération (``publish``) ; la
documentation la reprend telle que ``render_markdown`` la rend, et un test le
vérifie. Une route absente de la matrice empêche l'application de se
construire (``undeclared``) : rien ne s'ouvre par oubli.

Deux routes restent ouvertes sans jeton, et ne disent rien du lignage : la
sonde de vie, qui ne lit pas la base, et ce que l'explorateur doit savoir pour
se connecter (l'émetteur et son client public). La spécification OpenAPI et
les fichiers de l'explorateur sont servis hors des routes de l'API : le contrat
est publié dans le dépôt, et la page ne porte aucune donnée.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from fastapi.routing import APIRoute, iter_route_contexts
from starlette.routing import BaseRoute

from answerlineage_api.auth.roles import LABELS, PERSON_ROLES, Role

# Le schéma de sécurité de l'OpenAPI : un jeton porteur JWT.
SCHEME: Final = "bearer"


@dataclass(frozen=True)
class Access:
    """Les rôles qui ouvrent une route ; aucun rôle pour une route ouverte sans jeton."""

    roles: frozenset[Role]
    # Pourquoi la route s'ouvre à ces rôles, en une phrase.
    why: str

    @property
    def public(self) -> bool:
        return not self.roles


def _only(*roles: Role, why: str) -> Access:
    return Access(frozenset(roles), why)


_EVERY_PERSON = PERSON_ROLES
_STEWARDS = (Role.DATA_STEWARD, Role.INCIDENT_MANAGER, Role.ADMINISTRATOR)
_OPERATORS = (Role.AUDITOR, Role.ADMINISTRATOR)
# Un incident proposé par un test de qualité attend un humain qui répond des données ou des
# incidents, comme une déclaration : lui seul le confirme ou l'écarte ; toute personne qui a
# un rôle le voit attendre, comme les incidents.
_PROPOSAL_DECIDERS = _STEWARDS

# Révéler un destinataire sort une personne de son pseudonyme : une action tracée, qui
# revient à qui instruit les incidents et prévient leurs destinataires. Pas à
# l'administrateur : qui exploite la plateforme, et peut en approcher la base et le coffre
# à clés, ne révèle pas aussi ; il lit le journal, avec l'auditeur.
_REVEALERS = (Role.INCIDENT_MANAGER,)

_LINEAGE = "Lire le lignage : toute personne qui a un rôle."
_INCIDENT_READ = "Lire les incidents et leurs réponses touchées : toute personne qui a un rôle."
_PROPOSAL_READ = "Lire les incidents proposés : toute personne qui a un rôle."

ACCESS: Final[Mapping[tuple[str, str], Access]] = {
    # Ingestion : les émetteurs seuls écrivent ce que le graphe tiendra.
    ("POST", "/api/v1/lineage"): _only(
        Role.EMITTER, why="Envoyer des événements : les émetteurs seuls."
    ),
    ("POST", "/api/v1/lineage/batch"): _only(
        Role.EMITTER, why="Envoyer des événements : les émetteurs seuls."
    ),
    ("POST", "/api/v1/coverage"): _only(
        Role.EMITTER, why="Publier un relevé de couverture : les émetteurs seuls."
    ),
    # Exploitation de l'ingestion.
    ("GET", "/api/v1/ingestion/duplicates"): _only(
        Role.AGENT_OWNER,
        *_OPERATORS,
        why="Les doublons par émetteur : qui répond des agents et de l'exploitation.",
    ),
    ("GET", "/api/v1/ingestion/undescribed-tools"): _only(
        Role.AGENT_OWNER,
        *_OPERATORS,
        why="Les outils sans règle de lecture : qui répond des agents et de l'exploitation.",
    ),
    ("GET", "/api/v1/ingestion/dead-letters"): _only(*_OPERATORS, why="Le rebut : l'exploitation."),
    ("POST", "/api/v1/ingestion/dead-letters/replay"): _only(
        Role.ADMINISTRATOR, why="Rejouer le rebut : l'administrateur seul."
    ),
    # Lecture du lignage.
    ("GET", "/api/v1/coverage"): Access(_EVERY_PERSON, _LINEAGE),
    ("GET", "/api/v1/answers"): Access(_EVERY_PERSON, _LINEAGE),
    ("GET", "/api/v1/answers/{answer_id}/sources"): Access(_EVERY_PERSON, _LINEAGE),
    ("GET", "/api/v1/graph"): Access(_EVERY_PERSON, _LINEAGE),
    ("GET", "/api/v1/datasets"): Access(_EVERY_PERSON, _LINEAGE),
    ("GET", "/api/v1/impact"): Access(_EVERY_PERSON, _LINEAGE),
    ("GET", "/api/v1/exposure"): Access(_EVERY_PERSON, _LINEAGE),
    # Incidents et quarantaines.
    ("POST", "/api/v1/incidents"): _only(
        *_STEWARDS, why="Déclarer un incident : qui répond des données ou des incidents."
    ),
    ("GET", "/api/v1/incidents"): Access(_EVERY_PERSON, _INCIDENT_READ),
    ("GET", "/api/v1/incidents/{incident_id}"): Access(_EVERY_PERSON, _INCIDENT_READ),
    ("POST", "/api/v1/incidents/{incident_id}/quarantine"): _only(
        *_STEWARDS, why="Mettre en quarantaine : qui répond des données ou des incidents."
    ),
    ("POST", "/api/v1/incidents/{incident_id}/quarantine/lift"): _only(
        *_STEWARDS, why="Lever une quarantaine : qui répond des données ou des incidents."
    ),
    ("GET", "/api/v1/incident-proposals"): Access(_EVERY_PERSON, _PROPOSAL_READ),
    ("GET", "/api/v1/incident-proposals/{proposal_id}"): Access(_EVERY_PERSON, _PROPOSAL_READ),
    ("POST", "/api/v1/incident-proposals/{proposal_id}/confirm"): _only(
        *_PROPOSAL_DECIDERS,
        why="Confirmer un incident proposé : qui répond des données ou des incidents.",
    ),
    ("POST", "/api/v1/incident-proposals/{proposal_id}/dismiss"): _only(
        *_PROPOSAL_DECIDERS,
        why="Écarter un incident proposé : qui répond des données ou des incidents.",
    ),
    # Destinataires pseudonymisés.
    ("POST", "/api/v1/recipients/reveal"): _only(
        *_REVEALERS,
        why=(
            "Révéler un destinataire, action tracée : qui instruit les incidents, pas"
            " l'administrateur qui exploite la plateforme."
        ),
    ),
    ("GET", "/api/v1/recipients/revelations"): _only(
        *_OPERATORS, why="Le journal des révélations : l'audit et l'exploitation."
    ),
    ("GET", "/api/v1/quarantine"): _only(
        *_EVERY_PERSON,
        Role.EMITTER,
        why="Les périmètres en quarantaine : les proxys, et toute personne qui a un rôle.",
    ),
    # Installation.
    ("GET", "/api/v1/health"): _only(
        *_OPERATORS, why="L'état détaillé, ses files et ses compteurs : l'exploitation."
    ),
    ("GET", "/api/v1/live"): Access(
        frozenset(), "La sonde de vie : ouverte, elle ne lit pas la base et ne dit rien."
    ),
    ("GET", "/api/v1/authentication"): Access(
        frozenset(),
        "Ce que l'explorateur doit savoir pour se connecter : ouvert, rien du lignage.",
    ),
}


def access_of(method: str, path: str) -> Access | None:
    """Les droits d'une route ; ``None`` pour une route que la matrice ne déclare pas.

    ``HEAD`` suit ``GET`` : il en rend les en-têtes."""
    return ACCESS.get(("GET" if method == "HEAD" else method, path))


def operations(routes: Iterable[BaseRoute]) -> set[tuple[str, str]]:
    """Les opérations des routes de l'API : méthode et chemin, tel que la route le déclare."""
    return {
        (method, context.path)
        for context in iter_route_contexts(list(routes))
        if isinstance(context.original_route, APIRoute) and context.path is not None
        for method in context.methods or ()
        if method != "HEAD"
    }


def undeclared(routes: Iterable[BaseRoute]) -> list[str]:
    """Les opérations que la matrice ne déclare pas, et les entrées de la matrice sans route."""
    present = operations(routes)
    return [
        *(
            f"{method} {path} n'est pas dans la matrice"
            for method, path in sorted(present - ACCESS.keys())
        ),
        *(f"{method} {path} n'a pas de route" for method, path in sorted(ACCESS.keys() - present)),
    ]


def security(access: Access) -> list[dict[str, list[str]]]:
    """Les exigences de sécurité d'une opération, en OpenAPI 3.1 : un rôle suffit.

    Chaque objet est une alternative ; pour un schéma ``http``, la liste nomme les rôles
    exigés. Une route ouverte n'exige rien (liste vide)."""
    return [{SCHEME: [role.value]} for role in sorted(access.roles)]


_REFUSED = {"$ref": "#/components/schemas/RequestRefused"}
_UNAUTHENTICATED = (
    "Le jeton manque (« missing_token »), est invalide (« invalid_token ») ou a expiré"
    " (« expired_token »)."
)
_FORBIDDEN = "Le jeton n'a aucun des rôles qui ouvrent la route (« insufficient_role »)."
_UNAVAILABLE = (
    "L'émetteur OIDC ne donne pas la clé qui vérifierait le jeton (« authentication_unavailable »)."
)


def _refusal(description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": _REFUSED}}}


def publish(document: dict[str, Any]) -> None:
    """Porte la matrice dans la spécification : le schéma de sécurité, les rôles de chaque
    opération et ses refus ``401``, ``403`` et ``503``."""
    components = document.setdefault("components", {})
    components.setdefault("securitySchemes", {})[SCHEME] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "Jeton d'accès JWT de l'émetteur OIDC configuré, pour l'audience de l'API. Chaque"
            " opération nomme les rôles qui l'ouvrent : un seul suffit."
        ),
    }
    for path, item in document.get("paths", {}).items():
        for method, operation in item.items():
            access = ACCESS[(method.upper(), path)]
            operation["security"] = security(access)
            if access.public:
                continue
            responses = operation.setdefault("responses", {})
            responses["401"] = _refusal(_UNAUTHENTICATED)
            forbidden = responses.get("403")
            if forbidden is None:
                responses["403"] = _refusal(_FORBIDDEN)
            else:
                forbidden["description"] = f"{_FORBIDDEN} {forbidden['description']}"
            unavailable = responses.get("503")
            if unavailable is None:
                responses["503"] = _refusal(_UNAVAILABLE)
            else:
                unavailable["description"] = f"{unavailable['description']} {_UNAVAILABLE}"


def render_markdown() -> str:
    """La matrice telle que la documentation la publie : une ligne par opération."""
    order = list(Role)
    header = "| Route | " + " | ".join(LABELS[role] for role in order) + " | Pourquoi |"
    rule = "| --- |" + " :-: |" * len(order) + " --- |"
    lines = [header, rule]
    for (method, path), access in ACCESS.items():
        if access.public:
            cells = ["ouvert"] * len(order)
        else:
            cells = ["oui" if role in access.roles else "" for role in order]
        lines.append(f"| `{method} {path}` | " + " | ".join(cells) + f" | {access.why} |")
    return "\n".join(lines) + "\n"
