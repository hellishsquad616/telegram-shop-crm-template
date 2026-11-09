"""One-time interactive login used by the headless Tilda automation.

The generated browser state contains sensitive cookies.  It is stored only in
``.auth`` (ignored by git) and must be kept with permissions 0600.
"""

from pathlib import Path
import argparse
import json
import os
import shlex
import socket
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

from dotenv import dotenv_values
from playwright.sync_api import sync_playwright
from integrations.tilda_promocodes import TildaPromocodeClient, save_storage_state


PROJECT_DIR = Path(__file__).resolve().parent
AUTH_DIR = PROJECT_DIR / ".auth"
STATE_PATH = AUTH_DIR / "tilda_state.json"
URL_PATH = AUTH_DIR / "tilda_promocodes_url.txt"
HTML_PATH = AUTH_DIR / "tilda_promocodes_page.html"
PROFILE_PATH = AUTH_DIR / "tilda_browser_profile"
DEFAULT_PROMOCODES_URL = (
    "https://tilda.ru/projects/"
)


def is_promocodes_page(page, project_id=None) -> bool:
    """Return True only for an authenticated Tilda page about promo codes."""
    url = page.url.lower()
    parsed = urlsplit(url)
    if parsed.hostname not in {"tilda.cc", "tilda.ru"}:
        return False
    if project_id and parse_qs(parsed.query).get("projectid") != [str(project_id)]:
        return False
    try:
        texts = [page.locator("body").inner_text(timeout=2_000).lower()]
        for frame in page.frames[1:]:
            try:
                texts.append(frame.locator("body").inner_text(timeout=1_000).lower())
            except Exception:
                continue
    except Exception:
        return False
    body = "\n".join(texts)
    if not (
        "/projects/payments/promocodes/" in url
        and ("промокод" in body or "promo code" in body or "promocode" in url)
    ):
        return False
    try:
        return page.locator(".js-create-button").is_visible(timeout=2_000)
    except Exception:
        return False


def server_python(server, remote_dir, source, payload=None):
    """Keep account data in subprocess pipes, never in command arguments."""
    interpreter = str(Path(remote_dir) / ".venv/bin/python")
    if payload is None:
        command = f"{shlex.quote(interpreter)} -"
        stdin = source
    else:
        command = f"{shlex.quote(interpreter)} -c {shlex.quote(source)}"
        stdin = json.dumps(payload)
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", server, command],
            input=stdin, text=True, capture_output=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "SSH-проверка не успела завершиться. Проверьте серверную сессию "
            "командой tilda_login.py --server SSH_АДРЕС --check."
        ) from None
    if result.returncode:
        raise RuntimeError("Не удалось выполнить команду Tilda на сервере через SSH")
    return result.stdout


def server_config(server, remote_dir):
    source = f"""
import json
from pathlib import Path
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright
root = Path({remote_dir!r})
env = dotenv_values(root / '.env')
state = root / '.auth/tilda_state.json'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    user_agent = page.evaluate('navigator.userAgent')
    browser.close()
print(json.dumps({{
    'email': env.get('TILDA_EMAIL', ''),
    'password': env.get('TILDA_PASSWORD', ''),
    'project_id': env.get('TILDA_PROMOCODES_PROJECT_ID', ''),
    'state': json.loads(state.read_text()) if state.exists() else None,
    'user_agent': user_agent,
}}))
"""
    return json.loads(server_python(server, remote_dir, source))


def upload_state(server, remote_dir, state, project_id):
    source = f"""
import json, os, sys, tempfile
from pathlib import Path
root = Path({remote_dir!r})
sys.path.insert(0, str(root))
from integrations.tilda_promocodes import TildaPromocodeClient, TildaPromocodeError
directory = root / '.auth'
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
fd, temporary = tempfile.mkstemp(prefix='.tilda_state_', dir=directory)
try:
    with os.fdopen(fd, 'w') as file:
        json.dump(json.load(sys.stdin), file)
        file.flush()
        os.fsync(file.fileno())
    client = TildaPromocodeClient('', '', {project_id!r}, state_path=Path(temporary))
    count = client._check_connection_sync()
    os.replace(temporary, directory / 'tilda_state.json')
    print(json.dumps({{'count': count}}))
except TildaPromocodeError as error:
    print(json.dumps({{'error': str(error)}}))
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
"""
    result = json.loads(server_python(server, remote_dir, source, state))
    if result.get('error'):
        raise RuntimeError('Серверная проверка Tilda: ' + result['error'])
    return int(result['count'])


