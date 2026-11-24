# Extension A2A d'AnswerLineage

Quand un agent en appelle un autre par le protocole [A2A](https://github.com/a2aproject/A2A) (Agent2Agent), la réponse du premier dépend de ce que le second a lu. Cette extension relie les deux lignages : l'agent appelant transmet le contexte de trace de son appel, l'agent appelé rattache son run au run de cet appel et lui renvoie un résumé de lignage borné, sans contenu.

Cette page est la spécification de la version 1. Deux équipes l'implémentent chacune de son côté, l'une l'appelant, l'autre l'appelé, à partir de cette page et des schémas publiés dans [`spec/a2a/v1/`](../spec/a2a/v1/README.md), sans lire le code d'AnswerLineage. La [suite de conformité](#la-suite-de-conformité) juge ce qu'elles ont échangé.

Les mots **doit**, **ne doit pas**, **devrait** et **peut** ont ici le sens de MUST, MUST NOT, SHOULD et MAY.

## L'URI

```text
https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1
```

C'est l'identifiant de l'extension, et l'adresse du dossier qui publie ses schémas : elle se résout vers la description de sa version, comme A2A le recommande. Elle se compare à l'octet près. La version est dans l'URI : une version 2 aurait une autre URI, et un agent qui ne connaît que la version 1 l'ignorerait.

## Le protocole A2A suivi

L'extension suit **A2A 1.0** : la version publiée `1.0.0` de la spécification, paquet protobuf `lf.a2a.v1`. Les textes de référence sont ceux du dépôt `a2aproject/A2A` à l'étiquette `v1.0.0` : `docs/specification.md`, `specification/a2a.proto` et la page `docs/topics/extensions.md`. Ce que l'extension en utilise :

| Mécanisme d'A2A | Où | Ce qu'il dit |
| --- | --- | --- |
| Déclaration | §4.4.3, §4.4.4, §4.6.1 | Un agent déclare les extensions qu'il prend en charge dans `capabilities.extensions` de son Agent Card. Chaque entrée (`AgentExtension`) a `uri`, `description`, `required` et `params`, ce dernier libre et défini par l'extension |
| Activation | §3.2.6 | Le paramètre de service `A2A-Extensions` : la liste, séparée par des virgules, des URI d'extensions que le client veut utiliser pour cette requête. En JSON-RPC et en HTTP+JSON, c'est un en-tête HTTP ; en gRPC, une métadonnée. Le nom d'un paramètre de service se lit sans égard à la casse |
| Écho | page *Extensions* | La réponse devrait porter `A2A-Extensions`, avec les extensions effectivement activées |
| Données d'une extension | §4.6.2 | `Message.extensions` liste les URI des extensions présentes dans le message ; `Message.metadata`, `Task.metadata` et `Artifact.metadata` sont des objets libres. L'exemple de la spécification range les données d'une extension sous la clé de son URI |
| Versions | §4.6.3 | La version est dans l'URI ; un changement incompatible demande une nouvelle URI. Une extension, ou une version d'extension, demandée et non prise en charge devrait être ignorée ; l'agent ne doit pas se rabattre de lui-même sur une autre version |
| Extension obligatoire | §3.3.2, §3.3.4, §5.4 | Un agent qui marque une extension `required: true` rejette le client qui ne l'active pas : `ExtensionSupportRequiredError`, code JSON-RPC `-32008`, gRPC `FAILED_PRECONDITION`, HTTP `400` |
| Forme JSON | §5.5 | Les noms de champs en camelCase (`messageId`, `contextId`), les énumérations par leur nom (`ROLE_USER`, `TASK_STATE_COMPLETED`) |

Les numéros de section sont ceux de `docs/specification.md` à cette étiquette. Cette page ne dépend pas de la façon dont une liaison enveloppe la réponse : elle désigne les objets d'A2A (`SendMessageRequest`, `SendMessageResponse`, `Task`, `Message`) sous leur forme JSON.

## En bref

1. L'appelé **déclare** l'extension dans son Agent Card, avec le job OpenLineage sous lequel un appel à lui s'inscrit (`callJob`).
2. L'appelant ouvre le span de son appel, en **dérive** l'identifiant du run de l'appel, inscrit ce run comme enfant du sien sous le job `callJob`, et **active** l'extension : il envoie le contexte de trace et l'identifiant de run, rien d'autre.
3. L'appelé vérifie ce contexte, **rattache** son run au run de l'appel par la facette standard `parent`, répond comme il l'aurait fait sans l'extension, et joint un **résumé** de lignage.
4. L'appelant **valide** le résumé comme toute entrée hostile, et peut en tirer les lectures de son appel.

Le lignage remonte alors sans rupture : jeu de données lu, appel d'outil de l'appelé, run de l'appelé, run de l'appel, run de l'appelant, réponse diffusée. La [propagation](lecture.md#propager-un-incident) d'un incident sur ce jeu de données atteint la réponse de l'appelant.

## Déclaration dans l'Agent Card

L'appelé déclare l'extension une fois, dans `capabilities.extensions`. Un extrait de carte, les autres champs étant ceux d'A2A :

```json
{
  "name": "Agent des taux de change",
  "capabilities": {
    "extensions": [
      {
        "uri": "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1",
        "description": "Rattache son run à celui de l'appel et rend un résumé de lignage.",
        "required": false,
        "params": {
          "callJob": { "namespace": "a2a://change", "name": "taux-du-mois" }
        }
      }
    ]
  }
}
```

| Champ | Obligatoire | Règle |
| --- | --- | --- |
| `uri` | oui | L'URI de la version 1, exactement |
| `description` | non | Texte libre, 1 024 caractères au plus, pour un lecteur humain |
| `required` | non | `false`, ou absent : le lignage ne conditionne jamais un appel. Une extension obligatoire ferait rejeter par A2A (`-32008`) tout appelant qui ne l'active pas, et la capture ne doit jamais bloquer un agent |
| `params.callJob.namespace` | oui | Espace de noms du job d'un appel à cet agent, 512 caractères au plus |
| `params.callJob.name` | oui | Nom de ce job, 512 caractères au plus |

`callJob` est le job OpenLineage du run d'un appel à cet agent : l'appelant le donne à ce run, l'appelé le cite dans sa facette `parent`. Les deux émetteurs nomment ainsi le même job sans qu'il passe dans la requête. Ses deux champs ont la forme d'un nom au sens de l'[ingestion](ingestion.md#identifiants) : lettres, chiffres et `. _ - / : @ = + ~ % , # $ * [ ]`, ni blanc, ni balise, ni caractère invisible.

Le schéma est [`AgentExtension.json`](../spec/a2a/v1/AgentExtension.json). L'objet est fermé : un champ inconnu est refusé, ce qui interdit de glisser autre chose dans `params`. Un nouveau paramètre fera une nouvelle version.

## Ce que l'appelant envoie

L'appelant n'active l'extension qu'auprès d'un agent qui la déclare : il lui faut `callJob`. Il ouvre d'abord le span de son appel, comme pour tout appel tracé (W3C [Trace Context](https://www.w3.org/TR/trace-context/)), puis il ajoute à sa requête `SendMessage` :

- le paramètre de service `A2A-Extensions`, qui nomme l'URI, à côté des autres extensions qu'il active ;
- l'URI dans `message.extensions` ;
- son contexte sous la clé de l'URI dans `message.metadata`, deux champs et rien d'autre.

```json
{
  "message": {
    "messageId": "question-1",
    "role": "ROLE_USER",
    "parts": [{ "text": "Quel taux de change retenir pour les ventes de novembre ?" }],
    "extensions": ["https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1"],
    "metadata": {
      "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1": {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "runId": "a7e19836-59d9-8033-bd72-8a4bccb7271f"
      }
    }
  }
}
```

| Champ | Règle |
| --- | --- |
| `traceparent` | Le contexte de trace W3C de l'appel, celui du span qui fait l'appel : version `00`, `trace-id` (32 hexadécimaux) et `parent-id` (16), en minuscules, jamais nuls, drapeaux (2). 55 caractères exactement |
| `runId` | L'identifiant du run de l'appel : l'UUID que la [dérivation publiée](proxy.md#la-dérivation-à-loctet-près) (`answerlineage/run-id/v1`) donne pour le `trace-id` et le `parent-id` de `traceparent`, sous sa forme canonique en minuscules. Les [vecteurs](../spec/run-id/vectors-v1.json) la vérifient |

Le schéma est [`CallerContext.json`](../spec/a2a/v1/CallerContext.json). L'exemple suit celui de la page du proxy : ce `traceparent` donne cet identifiant.

Le `runId` se calcule à partir du `traceparent` ; il est envoyé quand même, pour que l'appelé, et la suite de conformité, vérifient que l'appelant parle bien du run qu'il a émis. Tout émetteur qui voit ce contexte, un point de capture placé entre les deux agents par exemple, en dérive le même run, et les écritures du graphe, idempotentes, fusionnent leurs événements.

Seule la version `00` du contexte est admise : une version future peut être suivie de champs libres, et un champ libre est l'endroit où un contenu se glisserait. Pour la même raison, ni `tracestate`, ni le job de l'appelant, ni aucun texte de la conversation ne sont envoyés.

Dans ses propres événements OpenLineage, l'appelant émet le run de l'appel : `run.runId` vaut `runId`, le job est `callJob`, et la facette `parent` le rattache au run de l'agent appelant. Il l'ouvre (`START`) avant d'envoyer la requête et le clôt (`COMPLETE` ou `FAIL`) à la réponse, comme un [adaptateur](conformite.md#les-deux-profils) le fait d'un appel d'outil.

## Ce que fait l'appelé

L'appelé ne lit le contexte que si la requête active l'extension par `A2A-Extensions`. Il le juge alors, dans cet ordre :

| Contexte reçu | `attachment` | `rejection` | Le run de l'appelé |
| --- | --- | --- | --- |
| absent de `message.metadata` | `missing` | | sans parent venu de l'appel |
| contraire au schéma : pas un objet, champ manquant ou inconnu, forme ou longueur fautive | `rejected` | `context_malformed` | sans parent venu de l'appel |
| `runId` différent de la dérivation | `rejected` | `run_id_not_derived` | sans parent venu de l'appel |
| conforme | `attached` | | rattaché au run de l'appel |

Rattacher, c'est porter, dans chaque événement OpenLineage du run de l'appelé, la facette standard `parent` : `run.runId` vaut le `runId` reçu, `job` est le `callJob` de sa propre carte.

```json
{
  "parent": {
    "_producer": "https://exemple.org/agent-des-taux",
    "_schemaURL": "https://openlineage.io/spec/facets/1-2-0/ParentRunFacet.json#/$defs/ParentRunFacet",
    "run": { "runId": "a7e19836-59d9-8033-bd72-8a4bccb7271f" },
    "job": { "namespace": "a2a://change", "name": "taux-du-mois" }
  }
}
```

Dans tous les cas :

- l'appel se déroule comme sans l'extension : aucun contexte, si fautif soit-il, ne fait échouer ni retarder la requête, et aucune erreur A2A n'est rendue pour le lignage. Le contrôle est un calcul local, sans entrée-sortie ;
- le run de l'appelé a son propre identifiant, un UUID canonique en minuscules ; il ne reprend jamais le `runId` reçu, qui est celui de l'appel ;
- la réponse nomme l'URI dans son paramètre `A2A-Extensions`. A2A ne le recommande que ; l'extension l'exige, pour que l'appelant sache que son activation a été lue.

L'appelé peut continuer la trace W3C à partir du `traceparent` reçu, ses propres spans en étant les enfants : c'est l'affaire de sa trace, pas du lignage.

## Ce que l'appelé renvoie

Le résumé se place sous la clé de l'URI dans le `metadata` de ce que l'appelé rend :

- une tâche (`SendMessageResponse.task`, ou `GetTask`) : `task.metadata` ;
- un message (`SendMessageResponse.message`) : `message.metadata`, et l'URI dans `message.extensions`.

Il dit ce que le run de l'appelé a lu jusque-là ; une tâche terminée porte le résumé final. La version 1 ne dit rien des événements d'un flux (`SendStreamingMessage`).

```json
{
  "runId": "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0d01",
  "attachment": "attached",
  "parentRunId": "a7e19836-59d9-8033-bd72-8a4bccb7271f",
  "datasets": [
    {
      "namespace": "bigquery",
      "name": "finance.ref.taux_change",
      "version": "2026-11-01",
      "precision": "columns"
    },
    { "namespace": "postgres://crm:5432", "name": "ventes.public.commandes", "precision": "table" }
  ],
  "datasetCount": 2,
  "unresolvedReads": 0,
  "truncated": false
}
```

| Champ | Obligatoire | Règle |
| --- | --- | --- |
| `runId` | oui | Le run de l'appelé, UUID canonique en minuscules |
| `attachment` | oui | `attached`, `missing` ou `rejected`, selon [le contexte reçu](#ce-que-fait-lappelé) |
| `parentRunId` | si `attached` | Le `runId` reçu, auquel l'appelé a rattaché son run. Absent sinon |
| `rejection` | si `rejected` | `context_malformed` ou `run_id_not_derived`. Absent sinon. Le motif est un code fixe : la valeur reçue n'est jamais recopiée |
| `datasets` | oui | Les jeux de données lus par le run de l'appelé et par ses runs enfants, 100 au plus |
| `datasets[].namespace` | oui | 512 caractères au plus |
| `datasets[].name` | oui | 1 024 caractères au plus |
| `datasets[].version` | non | La version lue, quand elle est connue (`datasetVersion` de la facette standard `version`), 255 caractères au plus |
| `datasets[].precision` | oui | Ce que l'on sait de la lecture : `table` (le jeu entier, ou rien de plus précis), `columns` (certaines colonnes), `subset` (certaines lignes), `keys` (les lignes de clés désignées) |
| `datasetCount` | oui | Le nombre de jeux de données lus, listés ou non : au moins la longueur de `datasets`. Un milliard au plus |
| `unresolvedReads` | oui | Les lectures dont l'appelé ne connaît pas le jeu de données (une lecture « non résolue » du [proxy](proxy.md#motifs-dune-lecture-non-résolue), par exemple) : elles manquent à `datasets` comme à `datasetCount`. Un milliard au plus |
| `truncated` | oui | `true` si et seulement si `datasets` ne liste pas tous les jeux comptés : `datasetCount` est plus grand que la longueur de `datasets` |

Le schéma est [`LineageSummary.json`](../spec/a2a/v1/LineageSummary.json). Il faut aussi :

- **un jeu de données par entrée** : une entrée par triplet espace de noms, nom et version ; un jeu lu sans version déclarée a son entrée à lui. Lu plusieurs fois, il a la précision de la [fusion](schema.md#fusion-de-deux-lectures) de ses lectures, comme dans le graphe et la [remontée](lecture.md#ce-qui-compte-comme-source) : la plus étroite des lectures qui les couvrent toutes, lignes et colonnes, celle qui ne fait écarter aucune réponse. L'appelé la calcule sur les colonnes lues, même s'il ne les transmet pas, et une lecture qui ne nomme pas ses colonnes les a toutes lues. Ainsi un sous-ensemble puis des clés, deux restrictions différentes des lignes, couvrent toutes les lignes : `columns` quand les deux lectures nomment leurs colonnes, `table` sinon ;
- **des noms qui ont la forme d'un nom** : `namespace`, `name` et `version` suivent la règle des [identifiants](ingestion.md#identifiants) de l'ingestion, comme `callJob` ;
- **une liste bornée** : au-delà de 100 jeux, l'appelé en liste 100, compte le reste dans `datasetCount` et dit `truncated`. Il devrait les ranger par espace de noms, nom puis version, pour qu'un même run donne le même résumé. Il peut en lister moins qu'il n'en connaît, aucun s'il ne veut pas révéler ses noms : la troncature est dite, et le compte reste juste.

Le résumé est fermé : un champ inconnu est refusé. Il ne porte aucun contenu, ni question, ni résultat d'outil, ni réponse : des identifiants, des codes fixes et des nombres. Une donnée tirée d'un contenu ne pourrait y figurer que sous forme d'empreinte ; la version 1 n'en transporte aucune.

## Ce que l'appelant fait du résumé

Un résumé est une entrée hostile, écrite par un autre agent. L'appelant :

- n'en lit un que si la réponse nomme l'URI dans `A2A-Extensions` ;
- le valide en entier, schéma et forme des noms : un résumé qui n'y satisfait pas est écarté en entier, sans en recopier une partie, et l'appel n'échoue pas pour autant ;
- vérifie que `parentRunId`, quand il est présent, est son `runId`, et que `runId` ne l'est pas ; sinon le résumé ne concerne pas son appel ;
- peut porter ces jeux de données en entrées (`inputs`) du run de son appel, avec leur version (facette standard `version`). La propagation d'un incident atteint alors sa réponse, même quand les événements de l'appelé ne parviennent pas à son AnswerLineage. Une précision plus fine que `table` ne se transmet pas : la lecture vaut pour le jeu entier, ce qui ne fait écarter aucune réponse ;
- ne s'en sert jamais pour écarter une réponse : un jeu absent du résumé ne prouve pas que la réponse n'en dépend pas (voir [Sécurité](#sécurité)).

## Erreurs

Le lignage ne rend jamais d'erreur A2A et ne fait jamais échouer un appel. Chaque écart a sa conduite :

| Situation | Ce que fait l'appelé | Ce que voit l'appelant |
| --- | --- | --- |
| L'appelé ne prend pas l'extension en charge | Il ignore l'activation, comme A2A le prévoit : ni écho, ni résumé | Une réponse ordinaire. Son appel reste sans lignage en aval ; rien ne l'en avertit d'autre que l'absence d'écho |
| Une autre version de l'extension est demandée | Il l'ignore, sans se rabattre sur la version 1 | Comme ci-dessus |
| Le contexte est envoyé sans que l'extension soit activée | Il l'ignore : ni rattachement, ni écho, ni résumé | Comme ci-dessus |
| L'extension est activée sans contexte | `attachment` vaut `missing` | Un résumé, sans rattachement |
| Le contexte est contraire au schéma ou hors bornes | `attachment` vaut `rejected`, `rejection` vaut `context_malformed` | Un résumé, sans rattachement, et le motif |
| Le `runId` ne dérive pas du `traceparent` | `attachment` vaut `rejected`, `rejection` vaut `run_id_not_derived` | Comme ci-dessus |
| Le résumé reçu est invalide | | L'appelant l'écarte en entier, sans échouer |

Aucun de ces cas ne recopie la valeur reçue : ni dans le résumé, dont les motifs sont des codes fixes, ni dans les journaux de l'appelé, qui ne disent que le code. L'extension n'est jamais marquée `required` : l'erreur `-32008` d'A2A ne peut donc pas venir d'elle.

## Sécurité

**Ce qu'un résumé prouve.** Rien de plus que la parole de l'appelé. La version 1 n'est pas signée : le transport (TLS, l'authentification d'A2A) dit quel agent a répondu, pas que ce qu'il déclare est vrai. Un résumé dit que l'appelé prétend avoir rattaché son run à l'appel et lu ces jeux de données.

**Ce qu'il ne prouve pas.** Que la liste est complète : elle peut être tronquée, des lectures peuvent être non résolues, et l'appelé peut omettre ou mentir. Que l'appelé a émis les événements qu'il annonce. Que ses noms désignent les jeux de données que l'appelant croit. Un résumé sert donc à ajouter des dépendances, jamais à en retirer : l'analyse d'impact se trompe du côté sûr, en gardant une réponse de trop plutôt qu'en en écartant une touchée.

**Ce qu'un contexte permet.** Le contexte de trace n'est pas un secret, et la dérivation n'authentifie rien : quiconque en connaît un, l'appelé qui l'a reçu ou un tiers qui l'a lu dans des traces, peut en dériver le run et y accrocher les siens. Leurs lectures s'ajoutent alors aux sources de l'appelant, ce qui fait rappeler plus, jamais moins.

**Comment il est borné.** Les trois objets sont fermés, chaque texte a une longueur maximale, la liste 100 entrées, chaque nombre un milliard : un résumé valide compte moins de 200 000 caractères quoi qu'il arrive, et se valide en une passe. Un appelant ne lit rien avant d'avoir validé le tout.

**Pourquoi aucun contenu.** AnswerLineage ne stocke, ne journalise ni ne met en métrique aucun contenu. Le résumé franchit la frontière entre deux équipes, parfois deux organisations : ce qu'il porterait finirait dans les événements, les journaux et le graphe de l'appelant, hors du contrôle de l'appelé. Un texte libre y serait aussi une instruction possible pour le modèle de l'appelant ; un nom, qui n'a ni blanc ni balise, ne peut pas être une phrase.

**Ce que l'appelé révèle.** Les noms de ses jeux de données, à l'appelant. C'est son choix : il peut n'en lister aucun, et dire la troncature.

Une version signée de l'extension aurait une autre URI.

## La suite de conformité

Le [kit de conformité](conformite.md) juge l'extension avec la commande `a2a`, à partir de deux fichiers, sans réseau :

```bash
uv run answerlineage-conformance a2a --card carte.json --exchange echange.json
uv run answerlineage-conformance a2a --card carte.json --exchange echange.json --format json
```

| Code de sortie | Sens |
| --- | --- |
| `0` | Conforme : aucun problème |
| `1` | Non conforme : le rapport liste les problèmes |
| `2` | Entrée illisible ou usage fautif : aucun verdict n'est rendu, le motif est sur la sortie d'erreur |

La carte et l'échange se lisent comme les fichiers de `check` : JSON strict, sans `NaN`, sans clé en double, 256 Mio au plus, non compressé.

### L'échange capturé

L'Agent Card est le document JSON que sert l'appelé. L'échange est un objet qui porte la requête de l'appelant et la réponse de l'appelé, sous leur forme JSON d'A2A, quelle que soit la liaison :

| Champ | Contenu |
| --- | --- |
| `request.serviceParameters` | Les paramètres de service de la requête, nom et valeur : en JSON-RPC et en HTTP+JSON, les en-têtes HTTP, au moins `A2A-Extensions`. Un en-tête répété s'écrit une fois, ses valeurs jointes par des virgules |
| `request.params` | La `SendMessageRequest` envoyée. En JSON-RPC, c'est le `params` de la requête |
| `response.serviceParameters` | Les paramètres de service de la réponse |
| `response.result` | La `SendMessageResponse` rendue : un objet qui porte `task` ou `message`. Pour une tâche relue par `GetTask`, `{"task": …}` |

Celui des [jouets](#la-preuve-par-deux-implémentations) :

```json
{
  "request": {
    "serviceParameters": {
      "A2A-Version": "1.0",
      "A2A-Extensions": "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1"
    },
    "params": {
      "message": {
        "messageId": "question-1",
        "role": "ROLE_USER",
        "parts": [{ "text": "Quel taux de change retenir pour les ventes de novembre ?" }],
        "extensions": ["https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1"],
        "metadata": {
          "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1": {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "runId": "a7e19836-59d9-8033-bd72-8a4bccb7271f"
          }
        }
      }
    }
  },
  "response": {
    "serviceParameters": {
      "A2A-Extensions": "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1"
    },
    "result": {
      "task": {
        "id": "tache-1",
        "contextId": "contexte-1",
        "status": { "state": "TASK_STATE_COMPLETED" },
        "artifacts": [
          {
            "artifactId": "reponse",
            "parts": [{ "text": "Le taux retenu pour novembre est de 1,0842 dollar pour un euro." }]
          }
        ],
        "metadata": {
          "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1": {
            "runId": "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0d01",
            "attachment": "attached",
            "parentRunId": "a7e19836-59d9-8033-bd72-8a4bccb7271f",
            "datasets": [
              {
                "namespace": "bigquery",
                "name": "finance.ref.taux_change",
                "version": "2026-11-01",
                "precision": "columns"
              }
            ],
            "datasetCount": 1,
            "unresolvedReads": 0,
            "truncated": false
          }
        }
      }
    }
  }
}
```

### Ce que la suite vérifie

Cinq groupes de contrôles, tous exécutés à chaque fois. Chaque contrôle a un code stable. La suite juge les deux côtés : ce que l'appelant a envoyé, et ce que l'appelé en a fait. Elle recalcule elle-même, par la dérivation publiée, le rattachement que le contexte reçu commandait, et le compare à celui que dit le résumé. Elle ne juge la réponse que si la requête a activé l'extension.

#### Version

| Code | Ce qui le déclenche |
| --- | --- |
| `other_extension_version` | Une URI qui commence comme celle de l'extension sans être celle de la version 1, dans la carte, un paramètre `A2A-Extensions`, les `extensions` ou les clés du `metadata` d'un message : une autre version, ou une faute de frappe |

#### Déclaration

| Code | Ce qui le déclenche |
| --- | --- |
| `extension_not_declared` | La carte ne déclare pas l'URI dans `capabilities.extensions` |
| `extension_declared_twice` | La carte la déclare plusieurs fois : ses paramètres seraient ambigus |
| `declaration_schema_violation` | La déclaration ne respecte pas `AgentExtension.json` : `params` ou `callJob` manquant, `required` à `true`, champ inconnu… Le motif est le mot-clé de JSON Schema violé |

#### Appel

| Code | Ce qui le déclenche |
| --- | --- |
| `extension_not_activated` | `A2A-Extensions` de la requête ne nomme pas l'URI |
| `context_missing` | L'extension est activée, et `message.metadata` ne porte pas de contexte sous la clé de l'URI |
| `context_schema_violation` | Le contexte ne respecte pas `CallerContext.json`. Le motif est le mot-clé violé ; pour un champ manquant, le pointeur désigne ce champ |
| `context_run_id_not_derived` | Le `runId` n'est pas celui que la dérivation donne pour le `traceparent` |
| `context_not_listed` | Le message porte le contexte sans nommer l'URI dans `message.extensions` |

#### Réponse

| Code | Ce qui le déclenche |
| --- | --- |
| `extension_not_echoed` | `A2A-Extensions` de la réponse ne nomme pas l'URI |
| `summary_missing` | La tâche ou le message rendu ne porte pas de résumé, ou la réponse ne rend ni l'un ni l'autre |
| `summary_schema_violation` | Le résumé ne respecte pas `LineageSummary.json` : trop de jeux de données, texte trop long, champ inconnu, `parentRunId` sans rattachement… Le motif est le mot-clé violé |
| `summary_attachment_wrong` | Le rattachement que dit le résumé n'est pas celui que commande le contexte reçu. Motif : `expected_attached`, `expected_missing` ou `expected_rejected` (ce qui était attendu), `wrong_parent`, `wrong_rejection`, ou `run_id_reused` (l'appelé a pris le run de l'appel pour le sien) |
| `summary_inconsistent` | Le résumé se contredit. Motif : `dataset_repeated` (un jeu listé deux fois), `count_below_listed` (`datasetCount` inférieur à la liste), `truncated_mismatch` (`truncated` ne dit pas si la liste est complète) |
| `summary_not_listed` | Le message rendu porte le résumé sans nommer l'URI dans `message.extensions` |

#### Contenu

| Code | Ce qui le déclenche |
| --- | --- |
| `identifier_rejected` | Un nom de l'extension (`callJob`, `namespace`, `name` ou `version` d'un jeu de données) n'a pas la forme d'un nom. Le motif est le code de l'ingestion : `identifier_whitespace` pour une phrase, `identifier_markup`, `identifier_invisible`… |
| `content_in_extension` | Un texte échangé se retrouve tel quel dans le contexte ou le résumé, clé ou valeur : le texte d'une partie (`text`, ou un texte de `data`) de la question, de la réponse, de l'historique ou d'un artefact, d'au moins 12 caractères |

### Le rapport

Le rapport dit la suite, l'extension, le verdict, puis les problèmes, chacun avec le fichier et un pointeur JSON (RFC 6901) vers le champ. Comme celui de `check`, il ne recopie aucune valeur reçue : un motif est un code fixe, et une clé qui n'a pas la forme d'un nom, ou qui contient un texte échangé, est remplacée par `<clé masquée>`. Les mêmes fichiers, sous les mêmes chemins, donnent le même rapport, à l'octet près : il ne dépend d'aucune heure.

```text
Kit de conformité AnswerLineage 0.0.0
Suite : extension A2A, version 1
Extension : https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1
Verdict : NON CONFORME

Problèmes : 1
  [réponse] summary_schema_violation : echange.json, /response/result/task/metadata/https:~1~1github.com~1alebgl77~1AnswerLineage~1tree~1main~1spec~1a2a~1v1/datasets (maxItems)

Ce que disent ces codes
  summary_schema_violation : Le résumé ne respecte pas le schéma publié ; le motif est la règle violée.

Contrôles exécutés : 17
  …

Fichiers fouillés
  Agent Card  carte.json  598 octets  sha256:…
  échange  echange.json  2911 octets  sha256:…
```

`--format json` rend le même contenu sous une forme stable :

```json
{
  "report_version": 1,
  "kit": { "name": "answerlineage-conformance", "version": "0.0.0" },
  "suite": "a2a",
  "extension": "https://github.com/alebgl77/AnswerLineage/tree/main/spec/a2a/v1",
  "verdict": "non_conformant",
  "files": [
    {
      "role": "card",
      "path": "carte.json",
      "bytes": 598,
      "sha256": "0c4d9b6f5a3e8d27b1c6a4f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0"
    }
  ],
  "controls": [{ "code": "other_extension_version", "group": "version", "problems": 0 }],
  "problem_count": 1,
  "truncated": false,
  "problems": [
    {
      "code": "summary_schema_violation",
      "group": "response",
      "message": "Le résumé ne respecte pas le schéma publié ; le motif est la règle violée.",
      "file": "echange.json",
      "pointer": "/response/result/task/metadata/https:~1~1github.com~1alebgl77~1AnswerLineage~1tree~1main~1spec~1a2a~1v1/datasets",
      "reason": "maxItems"
    }
  ]
}
```

| Champ | Contenu |
| --- | --- |
| `report_version` | Version de cette forme : elle change quand un champ change de sens ou disparaît |
| `kit` | Nom et version du kit |
| `suite` | `a2a` |
| `extension` | L'URI de la version jugée |
| `verdict` | `conformant` ou `non_conformant` |
| `files` | La carte (`card`) puis l'échange (`exchange`) : chemin tel qu'il a été donné, taille en octets, empreinte SHA-256 |
| `controls` | Les contrôles exécutés, dans l'ordre de cette page, avec le nombre de problèmes de chacun |
| `problem_count` | Nombre de problèmes |
| `truncated` | `true` quand `problems` n'en liste que les 1 000 premiers |
| `problems` | Les problèmes, par groupe, puis par fichier et par pointeur : `code`, `group` (`version`, `declaration`, `call`, `response`, `content`), `message` (texte fixe du contrôle), `file`, `pointer`, et le motif (`reason`), `null` quand il est sans objet |

### Ce que la suite ne prouve pas

- **Les événements.** Elle juge ce que les agents se sont dit, pas ce qu'ils ont émis vers AnswerLineage. Le rattachement par la facette `parent` se vérifie avec `check`, sous le profil `adapter`, sur la capture réunie des événements des deux agents : sans lui, la chaîne de la table lue à la réponse diffusée reste incomplète (`no_complete_chain`).
- **Un échange, pas tous.** Un appelé qui ne se trompe que sur un contexte invalide n'est vu que si l'échange en porte un. Jouer aussi un appel sans contexte et un appel au contexte fautif, et juger chaque échange.
- **Un contenu transformé.** Seule la copie exacte d'un texte échangé est cherchée ; un extrait, une traduction ou un encodage passeraient. La forme des noms et la fermeture des objets restent la vraie garde.
- **La vérité du résumé.** Que l'appelé ait lu ce qu'il dit, et rien d'autre, ne se voit pas dans l'échange. Ni que la précision d'un jeu lu plusieurs fois soit la fusion de ses lectures : l'échange n'en porte que le résultat, et la suite n'en vérifie que la valeur admise et l'unicité de l'entrée (`dataset_repeated`).

### La preuve par deux implémentations

Les tests du kit écrivent un appelant et un appelé jouets à partir de cette seule page et des schémas publiés, sans rien importer d'AnswerLineage (un test le vérifie) : la dérivation y est refaite d'après la page du proxy, la fusion des lectures d'après celle du [schéma du graphe](schema.md#fusion-de-deux-lectures), les schémas appliqués avec `jsonschema`. Un test tient cette fusion d'accord avec celle du graphe (`merge_reads`), sur toutes les paires de précisions, avec et sans colonnes, et sur des triplets. Leurs échanges passent la suite, et les événements qu'ils émettent passent `check` avec une chaîne complète, à travers l'appel. Des mutations (un paramètre manquant, un résumé trop long, un contenu dans le résumé, l'URI d'une autre version, une extension non déclarée) sont chacune nommées par la suite.

## Versions

Les fichiers de `spec/a2a/v1/` ne changent plus, pas d'un octet : `make contract` refuse un fichier publié sur la branche cible qui a changé ou disparu. Le kit en embarque une copie identique, qu'un test tient à l'octet près.

Toute évolution, même l'ajout d'un champ facultatif, publie une nouvelle version : un nouveau dossier (`spec/a2a/v2/`) et une nouvelle URI. Les objets étant fermés, un ajout casserait un lecteur de la version 1. Un agent peut déclarer plusieurs versions ; un appelant active celle qu'il connaît. La version 1 reste servie.

## Limites connues

- Les flux (`SendStreamingMessage`, `SubscribeToTask`) ne sont pas couverts : le résumé n'est défini que sur la tâche ou le message rendu.
- Le résumé n'est pas signé.
- L'appelé ne transmet pas les colonnes lues : `columns` dit qu'une partie seulement des colonnes l'a été, pas lesquelles, et `subset` ou `keys` ne disent pas si toutes l'ont été.
- La version 1 ne connaît qu'un appel direct : un appelé qui en appelle un autre compte les lectures de ce dernier dans les siennes s'il les connaît (par son propre résumé reçu), sans dire par qui elles ont été faites.
- Aucun adaptateur A2A n'est encore fourni par AnswerLineage : cette page et la suite sont le contrat qu'un adaptateur devra remplir.
