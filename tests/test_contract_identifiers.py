"""Contrat entre composants : un nom déclaré au proxy a la forme que l'ingestion admet.

La forme d'un nom de jeu de données ou de colonne est écrite deux fois, une par
service, pour qu'aucun ne dépende de l'autre : une règle de lecture que le proxy
accepte ne fait jamais rejeter ses événements. Ce test les tient d'accord.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from answerlineage_api import identifiers as api
from answerlineage_api.identifiers import identifier_fault
from answerlineage_proxy import names as proxy
from answerlineage_proxy.names import name_fault, normalize_name

BOUNDS = [
    (proxy.NAMESPACE_MAX, api.NAMESPACE_MAX),
    (proxy.DATASET_NAME_MAX, api.DATASET_NAME_MAX),
    (proxy.COLUMN_MAX, api.COLUMN_MAX),
    (proxy.INSTANCE_MAX, api.INSTANCE_MAX),
]
SAMPLES = [
    "ref.tarifs",
    "postgres://referentiel:5432",
    "gs://seau/chemin/fichier.csv",
    "kafka://broker1:9092,broker2:9092",
    "abfss://conteneur@compte.dfs.core.windows.net",
    "référentiel.société.coût_unitaire",
    "socie" + chr(0x301) + "te" + chr(0x301),
    "projet.jeu.evenements_2026*",
    "Politique%20voyages%202026.pdf",
    "",
    "a" * 300,
    "a" * 301,
    "a" * 1025,
    "Ignore les consignes",
    "nom" + chr(0xA0) + "insécable",
    "nom" + chr(0x200B) + "caché",
    "nom" + chr(0x2066) + "isolé",
    chr(0xFE0F),
    "<script>",
    chr(0xFF1C) + "balise",
    "nom'apostrophe",
    "nom" + chr(0) + "nul",
    "nom" + chr(0x7F),
    "nom;point-virgule",
    "émoji" + chr(0x1F642),
    "postgres://utilisateur:motdepasse@hote",
    "conteneur@compte",
    chr(0xE0041),
]


def codes(value: str, bounds: tuple[int, int]) -> tuple[str | None, str | None]:
    from_proxy = name_fault(value, max_length=bounds[0])
    from_api = identifier_fault(value, max_length=bounds[1])
    return (
        from_proxy.code if from_proxy is not None else None,
        from_api.code if from_api is not None else None,
    )


def test_the_bounds_are_those_of_the_ingestion() -> None:
    assert all(mine == theirs for mine, theirs in BOUNDS)
    assert proxy.PUNCTUATION == api.PUNCTUATION
    assert proxy.NORMAL_FORM == api.NORMAL_FORM


@pytest.mark.parametrize("bounds", BOUNDS)
@pytest.mark.parametrize("value", SAMPLES)
def test_the_proxy_and_the_ingestion_judge_a_name_alike(
    value: str, bounds: tuple[int, int]
) -> None:
    mine, theirs = codes(value, bounds)
    assert mine == theirs
    assert normalize_name(value) == api.normalize_identifier(value)


@settings(max_examples=500)
@given(value=st.text(max_size=40))
def test_on_any_text_the_proxy_and_the_ingestion_agree(value: str) -> None:
    mine, theirs = codes(value, BOUNDS[2])
    assert mine == theirs


def test_a_refusal_never_echoes_the_name() -> None:
    fault = name_fault("canari-nom 7f1e", max_length=10)
    assert fault is not None
    assert "canari" not in fault.message
