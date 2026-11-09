"""Reconcile a completed MS document with an existing refund; no money writes."""
from contextlib import closing
from datetime import datetime
import uuid

from customer_refunds import (
    OWNER, SHOP_ID, SETTINGS, MOSCOW, RefundError, approval_hash, cents, quantity,
    money, dump, now, norm, hashed, completed_return_state, order_matches,
    selected_document_items, document_items, gift_checked_items, make_receipt,
    names_match, delivery, CompletedMismatch, valid_contact, GiftClarification,
)
from refund_storage import connect_database


def financial_items(receipt):
    items=[]
    for i in receipt.get("items",[]):
        if (i.get("amount") or {}).get("currency")!="RUB":
            raise RefundError("Валюта чека отличается от платежа.")
        unit=cents(i["amount"]["value"])
        if unit<0:raise RefundError("Отрицательная цена в чеке возврата.")
        items.append({"name":i["description"],"quantity":quantity(i["quantity"]),
                      "unit_cents":unit,"delivery":delivery(i["description"])})
    return items


def same_financial_items(left,right):
    remaining=[dict(i) for i in right if i["unit_cents"]>0]
    for item in (i for i in left if i["unit_cents"]>0):
        matches=[i for i in remaining if i["quantity"] and i["unit_cents"]==item["unit_cents"] and names_match(i["name"],item["name"])]
        if len(matches)!=1 or matches[0]["quantity"]<item["quantity"]:return False
        matches[0]["quantity"]-=item["quantity"]
    return not any(i["quantity"] for i in remaining)


def return_date(order):
    values=[a.get("value") for a in order.get("attributes",[]) if norm(a.get("name"))=="дата проведения возврата средств" and a.get("value")]
    if len(values)!=1:return None
    try:
        stamp=datetime.fromisoformat(str(values[0]))
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=MOSCOW)).isoformat()
    except (TypeError,ValueError):return None


