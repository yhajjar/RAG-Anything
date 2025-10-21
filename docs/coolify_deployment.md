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

Build context should point at the repository root so the Dockerfile and app folder are available during the image build.

### Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | ✅ | Key with access to `gpt-4o-mini` and `text-embedding-3-large` |
| `OPENAI_BASE_URL` | optional | Override for Azure/OpenRouter compatible endpoints |
| `RAGANYTHING_TOKEN` | optional | Shared secret enforced on `/ingest` and `/query` |
| `WORK_DIR` | optional | Defaults to `/data`; keep it pointing at the mounted volume |

### Persisted storage

Mount a Coolify volume to `/data` so document indexes survive container restarts and updates.

## Coolify steps

1. Create a new **Application → Dockerfile** deployment and link this repository.
2. Leave build settings at default (Dockerfile located in `deploy/coolify/Dockerfile`).
3. Add the environment variables from the table above.
4. Attach a volume and mount it to `/data`.
5. Keep the default exposed port `8000` and configure the health check to use `/healthz`.
6. Deploy; the container will start `uvicorn` with the FastAPI wrapper.

### Quick health test

```bash
curl -fsS https://<coolify-domain>/healthz
```

## Using the API from n8n

1. Add an **HTTP Request** node.
2. Set the URL to `https://<coolify-domain>/ingest` or `/query`.
3. Choose method `POST`, body content type `JSON`, and add a JSON payload for the respective endpoint:

```json
{
  "file_url": "https://example.com/manual.pdf",
  "filename": "manual.pdf",
  "doc_id": "oven-manual",
  "token": "YOUR_SHARED_TOKEN"
}
```

```json
{
  "question": "How do I install the Impava oven?",
  "mode": "hybrid",
  "token": "YOUR_SHARED_TOKEN"
}
```

4. Store `YOUR_SHARED_TOKEN` in n8n credentials or environment variables and map it into the JSON field.
5. Chain downstream workflow nodes to handle the `answer` and `sources` attributes from the response.

### Optional: ingest base64 content

If n8n downloads a file within the workflow, switch the HTTP node to send:

```json
{
  "file_b64": "{{ $json[\"fileData\"] }}",
  "filename": "upload.pdf",
  "doc_id": "doc-001",
  "token": "YOUR_SHARED_TOKEN"
}
```

Provide a preceding node that base64 encodes the binary data (e.g., n8n's **Move Binary Data** node).

## Next steps

- Protect the endpoint with your preferred gateway (Cloudflare, API Gateway, etc.) if you need more than shared-secret security.
- Swap in alternate `parser` or `parse_method` options in `deploy/coolify/app/main.py` to fine-tune ingestion behaviour.
