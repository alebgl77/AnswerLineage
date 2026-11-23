"""Adaptateurs d'AnswerLineage : le lignage d'un agent, capté par les crochets de son kit.

Un adaptateur se branche sur un agent sans toucher à sa logique : il émet le
run de l'agent, un run par appel d'outil, rattaché au premier, et un run par
sous-agent. Il s'appuie sur l'émetteur commun (``answerlineage_emitter``) pour
tout ce qui est sensible : empreintes à clé, identifiant de run, événements,
boîte d'envoi et jeton.

- ``answerlineage_adapters.claude_agent_sdk`` : le Claude Agent SDK, installé
  par l'extra ``claude-agent-sdk``.
"""

__version__ = "0.0.0"
