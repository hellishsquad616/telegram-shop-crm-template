"""Gift scarves in MoySklad only: zero price, stock reservation, durable retries."""
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, MessageHandler, filters

from customer_refunds import MS_API, RefundError, now
from support_order_info import OrderInformation, UUID, normalized

LOG = logging.getLogger(__name__)
CHAT_KEY = "gift_scarves_chat_id"
TOPIC_KEY = "gift_scarves_topic_id"
COLORS = ("BLACK", "GREY", "RED", "PINK", "BROWN")
COLOR_NAMES = {"BLACK":"чёрный", "GREY":"серый", "RED":"красный", "PINK":"розовый", "BROWN":"коричневый"}
COLOR_PATTERNS = {
    "BLACK": r"\b(?:black|черн\w*)\b",
    "GREY": r"\b(?:gr[ae]y|серый|серого|серому|серым|серая|сер\b)\b",
    "RED": r"\b(?:red|красн\w*)\b",
    "PINK": r"\b(?:pink|розов\w*)\b",
    "BROWN": r"\b(?:brown|коричнев\w*)\b",
}


class GiftClarification(RefundError):
    pass


class GiftConfirmation(GiftClarification):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_gift(text, reply_text=""):
    text = str(text or "").replace("ё", "е")
    markers = list(re.finditer(r"\b(?:шарф\w*|scarf)\b", text, re.I))
    if not markers:
        return None
    slot = 2 if re.search(r"\bвтор(?:ой|ого)\s+шарф", text, re.I) else 1
    def numbers(value):
        result = re.findall(r"(?:\border\s*#?|\bзаказ\s*[№#]?|#|№)\s*(\d{5,20})(?!\d)", value, re.I)
        first = next((line.strip(" *_") for line in value.splitlines() if line.strip()), "")
        if re.fullmatch(r"\d{5,20}", first): result.append(first)
        return set(result)
    found = numbers(text) or numbers(str(reply_text or ""))
    if len(found) != 1:
        raise GiftClarification("Укажите один номер заказа: Order #1010101002, затем шарф и цвет. Для второго подарка добавьте «Второй шарф в подарок».")
    tail = text[markers[-1].end():].casefold().strip(" \n\t(),:-*_")
    colors = [color for color, pattern in COLOR_PATTERNS.items()
              if re.search(pattern, tail) or re.search("(?:"+pattern+r")\s+(?:шарф\w*|scarf)\b", text, re.I)]
    prefix = text[text.rfind("\n",0,markers[-1].start())+1:markers[-1].start()].casefold().strip(" *_:-")
    if not colors and re.search(r"\b(?!(?:второй|второго|любой|подарочный)\b)\w+(?:ый|ий|ой|ая|ого)\s*$", prefix):
        raise GiftClarification("Не распознан цвет шарфа. Укажите BLACK, GREY, RED, PINK, BROWN или «любой цвет».")
    if len(colors) > 1 or re.search(r"\b(?:не|кроме)\b", prefix+" "+tail):
        raise GiftClarification("Укажите один цвет шарфа: BLACK, GREY, RED, PINK или BROWN. Либо напишите «Шарф, любой цвет».")
    if re.search(r"(?:\b[2-9]\s*(?:шт|шарф)|[xх×]\s*[2-9]|\bдва\s+шарф)", text, re.I):
        raise GiftClarification("Одна заявка добавляет один шарф. Для второго напишите «Второй шарф в подарок» и номер заказа.")
    if not colors and tail and not re.fullmatch(r"(?:любой(?:\s+цвет)?|цвет\s+любой|без\s+разницы|рандом(?:ный)?|any(?:\s+colou?r)?)\s*[.!]?", tail):
        raise GiftClarification("Не распознан цвет шарфа. Доступные цвета: BLACK, GREY, RED, PINK, BROWN. Для выбора по наличию напишите «любой цвет».")
    return {"order_number": found.pop(), "color": colors[0] if colors else None, "slot": slot}


