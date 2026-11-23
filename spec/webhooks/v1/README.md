# Webhooks d'AnswerLineage, version 1

Les workers annoncent chaque changement d'état d'un incident au récepteur que règle l'exploitant : un message JSON par requête `POST`, signé avec son heure d'émission dans l'en-tête `AnswerLineage-Signature`. Le réglage, le format, la vérification et les garanties sont dans [`docs/webhooks.md`](../../../docs/webhooks.md).

| Fichier | Ce qu'il décrit |
| --- | --- |
| [`Message.json`](Message.json) | Le corps d'un message : un gabarit fixe, sans texte libre ni contenu |
| [`vectors.json`](vectors.json) | Des signatures produites, et les décisions qu'un récepteur doit prendre : messages valides, rejoués hors de la tolérance, signatures fausses, corps altérés, rotation du secret |

La fonction de vérification de référence, en bibliothèque standard de Python, est [`webhook_signature.py`](../../../workers/src/answerlineage_workers/webhook_signature.py) : elle se copie telle quelle. Une vérification écrite dans un autre langage se juge sur les vecteurs.

Les fichiers de ce dossier ne changent plus. Une évolution du format publie une autre version, dans un autre dossier.
