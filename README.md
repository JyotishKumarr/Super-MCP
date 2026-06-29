# Super-MCP
# OData + OpenAPI MCP Server

Dynamically generates tools from any **OData `$metadata`** or **OpenAPI / Swagger spec** and lets you query them with natural language via **SAP AI Core (GPT-4o)**.

- Load once via `/generate` — tools persist to disk and **auto-load on every restart**
- `spec_id` is always optional — auto-detected when only one spec is loaded
- Credentials live **only in `.env` on the server** — never passed through API requests or seen by the LLM
- Full **RBAC** (read-only default, admin required for writes)

Two ways to use it:

| Mode | Entry point | Best for |
|------|------------|----------|
| **REST API** | `api.py` | curl, Postman, programmatic access; docs at `/docs` |
| **MCP server** | `mcp_server.py` | Claude Desktop, Claude Code, any MCP client |

---

## Project Structure

```
mcp/
├── .env                    # Credentials — SAP AI Core + per-spec API keys (never commit)
├── .env.example            # Template — copy to .env and fill in values
├── requirements.txt
│
├── api.py                  # FastAPI REST server — primary entry point
├── mcp_server.py           # FastMCP server for Claude Desktop / Claude Code
│
├── odata_parser.py         # Parses OData v2/v4 $metadata XML
├── odata_executor.py       # Executes OData HTTP calls (GET/POST/PATCH/DELETE + CSRF)
├── openapi_parser.py       # Parses OpenAPI 3.x and Swagger 2.0 (JSON or YAML)
├── openapi_executor.py     # Executes HTTP calls for OpenAPI operations + 401-retry
├── auth_manager.py         # Reads credentials from env vars; fetches/refreshes tokens
├── rbac_manager.py         # Role-based access control (default = read-only, admin = writes)
├── tool_generator.py       # Writes tools/{spec_id}.py from a parsed spec
├── sap_ai_client.py        # SAP AI Core OAuth2 + GPT-4o client
├── config.py               # Loads SAP AI Core config from .env
├── state.py                # In-memory spec / tool registry (shared by api.py + mcp_server.py)
│
└── tools/                  # Auto-generated tool files (auto-loaded on startup)
    └── .gitkeep
```

---

## Quick Setup

```bash
cd mcp
pip install -r requirements.txt
cp .env .env.example       # keep a credential-free template for the repo

# Start the REST API
python3 api.py
# → http://localhost:8080
# → Swagger UI: http://localhost:8080/docs
```

---

## REST API Endpoints

### OData

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/generate` | Fetch OData `$metadata` URL → parse → save `tools/{id}.py` |
| `POST` | `/api/v1/query` | Natural-language query against a loaded OData spec |

### OpenAPI / Swagger

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/generate/openapi` | Fetch OpenAPI/Swagger spec → parse → save `tools/{id}.py` |
| `POST` | `/api/v1/query/openapi` | Natural-language query against a loaded OpenAPI spec |

### Unified

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/ask` | Query anything — auto-detects spec type; `spec_id` always optional |
| `POST` | `/api/v1/call/{tool_name}` | Call a specific tool directly by name with explicit params |

### Auth

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/detect-auth` | Probe a URL without credentials — identify what auth is needed |
| `GET`  | `/api/v1/auth-status/{spec_id}` | Show whether credentials are configured (values never returned) |
| `POST` | `/api/v1/auth/invalidate/{spec_id}` | Force token refresh on the next call |

### Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/api/v1/specs` | List all loaded specs with entity/operation counts |
| `GET`  | `/api/v1/files` | List all generated tool files on disk |
| `DELETE` | `/api/v1/files/{spec_id}` | Delete a tool file from disk |
| `GET`  | `/health` | Health check — uptime and loaded spec count |

---

## OData Quick Start

### 1. Load a spec

```bash
# Northwind v2 (public demo)
curl -s -X POST http://localhost:8080/api/v1/generate \
  -H "Content-Type: application/json" \
  -d '{"source": "https://services.odata.org/V2/Northwind/Northwind.svc/$metadata",
       "spec_id": "nw"}' | jq

# TripPin v4 (public demo)
curl -s -X POST http://localhost:8080/api/v1/generate \
  -H "Content-Type: application/json" \
  -d '{"source": "https://services.odata.org/TripPinRESTierService/$metadata",
       "spec_id": "trip"}' | jq

# With GPT-4o enriched docstrings (~30s)
curl -s -X POST http://localhost:8080/api/v1/generate \
  -H "Content-Type: application/json" \
  -d '{"source": "https://services.odata.org/TripPinRESTierService/$metadata",
       "spec_id": "trip",
       "use_ai_descriptions": true}' | jq
```

