"""
source_control.py
-----------------
Clones a remote git repository into a secure temporary directory and
automatically removes it when the context manager exits.

Credential handling
~~~~~~~~~~~~~~~~~~~
GitPython calls the system ``git`` binary under the hood, so it inherits
the **same credential store you use in the terminal** — macOS Keychain,
``git-credential-osxkeychain``, ``gh auth login``, SSH keys, etc.

If you can run ``git clone <url>`` in your terminal without being asked for
a password, this script will work the same way with no extra configuration.

GitHub Enterprise / SAML SSO
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
If your organisation enforces SAML SSO and a clone redirects to the SSO
login page, it means the credential stored in your keychain has not been
authorised for that SSO organisation yet.  Fix it once in the terminal:

    gh auth refresh -h github.example.com -s repo   # if you use GitHub CLI
    # — or —
    # Go to your PAT on GitHub → Configure SSO → Authorise for your org.

After that, both the terminal and this script will work transparently.
"""

from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import git  # GitPython

logger = logging.getLogger(__name__)


_SSO_MARKERS = (b"github.com/login", b"saml/sso", b"Redirecting", b"<html")


def _check_sso_redirect(local_path: Path) -> None:
    """Raise RuntimeError if git cloned an SSO redirect page instead of a repo.

    When GitHub Enterprise SSO is not satisfied the HTTPS server may return
    an HTML redirect page with a 200 OK, causing git to "successfully" clone
    a repository containing only that HTML page.  We detect this early and
    surface a clear, actionable error message.
    """
    for candidate in list(local_path.glob("README*")) + list(local_path.glob("index.html")):
        try:
            content = candidate.read_bytes()
            if any(marker in content for marker in _SSO_MARKERS):
                raise RuntimeError(
                    f"Cloned repository looks like an SSO/login redirect page "
                    f"({candidate.name}). Your git credentials have not been "
                    f"authorised for the SSO organisation yet.\n\n"
                    f"Fix it once in your terminal:\n"
                    f"  gh auth refresh -h <github-hostname> -s repo\n"
                    f"  # — or — open your PAT on GitHub → Configure SSO → Authorise.\n\n"
                    f"After that, both the terminal and this script will work without "
                    f"any extra configuration."
                )
        except OSError:
            pass


@contextmanager
def cloned_repo(repo_url: str) -> Generator[Path, None, None]:
    """Context manager that clones *repo_url* and yields the local path.

    Uses the same git credentials as your terminal (macOS Keychain, SSH keys,
    ``gh`` CLI, etc.).  No extra token configuration is needed if you can
    already ``git clone <url>`` from the command line.

    The cloned directory is unconditionally deleted when the block exits,
    regardless of exceptions.

    Args:
        repo_url: Clonable git URL (HTTPS or SSH).

    Yields:
        :class:`pathlib.Path` pointing at the repository root.

    Raises:
        git.GitCommandError: If the clone operation fails.
        RuntimeError:        If the clone looks like an SSO redirect page,
                             meaning the stored credential is not yet
                             authorised for the SSO organisation.
    """
    with tempfile.TemporaryDirectory(prefix="api_discovery_") as tmp_dir:
        local_path = Path(tmp_dir) / "repo"
        logger.info("Cloning %s → %s", repo_url, local_path)

        try:
            git.Repo.clone_from(
                repo_url,
                str(local_path),
                depth=1,           # shallow clone – we only need the latest snapshot
                no_single_branch=False,
            )
        except git.GitCommandError as exc:
            logger.error("Failed to clone '%s': %s", repo_url, exc)
            raise

        _check_sso_redirect(local_path)

        yield local_path
        logger.debug("Cleaning up temporary clone at %s", local_path)
