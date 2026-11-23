"""Un destinataire de bout en bout : seul son pseudonyme entre dans la base, et rien ne l'y relie
à une personne.

Le parcours est complet : un émetteur pseudonymise ses destinataires avec la clé du
coffre, l'ingestion reçoit la diffusion (et refuse une identité en clair), un worker la
projette dans le graphe, un incident est déclaré et analysé, un gestionnaire d'incident
révèle un destinataire. Puis la base entière est parcourue, table par table du schéma
déclaré : aucune identité, sous aucune forme, ni la clé ; seul le pseudonyme, et seulement
là où il doit être.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import CheckConstraint, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.auth.roles import Role
from answerlineage_api.pseudonyms.vault import LocalVault, PseudonymKey, pseudonym_of
from answerlineage_api.store import schema
from answerlineage_workers.incidents import process_analysis
from answerlineage_workers.queue import process_batch
from api.tests.issuer import TEST_ISSUER, authentication

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PRODUCER = "https://exemple.org/emetteur-essai"
AGENT_FACET = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/AgentRunFacet.json#/$defs/AgentRunFacet"
TOOL_FACET = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
DELIVERY_FACET = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/DeliveryOutputDatasetFacet.json#/$defs/DeliveryOutputDatasetFacet"
PARENT_FACET = "https://openlineage.io/spec/facets/1-0-1/ParentRunFacet.json#/$defs/ParentRunFacet"
SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
AGENT_RUN = "019a4d1e-7a10-7c3e-9d5e-2f1b6c8a0101"
TOOL_RUN = "019a4d1e-7a10-7c3e-9d5e-2f1b6c8a0102"
AGENT = {"namespace": "agent://finance", "name": "analyste-tresorerie"}
RATES = {"namespace": "bigquery", "name": "finance.ref.taux_change"}
T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

# Les tables qui tiennent un pseudonyme, et pourquoi : l'événement reçu, le destinataire du
# graphe, celui d'une réponse touchée, et le journal des révélations. Aucune autre.
HOLDING_PSEUDONYMS = {"event_raw", "recipient", "incident_recipient", "recipient_revelation"}


def facet(url: str, **fields: Any) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": url, **fields}


def event(at: datetime, **fields: Any) -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": at.isoformat().replace("+00:00", "Z"),
        "producer": PRODUCER,
        "schemaURL": SCHEMA_URL,
        "inputs": [],
        "outputs": [],
        **fields,
    }


def request_events(recipients: list[str]) -> list[dict[str, Any]]:
    """Une demande d'agent : un appel d'outil qui lit les taux, puis la réponse diffusée."""
    agent_run = {"runId": AGENT_RUN, "facets": {"answerlineage_agent": facet(AGENT_FACET)}}
    tool_call = event(
        T0 + timedelta(seconds=5),
        run={
            "runId": TOOL_RUN,
            "facets": {
                "parent": facet(PARENT_FACET, run={"runId": AGENT_RUN}, job=AGENT),
                "answerlineage_toolCall": facet(TOOL_FACET, server="entrepot", tool="execute_sql"),
            },
        },
        job={"namespace": "mcp://entrepot", "name": "execute_sql"},
        inputs=[RATES],
    )
    answer = event(
        T0 + timedelta(seconds=30),
        run=agent_run,
        job=AGENT,
        outputs=[
            {
                "namespace": "agent://finance",
                "name": "analyste-tresorerie/reponses",
                "outputFacets": {
                    "answerlineage_delivery": facet(
                        DELIVERY_FACET,
                        fingerprint="sha256:" + "5d" * 32,
                        channel="email",
                        messageRef="m-1",
                        recipients=recipients,
                    )
                },
            }
        ],
    )
    return [event(T0, eventType="START", run=agent_run, job=AGENT), tool_call, answer]


def headers(*roles: Role) -> dict[str, str]:
    return {"authorization": f"Bearer {TEST_ISSUER.token(*roles)}"}


def traces(identity: str) -> set[str]:
    """Ce qu'une identité laisserait d'elle dans une ligne : telle quelle, en minuscules, en
    hexadécimal, en base64, encodée pour une adresse, et ses condensés sans clé, qu'une
    énumération de l'annuaire retournerait."""
    raw = identity.encode()
    lowered = identity.lower().encode()
    return {
        identity,
        identity.lower(),
        identity.upper(),
        raw.hex(),
        lowered.hex(),
        base64.b64encode(raw).decode().rstrip("="),
        base64.b64encode(lowered).decode().rstrip("="),
        quote(identity),
        *(
            hashlib.new(algorithm, data).hexdigest()
            for algorithm in ("md5", "sha1", "sha256")
            for data in (raw, lowered)
        ),
    }


async def declared_rows(engine: AsyncEngine) -> dict[str, list[str]]:
    """Chaque table du schéma déclaré, ligne par ligne, sous forme de texte. Une table
    partitionnée se lit en entier par son parent."""
    found: dict[str, list[str]] = {}
    async with engine.connect() as conn:
        for table in schema.metadata.sorted_tables:
            rows = await conn.execute(
                text(f'SELECT CAST(row_to_json(t) AS text) FROM "{table.name}" t')  # noqa: S608
            )
            found[table.name] = list(rows.scalars())
    return found