### 2. Query in natural language

```bash
# spec_id auto-detected when only one spec is loaded
curl -s -X POST http://localhost:8080/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Get top 5 most expensive products"}' | jq

# explicit spec_id (required when multiple specs are loaded)
curl -s -X POST http://localhost:8080/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"spec_id": "trip", "query": "Get person with username russellwhyte"}' | jq

# cap the number of records
curl -s -X POST http://localhost:8080/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"query": "List airports", "max_records": 5}' | jq
```

### 3. Call tools directly

Tool names follow the pattern `{spec_id}__{EntitySet}__{operation}`:

```bash
# filter + sort + field selection
curl -s -X POST http://localhost:8080/api/v1/call/nw__Products__list \
  -H "Content-Type: application/json" \
  -d '{"top": 5, "orderby": "UnitPrice desc",
       "select": "ProductID,ProductName,UnitPrice"}' | jq

# single record by key
curl -s -X POST http://localhost:8080/api/v1/call/nw__Products__get \
  -H "Content-Type: application/json" \
  -d '{"ProductID": 1}' | jq
```

---

## OpenAPI / Swagger Quick Start

### 1. Load a spec

```bash
# Swagger 2.0
curl -s -X POST http://localhost:8080/api/v1/generate/openapi \
  -H "Content-Type: application/json" \
  -d '{"source": "https://petstore.swagger.io/v2/swagger.json",
       "spec_id": "petstore"}' | jq

# OpenAPI 3.x
curl -s -X POST http://localhost:8080/api/v1/generate/openapi \
  -H "Content-Type: application/json" \
  -d '{"source": "https://petstore3.swagger.io/api/v3/openapi.json",
       "spec_id": "ps3",
       "base_url": "https://petstore3.swagger.io/api/v3"}' | jq
```

