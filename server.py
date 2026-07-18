"""FastAPI backend for the Electron UI. Exposes coaches, memory, sessions, source
management, and streaming chat over HTTP + WebSocket.

Run directly:
    python server.py                    # binds 127.0.0.1:7823
    python server.py --port 7823        # override port
    python server.py --user justin      # override default user

Electron spawns this at startup and kills it at close.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import (
    FastAPI,
    HTTPException,
    UploadFile,
    File,
    WebSocket,
    WebSocketDisconnect,
    Depends,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import receive_coach as rc


app = FastAPI(title="Receive Coaching UI Backend", version="1.0.0")
# CORS: with the Bearer-token gate below, cross-origin webpages can't call our
# API without knowing the token (which only the Electron shell holds). We drop
# `allow_credentials` because we don't use cookies — that was the actively
# dangerous combination with `allow_origins=["*"]`.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth
#
# The Electron main process generates a random per-session token, hands it to
# us via RECEIVE_COACH_AUTH_TOKEN, and passes the same value to the renderer
# via the URL hash. Every HTTP request must present the token in an
# Authorization: Bearer <token> header; every WebSocket connection must present
# it as a `?token=...` query parameter (browsers can't set custom headers on
# WebSocket handshakes).
#
# If RECEIVE_COACH_AUTH_TOKEN is unset (e.g. running server.py directly for
# CLI-only development), auth is disabled. The Electron launcher always sets it.
# ---------------------------------------------------------------------------

_AUTH_TOKEN: Optional[str] = os.environ.get("RECEIVE_COACH_AUTH_TOKEN") or None


def _token_matches(presented: str) -> bool:
    try:
        return hmac.compare_digest(presented, _AUTH_TOKEN)
    except TypeError:
        # Non-ASCII input makes compare_digest raise; that's a bad token, not a 500.
        return False


def require_token(request: Request) -> None:
    """FastAPI dependency: require a matching Bearer token when auth is enabled."""
    if not _AUTH_TOKEN:
        return
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented or not _token_matches(presented):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _ws_token_ok(ws: WebSocket) -> bool:
    if not _AUTH_TOKEN:
        return True
    presented = ws.query_params.get("token") or ""
    return bool(presented) and _token_matches(presented)


# ---------------------------------------------------------------------------
# Per-user write serialisation
#
# Memory is read-modify-write on a JSON file. Two concurrent chats for the
# same user (two windows, or WS + REST at once) would each load a snapshot,
# both append a session, and the second save would silently drop the first's.
# A per-user lock held across load→respond→save closes that race within this
# process. (A simultaneous CLI session is a separate process and is NOT
# covered — documented limitation.)
# ---------------------------------------------------------------------------

_user_locks: Dict[str, threading.Lock] = {}
_user_locks_guard = threading.Lock()


def _lock_for_user(user: str) -> threading.Lock:
    key = rc.sanitize_user_id(user or "default_user")
    with _user_locks_guard:
        return _user_locks.setdefault(key, threading.Lock())


# ---------------------------------------------------------------------------
# Lazy loading of coaches and per-coach source indexes
# ---------------------------------------------------------------------------

_coaches_cache: Optional[Dict[str, rc.Coach]] = None
_indexes: Dict[str, rc.SourceIndex] = {}


def get_coaches() -> Dict[str, rc.Coach]:
    global _coaches_cache
    if _coaches_cache is None:
        _coaches_cache = rc.load_coaches()
    return _coaches_cache


def get_index(coach: rc.Coach) -> rc.SourceIndex:
    if coach.name not in _indexes:
        idx = rc.SourceIndex(coach)
        idx.reload(verbose=False)
        _indexes[coach.name] = idx
    return _indexes[coach.name]


def invalidate_index(coach_name: str) -> None:
    _indexes.pop(coach_name, None)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CoachSummary(BaseModel):
    name: str
    display_name: str
    description: str
    model: str
    source_count: int  # number of files in sources/ (not including README.md)


class CoachDetail(CoachSummary):
    system_prompt: str
    sources_dir: str
    embeddings_exist: bool


class MemoryView(BaseModel):
    user_id: str
    user_profile: dict
    last_coach: str
    sessions: list
    patterns: dict
    accountability: dict
    meta: dict


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    current_focus: Optional[str] = None
    add_goal: Optional[str] = None


class ChatRequest(BaseModel):
    user: str
    coach: str
    message: str
    history: Optional[List[Dict[str, str]]] = None


class InsightsRequest(BaseModel):
    coach: Optional[str] = None  # limit to one coach's sessions; None = all


# ---------------------------------------------------------------------------
# Health and configuration
# ---------------------------------------------------------------------------

@app.get("/api/health", dependencies=[Depends(require_token)])
def health():
    return {"ok": True, "base_url": rc.OLLAMA_BASE, "embed_model": rc.EMBED_MODEL}


@app.get("/api/config", dependencies=[Depends(require_token)])
def get_config():
    """Expose the active backend configuration so the UI can show it."""
    return {
        "base_url": rc.OLLAMA_BASE,
        "embed_model": rc.EMBED_MODEL,
        "embed_format": os.environ.get("RECEIVE_COACH_EMBED_FORMAT", "auto"),
        "has_api_key": bool(os.environ.get("RECEIVE_COACH_API_KEY")),
        # Why the last LLM call fell back (None if the last call succeeded) —
        # lets the UI distinguish "Ollama not running" from a config/auth bug.
        "last_llm_error": rc.LAST_LLM_ERROR,
        "pdf_support": rc.PDF_SUPPORT,
        "model_override": rc.get_model_override(),
        # Optional local whisper.cpp server for voice input (mic button shows
        # in the UI only when this is configured).
        "stt_url": os.environ.get("RECEIVE_COACH_STT_URL") or None,
    }


class ModelOverrideRequest(BaseModel):
    model: Optional[str] = None  # None clears the override


@app.get("/api/models", dependencies=[Depends(require_token)])
def list_models():
    """List models available on the chat backend. Tries Ollama's native
    /api/tags first, then the OpenAI-compatible /v1/models."""
    import urllib.request as _url

    names: List[str] = []
    try:
        with _url.urlopen(f"{rc.OLLAMA_RAW.rstrip('/')}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        try:
            req = _url.Request(f"{rc.OLLAMA_BASE.rstrip('/')}/models")
            api_key = os.environ.get("RECEIVE_COACH_API_KEY")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with _url.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            names = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception as exc:
            raise HTTPException(503, f"Could not list models from the backend: {type(exc).__name__}")
    return {"models": sorted(set(names)), "override": rc.get_model_override()}


@app.post("/api/config/model", dependencies=[Depends(require_token)])
def set_model(req: ModelOverrideRequest):
    model = (req.model or "").strip() or None
    rc.set_model_override(model)
    return {"ok": True, "override": rc.get_model_override()}


# ---------------------------------------------------------------------------
# Coaches
# ---------------------------------------------------------------------------

def _coach_source_count(coach: rc.Coach) -> int:
    if not coach.sources_dir.exists():
        return 0
    count = 0
    for p in coach.sources_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in rc.SourceIndex.SUPPORTED_EXT and p.name.lower() != "readme.md":
            count += 1
    return count


@app.get("/api/coaches", response_model=List[CoachSummary], dependencies=[Depends(require_token)])
def list_coaches():
    coaches = get_coaches()
    return [
        CoachSummary(
            name=c.name,
            display_name=c.display_name,
            description=c.description,
            model=c.model,
            source_count=_coach_source_count(c),
        )
        for c in sorted(coaches.values(), key=lambda x: x.name)
    ]


@app.get("/api/coaches/{name}", response_model=CoachDetail, dependencies=[Depends(require_token)])
def coach_detail(name: str):
    coaches = get_coaches()
    if name not in coaches:
        raise HTTPException(404, f"Unknown coach: {name}")
    c = coaches[name]
    return CoachDetail(
        name=c.name,
        display_name=c.display_name,
        description=c.description,
        model=c.model,
        source_count=_coach_source_count(c),
        system_prompt=c.system_prompt,
        sources_dir=str(c.sources_dir),
        embeddings_exist=c.embeddings_cache.exists(),
    )


@app.post("/api/coaches/{name}/reindex", dependencies=[Depends(require_token)])
def coach_reindex(name: str):
    coaches = get_coaches()
    if name not in coaches:
        raise HTTPException(404, f"Unknown coach: {name}")
    invalidate_index(name)
    idx = get_index(coaches[name])
    return {"ok": True, "chunks": len(idx.chunks)}


# ---------------------------------------------------------------------------
# Sources (files within a coach's sources/ directory)
# ---------------------------------------------------------------------------

@app.get("/api/coaches/{name}/sources", dependencies=[Depends(require_token)])
def list_sources(name: str):
    coaches = get_coaches()
    if name not in coaches:
        raise HTTPException(404, f"Unknown coach: {name}")
    c = coaches[name]
    if not c.sources_dir.exists():
        return {"files": []}
    files = []
    for p in sorted(c.sources_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in rc.SourceIndex.SUPPORTED_EXT:
            rel = str(p.relative_to(c.sources_dir))
            files.append({
                "name": rel,
                "size": p.stat().st_size,
                "is_readme": p.name.lower() == "readme.md",
            })
    return {"files": files}


MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB per source file


@app.post("/api/coaches/{name}/sources/upload", dependencies=[Depends(require_token)])
async def upload_source(name: str, file: UploadFile = File(...)):
    coaches = get_coaches()
    if name not in coaches:
        raise HTTPException(404, f"Unknown coach: {name}")
    c = coaches[name]
    # Restrict to supported extensions
    suffix = Path(file.filename).suffix.lower()
    if suffix == ".pdf" and not rc.PDF_SUPPORT:
        raise HTTPException(400, "PDF support needs pypdf on the backend: pip install pypdf")
    if suffix not in rc.SourceIndex.SUPPORTED_EXT:
        raise HTTPException(400, f"Only {sorted(rc.SourceIndex.SUPPORTED_EXT)} supported")
    # Sanitise filename (no path traversal)
    safe_name = Path(file.filename).name
    if safe_name.lower() == "readme.md":
        raise HTTPException(400, "Cannot overwrite README.md")
    target = c.sources_dir / safe_name
    c.sources_dir.mkdir(parents=True, exist_ok=True)
    # Stream to a temp file with a size cap, then move into place — an
    # over-limit or interrupted upload must not destroy an existing file
    # of the same name.
    tmp_target = target.with_name(target.name + ".uploading")
    size = 0
    too_big = False
    try:
        with tmp_target.open("wb") as f:
            while True:
                chunk = file.file.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    too_big = True
                    break
                f.write(chunk)
        if too_big:
            raise HTTPException(413, f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
        tmp_target.replace(target)
    finally:
        if tmp_target.exists():
            try:
                tmp_target.unlink()
            except OSError:
                pass
    # Invalidate index so next chat re-embeds
    invalidate_index(name)
    return {"ok": True, "saved_as": safe_name}


@app.delete("/api/coaches/{name}/sources/{filename}", dependencies=[Depends(require_token)])
def delete_source(name: str, filename: str):
    coaches = get_coaches()
    if name not in coaches:
        raise HTTPException(404, f"Unknown coach: {name}")
    c = coaches[name]
    # Sanitise — no path traversal
    safe_name = Path(filename).name
    target = c.sources_dir / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"File not found: {safe_name}")
    if safe_name.lower() == "readme.md":
        raise HTTPException(400, "Cannot delete README.md")
    target.unlink()
    invalidate_index(name)
    return {"ok": True, "deleted": safe_name}


# ---------------------------------------------------------------------------
# Memory & sessions
# ---------------------------------------------------------------------------

@app.get("/api/memory/{user}", dependencies=[Depends(require_token)])
def get_memory(user: str):
    # Lock even for reads: constructing MemoryManager can WRITE (creates the
    # default file for a new user, or backs up + rewrites a corrupt one), and
    # an unlocked write here can collide with a chat's save on Windows.
    with _lock_for_user(user):
        mm = rc.MemoryManager(user)
        return mm.data


@app.post("/api/memory/{user}/profile", dependencies=[Depends(require_token)])
def update_profile(user: str, update: ProfileUpdate):
    with _lock_for_user(user):
        mm = rc.MemoryManager(user)
        if update.name is not None:
            mm.data["user_profile"]["name"] = update.name
        if update.current_focus is not None:
            mm.data["user_profile"]["current_focus"] = update.current_focus
        if update.add_goal:
            goals = mm.data["user_profile"]["goals"]
            if update.add_goal not in goals:
                goals.append(update.add_goal)
        mm.save()
    return {"ok": True}


@app.get("/api/sessions/{user}", dependencies=[Depends(require_token)])
def get_sessions(user: str, coach: Optional[str] = None, limit: int = 50):
    with _lock_for_user(user):
        mm = rc.MemoryManager(user)
        sessions = mm.data.get("sessions", [])
    if coach:
        sessions = [s for s in sessions if s.get("coach") == coach]
    return {"sessions": sessions[-limit:]}


@app.post("/api/insights/{user}", dependencies=[Depends(require_token)])
def generate_insights(user: str, req: InsightsRequest):
    """LLM-generated recap over recent sessions: themes, emotional trend,
    progress, one suggestion. On-demand equivalent of the 'weekly report'
    that cloud coaching apps ship — runs entirely against the local model."""
    with _lock_for_user(user):
        mm = rc.MemoryManager(user)
        data = mm.data
    sessions = data.get("sessions", [])
    if req.coach:
        sessions = [s for s in sessions if s.get("coach") == req.coach]
    recent = sessions[-14:]
    if len(recent) < 3:
        raise HTTPException(400, "Not enough sessions yet — have at least 3 conversations first.")

    lines = []
    for s in recent:
        lines.append(
            f"- {s.get('date','?')} [{s.get('coach','?')}] feeling={s.get('emotional_state','?')} "
            f"issue: {s.get('main_issue','')} next: {s.get('action_step','') or '(none)'}"
        )
    patterns = data.get("patterns", {}).get("recurring_blocks", [])[-8:]
    commitments = data.get("accountability", {}).get("active_commitments", [])[-8:]
    profile = data.get("user_profile", {})

    context_parts = ["Recent sessions (oldest first):", *lines]
    if patterns:
        context_parts.append("Recurring patterns: " + "; ".join(p.get("pattern", "") for p in patterns))
    if commitments:
        context_parts.append("Active commitments: " + "; ".join(commitments))
    if profile.get("current_focus"):
        context_parts.append("Stated focus: " + profile["current_focus"])

    messages = [
        {"role": "system", "content": (
            "You are the insights engine of a private coaching app. From the session "
            "log below, write a short personal report with these sections: "
            "**Themes** (2-3 recurring topics), **Emotional trend** (one sentence), "
            "**Progress** (what moved forward, referencing commitments), and "
            "**One suggestion** (a single concrete next step). Warm, direct, no filler. "
            "Under 200 words. Address the user as 'you'."
        )},
        {"role": "user", "content": "\n".join(context_parts)},
    ]

    coaches = get_coaches()
    pick = req.coach or data.get("last_coach") or "general"
    model = coaches[pick].model if pick in coaches else "llama3.1"
    text = rc.llama_chat(messages, model=model)
    if not text:
        raise HTTPException(503, f"Model unavailable ({rc.LAST_LLM_ERROR or 'no details'}). Is Ollama running?")
    return {"insights": text, "sessions_analyzed": len(recent)}


# ---------------------------------------------------------------------------
# Streaming chat via WebSocket
#
# Client opens ws://127.0.0.1:PORT/ws/chat, sends a JSON message:
#   {"user": "justin", "coach": "business", "message": "..."}
# Server streams back JSON messages:
#   {"type": "token", "text": "..."}           (zero or more)
#   {"type": "done", "used_llm": true, "full_text": "..."}
#   {"type": "error", "message": "..."}
# ---------------------------------------------------------------------------

@app.websocket("/ws/chat")
async def chat_ws(ws: WebSocket):
    # Verify the token BEFORE accepting the handshake — browsers can't set
    # custom headers on ws:// so we check ?token=... from the query string.
    if not _ws_token_ok(ws):
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                req = json.loads(raw)
            except Exception:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            user = req.get("user") or "default_user"
            coach_name = req.get("coach")
            message = (req.get("message") or "").strip()
            history = req.get("history")
            if history is not None and not isinstance(history, list):
                history = None
            # Regeneration replays a prompt whose turn is already in memory —
            # don't record a duplicate session for it. Strict `is True` so a
            # string "false" from a sloppy client doesn't silently skip memory.
            regenerate = req.get("regenerate") is True
            if not coach_name or not message:
                await ws.send_json({"type": "error", "message": "user, coach, and message required"})
                continue

            coaches = get_coaches()
            if coach_name not in coaches:
                await ws.send_json({"type": "error", "message": f"Unknown coach: {coach_name}"})
                continue

            active = coaches[coach_name]
            idx = get_index(active)
            peer_list = list(coaches.values())

            # Immediate ack so the client sees the request was accepted and the
            # connection stays warm during model first-token latency (cold 8B can
            # take 10-30s). Combined with a periodic heartbeat while idle, this
            # prevents WebSocket keepalive timeouts.
            await ws.send_json({"type": "thinking"})

            loop = asyncio.get_event_loop()
            queue: asyncio.Queue = asyncio.Queue()

            def producer():
                try:
                    # Load memory INSIDE the per-user lock so concurrent chats
                    # serialise on the whole load→respond→save cycle.
                    with _lock_for_user(user):
                        mm = rc.MemoryManager(user)
                        engine = rc.CoachEngine(mm, active, idx, peer_coaches=peer_list)
                        for piece in engine.respond_stream(
                            message, history=history, update_memory=not regenerate
                        ):
                            loop.call_soon_threadsafe(queue.put_nowait, piece)
                except Exception as exc:
                    loop.call_soon_threadsafe(queue.put_nowait, {"_error": str(exc)})
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            # Fire and forget; we consume the queue below.
            loop.run_in_executor(None, producer)

            while True:
                try:
                    # Send a heartbeat if no token arrives within 10s so clients
                    # and intermediaries don't time out on slow first-token latency.
                    item = await asyncio.wait_for(queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "heartbeat"})
                    continue

                if item is None:
                    break
                if isinstance(item, dict):
                    if item.get("_error"):
                        await ws.send_json({"type": "error", "message": item["_error"]})
                        continue
                    if item.get("_done"):
                        await ws.send_json({
                            "type": "done",
                            "used_llm": item.get("used_llm", False),
                            "full_text": item.get("full_text", ""),
                        })
                        continue
                else:
                    await ws.send_json({"type": "token", "text": str(item)})
    except WebSocketDisconnect:
        return


# ---------------------------------------------------------------------------
# Simple non-streaming chat (fallback for environments without WebSocket)
# ---------------------------------------------------------------------------

@app.post("/api/chat", dependencies=[Depends(require_token)])
def chat_once(req: ChatRequest):
    coaches = get_coaches()
    if req.coach not in coaches:
        raise HTTPException(404, f"Unknown coach: {req.coach}")
    active = coaches[req.coach]
    idx = get_index(active)
    peer_list = list(coaches.values())
    with _lock_for_user(req.user):
        mm = rc.MemoryManager(req.user)
        engine = rc.CoachEngine(mm, active, idx, peer_coaches=peer_list)
        reply, used_llm = engine.respond(req.message, history=req.history)
    return {"reply": reply, "used_llm": used_llm}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7823)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        ws_ping_interval=30,
        ws_ping_timeout=120,
    )


if __name__ == "__main__":
    main()
