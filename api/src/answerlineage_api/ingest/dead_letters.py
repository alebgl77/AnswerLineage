"""File de rebut : les événements que la projection n'a pas pu intégrer au graphe.

Un worker met au rebut un événement dont la projection échoue d'une erreur qui
ne se corrigera pas en réessayant, ou après le nombre d'essais réglé pour les
autres (``answerlineage_workers.queue``). Le passage au rebut retire
l'événement de la file dans la même transaction (``bury``) : il n'est ni perdu,
ni traité deux fois.

Le rebut désigne l'événement brut, gardé dans ``event_raw``, sans le recopier.
Il garde son émetteur, l'espace de noms normalisé de son job comme partout, le
motif, un code d'une liste fermée et jamais le message d'une exception, le
nombre d'essais, la première et la dernière heure d'échec. Aucun contenu.

Deux routes :

- la liste, bornée, paginée et sous le délai des lectures, filtrable par
  émetteur et par motif, avec les décomptes par motif ;
- le rejeu, qui remet en file les événements du rebut, tous ou ceux que des
  filtres désignent, en nombre borné. Un worker les projette de nouveau : un
  événement qui échoue encore revient au rebut, avec son nouveau motif. Rejouer
  ne change pas le graphe d'un événement déjà intégré : la projection est
  idempotente.

Comme toute route de l'API, chacune exige un jeton OIDC et un rôle que la
matrice des droits lui accorde (``auth/matrix.py``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api import identifiers
from answerlineage_api.identifiers import identifier_fault, normalize_identifier
from answerlineage_api.ingest.models import FieldError, json_pointer
from answerlineage_api.ingest.routes import REFUSED_400, REFUSED_413, REFUSED_415, read_json
from answerlineage_api.lineage.paging import CursorError, decode_instant, encode_instant
from answerlineage_api.lineage.reading import TIMED_OUT, Reader, reading
from answerlineage_api.responses import RequestRefused, RequestRejected
from answerlineage_api.store.schema import DEAD_LETTER_REASONS, DeadLetterReason

# Une page de la liste.
MAX_LIMIT: Final = 500
DEFAULT_LIMIT: Final = 100
# Au-delà, les décomptes par motif sont des minimums : la lecture compte au plus
# une ligne de plus.
MAX_COUNTED: Final = 100_000
# Un rejeu remet en file au plus ce nombre d'événements ; la suite se rejoue ensuite.
MAX_REPLAY: Final = 10_000
DEFAULT_REPLAY: Final = 1_000
# Identifiants donnés à un rejeu.
MAX_REPLAY_IDS: Final = 1_000
# Mille identifiants et un émetteur des plus longs tiennent largement dans 256 Kio.
MAX_REPLAY_BYTES: Final = 256 * 1024

# L'identifiant d'un événement : l'empreinte SHA-256 de sa forme canonique.
EVENT_ID_PATTERN: Final = r"^[0-9a-f]{64}$"
# Le curseur de la liste : l'heure du dernier échec du dernier événement de la
# page, en microsecondes depuis l'an 1, puis son identifiant.
DEAD_LETTER_CURSOR_PATTERN: Final = r"^[0-9]{1,18}\.[0-9a-f]{64}$"
UNKNOWN_FIELD: Final = "unknown_field"

_REASONS_DESCRIPTION: Final = (
    "« invalid_identifier » : un identifiant obligatoire (espace de noms ou nom du job, d'un"
    " jeu de données) n'a pas la forme d'un nom. « invalid_event » : l'événement n'a pas la"
    " forme que la projection lit. « constraint_violation » : l'écriture du graphe viole une"
    " contrainte de la base. « invalid_value » : la base refuse une valeur. Ces quatre motifs"
    " mettent l'événement au rebut au premier échec. « database_error » : la base a échoué"
    " (connexion, interblocage, schéma) ; « projection_error » : toute autre erreur. Ces deux"
    " motifs ne mettent l'événement au rebut qu'après le nombre d'essais réglé sur le worker."
)


# Ce que les routes rendent : des identifiants, des codes, des heures, des décomptes.


class DeadLetter(BaseModel):
    """Un événement au rebut, désigné par son identifiant : son contenu n'est pas recopié."""

    event_id: str = Field(
        description=(
            "Identifiant de l'événement : l'empreinte de sa forme canonique, celle que rend"
            " l'ingestion."
        )
    )
    run_id: UUID = Field(description="Run que l'événement décrit.")
    emitter: str = Field(
        description="Émetteur : l'espace de noms du job de l'événement, sous sa forme normalisée."
    )
    reason: DeadLetterReason = Field(description="Motif du dernier échec. " + _REASONS_DESCRIPTION)
    attempts: int = Field(description="Essais de projection en échec depuis la mise en file.")
    received_at: AwareDatetime = Field(description="Réception de l'événement par l'API.")
    first_failed_at: AwareDatetime = Field(description="Premier essai en échec.")
    last_failed_at: AwareDatetime = Field(description="Dernier essai en échec : la mise au rebut.")