For **authenticated services** — see the [Auth section](#auth--secure-credential-handling) below. Credentials go in `.env`, not in the request body.

### 2. Query

```bash
# auto-detected spec
curl -s -X POST http://localhost:8080/api/v1/query/openapi \
  -H "Content-Type: application/json" \
  -d '{"query": "Get all available pets"}' | jq

# unified endpoint (works for both OData and OpenAPI)
curl -s -X POST http://localhost:8080/api/v1/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "Find pets with status sold"}' | jq
```

### 3. Call tools directly

Tool names follow `{spec_id}__{operationId}`:

```bash
curl -s -X POST http://localhost:8080/api/v1/call/petstore__findPetsByStatus \
  -H "Content-Type: application/json" \
  -d '{"status": "available"}' | jq

curl -s -X POST http://localhost:8080/api/v1/call/petstore__getPetById \
  -H "Content-Type: application/json" \
  -d '{"petId": 1}' | jq
```

---

## Auth — Secure Credential Handling

Credentials are stored **only in `.env` on the server**. The LLM and API callers never provide or see credentials. Tokens are fetched and refreshed automatically at runtime.

### Supported auth types

| Type | `.env` value | Description |
|------|-------------|-------------|
| SAP XSUAA / OAuth2 | `xsuaa` or `oauth2` | Client-credentials flow; tokens auto-refreshed |
| Static bearer token | `bearer_static` | Fixed JWT/token — no refresh |
| HTTP Basic | `basic` | Username + password → Base64 Authorization header |
| API key (header) | `apikey` | Key injected into a request header |
| API key (query) | `apikey` | Key appended as a query parameter |
| API key (cookie) | `apikey` | Key sent as a cookie |
| None | `none` | Public APIs — no auth |

### Step 1 — Detect what auth is required

```bash
curl -s -X POST http://localhost:8080/api/v1/detect-auth \
  -H "Content-Type: application/json" \
  -d '{"url": "https://yourapp.cfapps.eu10.hana.ondemand.com",
       "spec_id": "myservice"}' | jq
```

The response shows the detected auth type and the exact env-var names to set:

```json
{
  "auth_required": true,
  "auth_type": "Bearer (SAP XSUAA)",
  "is_xsuaa": true,
  "configured": false,
  "env_vars_to_set": {
    "MYSERVICE_AUTH_TYPE":   "xsuaa",
    "MYSERVICE_AUTH_URL":    "<url from XSUAA service credentials>",
    "MYSERVICE_CLIENT_ID":   "<clientid>",
    "MYSERVICE_CLIENT_SECRET": "<clientsecret>"
  },
  "how_to_get_creds": "BTP Cockpit → Space → Service Instances → XSUAA → View Credentials"
}
```

### Step 2 — Add credentials to `.env`

The naming convention is `{SPEC_ID_UPPERCASE}_{VAR}`. Examples:

```env
# ── SAP BTP / XSUAA (OAuth2 client_credentials) ────────────────────────────
MYSERVICE_AUTH_TYPE=xsuaa
MYSERVICE_AUTH_URL=https://<subaccount>.authentication.eu10.hana.ondemand.com
MYSERVICE_CLIENT_ID=sb-app!t1234
MYSERVICE_CLIENT_SECRET=xxxxxxxxxxxxxxxx

# Optionally override the token endpoint (default: AUTH_URL/oauth/token)
MYSERVICE_TOKEN_URL=https://<subaccount>.authentication.eu10.hana.ondemand.com/oauth/token

# ── Static bearer token ─────────────────────────────────────────────────────
REPORTAPI_AUTH_TYPE=bearer_static
REPORTAPI_BEARER_TOKEN=eyJhbGciOiJSUzI1NiJ9...

# ── HTTP Basic auth ─────────────────────────────────────────────────────────
LEGACYAPI_AUTH_TYPE=basic
LEGACYAPI_USERNAME=admin
LEGACYAPI_PASSWORD=secret

# ── API key in a header ─────────────────────────────────────────────────────
WEATHERAPI_AUTH_TYPE=apikey
WEATHERAPI_API_KEY=abc123xyz
WEATHERAPI_API_KEY_NAME=X-API-Key
WEATHERAPI_API_KEY_IN=header       # header | query | cookie

# ── API key as query param ──────────────────────────────────────────────────
MAPSAPI_AUTH_TYPE=apikey
MAPSAPI_API_KEY=mymapskey
MAPSAPI_API_KEY_NAME=key
MAPSAPI_API_KEY_IN=query
```

Restart `api.py` — it loads `.env` on startup. For the MCP server, set these in `.env` the same way.

### Step 3 — Load the spec (no credentials needed in the request)

```bash
curl -s -X POST http://localhost:8080/api/v1/generate/openapi \
  -H "Content-Type: application/json" \
  -d '{"source": "https://yourapp.cfapps.eu10.hana.ondemand.com/api/openapi.json",
       "spec_id": "myservice"}' | jq
```

The server reads `MYSERVICE_AUTH_TYPE`, fetches an XSUAA token, and stores it internally. No credentials in the request.

### Step 4 — Query normally

```bash
curl -s -X POST http://localhost:8080/api/v1/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "Get all open orders"}' | jq
```

Before every outgoing API call the executor checks whether the cached token is still valid (60-second buffer). If expired it silently fetches a new one and retries. On an unexpected 401 it invalidates the cache and retries once.

### Auth management

```bash
# See configuration status — credential values are never returned
curl -s http://localhost:8080/api/v1/auth-status/myservice | jq
# → {"configured": true, "auth_type": "xsuaa",
#    "token_status": {"cached": true, "expires_in_seconds": 43140}}

# Force token refresh before the next call
curl -s -X POST http://localhost:8080/api/v1/auth/invalidate/myservice | jq
```

---

## RBAC — Role-Based Access Control

Every operation is gated by role. The **default role is read-only** — it can only call `list` and `get` (OData) and `GET`/`HEAD` (HTTP). Write operations (`create`, `update`, `delete` / `POST`, `PUT`, `PATCH`, `DELETE`) require the **admin role**.

### Configuring roles (environment variables)

```env
# ── Option 1: single-role for the whole server instance ────────────────────
MCP_USER_ROLE=admin           # everyone on this instance is admin

# ── Option 2: tie role to the server's email identity ──────────────────────
MCP_USER_EMAIL=alice@acme.com
RBAC_EMAIL_ROLES=alice@acme.com:admin,bob@acme.com:default

# ── Option 3: grant a role to an entire domain ─────────────────────────────
RBAC_DOMAIN_ROLES=acme.com:admin,partner.com:default

# ── Option 4: opaque API keys (HTTP / SSE multi-user transport) ─────────────
RBAC_USERS=secret_adminkey123:admin,readonly_xyz:default
```

Resolution order (first match wins): `MCP_USER_ROLE` → email exact match → email domain → `X-API-Key` header → fallback `default`.

### Checking your role (MCP tool)

```
whoami           → shows role, permitted operations, and RBAC config
```

### Error response when role is insufficient

```json
{
  "error": "This operation requires the 'admin' role. Current role: 'default'. Set MCP_USER_ROLE=admin or add your email to RBAC_EMAIL_ROLES.",
  "required_role": "admin"
}
```

---

## Persistence — Auto-load on Restart

Every `/generate` call writes `tools/{spec_id}.py` that embeds the full spec as base64. On the next startup:

```
server starts
 └─ scans tools/*.py
 └─ decodes embedded spec (no network call needed)
 └─ restores all in-memory state (specs, tools)
 └─ server is query-ready immediately
```

Auth credentials are **not** stored in tool files. They are re-read from `.env` on every execute call.

---

## MCP Server — Claude Desktop / Claude Code

### Start

```bash
# stdio (Claude Desktop / Claude Code)
python3 mcp_server.py

# SSE transport on a custom port
python3 mcp_server.py --sse 8001
# → http://localhost:8001/sse

# Streamable-HTTP transport
python3 mcp_server.py --http 8002
# → http://localhost:8002/mcp
```

### Claude Desktop config (`~/.claude/claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "odata-mcp": {
      "command": "python3",
      "args": ["/home/user/projects/mcp/mcp_server.py"]
    }
  }
}
```

### Static MCP tools (always available)

| Tool | Description |
|------|-------------|
| `load_odata_spec` | Load an OData `$metadata` URL and register CRUD tools |
| `load_openapi_spec` | Load an OpenAPI/Swagger spec and register operation tools |
| `smart_query` | Natural-language query over any loaded spec |
| `generate_tool_file` | Persist the current spec to `tools/{spec_id}.py` |
| `list_generated_tools` | List all dynamically registered tools |
| `list_tool_files` | List tool files on disk |
| `get_ai_insights` | Ask GPT-4o a question about a loaded spec |
| `generate_odata_query` | Convert natural language to an OData REST call |
| `test_ai_connection` | Ping SAP AI Core / GPT-4o |
| `whoami` | Show current role and permitted operations |

---

## How It Works

```
POST /api/v1/generate  (or load_odata_spec in MCP)
  └─ fetch spec from URL (follows redirects)
  └─ parse → EntitySets / operations, parameters, keys, security schemes
  └─ register tools in memory
  └─ write tools/{spec_id}.py (embedded spec as base64)

