"""FastAPI backend for SlayMetrics Hypothesis Dashboard."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from parser import discover_sessions, load_comparison, load_parameter_summary, load_session

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parents[1]
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


# Serve frontend static files
if os.path.exists(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str = ""):
        index = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index):
            return FileResponse(index)
        return {"error": "Frontend not built"}
