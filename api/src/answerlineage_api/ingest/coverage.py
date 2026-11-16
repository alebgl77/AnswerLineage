"""Relevés de couverture : ce qu'un émetteur a vu pendant une fenêtre de temps.

Un émetteur (le proxy placé devant un serveur MCP, un adaptateur d'agents)
publie, fenêtre par fenêtre, le nombre d'appels qu'il a vus, d'événements
qu'il a émis et perdus, et de requêtes qu'il a relayées sans pouvoir les
inspecter. Comparés au graphe, ces compteurs disent la part du trafic
réellement captée (``lineage/coverage.py``).

Le relevé est une entrée hostile comme une autre : identifiant de l'émetteur
contrôlé, fenêtre et compteurs bornés et cohérents, aucune valeur recopiée dans
une erreur. Deux relevés de la même fenêtre se fusionnent compteur par
compteur, au plus grand : rejouer un relevé ne change rien, et l'ordre
d'arrivée de deux relevés ne change pas l'état obtenu.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import BigInteger, DateTime, and_, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api import identifiers
from answerlineage_api.identifiers import identifier_fault, normalize_identifier
from answerlineage_api.ingest.models import (
    EVENT_TIME_AHEAD,
    EVENT_TIME_EARLIEST,
    EventTime,
    FieldError,
    json_pointer,
)
from answerlineage_api.ingest.routes import (
    REFUSED_400,
    REFUSED_413,
    REFUSED_415,
    read_json,
)
from answerlineage_api.responses import RequestRefused, RequestRejected
from answerlineage_api.store import schema

# Une fenêtre dure au plus un jour : un relevé dit ce qu'un émetteur a vu
# récemment, et un trou se situe à la fenêtre près.
MAX_WINDOW: Final = timedelta(days=1)
# Un compteur par fenêtre : un milliard d'appels en un jour, plus de dix mille
# par seconde, dépasse tout émetteur réel.
MAX_COUNT: Final = 1_000_000_000
COUNTERS: Final = ("calls_seen", "events_emitted", "events_lost", "uninspected")

Count = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNT)]


class ReportWindow(BaseModel):
    """Fenêtre d'un relevé : début inclus, fin exclue."""

    model_config = ConfigDict(extra="ignore")

    start: EventTime = Field(alias="from", description="Début, inclus (RFC 3339, avec fuseau).")
    end: EventTime = Field(alias="to", description="Fin, exclue (RFC 3339, avec fuseau).")


class CoverageReport(BaseModel):
    """Ce qu'un émetteur a vu pendant une fenêtre."""

    model_config = ConfigDict(extra="ignore")

    emitter: str = Field(
        description=(
            "Identifiant de l'émetteur : l'espace de noms de ses jobs, « mcp://entrepot »"
            " pour le proxy placé devant ce serveur."
        ),
        json_schema_extra={"minLength": 1, "maxLength": identifiers.NAMESPACE_MAX},
    )
    window: ReportWindow
    calls_seen: Count = Field(description="Appels d'outils vus pendant la fenêtre.")
    events_emitted: Count = Field(description="Événements remis à leur destination.")
    events_lost: Count = Field(description="Événements que l'émetteur n'a pas pu remettre.")
    uninspected: Count = Field(
        default=0,
        description=(
            "Requêtes relayées sans pouvoir être inspectées : elles cachent peut-être des appels."
        ),
    )


_REPORT: TypeAdapter[CoverageReport] = TypeAdapter(CoverageReport)
_WINDOW: TypeAdapter[ReportWindow] = TypeAdapter(ReportWindow)


def _error(path: str, kind: str, message: str) -> FieldError:
    return FieldError(path=path, type=kind, message=message)


def _window_errors(window: ReportWindow, received_at: datetime) -> list[FieldError]:
    if window.end <= window.start:
        return [_error("/window", "window_inverted", "Le début de la fenêtre précède sa fin.")]
    errors = []
    if window.end - window.start > MAX_WINDOW:
        errors.append(_error("/window", "window_too_long", "Une fenêtre dure au plus un jour."))
    if window.start < EVENT_TIME_EARLIEST:
        errors.append(
            _error(
                "/window/from",
                "window_out_of_range",
                "Une fenêtre commence après le 1er janvier 1970.",
            )
        )
    if window.end > received_at + EVENT_TIME_AHEAD:
        errors.append(
            _error(
                "/window/to",
                "window_in_future",
                "Une fenêtre s'achève au plus un jour après la réception du relevé.",
            )
        )
    return errors


