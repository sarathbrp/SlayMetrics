"""FastAPI backend for SlayMetrics Hypothesis Dashboard."""

from __future__ import annotations

import os

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from parser import discover_sessions, load_comparison, load_session

DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATIC_DIR = os.environ.get("STATIC_DIR", "/app/static")

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


# Serve frontend static files
if os.path.exists(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str = ""):
        index = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index):
            return FileResponse(index)
        return {"error": "Frontend not built"}
