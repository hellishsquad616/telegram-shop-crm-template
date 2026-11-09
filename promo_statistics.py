"""Read-only sales statistics for promo codes found in received orders."""
import re
from typing import Any, Dict, List, Optional
from sales_categories import MODEL_LABELS


PAID_PROMO_STATUSES = (
    'paid', 'processing', 'partially_returned', 'exchange', 'exchange_inbound', 'deposit',
)


def buyer_contacts(order: Dict[str, Any]) -> List[str]:
    """Normalize contact identifiers; never merge people by their names alone."""
    contacts = []
    phone = re.sub(r'\D', '', str(order.get('phone') or ''))
    if len(phone) == 11 and phone.startswith('8'):
        phone = '7' + phone[1:]
    if 7 <= len(phone) <= 15:
        contacts.append('phone:' + phone)
    email = str(order.get('email') or '').strip().casefold()
    if '@' in email:
        contacts.append('email:' + email)
    telegram = str(order.get('telegram_username') or '').strip().casefold()
    for prefix in ('https://t.me/', 'http://t.me/', 't.me/'):
        if telegram.startswith(prefix):
            telegram = telegram[len(prefix):]
            break
    telegram = telegram.lstrip('@').rstrip('/')
    if telegram and telegram not in {'-', '—', 'нет', 'none', 'null'} and ' ' not in telegram:
        contacts.append('telegram:' + telegram)
    return contacts


