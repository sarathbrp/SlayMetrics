from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
LOGS_DIR    = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config.yaml"
PROMPTS_DIR = BASE_DIR / "prompts"
SCRIPTS_DIR = BASE_DIR / "scripts"
DSPY_DIR    = BASE_DIR / "dspy_data"
REPORTS_DIR = BASE_DIR / "rca_reports"

AUDIT_SCRIPT = "omega_master_audit.sh"
REMOTE_TMP   = "/tmp"
LOG_FORMAT   = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
LOG_DATEFMT  = "%Y-%m-%d %H:%M:%S"
