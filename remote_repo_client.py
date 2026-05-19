"""
remote_repo_client.py
---------------------
Abstract interface and concrete implementations for listing and fetching
repository files via REST APIs — without cloning.

Supported providers
~~~~~~~~~~~~~~~~~~~
* **GitHub** (public + GitHub Enterprise)
  - Trees API for full file listing (single request)
  - Contents API for file fetching (base64 decoded inline)
* **GitLab** (public + self-hosted)
  - Repository Tree API (paginated) for full file listing
  - Repository Files API for file fetching (base64 decoded inline)

Both clients accept a ``base_url`` so they work transparently with
on-premises GitHub Enterprise and self-hosted GitLab instances.
"""

from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional
from urllib.parse import quote

import requests

from http_utils import rate_limited_get

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------

@dataclass
class RemoteFile:
    """Metadata for a single file in a remote repository."""
    path: str          # Repo-relative path, e.g. "src/api/routes.py"
    size: int          # Size in bytes (0 if unknown)
    sha: str = ""      # Blob SHA (used by GitHub for direct blob fetch)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class RemoteRepoClient(ABC):
    """Common interface for fetching file trees and contents from remote repos."""

    @abstractmethod
    def list_files(self, owner: str, repo: str, ref: str = "HEAD") -> list[RemoteFile]:
        """Return all files in the repository at *ref*.

        Args:
            owner: Organisation or user slug.
            repo:  Repository slug.
            ref:   Branch name, tag, or commit SHA. Defaults to ``"HEAD"``.

        Returns:
            Flat list of :class:`RemoteFile` objects (directories excluded).
        """

    @abstractmethod
    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str = "HEAD"
    ) -> str:
        """Fetch and return the text content of a single file.

        Args:
            owner: Organisation or user slug.
            repo:  Repository slug.
            path:  Repo-relative file path.
            ref:   Branch name, tag, or commit SHA.

        Returns:
            Decoded UTF-8 text (binary files may contain replacement chars).

        Raises:
            requests.HTTPError: If the file is not found or auth fails.
        """


# ---------------------------------------------------------------------------
# GitHub implementation
# ---------------------------------------------------------------------------

class GitHubRemoteClient(RemoteRepoClient):
    """Fetches file trees and contents from GitHub or GitHub Enterprise.

    Uses the Git Trees API (``/repos/{owner}/{repo}/git/trees/{ref}?recursive=1``)
    to retrieve the full file listing in a **single request**, then fetches
    individual file content via the Contents API.

    Args:
        base_url: Root URL of the GitHub instance.
                  - Public GitHub: ``https://github.com``
                  - GitHub Enterprise: ``https://github.example.com``
        token:    Personal Access Token (PAT) with ``repo`` scope.
        session:  Optional pre-configured :class:`requests.Session`.
    """

    def __init__(
        self,
        base_url: str = "https://github.com",
        token: str = "",
        session: Optional[requests.Session] = None,
    ) -> None:
        # GitHub Enterprise REST API lives at <host>/api/v3
        # Public GitHub REST API lives at https://api.github.com
        host = base_url.rstrip("/")
        if host in ("https://github.com", "http://github.com"):
            self._api_base = "https://api.github.com"
        else:
            self._api_base = f"{host}/api/v3"

        self._session = session or requests.Session()
        if token:
            self._session.headers["Authorization"] = f"token {token}"
        self._session.headers["Accept"] = "application/vnd.github+json"
        self._session.headers["X-GitHub-Api-Version"] = "2022-11-28"

    def list_files(self, owner: str, repo: str, ref: str = "HEAD") -> list[RemoteFile]:
        """Return all blobs (files) from the git tree at *ref* in one API call."""
        url = f"{self._api_base}/repos/{owner}/{repo}/git/trees/{ref}"
        logger.debug("GitHub tree listing: %s", url)

        resp = rate_limited_get(
            self._session, url, params={"recursive": "1"}, timeout=30
        )
        data = resp.json()

        if data.get("truncated"):
            logger.warning(
                "GitHub tree response was truncated for %s/%s. "
                "Very large repos may have incomplete file listings.",
                owner, repo,
            )

        files: list[RemoteFile] = []
        for item in data.get("tree", []):
            if item.get("type") != "blob":
                continue
            files.append(RemoteFile(
                path=item["path"],
                size=item.get("size", 0),
                sha=item.get("sha", ""),
            ))

        logger.info("GitHub: listed %d files for %s/%s@%s", len(files), owner, repo, ref)
        return files

    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str = "HEAD"
    ) -> str:
        """Fetch a single file via the Contents API and return decoded text."""
        url = f"{self._api_base}/repos/{owner}/{repo}/contents/{path}"
        logger.debug("GitHub fetch file: %s", url)

        resp = rate_limited_get(
            self._session, url, params={"ref": ref}, timeout=30
        )
        data = resp.json()

        encoded = data.get("content", "")
        # GitHub returns base64 with newlines embedded; strip before decoding.
        raw = base64.b64decode(encoded.replace("\n", ""))
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# GitLab implementation
# ---------------------------------------------------------------------------

