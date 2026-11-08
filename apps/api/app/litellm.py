"""Thin client for the LiteLLM proxy (virtual keys + request forwarding).

The master key stays server-side. NOTE: payload shapes follow LiteLLM's documented
/key/generate, /key/update and /key/delete endpoints; validate against the pinned LiteLLM
version in Phase 0 (roadmap §33) before production use.
"""

import httpx

from app.config import get_settings
from app.deps import ApiError


class LiteLLMClient:
    def __init__(self, base_url: str, master_key: str, timeout: float,
                 transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.master_key = master_key
        self.timeout = timeout
        self._transport = transport

    # ---- management (sync, master key) -------------------------------------------------
    def _manage(self, path: str, payload: dict) -> dict:
        try:
            with httpx.Client(timeout=30.0, transport=self._transport) as c:
                r = c.post(f"{self.base_url}{path}", json=payload,
                           headers={"Authorization": f"Bearer {self.master_key}"})
        except httpx.HTTPError as e:
            raise ApiError(502, "GATEWAY_UNAVAILABLE", "Model gateway is unavailable.") from e
        if r.status_code >= 400:
            raise ApiError(502, "GATEWAY_ERROR", f"Model gateway rejected the request ({r.status_code}).")
        return r.json() if r.content else {}

    def generate_key(self, *, key: str, alias: str, models: list[str], duration: str | None,
                     rpm: int | None, tpm: int | None, metadata: dict) -> str:
        """Create a virtual key with a caller-chosen secret. Returns LiteLLM's key id (hash)."""
        body = {"key": key, "key_alias": alias, "models": models, "duration": duration,
                "rpm_limit": rpm, "tpm_limit": tpm, "metadata": metadata}
        data = self._manage("/key/generate", {k: v for k, v in body.items() if v is not None})
        return data.get("token") or data.get("token_id") or ""

    def update_key(self, key_id: str, **fields) -> None:
        self._manage("/key/update", {"key": key_id, **{k: v for k, v in fields.items() if v is not None}})

    def delete_key(self, key_id: str) -> None:
        self._manage("/key/delete", {"keys": [key_id]})

    def health(self, model_name: str, timeout: float) -> dict:
        """Probe one model through LiteLLM's /health?model=... (it makes a minimal backend call)."""
        try:
            with httpx.Client(timeout=timeout, transport=self._transport) as c:
                r = c.get(f"{self.base_url}/health", params={"model": model_name},
                          headers={"Authorization": f"Bearer {self.master_key}"})
        except httpx.HTTPError as e:
            return {"healthy": False, "detail": f"gateway unreachable: {type(e).__name__}"}
        if r.status_code >= 400:
            return {"healthy": False, "detail": f"gateway returned {r.status_code}"}
        data = r.json()
        healthy, bad = int(data.get("healthy_count") or 0), int(data.get("unhealthy_count") or 0)
        ok = healthy > 0 and bad == 0
        return {"healthy": ok, "detail": "ok" if ok else f"{bad} unhealthy / {healthy} healthy endpoints"}

    # ---- request forwarding (async, caller's own virtual key) --------------------------
    async def forward(self, path: str, body: dict, caller_key: str) -> httpx.Response:
        """Send a streaming request; caller must iterate and close the response."""
        client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0),
                                   transport=self._transport)
        try:
            req = client.build_request("POST", f"{self.base_url}{path}", json=body,
                                       headers={"Authorization": f"Bearer {caller_key}"})
            resp = await client.send(req, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise
        resp.extensions["nexora_client"] = client
        return resp


def get_litellm() -> LiteLLMClient:
    s = get_settings()
    return LiteLLMClient(s.litellm_base_url, s.litellm_master_key, s.gateway_timeout_seconds)
