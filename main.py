#!/usr/bin/env python3
"""
main.py
-------
CLI entrypoint for the Backstage API Discovery Tool.

Usage
~~~~~
    python main.py --help

    # Discover all services
    python main.py --backstage-url https://backstage.example.com \
                   --token $BACKSTAGE_TOKEN

    # Filter to a single service
    python main.py --backstage-url https://backstage.example.com \
                   --token $BACKSTAGE_TOKEN \
                   --filter-word payment-service

    # Custom GitHub Enterprise + output directory
    python main.py --backstage-url https://backstage.example.com \
                   --token $BACKSTAGE_TOKEN \
                   --github-url https://github.example.com \
                   --output-dir ./reports \
                   --format json csv
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from backstage_client import BackstageClient
from codebase_analyzer import analyze
from output_generator import ServiceReport, write_reports
from source_control import cloned_repo


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    client = BackstageClient(
        base_url=args.backstage_url,
        token=args.token,
        github_base_url=args.github_url,
        gitlab_base_url=args.gitlab_url,
    )

    reports: list[ServiceReport] = []

    logging.info("Fetching services from Backstage (filter_word=%r)…", args.filter_word)
    services = list(client.get_services(filter_word=args.filter_word))

    if not services:
        logging.warning("No matching services found. Exiting.")
        return

    logging.info("Found %d service(s) to analyse.", len(services))

    for svc in services:
        name = svc["name"]
        repo_url = svc["repo_url"]
        logging.info("─" * 60)
        logging.info("Analysing service: %s", name)
        logging.info("Repository:        %s", repo_url)

        try:
            with cloned_repo(repo_url, github_token=args.github_token) as local_path:
                result = analyze(local_path)
        except Exception as exc:
            logging.error("Failed to process '%s': %s – skipping", name, exc)
            continue

        report = ServiceReport(
            service_name=name,
            repo_url=repo_url,
            analysis_method=result.method,
            endpoints=result.endpoints,
        )
        reports.append(report)

        logging.info(
            "  → %s analysis: %d endpoint(s) discovered",
            result.method,
            len(result.endpoints),
        )
        for ep in result.endpoints:
            logging.info("      %s", ep)

    logging.info("═" * 60)
    logging.info("Analysis complete. Total services: %d", len(reports))

    write_reports(
        reports=reports,
        output_dir=Path(args.output_dir),
        formats=args.format,
    )


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api-discovery",
        description="Discover API endpoints across your organisation's Backstage services.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Required
    parser.add_argument(
        "--backstage-url",
        default=os.environ.get("BACKSTAGE_URL", ""),
        help="Root URL of the Backstage instance (e.g. https://backstage.example.com). "
             "Can also be set via BACKSTAGE_URL env var.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("BACKSTAGE_TOKEN", ""),
        help="Bearer token for the Backstage API. "
             "Can also be set via BACKSTAGE_TOKEN env var.",
    )

    # Optional
    parser.add_argument(
        "--filter-word",
        default=None,
        help="Substring to match against service names (case-insensitive). "
             "Omit to analyse all services.",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="Personal Access Token for GitHub / GitHub Enterprise. "
             "Required when the org enforces SAML SSO — the token must be "
             "authorised for the SSO organisation. "
             "Can also be set via GITHUB_TOKEN env var.",
    )
    parser.add_argument(
        "--github-url",
        default=os.environ.get("GITHUB_BASE_URL", "https://github.com"),
        help="Base URL for GitHub (override for GitHub Enterprise). "
             "Can also be set via GITHUB_BASE_URL env var.",
    )
    parser.add_argument(
        "--gitlab-url",
        default=os.environ.get("GITLAB_BASE_URL", "https://gitlab.com"),
        help="Base URL for GitLab (override for self-hosted GitLab). "
             "Can also be set via GITLAB_BASE_URL env var.",
    )
    parser.add_argument(
        "--output-dir",
        default="./output",
        help="Directory where report files are written.",
    )
    parser.add_argument(
        "--format",
        nargs="+",
        choices=["json", "csv"],
        default=["json", "csv"],
        help="Output format(s).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    _configure_logging(args.verbose)

    # Validate required args
    missing: list[str] = []
    if not args.backstage_url:
        missing.append("--backstage-url (or BACKSTAGE_URL)")
    if not args.token:
        missing.append("--token (or BACKSTAGE_TOKEN)")

    if missing:
        parser.error("Missing required arguments:\n  " + "\n  ".join(missing))

    run(args)


if __name__ == "__main__":
    main()
