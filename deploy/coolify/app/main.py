import asyncio
import base64
import json
import os
import shutil
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc
from raganything import RAGAnything, RAGAnythingConfig

APP_TOKEN = os.getenv("APP_TOKEN", os.getenv("RAGANYTHING_TOKEN", "changeme"))
DATA_ROOT = Path(os.getenv("DATA_ROOT", os.getenv("WORK_DIR", "/data/out")))
DEFAULT_PARSER = os.getenv("DEFAULT_PARSER", "mineru")
DEFAULT_PARSE_METHOD = os.getenv("DEFAULT_PARSE_METHOD", "auto")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "1800"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "3072"))

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required")

DATA_ROOT.mkdir(parents=True, exist_ok=True)


def _auth_ok(authorization: Optional[str], x_token: Optional[str]) -> bool:
    if x_token and x_token == APP_TOKEN:
        return True
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1] == APP_TOKEN
    return False


class WorkspaceManager:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self._instances: Dict[str, Dict[str, Any]] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.embedding_func = EmbeddingFunc(
            embedding_dim=EMBEDDING_DIM,
            max_token_size=8192,
            func=lambda texts: openai_embed(
                texts,
                model=EMBEDDING_MODEL,
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_BASE_URL or None,
            ),
        )

    def _key(self, workspace: str, parser_engine: str, parse_method: str) -> str:
        return f"{workspace}:{parser_engine}:{parse_method}"

    def _workspace_dir(self, workspace: str) -> Path:
        path = self.base_dir / workspace
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def shutdown(self) -> None:
        for entry in list(self._instances.values()):
            rag: RAGAnything = entry["rag"]
            try:
                await rag.finalize_storages()
            except Exception:
                pass
        self._instances.clear()
        self._locks.clear()

    async def purge_idle(self, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        for key, entry in list(self._instances.items()):
            if entry["last_used"] < cutoff:
                rag: RAGAnything = entry["rag"]
                try:
                    await rag.finalize_storages()
                except Exception:
                    pass
                self._instances.pop(key, None)

    async def get_rag(
        self,
        workspace: str,
        parser_engine: str,
        parse_method: str,
    ) -> RAGAnything:
        await self.purge_idle(CACHE_TTL_SECONDS)
        key = self._key(workspace, parser_engine, parse_method)
        entry = self._instances.get(key)
        if entry:
            entry["last_used"] = datetime.now(timezone.utc)
            return entry["rag"]

        working_dir = self._workspace_dir(workspace)
        cfg = RAGAnythingConfig(
            working_dir=str(working_dir),
            parser=parser_engine,
            parse_method=parse_method,
            enable_image_processing=True,
            enable_table_processing=True,
            enable_equation_processing=True,
        )
        rag = RAGAnything(
            config=cfg,
            llm_model_func=lambda prompt, system_prompt=None, history_messages=None, **kwargs: openai_complete_if_cache(
                LLM_MODEL,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages or [],
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_BASE_URL or None,
                **kwargs,
            ),
            embedding_func=self.embedding_func,
        )
        self._instances[key] = {
            "rag": rag,
            "last_used": datetime.now(timezone.utc),
        }
        return rag

    async def clear_workspace(self, workspace: str) -> None:
        keys = [k for k in self._instances if k.startswith(f"{workspace}:")]
        for key in keys:
            entry = self._instances.pop(key, None)
            if entry:
                try:
                    await entry["rag"].finalize_storages()
                except Exception:
                    pass
        self._locks.pop(workspace, None)

    def lock(self, workspace: str) -> asyncio.Lock:
        return self._locks[workspace]


workspace_manager = WorkspaceManager(DATA_ROOT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await workspace_manager.shutdown()


app = FastAPI(title="RAG-Anything Workspace API", version="1.3", lifespan=lifespan)


class Base64File(BaseModel):
    filename: str
    b64: str


class TextItem(BaseModel):
    filename: str
    text: str


class IngestBody(BaseModel):
    urls: List[str] = Field(default_factory=list)
    base64: List[Base64File] = Field(default_factory=list)
    texts: List[TextItem] = Field(default_factory=list)
    parser: str = DEFAULT_PARSER
    parse_method: str = DEFAULT_PARSE_METHOD
    reset: bool = False
    extra: Dict[str, Any] = Field(default_factory=dict)


class QueryBody(BaseModel):
    query: str
    mode: str = "hybrid"
    top_k: int = 10
    images_b64: Optional[List[str]] = None


class ResetBody(BaseModel):
    mode: str = "hard"


class CourseReq(BaseModel):
    topic: str
    mode: str = "hybrid"
    top_k: int = 12


class AssessReq(BaseModel):
    task: str = "Generate a mixed assessment grounded in the corpus."
    mode: str = "hybrid"
    top_k: int = 12


COURSE_PLANNER_PROMPT = """You are an expert instructional designer.
Given the retrieved context, produce:
1) 6–10 module course outline (module title + 1–2 sentence summary).
2) 6–10 Learning Objectives using Bloom verbs.
3) Prerequisites and target audience.
Return STRICT JSON with keys:
- modules: [{id, title, summary}]
- learning_objectives: [{id, text}]
- prerequisites: [string]
- audience: [string]
Ground all items in the provided sources and include citation markers like [source:N] where you used them.
"""


ASSESSMENT_PROMPT = """You are a test designer. Using the retrieved context:
- Write 8 MCQs, 2 short-answer, 1 practical task.
- Each MCQ = {stem, options[A-D], correct, rationale, maps_to_LO, difficulty}.
- For SA & practical, include scoring rubric and expected key points.
Return STRICT JSON with keys: mcq, short_answer, practical, blueprint.
Ground every item with [source:N] markers to the provided context.
"""


def human_bytes(value: int) -> str:
    suffixes = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    i = 0
    while size >= 1024 and i < len(suffixes) - 1:
        size /= 1024
        i += 1
    return f"{size:.2f} {suffixes[i]}"


def dir_stats(path: Path, list_limit: int = 100) -> Dict[str, Any]:
    total_bytes = 0
    total_files = 0
    newest = 0.0
    by_ext: Counter[str] = Counter()
    files: List[Dict[str, Any]] = []

    if not path.exists():
        return {
            "exists": False,
            "total_files": 0,
            "total_bytes": 0,
            "total_bytes_h": "0 B",
            "files": [],
        }

    for item in path.rglob("*"):
        if item.is_file():
            stat = item.stat()
            total_bytes += stat.st_size
            total_files += 1
            newest = max(newest, stat.st_mtime)
            by_ext[item.suffix.lower() or ""] += 1
            if len(files) < list_limit:
                files.append(
                    {
                        "relpath": str(item.relative_to(path)),
                        "bytes": stat.st_size,
                        "bytes_h": human_bytes(stat.st_size),
                        "modified_at": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )

    latest = (
        datetime.fromtimestamp(newest, tz=timezone.utc).isoformat() if newest else None
    )

    return {
        "exists": True,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "total_bytes_h": human_bytes(total_bytes),
        "by_extension": dict(by_ext.most_common()),
        "latest_modified_at": latest,
        "files": files,
    }


@app.get("/")
async def root():
    return {
        "ok": True,
        "message": "RAG-Anything workspace API ready",
        "healthz": "/healthz",
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True, "data_root": str(DATA_ROOT)}


@app.post("/workspaces/{workspace}/ingest")
async def ingest_workspace(
    workspace: str,
    body: IngestBody,
    authorization: Optional[str] = Header(default=None),
    x_webhook_token: Optional[str] = Header(default=None),
):
    if not _auth_ok(authorization, x_webhook_token):
        raise HTTPException(status_code=401, detail="unauthorized")

    workspace_dir = DATA_ROOT / workspace
    if body.reset and workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    rag = await workspace_manager.get_rag(
        workspace, body.parser, body.parse_method
    )

    saved_paths: List[Path] = []

    async with workspace_manager.lock(workspace):
        async with httpx.AsyncClient(timeout=120) as client:
            for url in body.urls:
                name = url.split("?")[0].split("/")[-1] or "remote_file"
                dest = workspace_dir / name
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Failed to download {url}: {exc}",
                    ) from exc
                dest.write_bytes(response.content)
                saved_paths.append(dest)

        for item in body.base64:
            dest = workspace_dir / item.filename
            dest.write_bytes(base64.b64decode(item.b64))
            saved_paths.append(dest)

        for item in body.texts:
            dest = workspace_dir / item.filename
            dest.write_text(item.text, encoding="utf-8")
            saved_paths.append(dest)

        for path in saved_paths:
            await rag.process_document_complete(
                file_path=str(path),
                output_dir=str(workspace_dir),
                **body.extra,
            )

    return {
        "workspace": workspace,
        "ingested": [str(p) for p in saved_paths],
        "stats": dir_stats(workspace_dir),
    }


@app.post("/workspaces/{workspace}/query")
async def query_workspace(
    workspace: str,
    body: QueryBody,
    authorization: Optional[str] = Header(default=None),
    x_webhook_token: Optional[str] = Header(default=None),
):
    if not _auth_ok(authorization, x_webhook_token):
        raise HTTPException(status_code=401, detail="unauthorized")

    rag = await workspace_manager.get_rag(
        workspace, DEFAULT_PARSER, DEFAULT_PARSE_METHOD
    )

    if body.images_b64:
        multimodal = [{"type": "image", "image_b64": img} for img in body.images_b64]
        answer = await rag.aquery_with_multimodal(
            body.query,
            multimodal_content=multimodal,
            mode=body.mode,
        )
    else:
        answer = await rag.aquery(body.query, mode=body.mode, top_k=body.top_k)

    return JSONResponse(
        {
            "workspace": workspace,
            "question": body.query,
            "mode": body.mode,
            "answer": str(answer),
        }
    )


@app.post("/workspaces/{workspace}/generate/course")
async def generate_course(
    workspace: str,
    body: CourseReq,
    authorization: Optional[str] = Header(default=None),
    x_webhook_token: Optional[str] = Header(default=None),
):
    if not _auth_ok(authorization, x_webhook_token):
        raise HTTPException(status_code=401, detail="unauthorized")

    rag = await workspace_manager.get_rag(
        workspace, DEFAULT_PARSER, DEFAULT_PARSE_METHOD
    )

    prompt = f"{COURSE_PLANNER_PROMPT}\n\nUser Topic: {body.topic}"
    result = await rag.aquery(prompt, mode=body.mode, top_k=body.top_k)

    parsed = None
    try:
        parsed = json.loads(str(result))
    except Exception:
        parsed = None

    return {
        "workspace": workspace,
        "topic": body.topic,
        "raw": str(result),
        "parsed": parsed,
    }


@app.post("/workspaces/{workspace}/generate/assessment")
async def generate_assessment(
    workspace: str,
    body: AssessReq,
    authorization: Optional[str] = Header(default=None),
    x_webhook_token: Optional[str] = Header(default=None),
):
    if not _auth_ok(authorization, x_webhook_token):
        raise HTTPException(status_code=401, detail="unauthorized")

    rag = await workspace_manager.get_rag(
        workspace, DEFAULT_PARSER, DEFAULT_PARSE_METHOD
    )

    prompt = f"{ASSESSMENT_PROMPT}\n\nAssessment Task: {body.task}"
    result = await rag.aquery(prompt, mode=body.mode, top_k=body.top_k)

    parsed = None
    try:
        parsed = json.loads(str(result))
    except Exception:
        parsed = None

    return {
        "workspace": workspace,
        "task": body.task,
        "raw": str(result),
        "parsed": parsed,
    }


@app.post("/workspaces/{workspace}/reset")
async def reset_workspace(
    workspace: str,
    body: ResetBody,
    authorization: Optional[str] = Header(default=None),
    x_webhook_token: Optional[str] = Header(default=None),
):
    if not _auth_ok(authorization, x_webhook_token):
        raise HTTPException(status_code=401, detail="unauthorized")

    await workspace_manager.clear_workspace(workspace)

    if body.mode == "hard":
        workspace_dir = DATA_ROOT / workspace
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
        workspace_dir.mkdir(parents=True, exist_ok=True)

    return {"workspace": workspace, "status": f"reset-{body.mode}"}


@app.get("/workspaces/{workspace}/stats")
async def workspace_stats(
    workspace: str,
    authorization: Optional[str] = Header(default=None),
    x_webhook_token: Optional[str] = Header(default=None),
):
    if not _auth_ok(authorization, x_webhook_token):
        raise HTTPException(status_code=401, detail="unauthorized")

    return {"workspace": workspace, "stats": dir_stats(DATA_ROOT / workspace)}


@app.delete("/workspaces/{workspace}")
async def delete_workspace(
    workspace: str,
    authorization: Optional[str] = Header(default=None),
    x_webhook_token: Optional[str] = Header(default=None),
):
    if not _auth_ok(authorization, x_webhook_token):
        raise HTTPException(status_code=401, detail="unauthorized")

    await workspace_manager.clear_workspace(workspace)
    workspace_dir = DATA_ROOT / workspace
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)

    return {"workspace": workspace, "deleted": True}
