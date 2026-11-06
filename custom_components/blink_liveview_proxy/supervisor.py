"""Supervisor's rules: which add-on is ours, and how to reach it.

Nothing here imports Home Assistant. Supervisor's slug and hostname rules are
easy to get subtly wrong and expensive to get wrong — a bad address is a
`cannot_connect` on the first screen anyone sees — so they live in a module
that can be imported and tested on its own.
"""

from __future__ import annotations

from collections.abc import Iterable

# The add-on's default listening port, and the one it publishes to the host:
# none. `ports: 8088/tcp: null` in the add-on's config.yaml means the port is
# offered but not mapped unless someone opts in, so a host address like
# homeassistant.local:8088 reaches nothing on a stock install.
ADDON_PORT = 8088


def addon_slug(addons: Iterable[str] | None, domain: str) -> str | None:
    """Find this add-on despite Supervisor's repository slug prefix.

    Supervisor prefixes an add-on slug with an id for the repository it came
    from — `local_` for a local build, a hash for an added repository — so the
    slug is only known at runtime, and only by asking.
    """
    for candidate in addons or ():
        slug = str(candidate or "")
        if slug == domain or slug.endswith(f"_{domain}"):
            return slug
    return None


def addon_internal_url(slug: str | None, port: int = ADDON_PORT) -> str:
    """Return the address Home Assistant reaches an add-on on, no port published.

    Supervisor puts every add-on on Home Assistant's own Docker network and
    gives it a hostname: the full slug with underscores turned into dashes.
    That name resolves from Home Assistant whether or not the add-on publishes
    anything to the host, which is what makes it the right default here.
    """
    text = str(slug or "").strip()
    if not text:
        return ""
    return f"http://{text.replace('_', '-')}:{port}"


# The repository that carries the add-on. It is this one: repository.yaml sits
# in its root and the add-on lives in addon/.
ADDON_REPOSITORY_URL = "https://github.com/Teethree89/ha-blink-live-view-proxy"


def _repository_key(source: object) -> str:
    """Reduce a repository URL to what identifies it.

    Supervisor stores a repository exactly as it was typed, so the same one
    turns up as `https://github.com/x/y`, `https://github.com/x/y/` and
    `https://github.com/x/y.git`. Offering to add a repository that is already
    there would be harmless but untidy, and installing from a second copy of it
    would give the add-on a second slug.
    """
    text = str(source or "").strip().lower().rstrip("/")
    text = text.removesuffix(".git").rstrip("/")
    for scheme in ("https://", "http://"):
        text = text.removeprefix(scheme)
    return text.removeprefix("www.")


def is_addon_repository(source: object) -> bool:
    """Report whether a Supervisor repository entry is the one that ships the add-on."""
    return _repository_key(source) == _repository_key(ADDON_REPOSITORY_URL)


def store_addon_slug(repository_slug: str, domain: str) -> str:
    """Return the slug Supervisor gives an add-on that came from a repository."""
    return f"{repository_slug}_{domain}"
