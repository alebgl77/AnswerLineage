# Adaptateurs d'agents

Un adaptateur se branche sur un agent par les crochets de son kit de développement, sans toucher à sa logique. Il émet ce que seul l'agent connaît : le run de chaque demande, un run par appel d'outil, rattaché au premier, et un run par sous-agent. Là où le [proxy MCP](proxy.md) voit les appels d'outils sans connaître l'agent, l'adaptateur les rattache à la demande qui les a faits.

Le composant `adapters/` (paquet `answerlineage-adapters`) n'a aujourd'hui qu'un adaptateur, celui du [Claude Agent SDK](#claude-agent-sdk). Il s'appuie sur l'émetteur commun (`emitter/`) pour tout ce qui est sensible, comme le proxy : les [empreintes à clé](proxy.md#empreintes-à-clé), la [dérivation de l'identifiant de run](proxy.md#la-dérivation-à-loctet-près), les briques des événements et la [boîte d'envoi](proxy.md#boîte-denvoi) avec le jeton du client.

## Claude Agent SDK

### Installer

Le SDK est un extra facultatif : le paquet de l'adaptateur ne l'installe que sur demande.

```bash
pip install "answerlineage-adapters[claude-agent-sdk]"
```

L'extra demande le SDK 0.2.182 au moins, la version sur laquelle les crochets ont été vérifiés. Sa roue embarque la CLI Claude Code (2.1.325 pour cette version), que le SDK lance : elle pèse environ 160 Mo. Dans ce dépôt, `uv sync --all-packages` l'installe avec les outils de développement, pour que les tests emploient les vrais types du SDK ; l'image de la démonstration ne l'embarque pas.

### Brancher les crochets

L'application nomme son agent, lit les réglages de l'environnement, et passe les crochets de l'adaptateur à `ClaudeAgentOptions`. L'adaptateur reste ouvert le temps de la vie de l'agent : `async with` démarre sa boîte d'envoi, et la vide à la sortie.

```python
import os

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from answerlineage_adapters.claude_agent_sdk import ClaudeAgentLineage
from answerlineage_adapters.settings import AdapterSettings, AgentJob

AGENT = AgentJob.create("agent://finance", "analyste-change")


async def main(question: str) -> None:
    settings = AdapterSettings.from_environment(os.environ, agent=AGENT)
    async with ClaudeAgentLineage.from_settings(settings) as lineage:
        options = ClaudeAgentOptions(hooks=lineage.hooks())
        async with ClaudeSDKClient(options=options) as client:
            await client.query(question)
            async for message in client.receive_response():
                ...
```

L'exemple complet, avec un serveur MCP joint à travers le proxy, est dans [`adapters/examples/claude_agent.py`](../adapters/examples/claude_agent.py). Il appelle un vrai modèle, avec une clé d'API : il ne tourne pas dans le pipeline, où les crochets sont éprouvés sans modèle ni réseau ([ci-dessous](#ce-qui-le-vérifie)).

- **D'autres crochets.** `lineage.hooks(existing=mes_crochets)` rend les crochets de l'application, suivis de ceux de l'adaptateur.
- **Une partie des outils.** `lineage.hooks(tools="mcp__.*")` ne suit que les outils que ce filtre désigne, avec la syntaxe des filtres du SDK ; par défaut, tous, outils intégrés compris (`Read`, `Bash`, `WebFetch`…).
- **Sans réglage de l'environnement.** `ClaudeAgentLineage(agent, sink, fingerprint_key=…)` prend une destination quelconque de l'émetteur commun.

### Les crochets

Les noms des crochets et leurs charges utiles ont été lus dans le SDK installé, version 0.2.182 (`claude_agent_sdk/types.py` : `HookEvent`, les `TypedDict` de chaque entrée, `AsyncHookJSONOutput`), et dans sa documentation, [Intercept and control agent behavior with hooks](https://code.claude.com/docs/en/agent-sdk/hooks) : le tableau des crochets offerts au SDK Python, et la sortie asynchrone.

| Crochet | Ce que l'adaptateur émet | Ce qu'il lit de l'entrée |
| --- | --- | --- |
| `UserPromptSubmit` | L'ouverture du run de l'agent (`START`) : une demande, un run | `session_id`, `prompt_id` ; jamais `prompt` |
| `PostToolUse` | L'appel d'outil réussi (`COMPLETE`), run enfant de l'agent, ou du sous-agent qui l'a fait | `tool_name`, `duration_ms`, `agent_id`, `agent_type` ; `tool_input` et `tool_response` en empreintes seulement |
| `PostToolUseFailure` | L'appel en échec (`FAIL`), ou interrompu (`ABORT`) : l'étape fautive | `tool_name`, `is_interrupt`, `duration_ms`, `agent_id` ; `tool_input` en empreinte ; jamais `error` |
| `SubagentStart` | L'ouverture du run du sous-agent (`START`), enfant de celui de l'agent | `agent_id`, `agent_type` |
| `SubagentStop` | La fin du run du sous-agent (`COMPLETE`) | `agent_id` ; jamais `last_assistant_message` |
| `Stop` | La fin du run de l'agent (`COMPLETE`) | `session_id`, `prompt_id` ; jamais `last_assistant_message` |

Une demande est identifiée par sa session et son `prompt_id` ; un second `Stop` pour la même demande, quand un autre crochet a relancé l'agent, ne clôt pas un second run. Une fin vue sans son début, quand l'adaptateur est branché en cours de demande, clôt un run qui n'a pas d'ouverture. À la fermeture de l'adaptateur, un run resté ouvert est clos en `ABORT`.

### Rien ne retarde l'agent

Chaque crochet rend aussitôt la sortie asynchrone du SDK, `{"async_": True}` (`"async": true` pour la CLI) : la CLI poursuit sans attendre l'adaptateur. Le crochet ne fait que des opérations en mémoire, en temps constant : retrouver le run de la demande, tirer un span, mettre l'appel en file. L'événement se construit ensuite dans une tâche de fond, ses empreintes dans un fil à part, puis part par la boîte d'envoi de l'émetteur commun, qui le prend en charge sans attendre et l'envoie par lots. Une API en panne, lente ou muette ne retient donc aucun appel d'outil.

- **Tout est borné.** La file des événements à construire garde 10 000 événements au plus ; pleine, elle abandonne le plus ancien, le compte, et le journal le dit une fois par épisode. Les runs suivis sont bornés de même ; un run oublié encore ouvert est clos en `ABORT`. La boîte d'envoi a [ses propres bornes](proxy.md#bornes-et-ce-qui-se-perd).
- **Aucune erreur n'atteint l'agent.** Une erreur de l'adaptateur ou de la destination est comptée, et journalisée une fois par classe, sans message ; le crochet rend la main comme toujours.
- **À la fermeture**, l'adaptateur construit et remet ce qui attend en 5 secondes au plus (`flush_seconds`), clôt les runs restés ouverts, puis vide la boîte d'envoi dans son propre délai. Le reste est abandonné et compté.

`lineage.stats` compte, sans aucune valeur venue de l'agent : `emitted` (événements remis à la destination), `dropped` (abandonnés : file pleine, crochet appelé après la fermeture, délai écoulé), `failures` (erreurs comptées).

### Réglages

L'agent se nomme dans le code de l'application ; le reste se lit de l'environnement de son processus. Un réglage refusé arrête l'adaptateur avec une `ConfigurationError` qui nomme le réglage, jamais sa valeur.

| Réglage | Où | Défaut |
| --- | --- | --- |
| Le job de l'agent : espace de noms et nom, 1 à 255 lettres, chiffres et signes `. _ : / @ -` | `AgentJob.create("agent://finance", "analyste-change")` | obligatoire |
| Adresse de l'API d'AnswerLineage | `ANSWERLINEAGE_ADAPTER_API_URL` | aucune : rien n'est envoyé, et le journal le dit |
| Clé d'empreinte, en hexadécimal, de 32 à 64 octets | `ANSWERLINEAGE_ADAPTER_FINGERPRINT_KEY`, ou le fichier que nomme `ANSWERLINEAGE_ADAPTER_FINGERPRINT_KEY_FILE` | aucune : pas d'empreinte |
| Émetteur OIDC, identifiant du client, secret | `ANSWERLINEAGE_ADAPTER_OIDC_ISSUER`, `…_OIDC_CLIENT_ID`, `…_OIDC_CLIENT_SECRET` ou le fichier de `…_OIDC_CLIENT_SECRET_FILE` | aucun : l'API est appelée sans jeton |
| Portée, audience, pour un émetteur OIDC qui les exige | `ANSWERLINEAGE_ADAPTER_OIDC_SCOPE`, `…_OIDC_AUDIENCE` | aucune |
| Bornes de la boîte d'envoi | `AdapterSettings.from_environment(…, max_events=…, max_bytes=…, flush_seconds=…)` | celles du proxy |
| Outils suivis | `lineage.hooks(tools=…)` | tous |

Le jeton est celui de l'émetteur commun, comme [celui du proxy](proxy.md#le-jeton-du-proxy-envers-lapi) : octroi `client_credentials`, point de jeton découvert, gardé en mémoire, demandé par la seule tâche d'envoi. L'émetteur, le client et le secret vont ensemble et demandent une adresse d'API ; le jeton ne part qu'en `https`, ou en `http` vers la boucle locale. Côté API, le client de l'adaptateur s'ajoute aux clients émetteurs (`ANSWERLINEAGE_OIDC_EMITTER_CLIENTS`, [authentification](securite/authentification.md#régler-lémetteur)).

### Ce qui sort

Trois sortes de runs, tous du producteur `https://github.com/alebgl77/AnswerLineage/tree/<version>/adapters` :

- **le run de l'agent** : le job de l'agent, la facette `answerlineage_agent` avec `framework: "claude-agent-sdk"` ;
- **le run d'un sous-agent** : le job `<nom de l'agent>/<type du sous-agent>`, la même facette, et la facette standard `parent` vers le run de l'agent ;
- **le run d'un appel d'outil** : la facette `parent` vers le run de l'agent ou du sous-agent qui l'a fait, la facette `answerlineage_toolCall`, et la facette `answerlineage_qos`.

```json
{
  "eventType": "COMPLETE",
  "eventTime": "2026-11-02T09:00:02.000000Z",
  "producer": "https://github.com/alebgl77/AnswerLineage/tree/0.0.0/adapters",
  "run": {
    "runId": "…",
    "facets": {
      "parent": { "run": { "runId": "…" }, "job": { "namespace": "agent://finance", "name": "analyste-change" } },
      "answerlineage_toolCall": {
        "server": "entrepot",
        "tool": "execute_sql",
        "invalidToolName": false,
        "readResolution": "unresolved",
        "unresolvedReason": "undescribed_tool",
        "traceId": "…",
        "spanId": "…",
        "argumentsFingerprint": "hmac-sha256:…",
        "resultSize": 38,
        "resultFingerprint": "hmac-sha256:…"
      },
      "answerlineage_qos": { "errorCount": 0, "latencyMs": 42, "outcome": "success" }
    }
  },
  "job": { "namespace": "mcp://entrepot", "name": "execute_sql" },
  "inputs": [],
  "outputs": []
}
```

- **Le nom de l'outil.** Un outil MCP, que le SDK nomme `mcp__<serveur>__<outil>`, devient le job `mcp://<serveur>` / `<outil>`, comme pour le proxy. Un outil intégré à Claude Code devient le job `claude-code://builtin` / `<outil>`, sous le serveur `claude-code`. Un nom hors de la forme recommandée par MCP (1 à 128 lettres, chiffres, `_`, `-`, `.`) n'est pas recopié : le job est `outil-non-resolu`, la facette dit `invalidToolName`, et un nom MCP illisible a pour serveur `serveur-non-resolu`. Un type de sous-agent hors de cette forme devient `sous-agent-non-resolu`.
- **Les lectures.** L'adaptateur ne dit pas ce qu'un outil a lu : chaque appel se déclare `unresolved`, motif `undescribed_tool`, et entre dans la liste des [outils à décrire](ingestion.md#outils-à-décrire) de l'API, sous l'espace de noms de son job.
- **L'échec.** Un appel en échec part en `FAIL` avec `errorCount: 1` ; un appel interrompu en `ABORT` avec l'issue `client_abort`. Le message d'erreur, un texte libre, ne sort jamais.
- **Le contexte de trace.** Chaque demande ouvre une trace W3C, dont l'identifiant de run de l'agent dérive ; chaque sous-agent et chaque appel d'outil y est un span. La facette de l'appel cite `traceId` et `spanId`, dont son identifiant de run dérive par la [dérivation publiée](proxy.md#la-dérivation-à-loctet-près). Elle ne porte pas `traceOrigin`, dont les valeurs publiées ne disent que ce que voit le proxy.

### Ce qui ne sort jamais

Ni le prompt, ni les arguments, ni le résultat d'un outil, ni le message d'une erreur, ni la réponse de l'agent ou d'un sous-agent, ni les chemins et identifiants que les crochets donnent (`transcript_path`, `cwd`, `session_id`, `prompt_id`, `agent_id`, `tool_use_id`). Les arguments et le résultat partent en empreintes HMAC à clé de locataire : les arguments sur la forme canonique JSON (RFC 8785) de `tool_input`, comme le proxy sur `params.arguments`, le résultat sur la forme canonique JSON de `tool_response`, le résultat tel que l'agent le reçoit, décodé ; sa taille est celle de cette forme. Sans clé, aucune empreinte. Les identifiants de session, de demande et de sous-agent ne servent que de clés en mémoire, bornées à 256 caractères.

Le journal (`answerlineage_adapters`) ne dit que des nombres, des classes d'erreur et des textes fixes.

### Ce qui le vérifie

- **Les vrais types du SDK, sans modèle ni réseau.** `adapters/tests/test_claude_agent_sdk.py` appelle les crochets avec les `TypedDict` du SDK installé, et les fait passer par le protocole de contrôle du SDK : une CLI jouée par le test (`adapters/tests/fake_cli.py`) reçoit la déclaration des crochets, les déclenche, et reçoit `{"async": true}`.
- **Le kit de conformité, comme tout émetteur.** `tests/test_conformance_adapter.py` joue une demande avec trois canaris, dans le prompt et les arguments, dans le résultat et un message d'erreur, dans la réponse de l'agent et d'un sous-agent ; les événements partent par la vraie boîte d'envoi. Aucun canari ne ressort des événements, du journal au niveau le plus bavard, des sorties ni des compteurs, et [le kit](conformite.md) le confirme. Il ne relève qu'une chose : la capture n'a pas encore de chaîne complète (`no_complete_chain`), faute de lecture décrite et de diffusion de la réponse.

## Limites connues

- **Aucune lecture décrite.** L'adaptateur rattache chaque appel d'outil à l'agent, mais ne sait pas ce que l'outil a lu : une réponse ne remonte pas à ses données par lui seul.
- **Pas de contexte partagé avec le proxy.** Les crochets du SDK ne portent pas le contexte de trace de l'appel MCP, et ne peuvent pas l'écrire dans `_meta` : un appel vu par l'adaptateur et par un proxy placé devant le même serveur fait deux runs, et les lectures que décrit le proxy ne se rattachent pas à l'agent. Le modèle de menaces l'accepte, à signer (`adapters/lecture-non-decrite`).
- **La réponse et sa diffusion ne sont pas encore émises.** Elles viendront avec la pseudonymisation des destinataires : la fin du run de l'agent, au crochet `Stop`, est l'endroit où elles s'ajouteront à ses sorties.
- **Empreinte du résultat.** Elle porte sur la forme canonique du résultat décodé, pas sur les octets de la réponse du serveur MCP : elle ne se compare pas à celle du proxy pour le même appel. Celle des arguments, si.
- **Un tour qui finit en erreur.** Le SDK Python n'offre pas les crochets `StopFailure`, `SessionStart` ni `SessionEnd` : une demande close par une erreur de l'API n'a pas de `Stop`, et son run reste ouvert jusqu'à la fermeture de l'adaptateur, qui le clôt en `ABORT`.
- **Un nom MCP ambigu.** `mcp__a__b__c` se lit comme l'outil `b__c` du serveur `a`.
- **asyncio seulement.** La boîte d'envoi et la construction des événements tournent dans la boucle asyncio de l'application ; une application sous trio n'est pas prise en charge.
- **La version du SDK.** Les crochets ont été vérifiés sur la version 0.2.182 ; le SDK est en version 0.x, et ses types peuvent changer d'une version à l'autre.
