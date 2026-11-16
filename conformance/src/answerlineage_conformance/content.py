"""Contenu : aucun canari ne ressort de l'émetteur.

L'intégrateur joue une demande avec trois canaris, dans le prompt, le résultat
d'outil et la réponse. Le témoin, l'échange tel que l'utilisateur et l'outil
l'ont vu, prouve qu'ils sont bien passés : sans lui, une demande qui n'a rien
traversé serait verte. Les canaris sont ensuite cherchés dans les événements,
clés comme valeurs, puis ligne par ligne dans les journaux et les métriques.

Un problème dit où est le canari, lequel, et sous quelle forme. Jamais la
valeur, ni la ligne, ni un extrait.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from answerlineage_conformance.canaries import Canary, Searcher
from answerlineage_conformance.findings import Finding
from answerlineage_conformance.pointers import Location
from answerlineage_conformance.sources import SourceFile, searchable_text, text_lines


@dataclass(frozen=True)
class Evidence:
    """Ce qu'une demande jouée avec les canaris a laissé derrière elle."""

    canaries: tuple[Canary, ...]
    witness: SourceFile
    logs: tuple[SourceFile, ...] = ()
    metrics: tuple[SourceFile, ...] = ()


def _texts(event: object) -> Iterator[tuple[Location, str]]:
    """Tous les textes d'un événement, avec leur emplacement : clés, valeurs et nombres.

    Le parcours tient sa propre pile : un événement très imbriqué ne l'arrête pas.
    """
    pending: list[tuple[Location, object]] = [((), event)]
    while pending:
        location, value = pending.pop()
        if isinstance(value, dict):
            for key, inside in value.items():
                yield (*location, key), key
                pending.append(((*location, key), inside))
        elif isinstance(value, list):
            pending.extend(((*location, index), inside) for index, inside in enumerate(value))
        elif isinstance(value, str):
            yield location, value
        elif isinstance(value, int | float) and not isinstance(value, bool):
            # Un tirage fait de chiffres seuls peut ressortir dans un nombre.
            yield location, repr(value)


def _in_events(events: Sequence[object], searcher: Searcher) -> Iterator[Finding]:
    for rank, event in enumerate(events):
        for location, text in _texts(event):
            for sighting in searcher.sightings(text):
                yield Finding(
                    "canary_in_events", rank, location, canary=sighting.role, form=sighting.form
                )


def _in_file(code: str, file: SourceFile, searcher: Searcher) -> Iterator[Finding]:
    for number, line in enumerate(text_lines(file), start=1):
        for sighting in searcher.sightings(line):
            yield Finding(
                code, file=file.path, line=number, canary=sighting.role, form=sighting.form
            )


def content(events: Sequence[object], evidence: Evidence, searcher: Searcher) -> list[Finding]:
    """Les canaris absents du témoin, et ceux qui ressortent de l'émetteur."""
    findings = [
        Finding("canary_missing_from_witness", file=evidence.witness.path, canary=role)
        for role in searcher.missing(searchable_text(evidence.witness))
    ]
    findings.extend(_in_events(events, searcher))
    for file in evidence.logs:
        findings.extend(_in_file("canary_in_logs", file, searcher))
    for file in evidence.metrics:
        findings.extend(_in_file("canary_in_metrics", file, searcher))
    return findings
