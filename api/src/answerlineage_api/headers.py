"""En-têtes de sécurité, posés sur toute réponse de l'application.

Ils sont posés à un seul endroit, autour de toute l'application : une réponse
d'erreur les porte comme une réponse réussie, qu'elle vienne d'une route, des
fichiers de l'explorateur (404, 405 et 416 levés par Starlette), du contrôle
des hôtes (403) ou d'une panne (500).
"""

from __future__ import annotations

from collections.abc import Sequence

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def content_security_policy(connect_sources: Sequence[str] = ()) -> str:
    """La politique de l'explorateur : ses propres fichiers, aucun script ni style en
    ligne, aucune ressource tierce. Il ne joint que l'API et, pour se connecter, les
    origines données : celle de l'émetteur OIDC. Une réponse JSON n'en a pas besoin,
    mais la politique ne lui ôte rien."""
    return "; ".join(
        (
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            " ".join(("connect-src 'self'", *connect_sources)),
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        )
    )


CONTENT_SECURITY_POLICY = content_security_policy()


def security_headers(connect_sources: Sequence[str] = ()) -> tuple[tuple[bytes, bytes], ...]:
    return (
        (b"content-security-policy", content_security_policy(connect_sources).encode("ascii")),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-opener-policy", b"same-origin"),
        # Une page d'un autre site ne charge aucune réponse par <img> ou <script>.
        (b"cross-origin-resource-policy", b"same-origin"),
    )


SECURITY_HEADERS = security_headers()
_NAMES = frozenset(name for name, _ in SECURITY_HEADERS)


class SecurityHeadersMiddleware:
    """Complète le début de chaque réponse HTTP avec les en-têtes de sécurité."""

    def __init__(self, app: ASGIApp, connect_sources: Sequence[str] = ()) -> None:
        self._app = app
        self._headers = security_headers(connect_sources)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if bytes(name).lower() not in _NAMES
                ]
                message = {**message, "headers": [*headers, *self._headers]}
            await send(message)

        await self._app(scope, receive, send_secured)
