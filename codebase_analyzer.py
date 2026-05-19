"""
codebase_analyzer.py
--------------------
Scans a repository for API endpoints.

Two entry points are provided:

* :func:`analyze` – scans a **locally cloned** repository (original mode).
* :func:`analyze_remote` – scans a **remote repository via API** without
  cloning.  Pass a :class:`~remote_repo_client.RemoteRepoClient` (GitHub or
  GitLab) together with the owner/repo slugs and an optional ref.

Strategy
~~~~~~~~
1. **Phase 1 – OpenAPI/Swagger specs**: Look for standard spec files and parse
   them directly. This gives the highest accuracy results.
2. **Phase 2 – Heuristic regex**: Walk source files and apply per-language
   regular expressions to extract route definitions.

Supported languages & frameworks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* Java  – Spring Boot, JAX-RS
* Python – FastAPI, Flask, Django
* Node.js – Express, NestJS
* Go – Gin, Echo, Fiber, net/http
* C# / .NET – ASP.NET Core
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

if TYPE_CHECKING:
    from remote_repo_client import RemoteRepoClient, RemoteFile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    method: str
    path: str

    def __str__(self) -> str:
        return f"{self.method.upper()} {self.path}"


@dataclass
class AnalysisResult:
    method: str                    # "OpenAPI" | "Regex"
    endpoints: list[Endpoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# OpenAPI / Swagger file discovery
# ---------------------------------------------------------------------------

_OPENAPI_FILENAMES = {
    "openapi.yaml", "openapi.yml", "openapi.json",
    "swagger.yaml", "swagger.yml", "swagger.json",
}

_HTTP_METHODS = ["get", "post", "put", "patch", "delete", "head", "options", "trace"]


def _find_openapi_files(repo_root: Path) -> list[Path]:
    """Return all OpenAPI / Swagger spec files found anywhere in the repo."""
    return [
        p for p in repo_root.rglob("*")
        if p.is_file() and p.name.lower() in _OPENAPI_FILENAMES
    ]


def _parse_openapi(spec_path: Path) -> list[Endpoint]:
    """Parse an OpenAPI / Swagger file and return a list of endpoints."""
    try:
        text = spec_path.read_text(encoding="utf-8", errors="replace")
        if spec_path.suffix.lower() == ".json":
            spec = json.loads(text)
        else:
            spec = yaml.safe_load(text)
    except Exception as exc:
        logger.warning("Could not parse OpenAPI spec %s: %s", spec_path, exc)
        return []

    if not isinstance(spec, dict):
        return []

    endpoints: list[Endpoint] = []

    # OpenAPI 2 (Swagger) uses "basePath"; OpenAPI 3 uses "servers"
    base_path = spec.get("basePath", "")

    for route, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            if method in path_item:
                endpoints.append(Endpoint(method=method.upper(), path=base_path + route))

    return endpoints


# ---------------------------------------------------------------------------
# Per-language regex patterns
# ---------------------------------------------------------------------------

# Each entry: (compiled_regex, method_group_index_or_None, path_group_index)
# When method_group_index is None the method is encoded in the pattern itself.

_PATTERNS: list[tuple[re.Pattern, Optional[int], int, str]] = []

def _add(pattern: str, method_group: Optional[int], path_group: int, label: str) -> None:
    _PATTERNS.append((re.compile(pattern, re.MULTILINE), method_group, path_group, label))


# --- Java (Spring Boot) ---
_add(r'@(Get|Post|Put|Patch|Delete|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
     1, 2, "Java/Spring")

# --- Java (JAX-RS) ---
_add(r'@(GET|POST|PUT|PATCH|DELETE)\b[\s\S]{0,80}?@Path\s*\(\s*["\']([^"\']+)["\']',
     1, 2, "Java/JAX-RS")

# --- Python (Flask / FastAPI decorator style) ---
_add(r'@(?:app|router|blueprint)\.(get|post|put|patch|delete|head|options)\s*\(\s*["\']([^"\']+)["\']',
     1, 2, "Python/Flask+FastAPI")

# --- Python (Django urls.py – path / re_path) ---
_add(r'(?:path|re_path|url)\s*\(\s*["\']([^"\']+)["\']',
     None, 1, "Python/Django")

# --- Node.js (Express / Fastify) ---
_add(r'(?:app|router|server)\.(get|post|put|patch|delete|head|options)\s*\(\s*["\`]([^"\`\']+)["\`\'"]',
     1, 2, "Node/Express")

# --- Node.js (NestJS decorator) ---
_add(r'@(Get|Post|Put|Patch|Delete|Head|Options)\s*\(\s*(?:["\']([^"\']*)["\'])?\s*\)',
     1, 2, "Node/NestJS")

# --- Go (Gin / Echo / Fiber) ---
_add(r'\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*\(\s*["\']([^"\']+)["\']',
     1, 2, "Go/Gin+Echo+Fiber")

# --- Go (net/http HandleFunc) ---
_add(r'http\.HandleFunc\s*\(\s*["\']([^"\']+)["\']',
     None, 1, "Go/net-http")

# --- C# ASP.NET Core (route attribute) ---
_add(r'\[Http(Get|Post|Put|Patch|Delete|Head|Options)\s*(?:\(\s*["\']([^"\']*)["\'])?\]',
     1, 2, "CSharp/ASP.NET")

# --- C# ASP.NET Core ([Route("...")] on controller) ---
_add(r'\[Route\s*\(\s*["\']([^"\']+)["\']',
     None, 1, "CSharp/Route")


# File extensions to scan (skip binaries, assets, etc.)
_SCAN_EXTENSIONS = {
    ".java", ".kt",                          # JVM
    ".py",                                   # Python
    ".js", ".mjs", ".cjs", ".ts", ".tsx",   # JavaScript / TypeScript
    ".go",                                   # Go
    ".cs",                                   # C#
    ".rb",                                   # Ruby (bonus)
    ".php",                                  # PHP (bonus)
}

# Directories that are safe to skip
_SKIP_DIRS = {
    "node_modules", ".git", "vendor", "dist", "build",
    "__pycache__", ".venv", "venv", "target", "bin", "obj",
}


def _scan_with_regex(repo_root: Path) -> list[Endpoint]:
    """Walk the repo and apply heuristic regex patterns to source files."""
    endpoints: list[Endpoint] = []

    for file_path in repo_root.rglob("*"):
        # Skip unwanted directories
        if any(part in _SKIP_DIRS for part in file_path.parts):
            continue
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in _SCAN_EXTENSIONS:
            continue

        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for pattern, method_group, path_group, label in _PATTERNS:
            for match in pattern.finditer(text):
                try:
                    method = (
                        match.group(method_group).upper()
                        if method_group is not None
                        else "ANY"
                    )
                    path = match.group(path_group) or "/"
                except IndexError:
                    continue

                # Normalise Spring's RequestMapping to GET/POST/etc.
                if method == "REQUEST":
                    method = "ANY"

                ep = Endpoint(method=method, path=path)
                if ep not in endpoints:
                    endpoints.append(ep)
                    logger.debug("[%s] %s %s  (%s)", label, method, path, file_path.name)

    return endpoints


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def analyze(repo_root: Path) -> AnalysisResult:
    """Analyse *repo_root* and return discovered endpoints.

    Tries OpenAPI specs first; falls back to regex heuristics if none found.

    Args:
        repo_root: Path to the root of the locally cloned repository.

    Returns:
        :class:`AnalysisResult` containing the analysis method and endpoints.
    """
    # --- Phase 1: OpenAPI ---
    spec_files = _find_openapi_files(repo_root)
    if spec_files:
        all_endpoints: list[Endpoint] = []
        for spec_file in spec_files:
            logger.info("Parsing OpenAPI spec: %s", spec_file)
            all_endpoints.extend(_parse_openapi(spec_file))

        if all_endpoints:
            logger.info("OpenAPI analysis found %d endpoints", len(all_endpoints))
            return AnalysisResult(method="OpenAPI", endpoints=all_endpoints)

    # --- Phase 2: Regex heuristics ---
    logger.info("No OpenAPI spec found – falling back to regex heuristics")
    endpoints = _scan_with_regex(repo_root)
    logger.info("Regex analysis found %d endpoints", len(endpoints))
    return AnalysisResult(method="Regex", endpoints=endpoints)


# ---------------------------------------------------------------------------
# Remote (no-clone) analysis
# ---------------------------------------------------------------------------

# Maximum file size to fetch via API (bytes).  Larger files are skipped to
# avoid excessive API calls on auto-generated or minified assets.
_MAX_REMOTE_FILE_SIZE = 512_000


def _should_scan_remote(remote_file: "RemoteFile") -> bool:
    """Return True if *remote_file* is worth fetching for regex analysis.

    Applies the same directory skip list and extension allow list as the
    local :func:`_scan_with_regex`, plus a size guard so we never fetch
    large binary/generated files.
    """
    path = Path(remote_file.path)
    parts = path.parts

    if any(d in parts for d in _SKIP_DIRS):
        return False
    if path.suffix.lower() not in _SCAN_EXTENSIONS:
        return False
    # size == 0 means the provider didn't return size (e.g. GitLab tree API);
    # allow those through and let the content call decide.
    if remote_file.size > 0 and remote_file.size > _MAX_REMOTE_FILE_SIZE:
        logger.debug("Skipping large remote file (%d bytes): %s", remote_file.size, remote_file.path)
        return False
    return True


def _is_openapi_file_remote(remote_file: "RemoteFile") -> bool:
    """Return True if the remote file looks like an OpenAPI/Swagger spec."""
    return Path(remote_file.path).name.lower() in _OPENAPI_FILENAMES


def _parse_openapi_text(text: str, label: str) -> list[Endpoint]:
    """Parse OpenAPI/Swagger YAML or JSON from *text* and return endpoints."""
    try:
        if label.endswith(".json"):
            spec = json.loads(text)
        else:
            spec = yaml.safe_load(text)
    except Exception as exc:
        logger.warning("Could not parse OpenAPI spec (%s): %s", label, exc)
        return []

    if not isinstance(spec, dict):
        return []

    endpoints: list[Endpoint] = []
    base_path = spec.get("basePath", "")

    for route, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            if method in path_item:
                endpoints.append(Endpoint(method=method.upper(), path=base_path + route))

    return endpoints


def _scan_text_with_regex(text: str, filename: str) -> list[Endpoint]:
    """Apply all heuristic regex patterns to *text* and return endpoints."""
    endpoints: list[Endpoint] = []
    for pattern, method_group, path_group, label in _PATTERNS:
        for match in pattern.finditer(text):
            try:
                method = (
                    match.group(method_group).upper()
                    if method_group is not None
                    else "ANY"
                )
                path = match.group(path_group) or "/"
            except IndexError:
                continue

            if method == "REQUEST":
                method = "ANY"

            ep = Endpoint(method=method, path=path)
            if ep not in endpoints:
                endpoints.append(ep)
                logger.debug("[%s] %s %s  (%s)", label, method, path, filename)
    return endpoints


def analyze_remote(
    client: "RemoteRepoClient",
    owner: str,
    repo: str,
    ref: str = "HEAD",
) -> AnalysisResult:
    """Analyse a remote repository via API calls — no local clone required.

    Mirrors the two-phase strategy of :func:`analyze`:

    1. **Phase 1 – OpenAPI**: Fetch any OpenAPI/Swagger spec files found in
       the file tree and parse them.  Returns immediately if endpoints are
       found.
    2. **Phase 2 – Regex**: Fetch each source file that passes the extension
       and size filters and apply heuristic regex patterns in memory.

    Args:
        client: A :class:`~remote_repo_client.RemoteRepoClient` instance
                (:class:`~remote_repo_client.GitHubRemoteClient` or
                :class:`~remote_repo_client.GitLabRemoteClient`).
        owner:  Organisation or user slug.
        repo:   Repository slug.
        ref:    Branch, tag, or commit SHA to scan.  Defaults to ``"HEAD"``.

    Returns:
        :class:`AnalysisResult` containing the analysis method and endpoints.
    """
    logger.info("Remote analysis: %s/%s @ %s", owner, repo, ref)

    all_files = client.list_files(owner, repo, ref=ref)

    # --- Phase 1: OpenAPI specs -------------------------------------------------
    spec_files = [f for f in all_files if _is_openapi_file_remote(f)]
    if spec_files:
        all_endpoints: list[Endpoint] = []
        for remote_file in spec_files:
            logger.info("Fetching OpenAPI spec: %s", remote_file.path)
            try:
                text = client.get_file_content(owner, repo, remote_file.path, ref=ref)
            except Exception as exc:
                logger.warning("Could not fetch %s: %s", remote_file.path, exc)
                continue
            all_endpoints.extend(_parse_openapi_text(text, label=remote_file.path))

        if all_endpoints:
            logger.info("Remote OpenAPI analysis found %d endpoints", len(all_endpoints))
            return AnalysisResult(method="OpenAPI", endpoints=all_endpoints)

    # --- Phase 2: Regex heuristics ----------------------------------------------
    logger.info("No OpenAPI spec found – falling back to remote regex heuristics")
    scannable = [f for f in all_files if _should_scan_remote(f)]
    logger.info("%d files selected for regex scan out of %d total", len(scannable), len(all_files))

    endpoints: list[Endpoint] = []
    for remote_file in scannable:
        try:
            text = client.get_file_content(owner, repo, remote_file.path, ref=ref)
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", remote_file.path, exc)
            continue

        for ep in _scan_text_with_regex(text, filename=remote_file.path):
            if ep not in endpoints:
                endpoints.append(ep)

    logger.info("Remote regex analysis found %d endpoints", len(endpoints))
    return AnalysisResult(method="Regex", endpoints=endpoints)
