import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("slayMetrics.report")


class ReportWriter:
    """Saves RCA reports to timestamped markdown files."""

    def __init__(self, reports_dir: Path):
        self.reports_dir = reports_dir

    def save(self, rca_report: str, session_id: str) -> Path:
        session_dir = self.reports_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / "rca_report.md"
        path.write_text(rca_report)
        logger.info("Report saved to %s", path)
        return path