class ReasonCount(BaseModel):
    """Événements au rebut pour un motif."""

    reason: DeadLetterReason
    count: int


class DeadLetterList(BaseModel):
    """Une page du rebut, et les décomptes par motif."""

    dead_letters: list[DeadLetter] = Field(
        description="Les plus récemment mis au rebut d'abord, sous les filtres demandés."
    )
    counts: list[ReasonCount] = Field(
        description=(
            "Événements au rebut par motif, chaque motif de la liste fermée dans son ordre,"
            " zéro compris. Ils suivent le filtre d'émetteur, pas celui de motif : ils disent la"
            " répartition des motifs."
        )
    )
    truncated: bool = Field(
        description=(
            f"Vrai quand le rebut compte plus de {MAX_COUNTED} événements sous le filtre"
            " d'émetteur : les décomptes sont alors des minimums."
        )
    )
    more: bool = Field(description="Vrai quand d'autres événements suivent cette page.")
    next_cursor: str | None = Field(
        description=(
            "Curseur de la page suivante, à rendre tel quel dans le paramètre « cursor »."
            " Nul quand il n'y en a plus."
        )
    )


class DeadLetterReplay(BaseModel):
    """Les événements du rebut à remettre en file : tous, ou ceux que les filtres désignent.

    Les filtres donnés se cumulent. Sans filtre, tout le rebut est rejoué, ``limit``
    événements à la fois."""

    model_config = ConfigDict(extra="forbid")

    emitter: str | None = Field(
        default=None,
        description="Seulement les événements de cet émetteur : l'espace de noms de leur job.",
        json_schema_extra={"minLength": 1, "maxLength": identifiers.NAMESPACE_MAX},
    )
    reason: DeadLetterReason | None = Field(
        default=None, description="Seulement les événements mis au rebut pour ce motif."
    )
    event_ids: list[Annotated[str, StringConstraints(pattern=EVENT_ID_PATTERN)]] | None = Field(
        default=None,
        description="Seulement ces événements, par leur identifiant.",
        json_schema_extra={"minItems": 1, "maxItems": MAX_REPLAY_IDS},
    )
    limit: int = Field(
        default=DEFAULT_REPLAY,
        strict=True,
        ge=1,
        le=MAX_REPLAY,
        description="Nombre maximal d'événements remis en file par ce rejeu, les plus anciens"
        " d'abord.",
    )


class ReplayResult(BaseModel):
    """Bilan d'un rejeu."""

    replayed: int = Field(
        description="Événements retirés du rebut et remis en file : un worker les projettera."
    )
    more: bool = Field(
        description=(
            "Vrai quand d'autres événements du rebut répondent aux filtres : la borne est"
            " atteinte, ou un autre rejeu les tenait. Relancer le rejeu."
        )
    )


_REPLAY: TypeAdapter[DeadLetterReplay] = TypeAdapter(DeadLetterReplay)
_FIELDS: Final = frozenset(DeadLetterReplay.model_fields)


def replay_json_schema() -> dict[str, Any]:
    """Schéma JSON d'une demande de rejeu, pour la spécification OpenAPI."""
    return DeadLetterReplay.model_json_schema(ref_template="#/components/schemas/{model}")


# Passage au rebut, depuis la file.

# Retire l'événement de la file et le met au rebut, dans la transaction du lot.
# Un événement déjà au rebut sous cet identifiant y garde une ligne, la plus
# récente : un conflit ferait échouer le lot entier, qui tournerait sans fin.
_BURY = text(
    """
    WITH failed AS (
        DELETE FROM ingest_queue WHERE id = :queue_id
        RETURNING event_id, recorded_month, attempts + 1 AS attempts,
                  COALESCE(first_failed_at, now()) AS first_failed_at
    )
    INSERT INTO dead_letter (event_id, recorded_month, emitter, reason, attempts,
                             first_failed_at, last_failed_at)
    SELECT event_id, recorded_month, :emitter, :reason, attempts, first_failed_at, now()
    FROM failed
    ON CONFLICT (event_id) DO UPDATE SET
        recorded_month = EXCLUDED.recorded_month,
        emitter = EXCLUDED.emitter,
        reason = EXCLUDED.reason,
        attempts = EXCLUDED.attempts,
        first_failed_at = EXCLUDED.first_failed_at,
        last_failed_at = EXCLUDED.last_failed_at
    RETURNING attempts
    """
)


