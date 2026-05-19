"""
output_generator.py
-------------------
Aggregates analysis results from multiple services and writes a structured
report as JSON and/or CSV.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from codebase_analyzer import AnalysisResult, Endpoint

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ServiceReport:
    """Holds the discovered information for a single service."""

    service_name: str
    repo_url: str
    analysis_method: str
    endpoints: list[Endpoint] = field(default_factory=list)

    def to_flat_rows(self) -> list[dict]:
        """Explode into one row per endpoint (for CSV output)."""
        if not self.endpoints:
            return [
                {
                    "service_name": self.service_name,
                    "repo_url": self.repo_url,
                    "analysis_method": self.analysis_method,
                    "http_method": "",
                    "endpoint_path": "",
                }
            ]
        return [
            {
                "service_name": self.service_name,
                "repo_url": self.repo_url,
                "analysis_method": self.analysis_method,
                "http_method": ep.method,
                "endpoint_path": ep.path,
            }
            for ep in self.endpoints
        ]

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        return {
            "service_name": self.service_name,
            "repo_url": self.repo_url,
            "analysis_method": self.analysis_method,
            "endpoints": [
                {"method": ep.method, "path": ep.path} for ep in self.endpoints
            ],
        }


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_json(reports: list[ServiceReport], output_path: Path) -> None:
    """Write *reports* as a JSON array to *output_path*."""
    data = [r.to_dict() for r in reports]
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("JSON report written → %s  (%d services)", output_path, len(reports))


def write_csv(reports: list[ServiceReport], output_path: Path) -> None:
    """Write *reports* as a flat CSV to *output_path* (one row per endpoint)."""
    fieldnames = ["service_name", "repo_url", "analysis_method", "http_method", "endpoint_path"]
    rows = [row for r in reports for row in r.to_flat_rows()]

    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("CSV report written → %s  (%d rows)", output_path, len(rows))


def write_reports(
    reports: list[ServiceReport],
    output_dir: Path,
    formats: Iterable[str] = ("json", "csv"),
) -> None:
    """Write reports in all requested *formats* to *output_dir*.

    Args:
        reports:    List of :class:`ServiceReport` objects to serialise.
        output_dir: Directory where output files will be created.
        formats:    Iterable of format strings – ``"json"`` and/or ``"csv"``.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    for fmt in formats:
        fmt = fmt.lower().strip()
        if fmt == "json":
            write_json(reports, output_dir / "api_discovery_report.json")
        elif fmt == "csv":
            write_csv(reports, output_dir / "api_discovery_report.csv")
        else:
            logger.warning("Unknown output format '%s' – skipping", fmt)
