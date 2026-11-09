"""Interactive brand-specific setup. All verification requests are GET only."""
import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
import httpx

YOO_API = "https://api.yookassa.ru/v3"
SPLIT_API = "https://pay.yandex.ru/api/merchant"
BASE = Path(__file__).resolve().parent

async def verify_yoo(shop_id, key, payment_id, client):
    auth = httpx.BasicAuth(shop_id, key)
    response = await client.get(YOO_API + "/me", auth=auth)
    if response.status_code != 200:
        raise ValueError(f"Проверка ЮKassa: HTTP {response.status_code}. Ключ не сохранён.")
    account = response.json()
    if str(account.get("account_id")) != shop_id or account.get("test") is not False or account.get("status") != "enabled":
        raise ValueError("Нужен действующий боевой магазин с указанным shopId. Ключ не сохранён.")
    fiscal = account.get("fiscalization") or {}
    if not fiscal.get("enabled") or not fiscal.get("provider"):
        raise ValueError("Через ЮKassa не подтверждено подключение онлайн-кассы. Ключ не сохранён; нужна проверка схемы чеков.")
    response = await client.get(YOO_API + "/payments/" + payment_id, auth=auth)
    if response.status_code != 200:
        raise ValueError(f"Пример платежа недоступен: HTTP {response.status_code}. Ключ не сохранён.")
    payment = response.json()
    if payment.get("status") != "succeeded" or not payment.get("paid") or payment.get("test") is not False:
        raise ValueError("Пример должен быть успешно оплаченным боевым платежом. Ключ не сохранён.")
    if str((payment.get("recipient") or {}).get("account_id")) != shop_id:
        raise ValueError("Платёж относится к другому магазину. Ключ не сохранён.")
    response = await client.get(YOO_API + "/receipts", params={"payment_id": payment_id, "limit": 100}, auth=auth)
    if response.status_code != 200:
        raise ValueError(f"Проверка чеков: HTTP {response.status_code}. Ключ не сохранён.")
    receipts = response.json().get("items", [])
    purchases = [r for r in receipts if r.get("type") == "payment" and r.get("status") == "succeeded"]
    if not purchases:
        raise ValueError("Не найден зарегистрированный чек примера платежа. Ключ не сохранён.")
    return {"shop_id": shop_id, "secret_key": key, "provider": fiscal["provider"], "test": False,
            "verified_at": datetime.now(timezone.utc).isoformat(), "sample_payment_id": payment_id,
            "sample_amount": payment.get("amount"), "registered_receipts": len(purchases)}

def amount_cents(value):
    try:
        amount = Decimal(re.sub(r"\s+", "", str(value)).replace(",", "."))
        if not amount.is_finite() or amount <= 0 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError
        return int(amount * 100)
    except (ValueError, InvalidOperation):
        raise ValueError("Сумма должна быть положительной и точной до копейки.") from None

def identifiers(merchant_id, order_id):
    try:
        merchant_id = str(uuid.UUID(merchant_id.strip()))
    except (ValueError, AttributeError):
        raise ValueError("Merchant ID должен быть UUID из настроек Яндекс Пэй.") from None
    order_id = order_id.strip()
    if not order_id or len(order_id) > 2048 or order_id in {".", ".."} or any(ord(c) < 32 for c in order_id):
        raise ValueError("Укажите полный ID заказа из Яндекс Пэй.")
    return merchant_id, order_id

