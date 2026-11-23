"""Révéler un destinataire : une action tracée, réservée, bornée, qui ne garde aucune identité.

La personne apporte des identités candidates ; l'API dit laquelle a le pseudonyme
visé, en recalculant le HMAC avec les clés du coffre, et journalise l'action, son
auteur et son motif. Le lecteur ne révèle pas ; l'auditeur et l'administrateur lisent
le journal.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.auth.access import Authentication
from answerlineage_api.auth.roles import Role
from answerlineage_api.pseudonyms import store
from answerlineage_api.pseudonyms.vault import (
    LocalVault,
    PseudonymKey,
    PseudonymVault,
    pseudonym_of,
)
from answerlineage_api.store.graph import AgentRef, Answer, Delivery, RunRecord, record_run
from api.tests.issuer import ISSUER, TEST_ISSUER, authentication

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Les clés du coffre : tirées pour le test, nées et mortes ici.
CURRENT = PseudonymKey(secrets.token_bytes(32))
PREVIOUS = PseudonymKey(secrets.token_bytes(32))
# Des identités inventées, sous une marque que l'on cherche ensuite partout.
MARK = "canari" + "b7e4"
PERSON = f"camille.{MARK}@exemple.fr"
OTHER = f"dominique.{MARK}@exemple.fr"
FORMER = f"claude.{MARK}@exemple.fr"
STRANGER = f"inconnu.{MARK}@exemple.fr"


def pseudonym(key: PseudonymKey, identity: str) -> str:
    found = pseudonym_of(key, identity)
    assert found is not None
    return found


def vault() -> LocalVault:
    return LocalVault([CURRENT, PREVIOUS])


def app(engine: AsyncEngine, **changes: Any) -> Any:
    options: dict[str, Any] = {"authentication": authentication(), "pseudonym_vault": vault()}
    return create_app(engine, **{**options, **changes})


def bearer(*roles: Role, **claims: Any) -> dict[str, str]:
    return {"authorization": f"Bearer {TEST_ISSUER.token(*roles, **claims)}"}


def manager() -> dict[str, str]:
    """Le jeton d'un gestionnaire d'incident, frappé à l'appel : il vit cinq minutes."""
    return bearer(Role.INCIDENT_MANAGER)


@pytest.fixture
async def graph(engine: AsyncEngine) -> AsyncEngine:
    """Une réponse diffusée à deux personnes, la seconde pseudonymisée avant une rotation."""
    record = RunRecord(
        run_id=uuid4(),
        kind="agent",
        state="COMPLETE",
        event_time=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        agent=AgentRef("agent://ventes", "assistant"),
        answers=(
            Answer(
                "sha256:" + "f1" * 32,
                deliveries=(
                    Delivery(
                        "email",
                        "m-1",
                        recipients=(pseudonym(CURRENT, PERSON), pseudonym(PREVIOUS, FORMER)),
                    ),
                ),
            ),
        ),
    )
    async with engine.begin() as conn:
        await record_run(conn, record)
    return engine


@pytest.fixture
async def client(graph: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app(graph))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


def asking(target: str, *candidates: str, reason: str = "recall") -> dict[str, Any]:
    return {"pseudonym": target, "reason": reason, "candidates": list(candidates)}


async def journal(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT revealed_at, author_issuer, author_subject, reason, pseudonym,"
                " candidates, result, now() AS now FROM recipient_revelation ORDER BY id"
            )
        )
        return [dict(row._mapping) for row in rows]


async def whole_base(engine: AsyncEngine) -> str:
    """Toute la base, ligne par ligne, sous forme de texte."""
    async with engine.connect() as conn:
        names = (
            await conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
                    " AND NOT c.relispartition"
                )
            )
        ).scalars()
        found: list[str] = []
        for name in list(names):
            rows = await conn.execute(text(f'SELECT CAST(row_to_json(t) AS text) FROM "{name}" t'))  # noqa: S608
            found.extend(rows.scalars())
        return "\n".join(found)


