# Adaptateurs d'agents

Un adaptateur se branche sur un agent par les crochets de son kit de développement, sans toucher à sa logique. Il émet ce que seul l'agent connaît : le run de chaque demande, un run par appel d'outil, rattaché au premier, et un run par sous-agent ; puis la réponse, et à qui elle a été diffusée, quand l'intégration le lui dit. Là où le [proxy MCP](proxy.md) voit les appels d'outils sans connaître l'agent, l'adaptateur les rattache à la demande qui les a faits.

Le composant `adapters/` (paquet `answerlineage-adapters`) n'a aujourd'hui qu'un adaptateur, celui du [Claude Agent SDK](#claude-agent-sdk). Il s'appuie sur l'émetteur commun (`emitter/`) pour tout ce qui est sensible, comme le proxy : les [empreintes à clé](proxy.md#empreintes-à-clé), le [pseudonyme d'un destinataire](securite/pseudonymes.md#son-calcul-à-loctet-près), la [dérivation de l'identifiant de run](proxy.md#la-dérivation-à-loctet-près), les briques des événements et la [boîte d'envoi](proxy.md#boîte-denvoi) avec le jeton du client.

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
- **Sans réglage de l'environnement.** `ClaudeAgentLineage(agent, sink, fingerprint_key=…, pseudonym_key=…)` prend une destination quelconque de l'émetteur commun.

### Déclarer la diffusion de la réponse

Les crochets ne disent pas où la réponse est partie : c'est l'intégration qui la publie. Une fois la réponse publiée, elle la déclare à l'adaptateur par `record_delivery`, avec le canal, la référence du message, les destinataires, la réponse elle-même et la session de la demande. Pour une réponse publiée dans un fil Slack :

```python
from claude_agent_sdk import ResultMessage

from answerlineage_emitter.delivery import SLACK, slack_message_ref

async with ClaudeSDKClient(options=options) as client:
    await client.query(question)
    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            result = message
    # Le client Slack de l'application : chat.postMessage rend le canal et l'horodatage du
    # message publié ; les destinataires sont les membres du canal.
    posted = await slack.chat_postMessage(channel=canal, thread_ts=fil, text=result.result)
    membres = await slack.conversations_members(channel=posted["channel"])
    lineage.record_delivery(
        SLACK,
        slack_message_ref(posted["channel"], posted["ts"]),
        membres["members"],
        answer=result.result,
        session_id=result.session_id,
    )
```

| Argument | Ce qu'il porte | Ce qui en sort |
| --- | --- | --- |
| `channel` | Le canal de la diffusion : `slack`, `teams`, `email`… Un nom court, de 1 à 64 lettres minuscules, chiffres et signes `. _ -` | Tel quel, en minuscules ; sinon `canal-non-resolu` |
| `message_ref` | La référence du message sur ce canal. Sur Slack, `slack_message_ref(canal, ts)` : l'identifiant du canal (`C`, `G` ou `D` suivi de 8 à 20 majuscules ou chiffres) et l'horodatage du message (`ts`, dix chiffres, un point, six chiffres), `C024BE91L/1355517523.000005`. Sur un autre canal, de 1 à 512 lettres et chiffres ASCII et signes `. _ - : / @ = + ~ % , #`, sans mot de passe | Telle quelle ; sinon `message-non-resolu`, de même quand le canal n'est pas résolu |
| `recipients` | Les destinataires : des identités, telles que l'intégration les connaît (identifiants de membre, adresses). Sur Slack, l'identifiant de membre (`U…`, `W…`), celui que la liste des membres d'un canal donne : c'est celui qu'un gestionnaire d'incident apportera pour [révéler](securite/pseudonymes.md#la-révélation) un destinataire | Des pseudonymes, triés, sans doublon |
| `answer` | Le texte de la réponse, tel qu'il a été publié | Son empreinte à clé |
| `session_id` | La session de la demande, celle du `ResultMessage` | Rien : elle désigne le run, en mémoire |

Sur Slack, la référence garde l'identifiant du canal et l'horodatage du message : ensemble, ils désignent un message, et la messagerie retrouve son fil. Elle ne garde ni le nom du canal, qui peut dire de quoi il parle, ni l'espace de travail, ni l'horodatage du fil, ni le lien du message.

- **Le run de la diffusion.** La diffusion se rattache au dernier run d'agent de la session. Clos, le run reçoit un événement `OTHER`, qu'OpenLineage prévoit pour ajouter à un run terminé : il porte la réponse, daté de la fin du run, et l'état du run, que donne son [dernier événement](ingestion.md#heure-de-lévénement), reste celui de sa fin : à heure égale, le graphe retient l'état terminal. Encore ouvert, le run reçoit l'événement à l'heure présente, et son arrêt vient après. Sans run connu pour la session (l'adaptateur branché en cours de route, ou un run oublié au-delà de la borne), la diffusion clôt un run qui lui est propre, par un événement `COMPLETE`.
- **Plusieurs diffusions.** Une réponse publiée en deux endroits se déclare deux fois : deux diffusions d'une même réponse, sous la même empreinte.
- **Rien ne retarde l'intégration.** `record_delivery` ne calcule rien : il lit les destinataires (10 000 au plus), retrouve le run en mémoire, et met la diffusion en file. L'empreinte et les pseudonymes se calculent dans un fil, puis l'événement part par la boîte d'envoi. Aucune erreur n'en sort : elle est comptée, comme celle d'un crochet.

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

`lineage.stats` compte, sans aucune valeur venue de l'agent : `emitted` (événements remis à la destination), `dropped` (abandonnés : file pleine, crochet ou diffusion après la fermeture, délai écoulé, diffusion dont la réponse n'a pas d'empreinte), `failures` (erreurs comptées), `deliveries` (diffusions remises à la destination), `recipients_refused` (destinataires écartés sans pseudonyme).

### Réglages

L'agent se nomme dans le code de l'application ; le reste se lit de l'environnement de son processus. Un réglage refusé arrête l'adaptateur avec une `ConfigurationError` qui nomme le réglage, jamais sa valeur.

| Réglage | Où | Défaut |
| --- | --- | --- |
| Le job de l'agent : espace de noms et nom, 1 à 255 lettres, chiffres et signes `. _ : / @ -` | `AgentJob.create("agent://finance", "analyste-change")` | obligatoire |
| Adresse de l'API d'AnswerLineage | `ANSWERLINEAGE_ADAPTER_API_URL` | aucune : rien n'est envoyé, et le journal le dit |
| Clé d'empreinte, en hexadécimal, de 32 à 64 octets | `ANSWERLINEAGE_ADAPTER_FINGERPRINT_KEY`, ou le fichier que nomme `ANSWERLINEAGE_ADAPTER_FINGERPRINT_KEY_FILE` | aucune : pas d'empreinte, et aucune diffusion |
| Clé des pseudonymes, en hexadécimal, de 32 à 64 octets : la clé courante du [coffre à clés](securite/pseudonymes.md#la-clé-et-le-coffre-à-clés) de l'API | `ANSWERLINEAGE_ADAPTER_PSEUDONYM_KEY`, ou le fichier que nomme `ANSWERLINEAGE_ADAPTER_PSEUDONYM_KEY_FILE` | aucune : une diffusion part sans ses destinataires |
| Émetteur OIDC, identifiant du client, secret | `ANSWERLINEAGE_ADAPTER_OIDC_ISSUER`, `…_OIDC_CLIENT_ID`, `…_OIDC_CLIENT_SECRET` ou le fichier de `…_OIDC_CLIENT_SECRET_FILE` | aucun : l'API est appelée sans jeton |
| Portée, audience, pour un émetteur OIDC qui les exige | `ANSWERLINEAGE_ADAPTER_OIDC_SCOPE`, `…_OIDC_AUDIENCE` | aucune |
| Bornes de la boîte d'envoi | `AdapterSettings.from_environment(…, max_events=…, max_bytes=…, flush_seconds=…)` | celles du proxy |
| Outils suivis | `lineage.hooks(tools=…)` | tous |

Chaque clé vient de sa variable ou du fichier qu'elle nomme, pas des deux, jamais de la ligne de commande ; aucune ne s'affiche, dans un message, un journal ou une représentation. La clé des pseudonymes n'est pas celle des empreintes : l'API la tient pour révéler un destinataire, et ne doit pas pouvoir éprouver les arguments des appels d'outils. Deux clés de même valeur arrêtent l'adaptateur, sans qu'aucune soit citée. À une [rotation](securite/pseudonymes.md#la-rotation), l'adaptateur passe à la nouvelle clé à la date choisie, une fois qu'elle est en tête du coffre de l'API.

Le jeton est celui de l'émetteur commun, comme [celui du proxy](proxy.md#le-jeton-du-proxy-envers-lapi) : octroi `client_credentials`, point de jeton découvert, gardé en mémoire, demandé par la seule tâche d'envoi. L'émetteur, le client et le secret vont ensemble et demandent une adresse d'API ; le jeton ne part qu'en `https`, ou en `http` vers la boucle locale. Côté API, le client de l'adaptateur s'ajoute aux clients émetteurs (`ANSWERLINEAGE_OIDC_EMITTER_CLIENTS`, [authentification](securite/authentification.md#régler-lémetteur)).

### Ce qui sort

Trois sortes de runs, et la diffusion de la réponse, tous du producteur `https://github.com/alebgl77/AnswerLineage/tree/<version>/adapters` :

- **le run de l'agent** : le job de l'agent, la facette `answerlineage_agent` avec `framework: "claude-agent-sdk"` ;
- **le run d'un sous-agent** : le job `<nom de l'agent>/<type du sous-agent>`, la même facette, et la facette standard `parent` vers le run de l'agent ;
- **le run d'un appel d'outil** : la facette `parent` vers le run de l'agent ou du sous-agent qui l'a fait, la facette `answerlineage_toolCall`, et la facette `answerlineage_qos` ;
- **la diffusion de la réponse** : un événement du run de l'agent, qui porte en sortie la réponse et la facette [`answerlineage_delivery`](facettes.md#answerlineage_delivery), dans sa version 1-1-0 ([ci-dessous](#la-diffusion)).

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

#### La diffusion

```json
{
  "eventType": "OTHER",
  "eventTime": "2026-11-02T09:00:05.000000Z",
  "producer": "https://github.com/alebgl77/AnswerLineage/tree/0.0.0/adapters",
  "run": {
    "runId": "…",
    "facets": { "answerlineage_agent": { "framework": "claude-agent-sdk", "…": "…" } }
  },
  "job": { "namespace": "agent://finance", "name": "analyste-change" },
  "inputs": [],
  "outputs": [
    {
      "namespace": "agent://finance",
      "name": "analyste-change/reponses",
      "outputFacets": {
        "answerlineage_delivery": {
          "_producer": "https://github.com/alebgl77/AnswerLineage/tree/0.0.0/adapters",
          "_schemaURL": "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-1-0/DeliveryOutputDatasetFacet.json#/$defs/DeliveryOutputDatasetFacet",
          "fingerprint": "hmac-sha256:…",
          "channel": "slack",
          "messageRef": "C024BE91L/1355517523.000005",
          "recipients": ["hmac-sha256:…", "hmac-sha256:…"]
        }
      }
    }
  ]
}
```

- **La réponse** est la sortie `<nom de l'agent>/reponses` de l'espace de noms de l'agent, décrite par son empreinte : HMAC-SHA-256, sous la clé d'empreinte du locataire, de l'étiquette `answerlineage/fingerprint/answer/v1`, d'un octet nul et des octets UTF-8 du texte publié, en hexadécimal minuscule après `hmac-sha256:`, comme les [autres empreintes](proxy.md#le-calcul-à-loctet-près). Sans clé d'empreinte, ou pour une réponse qui n'est pas un texte Unicode valide, ou de plus de 16 777 216 caractères, pas d'empreinte : la diffusion ne part pas, elle est comptée (`dropped`), et le journal le dit une fois.
- **Sa taille ne sort pas.** Une réponse courte se devine à sa longueur (« oui », « non », un montant) : la facette n'en dit rien, ni exacte ni arrondie.
- **Les destinataires** sont des pseudonymes : HMAC-SHA-256, sous la clé des pseudonymes du locataire, de l'étiquette `answerlineage/pseudonym/recipient/v1`, d'un octet nul et de l'identité mise en minuscules puis normalisée en NFC ([le calcul à l'octet près](securite/pseudonymes.md#son-calcul-à-loctet-près)). Le calcul se fait dans le processus de l'agent : l'identité ne le quitte jamais, ni vers l'API ni ailleurs. Deux écritures d'une même identité font un seul destinataire.
- **Une identité sans pseudonyme est écartée** : vide, de plus de 320 caractères une fois canonique, qui porte un blanc, un séparateur, un caractère de contrôle, de formatage ou un demi-codet, ou qui n'est pas un texte. Elle n'est recopiée nulle part ; elle est comptée (`recipients_refused`), et le journal le dit une fois, par un texte fixe. De même, au-delà de 10 000 destinataires, la borne de l'ingestion, ceux qui suivent ; un texte donné pour la liste, qui n'est pas coupé en lettres ; une liste illisible ; et, sans clé des pseudonymes, tous : la diffusion part alors sans eux.

### Ce qui ne sort jamais

Ni le prompt, ni les arguments, ni le résultat d'un outil, ni le message d'une erreur, ni la réponse de l'agent ou d'un sous-agent, ni un fragment ou la taille de la réponse diffusée, ni l'identité d'un destinataire, ni un canal ou une référence de message hors de la forme admise, ni les chemins et identifiants que les crochets donnent (`transcript_path`, `cwd`, `session_id`, `prompt_id`, `agent_id`, `tool_use_id`). Les arguments et le résultat partent en empreintes HMAC à clé de locataire : les arguments sur la forme canonique JSON (RFC 8785) de `tool_input`, comme le proxy sur `params.arguments`, le résultat sur la forme canonique JSON de `tool_response`, le résultat tel que l'agent le reçoit, décodé ; sa taille est celle de cette forme. La réponse diffusée part en empreinte à clé, ses destinataires en pseudonymes. Sans clé, aucune empreinte, ni aucun pseudonyme. Les identifiants de session, de demande et de sous-agent ne servent que de clés en mémoire, bornées à 256 caractères.

Le journal (`answerlineage_adapters`) ne dit que des nombres, des classes d'erreur et des textes fixes.

### Ce qui le vérifie

- **Les vrais types du SDK, sans modèle ni réseau.** `adapters/tests/test_claude_agent_sdk.py` appelle les crochets avec les `TypedDict` du SDK installé, et les fait passer par le protocole de contrôle du SDK : une CLI jouée par le test (`adapters/tests/fake_cli.py`) reçoit la déclaration des crochets, les déclenche, et reçoit `{"async": true}`.
- **Le kit de conformité, comme tout émetteur.** `tests/test_conformance_adapter.py` joue une demande avec trois canaris, dans le prompt et les arguments, dans le résultat et un message d'erreur, dans la réponse de l'agent et d'un sous-agent, puis dans la réponse publiée dans un fil Slack et l'identité de l'un de ses destinataires ; les événements partent par la vraie boîte d'envoi. Aucun canari ne ressort des événements, du journal au niveau le plus bavard, des sorties ni des compteurs, et [le kit](conformite.md) le confirme. La diffusion y est, projetée avec ses destinataires. Le kit ne relève plus qu'une chose : la capture n'a pas de chaîne complète (`no_complete_chain`), faute de lecture décrite ; l'adaptateur ne sait pas ce que ses outils lisent.
- **De l'adaptateur au graphe.** `tests/test_delivery_end_to_end.py` joue une demande et la diffusion de sa réponse à travers la vraie boîte d'envoi, l'API d'ingestion et un worker : la réponse se retrouve par son message, rattachée au run de l'agent, dont l'état reste `COMPLETE`, avec ses destinataires sous les pseudonymes que l'API calcule elle-même ; un canari dans la réponse et les identités ne se retrouve ni dans les événements reçus, ni dans la base, ni au journal, et une identité refusée n'est recopiée nulle part.
- **Deux calculs du pseudonyme, d'accord.** L'émetteur et l'API calculent chacun le pseudonyme, de leur côté : `tests/test_contract_delivery.py` les tient d'accord sur les vecteurs publiés et sur des identités tirées au hasard, et vérifie qu'un canal ou une référence que l'émetteur garde a la forme qu'exige l'ingestion.

## Limites connues

- **Aucune lecture décrite.** L'adaptateur rattache chaque appel d'outil à l'agent, mais ne sait pas ce que l'outil a lu : une réponse ne remonte pas à ses données par lui seul.
- **Pas de contexte partagé avec le proxy.** Les crochets du SDK ne portent pas le contexte de trace de l'appel MCP, et ne peuvent pas l'écrire dans `_meta` : un appel vu par l'adaptateur et par un proxy placé devant le même serveur fait deux runs, et les lectures que décrit le proxy ne se rattachent pas à l'agent. Le modèle de menaces l'accepte, à signer (`adapters/lecture-non-decrite`).
- **La diffusion se rattache par la session.** Elle va au dernier run d'agent de la session : deux demandes menées en même temps dans une même session, dont la première serait diffusée après le début de la seconde, rattacheraient sa réponse à la seconde. Une application qui mène plusieurs demandes à la fois ouvre une session par demande.
- **L'heure de la diffusion.** Une diffusion déclarée après la fin du run porte l'heure de cette fin, et non celle de la publication : la réponse est datée du moment où l'agent l'a produite.
- **10 000 destinataires par diffusion.** Au-delà, la borne du schéma et de l'ingestion, les suivants sont écartés et comptés : un message vu par un canal plus peuplé ne garde que ses 10 000 premiers destinataires.
- **Les destinataires sont ceux que l'intégration donne.** L'adaptateur ne lit pas la liste des membres d'un canal : qui le rejoint après la publication, ou qui lit un message transféré, n'est pas compté.
- **Les clés.** Sans clé d'empreinte, aucune diffusion ne part ; sans clé des pseudonymes, une diffusion part sans ses destinataires. Le journal le dit une fois, au premier cas, pas au démarrage.
- **Empreinte du résultat.** Elle porte sur la forme canonique du résultat décodé, pas sur les octets de la réponse du serveur MCP : elle ne se compare pas à celle du proxy pour le même appel. Celle des arguments, si.
- **Un tour qui finit en erreur.** Le SDK Python n'offre pas les crochets `StopFailure`, `SessionStart` ni `SessionEnd` : une demande close par une erreur de l'API n'a pas de `Stop`, et son run reste ouvert jusqu'à la fermeture de l'adaptateur, qui le clôt en `ABORT`.
- **Un nom MCP ambigu.** `mcp__a__b__c` se lit comme l'outil `b__c` du serveur `a`.
- **asyncio seulement.** La boîte d'envoi et la construction des événements tournent dans la boucle asyncio de l'application ; une application sous trio n'est pas prise en charge.
- **La version du SDK.** Les crochets ont été vérifiés sur la version 0.2.182 ; le SDK est en version 0.x, et ses types peuvent changer d'une version à l'autre.
