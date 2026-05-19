"""
backstage_client.py
-------------------
Authenticates with a Backstage instance and retrieves Component entities,
optionally filtered by a search keyword. Extracts the source-control
repository URL from well-known annotations.
"""

from __future__ import annotations

import logging
from typing import Generator, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

# Annotation keys that may hold the repository URL, in priority order.
_REPO_ANNOTATION_KEYS = [
    "backstage.io/source-location",
    "github.com/project-slug",
    "gitlab.com/project-slug",
]


class BackstageClient:
    """Thin client for the Backstage Software Catalog REST API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        github_base_url: str = "https://github.com",
        gitlab_base_url: str = "https://gitlab.com",
        page_size: int = 500,
    ) -> None:
        """
        Args:
            base_url:        Root URL of the Backstage instance
                             (e.g. ``https://backstage.example.com``).
            token:           Bearer token for the Backstage API.
            github_base_url: Base URL for GitHub (default public; override for GHE).
            gitlab_base_url: Base URL for GitLab (default public; override for self-hosted).
            page_size:       Number of entities fetched per request.
        """
        self.base_url = base_url.rstrip("/")
        self.github_base_url = github_base_url.rstrip("/")
        self.gitlab_base_url = gitlab_base_url.rstrip("/")
        self.page_size = page_size
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_services(
        self, filter_word: Optional[str] = None
    ) -> Generator[dict, None, None]:
        """Yield service dicts for every matching Backstage Component entity.

        Args:
            filter_word: Optional substring to match against the component
                         *name*. Case-insensitive. If ``None`` all components
                         are returned.

        Yields:
            Dicts with keys ``name``, ``repo_url``.
        """
        for entity in self._iter_entities(kind="Component"):
            name: str = entity.get("metadata", {}).get("name", "unknown")

            if filter_word and filter_word.lower() not in name.lower():
                logger.debug("Skipping entity '%s' (filter_word not matched)", name)
                continue

            repo_url = self._extract_repo_url(entity)
            if not repo_url:
                logger.warning(
                    "No recognised repo annotation found for '%s' – skipping", name
                )
                continue

            yield {"name": name, "repo_url": repo_url}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _iter_entities(self, kind: str) -> Generator[dict, None, None]:
        """Paginate through all entities of the requested *kind*."""
        endpoint = urljoin(self.base_url + "/", "api/catalog/entities")
        offset = 0

        while True:
            params = {
                "filter": f"kind={kind}",
                "limit": self.page_size,
                "offset": offset,
            }
            logger.debug("GET %s  params=%s", endpoint, params)

            try:
                resp = self._session.get(endpoint, params=params, timeout=30)
                resp.raise_for_status()
            except requests.HTTPError as exc:
                logger.error("Backstage API error: %s", exc)
                raise

            entities: list[dict] = resp.json()
            if not entities:
                break

            yield from entities

            if len(entities) < self.page_size:
                break
            offset += self.page_size

    def _extract_repo_url(self, entity: dict) -> Optional[str]:
        """Return a clonable git URL from the entity's annotations, or *None*."""
        annotations: dict = entity.get("metadata", {}).get("annotations", {})

        for key in _REPO_ANNOTATION_KEYS:
            value = annotations.get(key)
            if not value:
                continue

            # backstage.io/source-location is a URI like
            # "url:https://github.com/org/repo" or just the bare URL.
            if key == "backstage.io/source-location":
                value = value.removeprefix("url:")

            # project-slug annotations contain "org/repo"
            if key == "github.com/project-slug":
                value = f"{self.github_base_url}/{value}"

            if key == "gitlab.com/project-slug":
                value = f"{self.gitlab_base_url}/{value}"

            # Normalise to a .git clone URL if not already
            if not value.endswith(".git"):
                value = value + ".git"

            return value

        return None
