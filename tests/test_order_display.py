import unittest
from types import SimpleNamespace

from bot import order_for_display


class FakeDatabase:
    def __init__(self) -> None:
        self.order = {
            "id": 7,
            "order_number": "LM-7",
            "status": "paid",
            "delivery_method": "CDEK",
            "cdek_uuid": "",
            "cdek_number": "",
        }
        self.attempted = False

    def get_order(self, order_id):
        return dict(self.order) if order_id == 7 else None

    def mark_cdek_sync_attempt(self, order_id):
        self.attempted = order_id == 7

    def save_cdek_order_data(self, order_id, cdek_uuid, cdek_number, payload):
        self.order["cdek_uuid"] = cdek_uuid
        self.order["cdek_number"] = cdek_number
        return True


class FakeCdek:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    async def find_order_by_im_number(self, order_number):
        self.calls += 1
        return {"entity": {"uuid": "uuid-7", "cdek_number": "TRACK-7"}}


class OrderDisplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_track_is_loaded_before_card_render(self) -> None:
        database = FakeDatabase()
        client = FakeCdek()
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"database": database, "cdek": client}
            )
        )

        order = await order_for_display(context, 7)

        self.assertTrue(database.attempted)
        self.assertEqual(client.calls, 1)
        self.assertEqual(order["cdek_uuid"], "uuid-7")
        self.assertEqual(order["cdek_number"], "TRACK-7")

    async def test_existing_track_does_not_call_cdek_again(self) -> None:
        database = FakeDatabase()
        database.order["cdek_number"] = "TRACK-OLD"
        client = FakeCdek()
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"database": database, "cdek": client}
            )
        )

        order = await order_for_display(context, 7)

        self.assertFalse(database.attempted)
        self.assertEqual(client.calls, 0)
        self.assertEqual(order["cdek_number"], "TRACK-OLD")


if __name__ == "__main__":
    unittest.main()
