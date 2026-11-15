"""En-têtes de sécurité, posés sur toute réponse de l'application.

Ils sont posés à un seul endroit, autour de toute l'application : une réponse
d'erreur les porte comme une réponse réussie, qu'elle vienne d'une route, des
fichiers de l'explorateur (404, 405 et 416 levés par Starlette), du contrôle
des hôtes (403) ou d'une panne (500).
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# L'explorateur n'utilise que ses propres fichiers : aucun script ni style en
# ligne, aucune ressource tierce. Une réponse JSON n'en a pas besoin, mais la
# politique ne lui ôte rien.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    # Une page d'un autre site ne charge aucune réponse par <img> ou <script>.
    (b"cross-origin-resource-policy", b"same-origin"),
)
_NAMES = frozenset(name for name, _ in SECURITY_HEADERS)


class SecurityHeadersMiddleware:
    """Complète le début de chaque réponse HTTP avec les en-têtes de sécurité."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

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
                message = {**message, "headers": [*headers, *SECURITY_HEADERS]}
            await send(message)

        await self._app(scope, receive, send_secured)
