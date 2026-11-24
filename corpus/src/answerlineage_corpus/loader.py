"""Envoi à l'API d'ingestion d'AnswerLineage : les événements lot par lot, puis les relevés.

L'API demande un jeton porteur OIDC, celui d'un émetteur. Le jeton se lit de
``ANSWERLINEAGE_TOKEN`` ou du fichier que nomme ``ANSWERLINEAGE_TOKEN_FILE``
(``token_from_environment``), jamais de la ligne de commande ; il ne part qu'en
https, ou en http vers la boucle locale, et aucun message ne le cite. La
démonstration, dont l'authentification est désactivée, n'en demande pas.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_BATCH_SIZE = 500
BATCH_PATH = "/api/v1/lineage/batch"
COVERAGE_PATH = "/api/v1/coverage"
ENV_TOKEN = "ANSWERLINEAGE_TOKEN"  # noqa: S105
ENV_TOKEN_FILE = "ANSWERLINEAGE_TOKEN_FILE"  # noqa: S105
# Un jeton porteur (RFC 6750, 2.1), au plus de la taille que l'API admet.
MAX_TOKEN_LENGTH = 16 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


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


def token_from_environment(environment: Mapping[str, str]) -> str | None:
    """Le jeton de ``ANSWERLINEAGE_TOKEN`` ou du fichier de ``ANSWERLINEAGE_TOKEN_FILE`` ;
    ``None`` sans jeton. Aucun message ne cite le jeton ni le chemin du fichier."""
    path = environment.get(ENV_TOKEN_FILE) or None
    text = environment.get(ENV_TOKEN) or None
    if path is not None and text is not None:
        raise LoadError(f"Le jeton vient de {ENV_TOKEN_FILE} ou de {ENV_TOKEN}, pas des deux.")
    if path is not None:
        try:
            with Path(path).open("rb") as source:
                text = source.read(MAX_TOKEN_LENGTH + 2).decode("ascii", errors="replace").strip()
        except OSError:
            raise LoadError("Le fichier du jeton est illisible.") from None
    if text is not None and (len(text) > MAX_TOKEN_LENGTH or not _TOKEN.fullmatch(text)):
        raise LoadError("Le jeton est illisible : un jeton porteur, sans préfixe « Bearer ».")
    return text


def _api_url(base_url: str, path: str, token: str | None = None) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise LoadError("L'adresse de l'API doit commencer par http:// ou https://.")
    host = parts.hostname or ""
    loopback = host in _LOOPBACK or host.startswith("127.")
    if token is not None and parts.scheme != "https" and not loopback:
        raise LoadError("Le jeton ne part qu'en https, ou en http vers la boucle locale.")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}{path}"


def batch_url(base_url: str) -> str:
    """Adresse du point d'entrée des lots, à partir de l'adresse de l'API."""
    return _api_url(base_url, BATCH_PATH)


def _post(url: str, payload: Any, timeout: float, token: str | None = None) -> Any:
    """Envoie un document JSON et rend la réponse lue ; lève ``HTTPError`` sur un refus."""
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 — le schéma est vérifié par _api_url.
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
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
    token: str | None = None,
) -> LoadResult:
    """Envoie les événements par lots et additionne les bilans de l'API."""
    if batch_size < 1:
        raise LoadError("Un lot contient au moins un événement.")
    url = _api_url(base_url, BATCH_PATH, token)
    accepted = duplicates = rejected = 0
    for offset in range(0, len(events), batch_size):
        try:
            report = _post(url, events[offset : offset + batch_size], timeout, token)
        except urllib.error.HTTPError as error:
            raise LoadError(f"L'API a refusé un lot (HTTP {error.code}).") from error
        accepted += int(report["accepted"])
        duplicates += int(report["duplicates"])
        rejected += len(report["rejected"])
    return LoadResult(accepted=accepted, duplicates=duplicates, rejected=rejected)


def send_reports(
    reports: Sequence[dict[str, Any]],
    base_url: str,
    *,
    timeout: float = 30.0,
    token: str | None = None,
) -> ReportResult:
    """Envoie les relevés de couverture un à un ; un relevé refusé (422) est compté."""
    url = _api_url(base_url, COVERAGE_PATH, token)
    accepted = rejected = 0
    for report in reports:
        try:
            _post(url, report, timeout, token)
        except urllib.error.HTTPError as error:
            if error.code != 422:
                raise LoadError(f"L'API a refusé un relevé (HTTP {error.code}).") from error
            rejected += 1
        else:
            accepted += 1
    return ReportResult(accepted=accepted, rejected=rejected)
