import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

from database import Database


class FakeRequest:
    headers = {'content-type': 'application/json'}

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class StatisticsWebhookTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'webhook.sqlite3'
        with patch.dict(os.environ, {'CRM_DB_PATH': str(self.path)}):
            self.webhook = importlib.import_module('webhook')
        self.database = Database(self.path)
        self.database.initialize()
        self.original_database = self.webhook.database
        self.webhook.database = self.database
        self.environment = patch.dict(os.environ, {'TILDA_WEBHOOK_SECRET': 'test-secret'})
        self.environment.start()

    def tearDown(self):
        self.webhook.database = self.original_database
        self.environment.stop()
        self.temp.cleanup()

    async def test_tilda_windbreaker_reaches_reports_once_without_stock_movements(self):
        payload = {'payment': {'orderid': 'WEB1', 'status': 'paid', 'products': [
            {'name': 'DEMO WINDBREAKER BLACK', 'externalid': 'WB-001', 'quantity': 2, 'price': 100,
             'options': [{'option': 'Размер', 'variant': 'M'}]},
        ]}}
        first = await self.webhook.tilda_webhook('test-secret', FakeRequest(payload))
        second = await self.webhook.tilda_webhook('test-secret', FakeRequest(payload))
        self.assertEqual(first['order_id'], second['order_id'])
        self.assertNotIn('stock', first)
        self.assertEqual(self.database.sales_summary('windbreaker')['items_quantity'], 2)
        order = self.database.get_order(first['order_id'])
        self.assertEqual(order['items'][0]['sku'], 'WB-001')
        self.assertEqual(order['items'][0]['size'], 'M')
        with self.database.connect() as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='warehouse_movements'").fetchone())

    async def test_secret_and_paid_checks_are_preserved(self):
        request = FakeRequest({'payment': {'orderid': 'UNPAID', 'products': [{'name': 'Ветровка'}]}})
        with self.assertRaises(HTTPException) as caught:
            await self.webhook.tilda_webhook('wrong-secret', request)
        self.assertEqual(caught.exception.status_code, 403)
        response = await self.webhook.tilda_webhook('test-secret', request)
        self.assertEqual(response['ignored'], 'not_paid')
        self.assertEqual(self.database.sales_summary('all')['orders_count'], 0)
        self.assertIsNone(self.database.get_order_by_number('UNPAID'))


if __name__ == '__main__':
    unittest.main()