class GitLabRemoteClient(RemoteRepoClient):
    """Fetches file trees and contents from GitLab (public or self-hosted).

    Uses the Repository Tree API (paginated) for file listing and the
    Repository Files API for content retrieval.

    Args:
        base_url: Root URL of the GitLab instance.
                  - Public GitLab:    ``https://gitlab.com``
                  - Self-hosted:      ``https://gitlab.example.com``
        token:    Personal Access Token with ``read_api`` scope.
        session:  Optional pre-configured :class:`requests.Session`.
    """

    def __init__(
        self,
        base_url: str = "https://gitlab.com",
        token: str = "",
        session: Optional[requests.Session] = None,
    ) -> None:
        self._api_base = f"{base_url.rstrip('/')}/api/v4"
        self._session = session or requests.Session()
        if token:
            self._session.headers["PRIVATE-TOKEN"] = token

    # ------------------------------------------------------------------
    # Project ID resolution (cached per owner/repo pair)
    # ------------------------------------------------------------------

    def _resolve_project_id(self, owner: str, repo: str) -> int:
        """Return the numeric GitLab project ID for ``owner/repo``."""
        return self._resolve_project_id_cached(owner, repo)

    @lru_cache(maxsize=256)
    def _resolve_project_id_cached(self, owner: str, repo: str) -> int:
        slug = quote(f"{owner}/{repo}", safe="")
        url = f"{self._api_base}/projects/{slug}"
        logger.debug("Resolving GitLab project ID: %s", url)
        resp = rate_limited_get(self._session, url, timeout=30)
        project_id: int = resp.json()["id"]
        logger.debug("GitLab project ID for %s/%s = %d", owner, repo, project_id)
        return project_id

    # ------------------------------------------------------------------
    # RemoteRepoClient interface
    # ------------------------------------------------------------------

    def list_files(self, owner: str, repo: str, ref: str = "HEAD") -> list[RemoteFile]:
        """Paginate through the GitLab tree API and collect all file entries."""
        project_id = self._resolve_project_id(owner, repo)
        url = f"{self._api_base}/projects/{project_id}/repository/tree"

        files: list[RemoteFile] = []
        page = 1

        while True:
            params = {
                "recursive": "true",
                "per_page": 100,
                "page": page,
                "ref": ref,
            }
            logger.debug("GitLab tree page %d: %s", page, url)
            resp = rate_limited_get(self._session, url, params=params, timeout=30)
            items = resp.json()

            for item in items:
                if item.get("type") != "blob":
                    continue
                files.append(RemoteFile(
                    path=item["path"],
                    size=0,   # GitLab tree API does not return size; filtered later
                    sha=item.get("id", ""),
                ))

            next_page = resp.headers.get("X-Next-Page")
            if not next_page:
                break
            page = int(next_page)

        logger.info("GitLab: listed %d files for %s/%s@%s", len(files), owner, repo, ref)
        return files

    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str = "HEAD"
    ) -> str:
        """Fetch a single file via the Repository Files API and return decoded text."""
        project_id = self._resolve_project_id(owner, repo)
        encoded_path = quote(path, safe="")
        url = f"{self._api_base}/projects/{project_id}/repository/files/{encoded_path}"
        logger.debug("GitLab fetch file: %s", url)

        resp = rate_limited_get(self._session, url, params={"ref": ref}, timeout=30)
        data = resp.json()

        raw = base64.b64decode(data.get("content", ""))
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

def build_remote_client(
    provider: str,
    base_url: str,
    token: str,
) -> RemoteRepoClient:
    """Return the appropriate :class:`RemoteRepoClient` for *provider*.

    Args:
        provider: ``"github"`` or ``"gitlab"``.
        base_url: Root URL of the SCM host.
        token:    Personal Access Token.

    Returns:
        A ready-to-use :class:`RemoteRepoClient` instance.

    Raises:
        ValueError: For unrecognised provider strings.
    """
    if provider == "github":
        return GitHubRemoteClient(base_url=base_url, token=token)
    if provider == "gitlab":
        return GitLabRemoteClient(base_url=base_url, token=token)
    raise ValueError(
        f"Unknown SCM provider {provider!r}. Expected 'github' or 'gitlab'."
    )
