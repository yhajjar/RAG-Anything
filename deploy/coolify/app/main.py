import os
import tempfile
import base64
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from raganything import RAGAnything, RAGAnythingConfig
from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc

APP_PORT = int(os.getenv("PORT", "8000"))
WORK_DIR = os.getenv("WORK_DIR", "/data")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
AUTH_TOKEN = os.getenv("RAGANYTHING_TOKEN", "")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required")

config = RAGAnythingConfig(
    working_dir=WORK_DIR,
    parser="mineru",
    parse_method="auto",
    enable_image_processing=True,
    enable_table_processing=True,
    enable_equation_processing=True,
)


def llm_model_func(
    prompt,
    system_prompt=None,
    history_messages=None,
    **kwargs,
):
    history_messages = history_messages or []
    return openai_complete_if_cache(
        "gpt-4o-mini",
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages,
        api_key=OPENAI_API_KEY,
        base_url=OPENAI_BASE_URL,
        **kwargs,
    )


embedding_func = EmbeddingFunc(
    embedding_dim=3072,
    max_token_size=8192,
    func=lambda texts: openai_embed(
        texts,
        model="text-embedding-3-large",
        api_key=OPENAI_API_KEY,
        base_url=OPENAI_BASE_URL,
    ),
)

app = FastAPI(title="RAG-Anything API")


def _auth_or_401(token: Optional[str]):
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


class IngestBody(BaseModel):
    file_url: Optional[str] = None
    file_b64: Optional[str] = None
    filename: Optional[str] = "upload.bin"
    token: Optional[str] = None
    doc_id: Optional[str] = "doc-001"


class QueryBody(BaseModel):
    question: str
    mode: Optional[str] = "hybrid"
    token: Optional[str] = None


@app.get("/")
async def root():
    return {
        "ok": True,
        "message": "RAG-Anything API ready. Use /ingest and /query endpoints.",
        "healthz": "/healthz",
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True, "working_dir": WORK_DIR}


@app.post("/ingest")
async def ingest(body: IngestBody):
    _auth_or_401(body.token)

    if not body.file_url and not body.file_b64:
        raise HTTPException(status_code=400, detail="Provide file_url or file_b64")

    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = os.path.join(tmp_dir, body.filename or "upload.bin")

        if body.file_url:
            response = requests.get(body.file_url, timeout=120)
            if response.status_code >= 400:
                raise HTTPException(
                    status_code=400,
                    detail=f"Download failed: {response.status_code}",
                )
            with open(file_path, "wb") as file_obj:
                file_obj.write(response.content)
        else:
            raw = base64.b64decode(body.file_b64)
            with open(file_path, "wb") as file_obj:
                file_obj.write(raw)

        rag = RAGAnything(
            config=config,
            llm_model_func=llm_model_func,
            embedding_func=embedding_func,
        )
        await rag.process_document_complete(
            file_path=file_path,
            output_dir=os.path.join(WORK_DIR, "out"),
            parse_method="auto",
        )

    return {"ok": True, "doc_id": body.doc_id, "stored_at": WORK_DIR}


@app.post("/query")
async def query(body: QueryBody):
    _auth_or_401(body.token)

    rag = RAGAnything(
        config=config,
        llm_model_func=llm_model_func,
        embedding_func=embedding_func,
    )
    result = await rag.aquery(body.question, mode=body.mode or "hybrid")

    return {
        "ok": True,
        "question": body.question,
        "mode": body.mode,
        "answer": getattr(result, "answer", None) or str(result),
        "sources": getattr(result, "sources", None),
    }