def check_server(server, remote_dir, project_id):
    source = f"""
import sys
from pathlib import Path
root = Path({remote_dir!r})
sys.path.insert(0, str(root))
from integrations.tilda_promocodes import TildaPromocodeClient
from integrations.tilda_promocodes import TildaPromocodeError
import json
client = TildaPromocodeClient('', '', {project_id!r}, state_path=root / '.auth/tilda_state.json')
try:
    print(json.dumps({{'count': client._check_connection_sync()}}))
except TildaPromocodeError as error:
    print(json.dumps({{'error': str(error)}}))
"""
    result = json.loads(server_python(server, remote_dir, source))
    if result.get('error'):
        raise RuntimeError('Серверная проверка Tilda: ' + result['error'])
    return int(result['count'])


def start_tunnel(server):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([
        'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
        '-o', 'ExitOnForwardFailure=yes', '-N', '-D', f'127.0.0.1:{port}', server,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return process, f'socks5://127.0.0.1:{port}'
        except OSError:
            time.sleep(0.2)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError('Не удалось открыть SSH-туннель для входа с IP сервера')


def main() -> None:
    parser = argparse.ArgumentParser(description="Восстановление сессии Tilda")
    parser.add_argument('--server', help='SSH-адрес сервера, например root@IP')
    parser.add_argument('--remote-dir', default='/srv/shop-crm-bot')
    parser.add_argument('--check', action='store_true', help='Проверить сессию без входа')
    args = parser.parse_args()
    if args.server and (args.server.startswith('-') or any(c.isspace() for c in args.server)):
        parser.error('Укажите SSH-адрес в формате user@host')
    if not args.remote_dir.startswith('/'):
        parser.error('--remote-dir должен быть абсолютным путём')
    env = dotenv_values(PROJECT_DIR / '.env')
    config = server_config(args.server, args.remote_dir) if args.server else {
        'email': env.get('TILDA_EMAIL', ''),
        'password': env.get('TILDA_PASSWORD', ''),
        'project_id': env.get('TILDA_PROMOCODES_PROJECT_ID', ''),
    }
    project_id = str(config['project_id'])
    if not project_id.isdigit():
        parser.error('TILDA_PROMOCODES_PROJECT_ID должен быть числом')
    if args.check:
        count = check_server(args.server, args.remote_dir, project_id) if args.server else (
            TildaPromocodeClient('', '', project_id, state_path=STATE_PATH)._check_connection_sync()
        )
        print(f'Сессия работает. Промокодов в Tilda: {count}.', flush=True)
        return
    AUTH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tunnel = None
    try:
        proxy = None
        if args.server:
            tunnel, proxy_url = start_tunnel(args.server)
            proxy = {'server': proxy_url}
        with sync_playwright() as playwright:
            browser = None
            if args.server:
                browser = playwright.chromium.launch(headless=False, proxy=proxy)
                context = browser.new_context(
                    storage_state=config.get('state'), user_agent=config['user_agent'],
                )
            else:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_PATH, headless=False,
                )
            try:
                page = context.new_page()
                target_url = f'https://tilda.ru/projects/payments/promocodes/?projectid={project_id}'
                page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
                if TildaPromocodeClient._is_login_page(page):
                    if config.get('email'):
                        page.locator('#email').fill(config['email'])
                    if config.get('password'):
                        page.locator('#password').fill(config['password'])
                print('В окне Tilda нажмите «Войти» и пройдите проверку «Я не робот», если она появится. Сессия сохранится автоматически.', flush=True)
                deadline = time.monotonic() + 15 * 60
                target_requested = False
                while time.monotonic() < deadline and not is_promocodes_page(page, project_id):
                    if not TildaPromocodeClient._is_login_page(page) and not target_requested:
                        target_requested = True
                        page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(1_000)
                if not is_promocodes_page(page, project_id):
                    raise RuntimeError("За 15 минут раздел промокодов Tilda не был открыт")
                if args.server:
                    count = upload_state(args.server, args.remote_dir, context.storage_state(), project_id)
                    print(f'Сессия сохранена на сервере и проверена. Промокодов: {count}.', flush=True)
                else:
                    save_storage_state(context, STATE_PATH)
                    URL_PATH.write_text(page.url.strip(), encoding="utf-8")
                    os.chmod(URL_PATH, 0o600)
                    print("Сессия и адрес раздела промокодов сохранены.", flush=True)
            finally:
                context.close()
                if browser:
                    browser.close()
    finally:
        if tunnel:
            tunnel.terminate()
            tunnel.wait(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