async def bury(
    conn: AsyncConnection, *, queue_id: int, emitter: str, reason: DeadLetterReason
) -> int:
    """Met au rebut l'événement de cette ligne de la file, et l'en retire ; rend ses essais.

    ``emitter`` est celui de l'événement brut. L'heure du dernier échec est celle
    de la transaction ; la première, celle du premier essai en échec, gardée par
    la file.
    """
    attempts: int = (
        await conn.execute(_BURY, {"queue_id": queue_id, "emitter": emitter, "reason": reason})
    ).scalar_one()
    return attempts


# Lecture du rebut.

_FILTERS = """(CAST(:emitter AS text) IS NULL OR d.emitter = :emitter)
          AND (CAST(:reason AS text) IS NULL OR d.reason = :reason)"""

_COUNTS = text(
    """
    SELECT reason, count(*) AS events FROM (
        SELECT d.reason FROM dead_letter d
        WHERE CAST(:emitter AS text) IS NULL OR d.emitter = :emitter
        LIMIT :limit
    ) AS bounded
    GROUP BY reason
    """
)


def _page(after: bool) -> TextClause:
    cursor = "AND (d.last_failed_at, d.event_id) < (:cursor_time, :cursor_id)" if after else ""
    return text(
        f"""
        SELECT d.event_id, e.run_id, d.emitter, d.reason, d.attempts,
               e.recorded_at AS received_at, d.first_failed_at, d.last_failed_at
        FROM dead_letter d
        JOIN event_raw e ON e.event_id = d.event_id AND e.recorded_month = d.recorded_month
        WHERE {_FILTERS} {cursor}
        ORDER BY d.last_failed_at DESC, d.event_id DESC
        LIMIT :limit
        """  # noqa: S608 — seuls des fragments constants sont interpolés.
    )


@dataclass(frozen=True)
class DeadLetterCursor:
    """Position dans le rebut : juste après cet événement."""

    last_failed_at: datetime
    event_id: str

    def encode(self) -> str:
        return f"{encode_instant(self.last_failed_at)}.{self.event_id}"

    @classmethod
    def decode(cls, value: str) -> DeadLetterCursor:
        """Lit un curseur dont la forme est déjà validée (``DEAD_LETTER_CURSOR_PATTERN``)."""
        micros, event_id = value.split(".")
        return cls(decode_instant(micros), event_id)

    def parameters(self) -> dict[str, Any]:
        return {"cursor_time": self.last_failed_at, "cursor_id": self.event_id}


async def dead_letters(
    reader: Reader,
    *,
    emitter: str | None,
    reason: str | None,
    limit: int,
    cursor: DeadLetterCursor | None,
) -> DeadLetterList:
    """Une page du rebut, les plus récemment mis au rebut d'abord, et les décomptes par motif."""
    filters = {"emitter": emitter, "reason": reason}
    params = filters | ({} if cursor is None else cursor.parameters())
    page = await reader.fetch(_page(cursor is not None), params, limit=limit)
    # Une ligne de plus que la borne est comptée : la dépasser se voit.
    counted = await reader.execute(_COUNTS, {"emitter": emitter, "limit": MAX_COUNTED + 1})
    found: dict[str, int] = {row.reason: row.events for row in counted}
    last = page.rows[-1] if page.rows else None
    return DeadLetterList(
        dead_letters=[DeadLetter.model_validate(row, from_attributes=True) for row in page.rows],
        counts=[
            ReasonCount.model_validate({"reason": code, "count": found.get(code, 0)})
            for code in DEAD_LETTER_REASONS
        ],
        truncated=sum(found.values()) > MAX_COUNTED,
        more=page.truncated,
        next_cursor=DeadLetterCursor(last.last_failed_at, last.event_id).encode()
        if page.truncated and last is not None
        else None,
    )


# Rejeu.

