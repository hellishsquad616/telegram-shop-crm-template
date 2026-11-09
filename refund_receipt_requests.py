"""Durable staff receipt requests. Reading proof never starts a refund."""
import asyncio
import json
import logging
import re
import time
from contextlib import closing

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import BadRequest, TelegramError, RetryAfter

LOG=logging.getLogger("shopcrm.refund_receipt_requests")


def parse_receipt_request(text):
    match=re.fullmatch(r"\s*нужен\s+чек\s+возврата\s*[-—–:]?\s*(?:(?:по\s+)?заказ(?:а|у)?\s*[:№#]?\s*)?[№#]?\s*(\d{5,20})[\s.!]*",str(text or ""),re.I)
    return match[1] if match else None


class ReceiptRequestStore:
    def __init__(self,path):
        self.path=path
        with closing(self.connect()) as c,c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS customer_refund_receipt_requests(
                    id INTEGER PRIMARY KEY,source_chat INTEGER NOT NULL,source_message INTEGER NOT NULL,
                    source_text TEXT NOT NULL,order_number TEXT NOT NULL,actor INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'new',refund_request_id INTEGER,
                    owner_message_id INTEGER,reply_message_id INTEGER,last_error TEXT,
                    next_check_at INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    UNIQUE(source_chat,source_message));
                CREATE TABLE IF NOT EXISTS customer_refund_receipt_deliveries(
                    request_id INTEGER NOT NULL,proof_key TEXT NOT NULL,message_id INTEGER NOT NULL,
                    status TEXT NOT NULL,PRIMARY KEY(request_id,proof_key),
                    FOREIGN KEY(request_id) REFERENCES customer_refund_receipt_requests(id));
            """)
            columns={r[1] for r in c.execute('PRAGMA table_info(customer_refund_receipt_requests)')}
            if 'outcome' not in columns:c.execute("ALTER TABLE customer_refund_receipt_requests ADD COLUMN outcome TEXT NOT NULL DEFAULT 'unknown'")

    def connect(self):
        from refund_storage import connect_database
        return connect_database(self.path)

    def get(self,number):
        with closing(self.connect()) as c:return dict(c.execute("SELECT * FROM customer_refund_receipt_requests WHERE id=?",(number,)).fetchone())

    def add(self,chat,message,text,actor):
        from customer_refunds import now,RefundError
        number=parse_receipt_request(text)
        if not number:raise RefundError("Укажите: Нужен чек возврата — номер заказа.")
        with closing(self.connect()) as c,c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM customer_refund_receipt_requests WHERE source_chat=? AND source_message=?",(chat,message)).fetchone()
            if row:
                if row["order_number"]!=number:raise RefundError("Для другого заказа отправьте новый запрос чека отдельным сообщением.")
                return dict(row),False
            cur=c.execute("INSERT INTO customer_refund_receipt_requests(source_chat,source_message,source_text,order_number,actor,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(chat,message,text,number,actor,now(),now()))
            number=cur.lastrowid
        return self.get(number),True

    def update(self,number,**fields):
        from customer_refunds import now
        assert fields and set(fields)<={"state","outcome","refund_request_id","owner_message_id","reply_message_id","last_error","next_check_at"}
        fields["updated_at"]=now()
        with closing(self.connect()) as c,c:
            c.execute("UPDATE customer_refund_receipt_requests SET "+",".join(k+"=?" for k in fields)+" WHERE id=?",(*fields.values(),number))
        return self.get(number)

    def pending(self):
        with closing(self.connect()) as c:
            return [dict(r) for r in c.execute("SELECT * FROM customer_refund_receipt_requests WHERE state<>'sent' AND next_check_at<=? ORDER BY id",(int(time.time()),))]

    def delivery(self,number,key):
        with closing(self.connect()) as c:
            row=c.execute("SELECT * FROM customer_refund_receipt_deliveries WHERE request_id=? AND proof_key=?",(number,key)).fetchone()
            return dict(row) if row else None

    def for_refund(self,number,order_number):
        with closing(self.connect()) as c:
            return [dict(r) for r in c.execute('SELECT * FROM customer_refund_receipt_requests WHERE refund_request_id=? OR order_number=? ORDER BY id DESC',(number,order_number))]

    def delivered(self,number,key,message,status):
        with closing(self.connect()) as c,c:
            c.execute("INSERT INTO customer_refund_receipt_deliveries VALUES(?,?,?,?) ON CONFLICT(request_id,proof_key) DO UPDATE SET message_id=excluded.message_id,status=excluded.status",(number,key,message,status))


class ReceiptRequests:
    def __init__(self,ui):
        self.ui=ui
        self.store=ReceiptRequestStore(ui.store.path)
        self.locks={}

    def annotate(self,record):
        if record['request'].get('kind')!='receipt_request':return record
        result=dict(record)
        jobs=self.store.for_refund(record['id'],record['request']['order_number'])
        if jobs:result['receipt_outcome']='ready' if jobs[0]['state']=='sent' else jobs[0]['outcome']
        related=next((r for r in self.ui.store.for_order(record['request']['order_number']) if r['id']!=record['id'] and (r['state']=='succeeded' or r['state'] in {'pending','uncertain','submitting'} and r.get('attempted_at'))),None)
        if related:
            result['related_refund']={k:related[k] for k in ('id','revision','state','receipt_json')}
            result['receipt_outcome']=('ready' if related.get('receipt_json') else 'receipt_pending') if related['state']=='succeeded' else 'processing'
        return result

    async def refresh_for_refund(self,number,context):
        record=self.ui.store.get(number)
        for job in self.store.for_refund(number,record['request']['order_number']):
            await self.process(job['id'],context)
        return self.annotate(self.ui.store.get(number))

    async def reply(self,job,context,text):
        from customer_refunds import SUPPORT_TOPIC
        args=dict(chat_id=job["source_chat"],message_thread_id=SUPPORT_TOPIC,text=text,parse_mode=None)
        try:return await context.bot.send_message(**args,reply_to_message_id=job["source_message"])
        except BadRequest as error:
            if not self.missing_reply(error):raise
            return await context.bot.send_message(**args)

    @staticmethod
    def missing_reply(error):
        text=str(error).lower()
        return "not found" in text and any(v in text for v in ("reply","replied"))

    async def known_proofs(self,records):
        from customer_refunds import ACTIVE,RefundError
        from yandex_refunds import is_yandex
        proofs=[];waiting=False
        for original in records:
            record=original
            async with self.ui.locks.setdefault(record["id"],asyncio.Lock()):
                record=self.ui.store.get(record["id"])
                if record["state"] in ACTIVE:
                    try:record=await self.ui.service.check(record)
                    except RefundError:waiting=True
                if record["state"]=="succeeded" and not record.get("receipt_json"):
                    try:record=await self.ui.service.receipt(record)
                    except RefundError:waiting=True
                if record["state"] in ACTIVE or record["state"]=="succeeded" and not record.get("receipt_json"):waiting=True
                if not record.get("receipt_json") or not record.get("refund_id"):continue
                if record["state"]=="succeeded" and (record["refund_status"]=="succeeded" or is_yandex(record) and record["refund_status"]=="SUCCESS"):
                    proofs.append(record)
                elif is_yandex(record) and record["state"]=="pending":
                    self.ui.service.split.pending_identity(record)
                    proofs.append(record)
                elif is_yandex(record) and record["state"]=="failed" and record["receipt_json"].get("status")=="FAIL":proofs.append(record)
        outcome='processing' if any(r['state'] in ACTIVE for r in records) else 'receipt_pending' if any(r['state']=='succeeded' for r in records) else 'unknown'
        return proofs,waiting,outcome

    async def external_proofs(self,job,records):
        """Recover authentic service documents even for manual/older refunds."""
        from customer_refunds import (parse_request,norm,RefundError,SHOP_ID,cents,money,
            registered_yoo_receipt,valid_contact,delivery,payment_method_from_label)
        from completed_refunds import yandex_history,yandex_proof,financial_items
        from database import Database
        s=self.ui.service
        original=Database(s.crm_path).get_order_by_number(job["order_number"]) if s.crm_path else None
        original=original or {}
        request=parse_request(job["order_number"])
        request.update(name=original.get("customer_name") or "",email=original.get("email") or "",phone=original.get("phone") or "")
        record={"request":request,"snapshot":{},"state":"succeeded"}
        bound=next((r for r in records if r.get('snapshot') and r.get('payment_id')),None)
        chosen=next((r for r in records if r['request'].get('method_source')=='owner'),None)
        method=(bound or chosen or {}).get('request',{}).get('method') or payment_method_from_label(original.get('payment_method')) or (records[0]['request'].get('method') if records else None)
        yandex=method=='yandex_split'
        if bound and not yandex and re.fullmatch(r'[a-f0-9-]{36}',bound['payment_id']):request['payment_id']=bound['payment_id']
        if yandex:
            oid=s.split.api.order_id(request)
            data=await s.split.api.order(oid)
            auth,refunds,total,reserved=yandex_history(data,oid)
            contact=valid_contact({"email":data["order"].get("fiscalContact")},request)
            if refunds and not contact:raise RefundError("Не получен контакт исходной оплаты для подтверждения Яндекса.")
            proofs=[]
            for op in refunds:
                proof=yandex_proof(data,op,contact)
                items=[]
                if cents(op["amount"])==total:
                    from yandex_refunds import cart_items
                    try:items=[{"name":i["title"],"quantity":i["quantity"],"unit_cents":i["unit_cents"],"delivery":delivery(i["title"])} for i in cart_items(data["order"])]
                    except RefundError:pass # The service may clear the cart after a full refund.
                proofs.append({**record,"request":{**request,"method":"yandex_split"},"refund_id":"yandex:"+op["operationId"],
                    "payment_id":"yandex:"+oid,"amount_cents":cents(op["amount"]),"state":"pending" if op["status"]=="PENDING" else "succeeded",
                    "receipt_json":proof,"snapshot":{"payment_method":data["order"]["paymentMethod"]["methodType"],"payment_amount_cents":total,"customer":request,"financial_items":items}})
            pending=any(op['status']=='PENDING' for op in refunds)
            return proofs,pending,'processing' if pending else 'ready' if refunds else 'not_refunded'
        payment=await s.payment(record,{})
        if payment.get("status")!="succeeded" or payment.get("paid") is not True or payment.get("test") is not False or (payment.get("recipient") or {}).get("account_id")!=SHOP_ID or not s.payment_matches(payment,job["order_number"]) or payment.get("amount",{}).get("currency")!="RUB" or payment.get("transfers") or payment.get("deal"):
            raise RefundError("Не подтверждена исходная оплата этого заказа в магазине ЮKassa.")
        history=await s.yoo.pages('/refunds',{'payment_id':payment['id']})
        if len({x.get('id') for x in history})!=len(history) or any(not x.get('id') or x.get('payment_id')!=payment['id'] or x.get('status') not in {'succeeded','pending','canceled'} or (x.get('amount') or {}).get('currency')!='RUB' for x in history):
            raise RefundError('Не подтверждена история возвратов ЮKassa.')
        returned=sum(cents(x['amount']['value']) for x in history if x['status']=='succeeded')
        reserved=sum(cents(x['amount']['value']) for x in history if x['status'] in {'succeeded','pending'})
        if returned!=cents((payment.get('refunded_amount') or money(0))['value']) or reserved>cents(payment['amount']['value']) or any(cents(x['amount']['value'])<=0 for x in history):raise RefundError('История возвратов ЮKassa ещё не согласована с оплатой.')
        proofs=[];waiting=any(x['status']=='pending' for x in history)
        for refund in history:
            if refund['status']!='succeeded':continue
            rows=await s.yoo.pages('/receipts',{'refund_id':refund['id']})
            rows=[x for x in rows if x.get('type')=='refund' and x.get('refund_id')==refund['id']]
            if not rows or len(rows)==1 and rows[0].get('status')=='pending':waiting=True;continue
            if len(rows)!=1 or rows[0].get('status')!='succeeded' or rows[0].get('payment_id') not in (None,payment['id']):raise RefundError('Не подтверждён зарегистрированный чек именно этого возврата.')
            item={**record,'refund_id':refund['id'],'payment_id':payment['id'],'amount_cents':cents(refund['amount']['value'])}
            item['receipt_json']=registered_yoo_receipt(rows[0],item)
            item['snapshot']={'payment_amount_cents':cents(payment['amount']['value']),'financial_items':financial_items(rows[0]),'customer':request}
            proofs.append(item)
        outcome='processing' if any(r['status']=='pending' for r in history) else 'receipt_pending' if returned and waiting else 'ready' if returned else 'not_refunded'
        return proofs,waiting,outcome

    async def deliver(self,job,context,record):
        from customer_refunds import hashed,rubles
        from refund_receipt_image import render_refund
        from yandex_refunds import is_yandex,yandex_label
        key=hashed([job['order_number'],record.get('payment_id'),record['refund_id']])
        status=record['receipt_json'].get('status','succeeded')
        previous=self.store.delivery(job['id'],key)
        if previous and previous['status']==status:return
        if status=='FAIL' and not previous:return
        same_source=bool(record.get('id') and record.get('source_chat')==job['source_chat'] and record.get('source_message')==job['source_message'])
        if same_source and record.get('ms_synced'):
            await self.ui.topic_result(record,context)
            current=self.ui.store.get(record['id'])
            if current.get('receipt_support_message_id'):
                self.store.delivered(job['id'],key,current['receipt_support_message_id'],status);return
        # The regular refund workflow may have already replied to this request.
        if not previous and record.get('source_chat')==job['source_chat'] and record.get('source_message')==job['source_message'] and record.get('receipt_support_message_id'):
            self.store.delivered(job['id'],key,record['receipt_support_message_id'],status);return
        split=is_yandex(record)
        title={'PENDING':'⏳ Возврат в процессе','FAIL':'❌ Возврат не выполнен'}.get(status,'✅ Возврат проведён')
        caption=f"{title} · заказ {job['order_number']}\nСумма: {rubles(record['amount_cents'])}\n"+(f"Подтверждение операции {yandex_label(record)}. Не является фискальным чеком." if split else 'Чек возврата зарегистрирован в ЮKassa.')
        picture=await asyncio.to_thread(render_refund,record)
        try:
            if previous:
                try:
                    await context.bot.edit_message_media(chat_id=job['source_chat'],message_id=previous['message_id'],media=InputMediaPhoto(media=picture,caption=caption,parse_mode=None))
                    self.store.delivered(job['id'],key,previous['message_id'],status);return
                except BadRequest as error:
                    if 'message is not modified' in str(error).lower():self.store.delivered(job['id'],key,previous['message_id'],status);return
                    if 'message to edit not found' not in str(error).lower():raise
                    picture.seek(0)
            message=await self.ui.support_photo({**record,'source_message':job['source_message']},context,picture,caption)
            self.store.delivered(job['id'],key,message.message_id,status)
            if same_source:self.ui.store.update(record['id'],receipt_support_message_id=message.message_id)
        finally:picture.close()

    async def route_owner(self,job,context,records,reason):
        from customer_refunds import OWNER,RefundError
        if not records:
            record,_=self.ui.store.ingest(job['source_chat'],job['source_message'],job['source_text'],job['actor'])
            if record['owner_message_id'] is None:self.ui.store.update(record['id'],owner_message_id=0)
            records=[record]
        record=records[0]
        job=self.store.update(job['id'],refund_request_id=record['id'])
        if not job['owner_message_id']:
            message=await context.bot.send_message(chat_id=OWNER,parse_mode=None,
                text=f"🧾 Сотрудник запросил чек возврата · заказ {job['order_number']}\nЗаявка #{record['id']}\n\n{reason}\n\nПроверьте существующий возврат. Если деньги ещё не возвращены, проведение потребует вашего подтверждения суммы.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Оформить возврат' if job['outcome']=='not_refunded' else 'Проверить чек' if job['outcome'] in {'processing','receipt_pending'} else 'Открыть возврат',callback_data=f"cr:view:{record['id']}:{record['revision']}")]]))
            job=self.store.update(job['id'],owner_message_id=message.message_id)
        if not job['reply_message_id']:
            detail={'not_refunded':'Чек возврата отсутствует: возврат ещё не проведён. Владельцу предложено оформить возврат.',
                'receipt_pending':'Возврат проведён, чек ещё формируется. Отправлю его автоматически после регистрации.',
                'processing':'Возврат обрабатывается платёжным сервисом. Повторное проведение не требуется.'}.get(job['outcome'],'Чек пока не подтверждён. Запрос передан владельцу для проверки.')
            message=await self.reply(job,context,f"Заказ {job['order_number']}: {detail}\nЗаявка #{record['id']}.")
            self.store.update(job['id'],reply_message_id=message.message_id)

    async def process(self,number,context):
        from customer_refunds import RefundError
        async with self.locks.setdefault(number,asyncio.Lock()):
            job=self.store.get(number)
            if job['state']=='sent':return
            try:
                records=self.ui.store.for_order(job['order_number'])
                proofs,waiting,outcome=await self.known_proofs(records)
                reason='В боте пока нет подтверждённого чека. Требуется проверка возврата.'
                if not proofs:
                    try:
                        proofs,external_waiting,external_outcome=await self.external_proofs(job,records)
                        waiting=waiting or external_waiting
                        if outcome=='unknown':outcome=external_outcome
                    except RefundError as error:reason=str(error)
                job=self.store.update(number,outcome=outcome)
                for record in proofs:await self.deliver(job,context,record)
                succeeded=any(r['state']=='succeeded' for r in proofs)
                if proofs and succeeded and not waiting:
                    self.store.update(number,state='sent',outcome='ready',last_error=None);return
                if outcome=='receipt_pending':reason='Деньги возвращены, чек ещё формируется. Отправим документ автоматически после регистрации; повторно возвращать деньги нельзя.'
                elif outcome=='processing':reason='Возврат ещё обрабатывается платёжным сервисом. Новую денежную операцию создавать нельзя.'
                elif outcome=='not_refunded':reason='Чек возврата отсутствует: платёжный сервис не подтвердил проведённого возврата. Можно оформить возврат после проверки состава и подтверждения суммы.'
                await self.route_owner(self.store.get(number),context,records,reason)
                self.store.update(number,state='waiting',last_error=reason,next_check_at=int(time.time())+30)
            except RetryAfter as error:
                delay=error.retry_after.total_seconds() if hasattr(error.retry_after,'total_seconds') else float(error.retry_after)
                self.store.update(number,last_error='Telegram временно ограничил доставку.',next_check_at=int(time.time()+delay+1))
            except (RefundError,TelegramError) as error:
                self.store.update(number,last_error=str(error),next_check_at=int(time.time())+30)
            except Exception as error:
                LOG.warning('Receipt request failed: %s',type(error).__name__)
                self.store.update(number,last_error='Проверка чека временно недоступна.',next_check_at=int(time.time())+30)

    async def reconcile(self,context):
        for job in self.store.pending():await self.process(job['id'],context)
