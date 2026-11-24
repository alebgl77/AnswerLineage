"""Le jugement d'une capture : les contrôles exécutés et les problèmes relevés.

``judge`` est une fonction pure des événements, du profil, de l'heure de
référence et des preuves de contenu : les mêmes entrées donnent le même
jugement, problèmes dans le même ordre.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from answerlineage_conformance.canaries import Searcher
from answerlineage_conformance.content import Evidence, content
from answerlineage_conformance.context import context, runs
from answerlineage_conformance.controls import BY_CODE, CONTROLS, GROUPS, Control, Group, Profile
from answerlineage_conformance.findings import Finding
from answerlineage_conformance.pointers import Masker
from answerlineage_conformance.projection import dropped, records
from answerlineage_conformance.shape import facets, ingestion

# Un émetteur fautif peut l'être à chaque ligne : la liste rendue est bornée, pas le compte.
MAX_PROBLEMS: Final = 1000


@dataclass(frozen=True)
class Problem:
    """Un problème du rapport : où, quoi, jamais la valeur reçue."""

    code: str
    group: Group
    message: str
    event: int | None = None
    pointer: str | None = None
    file: str | None = None
    line: int | None = None
    reason: str | None = None
    canary: str | None = None
    form: str | None = None


@dataclass(frozen=True)
class Judgement:
    profile: Profile
    events: int
    runs: int
    # Les contrôles exécutés, dans l'ordre du catalogue.
    controls: tuple[Control, ...]
    # Les problèmes listés, ``MAX_PROBLEMS`` au plus, dans un ordre fixe.
    problems: tuple[Problem, ...]
    # Le compte de tous les problèmes, par code de contrôle.
    counts: Mapping[str, int]

    @property
    def problem_count(self) -> int:
        return sum(self.counts.values())

    @property
    def truncated(self) -> bool:
        return self.problem_count > len(self.problems)

    @property
    def conformant(self) -> bool:
        return self.problem_count == 0


def _executed(profile: Profile, evidence: Evidence | None) -> tuple[Control, ...]:
    """Les contrôles du profil ; ceux du contenu, pour les fichiers réellement fournis."""
    skipped = set()
    if evidence is None:
        skipped = {control.code for control in CONTROLS if control.group == "content"}
    else:
        if not evidence.logs:
            skipped.add("canary_in_logs")
        if not evidence.metrics:
            skipped.add("canary_in_metrics")
    return tuple(
        control
        for control in CONTROLS
        if profile in control.profiles and control.code not in skipped
    )


def _order(problem: Problem) -> tuple[Any, ...]:
    """Par groupe ; dans un groupe, les événements, puis les fichiers, puis la capture entière."""
    if problem.event is not None:
        place: tuple[Any, ...] = (0, problem.event, problem.pointer or "")
    elif problem.file is not None:
        place = (1, problem.file, problem.line or 0)
    else:
        place = (2,)
    detail = (problem.code, problem.reason or "", problem.canary or "", problem.form or "")
    return (GROUPS.index(problem.group), *place, *detail)


def judge(
    events: Sequence[object],
    *,
    profile: Profile,
    received_at: datetime,
    evidence: Evidence | None = None,
) -> Judgement:
    """Juge une capture.

    ``received_at`` est l'heure de référence : l'ingestion borne l'heure d'un
    événement par celle de sa réception. Sans ``evidence``, le contenu n'est
    pas jugé, et aucun contrôle de contenu n'est dit exécuté.
    """
    rejected, accepted = ingestion(events, received_at)
    failed, projected = records(accepted)
    findings: list[Finding] = [
        *rejected,
        *facets(events),
        *context(events, projected, profile),
        *failed,
        *dropped(accepted, projected),
    ]
    searcher = None
    if evidence is not None:
        searcher = Searcher(evidence.canaries)
        findings.extend(content(events, evidence, searcher))

    masker = Masker(searcher)
    problems = sorted(
        {
            Problem(
                code=finding.code,
                group=BY_CODE[finding.code].group,
                message=BY_CODE[finding.code].message,
                event=finding.event,
                pointer=None if finding.location is None else masker.pointer(finding.location),
                file=finding.file,
                line=finding.line,
                reason=finding.reason,
                canary=finding.canary,
                form=finding.form,
            )
            for finding in findings
        },
        key=_order,
    )
    return Judgement(
        profile=profile,
        events=len(events),
        runs=len(runs(events)),
        controls=_executed(profile, evidence),
        problems=tuple(problems[:MAX_PROBLEMS]),
        counts=dict(Counter(problem.code for problem in problems)),
    )
