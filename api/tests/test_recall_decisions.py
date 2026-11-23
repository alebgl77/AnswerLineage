"""Valider, modifier ou refuser une liste de rappel : une action journalisée, réservée, faite
de listes fermées ; et aucun envoi sans validation, ce que la base impose elle-même."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.auth.access import Authentication
from answerlineage_api.auth.roles import Role
from api.tests.issuer import ISSUER, TEST_ISSUER, authentication
from api.tests.test_recall_list import declared, listed, pseudonym

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

OWNER = "responsable-agent-1"
CANARY = "canari" + "5d2e"
_DISPATCH = text("INSERT INTO recall_dispatch (incident_id, pseudonym) VALUES (:id, :who)")
_ESCALATED = text("INSERT INTO recall_decision (incident_id, decision) VALUES (:id, 'escalated')")


def bearer(*roles: Role, **claims: Any) -> dict[str, str]:
    return {"authorization": f"Bearer {TEST_ISSUER.token(*roles, **claims)}"}


def owner() -> dict[str, str]:
    """Le jeton d'un responsable d'agent, frappé à l'appel."""
    return bearer(Role.AGENT_OWNER, sub=OWNER)


@pytest.fixture
async def client(engine: AsyncEngine) -> Any:
    app = create_app(engine, authentication=authentication())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http


@pytest.fixture
async def pending(engine: AsyncEngine) -> UUID:
    """Un incident et sa liste en attente : deux destinataires."""
    incident_id = await declared(engine)
    await listed(engine, incident_id, [(2_000_000, 2), (1_000_000, 1)])
    return incident_id


def decision_of(incident_id: UUID) -> str:
    return f"/api/v1/incidents/{incident_id}/recall-list/decision"


async def journal(engine: AsyncEngine, incident_id: UUID | None = None) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT decision, author_issuer, author_subject, template, reason, decided_at"
                " FROM recall_decision"
                " WHERE CAST(:id AS uuid) IS NULL OR incident_id = :id ORDER BY id"
            ),
            {"id": incident_id},
        )
        return [tuple(row) for row in found]


async def dispatch(engine: AsyncEngine, incident_id: UUID, rank: int = 1) -> None:
    """Met un message à l'envoi directement dans la base, comme le ferait un code qui
    contournerait l'API."""
    async with engine.begin() as conn:
        await conn.execute(_DISPATCH, {"id": incident_id, "who": pseudonym(rank)})


def constraint_of(error: IntegrityError) -> str | None:
    diagnostic = getattr(error.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None)


async def test_validating_a_pending_list_journals_the_decision_its_author_and_the_time(
    engine: AsyncEngine, client: httpx.AsyncClient, pending: UUID
) -> None:
    """Critère : la décision, l'identité de son auteur et l'heure sont journalisées, et
    l'envoi devient possible."""
    async with engine.connect() as conn:
        before = (await conn.execute(text("SELECT now()"))).scalar_one()

    answered = await client.post(
        decision_of(pending), json={"decision": "validated"}, headers=owner()
    )

    assert answered.status_code == 200, answered.text
    body = answered.json()
    decided_at = datetime.fromisoformat(body["decision"]["decided_at"])
    assert body == {
        "incident_id": str(pending),
        "changed": True,
        "state": "validated",
        "decision": {
            "decision": "validated",
            "decided_at": body["decision"]["decided_at"],
            "author": {"issuer": ISSUER, "subject": OWNER},
            "template": "standard",
            "reason": None,
        },
        "escalated_at": None,
        "dispatched": 0,
    }
    ((decision, issuer, subject, template, reason, journaled_at),) = await journal(engine, pending)
    assert (decision, issuer, subject, template, reason) == (
        "validated",
        ISSUER,
        OWNER,
        "standard",
        None,
    )
    # L'heure est celle de la base, posée à l'insertion.
    assert journaled_at == decided_at and before <= journaled_at

    # L'envoi devient possible : la base accepte qu'un message parte.
    await dispatch(engine, pending)
    shown = await client.get(
        f"/api/v1/incidents/{pending}/recall-list", headers=bearer(Role.READER)
    )
    assert (shown.json()["state"], shown.json()["dispatched"]) == ("validated", 1)


