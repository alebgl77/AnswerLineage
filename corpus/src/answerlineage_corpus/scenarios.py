"""Corpus de vérité : des scénarios d'incident dont on connaît les réponses attendues.

Un scénario décrit quelques runs d'agents, un incident de données et sa
vérité : les réponses touchées, les réponses écartées et celles qui n'ont rien
à voir. La vérité est écrite à la main, dans le fichier du scénario. Elle sert
à mesurer le moteur d'impact comme on mesure un modèle : rappel sur les
réponses touchées, précision sur les réponses écartées.

Un scénario peut aussi tenir l'ordre d'exposition attendu de ses jeux de
données, et de leurs colonnes, à une heure de référence : la vérité du score
d'exposition, écrite à la main elle aussi.

Le contrôle du corpus refuse un scénario sans vérité, une vérité qui oublie une
réponse, et une vérité que la description du scénario contredit.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from answerlineage_corpus.enterprise import _Dice, _run_id
from answerlineage_corpus.events import (
    agent_event,
    answer_delivered,
    dataset_read,
    dataset_written,
    in_time_order,
    tool_event,
)

# Change dès qu'un scénario ou sa vérité change : deux mesures ne se comparent
# que sur la même version du corpus.
CORPUS_VERSION = "2"
_DIGESTS = {
    "1": "793e14c268cb1f1e9ebee171a9a8798ceeb467d67b27a9d3792308d139bdfa0e",
    "2": "bbb83390c22d1dd9dd5c721f20278a8876bf4309bc436287d3ac36405a7183c7",
}

Level = Literal["table", "columns", "subset", "keys"]
Confidence = Literal["certaine", "probable", "possible"]
Reach = Literal["direct", "indirect"]

LEVELS: tuple[Level, ...] = ("table", "columns", "subset", "keys")
CONFIDENCES: tuple[Confidence, ...] = ("certaine", "probable", "possible")

_DICE = _Dice(0)


class CorpusError(Exception):
    """Le corpus est incohérent : ``problems`` dit pourquoi, scénario par scénario."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = list(problems)


@dataclass(frozen=True)
class Dataset:
    namespace: str
    name: str
    kind: str = "table"


@dataclass(frozen=True)
class Read:
    dataset: Dataset
    columns: tuple[str, ...] = ()
    version: str | None = None
    # Sous-ensemble lu, par colonne de partition ; ``None`` quand il est inconnu.
    subset: Mapping[str, tuple[str, ...]] | None = field(default=None, hash=False)
    # Clés lues ; ``None`` quand elles sont inconnues.
    keys: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Call:
    run_id: UUID
    server: str
    tool: str
    at: datetime
    reads: tuple[Read, ...]


@dataclass(frozen=True)
class Answer:
    key: str
    fingerprint: str
    at: datetime
    channel: str
    message_ref: str
    recipients: tuple[str, ...]


@dataclass(frozen=True)
class Run:
    key: str
    run_id: UUID
    agent: tuple[str, str]
    start: datetime
    end: datetime
    parent: str | None
    failed: bool
    calls: tuple[Call, ...]
    writes: tuple[Dataset, ...]
    answer: Answer | None


@dataclass(frozen=True)
class Scope:
    """Périmètre de l'incident : un jeu de données, une fenêtre, et ce qui la précise."""

    dataset: Dataset
    start: datetime
    end: datetime
    columns: tuple[str, ...] = ()
    subset: Mapping[str, tuple[str, ...]] | None = field(default=None, hash=False)
    keys: tuple[str, ...] | None = None
    versions: tuple[str, ...] = ()

    @property
    def level(self) -> Level:
        if self.keys is not None:
            return "keys"
        if self.subset is not None:
            return "subset"
        return "columns" if self.columns else "table"


@dataclass(frozen=True)
class Expected:
    """Une réponse touchée, avec la confiance attendue et le chemin qui y mène."""

    answer: Answer
    confidence: Confidence
    # « indirect » : la réponse dépend d'une sortie d'un run touché, pas de la table elle-même.
    path: Reach