class PromoStatistics:
    def promocode_sales_report(
        self, start_at: Optional[int] = None, end_at: Optional[int] = None,
        code: Optional[str] = None, model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Count paid orders once; an optional model selects only its goods.

        Without a model, use final order totals including delivery. With a
        model, use active paid lines after allocating the order discount.

        Codes come from orders, not the managed-code table, so manually created
        and subsequently deleted Tilda codes keep their historical statistics.
        Contact links bridge an email-only repeat order to the same customer's
        order with both email and phone. The overall buyer total is deduplicated
        across codes rather than being the sum of each code's buyer count.
        """
        conditions = ["status IN (" + ','.join('?' for _ in PAID_PROMO_STATUSES) + ')',
                      "TRIM(COALESCE(promo_code, '')) <> ''"]
        parameters: List[Any] = list(PAID_PROMO_STATUSES)
        if start_at is not None:
            conditions.append('created_at >= ?')
            parameters.append(int(start_at))
        if end_at is not None:
            conditions.append('created_at < ?')
            parameters.append(int(end_at))
        if code is not None:
            if not str(code).strip():
                raise ValueError('Укажите промокод.')
            conditions.append('CASEFOLD(TRIM(promo_code)) = ?')
            parameters.append(str(code).strip().casefold())
        with self.connect() as connection:
            connection.execute('BEGIN')
            rows = [dict(row) for row in connection.execute(
                'SELECT id, promo_code, phone, email, telegram_username, total_kopecks, created_at '
                'FROM orders WHERE ' + ' AND '.join(conditions) + ' ORDER BY id', parameters,
            ).fetchall()]
            product_totals = None
            if model is not None:
                product_totals = self._promo_product_totals(connection, conditions, parameters, model)
                rows = [row for row in rows if row['id'] in product_totals['orders']]
            # Resolve identities against paid-order history so a code or date
            # filter cannot turn the same buyer into two different people.
            identity_rows = [dict(row) for row in connection.execute(
                'SELECT id, phone, email, telegram_username FROM orders WHERE status IN ('
                + ','.join('?' for _ in PAID_PROMO_STATUSES) + ')', PAID_PROMO_STATUSES,
            ).fetchall()]

        rows = [row for row in rows if str(row["promo_code"]).strip()]
        parents: Dict[str, str] = {}

        def find(token: str) -> str:
            parents.setdefault(token, token)
            root = token
            while parents[root] != root:
                root = parents[root]
            while parents[token] != token:
                following = parents[token]
                parents[token] = root
                token = following
            return root

        for row in identity_rows:
            contacts = buyer_contacts(row)
            if not contacts:
                contacts = [f"order:{row['id']}"]
            root = find(contacts[0])
            for contact in contacts[1:]:
                other = find(contact)
                parents[other] = root

        groups: Dict[str, Dict[str, Any]] = {}
        buyers = set()
        missing_contacts = 0
        for row in rows:
            contacts = buyer_contacts(row)
            if not contacts:
                contacts = [f"order:{row['id']}"]
                missing_contacts += 1
            normalized_code = str(row['promo_code']).strip().upper()
            if not normalized_code:
                continue
            group = groups.setdefault(normalized_code, {
                'code': normalized_code, 'orders_count': 0, 'amount_kopecks': 0, '_buyers': set(),
            })
            if product_totals is not None:
                group.setdefault('items_quantity', 0)
                group.setdefault('first_at', int(row['created_at']))
                group.setdefault('last_at', int(row['created_at']))
                group['first_at'] = min(group['first_at'], int(row['created_at']))
                group['last_at'] = max(group['last_at'], int(row['created_at']))
                quantity, revenue = product_totals['orders'][row['id']]
                group['items_quantity'] += quantity
            else:
                revenue = int(row['total_kopecks'] or 0)
            buyer = find(contacts[0])
            group['orders_count'] += 1
            group['amount_kopecks'] += revenue
            group['_buyers'].add(buyer)
            buyers.add(buyer)
        entries = []
        for group in groups.values():
            group['buyers_count'] = len(group.pop('_buyers'))
            entries.append(group)
        entries.sort(key=lambda row: (-row['orders_count'], -row['amount_kopecks'], row['code']))
        result = {
            'rows': entries,
            'buyers_count': len(buyers),
            'orders_count': sum(row['orders_count'] for row in entries),
            'amount_kopecks': sum(row['amount_kopecks'] for row in entries),
            'codes_count': len(entries),
            'orders_without_contacts': missing_contacts,
        }
        if product_totals is not None:
            result.update(
                items_quantity=sum(row['items_quantity'] for row in entries),
                first_at=min((row['first_at'] for row in entries), default=None),
                last_at=max((row['last_at'] for row in entries), default=None),
                models=product_totals['models'],
                model_label=product_totals['model_label'],
            )
        return result

    def _promo_product_totals(self, connection, conditions, parameters, model):
        """Classify a line once; allocate discounts before applying a model filter.

        Keep delivery and free gifts out of product sales. Distribute rounding
        cents by largest remainder, so separate models sum exactly to all goods.
        """
        categories = self.list_sales_categories()
        categories.sort(key=lambda row: (row['key'] not in MODEL_LABELS, row['id']))
        labels = {'m' + str(row['id']): MODEL_LABELS.get(row['key'], row['label']) for row in categories}
        labels['other'] = 'Другие изделия'
        if model != 'all' and model not in labels:
            raise ValueError('Модель скрыта или не найдена. Выберите модель заново.')
        eligible = 'SELECT id FROM orders WHERE ' + ' AND '.join(conditions)
        items = connection.execute(
            'SELECT i.*, o.discount_kopecks, o.created_at AS order_created_at, o.promo_code FROM order_items i '
            'JOIN orders o ON o.id=i.order_id WHERE i.item_status=\'active\' '
            'AND i.quantity>0 AND i.unit_price_kopecks>0 AND i.order_id IN (' + eligible + ') '
            'ORDER BY i.order_id, i.id', parameters,
        ).fetchall()
        orders = {}
        for item in items:
            if not str(item['promo_code']).strip():
                continue
            entry = dict(item)
            entry['model'] = 'other'
            for category in categories:
                matched = False
                for rule in category['rules']:
                    value = str(entry['sku'] if rule['kind'] == 'sku' else entry['product_name']).casefold()
                    target = rule['value'].casefold()
                    matches_rule = target in value if rule['kind'] == 'contains' else value == target
                    if matches_rule:
                        matched = True
                        break
                if matched:
                    entry['model'] = 'm' + str(category['id'])
                    break
            entry['gross'] = int(entry['quantity']) * int(entry['unit_price_kopecks'])
            orders.setdefault(entry['order_id'], []).append(entry)
        selected, models = {}, {}
        for order_id, lines in orders.items():
            gross = sum(line['gross'] for line in lines)
            discount = min(gross, max(0, int(lines[0]['discount_kopecks'] or 0)))
            for line in lines:
                line['discount'], line['remainder'] = divmod(discount * line['gross'], gross)
            remaining = discount - sum(line['discount'] for line in lines)
            for line in sorted(lines, key=lambda line: (-line['remainder'], line['id']))[:remaining]:
                line['discount'] += 1
            for line in lines:
                quantity, revenue = int(line['quantity']), line['gross'] - line['discount']
                key = line['model']
                group = models.setdefault(key, {'key': key, 'label': labels[key], 'items_quantity': 0,
                                               'amount_kopecks': 0, '_orders': set(), 'first_at': line['order_created_at']})
                group['items_quantity'] += quantity
                group['amount_kopecks'] += revenue
                group['_orders'].add(order_id)
                group['first_at'] = min(group['first_at'], line['order_created_at'])
                if model in ('all', key):
                    previous = selected.get(order_id, (0, 0))
                    selected[order_id] = (previous[0] + quantity, previous[1] + revenue)
        for group in models.values():
            group['orders_count'] = len(group.pop('_orders'))
        return {'orders': selected, 'models': sorted(models.values(), key=lambda row: (-row['items_quantity'], row['label'])),
                'model_label': labels.get(model, 'Все модели')}

    def promocode_statistics_codes(self, limit=10, offset=0):
        """Historical codes remain selectable even if never managed in this bot."""
        with self.connect() as connection:
            rows = connection.execute(
                'SELECT MIN(id) AS order_id, UPPER(TRIM(promo_code)) AS code FROM orders '
                'WHERE status IN (' + ','.join('?' for _ in PAID_PROMO_STATUSES) + ') '
                'AND TRIM(COALESCE(promo_code, \'\')) <> \'\' '
                'GROUP BY CASEFOLD(TRIM(promo_code)) ORDER BY CASEFOLD(TRIM(promo_code)) LIMIT ? OFFSET ?',
                [*PAID_PROMO_STATUSES, max(1, min(int(limit), 100)), max(0, int(offset))],
            ).fetchall()
        return [dict(row) for row in rows if str(row['code']).strip()]