def decimal(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite(): raise InvalidOperation
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise GiftClarification("МойСклад вернул некорректное количество или цену. Проверьте документ.") from None


def scarf_color(name):
    match = re.fullmatch(r"DEMO SCARF (BLACK|GREY|RED|PINK|BROWN)(?:\s*\([^\n]*\))?", normalized(name))
    return match[1] if match else None


def first_item_color(positions):
    """Use the first physical item only; services and scarves are not garments."""
    for position in positions:
        item = position.get("assortment") or {}
        name = normalized(item.get("name"))
        if decimal(position.get("quantity", 0)) <= 0: continue
        if (item.get("meta") or {}).get("type") == "service" or re.search(r"\b(?:SCARF|ШАРФ|CDEK|СДЭК|ДОСТАВКА|DELIVERY)\b", name): continue
        colors = [color for color, pattern in COLOR_PATTERNS.items() if re.search(pattern, name, re.I)]
        return colors[0] if len(colors) == 1 else None
    return None


def entity_id(entity):
    return str(entity.get("id") or (entity.get("meta") or {}).get("href", "").rstrip("/").split("/")[-1])


def position_identity(position):
    return {"id": position.get("id"), "assortment": entity_id(position.get("assortment") or {}),
            "quantity": format(decimal(position.get("quantity", 0)).normalize(),"f"),
            "price": format(decimal(position.get("price", 0)).normalize(),"f"),
            "discount": format(decimal(position.get("discount", 0)).normalize(),"f")}


def gift_positions(positions):
    result = []
    for position in positions:
        if not scarf_color((position.get("assortment") or {}).get("name")): continue
        quantity = decimal(position.get("quantity", 0))
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise GiftClarification("Проверьте количество шарфов в заказе МойСклад.")
        if decimal(position.get("price", 0)) == 0 or decimal(position.get("discount", 0)) == 100:
            result.append(position)
    return result


class GiftStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
        os.chmod(self.path, 0o600)
        with closing(self.connect()) as c, c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS gift_requests (
                    id INTEGER PRIMARY KEY, source_chat INTEGER NOT NULL, source_message INTEGER NOT NULL,
                    source_topic INTEGER NOT NULL, actor INTEGER, source_hash TEXT NOT NULL, request TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'new', order_id TEXT, slot INTEGER, plan TEXT, attempted_at TEXT,
                    result TEXT, reply_id INTEGER, notify_pending INTEGER NOT NULL DEFAULT 1,
                    edited_after_attempt INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(source_chat,source_message));
                CREATE UNIQUE INDEX IF NOT EXISTS gift_order_slot ON gift_requests(order_id,slot) WHERE attempted_at IS NOT NULL;
                CREATE TABLE IF NOT EXISTS gift_rotation (store_id TEXT PRIMARY KEY, color TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS gift_confirmations (
                    request_id INTEGER PRIMARY KEY REFERENCES gift_requests(id), source_hash TEXT NOT NULL,
                    snapshot TEXT NOT NULL, token TEXT NOT NULL, decision TEXT, actor INTEGER, decided_at TEXT);
            """)
            columns = {r[1] for r in c.execute("PRAGMA table_info(gift_requests)")}
            if "check_count" not in columns:
                c.execute("ALTER TABLE gift_requests ADD COLUMN check_count INTEGER NOT NULL DEFAULT 0")

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30); c.row_factory = sqlite3.Row; return c

    def decode(self, row):
        if row is None: return None
        record = dict(row)
        for key in ("request", "plan"):
            record[key] = json.loads(record[key]) if record[key] else None
        return record

    def get(self, number):
        with closing(self.connect()) as c:
            record = self.decode(c.execute("SELECT * FROM gift_requests WHERE id=?", (number,)).fetchone())
            if record:
                row = c.execute("SELECT * FROM gift_confirmations WHERE request_id=?", (number,)).fetchone()
                record["confirmation"] = dict(row) if row else None
            return record

    def ingest(self, message, request, text):
        digest = hashlib.sha256(text.encode()).hexdigest()
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM gift_requests WHERE source_chat=? AND source_message=?", (message.chat.id, message.message_id)).fetchone()
            if not row:
                cur = c.execute("INSERT INTO gift_requests(source_chat,source_message,source_topic,actor,source_hash,request,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                (message.chat.id,message.message_id,message.message_thread_id,getattr(message.from_user,"id",None),digest,encode(request),now(),now()))
                number = cur.lastrowid
            else:
                number = row["id"]
                if row["source_hash"] != digest:
                    if row["attempted_at"] or row["state"] == "done":
                        c.execute("UPDATE gift_requests SET edited_after_attempt=1,notify_pending=1,updated_at=? WHERE id=?", (now(),number))
                    else:
                        c.execute("UPDATE gift_requests SET source_hash=?,request=?,state='new',result=NULL,notify_pending=1,updated_at=? WHERE id=?", (digest,encode(request),now(),number))
        return self.get(number)

    def update(self, number, **fields):
        assert set(fields) <= {"state","result","reply_id","notify_pending"}
        with closing(self.connect()) as c, c:
            c.execute("UPDATE gift_requests SET " + ",".join(key+"=?" for key in fields) + ",updated_at=? WHERE id=?", (*fields.values(),now(),number))
        return self.get(number)

    def finish(self, record, **fields):
        assert set(fields) <= {"state","result","notify_pending"}
        with closing(self.connect()) as c, c:
            previous = c.execute("SELECT state,result,notify_pending FROM gift_requests WHERE id=?", (record["id"],)).fetchone()
            if previous and fields.get("notify_pending") == 1 and not previous["notify_pending"]:
                fields["notify_pending"] = int(any(key in fields and fields[key] != previous[key] for key in ("state","result")))
            c.execute("UPDATE gift_requests SET "+",".join(key+"=?" for key in fields)+",updated_at=? WHERE id=? AND source_hash=?",
                      (*fields.values(),now(),record["id"],record["source_hash"]))
        return self.get(record["id"])

    def confirmation(self, record, snapshot):
        data = encode(snapshot)
        token = hashlib.sha256((record["source_hash"]+data).encode()).hexdigest()[:16]
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT source_hash,attempted_at FROM gift_requests WHERE id=?", (record["id"],)).fetchone()
            if current["source_hash"] != record["source_hash"] or current["attempted_at"]:
                raise GiftClarification("Заявка изменилась. Откройте актуальную проверку подарка.")
            row = c.execute("SELECT * FROM gift_confirmations WHERE request_id=?", (record["id"],)).fetchone()
            if not row or row["token"] != token:
                c.execute("INSERT INTO gift_confirmations(request_id,source_hash,snapshot,token) VALUES(?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET source_hash=excluded.source_hash,snapshot=excluded.snapshot,token=excluded.token,decision=NULL,actor=NULL,decided_at=NULL",
                          (record["id"],record["source_hash"],data,token))
            return dict(c.execute("SELECT * FROM gift_confirmations WHERE request_id=?", (record["id"],)).fetchone())

    def decide(self, number, token, choice, actor):
        assert choice in ("yes","no")
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute("UPDATE gift_confirmations SET decision=?,actor=?,decided_at=? WHERE request_id=? AND token=? AND decision IS NULL AND source_hash=(SELECT source_hash FROM gift_requests WHERE id=? AND state='awaiting_confirmation' AND attempted_at IS NULL)",
                            (choice,actor,now(),number,token,number))
            if cur.rowcount != 1: return False
            text = "Добавление ещё одного шарфа подтверждено. Проверяю цвет, наличие и резерв…" if choice == "yes" else "Дополнительный шарф не добавлен. Существующий шарф оставлен в заказе."
            c.execute("UPDATE gift_requests SET state=?,result=?,notify_pending=1,updated_at=? WHERE id=?", ("new" if choice=="yes" else "declined",text,now(),number))
        return True

    def rotation_color(self, store_id, connection=None):
        if connection is None:
            with closing(self.connect()) as c: return self.rotation_color(store_id, c)
        row = connection.execute("SELECT color FROM gift_rotation WHERE store_id=?", (store_id,)).fetchone()
        if row: return row[0]
        # Seed from historical automatic choices without rewriting old plans.
        for row in connection.execute("SELECT request,plan FROM gift_requests WHERE attempted_at IS NOT NULL AND plan IS NOT NULL ORDER BY attempted_at DESC,id DESC"):
            request, plan = json.loads(row[0]), json.loads(row[1])
            if not request.get("color") and plan.get("action") == "add" and plan.get("store_id") == store_id:
                return scarf_color(plan.get("label"))
        return None

    def begin_check(self, number):
        with closing(self.connect()) as c, c:
            c.execute("UPDATE gift_requests SET check_count=check_count+1 WHERE id=?", (number,))
        return self.get(number)

    def claim(self, record, plan):
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            if plan.get("confirmed_extra"):
                confirmation = c.execute("SELECT decision,token,source_hash FROM gift_confirmations WHERE request_id=?", (record["id"],)).fetchone()
                if not confirmation or confirmation["decision"] != "yes" or confirmation["token"] != plan.get("confirmation_token") or confirmation["source_hash"] != record["source_hash"]:
                    raise GiftClarification("Добавление ещё одного шарфа не подтверждено. Откройте актуальный вопрос.")
            if plan.get("color_choice") == "rotation" and self.rotation_color(plan["store_id"], c) != plan.get("rotation_before"):
                raise GiftClarification("Очередь цветов обновилась. Нажмите «Проверить» для нового подбора шарфа.")
            try:
                cur = c.execute("UPDATE gift_requests SET order_id=?,slot=?,plan=?,attempted_at=?,state='submitting',updated_at=? WHERE id=? AND source_hash=? AND attempted_at IS NULL",
                                (plan["order_id"],plan.get("gift_slot",record["request"]["slot"]),encode(plan),now(),now(),record["id"],record["source_hash"]))
            except sqlite3.IntegrityError:
                raise GiftClarification("Такая заявка на подарок к этому заказу уже проводилась или ещё проверяется. Повторный шарф автоматически не добавляется.") from None
            if cur.rowcount != 1: raise GiftClarification("Заявка изменилась. Проверяю актуальный текст; подарок повторно не добавлен.")
            if plan.get("color_choice") == "rotation":
                c.execute("INSERT INTO gift_rotation(store_id,color) VALUES(?,?) ON CONFLICT(store_id) DO UPDATE SET color=excluded.color", (plan["store_id"],scarf_color(plan["label"])))
        return self.get(record["id"])

    def queue(self):
        with closing(self.connect()) as c:
            return [self.decode(row) for row in c.execute("SELECT * FROM gift_requests WHERE state IN ('new','submitting','uncertain') OR notify_pending=1 ORDER BY notify_pending DESC,updated_at,id LIMIT 20")]

    def retry(self, number):
        with closing(self.connect()) as c, c:
            c.execute("UPDATE gift_requests SET state=CASE WHEN attempted_at IS NOT NULL THEN 'uncertain' WHEN state IN ('done','declined') THEN state ELSE 'new' END,notify_pending=1,updated_at=? WHERE id=?", (now(),number))
        return self.get(number)


class GiftService:
    def __init__(self, service, database, store):
        self.service, self.store = service, store
        self.lookup = OrderInformation(service, database)
        self.lock = asyncio.Lock()

    def check_order(self, order, number):
        self.service.check_brand(order)
        if not re.search(r"(?<!\d)" + re.escape(number) + r"(?!\d)", str(order.get("name") or "")):
            raise GiftClarification("Номер документа МойСклад не совпадает с заявкой на подарок.")
        state = normalized((order.get("state") or {}).get("name"))
        if re.search(r"^(?:ВОЗВРАТ|ОТМЕН|ОБМЕН К НАМ|ОТГРУЖ|ДОСТАВЛЕН|ВРУЧЕН|ВЫДАН|В ПУТИ|ПРИНЯТ \(CDEK)", state):
            raise GiftClarification("У заказа статус «" + str((order.get("state") or {}).get("name") or "") + "». Подарок не добавлен; уточните заказ у ответственного менеджера.")
        if order.get("applicable") is not True:
            raise GiftClarification("Заказ МойСклад не проведён. Для резерва сначала проверьте проведение заказа.")
        store_id = entity_id(order.get("store") or {})
        if not re.fullmatch(UUID, store_id):
            raise GiftClarification("В заказе МойСклад не выбран склад для резерва.")
        return store_id

    async def document(self, docid):
        if not re.fullmatch(UUID, str(docid)): raise GiftClarification("Некорректный документ МойСклад.")
        order = await self.service.ms("GET", "/entity/customerorder/"+docid, params={"expand":"state"})
        positions = await self.lookup.rows("/entity/customerorder/"+docid+"/positions", {"expand":"assortment"})
        return order, positions

    async def catalog(self, store_id):
        rows = await self.lookup.rows("/entity/assortment", {"filter":"stockStore="+MS_API+"/entity/store/"+store_id+";search=DEMO SCARF;archived=false"})
        candidates = []
        for row in rows:
            color = scarf_color(row.get("name"))
            if not color or row.get("archived") or (row.get("meta") or {}).get("type") != "variant": continue
            if not re.fullmatch(UUID, str(row.get("id") or "")): continue
            if not re.search(r"\bONESIZE\b", normalized(row.get("name"))): continue
            row = dict(row, color=color, free=str(decimal(row.get("stock"))-decimal(row.get("reserve"))))
            candidates.append(row)
        return candidates

    def unavailable_color(self, color, catalog):
        available = [c for c in COLORS if any(p["color"] == c and decimal(p["free"]) >= 1 for p in catalog)]
        label = COLOR_NAMES[color]+" ("+color+")"
        if not available:
            return "Шарфа цвета «"+label+"» нет в свободном остатке на складе заказа. Других свободных цветов тоже нет. Подарок не добавлен и не заменён. Уточните у клиента другой цвет или ожидание поступления."
        choices = ", ".join(COLOR_NAMES[c]+" ("+c+")" for c in available)
        return ("Шарфа цвета «"+label+"» нет в свободном остатке на складе заказа. Для подарка потребуется другой цвет. "
                "Напишите клиенту и уточните, какой другой цвет шарфа он согласен получить. Доступны: "+choices+". "
                "После согласования отправьте новую заявку с номером заказа и согласованным цветом. "
                "Подарок пока не добавлен и не заменён.")

    async def prepare(self, record, *, check_only=False):
        request = record["request"]
        docs = await self.lookup.search("number", request["order_number"])
        # A return/exchange copy can share the code but is never a shipping order.
        docs = [d for d in docs if re.search(r"(?<!\d)"+re.escape(request["order_number"])+r"(?!\d)",str(d.get("name") or ""))
                and not re.match(r"^(?:возврат|отмен)",str(d.get("name") or "").strip(),re.I)]
        if len(docs) != 1:
            raise GiftClarification("Найдено подходящих заказов МойСклад: "+str(len(docs))+". Уточните номер или дубли документов у ответственного менеджера.")
        order, positions = await self.document(docs[0]["id"])
        store_id = self.check_order(order, request["order_number"])
        if len({p.get("id") for p in positions}) != len(positions) or any(not re.fullmatch(UUID,str(p.get("id") or "")) for p in positions):
            raise GiftClarification("Не удалось однозначно прочитать позиции заказа. Проверьте документ МойСклад.")
        gifts = gift_positions(positions)
        quantity = sum((decimal(p["quantity"]) for p in gifts), Decimal(0))
        slot = request["slot"]
        scarves = [p for p in positions if scarf_color((p.get("assortment") or {}).get("name")) and decimal(p.get("quantity",0)) > 0]
        confirmation = None
        if scarves and not check_only:
            if quantity >= 2:
                raise GiftClarification("В заказе уже есть два подарочных шарфа. Ещё один автоматически не добавляется; уточните у ответственного менеджера.")
            catalog = await self.catalog(store_id)
            if request["color"] and not any(p["color"] == request["color"] and decimal(p["free"]) >= 1 for p in catalog):
                raise GiftClarification(self.unavailable_color(request["color"],catalog)+" Сейчас в заказе: "+", ".join(p["assortment"]["name"] for p in scarves)+".")
            if not any(decimal(p["free"]) >= 1 for p in catalog):
                raise GiftClarification("Нет свободного шарфа на складе заказа. Существующий шарф оставлен в заказе.")
            confirmation = self.store.confirmation(record,{"order_id":order["id"],"scarves":[position_identity(p) for p in scarves]})
            if confirmation["decision"] != "yes":
                names = ", ".join(str(p["assortment"]["name"]) for p in scarves)
                raise GiftConfirmation("В заказе уже есть шарф: "+names+".\nДобавить ещё один? Существующий шарф останется в заказе."+("\nЗапрошен: "+COLOR_NAMES[request["color"]]+" ("+request["color"]+")." if request["color"] else ""))
            slot = int(quantity)+1
        if slot == 2 and quantity < 1:
            raise GiftClarification("Первый подарочный шарф в заказе не найден. Уточните первый подарок перед добавлением второго.")
        plan = {"order_id":order["id"],"order_number":request["order_number"],"store_id":store_id,
                "sum":str(decimal(order.get("sum",0))),"before":[position_identity(p) for p in positions],"gift_slot":slot}
        if confirmation:
            plan.update(confirmed_extra=True,confirmation_token=confirmation["token"])
        if quantity >= slot:
            names = ", ".join(dict.fromkeys(str(p["assortment"]["name"]) for p in gifts))
            if request["color"] and request["color"] not in {scarf_color(p["assortment"]["name"]) for p in gifts}:
                catalog = await self.catalog(store_id)
                if not any(p["color"] == request["color"] and decimal(p["free"]) >= 1 for p in catalog):
                    raise GiftClarification(self.unavailable_color(request["color"],catalog)+" Сейчас в заказе: "+names+".")
                raise GiftClarification("В заказе уже есть подарок: "+names+". В заявке указан "+request["color"]+"; этот цвет есть в наличии. Существующий подарок автоматически не заменён. Уточните замену у ответственного менеджера; второй шарф не добавлен.")
            remaining, targets = Decimal(slot), []
            for position in gifts:
                units = min(decimal(position["quantity"]),remaining); remaining -= units
                current = decimal(position.get("reserve") or 0)
                if units > current: targets.append({"id":position["id"],"reserve":str(units),"assortment":entity_id(position["assortment"])})
                if remaining == 0: break
            if not targets:
                return None, "Заказ "+request["order_number"]+": подарочных шарфов уже "+str(quantity)+", резерв подтверждён. "+names
            plan.update(action="reserve",targets=targets,label=names)
        else:
            catalog = await self.catalog(store_id)
            candidates = [p for p in catalog if (not request["color"] or p["color"] == request["color"]) and decimal(p["free"]) >= 1]
            if not candidates:
                if request["color"]:
                    raise GiftClarification(self.unavailable_color(request["color"],catalog))
                raise GiftClarification("Нет свободного шарфа доступного цвета на складе заказа. Подарок не добавлен и в резерв не поставлен.")
            available = {p["color"] for p in candidates}
            preferred = first_item_color(positions) if not request["color"] else request["color"]
            mode, previous = "explicit" if request["color"] else "item", None
            if preferred not in available:
                mode = "rotation"
                previous = self.store.rotation_color(store_id)
                start = (COLORS.index(previous)+1) % len(COLORS) if previous in COLORS else 0
                preferred = next(COLORS[(start+i) % len(COLORS)] for i in range(len(COLORS)) if COLORS[(start+i) % len(COLORS)] in available)
            chosen = next(p for p in candidates if p["color"] == preferred)
            if sum(p["color"]==chosen["color"] for p in candidates) != 1:
                raise GiftClarification("Найдено несколько модификаций шарфа одного цвета. Проверьте номенклатуру в МойСклад.")
            plan.update(action="add",assortment=chosen["id"],label="DEMO SCARF "+chosen["color"],
                        color_choice=mode,rotation_before=previous,
                        payload={"assortment":{"meta":{"href":MS_API+"/entity/variant/"+chosen["id"],"type":"variant","mediaType":"application/json"}},
                                 "quantity":1,"price":0,"discount":0,"reserve":1})
        return plan, None

    async def reserve(self, order, positions, targets):
        # Absolute position-only reserve updates are safe to reconcile/retry;
        # quantities, prices and other positions are never submitted in a PUT.
        catalog = None
        by_id = {p["id"]:p for p in positions}
        needed = {}
        for target in targets:
            position = by_id.get(target["id"])
            if not position or entity_id(position.get("assortment") or {}) != target["assortment"]:
                raise GiftClarification("Подарочная позиция изменилась. Проверьте резерв в МойСклад.")
            desired, current = decimal(target["reserve"]), decimal(position.get("reserve") or 0)
            if desired > decimal(position["quantity"]): raise GiftClarification("Количество подарка изменилось; проверьте резерв.")
            if current < desired:
                if decimal(position["quantity"]) != 1:
                    raise GiftClarification("Несколько подарков объединены в одной позиции без резерва. Проверьте резерв этой позиции в МойСклад.")
                needed[target["assortment"]] = needed.get(target["assortment"],Decimal(0)) + desired-current
        if needed:
            catalog = {p["id"]:p for p in await self.catalog(entity_id(order["store"]))}
            if any(key not in catalog or decimal(catalog[key]["free"]) < count for key,count in needed.items()):
                raise GiftClarification("Шарф есть в заказе, но свободного остатка для резерва не хватает. Проверьте склад; новый шарф не добавляется.")
        for target in targets:
            position = by_id[target["id"]]
            if decimal(position.get("reserve") or 0) < decimal(target["reserve"]):
                await self.service.ms("PUT","/entity/customerorder/"+order["id"]+"/positions/"+target["id"],body={"reserve":float(decimal(target["reserve"]))})

    async def reconcile(self, record):
        plan = record["plan"]
        order, positions = await self.document(plan["order_id"])
        self.service.check_brand(order)
        # A delayed response must never cause another append. Inspect the exact
        # new gift against the durable pre-request position IDs instead.
        if plan["action"] == "add":
            old_ids = {p["id"] for p in plan["before"]}
            added = [p for p in gift_positions(positions) if p["id"] not in old_ids and entity_id(p["assortment"]) == plan["assortment"] and decimal(p["quantity"]) == 1]
            if not added and not record["request"].get("color"):
                # A staff member may have fulfilled an unspecified-color gift
                # manually. Accept exactly the requested slot, never add again.
                gifts = gift_positions(positions)
                new = [p for p in gifts if p["id"] not in old_ids and decimal(p["quantity"]) == 1]
                if len(new) == 1 and sum((decimal(p["quantity"]) for p in gifts), Decimal(0)) == plan.get("gift_slot",record["request"]["slot"]):
                    added = new
            if len(added) != 1:
                found = ", ".join(dict.fromkeys(p["assortment"]["name"] for p in gift_positions(positions)))
                raise GiftClarification("Заказ "+record["request"]["order_number"]+": МойСклад не подтвердил добавление "+plan["label"]+"."+(" Найдены подарки: "+found+". Проверьте их цвет и количество." if found else " Подарочный шарф в заказе не найден.")+" Повторный шарф не добавлен; уточните у ответственного менеджера.")
            plan = dict(plan, label="DEMO SCARF "+scarf_color(added[0]["assortment"]["name"]), assortment=entity_id(added[0]["assortment"]))
            targets = [{"id":added[0]["id"],"reserve":"1","assortment":plan["assortment"]}]
        else: targets = plan["targets"]
        for old in plan["before"]:
            found = next((p for p in positions if p["id"] == old["id"]),None)
            if found is None or position_identity(found) != old:
                raise GiftClarification("Состав заказа изменился во время добавления подарка. Проверьте заказ и резерв в МойСклад; повторного добавления нет.")
        if decimal(order.get("sum",0)) != decimal(plan["sum"]):
            raise GiftClarification("Сумма заказа изменилась. Проверьте заказ в МойСклад; повторный подарок не добавляется.")
        if any(decimal(next(p for p in positions if p["id"]==t["id"]).get("reserve") or 0) < decimal(t["reserve"]) for t in targets):
            self.check_order(order,record["request"]["order_number"])
            await self.reserve(order,positions,targets)
            _, positions = await self.document(order["id"])
        by_id = {p["id"]:p for p in positions}
        if any(t["id"] not in by_id or decimal(by_id[t["id"]].get("reserve") or 0) < decimal(t["reserve"]) for t in targets):
            raise GiftClarification("Подарок найден, но резерв ещё не подтверждён. Нажмите «Проверить» позже.")
        return self.store.finish(record,state="done",result="Заказ "+record["request"]["order_number"]+": "+("дополнительный подарочный шарф" if plan.get("confirmed_extra") else "второй подарочный шарф" if record["request"]["slot"]==2 else "подарочный шарф")+" — "+plan["label"]+". В МойСклад "+("добавлен" if plan["action"]=="add" else "уже есть")+", цена 0 ₽, резерв подтверждён.",notify_pending=1)

    async def process(self, number):
        async with self.lock:
            record = self.store.begin_check(number)
            try:
                if record["attempted_at"]:
                    return await self.reconcile(record)
                if record["state"] == "done":
                    # An existing gift may have been added manually. Refresh
                    # its current presence/reserve instead of replaying a
                    # cached confirmation or creating another gift if removed.
                    plan, existing = await self.prepare(record,check_only=True)
                    if existing:
                        return self.store.finish(record,state="done",result=existing,notify_pending=1)
                    raise GiftClarification("При повторной проверке подарок или его резерв не подтверждены в МойСклад. Проверьте заказ у ответственного менеджера. Новый шарф не добавлен.")
                if record["state"] != "new": return record
                plan, existing = await self.prepare(record)
                if existing: return self.store.finish(record,state="done",result=existing,notify_pending=1)
                record = self.store.claim(record,plan)
                if plan["action"] == "add":
                    try:
                        await self.service.ms("POST","/entity/customerorder/"+plan["order_id"]+"/positions",body=plan["payload"])
                    except Exception as error:
                        LOG.warning("Gift append response needs reconciliation: %s",type(error).__name__)
                return await self.reconcile(record)
            except Exception as error:
                current = self.store.get(number)
                # An edited unattempted request will be picked up afresh.
                if current["source_hash"] != record["source_hash"]: return current
                state = "uncertain" if current["attempted_at"] else "awaiting_confirmation" if isinstance(error,GiftConfirmation) else "done" if record["state"] == "done" else "needs_info"
                text = str(error) if isinstance(error,GiftClarification) else "Не удалось проверить подарок в МойСклад. Нажмите «Проверить» позже; повторное добавление после задержки не выполняется."
                return self.store.finish(record,state=state,result=text,notify_pending=1)


class GiftUI:
    def __init__(self, service, database, path):
        self.database, self.store = database, GiftStore(path)
        self.service = GiftService(service,database,self.store)
        self.inflight = set()

    def location(self, message):
        if not message: return False
        try:
            chat = int(self.database.get_setting(CHAT_KEY) or 0)
            topic = int(self.database.get_setting(TOPIC_KEY) or 0)
        except ValueError: return False
        return bool(chat and message.chat.id == chat and int(message.message_thread_id or 0) == topic)

    async def external(self, update, context):
        message = update.message or update.edited_message
        if not self.location(message) or not message.text or (message.from_user and message.from_user.is_bot): return
        if message.text.startswith("/"): return
        reply = getattr(message,"reply_to_message",None)
        reply_text = str(getattr(reply,"text","") or "")
        try: request = parse_gift(message.text,reply_text)
        except GiftClarification as error:
            await message.reply_text(str(error),parse_mode=None); raise ApplicationHandlerStop
        if not request: return
        record = self.store.ingest(message,request,message.text+"\0"+reply_text)
        if len(self.inflight) < 8:
            context.application.create_task(self.run(record["id"],context.bot),update=update)
        raise ApplicationHandlerStop

    async def notify(self, record, bot):
        if not record["result"]: return
        # Owner-approved deployment checks have no Telegram source message.
        if record["source_chat"] == 0:
            self.store.update(record["id"],notify_pending=0)
            return
        checked = datetime.fromisoformat(record["updated_at"]).astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M:%S")
        text = "🎁 "+record["result"]+"\n\nПроверка №"+str(record["check_count"])+" · "+checked+" МСК"
        if record["edited_after_attempt"]: text += "\n\nТекст заявки изменён после обработки. Правка не меняет подарок автоматически; проверьте ранее добавленный шарф."
        keyboard = None if record["state"] == "declined" else InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Проверить",callback_data="gift:check:"+str(record["id"]))]])
        if record["state"] == "awaiting_confirmation" and record.get("confirmation"):
            prefix = "gift:decision:"+str(record["id"])+":"+record["confirmation"]["token"]+":"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Да, добавить ещё один",callback_data=prefix+"yes"),InlineKeyboardButton("Нет",callback_data=prefix+"no")]])
        try:
            if record["reply_id"]:
                try: await bot.edit_message_text(chat_id=record["source_chat"],message_id=record["reply_id"],text=text,reply_markup=keyboard,parse_mode=None)
                except BadRequest as error:
                    detail = str(error).lower()
                    if "message to edit not found" in detail or "message can't be edited" in detail:
                        record = dict(record,reply_id=None)
                    elif "message is not modified" not in detail: raise
            if not record["reply_id"]:
                reply = await bot.send_message(chat_id=record["source_chat"],message_thread_id=record["source_topic"] or None,
                                               reply_to_message_id=record["source_message"],allow_sending_without_reply=True,text=text,reply_markup=keyboard,parse_mode=None)
                self.store.update(record["id"],reply_id=reply.message_id)
            self.store.update(record["id"],notify_pending=0)
        except TelegramError:
            LOG.warning("Gift result delivery deferred")

    async def run(self, number, bot, manual=False):
        if number in self.inflight: return
        self.inflight.add(number)
        try:
            if manual:
                record = self.store.get(number)
                try:
                    await bot.edit_message_text(chat_id=record["source_chat"],message_id=record["reply_id"],
                        text="🎁 Заказ "+record["request"]["order_number"]+": проверяю подарок и резерв в МойСклад…",parse_mode=None)
                except TelegramError: pass
            record = await self.service.process(number)
            if record["notify_pending"]: await self.notify(record,bot)
        finally: self.inflight.discard(number)

    async def decision_result(self, number, bot):
        # An earlier result may still be sending. Apply the chosen answer last,
        # so stale buttons cannot overwrite the final shared message.
        while number in self.inflight:
            await asyncio.sleep(0.05)
        record = self.store.get(number)
        if record["state"] == "declined":
            await self.notify(record,bot)
        else:
            await self.run(number,bot,manual=True)

    async def callback(self, update, context):
        query = update.callback_query
        if not query or not self.location(query.message):
            if query: await query.answer("Проверка подарка доступна в теме «Подарки».",show_alert=True)
            raise ApplicationHandlerStop
        parts = query.data.split(":")
        record = self.store.get(int(parts[2]))
        if not record or record["source_chat"] != query.message.chat.id or record["source_topic"] != int(query.message.message_thread_id or 0):
            await query.answer("Заявка не найдена в этой теме.",show_alert=True); raise ApplicationHandlerStop
        if parts[1] == "decision":
            changed = self.store.decide(record["id"],parts[3],parts[4],getattr(query.from_user,"id",None))
            await query.answer(("Добавление подтверждено." if parts[4] == "yes" else "Дополнительный шарф не добавляется.") if changed else "Ответ уже принят или заявка изменилась. Откройте актуальное сообщение.")
            if changed:
                context.application.create_task(self.decision_result(record["id"],context.bot),update=update)
            raise ApplicationHandlerStop
        await query.answer("Проверка уже выполняется. Результат появится в этом сообщении." if record["id"] in self.inflight else "Проверяю подарок и резерв…")
        if record["id"] not in self.inflight:
            self.store.retry(record["id"])
            context.application.create_task(self.run(record["id"],context.bot,manual=True),update=update)
        raise ApplicationHandlerStop

    async def worker(self, application):
        while True:
            try:
                for record in self.store.queue():
                    if record["id"] not in self.inflight:
                        await self.run(record["id"],application.bot)
            except Exception as error:
                LOG.warning("Gift reconciliation deferred: %s",type(error).__name__)
            await asyncio.sleep(30)


def register_gifts(application, service, database, path):
    ui = GiftUI(service,database,path)
    application.bot_data["gift_scarves_ui"] = ui
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,ui.external),group=-8)
    application.add_handler(CallbackQueryHandler(ui.callback,pattern=r"^gift:(?:check:\d+|decision:\d+:[a-f0-9]{16}:(?:yes|no))$"),group=-8)
    return ui