async def test_only_a_pseudonym_enters_the_base_and_nothing_relates_it_to_a_person(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Critères : un destinataire n'est dans la base que sous son pseudonyme HMAC ; la base
    seule, sans le coffre, ne relie aucun pseudonyme à une personne."""
    key_material = secrets.token_bytes(32)
    key = PseudonymKey(key_material)
    # Deux personnes inventées, sous une marque que l'on cherche ensuite partout.
    mark = "canari" + secrets.token_hex(3)
    person, colleague = f"Camille.{mark}@Exemple.FR", f"dominique.{mark}@exemple.fr"
    pseudonyms = [
        value
        for identity in (person, colleague)
        if (value := pseudonym_of(key, identity)) is not None
    ]
    assert len(pseudonyms) == 2
    target = pseudonyms[0]

    app = create_app(engine, authentication=authentication(), pseudonym_vault=LocalVault([key]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        # Une identité en clair est refusée à l'ingestion, sans être recopiée ni gardée.
        clear = await client.post(
            "/api/v1/lineage/batch",
            json=request_events([person])[-1:],
            headers=headers(Role.EMITTER),
        )
        assert (clear.status_code, clear.json()["accepted"]) == (422, 0)
        assert [error["type"] for error in clear.json()["rejected"][0]["errors"]] == [
            "recipient_not_pseudonym"
        ]
        assert mark not in clear.text

        # L'émetteur pseudonymise ses destinataires avec la clé du coffre.
        sent = await client.post(
            "/api/v1/lineage/batch",
            json=request_events(pseudonyms),
            headers=headers(Role.EMITTER),
        )
        assert sent.json() == {"accepted": 3, "duplicates": 0, "rejected": []}
        while not (await process_batch(engine)).empty:
            pass

        incident = await client.post(
            "/api/v1/incidents",
            json={
                "scope": {"datasets": [RATES]},
                "window": {"from": "2026-11-03T06:00:00Z", "to": "2026-11-03T16:00:00Z"},
                "severity": "high",
            },
            headers=headers(Role.INCIDENT_MANAGER),
        )
        assert incident.status_code == 201
        assert await process_analysis(engine) is not None
        detail = await client.get(
            f"/api/v1/incidents/{incident.json()['id']}", headers=headers(Role.READER)
        )
        (touched,) = detail.json()["result"]["answers"]
        assert sorted(touched["recipients"]) == sorted(pseudonyms)

        revealed = await client.post(
            "/api/v1/recipients/reveal",
            json={
                "pseudonym": target,
                "reason": "recall",
                "candidates": [colleague, person.lower()],
            },
            headers=headers(Role.INCIDENT_MANAGER),
        )
        assert (revealed.status_code, revealed.json()["candidate"]) == (200, 1)

    base = await declared_rows(engine)
    # Le parcours a bien rempli ce qu'il devait : le pseudonyme est là où il doit être.
    holding = {name for name, rows in base.items() if any(target in row for row in rows)}
    assert holding == HOLDING_PSEUDONYMS
    # Aucune identité, sous aucune forme, ni dans la base, ni dans les journaux.
    whole = "\n".join(row for rows in base.values() for row in rows)
    for identity in (person, colleague):
        for trace in traces(identity):
            assert trace not in whole
            assert trace not in caplog.text
    assert mark not in whole
    # La clé du coffre n'est pas dans la base : sans elle, un pseudonyme ne se recalcule pas.
    for written in (key_material.hex(), base64.b64encode(key_material).decode().rstrip("=")):
        assert written not in whole


def test_no_column_of_the_declared_schema_could_hold_a_ciphered_identity() -> None:
    """Une identité, même chiffrée, à côté de son pseudonyme ferait une table de
    correspondance pour qui obtient la clé. Le schéma déclaré n'a aucune colonne binaire ; et
    à côté d'une colonne de pseudonymes, aucune colonne de texte libre : chacune est une clé,
    ou tenue par une contrainte (une liste fermée, une forme, une borne)."""
    with_pseudonyms = []
    for table in schema.metadata.sorted_tables:
        for column in table.columns:
            assert not isinstance(column.type, LargeBinary | BYTEA), (table.name, column.name)
        if "pseudonym" not in table.columns:
            continue
        with_pseudonyms.append(table.name)
        keys = {column.name for column in table.primary_key.columns} | {
            element.parent.name for element in table.foreign_keys
        }
        checked = " ".join(
            str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        )
        for column in table.columns:
            if column.name == "pseudonym" or not isinstance(column.type, Text | JSONB):
                continue
            assert column.name in keys or column.name in checked, (table.name, column.name)
    assert sorted(with_pseudonyms) == ["incident_recipient", "recipient", "recipient_revelation"]
