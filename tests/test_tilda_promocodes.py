import unittest
import json
import stat
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from integrations.tilda_promocodes import (
    TildaPromocodeClient, TildaPromocodeError, save_storage_state,
)
from tilda_login import is_promocodes_page, server_python, upload_state


class TildaPromocodeClientTest(unittest.TestCase):
    def test_formats_tilda_payload_values(self) -> None:
        percent = {"discount_type": "percent", "discount_value": "10.5"}
        fixed = {"discount_type": "fixed", "discount_value": "1500"}
        self.assertEqual(TildaPromocodeClient.discount_for_tilda(percent), "10.5%")
        self.assertEqual(TildaPromocodeClient.discount_for_tilda(fixed), "1500")

    def test_ownership_marker_is_stable_and_description_is_bounded(self) -> None:
        marker = TildaPromocodeClient.ownership_marker(42)
        description = TildaPromocodeClient.owned_description(42, "Акция " * 100)
        self.assertEqual(marker, "[SHOPCRM-CRM:42]")
        self.assertTrue(description.startswith(marker))
        self.assertLessEqual(len(description), 255)

    def test_client_requires_all_credentials(self) -> None:
        self.assertFalse(TildaPromocodeClient("", "", "").configured)
        self.assertTrue(TildaPromocodeClient("mail@example.com", "secret", "1").configured)

    def test_saved_session_is_sufficient_without_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{}')
            self.assertTrue(TildaPromocodeClient('', '', '1', state_path=path).configured)

    def test_captcha_error_does_not_expose_account_text(self) -> None:
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = (
            'Please check the box to let us know you are human account@example.com'
        )
        message = TildaPromocodeClient._login_failure(page)
        self.assertIn('Я не робот', message)
        self.assertIn('--server', message)
        self.assertNotIn('account@example.com', message)

    def test_login_timeout_is_reported_as_captcha(self) -> None:
        client = TildaPromocodeClient('mail@example.com', 'secret', '1')
        context = MagicMock()
        page = context.new_page.return_value
        page.url = 'https://tilda.ru/login/'
        page.wait_for_function.side_effect = PlaywrightTimeout('timeout')
        page.locator.return_value.inner_text.return_value = 'Подтвердите, что вы не робот'
        with self.assertRaisesRegex(TildaPromocodeError, 'Я не робот'):
            client._open_authenticated(context)
        context.storage_state.assert_not_called()

    def test_login_detection_supports_both_trailing_slash_variants(self) -> None:
        page = MagicMock()
        for url in ('https://tilda.ru/login', 'https://tilda.ru/login/?x=1'):
            page.url = url
            self.assertTrue(TildaPromocodeClient._is_login_page(page))
        page.url = 'https://tilda.ru/projects/?return=/login/'
        self.assertFalse(TildaPromocodeClient._is_login_page(page))

    def test_session_is_written_privately_and_failure_preserves_old_state(self) -> None:
        context = MagicMock()
        context.storage_state.return_value = {'cookies': [], 'origins': []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.auth/state.json'
            save_storage_state(context, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), context.storage_state.return_value)
            previous = path.read_bytes()
            context.storage_state.side_effect = RuntimeError('browser closed')
            with self.assertRaises(RuntimeError):
                save_storage_state(context, path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_interactive_login_requires_correct_project_and_loaded_controls(self) -> None:
        page = MagicMock()
        page.url = 'https://tilda.ru/projects/payments/promocodes/?projectid=1'
        page.frames = []
        page.locator.return_value.inner_text.return_value = 'Промокоды'
        page.locator.return_value.is_visible.return_value = True
        self.assertTrue(is_promocodes_page(page, '1'))
        self.assertFalse(is_promocodes_page(page, '2'))
        page.locator.return_value.is_visible.return_value = False
        self.assertFalse(is_promocodes_page(page, '1'))
        page.url = 'https://tilda.ru.attacker.test/projects/payments/promocodes/?projectid=1'
        page.locator.return_value.is_visible.return_value = True
        self.assertFalse(is_promocodes_page(page, '1'))

    def test_remote_session_payload_is_only_sent_via_stdin(self) -> None:
        with patch('tilda_login.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ''
            server_python('root@example.com', '/opt/crm with spaces', 'import json', {'cookie': 'secret'})
            self.assertNotIn('secret', str(run.call_args.args))
            self.assertIn('secret', run.call_args.kwargs['input'])
            self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_remote_session_rejection_is_reported(self) -> None:
        with patch('tilda_login.server_python', return_value='{"error": "Сессия истекла"}'):
            with self.assertRaisesRegex(RuntimeError, 'Серверная проверка Tilda: Сессия истекла'):
                upload_state('root@example.com', '/opt/crm', {'cookies': []}, '1')

    def test_remote_verified_session_returns_count(self) -> None:
        with patch('tilda_login.server_python', return_value='{"count": 23}'):
            self.assertEqual(upload_state('root@example.com', '/opt/crm', {'cookies': []}, '1'), 23)

    def test_ssh_timeout_reports_recheck_without_echoing_command(self) -> None:
        with patch('tilda_login.subprocess.run', side_effect=subprocess.TimeoutExpired('private command', 180)):
            with self.assertRaises(RuntimeError) as error:
                server_python('root@example.com', '/opt/crm', 'private command')
            self.assertIn('--check', str(error.exception))
            self.assertNotIn('private command', str(error.exception))


if __name__ == "__main__":
    unittest.main()
