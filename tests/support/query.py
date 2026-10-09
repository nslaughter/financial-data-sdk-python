"""Reading a query string as the SDK writes it."""

from __future__ import annotations

from urllib.parse import unquote


def parse_query(query: str) -> tuple[tuple[str, str], ...]:
    """Split a query string into its decoded parameters, in order.

    `+` stays `+`: the SDK percent-encodes every character it does not send
    as is, so a `+` on the wire is one sent as `+`. A lone surrogate, which
    the SDK encodes as its code unit, decodes back to itself.
    """
    if not query:
        return ()
    pairs = []
    for part in query.split("&"):
        name, _, value = part.partition("=")
        pairs.append(
            (
                unquote(name, errors="surrogatepass"),
                unquote(value, errors="surrogatepass"),
            )
        )
    return tuple(pairs)
