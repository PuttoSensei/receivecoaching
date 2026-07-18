
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import textwrap
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


APP_NAME = "Receive Coaching"

# ---- Path resolution -------------------------------------------------------
# In the usual case, everything lives next to this file. When launched from a
# packaged Electron build (.exe), read-only resources (config/, coaches/) are
# inside the app bundle while user-writable data (data/, logs/, embedding
# caches) belongs in a user-writable location (%APPDATA%). The Electron main
# process passes these through as env vars:
#
#   RECEIVE_COACH_REPO_ROOT  — where receive_coach.py + config + coaches are
#   RECEIVE_COACH_DATA_ROOT  — where data/ and logs/ should live
#
# Both default to BASE_DIR for the CLI / dev case.

BASE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(os.environ.get("RECEIVE_COACH_REPO_ROOT") or BASE_DIR).resolve()
_DATA_ROOT = Path(os.environ.get("RECEIVE_COACH_DATA_ROOT") or BASE_DIR).resolve()

CONFIG_DIR = _REPO_ROOT / "config"

# When running from a packaged .exe, REPO_ROOT is read-only (it's inside the
# installer's resources). The coaches/ folder needs to be writable so the app
# can add embedding caches, upload new source files, etc. On first run we
# seed a user-writable copy of coaches/ from the bundled starter set.
import shutil as _shutil  # noqa: E402
if _REPO_ROOT != _DATA_ROOT:
    _bundled_coaches = _REPO_ROOT / "coaches"
    _user_coaches = _DATA_ROOT / "coaches"
    if _bundled_coaches.exists() and not _user_coaches.exists():
        try:
            _shutil.copytree(_bundled_coaches, _user_coaches)
        except Exception as exc:
            print(
                f"[warn] could not seed writable coaches folder at {_user_coaches}: {exc}. "
                f"Falling back to the read-only bundled copy — source uploads and "
                f"re-indexing will fail until this is resolved.",
                file=sys.stderr,
            )
    COACHES_DIR = _user_coaches if _user_coaches.exists() else _bundled_coaches
else:
    COACHES_DIR = _REPO_ROOT / "coaches"

DATA_DIR = _DATA_ROOT / "data" / "users"
LOGS_DIR = _DATA_ROOT / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_COACH = "general"
EMBED_MODEL = os.environ.get("RECEIVE_COACH_EMBED_MODEL", "nomic-embed-text")

# User settings that survive restarts (currently just the chat-model override
# picked in the UI). Lives in the writable data root, not the repo config dir,
# because the latter is read-only in a packaged build.
SETTINGS_PATH = _DATA_ROOT / "data" / "settings.json"


def _load_settings() -> Dict[str, Any]:
    try:
        if SETTINGS_PATH.exists():
            with SETTINGS_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


_SETTINGS: Dict[str, Any] = _load_settings()


def get_model_override() -> Optional[str]:
    v = _SETTINGS.get("model_override")
    return v if isinstance(v, str) and v.strip() else None


def set_model_override(model: Optional[str]) -> None:
    """Set (or clear with None) the global chat-model override. Persisted."""
    if model is None:
        _SETTINGS.pop("model_override", None)
    else:
        _SETTINGS["model_override"] = model
    _atomic_write_json(SETTINGS_PATH, _SETTINGS)


def effective_model(coach: "Coach") -> str:
    return get_model_override() or coach.model
OLLAMA_BASE = os.environ.get("RECEIVE_COACH_BASE_URL", "http://127.0.0.1:11434/v1")
# Raw Ollama endpoint (for /api/embeddings) — derived from OLLAMA_BASE by stripping /v1.
# Normalise trailing slashes first so "http://host/v1/" works too.
_OLLAMA_BASE_NORM = OLLAMA_BASE.rstrip("/")
OLLAMA_RAW = _OLLAMA_BASE_NORM[:-3] if _OLLAMA_BASE_NORM.endswith("/v1") else _OLLAMA_BASE_NORM


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_json(path: Path, data: Any, **json_kwargs: Any) -> None:
    """Write JSON to `path` atomically: dump to a sibling tempfile, then os.replace.
    Prevents a zero-byte / truncated file if the process is killed mid-write
    (which happens routinely — Electron SIGKILLs the Python child on quit).
    """
    # Per-process tmp name so a UI backend and a CLI session saving the same
    # user don't interleave writes into one tmp file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, **json_kwargs)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _load_config(filename: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    """Load a config file from CONFIG_DIR, with a clear error and fallback on failure."""
    path = CONFIG_DIR / filename
    if not path.exists():
        print(f"[warn] config missing: {path} (using empty fallback)", file=sys.stderr)
        return fallback
    try:
        return load_json(path)
    except Exception as exc:
        print(f"[warn] config unreadable: {path} ({exc}); using empty fallback", file=sys.stderr)
        return fallback


MASTER = _load_config(
    "master_coaching_system_live_v3.json",
    {"question_categories": [], "exercises": []},
)
PATTERN_RULES = _load_config("pattern_detection_rules.json", {"patterns": {}})
MEMORY_RULES = _load_config("memory_update_rules.json", {})


# ---------------------------------------------------------------------------
# Coach definitions
# ---------------------------------------------------------------------------

@dataclass
class Coach:
    name: str
    display_name: str
    description: str
    model: str
    system_prompt: str
    dir: Path

    @property
    def sources_dir(self) -> Path:
        return self.dir / "sources"

    @property
    def embeddings_cache(self) -> Path:
        return self.dir / ".embeddings.json"