async def preview_completed(service,record,order,positions):
    r=record["request"]
    if not completed_return_state((order.get("state") or {}).get("name")) or not order_matches(order.get("name"),r["order_number"]):
        raise RefundError("Нужен проведённый документ именно этого заказа.")
    service.check_brand(order)
    if r["method"]=="yandex_split":return await preview_yandex_completed(service,record,order,positions)
    if r["method"]!="yookassa":raise RefundError("Не подтверждён платёжный сервис проведённого возврата.")
    checked_order={**order,"state":{"name":"Возврат"}}
    raw=document_items(checked_order,positions,"all")
    payment=await service.payment(record,order)
    pid=payment.get("id")
    if (payment.get("status")!="succeeded" or payment.get("paid") is not True or payment.get("test") is not False
        or (payment.get("recipient") or {}).get("account_id")!=SHOP_ID or not service.payment_matches(payment,r["order_number"])
        or (payment.get("amount") or {}).get("currency")!="RUB" or payment.get("transfers") or payment.get("deal")):
        raise RefundError("Не подтверждена связь боевой оплаты вашего магазина с номером заказа.")
    history=await service.yoo.pages("/refunds",{"payment_id":pid})
    if len({x.get("id") for x in history})!=len(history) or any(not x.get("id") or x.get("payment_id")!=pid or x.get("status") not in {"succeeded","pending","canceled"} or (x.get("amount") or {}).get("currency")!="RUB" for x in history):
        raise RefundError("Не подтверждена история существующих возвратов ЮKassa.")
    completed=[x for x in history if x["status"]=="succeeded"]
    refunded=sum(cents(x["amount"]["value"]) for x in completed)
    if refunded!=cents((payment.get("refunded_amount") or money(0))["value"]):
        raise RefundError("В МойСклад стоит «Возврат-проведен», но история ЮKassa ещё не согласована с возвращённой суммой. Повторная отправка денег запрещена.")
    if not completed:
        if any(x["status"]=="pending" for x in history):
            raise RefundError("В МойСклад стоит «Возврат-проведен», а ЮKassa ещё обрабатывает возврат. Дождитесь ответа сервиса; новая операция не создаётся.")
        review={"status":"ms_completed_yoo_not_refunded","document_id":order["id"],"document_name":order["name"],
            "document_updated":order.get("updated"),"items":raw,"ms_date":return_date(order),"payment_id":pid,
            "payment_amount_cents":cents(payment["amount"]["value"]),"refunded_cents":0,
            "source_hash":record["source_hash"],"settings_hash":hashed(SETTINGS),
            "refund_history":[{k:x.get(k) for k in ("id","status","amount")} for x in history]}
        review["basis"]=hashed(review)
        raise CompletedMismatch(review)
    raw,items=selected_document_items(checked_order,positions,r)
    receipts=await service.yoo.pages("/receipts",{"payment_id":pid})
    purchases=[x for x in receipts if x.get("type")=="payment" and x.get("status")=="succeeded"]
    if len(purchases)!=1:raise RefundError("Для сверки проведённого возврата нужен один исходный зарегистрированный чек оплаты.")
    paid=financial_items(purchases[0]);paid_total=cents(payment["amount"]["value"])
    if sum(i["quantity"]*i["unit_cents"] for i in paid)!=paid_total:
        raise RefundError("Состав исходного чека не совпал с оплаченной суммой.")
    details=await service.customer_details(record,order)
    if r['scope']=='compensation':
        from refund_compensation import compensation_target
        items=compensation_target(r,raw,paid)
        excluded,gift_review=[],None
    else:
        items,excluded,gift_review=gift_checked_items(record,order,raw,items,paid,pid,sorted(x["id"] for x in completed))
    refund_receipts={}
    for refund in completed:
        rows=await service.yoo.pages("/receipts",{"refund_id":refund["id"]})
        rows=[x for x in rows if x.get("type")=="refund" and x.get("refund_id")==refund["id"]]
        if len(rows)>1:raise RefundError("У существующего возврата несколько чеков. Нужна сверка.")
        if rows:
            receipt=rows[0]
            if receipt.get("payment_id") not in (None,pid) or receipt.get("status") not in {"succeeded","pending","canceled"}:
                raise RefundError("Не подтверждена принадлежность чека возврата исходной оплате.")
            actual=financial_items(receipt)
            if sum(i["quantity"]*i["unit_cents"] for i in actual)!=cents(refund["amount"]["value"]):
                raise RefundError("Сумма существующего чека возврата отличается от операции ЮKassa.")
            refund_receipts[refund["id"]]=receipt
    matches=[]
    for refund in completed:
        if r.get("refund_id") and r["refund_id"]!=refund["id"]:continue
        others=[refund_receipts.get(x["id"]) for x in completed if x["id"]!=refund["id"]]
        if any(not x or x.get("status")!="succeeded" for x in others):continue
        try:
            wanted=make_receipt(payment,purchases,others,items,details.get("receipt_contact") or {},r["scope"],excluded)
            if r['scope']=='compensation':
                from refund_compensation import compensation_receipt
                wanted=compensation_receipt(wanted,r['compensation_cents'])
            expected=financial_items(wanted)
        except RefundError:continue
        amount=sum(i["quantity"]*i["unit_cents"] for i in expected)
        actual=refund_receipts.get(refund["id"])
        if amount!=cents(refund["amount"]["value"]) and not (actual and r['scope']=='goods' and len(completed)==1 and cents(refund['amount']['value'])==paid_total):continue
        # With no receipt yet, only a single full-payment refund proves all items.
        if actual:
            if not same_financial_items(expected,financial_items(actual)):
                # A sole, fully refunded payment also proves a request for its
                # goods. Show the actual full receipt, including delivery;
                # this path can only publish proof, never send money again.
                if r['scope']!='goods' or len(completed)!=1 or cents(refund['amount']['value'])!=paid_total:continue
                try:
                    full=make_receipt(payment,purchases,[],items,details.get('receipt_contact') or {},'all',excluded)
                except RefundError:continue
                if not same_financial_items(financial_items(full),financial_items(actual)):continue
                expected=financial_items(full);amount=paid_total
                details['warnings']=list(dict.fromkeys(details['warnings']+['Платёж уже возвращён полностью, включая доставку. Отправляется фактический полный чек; новая денежная операция не создаётся.']))
        elif len(completed)!=1 or amount!=paid_total or r["scope"]!="all":continue
        stamp=refund.get("created_at") or (actual or {}).get("registered_at")
        try:
            stamp=datetime.fromisoformat(str(stamp).replace("Z","+00:00"))
            if stamp.tzinfo is None:raise ValueError
        except (TypeError,ValueError):continue
        matches.append((refund,expected,wanted["customer"],stamp.isoformat()))
    if len(matches)!=1:
        raise RefundError("В МойСклад уже стоит «Возврат-проведен». Не удалось однозначно сопоставить существующий возврат ЮKassa с выбранными товарами и доставкой. Укажите refund_id и проверьте состав документа. Новая денежная операция не создаётся.")
    refund,financial,contact,stamp=matches[0]
    snapshot={"order":{"id":order["id"],"name":order["name"],"state":order["state"]["name"],"updated":order.get("updated"),"items":raw},
        "payment_id":pid,"amount_cents":cents(refund["amount"]["value"]),"payment_amount_cents":paid_total,"available_cents":paid_total-refunded,
        "financial_items":financial,"customer_hash":await service.customer(record,order),"customer":{**details["customer"],**contact},
        "customer_warnings":details["warnings"],"receipt_contact":contact,"source_hash":record["source_hash"],"settings_hash":hashed(SETTINGS),
        "previous_refunds":sorted(x["id"] for x in completed),
        "existing_refund":{"id":refund["id"],"status":"succeeded","completed_at":stamp,"ms_date":return_date(order)}}
    if r.get("item_selector") or raw!=items:snapshot["selected_items"]=items
    if r['scope']=='compensation':snapshot['compensation']={'amount_cents':snapshot['amount_cents'],'item_stays_with_customer':True,'item_selector':r.get('item_selector')}
    if gift_review:snapshot.update(gift_review=gift_review,excluded_gifts=excluded)
    snapshot["hash"]=approval_hash(snapshot)
    return snapshot


