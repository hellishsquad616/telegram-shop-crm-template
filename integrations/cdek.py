import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx


class CdekError(RuntimeError):
    pass


class CdekClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        shipment_creation_enabled: bool = False,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.shipment_creation_enabled = shipment_creation_enabled
        self._token: Optional[str] = None
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _access_token(self) -> str:
        if not self.configured:
            raise CdekError("CDEK_CLIENT_ID и CDEK_CLIENT_SECRET не настроены")
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.base_url}/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
        except httpx.HTTPError as error:
            raise CdekError(f"СДЭК временно недоступен при авторизации: {error}") from error
        if response.is_error:
            raise CdekError(f"Ошибка авторизации СДЭК: HTTP {response.status_code}")
        try:
            data = response.json()
            self._token = str(data["access_token"])
        except (ValueError, KeyError, TypeError) as error:
            raise CdekError("СДЭК вернул некорректный ответ авторизации") from error
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(
                    method, f"{self.base_url}{path}", headers=headers, **kwargs
                )
        except httpx.HTTPError as error:
            raise CdekError(f"СДЭК временно недоступен: {error}") from error
        try:
            data = response.json()
        except ValueError as error:
            raise CdekError(f"СДЭК вернул не-JSON ответ: HTTP {response.status_code}") from error
        if response.is_error:
            message = data.get("message") or data.get("errors") or data
            raise CdekError(f"Ошибка СДЭК HTTP {response.status_code}: {message}")
        return data

    async def get_order(self, order_uuid: str) -> Dict[str, Any]:
        return await self._request("GET", f"/orders/{order_uuid}")

    async def find_order_by_im_number(self, im_number: str) -> Optional[Dict[str, Any]]:
        """Ищет накладную по номеру заказа в информационной системе магазина."""
        try:
            return await self._request(
                "GET",
                "/orders",
                params={"im_number": str(im_number).strip()},
            )
        except CdekError as error:
            message = str(error)
            if (
                "HTTP 404" in message
                or "v2_entity_not_found_im_number" in message
                or "Entity is not found by number" in message
            ):
                return None
            raise

    async def update_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not payload.get("uuid"):
            raise CdekError("Для обновления накладной отсутствует UUID заказа СДЭК")
        return await self._request("PATCH", "/orders", json=payload)

    async def create_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.shipment_creation_enabled:
            raise CdekError(
                "Создание накладных СДЭК отключено: трек-номера создаёт МойСклад"
            )
        return await self._request("POST", "/orders", json=payload)

    async def delete_order(self, order_uuid: str) -> Dict[str, Any]:
        if not str(order_uuid).strip():
            raise CdekError("Для удаления накладной отсутствует UUID заказа СДЭК")
        return await self._request("DELETE", f"/orders/{str(order_uuid).strip()}")

    async def get_cities(self, city: str, size: int = 20) -> Any:
        return await self._request(
            "GET",
            "/location/cities",
            params={"city": str(city).strip(), "size": max(1, min(size, 1000))},
        )

    async def get_delivery_points(self, city_code: int, size: int = 100) -> Any:
        return await self._request(
            "GET",
            "/deliverypoints",
            params={
                "city_code": int(city_code),
                "type": "PVZ",
                "is_handout": "true",
                "size": max(1, min(size, 1000)),
            },
        )

    async def calculate_tariff(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/calculator/tariff", json=payload)

    async def create_client_return(self, order_uuid: str, tariff_code: int) -> Dict[str, Any]:
        if not self.shipment_creation_enabled:
            raise CdekError(
                "Создание возвратных накладных СДЭК отключено: "
                "трек-номера создаёт МойСклад"
            )
        return await self._request(
            "POST", f"/orders/{order_uuid}/clientReturn", json={"tariff_code": tariff_code}
        )

    async def request_invoice(self, order_uuid: str) -> Dict[str, Any]:
        return await self.request_invoices([order_uuid])

    async def request_invoices(self, order_uuids: list[str]) -> Dict[str, Any]:
        values = [str(value).strip() for value in order_uuids if str(value).strip()]
        if not values:
            raise CdekError("Не выбраны накладные для печати")
        return await self._request(
            "POST",
            "/print/orders",
            json={
                "orders": [{"order_uuid": value} for value in values],
                "copy_count": 1,
            },
        )

    async def get_invoice(self, print_uuid: str) -> Dict[str, Any]:
        if not str(print_uuid).strip():
            raise CdekError("Не указан UUID печатной формы")
        return await self._request("GET", f"/print/orders/{str(print_uuid).strip()}")

    async def download_file(self, url: str) -> bytes:
        parsed = urlparse(str(url))
        base_host = (urlparse(self.base_url).hostname or "").lower()
        file_host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not file_host:
            raise CdekError("СДЭК не вернул безопасную ссылку на PDF")
        if file_host != base_host and not file_host.endswith(".cdek.ru"):
            raise CdekError("СДЭК вернул ссылку на неизвестный сервер")
        token = await self._access_token()
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                response = await client.get(
                    str(url),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/pdf",
                    },
                )
        except httpx.HTTPError as error:
            raise CdekError(f"Не удалось скачать PDF СДЭК: {error}") from error
        if response.is_error:
            raise CdekError(
                f"Не удалось скачать PDF СДЭК: HTTP {response.status_code}"
            )
        if not response.content.startswith(b"%PDF"):
            raise CdekError("СДЭК вернул файл не в формате PDF")
        return response.content
