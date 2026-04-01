"""FastAPI backend for SlayMetrics Hypothesis Dashboard."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from parser import (
    discover_sessions,
    load_comparison,
    load_leaderboard,
    load_leaderboard_export,
    load_leaderboard_row,
    load_parameter_summary,
    load_session,
)

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parents[1] if len(BACKEND_DIR.parents) > 1 else BACKEND_DIR.parent
DATA_DIR = os.environ.get("DATA_DIR", str(PROJECT_ROOT))
STATIC_DIR = os.environ.get("STATIC_DIR", str(BACKEND_DIR / "static"))

app = FastAPI(title="SlayMetrics Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _json_response(payload: dict | list, pretty: bool = False) -> Response:
    if pretty:
        return Response(
            content=json.dumps(payload, indent=2),
            media_type="application/json",
        )
    return JSONResponse(content=payload)


def _leaderboard_csv(payload: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "rank",
            "session_id",
            "best_small_rps",
            "best_homepage_rps",
            "improvement_pct",
            "performance_vs_reference_pct",
            "missing_count",
            "differing_count",
            "rank_delta",
            "is_new_entry",
            "timestamp",
        ]
    )
    reference = payload.get("reference") or {}
    writer.writerow(
        [
            1,
            reference.get("session_id", ""),
            reference.get("best_small_rps", 0),
            reference.get("best_homepage_rps", 0),
            reference.get("improvement_pct", 0),
            100.0,
            "",
            "",
            reference.get("rank_delta", ""),
            reference.get("is_new_entry", False),
            reference.get("timestamp", ""),
        ]
    )
    for row in payload.get("rows", []):
        writer.writerow(
            [
                row.get("leaderboard_rank", ""),
                row.get("session_id", ""),
                row.get("best_small_rps", 0),
                row.get("best_homepage_rps", 0),
                row.get("improvement_pct", 0),
                row.get("performance_vs_reference_pct", 0),
                row.get("missing_count", 0),
                row.get("differing_count", 0),
                row.get("rank_delta", ""),
                row.get("is_new_entry", False),
                row.get("timestamp", ""),
            ]
        )
    return output.getvalue()


@app.get("/api/sessions")
def list_sessions():
    return discover_sessions(DATA_DIR)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    return load_session(DATA_DIR, session_id)


@app.get("/api/compare")
def compare_sessions(sessions: str = Query(..., description="Comma-separated session IDs")):
    ids = [s.strip() for s in sessions.split(",") if s.strip()]
    return load_comparison(DATA_DIR, ids)


@app.get("/api/parameters")
def list_parameters(pretty: bool = Query(False)):
    return _json_response(load_parameter_summary(DATA_DIR), pretty=pretty)


@app.get("/api/leaderboard")
def get_leaderboard(pretty: bool = Query(False)):
    return _json_response(load_leaderboard(DATA_DIR), pretty=pretty)


@app.get("/api/leaderboard/export")
def export_leaderboard(
    format: str = Query("json", pattern="^(json|csv)$"),
    pretty: bool = Query(False),
):
    payload = load_leaderboard(DATA_DIR)
    if format == "csv":
        return Response(
            content=_leaderboard_csv(payload),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=leaderboard.csv"},
        )
    return _json_response(payload, pretty=pretty)


@app.get("/api/leaderboard/{session_id}")
def get_leaderboard_row(session_id: str, pretty: bool = Query(False)):
    row = load_leaderboard_row(DATA_DIR, session_id)
    if not row:
        raise HTTPException(status_code=404, detail="Leaderboard session not found")
    return _json_response(row, pretty=pretty)


@app.get("/api/leaderboard/{session_id}/export")
def get_leaderboard_export(session_id: str, pretty: bool = Query(False)):
    payload = load_leaderboard_export(DATA_DIR, session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Leaderboard export session not found")
    return _json_response(payload, pretty=pretty)


# Serve frontend static files
if os.path.exists(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str = ""):
        index = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index):
            return FileResponse(index)
        return {"error": "Frontend not built"}