def load_coaches() -> Dict[str, Coach]:
    coaches: Dict[str, Coach] = {}
    if not COACHES_DIR.exists():
        return coaches
    for entry in sorted(COACHES_DIR.iterdir()):
        if not entry.is_dir():
            continue
        coach_file = entry / "coach.json"
        if not coach_file.exists():
            continue
        try:
            spec = load_json(coach_file)
            name = spec["name"]
            system_prompt = spec["system_prompt"]
        except Exception as exc:
            # Either JSON is malformed or a required field is missing. Skip
            # the offender and load the rest — one bad coach.json shouldn't
            # take down all 43 coaches.
            print(f"[warn] skipping coach {entry.name}: {exc}", file=sys.stderr)
            continue
        if name in coaches:
            print(f"[warn] duplicate coach name '{name}' in {entry.name}; overriding "
                  f"the one from {coaches[name].dir.name}", file=sys.stderr)
        coaches[name] = Coach(
            name=name,
            display_name=spec.get("display_name", name.title()),
            description=spec.get("description", ""),
            model=spec.get("model", "llama3.1"),
            system_prompt=system_prompt,
            dir=entry,
        )
    return coaches


# ---------------------------------------------------------------------------
# Retrieval: chunk + embed + cache + top-K cosine similarity
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    text: str
    source: str  # filename
    embedding: List[float] = field(default_factory=list)


def chunk_text(text: str, target: int = 500, overlap: int = 80) -> List[str]:
    """Split text into ~`target`-char chunks on paragraph boundaries with some overlap."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > target * 2:
            # big paragraph — split by sentences
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sent in sentences:
                if len(buf) + len(sent) + 1 > target and buf:
                    chunks.append(buf.strip())
                    buf = buf[-overlap:] if overlap else ""
                buf += (" " if buf else "") + sent
        else:
            if len(buf) + len(para) + 2 > target and buf:
                chunks.append(buf.strip())
                buf = buf[-overlap:] if overlap else ""
            buf += ("\n\n" if buf else "") + para
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if len(c) >= 30]


def _try_embed_ollama(text: str) -> Optional[List[float]]:
    """Ollama native format: POST /api/embeddings with {model, prompt}."""
    payload = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        url=f"{OLLAMA_RAW.rstrip('/')}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vec = data.get("embedding")
    if isinstance(vec, list) and vec:
        return vec
    return None


def _try_embed_openai(text: str) -> Optional[List[float]]:
    """OpenAI-compatible format (used by llama-server and others):
    POST /v1/embeddings with {model, input} -> {data: [{embedding: [...]}]}."""
    api_key = os.environ.get("RECEIVE_COACH_API_KEY")
    payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url=f"{OLLAMA_BASE.rstrip('/')}/embeddings",
        data=payload,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = data.get("data") or []
    if items and isinstance(items[0].get("embedding"), list):
        return items[0]["embedding"]
    return None


# Cache which embedding endpoint works so we don't re-probe on every call
_embed_backend: Optional[str] = None  # "ollama" or "openai" once determined


def ollama_embed(text: str) -> Optional[List[float]]:
    """Embed text using whichever endpoint is available.

    Honours RECEIVE_COACH_EMBED_FORMAT if set ('ollama' or 'openai'); otherwise
    auto-detects on first call by trying Ollama's /api/embeddings first (works with
    the Ollama default), then falling back to the OpenAI-compatible /v1/embeddings
    (works with raw llama-server and other gateways).
    """
    global _embed_backend
    forced = os.environ.get("RECEIVE_COACH_EMBED_FORMAT", "").strip().lower()
    order: List[str]
    if forced == "openai":
        order = ["openai"]
    elif forced == "ollama":
        order = ["ollama"]
    elif _embed_backend:
        order = [_embed_backend]
    else:
        order = ["ollama", "openai"]

    last_exc: Optional[Exception] = None
    for backend in order:
        try:
            fn = _try_embed_ollama if backend == "ollama" else _try_embed_openai
            vec = fn(text)
            if vec:
                _embed_backend = backend  # cache the one that worked
                return vec
        except Exception as exc:
            last_exc = exc
            continue

    if last_exc is not None:
        _set_llm_error("embed", last_exc, logfile="embed_error.log")
    return None


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _log_error(filename: str, message: str) -> None:
    try:
        with (LOGS_DIR / filename).open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {message}\n")
    except Exception:
        pass


# Last LLM/embedding failure, kept in memory so the UI can distinguish "model
# server not running" from "auth/config error" without digging through logs.
# None = the most recent call succeeded.
LAST_LLM_ERROR: Optional[str] = None


def _set_llm_error(context: str, exc: Exception, logfile: str = "llama_errors.log") -> None:
    global LAST_LLM_ERROR
    LAST_LLM_ERROR = f"{context}: {type(exc).__name__}: {exc}"
    _log_error(logfile, LAST_LLM_ERROR)


def _clear_llm_error() -> None:
    global LAST_LLM_ERROR
    LAST_LLM_ERROR = None


LOG_ROTATE_BYTES = 10 * 1024 * 1024  # 10 MB


def _rotate_if_large(path: Path) -> None:
    """If a log file has grown past LOG_ROTATE_BYTES, rename it with a timestamp."""
    try:
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            archived = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
            path.rename(archived)
    except Exception:
        pass


# PDF support is optional: the CLI stays stdlib-only, but if pypdf is
# installed (pip install pypdf), .pdf sources are extracted and indexed too.
try:
    from pypdf import PdfReader as _PdfReader  # type: ignore
    PDF_SUPPORT = True
except ImportError:
    _PdfReader = None
    PDF_SUPPORT = False


def read_source_text(path: Path) -> Optional[str]:
    """Read a source file as text. PDFs go through pypdf when available;
    returns None when a file can't be read (caller logs and skips)."""
    if path.suffix.lower() == ".pdf":
        if not PDF_SUPPORT:
            return None
        try:
            reader = _PdfReader(str(path))
            pages = [(page.extract_text() or "") for page in reader.pages]
            text = "\n\n".join(p.strip() for p in pages if p.strip())
            return text or None
        except Exception as exc:
            _log_error("embed_error.log", f"pdf extract {path.name}: {exc}")
            return None
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