async def approve_completed(service,number,revision,actor):
    if not OWNER or actor!=OWNER:raise RefundError("Отправку чека подтверждает только владелец.")
    record=service.store.get(number)
    if record["state"] in {"succeeded","pending"} and (record.get("snapshot") or {}).get("existing_refund") and record["approved_by"]==OWNER:return record
    if record["state"]!="ready" or record["revision"]!=revision or record["attempted_at"] or not (record.get("snapshot") or {}).get("existing_refund"):
        raise RefundError("Сначала проверьте существующий возврат и подтвердите актуальную карточку.")
    fresh=await service.preview(record,record["ms_order_id"])
    if fresh["hash"]!=record["snapshot"]["hash"]:
        service.store.prepare(number,revision,fresh)
        raise RefundError("Данные существующего возврата изменились. Подтвердите обновлённую карточку.")
    existing=fresh["existing_refund"]
    split=fresh.get("provider")=="yandex_split"
    state=existing["status"]
    proof=existing.get("proof") if split else None
    receipt_status=("external_pending" if state=="pending" else "external") if split else "pending"
    with closing(connect_database(service.store.path)) as c,c:
        c.execute("BEGIN IMMEDIATE")
        duplicate=c.execute("SELECT id FROM customer_refund_requests WHERE refund_id=? AND id<>?",(existing["id"],number)).fetchone()
        if duplicate:raise RefundError("Этот возврат уже учтён в заявке #"+str(duplicate["id"])+". Откройте её, чтобы проверить отправленный чек.")
        changed=c.execute("""UPDATE customer_refund_requests SET state=?,refund_id=?,refund_status=?,
            succeeded_at=?,receipt_status=?,receipt_json=?,split_booked_at=?,ms_synced=1,approved_by=?,attempted_at=?,last_error=NULL,updated_at=?
            WHERE id=? AND revision=? AND state='ready' AND attempted_at IS NULL AND snapshot=?""",
            (state,existing["id"],proof["status"] if split else "succeeded",existing.get("completed_at"),receipt_status,
             dump(proof) if proof else None,existing.get("ms_date") if split else None,
             actor,now(),now(),number,revision,dump(record["snapshot"])))
        if changed.rowcount!=1:raise RefundError("Заявка изменилась. Откройте актуальную карточку.")
        service.store.audit(c,number,revision,actor,"existing_refund_publication_approved")
    return service.store.get(number)


