import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


class TildaPromocodeError(RuntimeError):
    pass


def save_storage_state(context: BrowserContext, state_path: Path) -> None:
    """Replace the session atomically, with private permissions from creation."""
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tilda_state_", dir=state_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(context.storage_state(), file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, state_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class TildaPromocodeClient:
    """Automates the official Tilda promo-code UI and verifies every mutation."""

    OWNERSHIP_PREFIX = "[SHOPCRM-CRM:"

    def __init__(
        self,
        email: str,
        password: str,
        project_id: str,
        *,
        state_path: Optional[Path] = None,
    ) -> None:
        self.email = email.strip()
        self.password = password
        self.project_id = str(project_id).strip()
        self.state_path = Path(state_path) if state_path else None
        self._lock: Optional[asyncio.Lock] = None

    @property
    def configured(self) -> bool:
        # A manually authenticated storage state is sufficient; forcing a
        # password as well made valid saved sessions appear "not configured".
        has_session = bool(self.state_path and self.state_path.exists())
        return bool(self.project_id and (has_session or (self.email and self.password)))

    @property
    def url(self) -> str:
        return (
            "https://tilda.ru/projects/payments/promocodes/"
            f"?projectid={self.project_id}"
        )

    @classmethod
    def ownership_marker(cls, local_id: int) -> str:
        return f"{cls.OWNERSHIP_PREFIX}{int(local_id)}]"

    @classmethod
    def owned_description(cls, local_id: int, description: str) -> str:
        marker = cls.ownership_marker(local_id)
        clean = str(description or "").strip()
        return f"{marker} {clean}".strip()[:255]

    @staticmethod
    def discount_for_tilda(promo: Dict[str, Any]) -> str:
        value = str(promo["discount_value"]).strip()
        return f"{value}%" if promo["discount_type"] == "percent" else value

    async def create(self, promo: Dict[str, Any]) -> str:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            return await asyncio.to_thread(self._create_sync, dict(promo))

    async def delete(self, promo: Dict[str, Any]) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, dict(promo))

    async def check_connection(self) -> int:
        """Authenticate and return the promo row count without changing Tilda."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            return await asyncio.to_thread(self._check_connection_sync)

    def _new_context(self, playwright) -> BrowserContext:
        browser = playwright.chromium.launch(headless=True)
        kwargs: Dict[str, Any] = {}
        if self.state_path and self.state_path.exists():
            kwargs["storage_state"] = str(self.state_path)
        return browser.new_context(**kwargs)

    def _save_state(self, context: BrowserContext) -> None:
        if not self.state_path:
            return
        save_storage_state(context, self.state_path)

    @staticmethod
    def _is_login_page(page: Page) -> bool:
        return urlsplit(page.url).path.rstrip("/") == "/login"

    @staticmethod
    def _login_failure(page: Page) -> str:
        # Do not copy page text into Telegram: it may contain account details.
        try:
            body = page.locator("body").inner_text(timeout=5_000).lower()
        except PlaywrightTimeout:
            body = ""
        if any(text in body for text in (
            "let us know you are human", "captcha", "капч", "не робот",
            "подтвердите, что вы", "prove you are human",
        )):
            return (
                "Tilda требует проверку «Я не робот». Администратору нужно "
                "восстановить сессию сервера через tilda_login.py --server SSH_АДРЕС "
                "на компьютере с браузером. После входа повторите передачу промокода."
            )
        return (
            "Tilda не выполнила вход. Проверьте TILDA_EMAIL и TILDA_PASSWORD "
            "на сервере. Для ручного входа используйте tilda_login.py --server "
            "SSH_АДРЕС на компьютере с браузером."
        )

    def _open_authenticated(self, context: BrowserContext) -> Page:
        if not self.configured:
            raise TildaPromocodeError(
                "Не настроены TILDA_EMAIL, TILDA_PASSWORD и "
                "TILDA_PROMOCODES_PROJECT_ID"
            )
        page = context.new_page()
        page.goto(self.url, wait_until="domcontentloaded", timeout=60_000)
        if self._is_login_page(page):
            if not self.email or not self.password:
                raise TildaPromocodeError(
                    "Сохранённая сессия Tilda истекла. Восстановите сессию через "
                    "tilda_login.py --server SSH_АДРЕС на компьютере с браузером "
                    "или заполните TILDA_EMAIL и TILDA_PASSWORD на сервере."
                )
            page.locator("#email").fill(self.email)
            page.locator("#password").fill(self.password)
            page.locator("#send").click()
            try:
                page.wait_for_function(
                    "() => !location.pathname.startsWith('/login')",
                    timeout=60_000,
                )
            except PlaywrightTimeout as error:
                raise TildaPromocodeError(self._login_failure(page)) from error
            page.goto(self.url, wait_until="domcontentloaded", timeout=60_000)
        if self._is_login_page(page):
            raise TildaPromocodeError("Tilda отклонила авторизацию")
        try:
            page.locator(".js-create-button").wait_for(state="visible", timeout=30_000)
        except PlaywrightTimeout as error:
            raise TildaPromocodeError(
                "Раздел промокодов Tilda не загрузился или аккаунт не имеет доступа"
            ) from error
        self._save_state(context)
        return page

    @staticmethod
    def _rows(page: Page) -> List[Dict[str, str]]:
        return page.locator("tr[id^='promocode']").evaluate_all(
            """
            rows => rows.map(row => ({
              id: row.id.replace('promocode', ''),
              code: (row.querySelector('.td-promocode__code')?.textContent || '').trim()
            }))
            """
        )

    @classmethod
    def _find_row(cls, page: Page, code: str) -> Optional[Dict[str, str]]:
        target = str(code).strip().upper()
        return next(
            (row for row in cls._rows(page) if row["code"].upper() == target),
            None,
        )

    @classmethod
    def _assert_owned(cls, page: Page, remote_id: str, local_id: int) -> None:
        try:
            page.evaluate(
                "remoteId => td__paymentspromocodes__onEdit(Number(remoteId))",
                str(remote_id),
            )
            description = page.locator("input[name='promocodes[name]']").input_value(
                timeout=10_000
            )
        except Exception as error:
            raise TildaPromocodeError(
                "Не удалось подтвердить принадлежность промокода боту"
            ) from error
        if not description.startswith(cls.ownership_marker(local_id)):
            raise TildaPromocodeError(
                "Защитная отмена: этот промокод не был создан ботом"
            )

    @staticmethod
    def _submit(page: Page, data: Dict[str, str]) -> str:
        try:
            return page.evaluate(
                """
                payload => new Promise((resolve, reject) => {
                  td__ajax({
                    url: '/projects/payments/submit/',
                    responseType: 'text',
                    dataToSend: payload,
                    onSuccess: response => resolve(response || ''),
                    onError: error => reject(new Error(String(error || 'request failed')))
                  });
                })
                """,
                data,
            )
        except Exception as error:
            raise TildaPromocodeError("Tilda не выполнила запрос сохранения") from error

    @staticmethod
    def _parse_response(raw_text: str) -> Dict[str, Any]:
        text = str(raw_text or "").strip()
        try:
            data = json.loads(text) if text.startswith("{") else {}
        except json.JSONDecodeError as error:
            raise TildaPromocodeError("Tilda вернула некорректный ответ") from error
        if data.get("error"):
            raise TildaPromocodeError(f"Ошибка Tilda: {data['error']}")
        return data

    def _create_sync(self, promo: Dict[str, Any]) -> str:
        local_id = int(promo["id"])
        code = str(promo["code"]).strip().upper()
        with sync_playwright() as playwright:
            context = self._new_context(playwright)
            try:
                page = self._open_authenticated(context)
                existing = self._find_row(page, code)
                if existing:
                    self._assert_owned(page, existing["id"], local_id)
                    return existing["id"]
                response_text = self._submit(
                    page,
                    {
                        "comm": "createpromocode",
                        "projectid": self.project_id,
                        "promocodeid": "",
                        "promocodes[projectid]": self.project_id,
                        "promocodes[code]": code,
                        "promocodes[discount]": self.discount_for_tilda(promo),
                        "promocodes[cnt]": str(int(promo.get("usage_limit") or 0) or ""),
                        "promocodes[expired]": str(promo.get("expires_on") or ""),
                        "promocodes[name]": self.owned_description(
                            local_id, str(promo.get("description") or "")
                        ),
                    },
                )
                self._parse_response(response_text)
                page.goto(self.url, wait_until="domcontentloaded", timeout=60_000)
                page.locator(".js-create-button").wait_for(timeout=30_000)
                created = self._find_row(page, code)
                if not created:
                    raise TildaPromocodeError(
                        "Tilda приняла запрос, но промокод не появился в списке"
                    )
                self._assert_owned(page, created["id"], local_id)
                return created["id"]
            finally:
                context.browser.close()

    def _check_connection_sync(self) -> int:
        with sync_playwright() as playwright:
            context = self._new_context(playwright)
            try:
                page = self._open_authenticated(context)
                return len(self._rows(page))
            finally:
                context.browser.close()

    def _delete_sync(self, promo: Dict[str, Any]) -> None:
        local_id = int(promo["id"])
        code = str(promo["code"]).strip().upper()
        with sync_playwright() as playwright:
            context = self._new_context(playwright)
            try:
                page = self._open_authenticated(context)
                row = self._find_row(page, code)
                if not row:
                    return
                stored_id = str(promo.get("tilda_id") or "").strip()
                if stored_id and row["id"] != stored_id:
                    raise TildaPromocodeError(
                        "Защитная отмена: ID промокода в Tilda не совпадает с CRM"
                    )
                self._assert_owned(page, row["id"], local_id)
                response_text = self._submit(
                    page,
                    {
                        "comm": "promocodelete",
                        "projectid": self.project_id,
                        "promocodeid": row["id"],
                        "lang": "RU",
                    },
                )
                self._parse_response(response_text)
                page.goto(self.url, wait_until="domcontentloaded", timeout=60_000)
                page.locator(".js-create-button").wait_for(timeout=30_000)
                if self._find_row(page, code):
                    raise TildaPromocodeError(
                        "Tilda приняла запрос, но промокод остался в списке"
                    )
            finally:
                context.browser.close()