class SourceIndex:
    """Lazy chunked + embedded index of a coach's sources/ folder. Cached to disk."""

    SUPPORTED_EXT = {".txt", ".md", ".pdf"} if PDF_SUPPORT else {".txt", ".md"}

    def __init__(self, coach: Coach) -> None:
        self.coach = coach
        self.chunks: List[Chunk] = []
        self._loaded = False

    def _load_cache(self) -> Dict[str, Any]:
        if self.coach.embeddings_cache.exists():
            try:
                return load_json(self.coach.embeddings_cache)
            except Exception:
                return {}
        return {}

    def _save_cache(self, cache: Dict[str, Any]) -> None:
        try:
            _atomic_write_json(self.coach.embeddings_cache, cache)
        except Exception as exc:
            _log_error("embed_error.log", f"cache save failed: {exc}")

    def _current_source_files(self) -> List[Path]:
        if not self.coach.sources_dir.exists():
            return []
        files: List[Path] = []
        for p in sorted(self.coach.sources_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in self.SUPPORTED_EXT and p.name.lower() != "readme.md":
                files.append(p)
        return files

    def reload(self, verbose: bool = False) -> int:
        """Refresh the in-memory chunk list, re-embedding only changed files. Returns # chunks.

        Cache is tagged with the embedding model name and vector dimensionality. If either
        changes (e.g. switching backend from Ollama's nomic-embed-text to a llama-server
        GGUF), the cache is invalidated so cosine similarity remains meaningful.
        """
        cache = self._load_cache()
        cached_model = cache.get("embed_model")
        cached_dim = cache.get("embed_dim")

        # Probe the current embedding dimensionality with a tiny call so we know what
        # we'd be storing. If the probe fails (server down), reuse any cached vectors.
        probe = ollama_embed("probe")
        current_dim = len(probe) if probe else None

        cache_is_valid = (
            cached_model == EMBED_MODEL
            and cached_dim is not None
            and (current_dim is None or cached_dim == current_dim)
        )
        files_cache = cache.get("files", {}) if cache_is_valid else {}
        if not cache_is_valid and cache and verbose:
            print(f"[index] embedding cache invalidated (model or dim changed); re-embedding")

        new_files_cache: Dict[str, Any] = {}
        all_chunks: List[Chunk] = []
        current = self._current_source_files()

        for path in current:
            rel = str(path.relative_to(self.coach.sources_dir))
            try:
                mtime = path.stat().st_mtime
            except Exception as exc:
                _log_error("embed_error.log", f"stat {path}: {exc}")
                continue
            content = read_source_text(path)
            if content is None:
                _log_error("embed_error.log", f"read {path}: unreadable or empty extract")
                continue
            content_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
            cached = files_cache.get(rel)
            # An entry marked "incomplete" (some chunks failed to embed) is
            # treated as a cache miss so a transient server hiccup doesn't
            # permanently drop chunks until the source file itself changes.
            if (cached and not cached.get("incomplete")
                    and cached.get("mtime") == mtime and cached.get("hash") == content_hash):
                entry = cached
            else:
                if verbose:
                    print(f"[index] embedding {rel}...")
                chunk_texts = chunk_text(content)
                chunk_entries: List[Dict[str, Any]] = []
                embed_failed = False
                for ct in chunk_texts:
                    emb = ollama_embed(ct)
                    if emb is None:
                        embed_failed = True
                        continue
                    chunk_entries.append({"text": ct, "embedding": emb})
                entry = {"mtime": mtime, "hash": content_hash, "chunks": chunk_entries}
                if embed_failed:
                    entry["incomplete"] = True
                    # If a previous partial attempt (same content) cached MORE
                    # chunks than this retry managed, keep the richer set —
                    # a retry while the server is still down must not wipe it.
                    if (cached and cached.get("hash") == content_hash
                            and len(cached.get("chunks", [])) > len(chunk_entries)):
                        entry = dict(cached)
                        entry["incomplete"] = True
                    _log_error(
                        "embed_error.log",
                        f"partial embed for {rel}: {len(entry.get('chunks', []))}/{len(chunk_texts)} chunks; will retry next reload",
                    )
            new_files_cache[rel] = entry
            for ch in entry.get("chunks", []):
                all_chunks.append(Chunk(text=ch["text"], source=rel, embedding=ch["embedding"]))

        # Determine dim to write: prefer the probe result; otherwise use a stored chunk's dim.
        stored_dim = current_dim
        if stored_dim is None and all_chunks:
            stored_dim = len(all_chunks[0].embedding)

        self._save_cache({
            "embed_model": EMBED_MODEL,
            "embed_dim": stored_dim,
            "files": new_files_cache,
        })
        self.chunks = all_chunks
        self._loaded = True
        return len(all_chunks)

    def retrieve(self, query: str, k: int = 3, min_score: float = 0.35) -> List[Tuple[Chunk, float]]:
        if not self._loaded:
            self.reload()
        if not self.chunks:
            return []
        qvec = ollama_embed(query)
        if qvec is None:
            return []
        scored = [(ch, cosine(qvec, ch.embedding)) for ch in self.chunks]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(ch, s) for ch, s in scored[:k] if s >= min_score]


