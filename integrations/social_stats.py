from __future__ import annotations

from typing import Any, Dict

import httpx


class SocialStatsError(RuntimeError):
    pass


class SocialStatsClient:
    """Read follower counts through the platforms' authenticated APIs."""

    def __init__(
        self,
        *,
        tiktok_access_token: str = "",
        instagram_access_token: str = "",
        instagram_user_id: str = "",
        instagram_api_url: str = "https://graph.instagram.com/v24.0",
    ) -> None:
        self.tiktok_access_token = tiktok_access_token.strip()
        self.instagram_access_token = instagram_access_token.strip()
        self.instagram_user_id = instagram_user_id.strip()
        self.instagram_api_url = instagram_api_url.rstrip("/")

    @property
    def tiktok_configured(self) -> bool:
        return bool(self.tiktok_access_token)

    @property
    def instagram_configured(self) -> bool:
        return bool(self.instagram_access_token and self.instagram_user_id)

    async def tiktok_followers(self) -> int:
        if not self.tiktok_configured:
            raise SocialStatsError("не настроен TIKTOK_ACCESS_TOKEN")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    "https://open.tiktokapis.com/v2/user/info/",
                    params={"fields": "username,follower_count"},
                    headers={"Authorization": f"Bearer {self.tiktok_access_token}"},
                )
        except httpx.HTTPError as error:
            raise SocialStatsError(f"TikTok временно недоступен: {error}") from error
        data = _json_response(response, "TikTok")
        error = data.get("error") or {}
        if error.get("code") not in (None, "", "ok"):
            raise SocialStatsError(
                f"TikTok: {error.get('message') or error.get('code')}"
            )
        user = (data.get("data") or {}).get("user") or {}
        return _follower_count(user, "TikTok")

    async def instagram_followers(self) -> int:
        if not self.instagram_configured:
            raise SocialStatsError(
                "не настроены INSTAGRAM_ACCESS_TOKEN и INSTAGRAM_USER_ID"
            )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    f"{self.instagram_api_url}/{self.instagram_user_id}",
                    params={
                        "fields": "username,followers_count",
                        "access_token": self.instagram_access_token,
                    },
                )
        except httpx.HTTPError as error:
            raise SocialStatsError(f"Instagram временно недоступен: {error}") from error
        data = _json_response(response, "Instagram")
        if data.get("error"):
            error = data["error"]
            raise SocialStatsError(
                f"Instagram: {error.get('message') or error.get('code') or 'ошибка API'}"
            )
        return _follower_count(data, "Instagram")


def _json_response(response: httpx.Response, platform: str) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError as error:
        raise SocialStatsError(
            f"{platform} вернул некорректный ответ HTTP {response.status_code}"
        ) from error
    if response.is_error:
        message = data.get("error") or data.get("message") or data
        raise SocialStatsError(f"{platform}: HTTP {response.status_code}: {message}")
    if not isinstance(data, dict):
        raise SocialStatsError(f"{platform} вернул некорректные данные")
    return data


def _follower_count(data: Dict[str, Any], platform: str) -> int:
    value = data.get("follower_count", data.get("followers_count"))
    try:
        count = int(value)
    except (TypeError, ValueError) as error:
        raise SocialStatsError(f"{platform} не вернул число подписчиков") from error
    if count < 0:
        raise SocialStatsError(f"{platform} вернул некорректное число подписчиков")
    return count