# Les plus anciens échecs d'abord. Un événement qu'un autre rejeu tient est
# sauté : il est déjà en train d'être remis en file.
_REPLAY_MOVE = text(
    f"""
    WITH chosen AS (
        SELECT d.event_id FROM dead_letter d
        WHERE {_FILTERS}
          AND (CAST(:event_ids AS text[]) IS NULL OR d.event_id = ANY(:event_ids))
        ORDER BY d.last_failed_at, d.event_id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    ), moved AS (
        DELETE FROM dead_letter d USING chosen c WHERE d.event_id = c.event_id
        RETURNING d.event_id, d.recorded_month, d.last_failed_at
    ), queued AS (
        INSERT INTO ingest_queue (event_id, recorded_month)
        SELECT event_id, recorded_month FROM moved ORDER BY last_failed_at, event_id
        ON CONFLICT (event_id) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) FROM moved
    """  # noqa: S608 — seuls des fragments constants sont interpolés.
)
_NO_STATEMENT_TIMEOUT = text("SET LOCAL statement_timeout TO DEFAULT")
_REPLAY_LEFT = text(
    f"""
    SELECT EXISTS (
        SELECT 1 FROM dead_letter d
        WHERE {_FILTERS}
          AND (CAST(:event_ids AS text[]) IS NULL OR d.event_id = ANY(:event_ids))
    )
    """  # noqa: S608 — seuls des fragments constants sont interpolés.
)


async def replay(reader: Reader, request: DeadLetterReplay) -> ReplayResult:
    """Retire du rebut et remet en file les événements demandés, dans la transaction du lecteur.

    Chaque événement remis en file repart sans essai : un nouvel échec ouvre un
    nouvel épisode, compté depuis le rejeu.
    """
    params = {
        "emitter": request.emitter,
        "reason": request.reason,
        "event_ids": request.event_ids,
        "limit": request.limit,
    }
    replayed: int = (await reader.execute(_REPLAY_MOVE, params)).scalar_one()
    more: bool = (await reader.execute(_REPLAY_LEFT, params)).scalar_one()
    return ReplayResult(replayed=replayed, more=more)


# Validation d'une demande de rejeu.


def _error(path: str, kind: str, message: str) -> FieldError:
    return FieldError(path=path, type=kind, message=message)


def _schema_errors(error: ValidationError) -> list[FieldError]:
    found: dict[tuple[str, str], FieldError] = {}
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        path = json_pointer(item["loc"])
        found[path, item["type"]] = _error(path, item["type"], item["msg"])
    return list(found.values())


def validate_replay(raw: object) -> tuple[DeadLetterReplay | None, list[FieldError]]:
    """Valide une demande de rejeu. Rend la demande, émetteur sous sa forme normalisée, ou
    toutes ses fautes, chacune par son chemin, un code et un motif fixe.

    Un champ inconnu est refusé, sans être nommé : un filtre mal orthographié
    ne doit pas rejouer tout le rebut, et son nom vient de l'appelant.
    """
    if not isinstance(raw, dict):
        return None, [_error("", "not_an_object", "Une demande de rejeu est un objet JSON.")]
    if set(raw) - _FIELDS:
        return None, [
            _error(
                "",
                UNKNOWN_FIELD,
                "Une demande de rejeu ne porte que emitter, reason, event_ids et limit.",
            )
        ]
    event_ids = raw.get("event_ids")
    if isinstance(event_ids, list) and not 1 <= len(event_ids) <= MAX_REPLAY_IDS:
        # Une liste trop longue n'est pas parcourue ; le motif cite la borne, pas le nombre reçu.
        return None, [
            _error(
                "/event_ids",
                "too_many_items" if event_ids else "too_few_items",
                f"Une demande de rejeu nomme de 1 à {MAX_REPLAY_IDS} événements.",
            )
        ]
    request: DeadLetterReplay | None = None
    errors: list[FieldError] = []
    try:
        request = _REPLAY.validate_python(raw)
    except ValidationError as error:
        errors = _schema_errors(error)
    emitter = raw.get("emitter")
    if isinstance(emitter, str):
        fault = identifier_fault(emitter, max_length=identifiers.NAMESPACE_MAX)
        if fault is not None:
            errors.append(_error("/emitter", fault.code, fault.message))
    if errors or request is None:
        return None, errors
    if request.emitter is None:
        return request, []
    return request.model_copy(update={"emitter": normalize_identifier(request.emitter)}), []


# Routes.

router = APIRouter(prefix="/api/v1", tags=["ingestion"])