POST /api/v1/ask  (or smart_query in MCP)
  └─ resolve spec_id
  │   ├─ single spec loaded → use it automatically
  │   └─ multiple specs loaded → GPT-4o picks the right one
  └─ build compact spec summary (entity names, keys, fields)
  └─ GPT-4o returns JSON: { entity_set, operation, args, explanation }
  └─ RBAC check (write ops require admin role)
  └─ executor called with resolved args

OpenAPI executor
  └─ auth_manager.get_auth_headers(spec_id)
  │    └─ reads {SPEC_ID}_AUTH_TYPE from env
  │    └─ for xsuaa/oauth2: fetch token (client_credentials)
  │    └─ cache token with 60-second expiry buffer
  │    └─ return { Authorization: "Bearer <token>" }
  └─ route args by location: path / query / header / body / cookie
  └─ build URL: base_url + path (with path-param substitution)
  └─ HTTP call
  └─ on 401: invalidate cache → refresh token → retry once

OData executor
  └─ build OData URL: base_url/EntitySet(key)?$filter=...&$top=...
  └─ for write ops: fetch X-CSRF-Token first (SAP OData v2 requirement)
  └─ POST/PATCH/DELETE with CSRF token + cookies
  └─ return parsed JSON
```

### Tool naming convention

```
OData    → {spec_id}__{EntitySet}__{operation}
           nw__Products__list     nw__Products__get
           nw__Products__create   nw__Products__update   nw__Products__delete

OpenAPI  → {spec_id}__{operationId}
           petstore__findPetsByStatus   petstore__getPetById   petstore__addPet