async def test_a_list_without_validation_has_sent_nothing_and_the_base_refuses_any_dispatch(
    engine: AsyncEngine, client: httpx.AsyncClient, pending: UUID
) -> None:
    """Critère : une liste non validée n'a rien envoyé. La base refuse un envoi forcé, même
    quand le code est contourné par une écriture directe ; ni une remontée ni un refus ne
    l'ouvrent."""
    shown = (
        await client.get(f"/api/v1/incidents/{pending}/recall-list", headers=bearer(Role.READER))
    ).json()
    assert (shown["state"], shown["decision"], shown["dispatched"]) == ("pending", None, 0)

    with pytest.raises(IntegrityError) as refused:
        await dispatch(engine, pending)
    assert constraint_of(refused.value) == "ck_recall_dispatch_validated"

    async with engine.begin() as conn:
        await conn.execute(_ESCALATED, {"id": pending})
    with pytest.raises(IntegrityError) as refused:
        await dispatch(engine, pending)
    assert constraint_of(refused.value) == "ck_recall_dispatch_validated"

    refusal = {"decision": "refused", "reason": "already_informed"}
    assert (
        await client.post(decision_of(pending), json=refusal, headers=owner())
    ).status_code == 200
    with pytest.raises(IntegrityError) as refused:
        await dispatch(engine, pending)
    assert constraint_of(refused.value) == "ck_recall_dispatch_validated"

    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM recall_dispatch"))).scalar() == 0