async def mark_completed_chargeback(service,record,actor):
    if not OWNER or actor!=OWNER:raise RefundError("Отметить чарджбек может только владелец.")
    if record.get("chargeback_at"):return record
    try:
        fresh=await service.preview(record,record["completed_review"]["document_id"])
    except CompletedMismatch as error:
        if error.review["basis"]!=record["completed_review"]["basis"]:
            service.store.preparation_error(record["id"],record["revision"],error)
            raise RefundError("Данные МойСклад или ЮKassa изменились. Проверьте обновлённую карточку перед отметкой чарджбека.")
        return service.store.chargeback(record["id"],record["revision"],actor)
    except RefundError as error:
        service.store.preparation_error(record["id"],record["revision"],error)
        raise
    service.store.prepare(record["id"],record["revision"],fresh)
    raise RefundError("Расхождение статусов больше не подтверждается. Проверьте документ и платёж заново; уведомление о чарджбеке не отправлено.")


async def check_completed(service,record):
    """Verify completion for publication without touching MS or making a POST."""
    try:
        order,positions=await service.document(record["ms_order_id"])
        snapshot=record["snapshot"]
        if not completed_return_state((order.get("state") or {}).get("name")) or order.get("name")!=snapshot["order"]["name"]:
            raise RefundError("Статус или номер проведённого документа МойСклад изменился. Чек не отправлен до сверки.")
        raw=document_items({**order,"state":{"name":"Возврат"}},positions,"all" if snapshot.get("selected_items") else record["request"]["scope"])
        if raw!=snapshot["order"]["items"] or return_date(order)!=snapshot["existing_refund"]["ms_date"]:
            raise RefundError("Проведённый документ МойСклад изменился. Нужна сверка перед отправкой чека.")
        if snapshot.get("provider")=="yandex_split":return await check_yandex_completed(service,record)
        refund=await service.yoo.get("/refunds/"+record["refund_id"])
        if refund.get("id")!=record["refund_id"] or refund.get("payment_id")!=record["payment_id"] or refund.get("status")!="succeeded" or (refund.get("amount") or {}).get("currency")!="RUB" or cents(refund["amount"]["value"])!=record["amount_cents"]:
            raise RefundError("ЮKassa не подтвердила существующий возврат. Публикация остановлена.")
    except RefundError as error:
        return service.store.update(record["id"],ms_synced=0,last_error=str(error))
    return service.store.update(record["id"],ms_synced=1,last_error=None)


def yandex_history(data,oid):
    from yandex_refunds import validate_order
    validate_order(data.get("order"),oid)
    if data["order"].get("paymentStatus") not in {"CAPTURED","PARTIALLY_REFUNDED","REFUNDED"}:
        raise RefundError("Яндекс не подтвердил оплаченное состояние заказа.")
    operations=data.get("operations")
    if not isinstance(operations,list) or not operations:raise RefundError("Не получена история Яндекс Пэй.")
    seen=set()
    for op in operations:
        try:
            valid=(op.get("orderId")==oid and str(uuid.UUID(op["operationId"]))==op["operationId"]
                and op["operationId"] not in seen and op.get("status") in {"PENDING","SUCCESS","FAIL"})
        except (TypeError,ValueError,KeyError,AttributeError):valid=False
        if not valid:raise RefundError("Не подтверждены операции Яндекс Пэй для этого заказа.")
        seen.add(op["operationId"])
    from yandex_refunds import original_paid_operation
    paid=original_paid_operation(data['order'],operations)
    total=cents(paid['amount'])
    refunds=[op for op in operations if op.get("operationType")=="REFUND" and op["status"] in {"PENDING","SUCCESS"}]
    returned=sum(cents(op["amount"]) for op in refunds if op["status"]=="SUCCESS")
    reserved=sum(cents(op["amount"]) for op in refunds)
    if reserved>total or any(cents(op["amount"])<=0 for op in refunds) or cents(data["order"]["orderAmount"]) not in {total,total-returned}:
        raise RefundError("Оплата и история возвратов Яндекса не сошлись по сумме.")
    return paid,refunds,total,reserved


