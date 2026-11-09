import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from integrations.tilda import (
    is_paid_tilda_payload,
    normalize_tilda_order,
    parse_tilda_created_at,
)


class TildaNormalizeTest(unittest.TestCase):
    def test_typical_order(self) -> None:
        order, items = normalize_tilda_order(
            {
                "tranid": "123:456",
                "orderid": "LM-777",
                "Name": "Анна",
                "Phone": "+79991112233",
                "Address": "Москва, ул. Тестовая",
                "amount": "12 500,00",
                "promocode": "WELCOME",
                "products": '[{"name":"Куртка","externalid":"JK-1","price":"12500","quantity":1,"options":[{"option":"Размер","variant":"M"}]}]',
            }
        )
        self.assertEqual(order["order_number"], "LM-777")
        self.assertEqual(order["total_kopecks"], 1_250_000)
        self.assertEqual(items[0]["size"], "M")
        self.assertEqual(items[0]["sku"], "JK-1")

    def test_flat_tilda_payment_fields(self) -> None:
        order, items = normalize_tilda_order(
            {
                "Name": "Тест CRM",
                "Phone": "+79990000000",
                "Email": "customer@example.com",
                "Ваш_username_в_Telegram": "@shopcrm",
                "payment[orderid]": "1010101008",
                "payment[amount]": "405.08",
                "payment[delivery_address]": "Москва, пункт выдачи",
                "payment[delivery_city]": "Москва",
                "payment[delivery_price]": "500.50",
                "payment[sys]": "yakassa",
                "payment[products][0][name]": "Худи",
                "payment[products][0][price]": "10",
                "payment[products][0][quantity]": "1",
                "payment[products][0][options][0][option]": "Размер",
                "payment[products][0][options][0][variant]": "XL",
            }
        )
        self.assertEqual(order["order_number"], "1010101008")
        self.assertEqual(order["total_kopecks"], 40_508)
        self.assertEqual(order["delivery_address"], "Москва, пункт выдачи")
        self.assertEqual(order["email"], "customer@example.com")
        self.assertEqual(order["delivery_city"], "Москва")
        self.assertEqual(order["delivery_cost_kopecks"], 50_050)
        self.assertEqual(order["payment_method"], "Полная оплата")
        self.assertEqual(order["telegram_username"], "@shopcrm")
        self.assertEqual(items[0]["product_name"], "Худи")
        self.assertEqual(items[0]["unit_price_kopecks"], 1_000)
        self.assertEqual(items[0]["size"], "XL")

    def test_paid_payload_detection(self) -> None:
        self.assertTrue(is_paid_tilda_payload({"payment[systranid]": "txn-1"}))
        self.assertTrue(is_paid_tilda_payload({"payment_status": "paid"}))
        self.assertFalse(is_paid_tilda_payload({"payment[orderid]": "100"}))

    def test_delivery_cost_falls_back_to_delivery_name(self) -> None:
        order, _ = normalize_tilda_order(
            {
                "orderid": "LM-DELIVERY",
                "order_delivery_name": "CDEK = 750,50",
            }
        )
        self.assertEqual(order["delivery_method"], "CDEK = 750,50")
        self.assertEqual(order["delivery_cost_kopecks"], 75_050)

    def test_tilda_created_time_uses_moscow_timezone(self) -> None:
        expected = int(
            datetime(
                2026, 8, 21, 14, 42, 21, tzinfo=ZoneInfo("Europe/Moscow")
            ).timestamp()
        )
        self.assertEqual(
            parse_tilda_created_at({"created": "2026-08-21 14:42:21"}),
            expected,
        )
        order, _ = normalize_tilda_order(
            {"orderid": "DATED-1", "created": "2026-08-21 14:42:21"}
        )
        self.assertEqual(order["created_at"], expected)


if __name__ == "__main__":
    unittest.main()