_INVALID: dict[int | str, dict[str, Any]] = {
    422: {"model": RequestRejected, "description": "Un paramètre est invalide."}
}


def _rejected(path: str, kind: str, message: str) -> JSONResponse:
    """Un paramètre bien formé mais inutilisable : son chemin et le motif, jamais sa valeur."""
    rejected = RequestRejected(errors=[_error(path, kind, message)])
    return JSONResponse(status_code=422, content=rejected.model_dump())


Emitter = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=identifiers.NAMESPACE_MAX,
        description=(
            "Seulement les événements de cet émetteur : l'espace de noms de leur job, par exemple"
            " « mcp://entrepot »."
        ),
    ),
]
Reason = Annotated[
    DeadLetterReason | None,
    Query(description="Seulement les événements mis au rebut pour ce motif."),
]
Limit = Annotated[
    int, Query(ge=1, le=MAX_LIMIT, description="Nombre maximal d'événements renvoyés.")
]
CursorParam = Annotated[
    str | None,
    Query(
        alias="cursor",
        pattern=DEAD_LETTER_CURSOR_PATTERN,
        description="Curseur opaque de la page suivante, tel que l'API le donne dans"
        " « next_cursor ».",
    ),
]


def _emitter(value: str | None) -> str | JSONResponse | None:
    if value is None:
        return None
    fault = identifier_fault(value, max_length=identifiers.NAMESPACE_MAX)
    if fault is not None:
        return _rejected("/query/emitter", fault.code, fault.message)
    return normalize_identifier(value)


@router.get(
    "/ingestion/dead-letters",
    response_model=DeadLetterList,
    summary="Liste les événements au rebut, que la projection n'a pas pu intégrer au graphe",
    responses={**_INVALID, **TIMED_OUT},
)
async def list_dead_letters(
    request: Request,
    emitter: Emitter = None,
    reason: Reason = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor_value: CursorParam = None,
) -> DeadLetterList | JSONResponse:
    """Les plus récemment mis au rebut d'abord, chacun avec son émetteur, son motif, ses essais
    et ses heures d'échec ; et les décomptes par motif. Aucun contenu d'événement.

    Comme toute lecture, elle s'exécute sous le délai maximal des lectures."""
    chosen = _emitter(emitter)
    if isinstance(chosen, JSONResponse):
        return chosen
    cursor = None
    if cursor_value is not None:
        try:
            cursor = DeadLetterCursor.decode(cursor_value)
        except CursorError:
            return _rejected(
                "/query/cursor", "cursor_invalid", "Ce curseur ne désigne aucune page."
            )
    async with reading(request) as reader:
        return await dead_letters(reader, emitter=chosen, reason=reason, limit=limit, cursor=cursor)


@router.post(
    "/ingestion/dead-letters/replay",
    status_code=202,
    response_model=ReplayResult,
    summary="Remet en file les événements du rebut, tous ou filtrés, en nombre borné",
    responses={
        400: {"model": RequestRefused, "description": REFUSED_400},
        413: {"model": RequestRefused, "description": REFUSED_413},
        415: {"model": RequestRefused, "description": REFUSED_415},
        422: {"model": RequestRejected, "description": "La demande de rejeu est invalide."},
        **TIMED_OUT,
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/DeadLetterReplay"}}
            },
        }
    },
)
async def replay_dead_letters(request: Request) -> JSONResponse:
    """Les événements retirés du rebut sont remis en file dans la même transaction ; un worker
    les projette de nouveau. Un événement qui échoue encore revient au rebut, avec son nouveau
    motif. Le corps est lu borné, compressé ou non (``gzip``).

    Le rejeu s'exécute sous le délai maximal des lectures : au-delà, rien n'est remis en file."""
    body, refusal = await read_json(request, max_bytes=MAX_REPLAY_BYTES)
    if refusal is not None:
        return refusal
    asked, errors = validate_replay(body)
    if asked is None:
        return JSONResponse(status_code=422, content=RequestRejected(errors=errors).model_dump())
    deadline = time.monotonic() + request.app.state.read_timeout_ms / 1000
    engine: AsyncEngine = request.app.state.engine
    async with engine.begin() as conn:
        result = await replay(Reader(conn, deadline=deadline), asked)
        # Le délai posé pour le rejeu ne vaut pas pour sa validation.
        await conn.execute(_NO_STATEMENT_TIMEOUT)
    return JSONResponse(status_code=202, content=result.model_dump())
