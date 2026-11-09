import tempfile
import unittest
from pathlib import Path

from database import Database


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.sqlite3")
        self.database.initialize()
        self.database.ensure_admins([100])
        self.order_id = self.database.upsert_order(
            {
                "order_number": "LM-001",
                "customer_name": "Иван Иванов",
                "phone": "+79990000000",
                "delivery_address": "Москва",
                "delivery_method": "CDEK = 500",
                "delivery_city": "Москва",
                "delivery_cost_kopecks": 50000,
                "payment_method": "Полная оплата",
                "total_kopecks": 2000000,
                "status": "paid",
                "source": "test",
            },
            [
                {
                    "product_name": "Худи",
                    "sku": "HD-1",
                    "size": "M",
                    "quantity": 1,
                    "unit_price_kopecks": 1000000,
                },
                {
                    "product_name": "Брюки",
                    "sku": "BR-1",
                    "size": "L",
                    "quantity": 1,
                    "unit_price_kopecks": 1000000,
                },
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_permissions_and_search(self) -> None:
        self.assertTrue(self.database.has_permission(100, "settings"))
        self.database.set_staff_role(200, "manager", 100)
        self.assertTrue(self.database.has_permission(200, "operations"))
        self.assertTrue(self.database.has_permission(200, "promocodes"))
        self.assertTrue(self.database.has_permission(200, "warehouse"))
        self.assertFalse(self.database.has_permission(200, "analytics"))
        self.assertFalse(self.database.has_permission(200, "settings"))
        self.assertEqual(self.database.search_orders("Иван")[0]["id"], self.order_id)

    def test_change_exchange_and_return_keep_history(self) -> None:
        order = self.database.get_order(self.order_id)
        first, second = order["items"]
        self.database.update_item_field(first["id"], "size", "XL", 100)
        exchange_id = self.database.create_exchange(
            self.order_id,
            {first["id"]: {"product_name": "Худи New", "size": "S"}},
            100,
            "Не подошёл размер",
        )
        self.assertGreater(exchange_id, 0)
        changed = self.database.get_order(self.order_id)
        self.assertEqual(len(changed["items"]), 3)
        self.assertEqual(
            next(item for item in changed["items"] if item["id"] == first["id"])["item_status"],
            "exchanged",
        )
        return_id = self.database.create_return(
            self.order_id, [second["id"]], 100, "Брак или повреждение"
        )
        self.assertGreater(return_id, 0)
        self.assertEqual(self.database.analytics()["exchanges_count"], 1)
        self.assertEqual(self.database.analytics()["returns_count"], 1)
        history = self.database.get_order(self.order_id)["operations"]
        self.assertEqual([row["kind"] for row in history], ["exchange", "return"])
        self.assertEqual(history[0]["reason"], "Не подошёл размер")
        self.assertEqual(history[0]["old_product"], "Худи")
        self.assertEqual(history[0]["new_product"], "Худи New")

    def test_consolidated_exchange_replaces_two_items_with_one(self) -> None:
        order = self.database.get_order(self.order_id)
        exchange_id = self.database.create_consolidated_exchange(
            self.order_id,
            [item["id"] for item in order["items"]],
            {"product_name": "DEMO PUFFER RED", "sku": "LPR1102S", "size": "S"},
            100,
            "Подтверждено накладной",
        )

        self.assertGreater(exchange_id, 0)
        refreshed = self.database.get_order(self.order_id)
        active = [item for item in refreshed["items"] if item["item_status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["product_name"], "DEMO PUFFER RED")
        self.assertEqual(active[0]["unit_price_kopecks"], 2000000)

    def test_status_summary_and_product_catalog(self) -> None:
        summary = self.database.status_summary("paid")
        self.assertEqual(summary["orders_count"], 1)
        self.assertEqual(summary["items_quantity"], 2)
        self.assertEqual(summary["items_amount"], 2_000_000)
        catalog = self.database.product_catalog()
        self.assertEqual(
            {entry["product_name"] for entry in catalog}, {"Худи", "Брюки"}
        )

    def test_sales_summary_splits_categories_and_excludes_delivery(self) -> None:
        mixed_id = self.database.upsert_order(
            {
                "order_number": "SALE-MIXED",
                "customer_name": "Покупатель",
                "status": "paid",
                "discount_kopecks": 1_000_000,
                "delivery_cost_kopecks": 50_000,
                "total_kopecks": 2_050_000,
                "created_at": 1_000,
            },
            [
                {
                    "product_name": "DEMO PUFFER BLACK", "size": "S",
                    "quantity": 1, "unit_price_kopecks": 1_200_000,
                },
                {
                    "product_name": '"By Mood" BASE Black', "size": "M",
                    "quantity": 1, "unit_price_kopecks": 800_000,
                },
            ],
        )
        puffer = self.database.sales_summary("puffer")
        # Historical rules remain available if explicitly re-enabled in settings.
        self.database.set_sales_category_active("longsleeve", True)
        longsleeve = self.database.sales_summary("longsleeve")
        all_sales = self.database.sales_summary("all")
        self.assertEqual(puffer["items_quantity"], 1)
        self.assertEqual(puffer["revenue_kopecks"], 600_000)
        self.assertEqual(longsleeve["items_quantity"], 1)
        self.assertEqual(longsleeve["revenue_kopecks"], 400_000)
        self.assertEqual(all_sales["revenue_kopecks"], 3_000_000)
        self.assertNotEqual(mixed_id, 0)
        historical = self.database.sales_summary("all", 900, 1_100)
        self.assertEqual(historical["orders_count"], 1)
        self.assertEqual(historical["revenue_kopecks"], 1_000_000)
        top_puffers = self.database.sales_top_items("puffer", limit=5)
        self.assertEqual(top_puffers[0]["product_name"], "DEMO PUFFER BLACK")
        self.assertEqual(top_puffers[0]["size"], "S")
        self.assertEqual(top_puffers[0]["quantity"], 1)
        self.assertEqual(top_puffers[0]["revenue_kopecks"], 600_000)

    def test_apply_reviewed_order_draft_atomically(self) -> None:
        self.assertEqual(self.database.confirmed_cdek_change_fields(self.order_id), frozenset())
        order = self.database.get_order(self.order_id)
        first_item = order["items"][0]
        self.database.apply_order_changes(
            self.order_id,
            {"email": "new@example.com", "phone": "+79991112233"},
            {str(first_item["id"]): {"size": "XL", "product_name": "HOODIE BLACK"}},
            100,
        )
        changed = self.database.get_order(self.order_id)
        self.assertEqual(changed["email"], "new@example.com")
        self.assertEqual(changed["phone"], "+79991112233")
        self.assertEqual(changed["items"][0]["size"], "XL")
        self.assertEqual(changed["items"][0]["product_name"], "HOODIE BLACK")
        self.assertEqual(
            self.database.confirmed_cdek_change_fields(self.order_id),
            frozenset({"email", "phone", "items"}),
        )

    def test_address_change_is_dirty_until_cdek_recreated(self) -> None:
        self.database.update_order_field(
            self.order_id, "delivery_address", "Балашиха, Яганова 3", 100
        )
        dirty = self.database.get_order(self.order_id)
        self.assertEqual(dirty["cdek_address_dirty"], 1)
        self.assertIn("address", self.database.confirmed_cdek_change_fields(self.order_id))
        self.database.save_recreated_cdek_order(
            self.order_id,
            {
                "entity": {
                    "uuid": "new-uuid",
                    "cdek_number": "NEW-TRACK",
                }
            },
            "ул. Яганова, 12",
            "Балашиха",
            100,
        )
        recreated = self.database.get_order(self.order_id)
        self.assertEqual(recreated["cdek_address_dirty"], 0)
        self.assertEqual(recreated["delivery_address"], "ул. Яганова, 12")
        self.assertEqual(recreated["delivery_city"], "Балашиха")
        self.assertEqual(recreated["cdek_number"], "NEW-TRACK")

    def test_verified_cdek_patch_can_clear_address_dirty_marker(self) -> None:
        order_id = self.database.upsert_order(
            {"order_number": "address-patch", "status": "paid"},
            [{"product_name": "A", "size": "S"}],
        )
        self.database.update_order_field(
            order_id, "delivery_address", "Москва, Тверская, 1", 1
        )
        self.assertEqual(self.database.get_order(order_id)["cdek_address_dirty"], 1)

        self.database.mark_cdek_address_synced(order_id)

        self.assertEqual(self.database.get_order(order_id)["cdek_address_dirty"], 0)

    def test_manual_item_addition_is_idempotent_and_audited(self) -> None:
        item_id, created = self.database.add_order_item(
            self.order_id, "DEMO SCARF BLACK", "GIFT-SCARF-BLACK", "", 1, 0, 100
        )
        repeated_id, repeated = self.database.add_order_item(
            self.order_id, "DEMO SCARF BLACK", "GIFT-SCARF-BLACK", "", 1, 0, 100
        )

        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(repeated_id, item_id)
        scarf = next(
            item
            for item in self.database.get_order(self.order_id)["items"]
            if item["id"] == item_id
        )
        self.assertEqual(scarf["quantity"], 1)
        self.assertEqual(scarf["unit_price_kopecks"], 0)
        with self.database.connect() as connection:
            event_types = [
                row["event_type"]
                for row in connection.execute(
                    "SELECT event_type FROM order_events WHERE order_id=?",
                    (self.order_id,),
                ).fetchall()
            ]
        self.assertIn("item_added", event_types)

    def test_repeated_source_webhook_is_idempotent_for_items(self) -> None:
        before = len(self.database.get_order(self.order_id)["items"])
        self.database.upsert_order(
            {"order_number": "LM-001", "customer_name": "Иван Иванов", "source": "test"},
            [{"product_name": "Повтор", "quantity": 1}],
        )
        after = len(self.database.get_order(self.order_id)["items"])
        self.assertEqual(before, after)

    def test_source_upsert_preserves_manual_fields_and_detects_reversion(self) -> None:
        order = self.database.get_order(self.order_id)
        first = order["items"][0]
        self.database.update_order_field(
            self.order_id, "customer_name", "Исправленное имя", 100
        )
        self.database.update_item_field(first["id"], "size", "XL", 100)
        self.database.upsert_order(
            {
                "order_number": "LM-001",
                "customer_name": "Старое имя из Tilda",
                "phone": "+70000000000",
                "status": "paid",
                "source": "tilda",
            },
            [{"product_name": "Худи", "size": "M", "quantity": 1}],
        )
        saved = self.database.get_order(self.order_id)
        self.assertEqual(saved["customer_name"], "Исправленное имя")
        self.assertEqual(saved["items"][0]["size"], "XL")
        self.assertEqual(self.database.reverted_manual_changes(self.order_id), [])
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE orders SET customer_name='Случайно возвращено' WHERE id=?",
                (self.order_id,),
            )
        issues = self.database.reverted_manual_changes(self.order_id)
        self.assertEqual(issues[0]["field"], "customer_name")
        self.assertEqual(issues[0]["expected"], "Исправленное имя")

    def test_promocode_usage_notification_is_sent_once(self) -> None:
        promocode_id = self.database.create_promocode(
            code="CHANNEL10",
            discount_type="percent",
            discount_value="10",
            usage_limit=10,
            expires_on="",
            description="channel",
            created_by=100,
            source_chat_id=-1001,
            source_message_id=77,
        )
        self.database.activate_promocode(promocode_id, 100, "remote-1")
        self.database.upsert_order(
            {
                "order_number": "PROMO-ORDER",
                "customer_name": "Клиент",
                "promo_code": "channel10",
                "status": "paid",
                "source": "tilda",
            },
            [{"product_name": "Худи", "size": "M"}],
        )
        pending = self.database.pending_promocode_notifications()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source_message_id"], 77)
        self.database.mark_promocode_notification(
            pending[0]["promocode_id"], pending[0]["order_id"]
        )
        self.assertEqual(self.database.pending_promocode_notifications(), [])
        order = self.database.get_order(self.order_id)
        self.assertEqual(order["delivery_method"], "CDEK = 500")
        self.assertEqual(order["delivery_city"], "Москва")
        self.assertEqual(order["delivery_cost_kopecks"], 50000)

    def test_source_timestamp_corrects_existing_order_date(self) -> None:
        exact_timestamp = 1_787_312_541
        self.database.upsert_order(
            {
                "order_number": "LM-001",
                "customer_name": "Иван Иванов",
                "phone": "+79990000000",
                "delivery_address": "Москва",
                "delivery_method": "CDEK = 500",
                "delivery_city": "Москва",
                "delivery_cost_kopecks": 50000,
                "payment_method": "Полная оплата",
                "total_kopecks": 2000000,
                "status": "paid",
                "source": "tilda",
                "created_at": exact_timestamp,
            },
            [],
        )
        self.assertEqual(
            self.database.get_order(self.order_id)["created_at"], exact_timestamp
        )

    def test_save_cdek_track(self) -> None:
        candidates = self.database.list_orders_for_cdek_sync()
        self.assertEqual(candidates[0]["id"], self.order_id)
        is_new = self.database.save_cdek_order_data(
            self.order_id,
            "cdek-uuid",
            "1234567890",
            {"entity": {"uuid": "cdek-uuid", "cdek_number": "1234567890"}},
        )
        self.assertTrue(is_new)
        order = self.database.get_order(self.order_id)
        self.assertEqual(order["cdek_uuid"], "cdek-uuid")
        self.assertEqual(order["cdek_number"], "1234567890")
        self.assertFalse(
            self.database.save_cdek_order_data(
                self.order_id, "cdek-uuid", "1234567890", {"entity": {}}
            )
        )
        self.assertEqual(self.database.list_orders_for_cdek_sync(), [])

    def test_order_statuses(self) -> None:
        self.database.set_order_status(self.order_id, "paid", 100)
        self.assertEqual(self.database.get_order(self.order_id)["status"], "paid")

    def test_order_period_summary(self) -> None:
        period_id = self.database.upsert_order(
            {
                "order_number": "PERIOD-1",
                "status": "paid",
                "total_kopecks": 345600,
                "created_at": 1000,
            },
            [],
        )
        result = self.database.order_period_summary(900, 1100)
        self.assertEqual(result["orders_count"], 1)
        self.assertEqual(result["orders_amount"], 345600)
        self.assertEqual(result["statuses"][0]["status"], "paid")
        exported = self.database.orders_for_export([period_id])
        self.assertEqual(exported[0]["order_number"], "PERIOD-1")
        self.assertEqual(exported[0]["items"], [])

    def test_bulk_status_by_numbers_and_product_filter(self) -> None:
        by_number = self.database.orders_by_numbers(["LM-001", "MISSING"])
        self.assertEqual([row["order_number"] for row in by_number], ["LM-001"])
        by_product = self.database.orders_by_product_query("Худи M")
        self.assertEqual([row["id"] for row in by_product], [self.order_id])
        changed = self.database.bulk_set_order_status(
            [self.order_id, self.order_id], "processing", 100
        )
        self.assertEqual(changed, 1)
        self.assertEqual(
            self.database.get_order(self.order_id)["status"], "processing"
        )
        self.assertEqual(
            self.database.order_ids_by_status("processing"), [self.order_id]
        )
        self.assertEqual(
            self.database.bulk_set_order_status(
                [self.order_id], "processing", 100
            ),
            0,
        )
        with self.assertRaises(ValueError):
            self.database.set_order_status(self.order_id, "unknown", 100)

    def test_cdek_sync_skips_pickup(self) -> None:
        self.database.upsert_order(
            {
                "order_number": "PICKUP-1",
                "status": "paid",
                "delivery_method": "Самовывоз = 0",
                "source": "test",
            },
            [],
        )
        candidates = self.database.list_orders_for_cdek_sync(limit=20)
        self.assertEqual([row["order_number"] for row in candidates], ["LM-001"])

    def test_analytics_delivery_cities_and_top_items(self) -> None:
        self.database.upsert_order(
            {
                "order_number": "LM-002",
                "customer_name": "Анна Смирнова",
                "phone": "+79990000001",
                "delivery_method": "Самовывоз = 0",
                "delivery_city": "Санкт-Петербург",
                "total_kopecks": 1000000,
                "status": "paid",
                "source": "test",
            },
            [
                {
                    "product_name": "Худи",
                    "size": "M",
                    "quantity": 2,
                    "unit_price_kopecks": 500000,
                }
            ],
        )
        result = self.database.analytics()
        self.assertEqual(result["orders_count"], 2)
        self.assertEqual(result["known_city_count"], 2)
        self.assertEqual(result["top_cities"][0]["city"], "Москва")
        self.assertEqual(result["delivery_count"], 1)
        self.assertEqual(result["pickup_count"], 1)
        self.assertEqual(result["cdek_delivery_amount"], 50000)
        self.assertEqual(result["top_items"][0]["product_name"], "Худи")
        self.assertEqual(result["top_items"][0]["quantity"], 3)
        self.assertCountEqual(result["customer_names"], ["Иван Иванов", "Анна Смирнова"])

    def test_cancellation_marks_items_and_removes_track(self) -> None:
        self.database.save_cdek_order_data(
            self.order_id,
            "cdek-uuid",
            "TRACK-1",
            {"entity": {"uuid": "cdek-uuid", "cdek_number": "TRACK-1"}},
        )
        first_item = self.database.get_order(self.order_id)["items"][0]
        result = self.database.create_cancellation(
            self.order_id, [first_item["id"]], True, 100
        )
        order = self.database.get_order(self.order_id)
        self.assertEqual(result["items_amount_kopecks"], 1000000)
        self.assertEqual(result["delivery_amount_kopecks"], 50000)
        self.assertEqual(result["total_amount_kopecks"], 1050000)
        self.assertEqual(order["status"], "cancelled")
        self.assertIsNone(order["cdek_uuid"])
        self.assertIsNone(order["cdek_number"])
        self.assertEqual(order["items"][0]["item_status"], "cancelled")
        self.assertEqual(order["items"][1]["item_status"], "active")
        self.assertEqual(
            self.database.search_orders("LM-001", status="cancelled")[0]["id"],
            self.order_id,
        )
        self.assertEqual(
            self.database.list_orders_by_status("cancelled")[0]["id"], self.order_id
        )

    def test_cancellation_can_record_exact_paid_refund_amount(self) -> None:
        items = self.database.get_order(self.order_id)["items"]
        result = self.database.create_cancellation(
            self.order_id,
            [item["id"] for item in items],
            True,
            100,
            "Полный возврат оплаты",
            1850000,
        )

        self.assertEqual(result["delivery_amount_kopecks"], 50000)
        self.assertEqual(result["items_amount_kopecks"], 1800000)
        self.assertEqual(result["total_amount_kopecks"], 1850000)

    def test_source_upsert_does_not_restore_cancelled_status(self) -> None:
        self.database.set_order_status(self.order_id, "cancelled", 100)
        self.database.upsert_order(
            {
                "order_number": "LM-001",
                "customer_name": "Иван Иванов",
                "status": "paid",
                "source": "tilda",
            },
            [],
        )
        self.assertEqual(self.database.get_order(self.order_id)["status"], "cancelled")

    def test_email_settings_and_change_request_queue(self) -> None:
        self.database.update_order_field(
            self.order_id, "email", "customer@example.com", 100
        )
        self.assertEqual(
            self.database.get_order(self.order_id)["email"], "customer@example.com"
        )
        self.database.set_setting("changes_chat_id", "-100123")
        self.assertEqual(self.database.get_setting("changes_chat_id"), "-100123")
        request_id, created = self.database.create_change_request(
            dedupe_key="unique-change",
            order_id=self.order_id,
            order_number="LM-001",
            action="email",
            payload={"old_email": "", "new_email": "customer@example.com"},
            raw_text="#LM-001 изменить почту",
            status="pending",
            source_chat_id=-100123,
            source_message_id=10,
            requested_by=100,
        )
        duplicate_id, duplicate_created = self.database.create_change_request(
            dedupe_key="unique-change",
            order_id=self.order_id,
            order_number="LM-001",
            action="email",
            payload={},
            raw_text="duplicate",
            status="pending",
            source_chat_id=-100123,
            source_message_id=11,
            requested_by=100,
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(request_id, duplicate_id)
        self.assertEqual(
            self.database.list_open_change_requests()[0]["id"], request_id
        )
        self.assertTrue(self.database.claim_change_request(request_id, 100))
        self.assertFalse(self.database.claim_change_request(request_id, 100))
        self.database.finish_change_request(request_id, "approved", 100, "ok")
        request = self.database.get_change_request(request_id)
        self.assertEqual(request["status"], "approved")
        self.assertEqual(request["payload"]["new_email"], "customer@example.com")
        self.assertEqual(self.database.list_open_change_requests(), [])

    def test_promocode_lifecycle_keeps_history_and_usage(self) -> None:
        promocode_id = self.database.create_promocode(
            code="demo10",
            discount_type="percent",
            discount_value="10",
            usage_limit=25,
            expires_on="2027-12-31",
            description="Новогодняя акция",
            created_by=100,
        )
        pending = self.database.get_promocode(promocode_id)
        self.assertEqual(pending["code"], "DEMO10")
        self.assertEqual(pending["status"], "pending_tilda")
        with self.assertRaises(ValueError):
            self.database.create_promocode(
                code="demo10",
                discount_type="fixed",
                discount_value="500",
                usage_limit=0,
                expires_on="",
                description="duplicate",
                created_by=100,
            )

        self.database.activate_promocode(promocode_id, 100, "21899999")
        self.database.upsert_order(
            {
                "order_number": "LM-001",
                "customer_name": "Иван Иванов",
                "promo_code": "demo10",
                "status": "paid",
                "source": "tilda",
            },
            [],
        )
        active = self.database.get_promocode(promocode_id)
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["tilda_id"], "21899999")
        self.assertEqual(active["paid_orders_count"], 1)

        self.database.delete_promocode(promocode_id, 100)
        deleted = self.database.get_promocode(promocode_id)
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(self.database.list_promocodes()[0]["id"], promocode_id)
        replacement_id = self.database.create_promocode(
            code="DEMO10",
            discount_type="fixed",
            discount_value="500",
            usage_limit=0,
            expires_on="",
            description="Новая версия",
            created_by=100,
        )
        self.assertNotEqual(replacement_id, promocode_id)
        self.assertEqual(self.database.promocode_counts()["deleted"], 1)
        self.database.fail_promocode_sync(replacement_id, "temporary error")
        failed = self.database.get_promocode(replacement_id)
        self.assertEqual(failed["status"], "sync_failed")
        self.assertEqual(failed["sync_error"], "temporary error")
        self.assertEqual(self.database.promocode_counts()["sync_failed"], 1)

    def test_change_audit_run_and_issue_history(self) -> None:
        since = self.database.change_audit_scope_since()
        candidates = self.database.list_change_audit_order_ids(since)
        self.assertIn(self.order_id, candidates)
        run_id = self.database.create_change_audit_run(100, since)
        issue_id = self.database.add_change_audit_issue(
            run_id,
            self.order_id,
            "LM-001",
            "cdek_data_mismatch",
            ["phone", "items"],
            "patch",
            "test mismatch",
        )
        self.database.finish_change_audit_run(
            run_id,
            1,
            {
                "issues_count": 1,
                "auto_fixable_count": 1,
                "manual_count": 0,
                "order_ids": [self.order_id],
            },
        )
        run = self.database.get_change_audit_run(run_id)
        self.assertEqual(run["status"], "needs_action")
        self.assertEqual(run["summary"]["order_ids"], [self.order_id])
        issue = self.database.list_change_audit_issues(run_id)[0]
        self.assertEqual(issue["fields"], ["phone", "items"])
        self.database.resolve_change_audit_issue(issue_id, "fixed", "ok")
        self.assertEqual(
            self.database.list_change_audit_issues(run_id)[0]["status"], "fixed"
        )

    def test_records_latest_source_reconciliation_by_field(self) -> None:
        self.database.record_cdek_source_reconciliation(
            self.order_id,
            "recipient_name",
            "crm-old",
            "cdek-old",
            "tilda_live",
            "recipient verified",
            100,
        )
        self.database.record_cdek_source_reconciliation(
            self.order_id,
            "recipient_name",
            "crm-new",
            "cdek-new",
            "tilda_live",
            "recipient verified again",
            100,
        )
        rows = self.database.cdek_source_reconciliations(self.order_id)
        self.assertEqual(rows["recipient_name"]["crm_fingerprint"], "crm-new")
        self.assertEqual(rows["recipient_name"]["cdek_fingerprint"], "cdek-new")

    def test_social_followers_daily_snapshot_is_upserted(self) -> None:
        self.database.save_social_followers(
            "telegram", "2026-09-06", 100, "telegram_bot_api", 10
        )
        self.database.save_social_followers(
            "telegram", "2026-09-07", 109, "telegram_bot_api", 20
        )
        self.database.save_social_followers(
            "telegram", "2026-09-07", 110, "telegram_bot_api", 30
        )
        today = self.database.social_followers_on("telegram", "2026-09-07")
        self.assertEqual(today["followers_count"], 110)
        self.assertEqual(today["captured_at"], 30)
        history = self.database.social_followers_history(
            "telegram", "2026-09-07"
        )
        self.assertEqual(
            [row["followers_count"] for row in history], [110, 100]
        )


if __name__ == "__main__":
    unittest.main()
