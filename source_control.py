"""
source_control.py
-----------------
Clones a remote git repository into a secure temporary directory and
automatically removes it when the context manager exits.
"""

from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import git  # GitPython

logger = logging.getLogger(__name__)


@contextmanager
def cloned_repo(repo_url: str) -> Generator[Path, None, None]:
    """Context manager that clones *repo_url* and yields the local path.

    The cloned directory is unconditionally deleted when the block exits,
    regardless of exceptions.

    Args:
        repo_url: Clonable git URL (HTTPS or SSH).

    Yields:
        :class:`pathlib.Path` pointing at the repository root.

    Raises:
        git.GitCommandError: If the clone operation fails.
    """
    with tempfile.TemporaryDirectory(prefix="api_discovery_") as tmp_dir:
        local_path = Path(tmp_dir) / "repo"
        logger.info("Cloning %s → %s", repo_url, local_path)

        try:
            git.Repo.clone_from(
                repo_url,
                str(local_path),
                depth=1,          # shallow clone – we only need the latest snapshot
                no_single_branch=False,
            )
        except git.GitCommandError as exc:
            logger.error("Failed to clone '%s': %s", repo_url, exc)
            raise

        yield local_path
        logger.debug("Cleaning up temporary clone at %s", local_path)