def validate_report(
    raw: object, *, received_at: datetime | None = None
) -> tuple[CoverageReport | None, list[FieldError]]:
    """Valide un relevé. Renvoie le relevé, l'émetteur sous sa forme normalisée, ou ses fautes.

    La fin de la fenêtre est bornée par ``received_at``, l'heure de réception ;
    à défaut, par l'heure courante. Toutes les fautes sont rapportées ensemble,
    chacune par son chemin, un code et un motif fixe, jamais la valeur reçue.
    """
    received_at = received_at or datetime.now(UTC)
    if not isinstance(raw, dict):
        return None, [_error("", "not_an_object", "Un relevé est un objet JSON.")]
    report: CoverageReport | None = None
    errors: list[FieldError] = []
    try:
        report = _REPORT.validate_python(raw)
    except ValidationError as error:
        # Aucune clé reçue n'atteint un chemin : les champs inconnus sont ignorés.
        errors = [
            _error(json_pointer(item["loc"]), item["type"], item["msg"])
            for item in error.errors(include_url=False, include_input=False, include_context=False)
        ]

    emitter = raw.get("emitter")
    if isinstance(emitter, str):
        fault = identifier_fault(emitter, max_length=identifiers.NAMESPACE_MAX)
        if fault is not None:
            errors.append(_error("/emitter", fault.code, fault.message))

    window = report.window if report is not None else _valid_window(raw.get("window"))
    if window is not None:
        errors.extend(_window_errors(window, received_at))

    if report is not None and report.events_emitted + report.events_lost > report.calls_seen:
        errors.append(
            _error(
                "/calls_seen",
                "counts_inconsistent",
                "Les événements émis et perdus ne dépassent pas les appels vus.",
            )
        )
    if errors or report is None:
        return None, errors
    return report.model_copy(update={"emitter": normalize_identifier(report.emitter)}), []


def _valid_window(raw: object) -> ReportWindow | None:
    try:
        return _WINDOW.validate_python(raw)
    except ValidationError:
        return None


async def store_report(conn: AsyncConnection, report: CoverageReport) -> CoverageReport:
    """Range le relevé, fusionné au plus grand avec celui de la même fenêtre ; rend l'état obtenu.

    Un relevé qui fait grandir un compteur, ou qui ouvre une fenêtre, fait de
    son émetteur un émetteur connu et date son dernier relevé : c'est ce que
    le contrôle des émetteurs muets surveille. Un rejeu ne change rien.

    La table est partitionnée par mois de première réception : la fenêtre est
    d'abord cherchée dans toutes les partitions et fusionnée sur place. Une
    fenêtre nouvelle est insérée dans le mois courant, et deux premières
    réceptions simultanées s'y fusionnent par la clé primaire.
    """
    table = schema.coverage_report
    window = and_(
        table.c.emitter == report.emitter,
        table.c.window_start == report.window.start,
        table.c.window_end == report.window.end,
    )
    counters = {counter: getattr(report, counter) for counter in COUNTERS}
    grown = or_(*(table.c[counter] < value for counter, value in counters.items()))
    changed = (
        await conn.execute(
            table.update()
            .where(window, grown)
            .values(
                {
                    counter: func.greatest(table.c[counter], value)
                    for counter, value in counters.items()
                }
            )
            .returning(table.c.emitter)
        )
    ).first()
    if changed is None:
        statement = insert(table).from_select(
            ["emitter", "window_start", "window_end", *COUNTERS],
            select(
                literal(report.emitter),
                literal(report.window.start, DateTime(timezone=True)),
                literal(report.window.end, DateTime(timezone=True)),
                *(literal(value, BigInteger) for value in counters.values()),
            ).where(~exists().where(window)),
        )
        excluded = statement.excluded
        changed = (
            await conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        table.c.emitter,
                        table.c.window_start,
                        table.c.window_end,
                        table.c.recorded_month,
                    ],
                    set_={
                        counter: func.greatest(table.c[counter], excluded[counter])
                        for counter in COUNTERS
                    },
                    where=or_(*(excluded[counter] > table.c[counter] for counter in COUNTERS)),
                ).returning(table.c.emitter)
            )
        ).first()
    if changed is not None:
        emitters = schema.coverage_emitter
        known = insert(emitters).values(emitter=report.emitter, last_report_at=func.now())
        await conn.execute(
            known.on_conflict_do_update(
                index_elements=[emitters.c.emitter],
                set_={
                    "last_report_at": func.greatest(
                        emitters.c.last_report_at, known.excluded.last_report_at
                    )
                },
            )
        )
    merged = (
        await conn.execute(
            select(*(func.max(table.c[counter]).label(counter) for counter in COUNTERS)).where(
                window
            )
        )
    ).one()
    return report.model_copy(update=dict(merged._mapping))


router = APIRouter(prefix="/api/v1", tags=["ingestion"])


def _report_schema() -> dict[str, Any]:
    return {"$ref": "#/components/schemas/CoverageReport"}


@router.post(
    "/coverage",
    status_code=201,
    response_model=CoverageReport,
    summary="Publie le relevé de couverture d'un émetteur pour une fenêtre",
    responses={
        400: {"model": RequestRefused, "description": REFUSED_400},
        413: {"model": RequestRefused, "description": REFUSED_413},
        415: {"model": RequestRefused, "description": REFUSED_415},
        422: {"model": RequestRejected, "description": "Le relevé est invalide."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _report_schema()}},
        }
    },
)
async def publish_report(request: Request) -> JSONResponse:
    """Rend l'état de la fenêtre après fusion : un relevé rejoué ne change rien.

    Le corps est lu comme celui des événements : borné, et compressé ou non (``gzip``)."""
    body, refusal = await read_json(request)
    if refusal is not None:
        return refusal
    report, errors = validate_report(body, received_at=datetime.now(UTC))
    if report is None:
        return JSONResponse(status_code=422, content=RequestRejected(errors=errors).model_dump())
    engine: AsyncEngine = request.app.state.engine
    async with engine.begin() as conn:
        stored = await store_report(conn, report)
    return JSONResponse(status_code=201, content=stored.model_dump(mode="json", by_alias=True))