# ---------------------------------------------------------------------------
# Pattern detection (unchanged)
# ---------------------------------------------------------------------------

@dataclass
class PatternMatch:
    name: str
    confidence: float
    triggers: List[str]


def sanitize_user_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())
    return safe or "default_user"


def default_memory(user_id: str) -> Dict[str, Any]:
    now = dt.datetime.now().strftime("%Y-%m-%d")
    return {
        "user_id": user_id,
        "user_profile": {
            "name": "",
            "goals": [],
            "current_focus": "",
            "challenges": [],
            "strengths": [],
            "values": []
        },
        "last_coach": "",
        "sessions": [],
        "patterns": {
            "recurring_blocks": [],
            "common_thought_loops": [],
            "progress_signals": []
        },
        "accountability": {
            "active_commitments": [],
            "last_check_in": "",
            "completion_rate": 0
        },
        "meta": {
            "created_at": now,
            "updated_at": now
        }
    }


class MemoryManager:
    def __init__(self, user_id: str) -> None:
        self.user_id = sanitize_user_id(user_id)
        self.path = DATA_DIR / f"{self.user_id}.json"
        self.data = self.load()

    def load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                return load_json(self.path)
            except Exception as exc:
                # Corrupt memory file — most likely a mid-write crash from a
                # previous session. Don't nuke the user's history silently:
                # move the file aside with a timestamp so they can inspect or
                # merge it later, then start fresh.
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = self.path.with_name(f"{self.user_id}.corrupt-{stamp}.json")
                try:
                    self.path.rename(backup)
                    _log_error(
                        "memory_errors.log",
                        f"corrupt memory for {self.user_id}: {type(exc).__name__}: {exc}; moved to {backup.name}",
                    )
                except Exception as move_exc:
                    _log_error(
                        "memory_errors.log",
                        f"corrupt memory for {self.user_id}: {exc}; could not back up: {move_exc}",
                    )
        data = default_memory(self.user_id)
        self.save(data)
        return data

    def save(self, data: Optional[Dict[str, Any]] = None) -> None:
        if data is not None:
            self.data = data
        self.data["meta"]["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d")
        _atomic_write_json(self.path, self.data, indent=2, ensure_ascii=False)

    def add_session(self, coach: str, summary: str, main_issue: str, action_step: str, emotional_state: str, coach_notes: str) -> None:
        session = {
            "session_id": f"sess_{len(self.data['sessions']) + 1:04d}",
            "date": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "coach": coach,
            "summary": summary,
            "main_issue": main_issue,
            "action_step": action_step,
            "emotional_state": emotional_state,
            "coach_notes": coach_notes
        }
        self.data["sessions"].append(session)
        self.data["accountability"]["last_check_in"] = session["date"]
        self.data["last_coach"] = coach
        if action_step and action_step not in self.data["accountability"]["active_commitments"]:
            self.data["accountability"]["active_commitments"].append(action_step)
        self.save()

    def remember_pattern(self, pattern_name: str, phrase: str) -> None:
        key = "recurring_blocks"
        item = {"pattern": pattern_name, "example": phrase}
        existing = self.data["patterns"][key]
        if item not in existing:
            existing.append(item)
        self.save()

    def remember_progress(self, text: str) -> None:
        if text not in self.data["patterns"]["progress_signals"]:
            self.data["patterns"]["progress_signals"].append(text)
            self.save()


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def detect_emotional_state(text: str) -> str:
    t = normalize(text)
    mapping = {
        "overwhelmed": ["overwhelmed", "too much", "stressed", "swamped", "anxious", "panic"],
        "stuck": ["stuck", "trapped", "same thing", "no progress"],
        "sad": ["sad", "down", "flat", "hurt", "upset"],
        "angry": ["angry", "furious", "resent", "annoyed", "mad"],
        "confused": ["confused", "unclear", "not sure", "lost"],
        "hopeful": ["better", "hopeful", "improving", "good progress", "proud"],
    }
    for label, cues in mapping.items():
        if any(cue in t for cue in cues):
            return label
    return "unclear"


def detect_patterns(text: str) -> List[PatternMatch]:
    text_l = normalize(text)
    found: List[PatternMatch] = []
    for name, spec in PATTERN_RULES.get("patterns", {}).items():
        # Config schema: values are dicts with a "keywords" list. Older shape
        # (bare list of phrases) is also accepted so hand-authored configs work.
        if isinstance(spec, dict):
            phrases = spec.get("keywords")
        elif isinstance(spec, list):
            phrases = spec
        else:
            continue
        if not isinstance(phrases, list):
            continue  # malformed config value must not take down every chat
        # Word-boundary match, not substring: "end it" must not fire inside
        # "blend it" / "spend it". Especially important for the crisis pattern.
        hits = [
            p for p in phrases
            if isinstance(p, str) and p
            and re.search(rf"\b{re.escape(p.lower())}\b", text_l)
        ]
        if hits:
            confidence = min(1.0, 0.35 + 0.2 * len(hits))
            found.append(PatternMatch(name=name, confidence=confidence, triggers=hits))
    if "should" in text_l and "but" in text_l and not any(p.name == "avoidance" for p in found):
        found.append(PatternMatch("avoidance", 0.45, ["should ... but"]))
    if text_l.count("what if") >= 1 and not any(p.name == "overthinking" for p in found):
        found.append(PatternMatch("overthinking", 0.55, ["what if"]))
    found.sort(key=lambda x: x.confidence, reverse=True)
    return found


