"""Un canari placé dans le prompt, le résultat d'outil et la réponse ne ressort nulle part.

Le parcours complet : un agent joué par un script reçoit un prompt, appelle un
outil à travers le vrai proxy, répond ; ses événements et ceux du proxy vont à
l'API d'ingestion, un worker les projette dans le graphe, l'API de lecture les
rend. Les trois canaris sont ensuite cherchés, sous toutes les formes que le
kit connaît, dans les événements, la base entière, les réponses de l'API, le
journal Python au niveau DEBUG, les sorties standard et les compteurs du proxy.

Le kit rend le même verdict sur les fichiers qu'un intégrateur garderait de ce
parcours : c'est la commande qu'il lancerait.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_conformance import cli
from answerlineage_conformance.canaries import Searcher, canaries_document, new_canaries
from answerlineage_workers.queue import process_batch
from tests.test_conformance_emitters import CHANNEL, MESSAGE_REF, RECEIVED_AT, T0, TABLE, play
from tests.test_no_token_anywhere import dump

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_three_canaries_cross_the_whole_path_and_come_out_nowhere(
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    canaries = new_canaries()
    searcher = Searcher(canaries)
    in_prompt, in_result, in_answer = (canary.value for canary in canaries)
    prompt = f"Quel est le taux de change du jour ? {in_prompt}"
    tool_result = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": f"taux : 1,08 ({in_result})"}]},
        }
    ).encode()
    answer = f"Le taux de change du jour est de 1,08. {in_answer}"
    responses: list[str] = []

    with caplog.at_level(logging.DEBUG):
        played = await play(in_prompt, tool_result, answer)

        transport = httpx.ASGITransport(app=create_app(engine))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as api:
            ingested = await api.post("/api/v1/lineage/batch", json=played.events)
            assert ingested.json() == {"accepted": 4, "duplicates": 0, "rejected": []}
            while not (await process_batch(engine)).empty:
                pass
            found = await api.get(
                "/api/v1/answers", params={"channel": CHANNEL, "message_ref": MESSAGE_REF}
            )
            (delivered,) = found.json()["answers"]
            sources = await api.get(f"/api/v1/answers/{delivered['id']}/sources")
            window = {"from": T0.isoformat(), "to": RECEIVED_AT.isoformat()}
            impact = await api.get(
                "/api/v1/impact", params={"namespace": "bigquery", "name": TABLE, **window}
            )
            others = [
                await api.get(path)
                for path in (
                    "/api/v1/graph",
                    "/api/v1/datasets",
                    "/api/v1/exposure",
                    "/api/v1/ingestion/duplicates",
                    "/api/v1/ingestion/dead-letters",
                )
            ]
            responses.extend(
                response.text for response in (ingested, found, sources, impact, *others)
            )
            assert {response.status_code for response in (found, sources, impact, *others)} == {200}
        stored = await dump(engine)
    output = capfd.readouterr()

    # Le témoin : l'échange tel que l'utilisateur et l'outil l'ont vu. Les trois canaris sont
    # bien passés, le prompt jusqu'au serveur MCP, le résultat jusqu'à l'agent.
    assert in_prompt.encode() in played.received.body
    assert played.result == tool_result
    witness = "\n".join(
        [
            f"utilisateur : {prompt}",
            f"appel reçu par l'outil : {played.received.body.decode()}",
            f"résultat reçu par l'agent : {played.result.decode()}",
            f"réponse reçue par l'utilisateur : {answer}",
        ]
    )
    assert searcher.missing(witness) == []

    # Le parcours a bien eu lieu : l'appel, la table lue et la réponse sont dans le graphe.
    assert '"kind":"tool"' in stored
    assert TABLE in stored
    counts = impact.json()["counts"]
    assert (counts["answers"], counts["recipients"]) == (1, 1)
    assert sources.json()["datasets"][0]["columns"] == ["devise", "taux"]

    # Ce que le proxy garde du contenu : des empreintes à clé, une taille, et la requête
    # sans ses littéraux. Le prompt était dans l'argument, le canari du résultat dans le corps.
    (from_proxy,) = played.from_proxy
    described = from_proxy["run"]["facets"]["answerlineage_toolCall"]
    assert described["argumentsFingerprint"].startswith("hmac-sha256:")
    assert described["resultFingerprint"].startswith("hmac-sha256:")
    assert described["resultSize"] == len(tool_result)
    assert from_proxy["job"]["facets"]["sql"]["query"].endswith("WHERE devise = ?")

    # Rien, nulle part, sous aucune forme.
    events = json.dumps(played.events, ensure_ascii=False)
    journal = "\n".join([caplog.text, output.out, output.err])
    counters = repr(played.stats)
    places = {
        "les événements": events,
        "la base": stored,
        "les réponses de l'API": "\n".join(responses),
        "le journal": caplog.text,
        "la sortie standard": output.out,
        "la sortie d'erreur": output.err,
        "les compteurs du proxy": counters,
    }
    seen = {name: searcher.sightings(place) for name, place in places.items()}
    assert {name: sightings for name, sightings in seen.items() if sightings} == {}
    assert played.stats.events_emitted == 1

    # Le verdict du kit, sur les fichiers qu'un intégrateur garderait de ce parcours.
    files = {
        "evenements.json": events,
        "canaris.json": json.dumps(canaries_document(canaries)),
        "echange.txt": witness,
        "journal.log": journal,
        "compteurs.txt": counters,
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    arguments = [
        "check",
        str(tmp_path / "evenements.json"),
        "--canaries",
        str(tmp_path / "canaris.json"),
        "--witness",
        str(tmp_path / "echange.txt"),
        "--logs",
        str(tmp_path / "journal.log"),
        "--metrics",
        str(tmp_path / "compteurs.txt"),
        "--received-at",
        RECEIVED_AT.isoformat(),
        "--format",
        "json",
    ]
    status = cli.main(arguments)
    report = json.loads(capfd.readouterr().out)
    assert report["problems"] == []
    assert (status, report["verdict"], report["profile"]) == (0, "conformant", "adapter")
    assert {control["code"] for control in report["controls"] if control["group"] == "content"} == {
        "canary_missing_from_witness",
        "canary_in_events",
        "canary_in_logs",
        "canary_in_metrics",
    }
