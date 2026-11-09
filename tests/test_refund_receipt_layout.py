import copy
import unittest
from unittest.mock import patch

from refund_receipt_image import ReceiptCanvas, receipt_view, render_refund, font


class ReceiptLayoutTests(unittest.TestCase):
    def record(self, split=True):
        proof = {'amount_cents':1399000, 'contact':'buyer@example.test'}
        if split:
            proof.update(provider='yandex_split',status='SUCCESS',order_id='10000000:123456789',
                operation_id='verified-operation',completed_at='2026-10-20T19:00:00+03:00',
                order_created_at='2026-09-07T16:45:00+03:00',paid_at='2026-09-07T17:08:00+03:00')
        else:
            proof.update(id='registered-receipt',fiscal_provider_id='10001',fiscal_document_number='10002',
                fiscal_storage_number='10003',fiscal_attribute='10004')
        return {'receipt_json':proof,'request':{'order_number':'123456789','name':'Тестовый Покупатель',
            'email':'buyer@example.test','phone':'+79990000000'},'snapshot':{'payment_amount_cents':1464993,
            'order':{'items':[{'name':'DEMO PUFFER BLACK, Размер=XS','unit_cents':1399000,'quantity':1}]}}}

    def text(self, record):
        with patch.object(ReceiptCanvas,'finish',lambda page,*args:page.blocks):
            blocks=render_refund(record)
        return '\n'.join(block[3] for block in blocks if block[0]=='text')

    def test_partial_split_keeps_original_payment_and_actual_refund_separate(self):
        text=self.text(self.record())
        for value in ('Исходная оплата','Оплачен','14 649,93 ₽','13 990,00 ₽','Сумма возврата',
                      'Состав возврата','Возврат оформлен','DEMO PUFFER BLACK','Размер=XS',
                      'Тестовый Покупатель','buyer@example.test','+79990000000',
                      '7 сентября 2026 г. в 16:45','20 октября 2026 г. в 19:00'):
            self.assertIn(value,text)
        for value in ('Возвращено','Сформировано','Не является фискальным'):
            self.assertNotIn(value,text)

    def test_full_split_shows_cart_and_uses_only_its_customer(self):
        record=self.record()
        record['snapshot']['payment_amount_cents']=1399000
        record['request']['name']='Другой Покупатель'
        text=self.text(record)
        self.assertIn('Состав корзины',text)
        self.assertIn('Другой Покупатель',text)
        self.assertNotIn('Тестовый Покупатель',text)

    def test_yandex_card_confirmation_preserves_actual_payment_method_and_pending_status(self):
        record=self.record()
        record['snapshot']['payment_method']='CARD'
        record['receipt_json'].update(status='PENDING',requested_at='2026-10-23T12:00:00+03:00')
        text=self.text(record)
        self.assertIn('Яндекс Пэй · Карта',text)
        self.assertNotIn('Яндекс Пэй · Сплит',text)
        self.assertIn('Оплачен',text);self.assertIn('Возврат в процессе',text)
        self.assertNotIn('Возврат оформлен',text)

    def test_pending_and_failed_split_never_claim_completed_refund(self):
        for status,label,field in (('PENDING','Возврат в процессе','requested_at'),
                                   ('FAIL','Возврат не выполнен','failed_at')):
            record=self.record()
            record['receipt_json'].update(status=status,**{field:'2026-10-21T12:00:00+03:00'})
            text=self.text(record)
            self.assertIn(label,text)
            self.assertIn('Оплачен',text)
            self.assertIn('21 октября 2026 г. в 12:00',text)
            self.assertNotIn('Возврат оформлен',text)
            self.assertNotIn('20 октября 2026 г. в 19:00',text)

    def test_yookassa_preserves_registered_fiscal_fields_without_footer(self):
        text=self.text(self.record(split=False))
        for value in ('Чек возврата на 13 990,00 ₽','registered-receipt','10001','10002','10003','10004',
                      'Контакты для чека','buyer@example.test','Данные чека в облачной кассе'):
            self.assertIn(value,text)
        for value in ('Сформировано','Покупатель','Состав возврата','Размер=XS','Тестовый Покупатель','+79990000000'):
            self.assertNotIn(value,text)

    def test_missing_ruble_glyph_uses_another_font(self):
        from types import SimpleNamespace
        good=font(48)
        missing=SimpleNamespace(getmask=lambda value:good.getmask(chr(0x10ffff) if value=='₽' else value))
        with patch('refund_receipt_image.Path.is_file',return_value=True), \
             patch('refund_receipt_image.ImageFont.truetype',side_effect=[missing,good]) as load:
            self.assertIs(font(48),good)
        self.assertEqual(load.call_count,2)

    def test_broken_font_file_uses_another_font(self):
        good=font(48)
        with patch('refund_receipt_image.Path.is_file',return_value=True), \
             patch('refund_receipt_image.ImageFont.truetype',side_effect=[OSError('bad font'),good]):
            self.assertIs(font(48),good)

    def test_large_amount_fits_the_restored_header(self):
        from PIL import Image
        record=self.record(split=False)
        record['receipt_json']['amount_cents']=2917985
        self.assertIn('Чек возврата на 29 179,85 ₽',self.text(record))
        picture=render_refund(record)
        with Image.open(picture) as image:
            self.assertEqual(image.width,956)
            self.assertGreaterEqual(image.height,1360)
        picture.close()

    def test_display_data_does_not_mutate_the_financial_snapshot(self):
        record=self.record();before=copy.deepcopy(record)
        data=receipt_view(record)
        data['items'][0]['name']='changed preview'
        data['customer']['name']='changed preview'
        self.assertEqual(record,before)

    def test_receipt_uses_service_items_when_ms_prices_differ(self):
        record=self.record()
        record['snapshot']['order']['items'][0]['unit_cents']=1599000
        record['snapshot']['financial_items']=[{'name':'DEMO PUFFER BLACK, Размер=XS','quantity':1,'unit_cents':1399000}]
        text=self.text(record)
        self.assertIn('13 990,00 ₽',text)
        self.assertNotIn('15 990,00 ₽',text)

    def test_receipt_uses_customer_from_order_instead_of_typo_in_request(self):
        record=self.record()
        record['request'].update(name='Ошибочное ФИО',email='ошибка',phone='123')
        record['snapshot']['customer']={'name':'Покупатель МойСклад','email':'paid@example.test','phone':'+79991112233'}
        text=self.text(record)
        for value in ('Покупатель МойСклад','paid@example.test','+79991112233'):self.assertIn(value,text)
        self.assertNotIn('Ошибочное ФИО',text)

    def test_sbp_confirmation_displays_sbp_instead_of_split(self):
        record=self.record()
        record['snapshot']['payment_method']='SBP'
        text=self.text(record)
        self.assertIn('Яндекс Пэй · СБП',text)
        self.assertNotIn('Сплит',text)
