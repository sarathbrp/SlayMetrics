"""
RCA Slay Metrics Agent
======================
Entry point. LangGraph workflow:

  deploy_and_run → run_benchmark → analyze → parse_fixes → remediate_fix ┐
                                                              ↑            │ more fixes
                                                              └────────────┘
                                                              ↓ done
                                                             END

DUT     : root@d21-h23-000-r650.rdu2.scalelab.redhat.com
System2 : root@d21-h24-000-r650.rdu2.scalelab.redhat.com (agent machine)

See core/ for implementation:
  core/rca_agent.py    — RCAAgent class + RCAState
  core/rca_nodes.py    — audit, benchmark, merge_fixes, remediate_fix nodes
  core/rca_analysis.py — network, kernel, nginx LLM analysis nodes
  core/orchestrator.py — InstallerOrchestrator + fleet helpers
  core/cli.py          — argument parsing + main()
"""

import logging

from core.constants import LOG_FORMAT, LOG_DATEFMT
from core.cli import main

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)

if __name__ == "__main__":
    main()
