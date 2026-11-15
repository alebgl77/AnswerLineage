"""Envoi à l'API d'ingestion d'AnswerLineage : les événements lot par lot, puis les relevés."""

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
COVERAGE_PATH = "/api/v1/coverage"


class LoadError(Exception):
    """L'envoi a échoué : l'API est injoignable ou a refusé un lot."""


@dataclass(frozen=True)
class LoadResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0


@dataclass(frozen=True)
class ReportResult:
    accepted: int = 0
    rejected: int = 0


def _api_url(base_url: str, path: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise LoadError("L'adresse de l'API doit commencer par http:// ou https://.")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}{path}"


def batch_url(base_url: str) -> str:
    """Adresse du point d'entrée des lots, à partir de l'adresse de l'API."""
    return _api_url(base_url, BATCH_PATH)


def _post(url: str, payload: Any, timeout: float) -> Any:
    """Envoie un document JSON et rend la réponse lue ; lève ``HTTPError`` sur un refus."""
    request = urllib.request.Request(  # noqa: S310 — le schéma est vérifié par _api_url.
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read())
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise LoadError(f"L'API est injoignable ou sa réponse est illisible ({url}).") from error


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
        try:
            report = _post(url, events[offset : offset + batch_size], timeout)
        except urllib.error.HTTPError as error:
            raise LoadError(f"L'API a refusé un lot (HTTP {error.code}).") from error
        accepted += int(report["accepted"])
        duplicates += int(report["duplicates"])
        rejected += len(report["rejected"])
    return LoadResult(accepted=accepted, duplicates=duplicates, rejected=rejected)


def send_reports(
    reports: Sequence[dict[str, Any]], base_url: str, *, timeout: float = 30.0
) -> ReportResult:
    """Envoie les relevés de couverture un à un ; un relevé refusé (422) est compté."""
    url = _api_url(base_url, COVERAGE_PATH)
    accepted = rejected = 0
    for report in reports:
        try:
            _post(url, report, timeout)
        except urllib.error.HTTPError as error:
            if error.code != 422:
                raise LoadError(f"L'API a refusé un relevé (HTTP {error.code}).") from error
            rejected += 1
        else:
            accepted += 1
    return ReportResult(accepted=accepted, rejected=rejected)
