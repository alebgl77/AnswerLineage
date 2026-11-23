"""Exemple : un agent du Claude Agent SDK dont AnswerLineage capte les appels d'outils.

L'exemple ne tourne pas dans le pipeline : il appelle un vrai modèle, avec une
clé d'API, et un vrai serveur MCP. Il montre ce qu'une application ajoute pour
brancher l'adaptateur : nommer l'agent, lire les réglages de l'environnement,
passer les crochets à ``ClaudeAgentOptions``, et garder l'adaptateur ouvert le
temps de la vie de l'agent. La logique de l'agent ne change pas.

    uv sync --all-packages        # l'extra claude-agent-sdk est dans le groupe dev
    export ANTHROPIC_API_KEY=…    # lu par la CLI Claude Code que le SDK lance
    export ANSWERLINEAGE_ADAPTER_API_URL=https://answerlineage.interne
    export ANSWERLINEAGE_ADAPTER_FINGERPRINT_KEY_FILE=/run/secrets/cle-des-empreintes
    export ANSWERLINEAGE_ADAPTER_OIDC_ISSUER=https://idp.interne/realms/lignage
    export ANSWERLINEAGE_ADAPTER_OIDC_CLIENT_ID=adaptateur-finance
    export ANSWERLINEAGE_ADAPTER_OIDC_CLIENT_SECRET_FILE=/run/secrets/adaptateur-finance
    uv run python adapters/examples/claude_agent.py "Quel est le taux de change du jour ?"

Le serveur MCP ``entrepot`` est joint ici à travers le proxy d'AnswerLineage
(voir ``docs/proxy.md``), qui décrit les tables lues ; l'adaptateur, lui,
rattache chaque appel d'outil au run de l'agent.
"""

from __future__ import annotations

import asyncio
import os
import sys

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from answerlineage_adapters.claude_agent_sdk import ClaudeAgentLineage
from answerlineage_adapters.settings import AdapterSettings, AgentJob

AGENT = AgentJob.create("agent://finance", "analyste-change")


async def main(question: str) -> None:
    settings = AdapterSettings.from_environment(os.environ, agent=AGENT)
    async with ClaudeAgentLineage.from_settings(settings) as lineage:
        options = ClaudeAgentOptions(
            hooks=lineage.hooks(),
            mcp_servers={"entrepot": {"type": "http", "url": "http://127.0.0.1:8717/mcp"}},
            allowed_tools=["mcp__entrepot__execute_sql"],
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.query(question)
            async for message in client.receive_response():
                if isinstance(message, ResultMessage) and message.result is not None:
                    print(message.result)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
