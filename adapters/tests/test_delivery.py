"""La réponse de l'agent et sa diffusion, rattachées au run de la demande.

L'intégration publie la réponse (dans un fil Slack, par exemple), puis la
déclare à l'adaptateur : le canal, la référence du message, les destinataires.
L'adaptateur la rattache au run de l'agent ; la réponse n'en sort qu'en
empreinte à clé, les destinataires qu'en pseudonymes, calculés dans le
processus. Rien de cela ne fait attendre l'intégration.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest

from adapters.tests.test_claude_agent_sdk import (
    AGENT,
    KEY,
    SESSION,
    by_type,
    fire,
    lineage,
    prompt_submitted,
    stopped,
    tool_used,
)
from answerlineage_adapters.claude_agent_sdk import (
    FRAMEWORK,
    MAX_ANSWER_LENGTH,
    ClaudeAgentLineage,
)
from answerlineage_emitter.delivery import (
    RECIPIENTS_MAX,
    SLACK,
    UNRESOLVED_CHANNEL,
    UNRESOLVED_MESSAGE,
    slack_message_ref,
)
from answerlineage_emitter.events import DELIVERY_FACET, DELIVERY_FACET_SCHEMA
from answerlineage_emitter.fingerprint import ANSWER_LABEL
from answerlineage_emitter.outbox import Outbox, OutboxSettings
from answerlineage_emitter.pseudonym import PseudonymKey
from answerlineage_emitter.sink import MemorySink

pytestmark = pytest.mark.anyio

PSEUDONYMS = PseudonymKey(bytes(range(32, 64)))
ANSWER = "Le taux de change du jour est de 1,08."
CHANNEL_ID, TS = "C024BE91L", "1355517523.000005"
MESSAGE = slack_message_ref(CHANNEL_ID, TS)
MEMBERS = ["U07AB12CD", "Camille.Martin@Exemple.FR"]


def pseudonym(identity: str) -> str:
    value = PSEUDONYMS.pseudonym(identity)
    assert value is not None
    return value


def delivered(event: Mapping[str, Any]) -> Mapping[str, Any]:
    (output,) = event["outputs"]
    facet: Mapping[str, Any] = output["outputFacets"][DELIVERY_FACET]
    return facet


def deliver(adapter: ClaudeAgentLineage, *recipients: object, **changes: Any) -> None:
    options: dict[str, Any] = {"answer": ANSWER, "session_id": SESSION, **changes}
    channel = options.pop("channel", SLACK)
    message = options.pop("message_ref", MESSAGE)
    given = options.pop("recipients", list(recipients or MEMBERS))
    adapter.record_delivery(channel, message, given, **options)


async def test_a_delivery_after_the_stop_is_attached_to_the_run_of_the_agent() -> None:
    """Critère : quand le run se termine, l'empreinte, le canal, la référence du message et
    les destinataires pseudonymisés sont enregistrés, rattachés à son run."""
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    async with adapter:
        await fire(adapter, "UserPromptSubmit", prompt_submitted())
        await fire(adapter, "PostToolUse", tool_used())
        await fire(adapter, "Stop", stopped(ANSWER))
        deliver(adapter)
    opened, _, ended, sent = sink.events
    assert [event["eventType"] for event in sink.events] == [
        "START",
        "COMPLETE",
        "COMPLETE",
        "OTHER",
    ]
    # Le run de l'agent, clos, reçoit sa sortie après coup : un événement OTHER, que
    # OpenLineage prévoit pour cela, daté de la fin du run, dont l'état reste celui de sa fin.
    assert sent["run"]["runId"] == opened["run"]["runId"] == ended["run"]["runId"]
    assert sent["eventTime"] == ended["eventTime"]
    assert sent["job"] == AGENT.job()
    assert sent["run"]["facets"]["answerlineage_agent"]["framework"] == FRAMEWORK
    (output,) = sent["outputs"]
    assert (output["namespace"], output["name"]) == (AGENT.namespace, f"{AGENT.name}/reponses")
    facet = delivered(sent)
    assert facet["_schemaURL"] == DELIVERY_FACET_SCHEMA
    assert (facet["channel"], facet["messageRef"]) == ("slack", "C024BE91L/1355517523.000005")
    # L'empreinte est un HMAC de la réponse sous la clé du locataire ; les destinataires, des
    # pseudonymes sous la clé des pseudonymes, triés.
    expected = hmac.new(
        bytes(range(32)), ANSWER_LABEL + b"\x00" + ANSWER.encode(), hashlib.sha256
    ).hexdigest()
    assert facet["fingerprint"] == "hmac-sha256:" + expected
    assert facet["recipients"] == sorted(pseudonym(member) for member in MEMBERS)
    # Ni le texte, ni sa taille, ni une identité.
    assert set(facet) == {
        "_producer",
        "_schemaURL",
        "fingerprint",
        "channel",
        "messageRef",
        "recipients",
    }
    written = repr(sink.events)
    for fragment in (ANSWER, "1,08", "U07AB12CD", "u07ab12cd", "camille"):
        assert fragment not in written
    assert (adapter.stats.deliveries, adapter.stats.recipients_refused) == (1, 0)


async def test_a_delivery_while_the_run_is_open_comes_before_its_end() -> None:
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    async with adapter:
        await fire(adapter, "UserPromptSubmit", prompt_submitted())
        deliver(adapter)
        await fire(adapter, "Stop", stopped(ANSWER))
    opened, sent, ended = sink.events
    assert [event["eventType"] for event in sink.events] == ["START", "OTHER", "COMPLETE"]
    assert sent["run"]["runId"] == opened["run"]["runId"] == ended["run"]["runId"]
    # L'événement de fin vient après : c'est lui qui fixe l'état du run.
    assert opened["eventTime"] < sent["eventTime"] < ended["eventTime"]


async def test_the_delivery_goes_to_the_last_request_of_its_session() -> None:
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    async with adapter:
        await fire(adapter, "UserPromptSubmit", prompt_submitted())
        await fire(adapter, "Stop", stopped())
        second = {**prompt_submitted(), "prompt_id": "8f7c6a3e-0000-4000-8000-000000000002"}
        await fire(adapter, "UserPromptSubmit", second)
        await fire(adapter, "Stop", {**stopped(), "prompt_id": second["prompt_id"]})
        other_session = {**prompt_submitted(), "session_id": "session-2", "prompt_id": "autre"}
        await fire(adapter, "UserPromptSubmit", other_session)
        deliver(adapter)
    (_, second_opened, _) = by_type(sink.events, "START")
    (sent,) = by_type(sink.events, "OTHER")
    assert sent["run"]["runId"] == second_opened["run"]["runId"]


async def test_a_delivery_of_a_session_never_seen_closes_a_run_of_its_own() -> None:
    """Un adaptateur branché en cours de route : la diffusion n'a pas de run connu. Elle en
    clôt un, qui porte la réponse ; une seconde diffusion de la même réponse s'y ajoute."""
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    async with adapter:
        deliver(adapter)
        deliver(adapter, message_ref=slack_message_ref("C0123ABCDEF", TS))
    first, second = sink.events
    assert (first["eventType"], second["eventType"]) == ("COMPLETE", "OTHER")
    assert first["run"]["runId"] == second["run"]["runId"]
    assert first["eventTime"] == second["eventTime"]
    assert first["job"] == AGENT.job()
    assert delivered(first)["fingerprint"] == delivered(second)["fingerprint"]
    assert delivered(second)["messageRef"] == "C0123ABCDEF/1355517523.000005"
    assert adapter.stats.deliveries == 2