async def test_modifying_chooses_a_template_and_refusing_gives_a_reason(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    modified, refused = await declared(engine), await declared(engine)
    for incident_id in (modified, refused):
        await listed(engine, incident_id, [(1_000_000, 1)])

    changed = await client.post(
        decision_of(modified),
        json={"decision": "modified", "template": "decision_review"},
        headers=owner(),
    )
    declined = await client.post(
        decision_of(refused),
        json={"decision": "refused", "reason": "not_affected"},
        headers=owner(),
    )

    assert changed.json()["state"] == "modified"
    assert changed.json()["decision"]["template"] == "decision_review"
    assert (declined.json()["state"], declined.json()["decision"]["reason"]) == (
        "refused",
        "not_affected",
    )
    assert declined.json()["decision"]["template"] is None
    await dispatch(engine, modified)
    with pytest.raises(IntegrityError):
        await dispatch(engine, refused)


async def test_a_decision_is_final_and_asking_it_again_changes_nothing(
    engine: AsyncEngine, client: httpx.AsyncClient, pending: UUID
) -> None:
    validated = {"decision": "validated"}
    first = await client.post(decision_of(pending), json=validated, headers=owner())
    again = await client.post(
        decision_of(pending), json=validated, headers=bearer(Role.ADMINISTRATOR, sub="autre")
    )
    other = await client.post(
        decision_of(pending), json={"decision": "refused", "reason": "no_impact"}, headers=owner()
    )
    modified = await client.post(
        decision_of(pending),
        json={"decision": "modified", "template": "information"},
        headers=owner(),
    )

    assert (again.status_code, again.json()["changed"]) == (200, False)
    # Rendue telle qu'elle a été décidée, par son premier auteur.
    assert again.json()["decision"] == first.json()["decision"]
    assert (other.status_code, other.json()) == (409, {"error": "recall_list_decided"})
    assert (modified.status_code, modified.json()) == (409, {"error": "recall_list_decided"})
    assert len(await journal(engine, pending)) == 1


async def test_a_decision_after_the_escalation_keeps_both_in_the_journal(
    engine: AsyncEngine, client: httpx.AsyncClient, pending: UUID
) -> None:
    async with engine.begin() as conn:
        await conn.execute(_ESCALATED, {"id": pending})

    answered = await client.post(
        decision_of(pending), json={"decision": "validated"}, headers=owner()
    )

    body = answered.json()
    assert (body["state"], body["changed"]) == ("validated", True)
    assert body["escalated_at"] is not None
    assert [row[0] for row in await journal(engine, pending)] == ["escalated", "validated"]


@pytest.mark.parametrize(
    "role", [Role.READER, Role.DATA_STEWARD, Role.INCIDENT_MANAGER, Role.AUDITOR, Role.EMITTER]
)
async def test_whoever_is_not_an_agent_owner_or_administrator_is_refused_and_nothing_is_journaled(
    engine: AsyncEngine, client: httpx.AsyncClient, pending: UUID, role: Role
) -> None:
    answered = await client.post(
        decision_of(pending), json={"decision": "validated"}, headers=bearer(role)
    )

    assert (answered.status_code, answered.json()) == (403, {"error": "insufficient_role"})
    assert await journal(engine) == []


async def test_the_administrator_decides_under_its_own_name(
    engine: AsyncEngine, client: httpx.AsyncClient, pending: UUID
) -> None:
    answered = await client.post(
        decision_of(pending),
        json={"decision": "validated"},
        headers=bearer(Role.ADMINISTRATOR, sub="exploitant-1"),
    )

    assert answered.json()["decision"]["author"] == {"issuer": ISSUER, "subject": "exploitant-1"}


@pytest.mark.parametrize("subject", [None, "", "a b", "<script>", "x" * 256])
async def test_a_token_without_an_admitted_subject_decides_nothing(
    engine: AsyncEngine, client: httpx.AsyncClient, pending: UUID, subject: str | None
) -> None:
    token = TEST_ISSUER.claims((str(Role.AGENT_OWNER),))
    if subject is None:
        del token["sub"]
    else:
        token["sub"] = subject

    answered = await client.post(
        decision_of(pending),
        json={"decision": "validated"},
        headers={"authorization": f"Bearer {TEST_ISSUER.sign(token)}"},
    )

    assert (answered.status_code, answered.json()) == (403, {"error": "author_unidentified"})
    assert await journal(engine) == []


async def test_without_authentication_no_one_is_identified_and_nothing_is_decided(
    engine: AsyncEngine, pending: UUID
) -> None:
    """La démonstration désactive l'authentification : chaque appel a tous les rôles, mais
    aucune identité, et une décision se journalise sous le nom de son auteur."""
    app = create_app(engine, authentication=Authentication.disabled())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        answered = await http.post(decision_of(pending), json={"decision": "validated"})

    assert (answered.status_code, answered.json()) == (403, {"error": "author_unidentified"})
    assert await journal(engine) == []


@pytest.mark.parametrize(
    ("body", "path", "code"),
    [
        ({"decision": "validated", "message": f"Écris à tous {CANARY}"}, "", "unknown_field"),
        (
            {"decision": "validated", "author": {"issuer": ISSUER, "subject": CANARY}},
            "",
            "unknown_field",
        ),
        ({"decision": "refused", "reason": f"autre : {CANARY}"}, "/reason", "literal_error"),
        ({"decision": "modified", "template": f"<b>{CANARY}</b>"}, "/template", "literal_error"),
        ({"decision": f"sent {CANARY}"}, "/decision", "literal_error"),
        ({"decision": "modified"}, "/template", "missing"),
        ({"decision": "refused"}, "/reason", "missing"),
        ({"decision": "validated", "template": "information"}, "/template", "unexpected_field"),
        (
            {"decision": "refused", "reason": "no_impact", "template": "standard"},
            "/template",
            "unexpected_field",
        ),
        ({"decision": "validated", "reason": "no_impact"}, "/reason", "unexpected_field"),
        ({}, "/decision", "missing"),
        ([CANARY], "", "not_an_object"),
    ],
)
async def test_free_text_or_a_value_out_of_its_list_is_refused_without_being_copied(
    engine: AsyncEngine,
    client: httpx.AsyncClient,
    pending: UUID,
    body: Any,
    path: str,
    code: str,
) -> None:
    answered = await client.post(decision_of(pending), json=body, headers=owner())

    assert answered.status_code == 422
    assert {"path": path, "type": code} in [
        {"path": error["path"], "type": error["type"]} for error in answered.json()["errors"]
    ]
    assert CANARY not in answered.text
    assert await journal(engine) == []


async def test_a_decision_needs_an_incident_with_a_list(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    without = await declared(engine)

    unknown = await client.post(
        decision_of(uuid4()), json={"decision": "validated"}, headers=owner()
    )
    unlisted = await client.post(
        decision_of(without), json={"decision": "validated"}, headers=owner()
    )

    assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_incident"})
    assert (unlisted.status_code, unlisted.json()) == (404, {"error": "no_recall_list"})


async def test_an_unreadable_or_oversized_body_is_refused(
    client: httpx.AsyncClient, pending: UUID
) -> None:
    headers = {**owner(), "content-type": "application/json"}
    unreadable = await client.post(decision_of(pending), content=b"{", headers=headers)
    oversized = await client.post(
        decision_of(pending), content=b'{"decision": "validated"}' + b" " * 5000, headers=headers
    )

    assert (unreadable.status_code, unreadable.json()) == (400, {"error": "invalid_json"})
    assert (oversized.status_code, oversized.json()) == (413, {"error": "body_too_large"})


# La base, quelle que soit la connexion qui écrit.


async def test_the_decision_journal_is_appended_to_never_changed(
    engine: AsyncEngine, pending: UUID
) -> None:
    async with engine.begin() as conn:
        await conn.execute(_ESCALATED, {"id": pending})
    for change in (
        "UPDATE recall_decision SET decision = 'validated', author_issuer = 'i',"
        " author_subject = 's', template = 'standard'",
        "UPDATE recall_decision SET decided_at = now() - interval '1 day'",
        "DELETE FROM recall_decision",
        "TRUNCATE recall_decision",
    ):
        with pytest.raises(IntegrityError) as refused:
            async with engine.begin() as conn:
                await conn.execute(text(change))
        assert constraint_of(refused.value) == "ck_recall_decision_append_only"
    async with engine.connect() as conn:
        withdrawn = await conn.execute(
            text(
                "SELECT a.privilege_type FROM pg_class c, aclexplode(c.relacl) a"
                " WHERE c.relname = 'recall_decision'"
                " AND a.privilege_type IN ('UPDATE', 'DELETE', 'TRUNCATE')"
            )
        )
        assert list(withdrawn.scalars()) == []
    assert len(await journal(engine, pending)) == 1


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ("'sent', 'i', 's', NULL, NULL", "ck_recall_decision_decision"),
        ("'modified', 'i', 's', 'texte libre', NULL", "ck_recall_decision_template"),
        ("'refused', 'i', 's', NULL, 'texte libre'", "ck_recall_decision_reason"),
        ("'validated', NULL, NULL, 'standard', NULL", "ck_recall_decision_author"),
        ("'escalated', 'i', 's', NULL, NULL", "ck_recall_decision_author"),
        ("'validated', 'i', NULL, 'standard', NULL", "ck_recall_decision_author"),
        ("'validated', 'i', 's', NULL, NULL", "ck_recall_decision_given"),
        ("'refused', 'i', 's', 'standard', 'no_impact'", "ck_recall_decision_given"),
        ("'validated', 'i', 's', 'standard', 'no_impact'", "ck_recall_decision_given"),
    ],
)
async def test_the_decision_journal_holds_only_closed_lists_and_an_author_for_a_decision(
    engine: AsyncEngine, pending: UUID, values: str, constraint: str
) -> None:
    # Seules les valeurs écrites dans ce test sont interpolées.
    columns = "incident_id, decision, author_issuer, author_subject, template, reason"
    inserted = f"INSERT INTO recall_decision ({columns}) VALUES (:id, {values})"  # noqa: S608
    with pytest.raises(IntegrityError) as refused:
        async with engine.begin() as conn:
            await conn.execute(text(inserted), {"id": pending})
    assert constraint_of(refused.value) == constraint


