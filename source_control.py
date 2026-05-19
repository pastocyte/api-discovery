"""
source_control.py
-----------------
Clones a remote git repository into a secure temporary directory and
automatically removes it when the context manager exits.

GitHub Enterprise / SSO note
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
GitHub Enterprise with SAML SSO redirects unauthenticated HTTPS clone
requests to the SSO login page.  To avoid this, pass a PAT (Personal
Access Token) that has been authorised for the SSO organisation via the
``github_token`` parameter or the ``GITHUB_TOKEN`` env var.  The token
is injected directly into the clone URL so that git never sees an
unauthenticated request.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urlparse, urlunparse

import git  # GitPython

logger = logging.getLogger(__name__)


def _inject_token_into_url(url: str, token: str) -> str:
    """Return *url* with *token* embedded as the HTTP Basic-Auth user.

    GitHub accepts ``https://<token>@github.example.com/org/repo.git``
    as a valid authenticated clone URL.  This avoids interactive
    credential prompts and SSO redirects.

    Only modifies ``http://`` / ``https://`` URLs; SSH URLs are left
    unchanged (SSH key authentication handles those).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return url  # SSH – leave as-is

    # Avoid double-embedding if a token is already present
    if parsed.username:
        return url

    authenticated = parsed._replace(netloc=f"{token}@{parsed.hostname}"
                                    + (f":{parsed.port}" if parsed.port else ""))
    return urlunparse(authenticated)


def _check_sso_redirect(local_path: Path) -> None:
    """Raise RuntimeError if git cloned an SSO redirect page instead of a repo.

    When GitHub Enterprise SSO is not satisfied, git may "successfully" clone
    a repository that contains only an HTML redirect page.  We detect this by
    looking for the tell-tale SSO URL in the cloned content.
    """
    sso_markers = (b"github.com/login", b"saml/sso", b"Redirecting", b"SSO")
    readme_candidates = list(local_path.glob("README*")) + list(local_path.glob("index.html"))
    for candidate in readme_candidates:
        try:
            content = candidate.read_bytes()
            if any(marker in content for marker in sso_markers):
                raise RuntimeError(
                    f"Cloned repository looks like an SSO redirect page ({candidate.name}). "
                    "Make sure your GitHub PAT is authorised for the SSO organisation. "
                    "See: https://docs.github.com/en/enterprise-cloud@latest/"
                    "authentication/authenticating-with-saml-single-sign-on/"
                    "authorizing-a-personal-access-token-for-use-with-saml-single-sign-on"
                )
        except OSError:
            pass


@contextmanager
def cloned_repo(
    repo_url: str,
    github_token: Optional[str] = None,
) -> Generator[Path, None, None]:
    """Context manager that clones *repo_url* and yields the local path.

    The cloned directory is unconditionally deleted when the block exits,
    regardless of exceptions.

    Args:
        repo_url:     Clonable git URL (HTTPS or SSH).
        github_token: Personal Access Token for GitHub / GitHub Enterprise.
                      If *None*, falls back to the ``GITHUB_TOKEN`` env var.
                      Required when the organisation enforces SAML SSO.

    Yields:
        :class:`pathlib.Path` pointing at the repository root.

    Raises:
        git.GitCommandError: If the clone operation fails.
        RuntimeError:        If the clone looks like an SSO redirect page.
    """
    token = github_token or os.environ.get("GITHUB_TOKEN", "")
    clone_url = _inject_token_into_url(repo_url, token) if token else repo_url

    if not token and urlparse(repo_url).scheme in ("http", "https"):
        logger.warning(
            "No GitHub token provided for HTTPS clone of '%s'. "
            "If your GitHub Enterprise org uses SAML SSO this will fail. "
            "Set --github-token or GITHUB_TOKEN env var.",
            repo_url,
        )

    with tempfile.TemporaryDirectory(prefix="api_discovery_") as tmp_dir:
        local_path = Path(tmp_dir) / "repo"
        # Log the original URL (never log the token-embedded one)
        logger.info("Cloning %s → %s", repo_url, local_path)

        try:
            git.Repo.clone_from(
                clone_url,
                str(local_path),
                depth=1,          # shallow clone – we only need the latest snapshot
                no_single_branch=False,
            )
        except git.GitCommandError as exc:
            logger.error("Failed to clone '%s': %s", repo_url, exc)
            raise

        _check_sso_redirect(local_path)

        yield local_path
        logger.debug("Cleaning up temporary clone at %s", local_path)
