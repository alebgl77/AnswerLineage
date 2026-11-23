"""La diffusion d'une réponse d'agent : son canal, la référence du message, sa facette.

La réponse diffusée est une sortie du run de l'agent, décrite par son empreinte
à clé ; la facette ``answerlineage_delivery`` dit où elle est partie (le canal
et la référence du message) et à qui (des pseudonymes). Ni le texte de la
réponse, ni sa taille, ni l'identité d'un destinataire n'y entrent.

Canal et référence viennent d'une messagerie, par l'intégration : ils ne sont
recopiés que s'ils ont une forme admise, plus étroite que celle d'un nom pour
l'ingestion, pour qu'une diffusion acceptée ici ne fasse jamais rejeter son
événement :

- le canal est un nom court, en minuscules : ``slack``, ``teams``, ``email`` ;
- sur Slack, un message se désigne par l'identifiant de son canal
  (``C``, ``G`` ou ``D`` suivi de 8 à 20 majuscules ou chiffres) et son
  horodatage (``ts``, dix chiffres, un point, six chiffres) :
  ``C024BE91L/1355517523.000005``. Ni le nom du canal, ni l'espace de travail,
  ni le fil, ni le lien du message n'en sont gardés ;
- sur un autre canal, la référence a la forme d'un nom : lettres et chiffres
  ASCII, et les signes ``. _ - : / @ = + ~ % , #``, sans mot de passe.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from answerlineage_emitter.events import DELIVERY_FACET, DELIVERY_FACET_SCHEMA, facet

# Bornes de l'ingestion, en caractères.
CHANNEL_MAX: Final = 64
MESSAGE_REF_MAX: Final = 512
# Destinataires d'une diffusion, au plus : la borne du schéma publié et de l'ingestion.
RECIPIENTS_MAX: Final = 10_000
SLACK: Final = "slack"
# Ce que l'événement dit d'un canal ou d'une référence sans forme admise : rien de reçu.
# Une référence ne se lit pas sans son canal : un canal non résolu l'emporte.
UNRESOLVED_CHANNEL: Final = "canal-non-resolu"
UNRESOLVED_MESSAGE: Final = "message-non-resolu"

_CHANNEL: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SLACK_MESSAGE: Final = re.compile(r"[CDG][A-Z0-9]{8,20}/[0-9]{10}\.[0-9]{6}")
_MESSAGE_REF: Final = re.compile(r"[A-Za-z0-9._:/@=+~%,#-]{1,512}")
# Un mot de passe devant l'hôte d'une adresse, que l'ingestion refuse.
_CREDENTIALS: Final = re.compile(r"://[^/@]*:[^/@]*@")


def channel_name(value: object) -> str | None:
    """Le canal, en minuscules, s'il a la forme d'un nom court ; ``None`` sinon."""
    if not isinstance(value, str) or len(value) > CHANNEL_MAX:
        return None
    lowered = value.lower()
    return lowered if _CHANNEL.fullmatch(lowered) else None


def slack_message_ref(channel_id: object, ts: object) -> str:
    """La référence d'un message Slack : l'identifiant de son canal et son horodatage, tels que
    ``chat.postMessage`` les rend (``channel``, ``ts``). Elle n'est contrôlée qu'à la diffusion."""
    if not isinstance(channel_id, str) or not isinstance(ts, str):
        return ""
    return f"{channel_id}/{ts}"


def message_ref(channel: str, value: object) -> str | None:
    """La référence du message, si elle a la forme admise sur ce canal ; ``None`` sinon."""
    if not isinstance(value, str) or len(value) > MESSAGE_REF_MAX:
        return None
    if channel == SLACK:
        return value if _SLACK_MESSAGE.fullmatch(value) else None
    if not _MESSAGE_REF.fullmatch(value) or _CREDENTIALS.search(value):
        return None
    return value


def delivery_output(
    producer: str,
    dataset: Mapping[str, str],
    *,
    fingerprint: str,
    channel: str,
    message_ref: str,
    recipients: Sequence[str],
) -> dict[str, Any]:
    """La réponse diffusée : une sortie que décrit la facette de diffusion. Sans destinataire
    connu, la facette ne dit rien d'eux."""
    fields: dict[str, Any] = {
        "fingerprint": fingerprint,
        "channel": channel,
        "messageRef": message_ref,
    }
    if recipients:
        fields["recipients"] = list(recipients)
    return {
        **dataset,
        "outputFacets": {DELIVERY_FACET: facet(producer, DELIVERY_FACET_SCHEMA, **fields)},
    }