def choose_questions(patterns_found: List[PatternMatch]) -> List[str]:
    category_map = {
        "avoidance": "Action and Movement",
        "overthinking": "Perspective Shifting",
        "self_doubt": "Beliefs and Assumptions",
        "emotional_overload": "Emotional Awareness",
        "external_blame": "Responsibility and Agency",
        "stuck_loop": "Pattern Recognition",
        "resentment": "Emotional Awareness",
        "conflict_avoidance": "Action and Movement",
    }
    default_categories = ["Clarifying the Situation", "Desired Direction"]
    categories = []
    if patterns_found:
        cat = category_map.get(patterns_found[0].name)
        if cat:
            categories.append(cat)
    categories.extend(default_categories)
    seen = set()
    selected_questions: List[str] = []
    for category in MASTER["question_categories"]:
        if category["name"] in categories and category["name"] not in seen:
            seen.add(category["name"])
            selected_questions.extend(category["questions"][:2])
    return selected_questions[:3]


def choose_exercise(patterns_found: List[PatternMatch]) -> Optional[Dict[str, Any]]:
    exercise_map = {
        "avoidance": "Small Action Builder",
        "overthinking": "Fact vs Story Separation",
        "self_doubt": "Belief Check",
        "emotional_overload": "Emotional Reset",
        "external_blame": "Fact vs Story Separation",
        "stuck_loop": "Pattern Mapping",
        "resentment": "Letting Go Prompt",
        "conflict_avoidance": "Communication Reset",
    }
    if not patterns_found:
        return None
    target_name = exercise_map.get(patterns_found[0].name)
    if not target_name:
        return None
    for exercise in MASTER["exercises"]:
        if exercise["name"] == target_name:
            return exercise
    return None


def extract_main_issue(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip())
    if len(cleaned) <= 140:
        return cleaned
    return cleaned[:137] + "..."


def extract_action_step(text: str, response: str) -> str:
    # Require the label to start a line and be followed by a colon or dash — this avoids
    # spurious mid-sentence matches like "my next step-mom" or "the action scene was...".
    patterns = [
        r"^\s*(?:next step|action|commitment|commit)\s*[:\-]\s*(.+)",
        r"^\s*(?:for today|today's step|your step|next move)\s*[:\-]?\s*(.+)",
    ]
    combined = f"{text}\n{response}"
    for pat in patterns:
        m = re.search(pat, combined, re.IGNORECASE | re.MULTILINE)
        if m:
            step = m.group(1).strip().splitlines()[0]
            # Reject if it's clearly not an action (too short, punctuation-only)
            if len(step) >= 3 and re.search(r"[a-zA-Z]", step):
                return step[:180]
    return ""


def build_memory_summary(memory: Dict[str, Any], coach_filter: Optional[str] = None) -> str:
    profile = memory.get("user_profile", {})
    commitments = memory.get("accountability", {}).get("active_commitments", [])
    recurring = memory.get("patterns", {}).get("recurring_blocks", [])
    sessions = memory.get("sessions", [])
    if coach_filter:
        sessions = [s for s in sessions if s.get("coach") == coach_filter]
    recent_sessions = sessions[-3:]
    chunks = []
    if profile.get("name"):
        chunks.append(f"Name: {profile['name']}")
    if profile.get("current_focus"):
        chunks.append(f"Current focus: {profile['current_focus']}")
    if commitments:
        chunks.append("Active commitments: " + "; ".join(commitments[-3:]))
    if recurring:
        recurring_text = "; ".join(f"{r['pattern']} ({r['example']})" for r in recurring[-3:])
        chunks.append("Recurring blocks: " + recurring_text)
    if recent_sessions:
        session_text = "; ".join(
            f"{s['date']} [{s.get('coach','?')}]: {s['main_issue']} -> {s['action_step']}"
            for s in recent_sessions
        )
        chunks.append("Recent sessions: " + session_text)
    return "\n".join(chunks) if chunks else "No prior memory."


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def llama_chat(messages: List[Dict[str, str]], model: str) -> Optional[str]:
    api_key = os.environ.get("RECEIVE_COACH_API_KEY")
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.5,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url=f"{OLLAMA_BASE.rstrip('/')}/chat/completions",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()
        _clear_llm_error()
        return content
    except Exception as exc:
        _set_llm_error("chat", exc)
        return None


def llama_chat_stream(messages: List[Dict[str, str]], model: str):
    """Stream tokens from an OpenAI-compatible /v1/chat/completions endpoint.

    Yields chunks of text as they arrive. Yields nothing on failure (errors are logged).
    """
    api_key = os.environ.get("RECEIVE_COACH_API_KEY")
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.5,
        "stream": True,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url=f"{OLLAMA_BASE.rstrip('/')}/chat/completions",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    _clear_llm_error()
                    return
                try:
                    obj = json.loads(data_str)
                except Exception:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    yield piece
    except Exception as exc:
        _set_llm_error("stream", exc)
        return


