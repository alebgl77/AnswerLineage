# Diffusion des périmètres en quarantaine, version 1

L'API signe et date chaque réponse de `GET /api/v1/quarantine`, `200` comme `304`, dans l'en-tête `AnswerLineage-Signature`, sous un secret qu'elle partage avec les proxys. Un proxy qui connaît le secret n'adopte qu'une diffusion dont la signature est juste et l'heure récente : un tiers ne peut ni lui faire croire à une quarantaine, ni en prolonger une levée en rejouant une réponse ancienne. Le réglage, la vérification et ce qu'elle garantit sont dans [`docs/incidents.md`](../../../docs/incidents.md#signée-et-datée) et [`docs/proxy.md`](../../../docs/proxy.md#une-diffusion-signée-et-datée).

| Fichier | Ce qu'il décrit |
| --- | --- |
| [`vectors.json`](vectors.json) | Des signatures produites, et les décisions qu'un proxy doit prendre : diffusions valides, `304` sur la version tenue, rotation du secret, en-têtes illisibles, signatures fausses, corps altérés, réponses rejouées hors de la tolérance ou venues du futur |

Le message signé commence par l'étiquette `answerlineage/quarantine/v1` et un octet nul : une signature d'un autre domaine, celle d'un webhook par exemple, ne vaut jamais pour une diffusion. La charge d'un `200` est son corps, octet pour octet ; celle d'un `304`, qui n'a pas de corps, est la version que le proxy tient : un `304` ne vaut que pour cette version et que pendant la tolérance.

La signature est écrite deux fois, sans que l'un dépende de l'autre : par l'API, qui signe ([`signature.py`](../../../api/src/answerlineage_api/incidents/signature.py)), et par le proxy, qui vérifie ([`quarantine_signature.py`](../../../proxy/src/answerlineage_proxy/quarantine_signature.py)). Les vecteurs ont été calculés sans l'une ni l'autre, en bibliothèque standard ; un test les recalcule de même, et juge les deux écritures dessus.