async def test_an_incident_manager_reveals_and_the_action_is_journaled(
    client: httpx.AsyncClient, graph: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Critère : l'action, son auteur et son motif sont journalisés ; jamais l'identité."""
    target = pseudonym(CURRENT, PERSON)
    response = await client.post(
        "/api/v1/recipients/reveal",
        json=asking(target, OTHER, PERSON.upper(), STRANGER, reason="incident_investigation"),
        headers=manager(),
    )
    assert response.status_code == 200, response.text
    revealed = response.json()
    assert (revealed["result"], revealed["candidate"]) == ("matched", 1)
    assert MARK not in response.text

    (entry,) = await journal(graph)
    assert entry["author_issuer"] == ISSUER
    assert entry["author_subject"] == "personne-essai"
    assert (entry["reason"], entry["pseudonym"], entry["candidates"], entry["result"]) == (
        "incident_investigation",
        target,
        3,
        "matched",
    )
    # L'heure est celle de la base, au moment de l'action.
    assert entry["now"] - entry["revealed_at"] < timedelta(minutes=1)
    assert revealed["revelation"] >= 1
    # Aucune identité, révélée ou candidate, n'est restée nulle part.
    assert MARK not in await whole_base(graph)
    assert MARK not in caplog.text


async def test_a_pseudonym_computed_before_a_rotation_is_still_revealed(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/recipients/reveal",
        json=asking(pseudonym(PREVIOUS, FORMER), PERSON, FORMER),
        headers=manager(),
    )
    assert (response.status_code, response.json()["candidate"]) == (200, 1)


async def test_no_candidate_matching_is_journaled_as_such(
    client: httpx.AsyncClient, graph: AsyncEngine
) -> None:
    response = await client.post(
        "/api/v1/recipients/reveal",
        json=asking(pseudonym(CURRENT, PERSON), OTHER, STRANGER),
        headers=manager(),
    )
    assert response.status_code == 200
    assert (response.json()["result"], response.json()["candidate"]) == ("no_match", None)
    assert [entry["result"] for entry in await journal(graph)] == ["no_match"]


async def test_a_pseudonym_no_recipient_carries_is_unknown_and_journaled(
    client: httpx.AsyncClient, graph: AsyncEngine
) -> None:
    response = await client.post(
        "/api/v1/recipients/reveal",
        json=asking(pseudonym(CURRENT, STRANGER), STRANGER),
        headers=manager(),
    )
    assert (response.status_code, response.json()) == (404, {"error": "unknown_recipient"})
    assert [entry["result"] for entry in await journal(graph)] == ["unknown_recipient"]


@pytest.mark.parametrize(
    "roles",
    [
        (Role.READER,),
        (Role.DATA_STEWARD,),
        (Role.AGENT_OWNER,),
        (Role.AUDITOR,),
        (Role.ADMINISTRATOR,),
        (Role.EMITTER,),
        (Role.READER, Role.AUDITOR, Role.ADMINISTRATOR),
    ],
    ids=lambda roles: "+".join(role.value for role in roles),
)
async def test_whoever_is_not_an_incident_manager_is_refused_and_nothing_is_journaled(
    client: httpx.AsyncClient, graph: AsyncEngine, roles: tuple[Role, ...]
) -> None:
    """Critère : un lecteur qui tente une révélation est refusé."""
    response = await client.post(
        "/api/v1/recipients/reveal",
        json=asking(pseudonym(CURRENT, PERSON), PERSON),
        headers=bearer(*roles),
    )
    assert (response.status_code, response.json()) == (403, {"error": "insufficient_role"})
    assert MARK not in response.text
    assert await journal(graph) == []


async def test_a_token_without_an_admitted_subject_reveals_nothing(
    client: httpx.AsyncClient, graph: AsyncEngine
) -> None:
    claims = TEST_ISSUER.claims((Role.INCIDENT_MANAGER.value,))
    anonymous = TEST_ISSUER.sign({name: value for name, value in claims.items() if name != "sub"})
    tokens = [
        anonymous,
        *(
            TEST_ISSUER.token(Role.INCIDENT_MANAGER, sub=subject)
            for subject in ("a b", "<b>", "x" * 256, "")
        ),
    ]
    for token in tokens:
        response = await client.post(
            "/api/v1/recipients/reveal",
            json=asking(pseudonym(CURRENT, PERSON), PERSON),
            headers={"authorization": f"Bearer {token}"},
        )
        assert (response.status_code, response.json()) == (403, {"error": "author_unidentified"})
    assert await journal(graph) == []


async def test_with_authentication_disabled_no_one_is_identified_to_reveal(
    graph: AsyncEngine,
) -> None:
    transport = httpx.ASGITransport(app=app(graph, authentication=Authentication.disabled()))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post(
            "/api/v1/recipients/reveal", json=asking(pseudonym(CURRENT, PERSON), PERSON)
        )
    assert (response.status_code, response.json()) == (403, {"error": "author_unidentified"})


async def test_without_a_key_vault_nothing_is_computed_nor_journaled(graph: AsyncEngine) -> None:
    transport = httpx.ASGITransport(app=create_app(graph, authentication=authentication()))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post(
            "/api/v1/recipients/reveal",
            json=asking(pseudonym(CURRENT, PERSON), PERSON),
            headers=manager(),
        )
    assert (response.status_code, response.json()) == (
        503,
        {"error": "pseudonym_keys_unavailable"},
    )
    assert await journal(graph) == []


@pytest.mark.parametrize(
    ("body", "path", "code"),
    [
        (asking(PERSON, PERSON), "/pseudonym", "pseudonym_malformed"),
        (asking("hmac-sha256:" + "AB" * 32, PERSON), "/pseudonym", "pseudonym_malformed"),
        ({**asking("hmac-sha256:" + "ab" * 32, PERSON), "reason": "curiosite"}, "/reason", None),
        (asking("hmac-sha256:" + "ab" * 32, PERSON, reason=f"motif {MARK}"), "/reason", None),
        (asking("hmac-sha256:" + "ab" * 32), "/candidates", "no_candidate"),
        (
            asking(
                "hmac-sha256:" + "ab" * 32, *(f"{MARK}{rank}@exemple.fr" for rank in range(101))
            ),
            "/candidates",
            "too_many_items",
        ),
        (asking("hmac-sha256:" + "ab" * 32, PERSON, f"Camille {MARK}"), "/candidates/1", None),
        (asking("hmac-sha256:" + "ab" * 32, MARK * 40), "/candidates/0", None),
        (asking("hmac-sha256:" + "ab" * 32, MARK + chr(0x200B)), "/candidates/0", None),
        ({**asking("hmac-sha256:" + "ab" * 32, PERSON), MARK: "x"}, "", "unknown_field"),
        ({**asking("hmac-sha256:" + "ab" * 32, PERSON), "candidates": [7]}, "/candidates/0", None),
        ([PERSON], "", "not_an_object"),
    ],
    ids=[
        "adresse au lieu du pseudonyme",
        "majuscules",
        "motif hors liste",
        "motif libre",
        "aucune candidate",
        "101 candidates",
        "nom avec un blanc",
        "trop longue",
        "invisible",
        "champ inconnu",
        "nombre",
        "liste",
    ],
)
async def test_a_wrong_request_is_rejected_without_echo_and_journals_nothing(
    client: httpx.AsyncClient,
    graph: AsyncEngine,
    body: Any,
    path: str,
    code: str | None,
) -> None:
    response = await client.post("/api/v1/recipients/reveal", json=body, headers=manager())
    assert response.status_code == 422, response.text
    errors = response.json()["errors"]
    assert path in [error["path"] for error in errors]
    if code is not None:
        assert code in [error["type"] for error in errors]
    assert MARK not in response.text
    assert PERSON not in response.text
    assert await journal(graph) == []


async def test_a_body_too_large_is_refused_before_being_read(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/recipients/reveal",
        content=b'{"candidates": ["' + b"a" * (300 * 1024) + b'"]}',
        headers={**manager(), "content-type": "application/json"},
    )
    assert (response.status_code, response.json()) == (413, {"error": "body_too_large"})


async def test_an_author_is_held_to_a_rate_then_told_when_to_return(
    client: httpx.AsyncClient, graph: AsyncEngine
) -> None:
    target = pseudonym(CURRENT, PERSON)
    for _ in range(store.REVELATIONS_PER_HOUR):
        response = await client.post(
            "/api/v1/recipients/reveal", json=asking(target, OTHER), headers=manager()
        )
        assert response.status_code == 200
    refused = await client.post(
        "/api/v1/recipients/reveal", json=asking(target, PERSON), headers=manager()
    )
    assert (refused.status_code, refused.json()) == (429, {"error": "revelation_rate_limited"})
    assert 1 <= int(refused.headers["retry-after"]) <= 3600
    # Le refus n'est pas journalisé : le journal ne grandit pas au rythme de qui insiste.
    assert len(await journal(graph)) == store.REVELATIONS_PER_HOUR
    # Une autre personne garde sa place.
    other = await client.post(
        "/api/v1/recipients/reveal",
        json=asking(target, PERSON),
        headers=bearer(Role.INCIDENT_MANAGER, sub="autre-personne"),
    )
    assert (other.status_code, other.json()["candidate"]) == (200, 0)


async def test_simultaneous_revelations_of_one_author_never_pass_the_rate(
    graph: AsyncEngine,
) -> None:
    target = pseudonym(CURRENT, PERSON)
    transport = httpx.ASGITransport(app=app(graph))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        import asyncio

        answers = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/recipients/reveal", json=asking(target, OTHER), headers=manager()
                )
                for _ in range(store.REVELATIONS_PER_HOUR + 5)
            )
        )
    assert (
        sorted(response.status_code for response in answers)
        == [200] * (store.REVELATIONS_PER_HOUR) + [429] * 5
    )
    assert len(await journal(graph)) == store.REVELATIONS_PER_HOUR


async def test_the_journal_is_read_by_the_auditor_and_the_administrator_only(
    client: httpx.AsyncClient, graph: AsyncEngine
) -> None:
    for identity in (OTHER, PERSON):
        await client.post(
            "/api/v1/recipients/reveal",
            json=asking(pseudonym(CURRENT, PERSON), identity, reason="data_subject_request"),
            headers=manager(),
        )
    for role in (Role.AUDITOR, Role.ADMINISTRATOR):
        first = await client.get(
            "/api/v1/recipients/revelations", params={"limit": 1}, headers=bearer(role)
        )
        assert first.status_code == 200, first.text
        page = first.json()
        (latest,) = page["revelations"]
        assert latest["result"] == "matched"
        assert latest["author"] == {"issuer": ISSUER, "subject": "personne-essai"}
        assert (latest["reason"], latest["candidates"]) == ("data_subject_request", 1)
        rest = await client.get(
            "/api/v1/recipients/revelations",
            params={"cursor": page["next_cursor"]},
            headers=bearer(role),
        )
        assert [entry["result"] for entry in rest.json()["revelations"]] == ["no_match"]
        assert rest.json()["next_cursor"] is None
        assert MARK not in first.text + rest.text
    for role in (Role.READER, Role.INCIDENT_MANAGER, Role.DATA_STEWARD, Role.AGENT_OWNER):
        refused = await client.get("/api/v1/recipients/revelations", headers=bearer(role))
        assert refused.status_code == 403


class CountingVault:
    """Un coffre qui compte ses calculs : un refus avant le corps n'en fait aucun."""

    def __init__(self, inner: PseudonymVault) -> None:
        self.inner = inner
        self.calls = 0

    async def pseudonyms(self, canonical: bytes) -> tuple[str, ...]:
        self.calls += 1
        return await self.inner.pseudonyms(canonical)


async def test_the_vault_stops_at_the_first_matching_candidate(graph: AsyncEngine) -> None:
    counting = CountingVault(vault())
    transport = httpx.ASGITransport(app=app(graph, pseudonym_vault=counting))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post(
            "/api/v1/recipients/reveal",
            json=asking(pseudonym(CURRENT, PERSON), OTHER, PERSON, STRANGER),
            headers=manager(),
        )
        refused = await client.post(
            "/api/v1/recipients/reveal",
            json=asking(pseudonym(CURRENT, PERSON), PERSON),
            headers=bearer(Role.READER),
        )
    assert (response.json()["candidate"], counting.calls) == (1, 2)
    assert refused.status_code == 403
    assert counting.calls == 2