# ---------------------------------------------------------------------------
# CoachEngine
# ---------------------------------------------------------------------------

class CoachEngine:
    def __init__(self, memory_manager: MemoryManager, coach: Coach, source_index: SourceIndex,
                 peer_coaches: Optional[List["Coach"]] = None) -> None:
        self.memory = memory_manager
        self.coach = coach
        self.sources = source_index
        # Peer coaches available to refer to (passed from main for cross-coach hints)
        self.peer_coaches = peer_coaches or []

    def build_messages(
        self,
        user_text: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, str]]:
        """Build the full OpenAI-format message list for this turn.

        Args:
            user_text: the current user message.
            history: optional list of previous turns as [{"role": "user"|"assistant",
                "content": "..."}]. The caller is responsible for trimming this to
                a sensible length; this method will cap at MAX_HISTORY_MSGS as a
                safety net.

        The message layout is:
            system: coach persona
            system: user memory summary (this coach only)
            system: retrieved source context (if any)
            system: per-turn guidance (patterns, suggested questions, peer coaches)
            ... history turns (alternating user/assistant) ...
            user: the current message
        """
        MAX_HISTORY_MSGS = 12  # ~6 turns — keeps token budget under control

        patterns_found = detect_patterns(user_text)
        questions = choose_questions(patterns_found)
        pattern_summary = ", ".join(f"{p.name}({p.confidence:.2f})" for p in patterns_found) or "none"
        memory_summary = build_memory_summary(self.memory.data, coach_filter=self.coach.name)

        retrieved = self.sources.retrieve(user_text, k=3)
        retrieved_block = ""
        if retrieved:
            parts = []
            for ch, score in retrieved:
                parts.append(f"[{ch.source} | score={score:.2f}]\n{ch.text}")
            retrieved_block = (
                "Retrieved context from the user's own sources. Ground your questions in this material "
                "and briefly reference the source when useful.\n\n" + "\n\n---\n\n".join(parts)
            )

        guidance = [
            "Detected patterns: " + pattern_summary,
            "Suggested questions: " + " | ".join(questions) if questions else "",
            "Keep the answer short. Ask at most 2 questions. Include one practical next step when ready.",
        ]
        if self.peer_coaches:
            peer_list = ", ".join(f"{c.name} ({c.description.split(' — ')[0] if ' — ' in c.description else c.description})" for c in self.peer_coaches if c.name != self.coach.name)
            if peer_list:
                guidance.append(
                    "Other coaches the user can switch to if their need is outside your scope: "
                    + peer_list[:1500]
                    + ". Only mention if the user's need is clearly a better fit elsewhere; do not reflexively refer."
                )
        guidance = [g for g in guidance if g]

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self.coach.system_prompt},
            {"role": "system", "content": "User memory (this coach only):\n" + memory_summary},
        ]
        if retrieved_block:
            messages.append({"role": "system", "content": retrieved_block})
        messages.append({"role": "system", "content": "\n".join(guidance)})

        # Append conversation history (validated, trimmed, role-normalised)
        if history:
            safe = []
            for h in history[-MAX_HISTORY_MSGS:]:
                if not isinstance(h, dict):
                    continue
                role = h.get("role")
                content = (h.get("content") or "").strip()
                if not content:
                    continue
                # Accept "coach" as a synonym for "assistant" (UI uses "coach")
                if role == "coach":
                    role = "assistant"
                if role not in ("user", "assistant"):
                    continue
                safe.append({"role": role, "content": content})
            messages.extend(safe)

        messages.append({"role": "user", "content": user_text})
        return messages

    def fallback_response(self, user_text: str) -> Tuple[str, List[PatternMatch]]:
        patterns_found = detect_patterns(user_text)
        # Safety handoff: if the message trips the crisis pattern, skip the
        # normal coaching flow and hand off to human support. The LLM path
        # gets this signalled via the guidance line; the rule-based fallback
        # (no model available) must not stay silent on it.
        if any(p.name == "crisis_or_high_risk" for p in patterns_found):
            crisis = (
                "I'm hearing something very serious in what you wrote, and I'm not "
                "the right kind of help for a moment like this.\n\n"
                "Please reach out to a real person right now:\n"
                "- If you're in the UK: Samaritans, call 116 123 (free, 24/7)\n"
                "- If you're in the US or Canada: call or text 988\n"
                "- If you're in Australia: Lifeline, call 13 11 14\n"
                "- Anywhere else: https://findahelpline.com\n\n"
                "If you or someone else is in immediate danger, call your local emergency number.\n\n"
                "I'll be here when you're ready to talk again."
            )
            return crisis, patterns_found
        emotion = detect_emotional_state(user_text)
        questions = choose_questions(patterns_found)
        exercise = choose_exercise(patterns_found)
        lead = "Let’s slow this down a bit."
        if emotion == "overwhelmed":
            lead = "This sounds like a lot all at once. Let’s slow it down."
        elif emotion == "angry":
            lead = "There’s some charge in this. Let’s separate what happened from the story around it."
        elif emotion == "stuck":
            lead = "It sounds like you’re caught in a loop. We can make this smaller."

        reflection = "What I’m hearing is that " + extract_main_issue(user_text).rstrip(".") + "."
        lines = [lead, reflection]

        if questions:
            lines.append("")
            for q in questions[:2]:
                lines.append(f"- {q}")

        action = "For today, pick one small step you can do in 5–10 minutes and do it before you revisit the whole problem."
        if patterns_found:
            top = patterns_found[0].name
            custom_actions = {
                "avoidance": "For today, reduce friction and do the first 5-minute version of the task.",
                "overthinking": "For today, choose one option that is good enough and take one concrete step with it.",
                "self_doubt": "For today, act once as if the more capable version of you is already in charge.",
                "emotional_overload": "For today, spend 90 seconds breathing slowly, then write the facts of the situation in one short list.",
                "external_blame": "For today, name one part that is still in your control and act on that part only.",
                "stuck_loop": "For today, interrupt the usual pattern by changing one step in the sequence.",
                "resentment": "For today, write what you are holding onto and decide what you want to focus on instead.",
                "conflict_avoidance": "For today, draft one clear message without blame and send or refine it.",
            }
            action = custom_actions.get(top, action)

        lines.append("")
        lines.append(action)

        if exercise:
            steps = "; ".join(exercise["steps"][:3])
            lines.append(f"Useful exercise: {exercise['name']} — {steps}.")

        return "\n".join(lines).strip(), patterns_found

    def respond(self, user_text: str, history: Optional[List[Dict[str, str]]] = None) -> Tuple[str, bool]:
        """Return (response_text, used_llm). used_llm=False means the rule-based fallback was used."""
        messages = self.build_messages(user_text, history=history)
        llm_text = llama_chat(messages, model=effective_model(self.coach))
        if llm_text:
            patterns_found = detect_patterns(user_text)
            self._update_memory(user_text, llm_text, patterns_found)
            return llm_text, True
        fallback, patterns_found = self.fallback_response(user_text)
        self._update_memory(user_text, fallback, patterns_found)
        return fallback, False

    def respond_stream(self, user_text: str, history: Optional[List[Dict[str, str]]] = None,
                       update_memory: bool = True):
        """Generator yielding tokens for the reply, then a final sentinel dict with
        metadata. Updates memory at the end (unless update_memory=False — used
        for regeneration, where the user turn is already recorded).

        Yields strings for token pieces, and finally a dict of the form:
            {"_done": True, "full_text": ..., "used_llm": bool}
        """
        messages = self.build_messages(user_text, history=history)
        pieces: List[str] = []
        stream_iter = llama_chat_stream(messages, model=effective_model(self.coach))

        # Try to stream. If nothing arrives, fall back to rule-based.
        got_anything = False
        for piece in stream_iter:
            got_anything = True
            pieces.append(piece)
            yield piece

        if got_anything:
            full_text = "".join(pieces).strip()
            if update_memory:
                patterns_found = detect_patterns(user_text)
                self._update_memory(user_text, full_text, patterns_found)
            yield {"_done": True, "full_text": full_text, "used_llm": True}
            return

        # No streaming output — use rule-based fallback and yield the whole thing.
        fallback, patterns_found = self.fallback_response(user_text)
        if update_memory:
            self._update_memory(user_text, fallback, patterns_found)
        yield fallback
        yield {"_done": True, "full_text": fallback, "used_llm": False}

    def _update_memory(self, user_text: str, response: str, patterns_found: List[PatternMatch]) -> None:
        main_issue = extract_main_issue(user_text)
        emotion = detect_emotional_state(user_text)
        action_step = extract_action_step(user_text, response)
        summary = main_issue
        coach_notes = f"Patterns: {', '.join(p.name for p in patterns_found) or 'none'}"
        self.memory.add_session(
            coach=self.coach.name,
            summary=summary,
            main_issue=main_issue,
            action_step=action_step,
            emotional_state=emotion,
            coach_notes=coach_notes,
        )
        for pat in patterns_found:
            if pat.name == "crisis_or_high_risk":
                # Safety flag, not a coaching pattern — don't record it as a
                # "recurring block" that later gets replayed into prompts.
                continue
            trigger = pat.triggers[0] if pat.triggers else pat.name
            self.memory.remember_pattern(pat.name, trigger)
        if re.search(r"\b(better|improved|did it|completed|followed through|made progress|proud)\b", user_text, re.I):
            self.memory.remember_progress(user_text[:180])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_banner(user_id: str, coach: Coach, chunk_count: int) -> None:
    print("=" * 72)
    print(f"{APP_NAME} — local coach")
    override = get_model_override()
    model_label = f"{override} (override)" if override else coach.model
    print(f"user: {user_id}   coach: {coach.display_name}   model: {model_label}")
    if chunk_count:
        print(f"sources indexed: {chunk_count} chunks")
    print("Type /help for commands, /quit to exit.")
    print("=" * 72)


