"""Read-only Meta Graph API client for Instagram media context."""

import re
from urllib.parse import quote

import httpx

from app.config import get_config
from app.integrations.meta_context.models import MetaMediaDetails

_GRAPH_VERSION_RE = re.compile(r"^v\d+\.\d+$")
_MEDIA_FIELDS = "id,permalink,caption,media_type,media_product_type,timestamp,thumbnail_url"


class MetaContextAPIError(RuntimeError):
    """Safe Meta context error that never includes credentials or response payloads."""


class MetaContextClient:
    def __init__(self, access_token: str, graph_api_version: str, timeout_seconds: float = 5.0):
        version = str(graph_api_version or "").strip()
        if not _GRAPH_VERSION_RE.fullmatch(version):
            raise MetaContextAPIError("Invalid Meta Graph API version")
        self._access_token = access_token
        self._base_url = f"https://graph.facebook.com/{version}"
        self._timeout = timeout_seconds

    @classmethod
    def from_config(cls) -> "MetaContextClient":
        config = get_config()
        return cls(config.instagram_access_token, config.meta_graph_api_version)

    async def get_media(self, media_id: str) -> MetaMediaDetails:
        safe_media_id = quote(str(media_id or "").strip(), safe="")
        if not safe_media_id:
            raise MetaContextAPIError("Missing Instagram media ID")
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                response = await client.get(
                    f"{self._base_url}/{safe_media_id}",
                    params={"fields": _MEDIA_FIELDS},
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
        except httpx.TimeoutException as exc:
            raise MetaContextAPIError("Meta Graph API request timed out") from exc
        except httpx.RequestError as exc:
            raise MetaContextAPIError("Meta Graph API request failed") from exc
        if response.status_code != 200:
            raise MetaContextAPIError(f"Meta Graph API request failed with status {response.status_code}")
        try:
            media = MetaMediaDetails.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise MetaContextAPIError("Meta Graph API returned invalid media data") from exc
        if media.id != str(media_id).strip():
            raise MetaContextAPIError("Meta Graph API returned a different media ID")
        return media