async def test_a_list_has_one_decision_and_one_escalation_at_most(
    engine: AsyncEngine, pending: UUID
) -> None:
    validated = text(
        "INSERT INTO recall_decision (incident_id, decision, author_issuer, author_subject,"
        " template) VALUES (:id, 'validated', 'i', 's', 'standard')"
    )
    async with engine.begin() as conn:
        await conn.execute(_ESCALATED, {"id": pending})
        await conn.execute(validated, {"id": pending})
    for statement, index in (
        (_ESCALATED, "uq_recall_decision_escalated"),
        (validated, "uq_recall_decision_decided"),
    ):
        with pytest.raises(IntegrityError) as refused:
            async with engine.begin() as conn:
                await conn.execute(statement, {"id": pending})
        assert constraint_of(refused.value) == index


async def test_a_decided_list_no_longer_changes_and_an_escalated_one_still_does(
    engine: AsyncEngine, client: httpx.AsyncClient, pending: UUID
) -> None:
    """Ce qui a été validé est ce qui partira : la base fige la liste décidée."""
    changes = (
        "UPDATE recall_list SET recipients = 9 WHERE incident_id = :id",
        "UPDATE recall_entry SET exposure = 0 WHERE incident_id = :id",
        "DELETE FROM recall_entry_answer WHERE incident_id = :id",
        "INSERT INTO recall_entry (incident_id, pseudonym, rank, exposure, answers,"
        " latest_answer_at) VALUES (:id, 'hmac-sha256:intrus', 99, 1, 1, now())",
    )
    async with engine.begin() as conn:
        await conn.execute(_ESCALATED, {"id": pending})
        await conn.execute(text(changes[0]), {"id": pending})

    await client.post(decision_of(pending), json={"decision": "validated"}, headers=owner())

    for change in changes:
        with pytest.raises(IntegrityError) as refused:
            async with engine.begin() as conn:
                await conn.execute(text(change), {"id": pending})
        assert constraint_of(refused.value) == "ck_recall_list_frozen"