def yandex_proof(data,op,contact):
    def stamp(value):
        try:
            parsed=datetime.fromisoformat(str(value).replace("Z","+00:00"))
            if parsed.tzinfo is None:raise ValueError
        except (ValueError,TypeError):raise RefundError("Яндекс ещё не передал дату существующей операции.") from None
        return parsed.isoformat()
    proof={"provider":"yandex_split","status":op["status"],"amount_cents":cents(op["amount"]),
        "order_id":op["orderId"],"operation_id":op["operationId"],"external_operation_id":op.get("externalOperationId"),
        "requested_at":stamp(op.get("created")),"order_created_at":data["order"].get("created"),
        "contact":next(iter(contact.values()))}
    from yandex_refunds import original_paid_operation
    proof['paid_at']=original_paid_operation(data['order'],data['operations']).get('created')
    if op["status"]=="SUCCESS":proof["completed_at"]=stamp(op.get("updated") or op.get("created"))
    if op["status"]=="FAIL":proof["failed_at"]=stamp(op.get("updated") or op.get("created"))
    return proof


async def preview_yandex_completed(service,record,doc,positions):
    from yandex_refunds import cart_items,matched_cart_items,decimal_amount
    oid=service.split.api.order_id(record["request"])
    data=await service.split.api.order(oid)
    auth,refunds,total,reserved=yandex_history(data,oid)
    if not refunds:raise RefundError("В МойСклад стоит «Возврат-проведен», но Яндекс не подтвердил ни проведённого возврата, ни операции в обработке. Повторная отправка денег остановлена.")
    r=record["request"]
    raw,selected=selected_document_items({**doc,"state":{"name":"Возврат"}},positions,r)
    details=await service.customer_details(record,doc)
    contact_text=str(data["order"].get("fiscalContact") or "")
    contact=valid_contact({"email" if "@" in contact_text else "phone":contact_text},details.get("receipt_contact"))
    if not contact:raise RefundError("Не найден контакт исходной оплаты Яндекса для документа.")
    old=record.get("snapshot") or {}
    cached=(old.get("hash")==approval_hash(old) and old.get("provider")=="yandex_split"
        and old.get("payment_id")=="yandex:"+oid and old.get("source_hash")==record["source_hash"]
        and old.get("order",{}).get("id")==doc["id"] and old.get("order",{}).get("items")==raw
        and old.get("payment_amount_cents")==total)
    matches=[]
    for op in refunds:
        rid="yandex:"+op["operationId"]
        wanted_id=r.get("refund_id") or (old.get("existing_refund") or {}).get("id")
        if wanted_id and wanted_id.removeprefix("yandex:")!=op["operationId"]:continue
        linked=cached and ((old.get("existing_refund") or {}).get("id")==rid or
            (record.get("attempted_at") and record.get("idempotence_key") and record["idempotence_key"]==op.get("externalOperationId")))
        # An external partial refund needs its original operation-bound snapshot.
        if not linked and not (len(refunds)==1 and cents(op["amount"])==total and r["scope"]=="all"):continue
        financial=None;gift_review=None;excluded=[]
        if cached and old.get("amount_cents")==cents(op["amount"]):
            financial=old.get("financial_items");gift_review=old.get("gift_review");excluded=old.get("excluded_gifts",[])
        if financial is None:
            cart=data["order"].get("cart") or {}
            try:
                original={**data["order"],"orderAmount":cart["total"]["amount"]}
                paid=cart_items(original)
                if cents(original["orderAmount"])!=total:continue
                items,excluded,gift_review=gift_checked_items(record,doc,raw,selected,
                    [{"name":i["title"],"quantity":i["quantity"],"unit_cents":i["unit_cents"]} for i in paid],"yandex:"+oid,sorted(x["operationId"] for x in refunds))
                financial=[{k:i[k] for k in ("name","quantity","unit_cents","delivery")} for i in matched_cart_items(original,items,r["scope"],excluded)]
            except GiftClarification:raise
            except (KeyError,TypeError,RefundError):continue
        if not financial or sum(i["quantity"]*i["unit_cents"] for i in financial)!=cents(op["amount"]):continue
        proof=yandex_proof(data,op,contact)
        matches.append((op,financial,proof,gift_review,excluded))
    if len(matches)!=1:raise RefundError("В МойСклад уже стоит «Возврат-проведен». Не удалось однозначно сопоставить существующую операцию Яндекса с исходными оплаченными товарами. Укажите refund_id и проверьте состав; новая операция не создаётся.")
    op,financial,proof,gift_review,excluded=matches[0]
    snapshot={"provider":"yandex_split","provider_order_id":oid,"payment_method":data["order"]["paymentMethod"]["methodType"],
        "order":{"id":doc["id"],"name":doc["name"],"state":doc["state"]["name"],"updated":doc.get("updated"),"items":raw},
        "payment_id":"yandex:"+oid,"amount_cents":cents(op["amount"]),"payment_amount_cents":total,"available_cents":total-reserved,
        "body":{"refundAmount":decimal_amount(cents(op["amount"]))},"financial_items":financial,
        "customer_hash":await service.customer(record,doc),"customer":{**details["customer"],**contact},"customer_warnings":details["warnings"],
        "receipt_contact":contact,"source_hash":record["source_hash"],"settings_hash":hashed(SETTINGS),
        "previous_refunds":sorted(x["operationId"] for x in refunds),
        "existing_refund":{"id":"yandex:"+op["operationId"],"status":"pending" if op["status"]=="PENDING" else "succeeded",
            "completed_at":proof.get("completed_at"),"ms_date":return_date(doc),"proof":proof}}
    if r.get("item_selector") or raw!=selected:snapshot["selected_items"]=selected
    if gift_review:snapshot.update(gift_review=gift_review,excluded_gifts=excluded)
    snapshot["hash"]=approval_hash(snapshot)
    return snapshot


