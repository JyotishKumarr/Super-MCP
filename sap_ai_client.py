"""
SAP AI Core client — handles OAuth2 token acquisition (with caching)
and exposes a chat_completion method compatible with the OpenAI API
format that AI Core deployments use.
"""

import os
import time
import httpx
from typing import Optional


class TokenCache:
    def __init__(self):
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def get(self) -> Optional[str]:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        return None

    def set(self, token: str, expires_in: int):
        self._token = token
        self._expires_at = time.time() + expires_in


class SAPAIClient:
    def __init__(self, config: dict):
        self.ai_api_url = config["ai_api_url"].rstrip("/")
        self.client_id = config["client_id"]
        self.client_secret = config["client_secret"]
        self.token_url = config["auth_url"].rstrip("/") + "/oauth/token"
        self.resource_group = config.get("resource_group", "default")
        self.model_name = config.get("model_name", "gpt-4o")
        self._cache = TokenCache()
        self._deployment_id: Optional[str] = None

    async def _get_token(self) -> str:
        cached = self._cache.get()
        if cached:
            return cached
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            token = data["access_token"]
            expires_in = int(data.get("expires_in", 43200))
            self._cache.set(token, expires_in)
            return token

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "AI-Resource-Group": self.resource_group,
            "Content-Type": "application/json",
        }

    async def list_deployments(self) -> list[dict]:
        """List all LM deployments via the correct SAP AI Core path."""
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.ai_api_url}/v2/lm/deployments",
                headers=self._headers(token),
            )
            if not resp.is_success:
                raise RuntimeError(f"AI Core [{resp.status_code}]: {resp.text[:400]}")
            data = resp.json()
            return data.get("resources", [])

    async def _resolve_deployment_id(self) -> str:
        """
        Return a deployment ID.
        Priority: SAP_AI_DEPLOYMENT_ID env var → auto-discover from /v2/lm/deployments.
        Prefers gpt-4o, then any gpt-4 variant, then first RUNNING deployment.
        """
        override = os.getenv("SAP_AI_DEPLOYMENT_ID", "")
        if override:
            return override

        if self._deployment_id:
            return self._deployment_id

        deployments = await self.list_deployments()
        running = [d for d in deployments if d.get("status") == "RUNNING"]

        def model_label(d: dict) -> str:
            return (
                d.get("details", {}).get("resources", {})
                 .get("backendDetails", {}).get("model", {}).get("name")
                or d.get("configurationName", "")
            ).lower()

        # Exact-match first to avoid gpt-4o matching gpt-4o-mini
        preferred_exact = ["gpt-4o", "gpt-4.1", "gpt-4", "gpt-5"]
        for pref in preferred_exact:
            for dep in running:
                if model_label(dep) == pref:
                    self._deployment_id = dep["id"]
                    self.model_name = model_label(dep)
                    return self._deployment_id
        # Substring fallback
        for pref in preferred_exact:
            for dep in running:
                if pref in model_label(dep):
                    self._deployment_id = dep["id"]
                    self.model_name = model_label(dep)
                    return self._deployment_id

        if running:
            self._deployment_id = running[0]["id"]
            self.model_name = model_label(running[0])
            return self._deployment_id

        raise RuntimeError("No RUNNING deployments found in SAP AI Core.")

    async def _inference_url(self) -> str:
        dep_id = await self._resolve_deployment_id()
        return f"{self.ai_api_url}/v2/inference/deployments/{dep_id}/v1/chat/completions"

    async def chat_completion(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> str:
        """
        Call GPT-4 (or configured model) via SAP AI Core Gen AI Hub.
        Uses model name as the deployment segment; override with SAP_AI_DEPLOYMENT_ID env var.
        """
        token = await self._get_token()
        url = await self._inference_url()
        model = os.getenv("SAP_AI_MODEL_NAME", self.model_name)

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=self._headers(token), json=payload)
            if not resp.is_success:
                raise RuntimeError(
                    f"AI Core inference failed [{resp.status_code}]: {resp.text[:600]}\n"
                    f"URL: {url}\n"
                    "Tip: If using a deployment ID, set SAP_AI_DEPLOYMENT_ID in .env"
                )
            data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"AI Core returned no choices: {data}")
        return choices[0]["message"]["content"]

    async def generate_tool_description(self, entity_name: str, properties: list[dict]) -> str:
        """Use AI Core to generate a human-friendly tool description for an OData entity."""
        prop_summary = ", ".join(
            f"{p['name']} ({p['type']})" for p in properties[:10]
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You generate concise, one-sentence tool descriptions for OData REST API operations. "
                    "Be specific about what the entity represents based on its name and properties."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Write a one-sentence description for tools that query the '{entity_name}' entity. "
                    f"Its key properties are: {prop_summary}. "
                    "Example: 'Access customer master data including name, address, and credit limit.'"
                ),
            },
        ]
        try:
            return await self.chat_completion(messages, max_tokens=100)
        except Exception:
            return f"Manage {entity_name} records via the OData API."

    async def analyze_spec(self, spec_summary: str, question: str) -> str:
        """Answer a user question about an OData spec using AI Core."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert in SAP OData APIs. "
                    "You help users understand OData service structures, query patterns, and data relationships. "
                    "Be concise and practical, include OData query examples where helpful."
                ),
            },
            {
                "role": "user",
                "content": f"OData service summary:\n{spec_summary}\n\nQuestion: {question}",
            },
        ]
        return await self.chat_completion(messages, max_tokens=1024)

    async def generate_query(self, spec_summary: str, natural_language: str) -> str:
        """Convert a natural language request into an OData query or API call."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You convert natural language into OData REST API calls. "
                    "Return a JSON object with: method (GET/POST/PATCH/DELETE), "
                    "path (e.g. /Orders?$filter=...), body (for POST/PATCH), and explanation. "
                    "Use OData v4 syntax by default, v2 if the spec is v2."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"OData service:\n{spec_summary}\n\n"
                    f"Generate the API call for: {natural_language}"
                ),
            },
        ]
        return await self.chat_completion(messages, max_tokens=512)
