"""Contrat entre composants : le job d'un agent que l'adaptateur accepte, l'ingestion l'admet.

L'adaptateur contrôle l'espace de noms et le nom de son agent au réglage, sans
dépendre de l'API : un nom qu'il laisse passer et que l'ingestion refuse ferait
rejeter tous les événements de l'agent, sans que personne ne le voie au démarrage.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from answerlineage_adapters.settings import AgentJob
from answerlineage_api import identifiers as api
from answerlineage_api.identifiers import identifier_fault
from answerlineage_emitter.config import ConfigurationError

SAMPLES = [
    "agent://finance",
    "agent://u:p@hote",
    "agent://utilisateur:motdepasse@hote/finance",
    "agent://conteneur@compte",
    "analyste-change",
    "analyste-change/sous-agent",
    "a" * 255,
]
ALPHABET = "abcAZ09_.:/@-"


def accepted(value: str) -> bool:
    try:
        AgentJob.create(value, value)
    except ConfigurationError:
        return False
    return True


def admitted(value: str) -> bool:
    return (
        identifier_fault(value, max_length=api.NAMESPACE_MAX) is None
        and identifier_fault(value, max_length=api.JOB_NAME_MAX) is None
    )


@pytest.mark.parametrize("value", SAMPLES)
def test_an_agent_job_accepted_by_the_adapter_is_admitted_by_the_ingestion(value: str) -> None:
    assert not accepted(value) or admitted(value)


def test_an_address_that_carries_a_password_is_refused_without_being_cited() -> None:
    with pytest.raises(ConfigurationError) as refused:
        AgentJob.create("agent://u:p@hote", "analyste")
    assert "u:p@" not in str(refused.value)


@settings(max_examples=500, deadline=None)
@given(st.text(alphabet=ALPHABET, min_size=1, max_size=40))
def test_no_generated_agent_job_is_accepted_then_refused(value: str) -> None:
    assert not accepted(value) or admitted(value)
