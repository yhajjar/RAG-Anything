# RAG-Anything Workspace API

This document describes the FastAPI service bundled under `deploy/coolify/app`. It wraps the RAG-Anything pipeline behind workspace-scoped HTTP endpoints so automation tools (e.g., n8n) can ingest documents, run retrieval, and trigger curriculum/assessment generation without managing vector stores directly.

## Overview

- **Isolation**: every `workspace_id` maps to its own directory under `DATA_ROOT/<workspace_id>`, keeping corpora separate.
- **Caching**: RAG-Anything instances are cached in-process and evicted after `CACHE_TTL_SECONDS` of inactivity. Cached state is finalized on shutdown and whenever a workspace is reset or deleted.
- **Concurrency**: ingestion for the same workspace is serialized through an async lock; different workspaces ingest in parallel.
- **Backends**: the app delegates parsing, embedding, and retrieval to RAG-Anything/LightRAG using OpenAI models (configurable). Heavy system tools (LibreOffice, tesseract, poppler) are preinstalled in the Docker image.
- **Authentication**: each request must provide the shared token as either `X-Webhook-Token: <APP_TOKEN>` or `Authorization: Bearer <APP_TOKEN>`.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `APP_TOKEN` | Shared secret for header auth | `changeme` |
| `OPENAI_API_KEY` | OpenAI (or compatible) API key | — |
| `OPENAI_BASE_URL` | Optional override for Azure/OpenRouter-style endpoints | empty |
| `LLM_MODEL` | Model passed to `openai_complete_if_cache` | `gpt-4o-mini` |
| `EMBEDDING_MODEL` | Embedding model used via `openai_embed` | `text-embedding-3-large` |
| `EMBEDDING_DIM` | Dimension expected from the embedding model | `3072` |
| `DATA_ROOT` | Root directory for workspace storage | `/data/out` |
| `DEFAULT_PARSER` | Default parser (`mineru` or `docling`) | `mineru` |
| `DEFAULT_PARSE_METHOD` | Default parse method (`auto`, `ocr`, `txt`) | `auto` |
| `CACHE_TTL_SECONDS` | Idle eviction window for cached RAG instances | `1800` |

> **Note:** The Dockerfile exports `PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu` so PyTorch CPU wheels are installed by default. Override in Coolify if you deploy on GPU hardware.

## Authentication

Include one of the following headers with every request (except `/healthz`):

- `X-Webhook-Token: <APP_TOKEN>`
- `Authorization: Bearer <APP_TOKEN>`

Requests missing or using the wrong token receive `401 Unauthorized`.

## Endpoints

All endpoints are rooted at the Coolify service FQDN, e.g., `https://raganything.example.com`.

### GET `/healthz`

Lightweight readiness probe.

**Response**

```json
{
  "ok": true,
  "data_root": "/data/out"
}
```

**cURL**

```bash
curl -fsS https://raganything.example.com/healthz
```

---

### POST `/workspaces/{workspace}/ingest`

Ingests documents into the given workspace. Supports remote URLs, base64-encoded files, and raw text snippets.

**Request body**

```json
{
  "reset": false,
  "parser": "mineru",
  "parse_method": "auto",
  "urls": ["https://example.com/manual.pdf"],
  "base64": [
    {"filename": "slide-deck.pptx", "b64": "<base64-blob>"}
  ],
  "texts": [
    {"filename": "overview.md", "text": "# Overview\n..."}
  ],
  "extra": {}
}
```

- `reset`: when `true`, deletes the existing workspace directory before ingesting.
- `parser` / `parse_method`: override the defaults per request (MinerU or Docling; `auto`, `ocr`, `txt`).
- `extra`: forwarded to `RAGAnything.process_document_complete` (future-proof for chunk controls).

**Response**

```json
{
  "workspace": "course-123",
  "ingested": [
    "/data/out/course-123/manual.pdf",
    "/data/out/course-123/slide-deck.pptx",
    "/data/out/course-123/overview.md"
  ],
  "stats": {
    "exists": true,
    "total_files": 3,
    "total_bytes": 5242880,
    "total_bytes_h": "5.00 MB",
    "by_extension": {".pdf": 1, ".pptx": 1, ".md": 1},
    "latest_modified_at": "2025-10-22T06:45:12.319Z",
    "files": [
      {"relpath": "manual.pdf", "bytes": 4194304, "bytes_h": "4.00 MB", "modified_at": "..."},
      ...
    ]
  }
}
```

**cURL**

```bash
curl -X POST https://raganything.example.com/workspaces/course-123/ingest \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: ${APP_TOKEN}" \
  -d '{
    "reset": true,
    "parser": "mineru",
    "parse_method": "auto",
    "urls": ["https://example.com/manual.pdf"],
    "texts": [{"filename":"overview.md","text":"# Intro\n..."}]
  }'
```

---

### POST `/workspaces/{workspace}/query`

Runs a retrieval-augmented query against the workspace. Supports hybrid, local, global, or graph modes and optional multimodal inputs (`images_b64`).

**Request body**

```json
{
  "query": "Summarize safety procedures and cite sources.",
  "mode": "hybrid",
  "top_k": 10,
  "images_b64": ["<optional base64 image>", "..."]
}
```

**Response**

