# 0014. Règles de lecture des outils non SQL : un fichier YAML déclaratif

Statut : en vigueur.

## Contexte

Un outil d'API, de fichiers ou de recherche documentaire ne dit pas ce qu'il lit, et aucun SQL ne le trahit. Sans description, la lecture de l'appel est « non résolue », motif `undescribed_tool` : le lignage sait qu'un outil a été appelé, pas sur quoi. Le proxy ne peut pas le deviner, et ne doit pas : décrire à partir d'une lecture douteuse ferait entrer dans le graphe une lecture que le serveur n'a pas faite.

Quelqu'un doit donc le déclarer : quel outil, sur quel serveur, quels jeux de données, quelles colonnes, quel argument porte la clé lue. Ce quelqu'un est l'exploitant d'un proxy, qui écrit ce fichier à côté de sa configuration.

## Décision

Les règles sont déclaratives, dans un fichier YAML donné par `--read-rules-file` ([Proxy MCP](../proxy.md#règles-de-lecture-des-outils-non-sql)) :

```yaml
version: 1
rules:
  - tool: get_price
    server: catalogue
    datasets:
      - namespace: postgres://referentiel:5432
        name: ref.tarifs
        columns: [produit, prix]
    key: produit
```

Le fichier est une entrée hostile comme une autre : sa taille est bornée avant toute lecture (256 Kio, 1 000 règles, 8 niveaux) ; il est lu par le chargeur sûr de PyYAML, qui refuse ancres, alias et étiquettes explicites avant de rien composer, et ne construit aucun objet Python : le document est parcouru nœud par nœud (`proxy/src/answerlineage_proxy/rules.py`) ; une valeur qui n'a pas le type attendu (une date là où un nom est attendu) est refusée, pas convertie ; un champ inconnu, répété ou absent est refusé ; une erreur nomme la règle, le champ et la position, jamais une valeur. Une règle invalide empêche le proxy de démarrer : on ne capte pas à moitié en silence. Le fichier ne se relit pas à chaud.

La clé lue est une valeur d'argument : elle n'est jamais conservée telle quelle, elle devient une empreinte à clé ([décision 0004](0004-aucun-contenu-empreintes-a-cle.md)).

## Alternative écartée

**Un format de la bibliothèque standard** : TOML (`tomllib`) ou JSON.

Ils auraient évité une dépendance, PyYAML, et un chargeur à durcir : `tomllib` et `json` ne construisent pas d'objet arbitraire, n'ont ni alias ni étiquettes, et se lisent déjà sans rien installer. Le proxy est un composant que l'on veut léger, et chaque dépendance est une surface de plus.

YAML est pourtant le format dans lequel les exploitants d'un déploiement conteneurisé écrivent déjà le reste de leur configuration (les fichiers compose, les définitions d'orchestration), accepte les commentaires qui disent pourquoi une règle existe, et exprime sans lourdeur des listes de listes (un jeu de données par règle, des colonnes par jeu). Le coût est assumé et borné : la dépendance est déclarée dans `proxy/pyproject.toml`, et le chargeur sûr refuse tout ce qui serait exécutable ou amplifiable, ce que des tests éprouvent.

## Conséquences

- Le proxy dépend de PyYAML ; c'est la seule dépendance du proxy qui ne serve qu'à un fichier de configuration.
- Le fichier est une entrée à durcir à part entière : bornes, chargeur sûr, types stricts, messages sans valeur. Tout changement du chargeur doit garder ces propriétés.
- Une règle se fie à l'outil : si l'outil lit autre chose que ce qu'elle déclare, le lignage le croit. Elle ne dit pas non plus quelles lignes d'un jeu de données correspondent à une clé : l'empreinte rapproche des appels, pas des lignes.
- Les règles ne se rechargent pas à chaud : un changement demande de relancer le proxy.
- Une règle peut viser un serveur ; celle qui nomme le serveur l'emporte sur une règle sans serveur, et un même fichier peut servir plusieurs proxys. Un outil déclaré par `--sql-tool` garde l'analyse de sa requête : sa règle est ignorée, et le proxy le dit au démarrage.
- Les outils appelés sans règle ni SQL sont listés par l'API (`GET /api/v1/ingestion/undescribed-tools`) pour dire à l'exploitant quelles règles écrire.
- Les noms de jeux de données et de colonnes d'une règle suivent la forme des identifiants de l'ingestion, écrite une seconde fois dans le proxy : un test de contrat les tient d'accord.