async def verify_split(merchant_id, key, order_id, client, expected_amount=None, *, require_split=True):
    merchant_id, order_id = identifiers(merchant_id, order_id)
    if not 16 <= len(key) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("Ключ Merchant API пустой или содержит недопустимые символы.")
    response = await client.get(
        SPLIT_API + "/v1/orders/" + quote(order_id, safe=""),
        headers={"Authorization": "Api-Key " + key, "X-Request-Id": str(uuid.uuid4()), "X-Request-Timeout": "10000"},
        follow_redirects=False,
    )
    if response.status_code != 200:
        raise ValueError(f"Яндекс Пэй: HTTP {response.status_code}. Ключ не сохранён; проверьте ключ и полный ID заказа.")
    try:
        result = response.json()
        order = result["data"]["order"]
        if result.get("status") != "success" or result.get("code") != 200 or not isinstance(order, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError("Яндекс Пэй вернул неподтверждённый ответ; ключ не сохранён.") from None
    if str(order.get("merchantId", "")).casefold() != merchant_id or order.get("orderId") != order_id:
        raise ValueError("Ответ относится к другому магазину или заказу. Ключ не сохранён.")
    if order.get("currencyCode") != "RUB":
        raise ValueError("Для подключения нужен пример заказа в рублях.")
    if order.get("paymentStatus") not in {"CAPTURED", "PARTIALLY_REFUNDED"}:
        raise ValueError("Нужен оплаченный заказ в статусе CAPTURED или PARTIALLY_REFUNDED. Ключ не сохранён.")
    method_data = order.get("paymentMethod")
    method = method_data.get("methodType") if isinstance(method_data, dict) else None
    known_methods = {"CARD","SPLIT","SBP","SPLIT_SBP","CASH_ON_DELIVERY","CARD_ON_DELIVERY","UNIQR_REUSABLE","UNIQR_ONETIME","DIGITAL_RUBLE"}
    if require_split and (not isinstance(method,str) or method not in {"SPLIT", "SPLIT_SBP"}):
        if isinstance(method,str) and method in known_methods:
            raise ValueError("Яндекс Пэй вернул способ оплаты " + method + "; Сплит не подтверждён. Проведение выключено.")
        raise ValueError("API не передал способ оплаты Сплит в ожидаемом формате. Нужна диагностика ответа; проведение выключено.")
    total = amount_cents(order.get("orderAmount"))
    if expected_amount is not None and total != amount_cents(expected_amount):
        raise ValueError("Сумма заказа не совпала. У частично возвращённого заказа orderAmount показывает остаток после возвратов.")
    cart = order.get("cart")
    if not isinstance(cart, dict) or not isinstance(cart.get("items"), list) or not cart["items"] or any(not isinstance(i, dict) for i in cart["items"]):
        raise ValueError("В ответе нет состава заказа. Нужна проверка интеграции магазина.")
    return {"merchant_id": merchant_id, "api_key": key, "environment": "production",
            "verified_at": datetime.now(timezone.utc).isoformat(), "sample_order_id": order_id,
            "sample_amount_cents": total, "sample_payment_status": order["paymentStatus"],
            "sample_payment_method": method if isinstance(method,str) and method in known_methods else None,
            "split_verified": method in {"SPLIT","SPLIT_SBP"} if isinstance(method,str) else False,
            "payment_method_container_type": type(method_data).__name__,
            "payment_method_fields": sorted(method_data) if isinstance(method_data,dict) else [],
            "sample_items": len(cart["items"]),
            "has_fiscal_item_data": any(bool(i.get("receipt")) for i in cart["items"]),
            "fiscalization_verified": False}

def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data,f,ensure_ascii=False); f.flush(); os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def gate_fingerprint(base, provider):
    names = ("refunds_config.json", "moysklad.json", "yookassa.json" if provider=="yookassa" else "yandex_pay.json")
    return {name: hashlib.sha256((Path(base)/".secrets"/name).read_bytes()).hexdigest() for name in names}


def require_gate(base, provider):
    from customer_refunds import RefundError
    try:
        marker=json.loads((Path(base)/".secrets"/(provider+"_refunds_enabled")).read_text())
        if marker.get("fingerprint") != gate_fingerprint(base,provider): raise ValueError
    except (OSError,ValueError,TypeError):
        raise RefundError("Проведение ещё не включено либо настройки изменились. Нужна проверка подключений без денежных операций.") from None


async def verify_ms(service):
    from customer_refunds import SETTINGS, norm, RefundError
    sample=SETTINGS.get("ms_sample_document_id")
    if not sample: raise RefundError("Укажите пример документа МойСклад SHOPCRM.")
    order,_=await service.document(sample)
    service.check_brand(order)
    metadata=await service.ms("GET","/entity/customerorder/metadata")
    attributes=await service.ms("GET","/entity/customerorder/metadata/attributes",params={"limit":1000})
    if len([x for x in metadata.get("states",[]) if norm(x.get("name"))=="возврат проведен"]) != 1:
        raise RefundError("Не найден однозначный статус «Возврат-проведен».")
    if len([x for x in attributes.get("rows",[]) if norm(x.get("name"))=="дата проведения возврата средств" and x.get("type")=="time"]) != 1:
        raise RefundError("Не найдено поле даты проведения возврата средств типа «Дата и время».")
    return order


