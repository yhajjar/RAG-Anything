# Deploy RAG-Anything on Coolify

This guide walks through exposing RAG-Anything as a FastAPI microservice that can be deployed as a Coolify application and called from automation platforms such as n8n.

## Container layout

```
deploy/coolify/
├─ Dockerfile
├─ requirements.txt
└─ app/
   └─ main.py
```

Root also contains an optional Compose manifest:

```
docker-compose.coolify.yml
```

Build context should point at the repository root so the Dockerfile and app folder are available during the image build.

### Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | ✅ | Key with access to `gpt-4o-mini` and `text-embedding-3-large` |
| `OPENAI_BASE_URL` | optional | Override for Azure/OpenRouter compatible endpoints |
| `APP_TOKEN` | ✅ | Shared secret validated via `X-Webhook-Token` or Bearer auth |
| `DATA_ROOT` | optional | Defaults to `/data/out`; working dir root for workspaces |
| `DEFAULT_PARSER` | optional | `mineru` (default) or `docling` |
| `DEFAULT_PARSE_METHOD` | optional | `auto`, `ocr`, or `txt` |
| `CACHE_TTL_SECONDS` | optional | Idle cache eviction window (default 1800) |

### Persisted storage

Mount a Coolify volume to `/data` so workspace folders survive container restarts and updates. Each workspace lives under `DATA_ROOT/<workspace_id>`.

## Using Docker Compose (alternative)

If you prefer Coolify's “Docker Compose” application type, reuse the provided file `docker-compose.coolify.yml` at the repository root. Key pointers:

1. In Coolify choose **Create → Docker Compose** and point the repository to this branch.
2. Leave the Compose file path at `docker-compose.coolify.yml` (Coolify reads it automatically from the repo root).
3. Under **Environment Variables**, add `OPENAI_API_KEY`, optional `OPENAI_BASE_URL`, and `RAGANYTHING_TOKEN`.
4. Coolify will create the named volume `raganything-data` automatically during the first deploy and mount it to `/data`.
5. The compose file already defines the health check and exposes port `8000`; Traefik handles TLS and routing.

## Coolify steps

1. Create a new **Application → Dockerfile** deployment and link this repository.
2. Set Dockerfile path to `deploy/coolify/Dockerfile`.
3. Add the environment variables from the table above (`OPENAI_API_KEY`, `APP_TOKEN`, etc.).
4. Attach a volume and mount it to `/data`.
5. Keep the exposed port at `8000` and configure the health check to use `/healthz`.
6. Deploy; the container will start `uvicorn` serving the workspace API.

### Quick health test

```bash
curl -fsS https://<coolify-domain>/healthz
```

## Using the API from n8n

The FastAPI service exposes workspace-scoped endpoints:

- `POST /workspaces/{workspace}/ingest`
- `POST /workspaces/{workspace}/query`
- `POST /workspaces/{workspace}/generate/course`
- `POST /workspaces/{workspace}/generate/assessment`
- `POST /workspaces/{workspace}/reset`
- `GET  /workspaces/{workspace}/stats`
- `DELETE /workspaces/{workspace}`

1. Add an **HTTP Request** node.
2. Set the URL to the desired workspace endpoint, e.g., `https://<coolify-domain>/workspaces/{{ $json.workspace_id }}/ingest`.
3. Choose method `POST`, body content type `JSON`, and add a JSON payload for the respective endpoint:

```json
{ 
  "reset": true,
  "parser": "mineru",
  "parse_method": "auto",
  "urls": ["https://example.com/manual.pdf"],
  "texts": [{"filename":"notes.md","text":"Intro text"}]
} 
```

```json
{
  "query": "Summarize safety requirements and cite sources.",
  "mode": "hybrid",
  "top_k": 10
}
```

4. Set header `X-Webhook-Token: {{ $env.APP_TOKEN }}`.
5. Chain downstream workflow nodes to handle responses (`answer`, `stats`, or structured generation outputs).

### Optional: ingest base64 content

If n8n downloads a file within the workflow, switch the HTTP node to send:

```json
{
  "file_b64": "{{ $json[\"fileData\"] }}",
  "filename": "upload.pdf",
  "parser": "mineru"
}
```

Provide a preceding node that base64 encodes the binary data (e.g., n8n's **Move Binary Data** node).

## Next steps

- Protect the endpoint with your preferred gateway (Cloudflare, API Gateway, etc.) if you need more than shared-secret security.
- Tune parser defaults or cache eviction (`CACHE_TTL_SECONDS`) via environment variables.
- Extend the prompts in `deploy/coolify/app/main.py` if you need additional generation endpoints.
