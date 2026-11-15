"""Envoi des événements à l'API d'ingestion d'AnswerLineage, lot par lot."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

DEFAULT_BATCH_SIZE = 500
BATCH_PATH = "/api/v1/lineage/batch"


class LoadError(Exception):
    """L'envoi a échoué : l'API est injoignable ou a refusé un lot."""


@dataclass(frozen=True)
class LoadResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0


def batch_url(base_url: str) -> str:
    """Adresse du point d'entrée des lots, à partir de l'adresse de l'API."""
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise LoadError("L'adresse de l'API doit commencer par http:// ou https://.")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}{BATCH_PATH}"


def send(
    events: Sequence[dict[str, Any]],
    base_url: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout: float = 30.0,
) -> LoadResult:
    """Envoie les événements par lots et additionne les bilans de l'API."""
    if batch_size < 1:
        raise LoadError("Un lot contient au moins un événement.")
    url = batch_url(base_url)
    accepted = duplicates = rejected = 0
    for offset in range(0, len(events), batch_size):
        request = urllib.request.Request(  # noqa: S310 — le schéma est vérifié par batch_url.
            url,
            data=json.dumps(events[offset : offset + batch_size]).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                report = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise LoadError(f"L'API a refusé un lot (HTTP {error.code}).") from error
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise LoadError(
                f"L'API est injoignable ou sa réponse est illisible ({url})."
            ) from error
        accepted += int(report["accepted"])
        duplicates += int(report["duplicates"])
        rejected += len(report["rejected"])
    return LoadResult(accepted=accepted, duplicates=duplicates, rejected=rejected)