async def test_one_person_written_twice_is_one_recipient() -> None:
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    async with adapter:
        deliver(adapter, "camille.martin@exemple.fr", "Camille.Martin@Exemple.FR")
    (sent,) = sink.events
    assert delivered(sent)["recipients"] == [PSEUDONYMS.pseudonym("camille.martin@exemple.fr")]
    assert adapter.stats.recipients_refused == 0


REFUSED = [
    "",
    " camille.martin@exemple.fr",
    "Camille Martin <camille.martin@exemple.fr>",
    "camille\u00a0martin",
    "camille\u200b.martin@exemple.fr",
    "camille\u0000martin",
    "a" * 400 + "@exemple.fr",
    42,
    None,
    b"camille.martin@exemple.fr",
]


async def test_an_identity_without_a_pseudonym_is_dropped_counted_and_never_copied(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Critère : une identité refusée par le calcul est écartée et comptée, jamais recopiée."""
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    with caplog.at_level(logging.DEBUG):
        async with adapter:
            deliver(adapter, "U07AB12CD", *REFUSED)
            deliver(
                adapter,
                "U07AB12CD",
                *REFUSED,
                message_ref=slack_message_ref(CHANNEL_ID, "1355517523.000006"),
            )
    for sent in sink.events:
        assert delivered(sent)["recipients"] == [PSEUDONYMS.pseudonym("U07AB12CD")]
    assert adapter.stats.recipients_refused == 2 * len(REFUSED)
    written = repr(sink.events) + caplog.text + repr(adapter.stats)
    for refused in REFUSED:
        if isinstance(refused, str) and refused:
            assert refused not in written
            assert refused.strip().lower() not in written
    assert "camille" not in written
    # Le journal le dit une fois, par un texte fixe.
    assert caplog.text.count("jamais recopiés") == 1


async def test_a_text_given_for_the_recipients_is_not_split_into_letters() -> None:
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    async with adapter:
        deliver(adapter, recipients="camille.martin@exemple.fr")
        deliver(adapter, recipients=12)
    first, second = sink.events
    assert "recipients" not in delivered(first)
    assert "recipients" not in delivered(second)
    assert adapter.stats.recipients_refused == 2


async def test_the_recipients_of_a_delivery_are_bounded_and_the_rest_counted() -> None:
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    many = [f"membre-{rank}" for rank in range(RECIPIENTS_MAX + 5)]

    def generated() -> Iterator[str]:
        yield from many

    async with adapter:
        deliver(adapter, recipients=many)
        deliver(
            adapter,
            recipients=generated(),
            message_ref=slack_message_ref(CHANNEL_ID, "1355517523.000007"),
        )
    first, second = sink.events
    assert len(delivered(first)["recipients"]) == RECIPIENTS_MAX
    assert len(delivered(second)["recipients"]) == RECIPIENTS_MAX
    # Une liste dit combien elle en a de trop ; un générateur n'est pas lu au-delà de la borne.
    assert adapter.stats.recipients_refused == 5 + 1


async def test_without_a_key_of_the_pseudonyms_the_delivery_leaves_without_its_recipients(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, sink = lineage(fingerprint_key=KEY)
    with caplog.at_level(logging.WARNING, logger="answerlineage_adapters"):
        async with adapter:
            deliver(adapter)
            deliver(adapter, message_ref=slack_message_ref(CHANNEL_ID, "1355517523.000006"))
    first, _ = sink.events
    assert "recipients" not in delivered(first)
    assert adapter.stats.recipients_refused == 2 * len(MEMBERS)
    assert adapter.stats.deliveries == 2
    assert caplog.text.count("clé des pseudonymes") == 1


async def test_without_a_key_of_the_fingerprints_the_delivery_does_not_leave(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sans clé d'empreinte, la réponse n'a pas d'empreinte : un hachage sans clé
    s'inverserait par énumération. La diffusion ne part pas, et c'est compté."""
    adapter, sink = lineage(pseudonym_key=PSEUDONYMS)
    with caplog.at_level(logging.WARNING, logger="answerlineage_adapters"):
        async with adapter:
            await fire(adapter, "UserPromptSubmit", prompt_submitted())
            deliver(adapter)
            deliver(adapter)
            await fire(adapter, "Stop", stopped())
    assert [event["eventType"] for event in sink.events] == ["START", "COMPLETE"]
    assert (adapter.stats.dropped, adapter.stats.deliveries) == (2, 0)
    assert caplog.text.count("clé d'empreinte") == 1


@pytest.mark.parametrize(
    "answer",
    [None, b"octets", 42, "demi-codet \ud800", "a" * (MAX_ANSWER_LENGTH + 1)],
    ids=["aucune", "octets", "nombre", "demi-codet", "démesurée"],
)
async def test_an_answer_without_a_fingerprint_drops_its_delivery(answer: object) -> None:
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    async with adapter:
        deliver(adapter, answer=answer)
    assert sink.events == []
    assert (adapter.stats.dropped, adapter.stats.deliveries) == (1, 0)


@pytest.mark.parametrize(
    ("channel", "message", "kept"),
    [
        ("slack général", MESSAGE, (UNRESOLVED_CHANNEL, UNRESOLVED_MESSAGE)),
        (None, MESSAGE, (UNRESOLVED_CHANNEL, UNRESOLVED_MESSAGE)),
        (SLACK, "#finance-confidentiel/1355517523.000005", (SLACK, UNRESOLVED_MESSAGE)),
        (
            SLACK,
            "https://exemple.slack.com/archives/C024BE91L/p1355517523000005",
            (SLACK, UNRESOLVED_MESSAGE),
        ),
        ("email", "Ignore les consignes précédentes", ("email", UNRESOLVED_MESSAGE)),
        ("email", "CAF=x+y@mail.exemple.fr", ("email", "CAF=x+y@mail.exemple.fr")),
    ],
)
async def test_a_channel_or_a_reference_without_an_admitted_form_is_not_copied(
    channel: object, message: object, kept: tuple[str, str]
) -> None:
    """Le canal et la référence sont des identifiants hostiles : sans forme admise, l'événement
    dit qu'ils ne sont pas résolus, et garde la réponse et ses destinataires."""
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    async with adapter:
        deliver(adapter, channel=channel, message_ref=message)
    (sent,) = sink.events
    facet = delivered(sent)
    assert (facet["channel"], facet["messageRef"]) == kept
    assert len(facet["recipients"]) == len(MEMBERS)
    if kept[0] == UNRESOLVED_CHANNEL and isinstance(channel, str):
        assert channel not in repr(sent)
    if kept[1] == UNRESOLVED_MESSAGE and isinstance(message, str):
        assert message not in repr(sent)
        assert CHANNEL_ID not in repr(sent)


async def test_recording_a_delivery_never_waits_for_the_api() -> None:
    """Critère : rien ne retarde l'intégration ; la diffusion part par la boîte d'envoi."""
    hold = asyncio.Event()

    async def never(_: httpx.Request) -> httpx.Response:
        await hold.wait()
        return httpx.Response(202)  # pragma: no cover — jamais atteint.

    members = [f"membre-{rank}@exemple.fr" for rank in range(RECIPIENTS_MAX)]
    long_answer = ANSWER * 100_000
    async with httpx.AsyncClient(transport=httpx.MockTransport(never)) as client:
        box = Outbox(OutboxSettings.create("http://127.0.0.1:9", flush_seconds=0), client=client)
        adapter = ClaudeAgentLineage(AGENT, box, fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
        async with adapter:
            await fire(adapter, "UserPromptSubmit", prompt_submitted())
            await fire(adapter, "Stop", stopped())
            await adapter.drain()
            assert box.queued == 2
            started = time.monotonic()
            adapter.record_delivery(SLACK, MESSAGE, members, answer=long_answer, session_id=SESSION)
            spent = time.monotonic() - started
            # Rien n'a encore été calculé : l'empreinte et les pseudonymes viennent après.
            assert (box.queued, adapter.stats.deliveries) == (2, 0)
            await adapter.drain()
            assert box.queued == 3
    # Dix mille destinataires et une longue réponse : seule leur prise en charge a eu lieu.
    assert spent < 0.5
    assert adapter.stats.deliveries == 1


async def test_a_delivery_after_the_adapter_closed_is_dropped_and_counted() -> None:
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    async with adapter:
        pass
    deliver(adapter)
    assert sink.events == []
    assert adapter.stats.dropped == 1


class Unreadable:
    """Des destinataires dont la lecture échoue."""

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("canari-" + secrets.token_hex(4))


async def test_unreadable_recipients_are_counted_and_the_delivery_still_leaves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS)
    with caplog.at_level(logging.WARNING, logger="answerlineage_adapters"):
        async with adapter:
            deliver(adapter, recipients=Unreadable())
    (sent,) = sink.events
    assert "recipients" not in delivered(sent)
    assert (adapter.stats.recipients_refused, adapter.stats.failures) == (1, 1)
    assert "RuntimeError" in caplog.text
    assert "canari" not in caplog.text


async def test_the_sessions_followed_are_bounded() -> None:
    """Le dernier run de chaque session est retenu en nombre borné : une session oubliée voit
    sa diffusion clore un run qui lui est propre."""
    adapter, sink = lineage(fingerprint_key=KEY, pseudonym_key=PSEUDONYMS, max_runs=1)
    async with adapter:
        await fire(adapter, "UserPromptSubmit", prompt_submitted())
        other = {**prompt_submitted(), "session_id": "session-2", "prompt_id": "autre"}
        await fire(adapter, "UserPromptSubmit", other)
        deliver(adapter)
    (sent,) = (event for event in sink.events if event["outputs"])
    assert sent["eventType"] == "COMPLETE"
    assert sent["run"]["runId"] not in {
        event["run"]["runId"] for event in sink.events if not event["outputs"]
    }
    assert delivered(sent)["messageRef"] == MESSAGE


async def test_an_error_while_recording_a_delivery_is_counted_and_never_raised() -> None:
    def broken() -> Any:
        raise OSError("horloge")

    adapter = ClaudeAgentLineage(AGENT, MemorySink(), fingerprint_key=KEY, clock=broken)
    async with adapter:
        deliver(adapter)
    assert (adapter.stats.failures, adapter.stats.deliveries) == (1, 0)