async def discover_ms(service, number):
    """Find one original order, fail closed on ambiguity, use only GET."""
    from customer_refunds import SETTINGS, RefundError
    if not re.fullmatch(r"\d{5,20}", number): raise RefundError("Некорректный номер заказа Tilda.")
    data = await service.ms("GET", "/entity/customerorder", params={"search": number,"limit":100})
    rows = data.get("rows", [])
    if int(data.get("meta", {}).get("size",len(rows))) > len(rows):
        raise RefundError("Слишком много документов. Укажите UUID организации и исходного документа явно.")
    prefix = SETTINGS.get("order_name_prefix", "")
    matches = [o for o in rows if o.get("name") in {number,prefix+number}]
    if len(matches) != 1:
        raise RefundError("Не найден один исходный заказ МойСклад. Укажите UUID организации и документа явно.")
    doc = matches[0]
    organization = doc.get("organization") or {}
    organization_id = organization.get("id") or (organization.get("meta") or {}).get("href", "").rstrip("/").split("/")[-1]
    try:
        uuid.UUID(organization_id); uuid.UUID(doc["id"])
    except (ValueError,TypeError,KeyError):
        raise RefundError("Организация или ID документа не подтверждены.") from None
    return organization_id, doc["id"]


async def save_ms_connection(service, base, token, number=None):
    """Save a verified read connection without selecting a refund document."""
    from customer_refunds import RefundError
    if not token or token != token.strip(): raise RefundError("Введите непустой токен МойСклад.")
    if number and not re.fullmatch(r"\d{5,20}",number): raise RefundError("Некорректный номер заказа Tilda.")
    data = await service.ms("GET", "/entity/organization", params={"limit":100})
    organizations = data.get("rows")
    if not isinstance(organizations,list) or not organizations:
        raise RefundError("Доступ к организациям МойСклад не подтверждён. Токен не сохранён.")
    try:
        for organization in organizations: uuid.UUID(organization["id"])
    except (ValueError,TypeError,KeyError):
        raise RefundError("Не подтверждены идентификаторы организаций МойСклад. Токен не сохранён.") from None
    # Lookup diagnostics may fail separately; access to the account is already proven.
    matches = None
    try:
        if number:
            found = await service.ms("GET", "/entity/customerorder", params={"search":number,"limit":100})
            matches = int(found.get("meta",{}).get("size",len(found.get("rows",[]))))
    except RefundError:
        pass
    private_json(Path(base)/".secrets/moysklad.json",{"token":token,"connected_at":datetime.now(timezone.utc).isoformat()})
    return {"organizations":len(organizations),"search_matches":matches,"document_selected":False}