def print_help(coaches: Dict[str, Coach]) -> None:
    print(textwrap.dedent(f"""
    Commands:
      /help                 Show help
      /memory               Print memory summary (for active coach)
      /sessions             Show recent sessions (for active coach)
      /sessions all         Show recent sessions across all coaches
      /coach                Show active coach and list all coaches
      /coach NAME           Switch to coach NAME ({len(coaches)} available; type /coach for list)
      /reindex              Re-scan the active coach's sources/ folder
      /setname NAME         Save a display name
      /focus TEXT           Save current focus
      /goal TEXT            Add a goal
      /quit                 Exit
    """).strip())


def _list_coaches_stdout(coaches: Dict[str, Coach]) -> None:
    """Print all coaches and exit. Used by --list-coaches."""
    width = max((len(c.name) for c in coaches.values()), default=10)
    for c in sorted(coaches.values(), key=lambda x: x.name):
        print(f"  {c.name:<{width}}  {c.description}")


def _coach_info_stdout(coach: Coach) -> None:
    """Print full config for a single coach. Used by --coach-info NAME."""
    print(f"Name:          {coach.name}")
    print(f"Display name:  {coach.display_name}")
    print(f"Model:         {coach.model}")
    print(f"Directory:     {coach.dir}")
    print(f"Sources dir:   {coach.sources_dir} ({'exists' if coach.sources_dir.exists() else 'missing'})")
    print(f"Description:   {coach.description}")
    print()
    print("System prompt:")
    print("-" * 72)
    print(coach.system_prompt)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Receive Coaching local coach.")
    parser.add_argument("--user", default="justin", help="User id for memory storage.")
    parser.add_argument("--coach", default=None,
                        help="Coach name. If omitted, uses the user's last coach, or 'general'.")
    parser.add_argument("--list-coaches", action="store_true",
                        help="List all available coaches and exit.")
    parser.add_argument("--coach-info", metavar="NAME",
                        help="Print the full config of a single coach and exit.")
    args = parser.parse_args()

    coaches = load_coaches()
    if not coaches:
        print(f"No coaches found under {COACHES_DIR}", file=sys.stderr)
        return 1

    # Non-interactive modes first
    if args.list_coaches:
        _list_coaches_stdout(coaches)
        return 0
    if args.coach_info:
        if args.coach_info not in coaches:
            print(f"Unknown coach '{args.coach_info}'. Available: {', '.join(sorted(coaches))}", file=sys.stderr)
            return 1
        _coach_info_stdout(coaches[args.coach_info])
        return 0

    # Load memory, then resolve default coach (explicit --coach > last_coach in memory > DEFAULT_COACH)
    mm = MemoryManager(args.user)
    requested = args.coach or mm.data.get("last_coach") or DEFAULT_COACH
    if requested not in coaches:
        print(f"Unknown coach '{requested}'. Available: {', '.join(sorted(coaches))}", file=sys.stderr)
        return 1

    active: Coach = coaches[requested]
    index = SourceIndex(active)
    chunk_count = index.reload(verbose=True)
    peer_list = list(coaches.values())
    engine = CoachEngine(mm, active, index, peer_coaches=peer_list)
    print_banner(mm.user_id, active, chunk_count)

    while True:
        try:
            user_text = input(f"\nYou ({active.name}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0

        if not user_text:
            continue

        if user_text == "/quit":
            print("Goodbye.")
            return 0
        if user_text == "/help":
            print_help(coaches)
            continue
        if user_text == "/memory":
            print(build_memory_summary(mm.data, coach_filter=active.name))
            continue
        if user_text == "/sessions":
            sessions = [s for s in mm.data.get("sessions", []) if s.get("coach") == active.name][-5:]
            if not sessions:
                print("No sessions yet with this coach.")
            else:
                for s in sessions:
                    print(f"- {s['date']} | issue: {s['main_issue']} | next: {s['action_step']}")
            continue
        if user_text == "/sessions all":
            sessions = mm.data.get("sessions", [])[-10:]
            if not sessions:
                print("No sessions yet.")
            else:
                for s in sessions:
                    print(f"- {s['date']} [{s.get('coach', '?')}] | {s['main_issue']} -> {s['action_step']}")
            continue
        if user_text == "/coach":
            print(f"Active: {active.display_name} ({active.name})")
            print("Available:")
            width = max(len(c.name) for c in coaches.values())
            for c in sorted(coaches.values(), key=lambda x: x.name):
                marker = "*" if c.name == active.name else " "
                print(f"  {marker} {c.name:<{width}}  {c.description}")
            continue
        if user_text.startswith("/coach "):
            target = user_text[len("/coach "):].strip()
            if target not in coaches:
                print(f"Unknown coach '{target}'. Available: {', '.join(sorted(coaches))}")
                continue
            active = coaches[target]
            index = SourceIndex(active)
            chunk_count = index.reload(verbose=True)
            engine = CoachEngine(mm, active, index, peer_coaches=peer_list)
            mm.data["last_coach"] = active.name
            mm.save()
            print(f"Switched to {active.display_name}. Sources indexed: {chunk_count} chunks.")
            continue
        if user_text == "/reindex":
            chunk_count = index.reload(verbose=True)
            print(f"Reindexed. {chunk_count} chunks.")
            continue
        if user_text.startswith("/setname "):
            mm.data["user_profile"]["name"] = user_text[len("/setname "):].strip()
            mm.save()
            print("Saved name.")
            continue
        if user_text.startswith("/focus "):
            mm.data["user_profile"]["current_focus"] = user_text[len("/focus "):].strip()
            mm.save()
            print("Saved focus.")
            continue
        if user_text.startswith("/goal "):
            goal = user_text[len("/goal "):].strip()
            if goal and goal not in mm.data["user_profile"]["goals"]:
                mm.data["user_profile"]["goals"].append(goal)
                mm.save()
            print("Saved goal.")
            continue

        reply, used_llm = engine.respond(user_text)
        suffix = "" if used_llm else "  [fallback: rule-based — model unreachable, see logs/llama_errors.log]"
        print(f"\n{active.display_name}:{suffix}\n{reply}")

        chat_log = LOGS_DIR / "chat_log.jsonl"
        _rotate_if_large(chat_log)
        log_line = {
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "user": mm.user_id,
            "coach": active.name,
            "input": user_text,
            "output": reply,
            "used_llm": used_llm,
        }
        with chat_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(log_line, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