async def check_yandex_completed(service,record):
    snapshot=record["snapshot"];existing=snapshot["existing_refund"]
    data=await service.split.api.order(service.split.identity(record))
    yandex_history(data,snapshot["provider_order_id"])
    matches=[op for op in data["operations"] if "yandex:"+op["operationId"]==record["refund_id"] and op.get("operationType")=="REFUND"]
    if len(matches)!=1 or cents(matches[0]["amount"])!=record["amount_cents"] or matches[0].get("externalOperationId")!=existing["proof"].get("external_operation_id"):
        raise RefundError("История Яндекса не подтвердила ранее выбранный возврат.")
    op=matches[0]
    if record["state"]=="succeeded" and op["status"]!="SUCCESS":raise RefundError("Яндекс изменил подтверждённый результат; нужна сверка.")
    proof=yandex_proof(data,op,snapshot["receipt_contact"])
    state={"PENDING":"pending","SUCCESS":"succeeded","FAIL":"failed"}[op["status"]]
    return service.store.update(record["id"],state=state,refund_status=op["status"],receipt_status={"PENDING":"external_pending","SUCCESS":"external","FAIL":"external_failed"}[op["status"]],
        receipt_json=proof,succeeded_at=proof.get("completed_at"),ms_synced=1,
        last_error="Яндекс не выполнил возврат; требуется сверка с владельцем." if state=="failed" else None)


def pending_completed_identity(service,record):
    snapshot=record.get("snapshot") or {};existing=snapshot.get("existing_refund") or {};proof=record.get("receipt_json") or {}
    oid=service.split.identity(record)
    if not (OWNER and record.get("approved_by")==OWNER and record.get("attempted_at") and record["state"]=="pending"
        and proof.get("status")=="PENDING" and proof.get("provider")=="yandex_split" and proof.get("order_id")==oid
        and record.get("refund_id")==existing.get("id")=="yandex:"+str(proof.get("operation_id"))
        and proof.get("external_operation_id")==existing.get("proof",{}).get("external_operation_id")
        and proof.get("amount_cents")==record["amount_cents"]==snapshot["amount_cents"]):
        raise RefundError("Не подтверждена привязка существующей операции Яндекса к одобрению владельца.")
    return oid