@dataclass(frozen=True)
class Exposure:
    """L'ordre d'exposition attendu à une heure de référence, le plus exposé d'abord."""

    at: datetime
    datasets: tuple[Dataset, ...]
    # Pour un jeu dont des lectures nomment des colonnes : l'ordre de ses colonnes.
    columns: tuple[tuple[Dataset, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    level: Level
    runs: tuple[Run, ...]
    incident: Scope
    touched: tuple[Expected, ...]
    # Réponses qui ont lu la table pendant la fenêtre sans dépendre de ce qui est faux.
    discarded: tuple[tuple[Answer, str], ...]
    untouched: tuple[Answer, ...]
    exposure: Exposure | None = None

    @property
    def answers(self) -> tuple[Answer, ...]:
        return tuple(run.answer for run in self.runs if run.answer is not None)

    def expected(self, path: Reach) -> tuple[Answer, ...]:
        """Réponses touchées par ce chemin."""
        return tuple(item.answer for item in self.touched if item.path == path)

    def events(self) -> list[dict[str, Any]]:
        """Les événements OpenLineage du scénario, dans l'ordre du temps."""
        by_key = {run.key: run for run in self.runs}
        events: list[dict[str, Any]] = []
        for run in self.runs:
            job = {"namespace": run.agent[0], "name": run.agent[1]}
            parent = None
            if run.parent is not None:
                above = by_key[run.parent]
                parent = (above.run_id, {"namespace": above.agent[0], "name": above.agent[1]})
            events.append(agent_event("START", run.start, run.run_id, job, parent=parent))
            events.extend(
                tool_event(
                    call.at,
                    call.run_id,
                    server=call.server,
                    tool=call.tool,
                    parent=(run.run_id, job),
                    inputs=[
                        dataset_read(
                            read.dataset.namespace,
                            read.dataset.name,
                            columns=read.columns,
                            version=read.version,
                            kind=read.dataset.kind,
                        )
                        for read in call.reads
                    ],
                )
                for call in run.calls
            )
            outputs = [
                dataset_written(written.namespace, written.name, kind=written.kind)
                for written in run.writes
            ]
            if run.answer is not None:
                outputs.append(
                    answer_delivered(
                        job,
                        fingerprint=run.answer.fingerprint,
                        channel=run.answer.channel,
                        message_ref=run.answer.message_ref,
                        recipients=run.answer.recipients,
                    )
                )
            events.append(
                agent_event(
                    "FAIL" if run.failed else "COMPLETE",
                    run.end,
                    run.run_id,
                    job,
                    parent=parent,
                    outputs=outputs,
                )
            )
        return in_time_order(events)


@dataclass(frozen=True)
class Corpus:
    version: str
    scenarios: tuple[Scenario, ...]

    def __iter__(self) -> Iterator[Scenario]:
        return iter(self.scenarios)


def _instant(value: object) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("une heure sans fuseau horaire")
    return moment


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("une liste de textes est attendue")
    return tuple(value)


def _subset(value: object) -> Mapping[str, tuple[str, ...]] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("un sous-ensemble se décrit par colonne de partition")
    return {str(column): _texts(values) for column, values in value.items()}


def _keys(value: object) -> tuple[str, ...] | None:
    return None if value is None else _texts(value)


def _exposure(value: object, datasets: Mapping[str, Dataset]) -> Exposure | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("un ordre d'exposition se décrit par un objet")
    columns = value.get("columns", {})
    if not isinstance(columns, dict):
        raise TypeError("les colonnes se décrivent par jeu de données")
    return Exposure(
        at=_instant(value["at"]),
        datasets=tuple(datasets[alias] for alias in _texts(value["datasets"])),
        columns=tuple((datasets[alias], _texts(names)) for alias, names in columns.items()),
    )


def _build(name: str, raw: Mapping[str, Any]) -> Scenario:
    """Construit le scénario. Une forme invalide lève KeyError, TypeError ou ValueError."""
    datasets = {alias: Dataset(**described) for alias, described in raw["datasets"].items()}
    agents = {alias: (a["namespace"], a["name"]) for alias, a in raw["agents"].items()}
    tools = {alias: (t["server"], t["name"]) for alias, t in raw["tools"].items()}

    runs: list[Run] = []
    for described in raw["runs"]:
        key = str(described["id"])
        start = _instant(described["start"])
        calls = tuple(
            Call(
                run_id=_run_id(_DICE, _instant(call["at"]), name, key, rank),
                server=tools[call["tool"]][0],
                tool=tools[call["tool"]][1],
                at=_instant(call["at"]),
                reads=tuple(
                    Read(
                        dataset=datasets[read["dataset"]],
                        columns=_texts(read.get("columns", [])),
                        version=read.get("version"),
                        subset=_subset(read.get("subset")),
                        keys=_keys(read.get("keys")),
                    )
                    for read in call["reads"]
                ),
            )
            for rank, call in enumerate(described.get("calls", []))
        )
        answer = None
        if "answer" in described:
            given = described["answer"]
            answer = Answer(
                key=str(given["id"]),
                fingerprint="sha256:" + _DICE.hex(64, "answer", name, given["id"]),
                at=_instant(given["at"]),
                channel=str(given["channel"]),
                message_ref=_DICE.hex(16, "message", name, given["id"]),
                recipients=tuple(
                    sorted(
                        "hmac-sha256:" + _DICE.hex(64, "recipient", name, alias)
                        for alias in _texts(given["recipients"])
                    )
                ),
            )
        moments = [start, *(call.at for call in calls)]
        if answer is not None:
            moments.append(answer.at)
        runs.append(
            Run(
                key=key,
                run_id=_run_id(_DICE, start, name, key),
                agent=agents[described["agent"]],
                start=start,
                end=_instant(described["end"]) if "end" in described else max(moments),
                parent=described.get("parent"),
                failed=bool(described.get("failed", False)),
                calls=calls,
                writes=tuple(datasets[alias] for alias in described.get("writes", [])),
                answer=answer,
            )
        )

    scope = raw["incident"]
    incident = Scope(
        dataset=datasets[scope["dataset"]],
        start=_instant(scope["from"]),
        end=_instant(scope["to"]),
        columns=_texts(scope.get("columns", [])),
        subset=_subset(scope.get("subset")),
        keys=_keys(scope.get("keys")),
        versions=_texts(scope.get("versions", [])),
    )

    answers = {run.answer.key: run.answer for run in runs if run.answer is not None}
    truth = raw["truth"]
    return Scenario(
        name=name,
        title=str(raw["title"]),
        level=cast(Level, raw["level"]),
        runs=tuple(runs),
        incident=incident,
        touched=tuple(
            Expected(
                answer=answers[item["answer"]],
                confidence=item["confidence"],
                path=item.get("path", "direct"),
            )
            for item in truth["touched"]
        ),
        discarded=tuple(
            (answers[item["answer"]], str(item["reason"])) for item in truth["discarded"]
        ),
        untouched=tuple(answers[key] for key in truth["untouched"]),
        exposure=_exposure(truth.get("exposure"), datasets),
    )


def _subtree(scenario: Scenario, root: Run) -> list[Run]:
    """Le run et tous ceux qu'il a délégués, à toute profondeur."""
    found = [root]
    for run in found:
        found.extend(other for other in scenario.runs if other.parent == run.key)
    return found


def _reaches(scenario: Scenario) -> tuple[set[str], set[str]]:
    """Runs touchés directement, puis indirectement, d'après la seule description.

    Directement : un run de son arbre a lu la table pendant la fenêtre.
    Indirectement : un run de son arbre a lu une sortie d'un run touché, après
    qu'elle a été écrite. La propagation ne remonte jamais le temps.
    """
    incident = scenario.incident
    direct = {
        run.key
        for run in scenario.runs
        if any(
            read.dataset == incident.dataset and incident.start <= call.at < incident.end
            for below in _subtree(scenario, run)
            for call in below.calls
            for read in call.reads
        )
    }
    written: dict[Dataset, datetime] = {}
    indirect: set[str] = set()
    changed = True
    while changed:
        changed = False
        for run in scenario.runs:
            if run.key not in direct and run.key not in indirect:
                continue
            for below in _subtree(scenario, run):
                for dataset in below.writes:
                    if dataset not in written or below.end < written[dataset]:
                        written[dataset] = below.end
                        changed = True
        for run in scenario.runs:
            if run.key in direct or run.key in indirect:
                continue
            if any(
                read.dataset in written and call.at >= written[read.dataset]
                for below in _subtree(scenario, run)
                for call in below.calls
                for read in call.reads
            ):
                indirect.add(run.key)
                changed = True
    return direct, indirect


def _inconsistencies(scenario: Scenario) -> list[str]:
    problems: list[str] = []
    if scenario.level not in LEVELS:
        problems.append(f"niveau inconnu « {scenario.level} »")
    elif scenario.level != scenario.incident.level:
        problems.append(
            f"niveau « {scenario.level} » déclaré, mais l'incident est décrit au niveau "
            f"« {scenario.incident.level} »"
        )
    if scenario.incident.start >= scenario.incident.end:
        problems.append("la fenêtre de l'incident est vide")
    keys = [run.key for run in scenario.runs]
    if len(set(keys)) != len(keys):
        problems.append("deux runs portent le même identifiant")
    for run in scenario.runs:
        if run.parent is not None and run.parent not in keys:
            problems.append(f"le run « {run.key} » désigne un parent inconnu")
        if run.failed and run.answer is not None:
            problems.append(f"le run « {run.key} » a échoué et ne peut pas avoir répondu")

    classified = (
        [item.answer.key for item in scenario.touched]
        + [answer.key for answer, _ in scenario.discarded]
        + [answer.key for answer in scenario.untouched]
    )
    for answer in scenario.answers:
        if classified.count(answer.key) == 0:
            problems.append(f"la réponse « {answer.key} » n'a pas de vérité")
        elif classified.count(answer.key) > 1:
            problems.append(f"la réponse « {answer.key} » est classée plusieurs fois")

    direct, indirect = _reaches(scenario)
    run_of = {run.answer.key: run.key for run in scenario.runs if run.answer is not None}
    for item in scenario.touched:
        key = item.answer.key
        if item.confidence not in CONFIDENCES:
            problems.append(f"confiance inconnue pour la réponse « {key} »")
        if item.path == "direct" and run_of[key] not in direct:
            problems.append(
                f"la réponse « {key} » est dite touchée, mais rien n'a lu la table "
                "pendant la fenêtre"
            )
        elif item.path == "indirect":
            if item.confidence == "certaine":
                problems.append(f"la réponse « {key} » est indirecte : au plus probable")
            if run_of[key] in direct:
                problems.append(f"la réponse « {key} » a lu la table : son chemin est direct")
            elif run_of[key] not in indirect:
                problems.append(
                    f"la réponse « {key} » est dite touchée indirectement, mais aucune sortie "
                    "d'un run touché ne la précède"
                )
        elif item.path not in ("direct", "indirect"):
            problems.append(f"chemin inconnu pour la réponse « {key} »")
    for answer, reason in scenario.discarded:
        if run_of[answer.key] not in direct:
            problems.append(
                f"la réponse « {answer.key} » est dite écartée, mais elle n'a pas lu la table "
                "pendant la fenêtre : elle n'est pas touchée"
            )
        if not reason.strip():
            problems.append(f"la réponse « {answer.key} » est écartée sans motif")
    for answer in scenario.untouched:
        if run_of[answer.key] in direct or run_of[answer.key] in indirect:
            problems.append(
                f"la réponse « {answer.key} » est dite hors d'atteinte, mais la description du "
                "scénario la relie à l'incident"
            )
    return problems


# La règle du score d'exposition, écrite ici sans passer par le produit : chaque
# réponse pèse 1 plus un par destinataire, moitié moins tous les 30 jours.
_HALF_LIFE = timedelta(days=30)


def _weight(answer: Answer, at: datetime) -> float:
    age = max(at - answer.at, timedelta(0))
    return math.pow(2.0, -(age / _HALF_LIFE)) * (1 + len(set(answer.recipients)))


def _exposed(
    scenario: Scenario, at: datetime
) -> tuple[dict[Dataset, float], dict[Dataset, dict[str, float]]]:
    """Le score de chaque jeu de données dont une réponse dépend, et de ses colonnes nommées.

    Une réponse dépend de ce que son run et ses runs enfants ont lu. Une lecture
    de la table entière compte pour chaque colonne qu'une autre lecture nomme.
    """
    weights: dict[Dataset, list[float]] = {}
    whole: dict[Dataset, list[float]] = {}
    named: dict[Dataset, dict[str, list[float]]] = {}
    for run in scenario.runs:
        if run.answer is None:
            continue
        weight = _weight(run.answer, at)
        reads = [
            read for below in _subtree(scenario, run) for call in below.calls for read in call.reads
        ]
        read_whole = {read.dataset for read in reads if not read.columns}
        for dataset in {read.dataset for read in reads}:
            weights.setdefault(dataset, []).append(weight)
            if dataset in read_whole:
                whole.setdefault(dataset, []).append(weight)
        for dataset, column in {(read.dataset, c) for read in reads for c in read.columns}:
            counted = named.setdefault(dataset, {}).setdefault(column, [])
            if dataset not in read_whole:
                counted.append(weight)
    scores = {dataset: math.fsum(found) for dataset, found in weights.items()}
    columns = {
        dataset: {
            column: math.fsum([*whole.get(dataset, []), *found]) for column, found in by.items()
        }
        for dataset, by in named.items()
    }
    return scores, columns


def _ranked[K](scores: Mapping[K, float], natural: Callable[[K], tuple[str, ...]]) -> list[K]:
    """Le plus exposé d'abord, au millionième ; à égalité, par identifiant naturel."""
    return sorted(scores, key=lambda key: (-round(scores[key] * 1_000_000), natural(key)))


def _exposure_inconsistencies(scenario: Scenario) -> list[str]:
    expected = scenario.exposure
    if expected is None:
        return []
    problems: list[str] = []
    scores, columns = _exposed(scenario, expected.at)
    listed = list(expected.datasets)
    if len(set(listed)) != len(listed):
        problems.append("un jeu de données figure deux fois dans l'ordre d'exposition")
    elif set(listed) != set(scores):
        problems.append(
            "l'ordre d'exposition ne nomme pas exactement les jeux de données dont une réponse "
            "dépend"
        )
    elif listed != _ranked(scores, lambda dataset: (dataset.namespace, dataset.name)):
        problems.append("l'ordre d'exposition des jeux de données contredit la description")
    given = dict(expected.columns)
    for dataset in sorted(set(given) | set(columns), key=lambda found: found.name):
        names = list(given.get(dataset, ()))
        found = columns.get(dataset, {})
        if sorted(names) != sorted(found):
            problems.append(
                f"les colonnes attendues de « {dataset.name} » ne sont pas celles que ses "
                "lectures nomment"
            )
        elif names != _ranked(found, lambda column: (column,)):
            problems.append(
                f"l'ordre d'exposition des colonnes de « {dataset.name} » contredit la description"
            )
    return problems


def check_scenario(name: str, raw: object) -> list[str]:
    """Problèmes d'un scénario ; la liste est vide quand il est cohérent."""
    if not isinstance(raw, dict):
        return [f"{name} : un scénario est un objet JSON"]
    if "truth" not in raw:
        return [f"{name} : aucune vérité associée"]
    try:
        scenario = _build(name, raw)
    except (KeyError, TypeError, ValueError) as error:
        return [f"{name} : forme invalide ({type(error).__name__} : {error})"]
    problems = _inconsistencies(scenario) + _exposure_inconsistencies(scenario)
    return [f"{name} : {problem}" for problem in problems]


def _coverage(scenarios: Sequence[Scenario]) -> list[str]:
    problems = [
        f"aucun scénario au niveau « {level} »"
        for level in LEVELS
        if not any(scenario.level == level for scenario in scenarios)
    ]
    if not any(scenario.expected("indirect") for scenario in scenarios):
        problems.append("aucun scénario de transitivité")
    if not any(scenario.discarded for scenario in scenarios):
        problems.append("aucun scénario avec une réponse écartée")
    if not any(scenario.exposure is not None for scenario in scenarios):
        problems.append("aucun scénario avec un ordre d'exposition")
    return problems


def scenario_files(directory: Path | None = None) -> dict[str, Any]:
    """Les scénarios tels qu'ils sont écrits, par nom ; ceux du paquet par défaut."""
    if directory is not None:
        files = {path.stem: path.read_text(encoding="utf-8") for path in directory.glob("*.json")}
    else:
        package = resources.files("answerlineage_corpus") / "scenario_data"
        files = {
            entry.name.removesuffix(".json"): entry.read_text(encoding="utf-8")
            for entry in package.iterdir()
            if entry.name.endswith(".json")
        }
    return {name: json.loads(files[name]) for name in sorted(files)}


def check_corpus(directory: Path | None = None) -> list[str]:
    """Problèmes du corpus : ceux de chaque scénario, puis ce que le corpus ne couvre pas."""
    try:
        raw = scenario_files(directory)
    except (OSError, ValueError) as error:
        return [f"corpus illisible ({type(error).__name__})"]
    problems = [problem for name, item in raw.items() for problem in check_scenario(name, item)]
    if problems:
        return problems
    return _coverage([_build(name, item) for name, item in raw.items()])


def load_corpus(directory: Path | None = None) -> Corpus:
    """Charge le corpus, après l'avoir contrôlé."""
    problems = check_corpus(directory)
    if problems:
        raise CorpusError(problems)
    raw = scenario_files(directory)
    return Corpus(
        version=CORPUS_VERSION,
        scenarios=tuple(_build(name, item) for name, item in raw.items()),
    )


def corpus_digest(directory: Path | None = None) -> str:
    """Empreinte du contenu du corpus, indépendante de la mise en forme des fichiers."""
    canonical = json.dumps(scenario_files(directory), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def recorded_digest(version: str = CORPUS_VERSION) -> str:
    """Empreinte enregistrée pour cette version du corpus."""
    return _DIGESTS[version]