async def save_yoo_connection(base, shop_id, key, payment_id, client, expected_amount=None):
    """Save authenticated shop access; leave active provider bindings unchanged."""
    if not re.fullmatch(r"\d{3,20}",shop_id) or not re.fullmatch(r"[a-f0-9-]{36}",payment_id):
        raise ValueError("Некорректный shopId или ID платежа.")
    if not key: raise ValueError("Ключ пустой; ничего не сохранено.")
    response=await client.get(YOO_API+"/me",auth=httpx.BasicAuth(shop_id,key))
    if response.status_code!=200: raise ValueError("Доступ ЮKassa не подтверждён: HTTP "+str(response.status_code)+". Ключ не сохранён.")
    account=response.json()
    if str(account.get("account_id"))!=shop_id or account.get("test") is not False or account.get("status")!="enabled":
        raise ValueError("Не подтверждён действующий боевой магазин с указанным shopId. Ключ не сохранён.")
    path=Path(base)/".secrets/yookassa.json"
    private_json(path,{"shop_id":shop_id,"secret_key":key,"test":False,"sample_payment_id":payment_id,"connection_verified_at":datetime.now(timezone.utc).isoformat()})
    try:
        data=await verify_yoo(shop_id,key,payment_id,client)
        if expected_amount is not None and ((data.get("sample_amount") or {}).get("currency")!="RUB" or amount_cents((data.get("sample_amount") or {}).get("value"))!=amount_cents(expected_amount)):
            raise ValueError("Сумма примера платежа не совпала с ожидаемой. Нужна сверка.")
        private_json(path,data)
        return {"connection_saved":True,"ready_for_activation":True}
    except ValueError as error:
        return {"connection_saved":True,"ready_for_activation":False,"diagnostic":str(error).replace("Ключ не сохранён.","").strip()}
    except httpx.HTTPError:
        return {"connection_saved":True,"ready_for_activation":False,"diagnostic":"Проверка платежа или чека временно недоступна; доступ к магазину сохранён."}


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="action",required=True)
    yoo=sub.add_parser("yookassa");yoo.add_argument("--shop-id",required=True);yoo.add_argument("--payment-id",required=True)
    yoo_connect=sub.add_parser("yookassa-connect")
    yoo_connect.add_argument("--shop-id",required=True);yoo_connect.add_argument("--payment-id",required=True);yoo_connect.add_argument("--amount",required=True)
    split=sub.add_parser("split");split.add_argument("--merchant-id",required=True);split.add_argument("--order-id",required=True);split.add_argument("--amount",required=True)
    split.add_argument("--fiscalization-verified",action="store_true",help="Подключение кассы проверено в кабинете данного магазина")
    split_connect=sub.add_parser("split-connect")
    split_connect.add_argument("--merchant-id",required=True);split_connect.add_argument("--order-id",required=True);split_connect.add_argument("--amount",required=True)
    ms=sub.add_parser("moysklad");ms.add_argument("--organization-id");ms.add_argument("--document-id");ms.add_argument("--order-number")
    ms.add_argument("--exclusive-organization",action="store_true")
    ms.add_argument("--scope-field",choices=["project","salesChannel","attribute"]);ms.add_argument("--scope-id");ms.add_argument("--scope-value")
    connect=sub.add_parser("moysklad-connect");connect.add_argument("--order-number")
    enable=sub.add_parser("enable");enable.add_argument("provider",choices=["yookassa","yandex_pay"])
    args=parser.parse_args()
    directory=BASE/".secrets";config_path=directory/"refunds_config.json"
    config=json.loads(config_path.read_text()) if config_path.exists() else {}
    if args.action != "enable" and not sys.stdin.isatty(): raise ValueError("Ключ вводится только в интерактивном SSH-терминале.")
    async with httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False) as client:
        if args.action=="yookassa":
            if not re.fullmatch(r"\d{3,20}",args.shop_id) or not re.fullmatch(r"[a-f0-9-]{36}",args.payment_id): raise ValueError("Некорректный shopId или ID платежа.")
            key=getpass.getpass("Секретный ключ ЮKassa (скрытый ввод): ").strip()
            data=await verify_yoo(args.shop_id,key,args.payment_id,client)
            config["yookassa_shop_id"]=args.shop_id
            private_json(directory/"yookassa.json",data)
        elif args.action=="yookassa-connect":
            key=getpass.getpass("Секретный ключ ЮKassa (скрытый ввод): ").strip()
            result=await save_yoo_connection(BASE,args.shop_id,key,args.payment_id,client,args.amount)
            print("Доступ к боевому магазину ЮKassa проверен. Ключ сохранён с правами 0600.")
            print("Платёж, сумма и зарегистрированный чек проверены." if result["ready_for_activation"] else result["diagnostic"])
            print("Проведение ЮKassa пока выключено. Настройки и проведение Сплита не изменены.")
            return
        elif args.action=="split":
            key=getpass.getpass("Ключ Merchant API Сплита (скрытый ввод): ").strip()
            data=await verify_split(args.merchant_id,key,args.order_id,client,args.amount)
            data["fiscalization_verified"]=args.fiscalization_verified
            config["yandex_merchant_id"]=data["merchant_id"]
            private_json(directory/"yandex_pay.json",data)
        elif args.action=="split-connect":
            key=getpass.getpass("Ключ Merchant API Сплита (скрытый ввод): ").strip()
            data=await verify_split(args.merchant_id,key,args.order_id,client,args.amount,require_split=False)
            config["yandex_merchant_id"]=data["merchant_id"]
            private_json(directory/"yandex_pay.json",data)
            private_json(config_path,config)
            print("Доступ к магазину Яндекс Пэй проверен. Ключ сохранён с правами 0600.")
            print("Способ оплаты по API: " + str(data["sample_payment_method"] or "не определён; требуется диагностика") + ".")
            print("Магазин, заказ и сумма совпали. Проведение денег выключено до полной проверки Сплита и кассы.")
            return
        elif args.action=="moysklad-connect":
            from customer_refunds import RefundService
            key=getpass.getpass("Токен МойСклад SHOPCRM (скрытый ввод): ").strip()
            if not key: raise ValueError("Токен пустой; ничего не сохранено.")
            service=RefundService(BASE/"customer_refunds.sqlite3",BASE,key)
            result=await save_ms_connection(service,BASE,key,args.order_number)
            print("Доступ к МойСклад проверен. Токен сохранён с правами 0600.")
            print("Организаций: " + str(result["organizations"]) + ". Документов по номеру: " + (str(result["search_matches"]) if result["search_matches"] is not None else "поиск требует проверки") + ".")
            print("Документ возврата не выбран. Проведение денег выключено до полной проверки настроек.")
            return
        elif args.action=="moysklad":
            if not args.exclusive_organization and not (args.scope_field and args.scope_id): raise ValueError("Для общей организации обязательно ограничение бренда.")
            if args.scope_field=="attribute" and not args.scope_value: raise ValueError("Укажите значение атрибута бренда.")
            from customer_refunds import RefundService, SETTINGS
            key=getpass.getpass("Токен МойСклад SHOPCRM (скрытый ввод): ").strip()
            service=RefundService(BASE/"customer_refunds.sqlite3",BASE,key)
            if args.order_number:
                if args.organization_id or args.document_id: raise ValueError("Укажите либо номер заказа, либо UUID документа и организации.")
                args.organization_id,args.document_id=await discover_ms(service,args.order_number)
            if not args.organization_id or not args.document_id: raise ValueError("Укажите номер заказа либо UUID документа и организации.")
            for value in (args.organization_id,args.document_id): uuid.UUID(value)
            config.update(ms_organization_id=args.organization_id,ms_organization_exclusive=args.exclusive_organization,ms_sample_document_id=args.document_id,
                          ms_brand_scope={"field":args.scope_field,"id":args.scope_id,"value":args.scope_value})
            old=dict(SETTINGS);SETTINGS.clear();SETTINGS.update(config)
            try:
                service=RefundService(BASE/"customer_refunds.sqlite3",BASE,key)
                await verify_ms(service)
            finally: SETTINGS.clear();SETTINGS.update(old)
            private_json(directory/"moysklad.json",{"token":key})
        else:
            from customer_refunds import RefundService, SETTINGS
            from refund_settings import OWNER, SUPPORT_CHAT, SUPPORT_TOPIC
            if not OWNER or not SUPPORT_CHAT or not SUPPORT_TOPIC: raise ValueError("Не настроены владелец и тема поддержки.")
            service=RefundService(BASE/"customer_refunds.sqlite3",BASE,"")
            await verify_ms(service)
            if args.provider=="yookassa":
                data=service.yoo.credentials()
                await verify_yoo(data["shop_id"],data["secret_key"],data["sample_payment_id"],client)
            else:
                data=service.split.api.credentials()
                if data.get("fiscalization_verified") is not True: raise ValueError("Сначала проверьте кассу Сплита в кабинете SHOPCRM.")
                await verify_split(data["merchant_id"],data["api_key"],data["sample_order_id"],client)
            # Check Telegram permissions without sending any messages.
            from dotenv import dotenv_values
            token=dotenv_values(BASE/".env").get("BOT_TOKEN")
            response=await client.get("https://api.telegram.org/bot"+str(token)+"/getMe")
            identity=response.json()
            if not identity.get("ok"): raise ValueError("Бот недоступен.")
            response=await client.get("https://api.telegram.org/bot"+str(token)+"/getChatMember",params={"chat_id":SUPPORT_CHAT,"user_id":identity["result"]["id"]})
            member=response.json()
            if not member.get("ok") or member["result"].get("status") not in {"administrator","creator"}: raise ValueError("Бот должен быть администратором группы поддержки.")
            private_json(directory/(args.provider+"_refunds_enabled"),{"fingerprint":gate_fingerprint(BASE,args.provider),"verified_at":datetime.now(timezone.utc).isoformat()})
            print("Подключения проверены только чтением. Проведение включено; каждый возврат требует личного подтверждения владельца.")
            return
    private_json(config_path,config)
    print("Подключение проверено только чтением. Настройки сохранены с правами 0600. Денежных операций не было. Проведение пока выключено.")
    print("После завершения настройки выполните enable и перезапустите shopcrm-crm-bot.service.")


if __name__=="__main__":
    try: asyncio.run(main())
    except (ValueError,RuntimeError) as error:
        print(str(error));raise SystemExit(1)
    except Exception as error:
        print("Проверка не завершена: "+type(error).__name__+". Секреты и ответы сервисов не выводятся.");raise SystemExit(1)
