"""FastAPI backend for SlayMetrics Hypothesis Dashboard."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
def list_parameters():
    return load_parameter_summary(DATA_DIR)


@app.get("/api/leaderboard")
def get_leaderboard():
    return load_leaderboard(DATA_DIR)


@app.get("/api/leaderboard/{session_id}")
def get_leaderboard_row(session_id: str):
    row = load_leaderboard_row(DATA_DIR, session_id)
    if not row:
        raise HTTPException(status_code=404, detail="Leaderboard session not found")
    return row


@app.get("/api/leaderboard/{session_id}/export")
def get_leaderboard_export(session_id: str):
    payload = load_leaderboard_export(DATA_DIR, session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Leaderboard export session not found")
    return payload


# Serve frontend static files
if os.path.exists(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str = ""):
        index = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index):
            return FileResponse(index)
        return {"error": "Frontend not built"}
