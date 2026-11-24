# Kit de conformité des émetteurs

Un émetteur est ce qui envoie des événements de lignage à AnswerLineage : le [proxy MCP](proxy.md), un adaptateur d'agents, ou tout programme écrit à partir des [schémas publiés](facettes.md). Le kit juge un émetteur sur ce qu'il a émis. Il répond à deux questions, avec un verdict et un rapport qui se gardent comme preuve :

- les événements sont-ils ceux qu'AnswerLineage attend, et relient-ils bien l'appel d'outil, l'agent et la réponse ?
- un contenu, prompt, résultat d'outil ou réponse, est-il sorti de l'émetteur ?

```bash
uv run answerlineage-conformance canaries > canaris.json
uv run answerlineage-conformance check evenements.json --canaries canaris.json --witness echange.txt --logs agent.log --metrics metriques.txt
```

| Code de sortie | Sens |
| --- | --- |
| `0` | Conforme : aucun problème |
| `1` | Non conforme : le rapport liste les problèmes |
| `2` | Entrée illisible ou usage fautif : aucun verdict n'est rendu, le motif est sur la sortie d'erreur |

Le kit ne lit que les fichiers qu'on lui donne. Il n'ouvre aucune connexion et n'a besoin d'aucune base de données : il juge avec le code même de l'ingestion et des workers, et avec la copie des schémas publiés qu'il embarque. Ce que le kit accepte est ce que le produit accepte.

Il juge aussi l'[extension A2A](a2a.md#la-suite-de-conformité) entre deux agents : `answerlineage-conformance a2a --card carte.json --exchange echange.json`, sur l'Agent Card de l'appelé et un échange capturé, avec ses propres codes.

## Intégrer un émetteur, pas à pas

**1. Tirer les canaris.** `canaries` écrit trois valeurs inventées, une par rôle :

```json
{
  "prompt": "canari-prompt-0f1e2d3c4b5a",
  "tool_result": "canari-resultat-6978695a4b3c",
  "answer": "canari-reponse-a1b2c3d4e5f6"
}
```

Un canari vaut `canari-<rôle>-<tirage>` : douze hexadécimaux tirés au sort derrière un rôle en clair. Il n'est fait que de minuscules, de chiffres et de tirets : ni JSON ni une URL ne le déforment. Ce n'est pas un secret. Chaque exécution tire les siens.

**2. Jouer une demande avec eux.** Une demande réelle, de bout en bout, dans un environnement d'essai :

- le canari `prompt` dans la question posée à l'agent ;
- le canari `tool_result` dans ce que l'outil renvoie ;
- le canari `answer` dans la réponse que l'agent rend.

**3. Garder ce que la demande a laissé.**

| Fichier | Contenu | Option |
| --- | --- | --- |
| Événements | Tout ce que l'émetteur a envoyé : un tableau JSON, ou un événement par ligne | argument |
| Témoin | L'échange tel que l'utilisateur et l'outil l'ont vu : la question, l'appel reçu par l'outil, son résultat, la réponse | `--witness` |
| Journaux | Les journaux de l'émetteur, au niveau le plus bavard | `--logs`, répétable |
| Métriques | Un relevé des métriques de l'émetteur, au format texte | `--metrics`, répétable |

Le témoin prouve que les canaris sont bien passés. Un canari qui n'y figure pas tel quel fait échouer le kit : sans cela, une demande qui n'a rien traversé serait verte.

**4. Lancer `check`**, puis garder le rapport avec les fichiers : il cite leur taille et leur empreinte SHA-256.

Les fichiers se lisent tels quels : texte non compressé, 256 Mio au plus par fichier. Un fichier compressé est refusé (code `2`) plutôt que fouillé à tort. Un journal en UTF-16, comme en écrit une redirection PowerShell, se fouille comme un autre. Le JSON des événements est lu comme l'ingestion le lit, `NaN` et `Infinity` refusés ; une clé en double dans un objet est refusée aussi : un lecteur garderait la première valeur, un autre la dernière, et un contenu pourrait se cacher dans celle que le kit ne verrait pas.

## Les deux profils