```

---

## SAP AI Core Configuration

```env
SAP_AI_API_URL=https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com
SAP_AI_CLIENT_ID=<client_id from AI Core service binding>
SAP_AI_CLIENT_SECRET=<client_secret>
SAP_AI_AUTH_URL=https://<subaccount>.authentication.eu10.hana.ondemand.com
SAP_AI_RESOURCE_GROUP=default
SAP_AI_MODEL_NAME=gpt-4o

# Pin to a specific deployment (skips auto-discovery at startup)
SAP_AI_DEPLOYMENT_ID=d5c7fe212eec831c
```

The client auto-discovers the right deployment ID from `/v2/lm/deployments` if `SAP_AI_DEPLOYMENT_ID` is not set. It prefers `gpt-4o` → `gpt-4.1` → `gpt-4` → first RUNNING deployment.

---

## Environment Variables — Full Reference

### SAP AI Core

| Variable | Description |
|----------|-------------|
| `SAP_AI_API_URL` | AI Core API base URL |
| `SAP_AI_CLIENT_ID` | OAuth2 client ID |
| `SAP_AI_CLIENT_SECRET` | OAuth2 client secret |
| `SAP_AI_AUTH_URL` | XSUAA auth URL (without `/oauth/token`) |
| `SAP_AI_RESOURCE_GROUP` | AI Core resource group (default: `default`) |
| `SAP_AI_MODEL_NAME` | Model name (default: `gpt-4o`) |
| `SAP_AI_DEPLOYMENT_ID` | Pin to a specific deployment ID |

### RBAC

| Variable | Description |
|----------|-------------|
| `MCP_USER_ROLE` | `admin` or `default` — server-wide role override |
| `MCP_USER_EMAIL` | Email to look up in `RBAC_EMAIL_ROLES` |
| `RBAC_EMAIL_ROLES` | `email:role,email:role,...` — exact email → role |
| `RBAC_DOMAIN_ROLES` | `domain:role,...` — domain-level catch-all |
| `RBAC_USERS` | `apikey:role,...` — opaque API keys for HTTP transport |

### Per-spec auth (`{PREFIX}` = spec_id uppercased, hyphens → underscores)

| Variable | Description |
|----------|-------------|
| `{PREFIX}_AUTH_TYPE` | `xsuaa` / `oauth2` / `basic` / `apikey` / `bearer_static` / `none` |
| `{PREFIX}_AUTH_URL` | XSUAA / OAuth2 auth URL |
| `{PREFIX}_CLIENT_ID` | OAuth2 client ID |
| `{PREFIX}_CLIENT_SECRET` | OAuth2 client secret |
| `{PREFIX}_GRANT_TYPE` | OAuth2 grant type (default: `client_credentials`) |
| `{PREFIX}_TOKEN_URL` | Override the token endpoint |
| `{PREFIX}_BEARER_TOKEN` | Static bearer token |
| `{PREFIX}_USERNAME` | Basic auth username |
| `{PREFIX}_PASSWORD` | Basic auth password |
| `{PREFIX}_API_KEY` | API key value |
| `{PREFIX}_API_KEY_NAME` | API key header/param name (default: `X-API-Key`) |
| `{PREFIX}_API_KEY_IN` | Where to send the key: `header` / `query` / `cookie` |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `port already in use` | `lsof -ti:8080 \| xargs kill -9` |
| `No specs loaded` | First run: call `/generate`. Subsequent runs: tools auto-load from `tools/*.py`. |
| AI Core 404 on inference | Set `SAP_AI_DEPLOYMENT_ID` to the hex deployment ID shown in BTP cockpit. |
| 401 on API call | Run `POST /api/v1/detect-auth` → set env vars → restart server. |
| 401 persists after setting env vars | Token may be stale: `POST /api/v1/auth/invalidate/{spec_id}` |
| XSUAA token fetch fails | Verify `AUTH_URL` has no trailing slash; `GRANT_TYPE=client_credentials`. |
| CSRF error on OData write | Handled automatically — executor fetches `X-CSRF-Token` before write ops. |
| TripPin returns 0 results | Session URL changes on each access — `/generate` follows redirects automatically. |
| Write op denied (role error) | Set `MCP_USER_ROLE=admin` in `.env` or add your email to `RBAC_EMAIL_ROLES`. |
| `permission denied` on MCP | Call the `whoami` MCP tool to see your current role and how to upgrade it. |