# Les listes, de l'incident le plus grave au moins grave.


async def test_the_lists_go_from_the_most_severe_incident_and_say_which_escalated(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    low, critical, high = [await declared(engine) for _ in range(3)]
    async with engine.begin() as conn:
        for incident_id, severity in ((low, "low"), (high, "high")):
            await conn.execute(
                text("UPDATE incident SET severity = :severity WHERE id = :id"),
                {"severity": severity, "id": incident_id},
            )
    for incident_id in (low, critical, high):
        await listed(engine, incident_id, [(1_000_000, 1)])
    async with engine.begin() as conn:
        await conn.execute(_ESCALATED, {"id": low})
    await client.post(decision_of(high), json={"decision": "validated"}, headers=owner())
    steward = bearer(Role.DATA_STEWARD)

    every = (await client.get("/api/v1/recall-lists", headers=steward)).json()
    escalated = (
        await client.get(
            "/api/v1/recall-lists",
            params={"state": "pending", "escalated": "true"},
            headers=steward,
        )
    ).json()
    decided = (
        await client.get("/api/v1/recall-lists", params={"state": "validated"}, headers=steward)
    ).json()

    assert [item["incident_id"] for item in every["lists"]] == [
        str(critical),
        str(high),
        str(low),
    ]
    assert [item["incident_id"] for item in escalated["lists"]] == [str(low)]
    (summary,) = escalated["lists"]
    assert (summary["state"], summary["severity"], summary["dispatched"]) == ("pending", "low", 0)
    assert summary["escalated_at"] is not None and summary["decision"] is None
    assert summary["counts"] == {"recipients": 1, "answers": 1}
    assert [item["incident_id"] for item in decided["lists"]] == [str(high)]
    assert decided["lists"][0]["decision"]["author"]["subject"] == OWNER


async def test_the_lists_are_read_page_by_page_without_loss_or_duplicate(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    created = []
    for _ in range(5):
        incident_id = await declared(engine)
        await listed(engine, incident_id, [])
        created.append(str(incident_id))

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2} | ({} if cursor is None else {"cursor": cursor})
        body = (
            await client.get("/api/v1/recall-lists", params=params, headers=bearer(Role.READER))
        ).json()
        seen.extend(item["incident_id"] for item in body["lists"])
        cursor = body["next_cursor"]
        assert body["more"] is (cursor is not None)
        if cursor is None:
            break
    assert seen == sorted(created)


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": f"<{CANARY}>"},
        {"cursor": "4." + "9" * 18 + "." + "0" * 32},
        {"state": CANARY},
        {"escalated": CANARY},
        {"limit": 101},
    ],
)
async def test_a_malformed_parameter_of_the_lists_is_refused_without_being_copied(
    client: httpx.AsyncClient, params: dict[str, Any]
) -> None:
    answered = await client.get("/api/v1/recall-lists", params=params, headers=bearer(Role.READER))

    assert answered.status_code == 422
    assert CANARY not in answered.text