Tous les émetteurs n'ont pas le même devoir. `--profile` dit lequel est jugé.

| Profil | Émetteur | Ce que sa capture porte | Ce qu'il doit |
| --- | --- | --- | --- |
| `adapter` (défaut) | Un adaptateur d'agents | La chaîne complète : le run de l'agent, ses appels d'outil, leurs lectures, la réponse et sa diffusion | Chaque appel d'outil rattaché au run de l'agent par la facette standard `parent`, et ce parent dans la capture |
| `capture` | Un point de capture qui ne connaît pas l'agent, comme le proxy MCP | Des appels d'outil seulement | Pour chaque appel, son contexte de trace (`traceId`, `spanId` dans `answerlineage_toolCall`), et un `run.runId` qui en dérive |

Sous `capture`, aucun `parent` n'est exigé : le point de capture ne connaît pas le job de l'agent. Mais son identifiant de run est exactement celui que la [dérivation publiée](proxy.md#la-dérivation-à-loctet-près) donne pour le contexte de trace qu'il cite. C'est ce qui permet à un adaptateur qui voit le même contexte d'émettre pour le même run. Le kit refait ce calcul lui-même, d'après la documentation, sans rien importer du proxy ; les vecteurs de [`spec/run-id/vectors-v1.json`](../spec/run-id/vectors-v1.json) tiennent les deux écritures d'accord. L'identifiant est comparé comme un UUID, sans égard à la casse.

**Capture combinée.** Un adaptateur et un point de capture qui ont vu la même demande se jugent ensemble, sous `adapter` : les événements des deux émetteurs pour un même run se complètent. Le devoir se juge par run, pas par événement. L'adaptateur ouvre l'appel (`START`) et le rattache à l'agent ; le point de capture le clôt (`COMPLETE`, `FAIL` ou `ABORT`) et dit ce qui a été lu. Un run n'a qu'un événement terminal : deux émetteurs qui le clôtureraient chacun de leur côté laisseraient l'état du run dépendre de leurs horloges.

## Ce que le kit vérifie

Quatre groupes de contrôles. Chaque contrôle a un code stable.

### Forme

| Code | Ce qui le déclenche |
| --- | --- |
| `ingestion_rejected` | L'[ingestion](ingestion.md) rejetterait l'événement : schéma, heure, identifiants, bornes de nombre et de profondeur, texte que la base ne peut pas conserver. Le motif est le code qu'elle rendrait (`missing`, `event_time_out_of_range`, `identifier_whitespace`, `recipient_not_pseudonym`, `too_many_items`, `json_too_deep`, `unstorable_text`…) |
| `facet_schema_violation` | Une facette propre ne respecte pas le schéma que son `_schemaURL` désigne. Le motif est la règle violée, mot-clé de JSON Schema (`required`, `type`, `maxLength`, `pattern`, `enum`…) ; pour un champ obligatoire, le pointeur désigne le champ qui manque |
| `facet_schema_unknown` | Le `_schemaURL` d'une facette propre n'est l'adresse d'aucune version publiée de son schéma. La facette est alors jugée contre la version la plus récente |
| `facet_unknown` | Une clé au préfixe `answerlineage`, quelle que soit sa casse, qui n'est pas celle d'une facette publiée : une faute de frappe ferait du run un job ordinaire |
| `facet_misplaced` | Une facette connue ailleurs qu'à la [place](facettes.md#les-cinq-facettes) que son schéma lui donne : `answerlineage_toolCall` sur le job, `answerlineage_delivery` dans `facets` au lieu de `outputFacets`… |

### Contexte

| Code | Profil | Ce qui le déclenche |
| --- | --- | --- |
| `parent_missing` | `adapter` | Un appel d'outil dont aucun événement ne porte la facette `parent` |
| `parent_run_id_missing` | `adapter` | Une facette `parent` sans `run.runId` |
| `parent_run_id_not_canonical` | `adapter` | Un `run.runId` de parent qui n'est pas un UUID sous sa forme canonique |
| `parent_not_in_capture` | `adapter` | Un parent dont le run n'a aucun événement dans la capture |
| `not_a_tool_call` | `capture` | Un événement sans la facette `answerlineage_toolCall` |
| `trace_context_missing` | `capture` | Un appel d'outil dont aucun événement ne cite `traceId` et `spanId` ensemble |
| `run_id_not_derived` | les deux | Un contexte de trace cité, bien formé, dont le `run.runId` ne dérive pas |
| `run_without_terminal_event` | les deux | Un run sans `COMPLETE`, `FAIL` ni `ABORT` |
| `run_with_several_terminal_events` | les deux | Un run avec plusieurs événements terminaux distincts. Un événement envoyé deux fois compte pour un : l'ingestion ne garde le second que comme un doublon |
| `no_complete_chain` | `adapter` | Aucune chaîne complète dans la capture : un jeu de données lu par un appel d'outil, rattaché, directement ou par un sous-agent, à un run d'agent qui diffuse une réponse. Une telle capture ne prouverait rien |
| `no_tool_call` | `capture` | Aucun appel d'outil dans la capture |

Un adaptateur qui cite un contexte de trace est tenu à la même dérivation : le schéma de la facette dit que l'identifiant de run en dérive.

### Contenu

| Code | Ce qui le déclenche |
| --- | --- |
| `canary_missing_from_witness` | Un canari absent du témoin |
| `canary_in_events` | Un canari dans les événements, clé comme valeur |
| `canary_in_logs` | Un canari dans un journal |
| `canary_in_metrics` | Un canari dans les métriques |

Un canari est cherché sous cinq formes, et le rapport dit laquelle :

| Forme | Ce qui est retrouvé |
| --- | --- |
| `exact` | Le canari tel quel |
| `case_insensitive` | Le canari, à la casse près |
| `base64` | Le canari dans un texte encodé en base64, quelle que soit sa place dans ce texte, alphabet courant ou sûr pour les URL |
| `hex` | Le canari encodé en hexadécimal, minuscules ou majuscules |
| `draw` | Le tirage seul : une copie tronquée, ou refaite avec d'autres séparateurs, comme un nom de métrique tiré du texte |

### Projection

Ce que l'émetteur déclare et que le graphe ignorerait. La [projection](workers.md) est tolérante : une facette mal formée est ignorée sans bruit, et l'événement est accepté. Le kit projette donc chaque événement avec le code des workers, et compare.

| Code | Ce qui le déclenche |
| --- | --- |
| `projection_failed` | La projection de l'événement échoue : il resterait dans la file. Le motif est la classe de l'erreur |
| `parent_dropped` | Une facette `parent` déclarée, sans run parent dans le graphe |
| `delivery_dropped` | Une diffusion déclarée et perdue : la sortie compterait comme une écriture ordinaire, la réponse resterait sans diffusion, ou des destinataires manqueraient |
| `columns_dropped` | Des colonnes déclarées dans la facette `schema` d'une entrée, absentes de la lecture |

## Lire un rapport

```text
Kit de conformité AnswerLineage 0.0.0
Verdict : NON CONFORME
Profil : adapter
Heure de référence : 2026-11-03T00:00:00Z
Capture : 3 événements, 2 runs

Problèmes : 3
  [contexte] parent_missing : événement 1, /run/facets/parent
  [contexte] no_complete_chain : capture entière
  [contenu] canary_in_logs : agent.log, ligne 2 (canari du résultat d'outil, en base64)

Ce que disent ces codes
  parent_missing : Appel d'outil sans facette parent : rien ne le rattache au run de l'agent.
  …

Contrôles exécutés : 21
  forme : ingestion_rejected, facet_schema_violation, facet_schema_unknown, facet_unknown, facet_misplaced
  …

Fichiers fouillés
  événements  evenements.json  2603 octets  sha256:7db28c47…
  canaris  canaris.json  137 octets  sha256:68470778…
  témoin  echange.txt  174 octets  sha256:27754cad…
  journal  agent.log  116 octets  sha256:fd912f3c…
  métriques  metriques.txt  15 octets  sha256:360b1919…
```

Un problème dit où il est, jamais ce qu'il a trouvé :

- dans un événement : son rang dans la capture, à partir de 0, et un pointeur JSON (RFC 6901) vers le champ. Le pointeur vide désigne l'événement entier ;
- dans un journal ou des métriques : le fichier et le numéro de la ligne, à partir de 1 ;
- pour un canari : son rôle et la forme sous laquelle il est vu.

Aucune valeur reçue n'est recopiée, ni la ligne d'un journal, ni un extrait. Une clé d'un événement n'entre dans un pointeur que si elle a la forme d'un nom, celle que l'[ingestion](ingestion.md#identifiants) exige du nom d'une facette, et ne porte aucun canari ; sinon elle y est remplacée par `<clé masquée>`. Un nom de fichier qui n'est pas anodin est remplacé par `<nom de fichier non affichable>`. Le rapport peut donc être rangé à côté des journaux sans y ajouter de contenu.

Les problèmes vont par groupe, puis par événement, par fichier, et enfin ceux qui tiennent à la capture entière. La liste est bornée à 1 000 problèmes ; le compte, lui, est exact.

Sans fichier de journaux ou de métriques, le rapport dit que rien n'a été fouillé, et le contrôle correspondant n'est pas compté parmi les contrôles exécutés.

### Rapport JSON

`--format json` rend le même contenu, sous une forme stable, à verser dans un pipeline ou un dossier d'audit :

```json
{
  "report_version": 1,
  "kit": { "name": "answerlineage-conformance", "version": "0.0.0" },
  "profile": "adapter",
  "verdict": "non_conformant",
  "received_at": "2026-11-03T00:00:00Z",
  "capture": { "events": 3, "runs": 2 },
  "files": [
    {
      "role": "events",
      "path": "evenements.json",
      "bytes": 2603,
      "sha256": "7db28c4769a08eaa80498f8021730afa6c26dc4009b4fc03e1aa2eec539388d0"
    }
  ],
  "controls": [{ "code": "parent_missing", "group": "context", "problems": 1 }],
  "problem_count": 3,
  "truncated": false,
  "problems": [
    {
      "code": "canary_in_logs",
      "group": "content",
      "message": "Canari retrouvé dans un journal.",
      "event": null,
      "pointer": null,
      "file": "agent.log",
      "line": 2,
      "reason": null,
      "canary": "tool_result",
      "form": "base64"
    }
  ]
}
```

| Champ | Contenu |
| --- | --- |
| `report_version` | Version de cette forme : elle change quand un champ change de sens ou disparaît |
| `kit` | Nom et version du kit |
| `profile` | `adapter` ou `capture` |
| `verdict` | `conformant` ou `non_conformant` |
| `received_at` | Heure de référence du jugement |
| `capture` | Nombre d'événements, et de runs distincts |
| `files` | Les fichiers fouillés, dans l'ordre de la ligne de commande : rôle (`events`, `canaries`, `witness`, `logs`, `metrics`), chemin tel qu'il a été donné, taille en octets, empreinte SHA-256 |
| `controls` | Les contrôles exécutés, dans l'ordre de cette page, avec le nombre de problèmes de chacun. Un contrôle absent n'a pas été exécuté : il n'est pas du profil, ou son fichier n'a pas été fourni |
| `problem_count` | Nombre de problèmes |
| `truncated` | `true` quand `problems` n'en liste que les 1 000 premiers |
| `problems` | Les problèmes : `code`, `group` (`shape`, `context`, `content`, `projection`), `message` (texte fixe du contrôle), puis le lieu (`event` et `pointer`, ou `file` et `line`), le motif (`reason`) et, pour un canari, son rôle (`canary`) et sa forme (`form`). Un champ sans objet vaut `null` |

## Un rapport reproductible

Les mêmes fichiers, donnés sous les mêmes chemins, donnent le même rapport, à l'octet près : rien n'y dépend de la machine ni de l'heure du jugement, sauf l'heure de référence. Le rapport sort en UTF-8, avec des fins de ligne `\n`, quels que soient le terminal et ses réglages régionaux.

L'ingestion borne l'heure d'un événement par celle de sa réception : un jour d'avance au plus. `check` prend donc une heure de référence, `--received-at` (RFC 3339, avec son fuseau), à défaut l'heure courante, et l'écrit dans le rapport. Pour rejouer un jugement, lui donner l'heure du premier rapport.

## Dans un pipeline

Le kit tourne sans réseau ni base de données, depuis un clone du dépôt (`make setup`) ou depuis l'[image de la démonstration](demonstration.md), où il est installé. Un job qui joue la demande puis juge l'émetteur :

```bash
set -eu

# 1. Les canaris de cette exécution.
uv run answerlineage-conformance canaries > canaris.json

# 2. La demande jouée par les tests d'intégration de l'émetteur, qui laissent
#    evenements.jsonl, echange.txt, agent.log et metriques.txt.
./jouer-une-demande.sh canaris.json

# 3. Le verdict. Le rapport est gardé avec les fichiers jugés, comme preuve.
uv run answerlineage-conformance check evenements.jsonl \
  --canaries canaris.json --witness echange.txt \
  --logs agent.log --metrics metriques.txt \
  --format json > rapport-conformite.json
```

`check` sort avec `1` ou `2` et arrête le job dès que l'émetteur n'est pas conforme ou qu'un fichier manque. Garder le rapport et les fichiers comme artefacts du job, y compris en cas d'échec.

## Les schémas embarqués

Le kit installé juge sans le dépôt. Son paquet porte, dans `answerlineage_conformance/schemas/`, la copie de [`spec/facets/`](../spec/facets/1-0-0), du [schéma d'OpenLineage](../spec/openlineage/README.md) que ces facettes prolongent, et des schémas de l'[extension A2A](../spec/a2a/v1/README.md). `spec/` reste la seule source : un test tient la copie identique à l'octet près, fichier pour fichier. Une nouvelle version d'une facette se recopie donc dans le paquet, et le kit la reconnaît sans autre changement : la clé d'une facette, ses versions, leur adresse et sa place viennent de son schéma. Les références vers le schéma d'OpenLineage se résolvent dans cette copie, jamais par le réseau.

## Ce que le kit ne prouve pas

- **L'absence du canari, non de tout contenu.** Un résumé, une traduction, un chiffrement ou un encodage que le kit ne connaît pas passeraient. Une copie tronquée qui coupe le tirage aussi : placer le canari au milieu du texte plutôt qu'au début ou à la fin.
- **Une demande jouée, pas les erreurs ni les reprises.** Un contenu qui ne sort que sur un chemin d'erreur n'est pas vu. Jouer aussi une demande qui échoue.
- **Le transport.** Un événement est jugé comme s'il était envoyé seul. La taille d'un corps, sa compression, la taille d'un lot et les reprises d'envoi tiennent à la façon d'envoyer, que la capture ne dit pas.
- **Une intégration de bonne foi.** Un émetteur malveillant reconnaîtrait un canari et s'abstiendrait.
- **Ce qui lui est donné.** Un journal ou un compteur qui ne figure pas parmi les fichiers n'est pas fouillé ; un canari coupé par un retour à la ligne non plus. Le témoin est fourni par l'intégrateur : le kit vérifie qu'il porte les canaris, pas qu'il dit vrai.
- **Les facettes standard.** Seules les facettes propres sont jugées contre un schéma. La facette `parent` l'est sur son seul `run.runId` : son `job`, que le standard exige, n'est pas exigé par l'ingestion, ni donc par le kit. Les autres, `schema` ou `sql` par exemple, ne sont lues que pour y chercher les canaris et pour comparer ce qu'elles déclarent à ce que le graphe retiendrait.
- **Dans ce dépôt, la chaîne complète ne traverse pas encore un seul vrai composant.** Le test de bout en bout joue l'agent par un script, et le proxy relaie son appel d'outil. L'[adaptateur du Claude Agent SDK](adaptateurs.md#ce-qui-le-vérifie) voit le prompt, les résultats et la réponse : il passe le kit sur une demande jouée par ses crochets, sans modèle, diffusion de la réponse comprise, avec une seule remarque, l'absence de chaîne complète (`no_complete_chain`), puisqu'il ne décrit pas les lectures.
