"""Brand-specific bindings. No OTHER_SHOP defaults or shared payment secrets."""
import json
from pathlib import Path
from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent
SETTINGS_PATH = BASE / ".secrets/refunds_config.json"
SETTINGS = json.loads(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else {}
ENV = dotenv_values(BASE / ".env")
admins = [int(x.strip()) for x in str(ENV.get("ADMIN_IDS") or "").split(",") if x.strip()]
OWNER = int(SETTINGS.get("owner_id") or (admins[0] if len(admins)==1 else 0))
SUPPORT_CHAT = int(SETTINGS.get("support_chat") or 0)
SUPPORT_TOPIC = int(SETTINGS.get("support_topic") or 0)
SHOP_ID = str(SETTINGS.get("yookassa_shop_id") or "")
MERCHANT = str(SETTINGS.get("yandex_merchant_id") or "")
