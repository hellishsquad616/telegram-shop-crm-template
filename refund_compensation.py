"""Explicit price compensation on an original payment; the item stays with the buyer."""
import re
from decimal import Decimal


def parse_compensation(lines, request):
    from customer_refunds import cents, norm
    relevant = [line for line in lines if not re.match(r'(?:name|фио|имя|email|e-mail|почта|phone|телефон|товар|вещь|изделие)\s*:', line, re.I)]
    if not any(re.search(r'компенсац|ателье|разниц\w*\s+(?:в\s+)?цен|вещь\s+оста[её]тся', norm(line)) for line in relevant):
        return
    request['scope'] = 'compensation'
    amounts = set()
    for line in relevant:
        if not re.search(r'компенсац|ателье|разниц\w*\s+(?:в\s+)?цен|сумма\s+(?:возврата|компенсации)', norm(line)):
            continue
        if re.search(r'[:—–]\s*-\s*\d', line):
            continue
        match = re.search(r'(?<![\d.,-])(\d[\d \u00a0\u202f]*(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?|р\.?)(?:\s*[.!]?\s*)$', line, re.I)
        if match:
            amounts.add(cents(match[1]))
    if len(amounts) != 1 or min(amounts, default=0) < 100:
        request['compensation_error'] = 'Укажите одну точную сумму: «Компенсация клиенту — 3 150 ₽». Минимум 1 ₽.'
    else:
        request['compensation_cents'] = amounts.pop()


def compensation_document(order, positions, request):
    from customer_refunds import document_items, RefundError
    if request.get('compensation_error'):
        raise RefundError(request['compensation_error'])
    amount = request.get('compensation_cents')
    if not isinstance(amount, int) or amount < 100:
        raise RefundError('Не указана точная сумма компенсации.')
    items = document_items(order, positions, 'all')
    total = Decimal(str(order.get('sum', -1)))
    if any(i['delivery'] for i in items) or total != amount or sum(i['quantity']*i['unit_cents'] for i in items) != amount:
        raise RefundError('Для компенсации нужен отдельный документ МойСклад ровно на указанную сумму, без доставки и полной стоимости вещи.')
    return items, items


def compensation_target(request, document, paid_items):
    from customer_refunds import selected_document_items, names_match, RefundError
    selector = str(request.get('item_selector') or '').strip()
    if not selector:
        matches = [p for p in paid_items if not p.get('delivery') and any(names_match(d['name'],p['name']) for d in document)]
        if len(matches) != 1:
            raise RefundError('Для компенсации укажите «Товар: название исходной вещи, Размер=...». Позиция «Ателье» не заменяет название оплаченной вещи.')
        selector = matches[0]['name']
    positions = [{'id':str(i), 'quantity':p['quantity'], 'price':p['unit_cents'], 'assortment':{'name':p['name']}}
                 for i,p in enumerate(paid_items) if not p.get('delivery')]
    _, selected = selected_document_items({'state':{'name':'Возврат'}}, positions,
                                         {'scope':'part', 'item_selector':selector})
    return selected


def compensation_receipt(receipt, amount):
    from customer_refunds import cents, RefundError, money
    if len(receipt['items']) != 1 or Decimal(str(receipt['items'][0]['quantity'])) != 1:
        raise RefundError('Компенсация должна относиться к одной однозначной оплаченной вещи.')
    raw = receipt['items'][0]
    if not 100 <= amount < cents(raw['amount']['value']):
        raise RefundError('Компенсация должна быть меньше оставшейся оплаченной стоимости выбранной вещи и не меньше 1 ₽.')
    return {**receipt, 'items':[{**raw, 'quantity':'1', 'amount':money(amount)}]}


def compensation_yandex(order, request, document):
    from yandex_refunds import cart_items, decimal_amount, same_item
    from customer_refunds import RefundError, delivery
    paid = cart_items(order)
    target = compensation_target(request, document, [
        {'name':p['title'],'unit_cents':p['unit_cents'],'quantity':p['quantity'],'delivery':False}
        for p in paid if not delivery(p['title'])])[0]
    matches = [p for p in paid if same_item(target['name'], p['title'])]
    if len(matches) != 1 or matches[0]['quantity'] != 1:
        raise RefundError('Для компенсации Яндекса нужна одна единица однозначно выбранной позиции; несколько одинаковых вещей требуют отдельной сверки.')
    chosen = matches[0]
    amount = request['compensation_cents']
    if not 100 <= amount < chosen['unit_cents']:
        raise RefundError('Компенсация должна быть меньше оставшейся оплаченной стоимости выбранной вещи и не меньше 1 ₽.')
    body = {'refundAmount':decimal_amount(amount), 'targetCart':{'items':[
        {'productId':p['product_id'], **({'price':decimal_amount(p['unit_cents']-amount)} if p is chosen else {})}
        for p in paid]}}
    financial = [{'name':chosen['title'],'quantity':1,'unit_cents':amount,'delivery':False}]
    return financial, body