```json
{
  "workspace": "course-123",
  "question": "Summarize safety procedures and cite sources.",
  "mode": "hybrid",
  "answer": "Safety procedures include ... [source:manual.pdf#p4]"
}
```

**cURL**

```bash
curl -X POST https://raganything.example.com/workspaces/course-123/query \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: ${APP_TOKEN}" \
  -d '{
    "query": "Summarize safety procedures and cite sources.",
    "mode": "hybrid",
    "top_k": 8
  }'
```

---

### POST `/workspaces/{workspace}/generate/course`

Retrieves context from the workspace and prompts the LLM to produce a structured course outline. The service attempts to parse the response as JSON and returns both the raw string and parsed content (if valid).

**Request body**

```json
{
  "topic": "Foundations of Data Science",
  "mode": "hybrid",
  "top_k": 12
}
```

**Response**

```json
{
  "workspace": "course-123",
  "topic": "Foundations of Data Science",
  "raw": "{\n  \"modules\": [...],\n  \"learning_objectives\": [...],\n ... }",
  "parsed": {
    "modules": [
      {"id": 1, "title": "Module 1: Introduction", "summary": "... [source:...]"},
      ...
    ],
    "learning_objectives": [
      {"id": 1, "text": "Describe ... [source:...]"},
      ...
    ],
    "prerequisites": ["..."],
    "audience": ["..."]
  }
}
```

**cURL**

```bash
curl -X POST https://raganything.example.com/workspaces/course-123/generate/course \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: ${APP_TOKEN}" \
  -d '{
    "topic": "Foundations of Data Science",
    "mode": "hybrid",
    "top_k": 12
  }'
```

---

### POST `/workspaces/{workspace}/generate/assessment`

Similar to the course generator, but tuned for assessments (MCQs, short-answer, practical tasks). Returns both raw text and parsed JSON if available.

**Request body**

```json
{
  "task": "Assess modules 1–3 with recall and application questions.",
  "mode": "hybrid",
  "top_k": 12
}
```

**Response**

```json
{
  "workspace": "course-123",
  "task": "Assess modules 1–3 with recall and application questions.",
  "raw": "{\n  \"mcq\": [...],\n  \"short_answer\": [...],\n  ... }",
  "parsed": {
    "mcq": [
      {
        "stem": "... [source:...]",
        "options": {"A": "...", "B": "..."},
        "correct": "B",
        "rationale": "...",
        "maps_to_LO": "LO-2",
        "difficulty": "medium"
      },
      ...
    ],
    "short_answer": [...],
    "practical": {...},
    "blueprint": {...}
  }
}
```

**cURL**

```bash
curl -X POST https://raganything.example.com/workspaces/course-123/generate/assessment \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: ${APP_TOKEN}" \
  -d '{
    "task": "Create a mixed assessment for modules 1-3.",
    "mode": "hybrid",
    "top_k": 12
  }'
```

---

### POST `/workspaces/{workspace}/reset`

Clears cached `RAGAnything` instances for the workspace. With `mode = "hard"`, also wipes the workspace directory on disk and recreates it.

**Request body**

```json
{ "mode": "hard" }
```

**Response**

```json
{
  "workspace": "course-123",
  "status": "reset-hard"
}
```

**cURL**

```bash
curl -X POST https://raganything.example.com/workspaces/course-123/reset \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: ${APP_TOKEN}" \
  -d '{"mode":"hard"}'
```

Use `"soft"` to only drop cached instances while leaving files intact.

---

### GET `/workspaces/{workspace}/stats`

Returns file counts, total storage, extension breakdown, and sample file metadata for a workspace.

**Response**

```json
{
  "workspace": "course-123",
  "stats": {
    "exists": true,
    "total_files": 5,
    "total_bytes": 7340032,
    "total_bytes_h": "7.00 MB",
    "by_extension": {".pdf": 2, ".pptx": 1, ".md": 2},
    "latest_modified_at": "2025-10-22T06:57:03.411Z",
    "files": [
      {"relpath": "manual.pdf", "bytes": 4194304, ...},
      ...
    ]
  }
}
```

**cURL**

```bash
curl -H "X-Webhook-Token: ${APP_TOKEN}" \
  https://raganything.example.com/workspaces/course-123/stats
```

---

### DELETE `/workspaces/{workspace}`

Finalizes and removes cached instances, deletes the workspace directory, and frees disk space.

**Response**

```json
{
  "workspace": "course-123",
  "deleted": true
}
```

**cURL**

```bash
curl -X DELETE https://raganything.example.com/workspaces/course-123 \
  -H "X-Webhook-Token: ${APP_TOKEN}"
```

---

## Operational tips

- Use `CACHE_TTL_SECONDS` to balance hot-workspace performance with memory usage. Setting it to `0` disables eviction (not recommended unless memory is plentiful).
- Workspace ingest is serialized per ID; schedule batch workloads accordingly if you need high throughput.
- The Docker image exposes `/data` as a volume. Ensure Coolify mounts persistent storage there so workspaces survive redeploys.
- For large or OCR-heavy documents, consider raising HTTP timeouts on the caller (n8n) or implementing a queue/worker pattern that polls `/stats` before querying.
- The course/assessment endpoints return both raw and parsed outputs; keep guardrails in n8n in case the language model returns malformed JSON.
- To reclaim disk space, use `/workspaces/{workspace}/reset` (hard) or `DELETE` after completing an automation run.
