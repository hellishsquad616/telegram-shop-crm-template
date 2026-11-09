import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence


from sales_categories import SalesCategories
from promo_statistics import PromoStatistics


ALL_PERMISSIONS = (
    "orders", "operations", "analytics", "settings", "promocodes", "warehouse",
    "check_order", "staff",
)
ORDER_STATUSES = (
    "new",
    "paid",
    "processing",
    "partially_returned",
    "cancelled",
    "returned",
    "exchange",
    "exchange_inbound",
    "deposit",
    "refund_completed",
)


class Database(SalesCategories, PromoStatistics):
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.create_function("CASEFOLD", 1, lambda value: str(value or "").casefold(), deterministic=True)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)

    def health_report(self) -> Dict[str, Any]:
        """Return non-destructive storage diagnostics for the settings screen."""
        with self.connect() as connection:
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in ("orders", "order_items", "order_events", "change_requests")
            }
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        return {
            "integrity": integrity,
            "size_bytes": page_count * page_size,
            **counts,
        }

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS staff (
                    telegram_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT 'manager',
                    can_orders INTEGER NOT NULL DEFAULT 1,
                    can_operations INTEGER NOT NULL DEFAULT 1,
                    can_analytics INTEGER NOT NULL DEFAULT 0,
                    can_settings INTEGER NOT NULL DEFAULT 0,
                    can_promocodes INTEGER NOT NULL DEFAULT 0,
                    can_warehouse INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    added_by INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_number TEXT NOT NULL UNIQUE,
                    customer_name TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    telegram_username TEXT NOT NULL DEFAULT '',
                    delivery_address TEXT NOT NULL DEFAULT '',
                    delivery_method TEXT NOT NULL DEFAULT '',
                    delivery_city TEXT NOT NULL DEFAULT '',
                    delivery_cost_kopecks INTEGER NOT NULL DEFAULT 0,
                    payment_method TEXT NOT NULL DEFAULT '',
                    promo_code TEXT NOT NULL DEFAULT '',
                    discount_kopecks INTEGER NOT NULL DEFAULT 0,
                    total_kopecks INTEGER NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    status TEXT NOT NULL DEFAULT 'new',
                    source TEXT NOT NULL DEFAULT 'manual',
                    tilda_tranid TEXT,
                    cdek_uuid TEXT,
                    cdek_number TEXT,
                    cdek_last_sync_at INTEGER NOT NULL DEFAULT 0,
                    cdek_address_dirty INTEGER NOT NULL DEFAULT 0,
                    shipment_hold_until TEXT NOT NULL DEFAULT '',
                    cdek_payload_json TEXT NOT NULL DEFAULT '{}',
                    raw_source_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_orders_customer_name
                ON orders(customer_name);
                CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(phone);
                CREATE INDEX IF NOT EXISTS idx_orders_tilda_tranid
                ON orders(tilda_tranid);

                CREATE TABLE IF NOT EXISTS social_follower_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    followers_count INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    captured_at INTEGER NOT NULL,
                    UNIQUE(platform, snapshot_date)
                );

                CREATE INDEX IF NOT EXISTS idx_social_followers_platform_date
                ON social_follower_snapshots(platform, snapshot_date);

                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    sku TEXT NOT NULL DEFAULT '',
                    size TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    unit_price_kopecks INTEGER NOT NULL DEFAULT 0,
                    item_status TEXT NOT NULL DEFAULT 'active',
                    original_item_id INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
                    FOREIGN KEY(original_item_id) REFERENCES order_items(id)
                );

                CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_telegram_id INTEGER,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS order_field_overrides (
                    order_id INTEGER NOT NULL,
                    field TEXT NOT NULL,
                    actor_telegram_id INTEGER,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(order_id, field),
                    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS exchanges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created',
                    amount_kopecks INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    cdek_return_uuid TEXT,
                    created_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                );

                CREATE TABLE IF NOT EXISTS exchange_items (
                    exchange_id INTEGER NOT NULL,
                    old_item_id INTEGER NOT NULL,
                    new_item_id INTEGER NOT NULL,
                    PRIMARY KEY(exchange_id, old_item_id),
                    FOREIGN KEY(exchange_id) REFERENCES exchanges(id) ON DELETE CASCADE,
                    FOREIGN KEY(old_item_id) REFERENCES order_items(id),
                    FOREIGN KEY(new_item_id) REFERENCES order_items(id)
                );

                CREATE TABLE IF NOT EXISTS returns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created',
                    amount_kopecks INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    cdek_return_uuid TEXT,
                    created_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                );

                CREATE TABLE IF NOT EXISTS return_items (
                    return_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    amount_kopecks INTEGER NOT NULL,
                    PRIMARY KEY(return_id, item_id),
                    FOREIGN KEY(return_id) REFERENCES returns(id) ON DELETE CASCADE,
                    FOREIGN KEY(item_id) REFERENCES order_items(id)
                );

                CREATE TABLE IF NOT EXISTS cancellations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    items_amount_kopecks INTEGER NOT NULL DEFAULT 0,
                    delivery_amount_kopecks INTEGER NOT NULL DEFAULT 0,
                    total_amount_kopecks INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    created_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                );

                CREATE TABLE IF NOT EXISTS cancellation_items (
                    cancellation_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    amount_kopecks INTEGER NOT NULL,
                    PRIMARY KEY(cancellation_id, item_id),
                    FOREIGN KEY(cancellation_id) REFERENCES cancellations(id) ON DELETE CASCADE,
                    FOREIGN KEY(item_id) REFERENCES order_items(id)
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS change_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    order_id INTEGER,
                    order_number TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    raw_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    source_chat_id INTEGER,
                    source_message_id INTEGER,
                    requested_by INTEGER,
                    reviewed_by INTEGER,
                    result_text TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    reviewed_at INTEGER,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                );

                CREATE INDEX IF NOT EXISTS idx_change_requests_status
                ON change_requests(status, created_at);

                CREATE TABLE IF NOT EXISTS promocodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    discount_type TEXT NOT NULL,
                    discount_value TEXT NOT NULL,
                    usage_limit INTEGER NOT NULL DEFAULT 0,
                    expires_on TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending_tilda',
                    sync_error TEXT NOT NULL DEFAULT '',
                    tilda_id TEXT NOT NULL DEFAULT '',
                    created_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    activated_by INTEGER,
                    activated_at INTEGER,
                    deleted_by INTEGER,
                    deleted_at INTEGER,
                    source_chat_id INTEGER,
                    source_message_id INTEGER
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_promocodes_live_code
                ON promocodes(code COLLATE NOCASE)
                WHERE status <> 'deleted';

                CREATE INDEX IF NOT EXISTS idx_promocodes_history
                ON promocodes(created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS promocode_notifications (
                    promocode_id INTEGER NOT NULL,
                    order_id INTEGER NOT NULL,
                    notified_at INTEGER NOT NULL,
                    PRIMARY KEY(promocode_id, order_id),
                    FOREIGN KEY(promocode_id) REFERENCES promocodes(id) ON DELETE CASCADE,
                    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS change_audit_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_by INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    scope_since INTEGER NOT NULL,
                    scanned_orders INTEGER NOT NULL DEFAULT 0,
                    issues_count INTEGER NOT NULL DEFAULT 0,
                    auto_fixable_count INTEGER NOT NULL DEFAULT 0,
                    manual_count INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    finished_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS change_audit_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    order_id INTEGER NOT NULL,
                    order_number TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    fields_json TEXT NOT NULL DEFAULT '[]',
                    fix_mode TEXT NOT NULL DEFAULT 'manual',
                    status TEXT NOT NULL DEFAULT 'open',
                    details TEXT NOT NULL DEFAULT '',
                    result_text TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    resolved_at INTEGER,
                    FOREIGN KEY(run_id) REFERENCES change_audit_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_change_audit_issues_run
                ON change_audit_issues(run_id, status, id);

                CREATE TABLE IF NOT EXISTS warehouse_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sku TEXT NOT NULL UNIQUE,
                    size TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT '',
                    price_kopecks INTEGER NOT NULL DEFAULT 0,
                    tilda_external_id TEXT NOT NULL DEFAULT '',
                    publish_requested INTEGER NOT NULL DEFAULT 0,
                    sync_status TEXT NOT NULL DEFAULT 'local',
                    sync_error TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stock_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    reference_type TEXT NOT NULL DEFAULT '',
                    reference_id INTEGER,
                    note TEXT NOT NULL DEFAULT '',
                    actor_telegram_id INTEGER,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(product_id) REFERENCES warehouse_products(id),
                    UNIQUE(product_id, kind, reference_type, reference_id)
                );

                CREATE INDEX IF NOT EXISTS idx_stock_movements_product
                ON stock_movements(product_id, created_at, id);

                CREATE TABLE IF NOT EXISTS inventory_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    number TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'draft',
                    note TEXT NOT NULL DEFAULT '',
                    created_by INTEGER,
                    created_at INTEGER NOT NULL,
                    posted_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS inventory_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    expected_quantity INTEGER NOT NULL,
                    counted_quantity INTEGER NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES inventory_documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(product_id) REFERENCES warehouse_products(id),
                    UNIQUE(document_id, product_id)
                );

                CREATE TABLE IF NOT EXISTS production_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    number TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'planned',
                    note TEXT NOT NULL DEFAULT '',
                    created_by INTEGER,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    completed_at INTEGER,
                    cancelled_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS production_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    production_order_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    FOREIGN KEY(production_order_id) REFERENCES production_orders(id) ON DELETE CASCADE,
                    FOREIGN KEY(product_id) REFERENCES warehouse_products(id),
                    UNIQUE(production_order_id, product_id)
                );

                CREATE TABLE IF NOT EXISTS warehouse_sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );
                """
            )
            order_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(orders)").fetchall()
            }
            if "delivery_method" not in order_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN delivery_method TEXT NOT NULL DEFAULT ''"
                )
            if "email" not in order_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN email TEXT NOT NULL DEFAULT ''"
                )
            if "delivery_city" not in order_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN delivery_city TEXT NOT NULL DEFAULT ''"
                )
            if "delivery_cost_kopecks" not in order_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN delivery_cost_kopecks INTEGER NOT NULL DEFAULT 0"
                )
            if "cdek_last_sync_at" not in order_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN cdek_last_sync_at INTEGER NOT NULL DEFAULT 0"
                )
            if "cdek_address_dirty" not in order_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN cdek_address_dirty INTEGER NOT NULL DEFAULT 0"
                )
            if "shipment_hold_until" not in order_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN shipment_hold_until TEXT NOT NULL DEFAULT ''"
                )
            staff_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(staff)").fetchall()
            }
            if "can_check_order" not in staff_columns:
                connection.execute("ALTER TABLE staff ADD COLUMN can_check_order INTEGER NOT NULL DEFAULT 0")
                # These two buttons previously checked can_orders, even when
                # the old (retired order-write) can_operations flag was zero.
                connection.execute("UPDATE staff SET can_check_order=can_orders, can_operations=can_orders")
            if "can_staff" not in staff_columns:
                connection.execute("ALTER TABLE staff ADD COLUMN can_staff INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE staff SET can_staff=can_settings")
            if "can_promocodes" not in staff_columns:
                connection.execute(
                    "ALTER TABLE staff ADD COLUMN can_promocodes INTEGER NOT NULL DEFAULT 0"
                )
            if "can_warehouse" not in staff_columns:
                connection.execute(
                    "ALTER TABLE staff ADD COLUMN can_warehouse INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    """
                    UPDATE staff SET can_warehouse=1
                    WHERE role IN ('admin', 'manager') AND is_active=1
                    """
                )
                connection.execute(
                    """
                    UPDATE staff
                    SET can_promocodes=1
                    WHERE role IN ('admin', 'manager') AND is_active=1
                    """
                )
            promocode_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(promocodes)").fetchall()
            }
            if "sync_error" not in promocode_columns:
                connection.execute(
                    "ALTER TABLE promocodes ADD COLUMN sync_error TEXT NOT NULL DEFAULT ''"
                )
            if "tilda_id" not in promocode_columns:
                connection.execute(
                    "ALTER TABLE promocodes ADD COLUMN tilda_id TEXT NOT NULL DEFAULT ''"
                )
            if "source_chat_id" not in promocode_columns:
                connection.execute(
                    "ALTER TABLE promocodes ADD COLUMN source_chat_id INTEGER"
                )
            if "source_message_id" not in promocode_columns:
                connection.execute(
                    "ALTER TABLE promocodes ADD COLUMN source_message_id INTEGER"
                )
            for table in ("exchanges", "returns", "cancellations"):
                columns = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if "reason" not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN reason TEXT NOT NULL DEFAULT ''"
                    )
            self.initialize_sales_categories(connection)
            historical_changes = connection.execute(
                """
                SELECT order_id, actor_telegram_id, details_json, created_at
                FROM order_events WHERE event_type='order_changed'
                """
            ).fetchall()
            allowed_overrides = {
                "customer_name", "phone", "email", "telegram_username",
                "delivery_address", "payment_method",
            }
            for change in historical_changes:
                try:
                    field = str(json.loads(change["details_json"] or "{}").get("field") or "")
                except (json.JSONDecodeError, TypeError):
                    continue
                if field in allowed_overrides:
                    connection.execute(
                        """
                        INSERT INTO order_field_overrides (
                            order_id, field, actor_telegram_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(order_id, field) DO UPDATE SET
                            actor_telegram_id=excluded.actor_telegram_id,
                            created_at=MAX(created_at, excluded.created_at)
                        """,
                        (
                            int(change["order_id"]), field,
                            change["actor_telegram_id"], int(change["created_at"]),
                        ),
                    )

    def ensure_admins(self, admin_ids: Iterable[int]) -> None:
        now = int(time.time())
        with self.connect() as connection:
            for telegram_id in admin_ids:
                connection.execute(
                    """
                    INSERT INTO staff (
                        telegram_id, role, can_orders, can_operations,
                        can_analytics, can_settings, can_promocodes, can_warehouse,
                        can_check_order, can_staff, is_active, created_at, updated_at
                    ) VALUES (?, 'admin', 1, 1, 1, 1, 1, 1, 1, 1, 1, ?, ?)
                    ON CONFLICT(telegram_id) DO NOTHING
                    """,
                    (telegram_id, now, now),
                )

    def ensure_owner(self, telegram_id: int) -> None:
        """Only the owner retains unconditional access across restarts."""
        if telegram_id <= 0:
            raise ValueError("Неверный ID владельца")
        self.ensure_admins([telegram_id])
        self.set_setting("staff_owner_id", str(telegram_id))
        with self.connect() as connection:
            flags = ", ".join(f"can_{key}=1" for key in ALL_PERMISSIONS)
            connection.execute(
                f"UPDATE staff SET {flags}, is_active=1, role='admin' WHERE telegram_id=?",
                (telegram_id,),
            )

    def protected_owner_id(self) -> int:
        return int(self.get_setting("staff_owner_id", "0"))

    def is_staff_revoked(self, telegram_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT is_active FROM staff WHERE telegram_id=?", (telegram_id,)).fetchone()
        return row is not None and not row["is_active"]

    def get_staff(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM staff WHERE telegram_id = ? AND is_active = 1",
                (telegram_id,),
            ).fetchone()
        return dict(row) if row else None

    def has_permission(self, telegram_id: int, permission: str) -> bool:
        if permission not in ALL_PERMISSIONS:
            return False
        staff = self.get_staff(telegram_id)
        return bool(staff and staff[f"can_{permission}"])

    def set_staff_role(self, telegram_id: int, role: str, added_by: int) -> None:
        permissions = {
            "admin": (1, 1, 1, 1, 1, 1),
            "manager": (1, 1, 0, 0, 1, 1),
            "analyst": (1, 1, 1, 0, 0, 0),
            "viewer": (1, 1, 0, 0, 0, 0),
            "blocked": (0, 0, 0, 0, 0, 0),
            "custom": (0, 0, 0, 0, 0, 0),
        }
        if role not in permissions:
            raise ValueError("Неизвестная роль")
        if telegram_id <= 0:
            raise ValueError("Нужен положительный Telegram ID")
        if telegram_id == self.protected_owner_id():
            raise ValueError("Права владельца изменить нельзя")
        now = int(time.time())
        flags = permissions[role]
        is_active = 0 if role == "blocked" else 1
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO staff (
                    telegram_id, role, can_orders, can_operations,
                    can_analytics, can_settings, can_promocodes, is_active, added_by,
                    can_warehouse, can_check_order, can_staff,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    role = excluded.role,
                    can_orders = excluded.can_orders,
                    can_operations = excluded.can_operations,
                    can_analytics = excluded.can_analytics,
                    can_settings = excluded.can_settings,
                    can_promocodes = excluded.can_promocodes,
                    can_warehouse = excluded.can_warehouse,
                    can_check_order = excluded.can_check_order,
                    can_staff = excluded.can_staff,
                    is_active = excluded.is_active,
                    added_by = excluded.added_by,
                    updated_at = excluded.updated_at
                """,
                (
                    telegram_id, role, *flags[:5], is_active, added_by,
                    flags[5], flags[0], flags[3], now, now,
                ),
            )

    def set_staff_permission(self, telegram_id: int, permission: str, allowed: bool, added_by: int) -> None:
        if permission not in ALL_PERMISSIONS:
            raise ValueError("Неизвестное право")
        if telegram_id == self.protected_owner_id():
            raise ValueError("Права владельца изменить нельзя")
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE staff SET can_{permission}=?, role='custom', added_by=?, updated_at=? "
                "WHERE telegram_id=? AND is_active=1",
                (int(allowed), added_by, int(time.time()), telegram_id),
            )
            if not cursor.rowcount:
                raise ValueError("Сотрудник удалён или не найден")

    def remove_staff(self, telegram_id: int, added_by: int) -> None:
        # Retain the revoked record so ADMIN_IDS cannot silently re-add it,
        # and keep historical orders and actions linked to their original actor.
        self.set_staff_role(telegram_id, "blocked", added_by)

    def list_staff(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM staff WHERE is_active = 1 ORDER BY role, telegram_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )

    def create_promocode(
        self,
        *,
        code: str,
        discount_type: str,
        discount_value: str,
        usage_limit: int,
        expires_on: str,
        description: str,
        created_by: int,
        source_chat_id: Optional[int] = None,
        source_message_id: Optional[int] = None,
    ) -> int:
        if discount_type not in {"percent", "fixed"}:
            raise ValueError("Неизвестный тип скидки")
        now = int(time.time())
        try:
            with self.connect() as connection:
                live_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM promocodes WHERE status <> 'deleted'"
                    ).fetchone()["count"]
                )
                if live_count >= 500:
                    raise ValueError(
                        "Достигнут лимит Tilda: 500 действующих промокодов"
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO promocodes (
                        code, discount_type, discount_value, usage_limit,
                        expires_on, description, status, created_by, created_at,
                        source_chat_id, source_message_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending_tilda', ?, ?, ?, ?)
                    """,
                    (
                        code.strip().upper(),
                        discount_type,
                        str(discount_value).strip(),
                        max(0, int(usage_limit)),
                        expires_on.strip(),
                        description.strip(),
                        created_by,
                        now,
                        source_chat_id,
                        source_message_id,
                    ),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise ValueError("Активный промокод с таким названием уже существует") from error

    def get_promocode(self, promocode_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT p.*,
                       (
                           SELECT COUNT(*) FROM orders o
                           WHERE UPPER(TRIM(COALESCE(o.promo_code, ''))) = UPPER(p.code)
                             AND o.status IN ('paid', 'processing', 'partially_returned', 'exchange', 'exchange_inbound', 'deposit')
                       ) AS paid_orders_count
                FROM promocodes p
                WHERE p.id=?
                """,
                (promocode_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_promocodes(self, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*,
                       (
                           SELECT COUNT(*) FROM orders o
                           WHERE UPPER(TRIM(COALESCE(o.promo_code, ''))) = UPPER(p.code)
                             AND o.status IN ('paid', 'processing', 'partially_returned', 'exchange', 'exchange_inbound', 'deposit')
                       ) AS paid_orders_count
                FROM promocodes p
                ORDER BY p.created_at DESC, p.id DESC
                LIMIT ? OFFSET ?
                """,
                (max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [dict(row) for row in rows]

    def promocode_counts(self) -> Dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM promocodes GROUP BY status"
            ).fetchall()
        result = {
            "pending_tilda": 0,
            "sync_failed": 0,
            "active": 0,
            "deleted": 0,
        }
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result

    def pending_promocode_notifications(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.id AS promocode_id, p.code, o.id AS order_id,
                       o.order_number, o.customer_name, o.created_at,
                       p.source_chat_id, p.source_message_id
                FROM promocodes p
                JOIN orders o
                  ON UPPER(TRIM(COALESCE(o.promo_code, ''))) = UPPER(p.code)
                 AND o.status <> 'cancelled'
                LEFT JOIN promocode_notifications n
                  ON n.promocode_id=p.id AND n.order_id=o.id
                WHERE p.status='active' AND n.order_id IS NULL
                ORDER BY o.created_at, o.id
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_promocode_notification(self, promocode_id: int, order_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO promocode_notifications (
                    promocode_id, order_id, notified_at
                ) VALUES (?, ?, ?)
                """,
                (int(promocode_id), int(order_id), int(time.time())),
            )

    def baseline_promocode_notifications(self) -> int:
        """Mark existing uses so binding a channel never sends historical spam."""
        now = int(time.time())
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO promocode_notifications (
                    promocode_id, order_id, notified_at
                )
                SELECT p.id, o.id, ?
                FROM promocodes p
                JOIN orders o
                  ON UPPER(TRIM(COALESCE(o.promo_code, ''))) = UPPER(p.code)
                 AND o.status <> 'cancelled'
                WHERE p.status='active'
                """,
                (now,),
            )
        return int(cursor.rowcount)

    def activate_promocode(
        self, promocode_id: int, actor_id: int, tilda_id: str = ""
    ) -> None:
        now = int(time.time())
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM promocodes WHERE id=?", (promocode_id,)
            ).fetchone()
            if not row:
                raise ValueError("Промокод не найден")
            if row["status"] == "deleted":
                raise ValueError("Удалённый промокод нельзя активировать")
            connection.execute(
                """
                UPDATE promocodes
                SET status='active', sync_error='', tilda_id=?,
                    activated_by=?, activated_at=?
                WHERE id=?
                """,
                (str(tilda_id or "").strip(), actor_id, now, promocode_id),
            )

    def delete_promocode(self, promocode_id: int, actor_id: int) -> None:
        now = int(time.time())
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM promocodes WHERE id=?", (promocode_id,)
            ).fetchone()
            if not row:
                raise ValueError("Промокод не найден")
            if row["status"] == "deleted":
                raise ValueError("Промокод уже удалён")
            connection.execute(
                """
                UPDATE promocodes
                SET status='deleted', sync_error='', deleted_by=?, deleted_at=?
                WHERE id=?
                """,
                (actor_id, now, promocode_id),
            )

    def fail_promocode_sync(self, promocode_id: int, error_text: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM promocodes WHERE id=?", (promocode_id,)
            ).fetchone()
            if not row:
                raise ValueError("Промокод не найден")
            if row["status"] == "deleted":
                raise ValueError("Промокод уже удалён")
            connection.execute(
                """
                UPDATE promocodes
                SET status='sync_failed', sync_error=?
                WHERE id=?
                """,
                (error_text.strip()[:1000], promocode_id),
            )

    def change_audit_scope_since(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(finished_at) AS finished_at
                FROM change_audit_runs
                WHERE finished_at IS NOT NULL
                """
            ).fetchone()
        return int(row["finished_at"] or (int(time.time()) - 86400))

    def list_change_audit_order_ids(
        self, since: int, explicit_ids: Optional[Sequence[int]] = None
    ) -> List[int]:
        if explicit_ids is not None:
            return sorted({int(value) for value in explicit_ids})
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT order_id FROM (
                    SELECT order_id
                    FROM order_events
                    WHERE created_at >= ?
                    UNION ALL
                    SELECT order_id
                    FROM change_requests
                    WHERE order_id IS NOT NULL
                      AND (created_at >= ? OR status IN ('pending', 'manual', 'failed', 'processing'))
                    UNION ALL
                    SELECT id AS order_id
                    FROM orders
                    WHERE updated_at >= ? OR cdek_address_dirty=1
                    UNION ALL
                    SELECT order_id
                    FROM change_audit_issues
                    WHERE status IN ('open', 'manual', 'failed')
                )
                WHERE order_id IS NOT NULL
                ORDER BY order_id
                """,
                (int(since), int(since), int(since)),
            ).fetchall()
        return [int(row["order_id"]) for row in rows]

    def open_change_requests_for_order(self, order_id: int) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, action, status, result_text
                FROM change_requests
                WHERE order_id=? AND status IN ('pending', 'manual', 'failed', 'processing')
                ORDER BY created_at, id
                """,
                (order_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_change_audit_run(self, started_by: int, scope_since: int) -> int:
        now = int(time.time())
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO change_audit_runs (
                    started_by, status, scope_since, created_at
                ) VALUES (?, 'running', ?, ?)
                """,
                (started_by, int(scope_since), now),
            )
        return int(cursor.lastrowid)

    def add_change_audit_issue(
        self,
        run_id: int,
        order_id: int,
        order_number: str,
        issue_type: str,
        fields: Sequence[str],
        fix_mode: str,
        details: str,
    ) -> int:
        now = int(time.time())
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO change_audit_issues (
                    run_id, order_id, order_number, issue_type, fields_json,
                    fix_mode, status, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    run_id,
                    order_id,
                    order_number,
                    issue_type,
                    json.dumps(list(fields), ensure_ascii=False),
                    fix_mode,
                    details,
                    now,
                ),
            )
        return int(cursor.lastrowid)

    def finish_change_audit_run(
        self, run_id: int, scanned_orders: int, summary: Dict[str, Any]
    ) -> None:
        now = int(time.time())
        issues_count = int(summary.get("issues_count") or 0)
        auto_count = int(summary.get("auto_fixable_count") or 0)
        manual_count = int(summary.get("manual_count") or 0)
        status = "ok" if issues_count == 0 else "needs_action"
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE change_audit_runs
                SET status=?, scanned_orders=?, issues_count=?,
                    auto_fixable_count=?, manual_count=?, summary_json=?,
                    finished_at=?
                WHERE id=?
                """,
                (
                    status,
                    int(scanned_orders),
                    issues_count,
                    auto_count,
                    manual_count,
                    json.dumps(summary, ensure_ascii=False),
                    now,
                    run_id,
                ),
            )

    def fail_change_audit_run(self, run_id: int, reason: str) -> None:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE change_audit_runs
                SET status='failed', summary_json=?, finished_at=?
                WHERE id=?
                """,
                (json.dumps({"error": reason}, ensure_ascii=False), now, run_id),
            )

    def get_change_audit_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM change_audit_runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["summary"] = json.loads(result.pop("summary_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            result["summary"] = {}
        return result

    def list_change_audit_issues(
        self, run_id: int, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        status_clause = " AND status=?" if status else ""
        parameters: List[Any] = [run_id]
        if status:
            parameters.append(status)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM change_audit_issues
                WHERE run_id=?{status_clause}
                ORDER BY id
                """,
                tuple(parameters),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            try:
                value["fields"] = json.loads(value.pop("fields_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                value["fields"] = []
            result.append(value)
        return result

    def resolve_change_audit_issue(
        self, issue_id: int, status: str, result_text: str
    ) -> None:
        if status not in {"fixed", "manual", "failed"}:
            raise ValueError("Неизвестный результат исправления")
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE change_audit_issues
                SET status=?, result_text=?, resolved_at=?
                WHERE id=?
                """,
                (status, result_text, now, issue_id),
            )

    def create_change_request(
        self,
        *,
        dedupe_key: str,
        order_id: Optional[int],
        order_number: str,
        action: str,
        payload: Dict[str, Any],
        raw_text: str,
        status: str,
        source_chat_id: Optional[int],
        source_message_id: Optional[int],
        requested_by: Optional[int],
    ) -> tuple[int, bool]:
        now = int(time.time())
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO change_requests (
                    dedupe_key, order_id, order_number, action, payload_json,
                    raw_text, status, source_chat_id, source_message_id,
                    requested_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dedupe_key,
                    order_id,
                    order_number,
                    action,
                    json.dumps(payload, ensure_ascii=False),
                    raw_text,
                    status,
                    source_chat_id,
                    source_message_id,
                    requested_by,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT id FROM change_requests WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
        return int(row["id"]), created

    def get_change_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM change_requests WHERE id=?", (request_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["payload"] = json.loads(result.pop("payload_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            result["payload"] = {}
        return result

    def list_open_change_requests(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM change_requests
                WHERE status IN ('pending', 'manual')
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [
            request
            for row in rows
            if (request := self.get_change_request(int(row["id"]))) is not None
        ]

    def claim_change_request(self, request_id: int, reviewer_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE change_requests
                SET status='processing', reviewed_by=?
                WHERE id=? AND status IN ('pending', 'manual')
                """,
                (reviewer_id, request_id),
            )
        return cursor.rowcount == 1

    def finish_change_request(
        self, request_id: int, status: str, reviewer_id: int, result_text: str
    ) -> None:
        if status not in {"approved", "rejected", "failed", "pending", "manual"}:
            raise ValueError("Неизвестный статус заявки на изменение")
        reviewed_at = None if status in {"pending", "manual"} else int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE change_requests
                SET status=?, reviewed_by=?, result_text=?, reviewed_at=?
                WHERE id=?
                """,
                (status, reviewer_id, result_text, reviewed_at, request_id),
            )

    def upsert_order(self, order: Dict[str, Any], items: Sequence[Dict[str, Any]]) -> int:
        now = int(time.time())
        provided_created_at = int(order.get("created_at") or 0)
        created_at = provided_created_at or now
        order_number = str(order["order_number"]).strip()
        if not order_number:
            raise ValueError("У заказа отсутствует номер")
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM orders WHERE order_number = ?", (order_number,)
            ).fetchone()
            if existing:
                order_id = int(existing["id"])
                protected_fields = {
                    str(row["field"])
                    for row in connection.execute(
                        "SELECT field FROM order_field_overrides WHERE order_id=?",
                        (order_id,),
                    ).fetchall()
                }

                def source_value(field: str, default: str = "") -> str:
                    if field in protected_fields:
                        return str(existing[field] or "")
                    return str(order.get(field, default) or "")

                source_email = source_value("email") or str(existing["email"] or "")
                connection.execute(
                    """
                    UPDATE orders SET
                        customer_name=?, phone=?,
                        email=COALESCE(NULLIF(?, ''), email), telegram_username=?,
                        delivery_address=?,
                        delivery_method=COALESCE(NULLIF(?, ''), delivery_method),
                        delivery_city=COALESCE(NULLIF(?, ''), delivery_city),
                        delivery_cost_kopecks=COALESCE(
                            NULLIF(?, 0), delivery_cost_kopecks
                        ),
                        payment_method=?, promo_code=?,
                        discount_kopecks=?, total_kopecks=?, currency=?,
                        status=CASE
                            WHEN status IN (
                                'cancelled', 'returned', 'exchange',
                                'exchange_inbound', 'deposit', 'refund_completed'
                            ) THEN status
                            ELSE ?
                        END,
                        source=?, tilda_tranid=?, cdek_uuid=COALESCE(?, cdek_uuid),
                        cdek_number=COALESCE(?, cdek_number),
                        cdek_payload_json=COALESCE(
                            NULLIF(?, '{}'), cdek_payload_json
                        ),
                        raw_source_json=?,
                        created_at=CASE WHEN ? > 0 THEN ? ELSE created_at END,
                        updated_at=?
                    WHERE order_number=?
                    """,
                    (
                        source_value("customer_name"),
                        source_value("phone"),
                        source_email,
                        source_value("telegram_username"),
                        source_value("delivery_address"),
                        order.get("delivery_method", ""),
                        order.get("delivery_city", ""),
                        int(order.get("delivery_cost_kopecks", 0)),
                        source_value("payment_method"),
                        order.get("promo_code", ""),
                        int(order.get("discount_kopecks", 0)),
                        int(order.get("total_kopecks", 0)),
                        order.get("currency", "RUB"),
                        order.get("status", "new"),
                        order.get("source", "manual"),
                        order.get("tilda_tranid"),
                        order.get("cdek_uuid"),
                        order.get("cdek_number"),
                        json.dumps(order.get("cdek_payload", {}), ensure_ascii=False),
                        json.dumps(order.get("raw_source", {}), ensure_ascii=False),
                        provided_created_at,
                        provided_created_at,
                        now,
                        order_number,
                    ),
                )
                items_exist = connection.execute(
                    "SELECT 1 FROM order_items WHERE order_id = ? LIMIT 1", (order_id,)
                ).fetchone()
                should_insert_items = not items_exist
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO orders (
                        order_number, customer_name, phone, email, telegram_username,
                        delivery_address, delivery_method, delivery_city,
                        delivery_cost_kopecks, payment_method, promo_code,
                        discount_kopecks, total_kopecks, currency, status, source,
                        tilda_tranid, cdek_uuid, cdek_number, cdek_payload_json,
                        raw_source_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_number,
                        order.get("customer_name", ""),
                        order.get("phone", ""),
                        order.get("email", ""),
                        order.get("telegram_username", ""),
                        order.get("delivery_address", ""),
                        order.get("delivery_method", ""),
                        order.get("delivery_city", ""),
                        int(order.get("delivery_cost_kopecks", 0)),
                        order.get("payment_method", ""),
                        order.get("promo_code", ""),
                        int(order.get("discount_kopecks", 0)),
                        int(order.get("total_kopecks", 0)),
                        order.get("currency", "RUB"),
                        order.get("status", "new"),
                        order.get("source", "manual"),
                        order.get("tilda_tranid"),
                        order.get("cdek_uuid"),
                        order.get("cdek_number"),
                        json.dumps(order.get("cdek_payload", {}), ensure_ascii=False),
                        json.dumps(order.get("raw_source", {}), ensure_ascii=False),
                        created_at,
                        now,
                    ),
                )
                order_id = int(cursor.lastrowid)
                should_insert_items = True

            for item in items if should_insert_items else []:
                connection.execute(
                    """
                    INSERT INTO order_items (
                        order_id, product_name, sku, size, quantity,
                        unit_price_kopecks, item_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        order_id,
                        item.get("product_name", "Товар"),
                        item.get("sku", ""),
                        item.get("size", ""),
                        max(1, int(item.get("quantity", 1))),
                        int(item.get("unit_price_kopecks", 0)),
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO order_events (order_id, event_type, details_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (order_id, "source_upsert", json.dumps({"source": order.get("source", "manual")}), now),
            )
        return order_id

    def search_orders(
        self, query: str, limit: int = 20, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        pattern = f"%{query.strip()}%"
        status_clause = " AND status = ?" if status else ""
        parameters: List[Any] = [pattern, pattern, pattern]
        if status:
            parameters.append(status)
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, order_number, customer_name, phone, status,
                       total_kopecks, created_at
                FROM orders
                WHERE (
                    order_number LIKE ? COLLATE NOCASE
                    OR customer_name LIKE ? COLLATE NOCASE
                    OR phone LIKE ?
                )
                {status_clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_orders_by_status(
        self, status: str, limit: int = 10, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, order_number, customer_name, phone, status,
                       total_kopecks, created_at
                FROM orders
                WHERE status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (status, max(1, limit), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def order_ids_by_status(self, status: str) -> List[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM orders WHERE status = ? ORDER BY created_at DESC, id DESC",
                (status,),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def orders_for_export(self, order_ids: Sequence[int]) -> List[Dict[str, Any]]:
        ids = list(dict.fromkeys(int(value) for value in order_ids))
        if not ids:
            return []
        orders: Dict[int, Dict[str, Any]] = {}
        items: Dict[int, List[Dict[str, Any]]] = {order_id: [] for order_id in ids}
        with self.connect() as connection:
            for index in range(0, len(ids), 500):
                chunk = ids[index:index + 500]
                placeholders = ",".join("?" for _ in chunk)
                for row in connection.execute(
                    f"SELECT * FROM orders WHERE id IN ({placeholders})", tuple(chunk)
                ).fetchall():
                    orders[int(row["id"])] = dict(row)
                for row in connection.execute(
                    f"""
                    SELECT * FROM order_items
                    WHERE order_id IN ({placeholders})
                    ORDER BY order_id, id
                    """,
                    tuple(chunk),
                ).fetchall():
                    items[int(row["order_id"])].append(dict(row))
        result = []
        for order_id in ids:
            order = orders.get(order_id)
            if order:
                order["items"] = items[order_id]
                result.append(order)
        return result

    def order_period_summary(self, start_at: int, end_at: int) -> Dict[str, Any]:
        with self.connect() as connection:
            total = connection.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(total_kopecks), 0) AS amount
                FROM orders WHERE created_at>=? AND created_at<?
                """,
                (int(start_at), int(end_at)),
            ).fetchone()
            statuses = connection.execute(
                """
                SELECT status, COUNT(*) AS count,
                       COALESCE(SUM(total_kopecks), 0) AS amount
                FROM orders WHERE created_at>=? AND created_at<?
                GROUP BY status ORDER BY count DESC, status
                """,
                (int(start_at), int(end_at)),
            ).fetchall()
        return {
            "orders_count": int(total["count"]),
            "orders_amount": int(total["amount"]),
            "statuses": [dict(row) for row in statuses],
        }

    def status_counts(self) -> Dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM orders GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def all_order_ids(self) -> List[int]:
        with self.connect() as connection:
            rows = connection.execute("SELECT id FROM orders ORDER BY id").fetchall()
        return [int(row["id"]) for row in rows]

    def status_summary(self, status: str) -> Dict[str, Any]:
        """Aggregate the contents and value of orders in one status."""
        with self.connect() as connection:
            base = connection.execute(
                """
                SELECT COUNT(*) AS orders_count,
                       COALESCE(SUM(total_kopecks), 0) AS orders_amount
                FROM orders WHERE status=?
                """,
                (status,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT i.product_name, i.size,
                       SUM(i.quantity) AS quantity,
                       SUM(i.unit_price_kopecks * i.quantity) AS amount
                FROM order_items i
                JOIN orders o ON o.id=i.order_id
                WHERE o.status=? AND i.item_status='active'
                GROUP BY i.product_name, i.size
                ORDER BY quantity DESC, i.product_name COLLATE NOCASE, i.size
                """,
                (status,),
            ).fetchall()
        return {
            "orders_count": int(base["orders_count"]),
            "orders_amount": int(base["orders_amount"]),
            "items_quantity": sum(int(row["quantity"] or 0) for row in rows),
            "items_amount": sum(int(row["amount"] or 0) for row in rows),
            "items": [dict(row) for row in rows],
        }

    def product_catalog(self) -> List[Dict[str, Any]]:
        """Build an inline-selection catalog from products already stored in CRM."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT product_name, size, COUNT(*) AS uses
                FROM order_items
                WHERE TRIM(product_name) <> '' AND item_status='active'
                GROUP BY product_name COLLATE NOCASE, size COLLATE NOCASE
                ORDER BY product_name COLLATE NOCASE, uses DESC, size COLLATE NOCASE
                """
            ).fetchall()
        products: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            name = str(row["product_name"]).strip()
            key = name.casefold()
            entry = products.setdefault(key, {"product_name": name, "sizes": []})
            size = str(row["size"] or "").strip()
            if size not in entry["sizes"]:
                entry["sizes"].append(size)
        return list(products.values())

    def get_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not row:
                return None
            items = connection.execute(
                "SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)
            ).fetchall()
            operations = connection.execute(
                """
                SELECT 'exchange' AS kind, e.id, e.reason, e.created_at,
                       old.product_name AS old_product, old.size AS old_size,
                       new.product_name AS new_product, new.size AS new_size,
                       e.amount_kopecks AS amount_kopecks
                FROM exchanges e
                JOIN exchange_items x ON x.exchange_id=e.id
                JOIN order_items old ON old.id=x.old_item_id
                JOIN order_items new ON new.id=x.new_item_id
                WHERE e.order_id=?
                UNION ALL
                SELECT 'return', r.id, r.reason, r.created_at,
                       item.product_name, item.size, '', '', ri.amount_kopecks
                FROM returns r
                JOIN return_items ri ON ri.return_id=r.id
                JOIN order_items item ON item.id=ri.item_id
                WHERE r.order_id=?
                UNION ALL
                SELECT 'cancellation', c.id, c.reason, c.created_at,
                       item.product_name, item.size, '', '', ci.amount_kopecks
                FROM cancellations c
                JOIN cancellation_items ci ON ci.cancellation_id=c.id
                JOIN order_items item ON item.id=ci.item_id
                WHERE c.order_id=?
                ORDER BY 4, 2
                """,
                (order_id, order_id, order_id),
            ).fetchall()
        result = dict(row)
        result["items"] = [dict(item) for item in items]
        result["operations"] = [dict(operation) for operation in operations]
        return result

    def get_order_by_number(self, order_number: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM orders WHERE order_number = ? COLLATE NOCASE",
                (order_number.strip(),),
            ).fetchone()
        return self.get_order(int(row["id"])) if row else None

    def reverted_manual_changes(self, order_id: int) -> List[Dict[str, Any]]:
        """Find current fields that differ from their latest approved change event."""
        with self.connect() as connection:
            order = connection.execute(
                "SELECT * FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            if not order:
                return []
            items = {
                int(row["id"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM order_items WHERE order_id=?", (order_id,)
                ).fetchall()
            }
            events = connection.execute(
                """
                SELECT event_type, details_json FROM order_events
                WHERE order_id=? AND event_type IN (
                    'order_changed', 'item_changed', 'cdek_order_recreated'
                )
                ORDER BY created_at, id
                """,
                (order_id,),
            ).fetchall()
        expected_order: Dict[str, str] = {}
        expected_items: Dict[tuple[int, str], str] = {}
        for event in events:
            try:
                details = json.loads(event["details_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if event["event_type"] == "order_changed":
                field = str(details.get("field") or "")
                if field:
                    expected_order[field] = str(details.get("new") or "")
            elif event["event_type"] == "cdek_order_recreated":
                expected_order["delivery_address"] = str(details.get("new_address") or "")
            else:
                try:
                    item_id = int(details["item_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                field = str(details.get("field") or "")
                if field:
                    expected_items[(item_id, field)] = str(details.get("new") or "")
        issues: List[Dict[str, Any]] = []
        for field, expected in expected_order.items():
            if field in order.keys() and str(order[field] or "") != expected:
                issues.append(
                    {"object": "order", "field": field, "expected": expected,
                     "current": str(order[field] or "")}
                )
        for (item_id, field), expected in expected_items.items():
            item = items.get(item_id)
            if item and field in item and str(item[field] or "") != expected:
                issues.append(
                    {"object": "item", "item_id": item_id, "field": field,
                     "expected": expected, "current": str(item[field] or "")}
                )
        return issues

    def confirmed_cdek_change_fields(self, order_id: int) -> FrozenSet[str]:
        """Return CDEK fields backed by an explicit CRM change event."""
        field_map = {
            "customer_name": "recipient_name",
            "phone": "phone",
            "email": "email",
            "delivery_address": "address",
        }
        confirmed = set()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_type, details_json
                FROM order_events
                WHERE order_id=? AND event_type IN (
                    'order_changed', 'item_changed', 'cdek_order_recreated'
                )
                ORDER BY created_at, id
                """,
                (order_id,),
            ).fetchall()
        for row in rows:
            if row["event_type"] == "item_changed":
                confirmed.add("items")
                continue
            if row["event_type"] == "cdek_order_recreated":
                confirmed.add("address")
                continue
            try:
                details = json.loads(row["details_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            cdek_field = field_map.get(str(details.get("field") or ""))
            if cdek_field:
                confirmed.add(cdek_field)
        return frozenset(confirmed)

    def cdek_source_reconciliations(self, order_id: int) -> Dict[str, Dict[str, Any]]:
        """Return latest source-backed CRM/CDEK fingerprints for each field."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT details_json
                FROM order_events
                WHERE order_id=? AND event_type='source_reconciled'
                ORDER BY created_at, id
                """,
                (order_id,),
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            try:
                details = json.loads(row["details_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            field = str(details.get("field") or "")
            if field:
                result[field] = details
        return result

    def record_cdek_source_reconciliation(
        self,
        order_id: int,
        field: str,
        crm_fingerprint: str,
        cdek_fingerprint: str,
        source: str,
        reason: str,
        actor_id: Optional[int] = None,
    ) -> None:
        if field not in {"items", "recipient_name", "phone", "email", "address"}:
            raise ValueError("Неизвестное поле сверки СДЭК")
        now = int(time.time())
        details = {
            "field": field,
            "crm_fingerprint": str(crm_fingerprint),
            "cdek_fingerprint": str(cdek_fingerprint),
            "source": str(source),
            "reason": str(reason),
        }
        with self.connect() as connection:
            self._event(
                connection,
                order_id,
                "source_reconciled",
                actor_id,
                details,
                now,
            )

    def get_items(self, item_ids: Sequence[int], active_only: bool = True) -> List[Dict[str, Any]]:
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        active_clause = " AND item_status = 'active'" if active_only else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM order_items WHERE id IN ({placeholders}){active_clause}",
                tuple(item_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_order_field(self, order_id: int, field: str, value: str, actor_id: int) -> None:
        allowed = {
            "order_number", "customer_name", "phone", "delivery_address",
            "delivery_method", "delivery_city", "payment_method",
            "telegram_username", "cdek_number", "email",
            "shipment_hold_until",
        }
        if field not in allowed:
            raise ValueError("Это поле нельзя изменить")
        now = int(time.time())
        with self.connect() as connection:
            old = connection.execute(
                f"SELECT {field} FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not old:
                raise ValueError("Заказ не найден")
            dirty_sql = ", cdek_address_dirty = 1" if field == "delivery_address" else ""
            connection.execute(
                f"UPDATE orders SET {field} = ?, updated_at = ?{dirty_sql} WHERE id = ?",
                (value.strip(), now, order_id),
            )
            if field in {
                "customer_name", "phone", "email", "telegram_username",
                "delivery_address", "delivery_method", "delivery_city",
                "payment_method",
                "shipment_hold_until",
            }:
                connection.execute(
                    """
                    INSERT INTO order_field_overrides (
                        order_id, field, actor_telegram_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(order_id, field) DO UPDATE SET
                        actor_telegram_id=excluded.actor_telegram_id,
                        created_at=excluded.created_at
                    """,
                    (order_id, field, actor_id, now),
                )
            self._event(
                connection, order_id, "order_changed", actor_id,
                {"field": field, "old": old[field], "new": value.strip()}, now,
            )

    def set_order_status(
        self, order_id: int, status: str, actor_id: Optional[int] = None
    ) -> None:
        if status not in ORDER_STATUSES:
            raise ValueError("Неизвестный статус заказа")
        now = int(time.time())
        with self.connect() as connection:
            current = connection.execute(
                "SELECT status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not current:
                raise ValueError("Заказ не найден")
            connection.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE id=?",
                (status, now, order_id),
            )
            self._event(
                connection,
                order_id,
                "status_changed",
                actor_id,
                {"old": current["status"], "new": status},
                now,
            )

    def bulk_set_order_status(
        self,
        order_ids: Sequence[int],
        status: str,
        actor_id: Optional[int] = None,
    ) -> int:
        if status not in ORDER_STATUSES:
            raise ValueError("Неизвестный статус заказа")
        unique_ids = sorted({int(value) for value in order_ids})
        if not unique_ids:
            return 0
        now = int(time.time())
        changed = 0
        with self.connect() as connection:
            for order_id in unique_ids:
                current = connection.execute(
                    "SELECT status FROM orders WHERE id=?", (order_id,)
                ).fetchone()
                if not current or current["status"] == status:
                    continue
                connection.execute(
                    "UPDATE orders SET status=?, updated_at=? WHERE id=?",
                    (status, now, order_id),
                )
                self._event(
                    connection,
                    order_id,
                    "status_changed",
                    actor_id,
                    {
                        "old": current["status"],
                        "new": status,
                        "source": "bulk_status",
                    },
                    now,
                )
                changed += 1
        return changed

    def orders_by_numbers(self, order_numbers: Sequence[str]) -> List[Dict[str, Any]]:
        numbers = list(
            dict.fromkeys(
                str(value).strip()
                for value in order_numbers
                if str(value).strip()
            )
        )
        if not numbers:
            return []
        placeholders = ",".join("?" for _ in numbers)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, order_number, customer_name, status,
                       cdek_uuid, cdek_number, created_at
                FROM orders
                WHERE order_number IN ({placeholders})
                """,
                numbers,
            ).fetchall()
        indexed = {str(row["order_number"]): dict(row) for row in rows}
        return [indexed[number] for number in numbers if number in indexed]

    def orders_by_product_query(
        self, query: str, limit: int = 500
    ) -> List[Dict[str, Any]]:
        normalized_query = " ".join(str(query).split()).casefold()
        if not normalized_query:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT o.id, o.order_number, o.customer_name, o.status,
                       o.cdek_uuid, o.cdek_number, o.created_at,
                       i.product_name, i.size
                FROM orders o
                JOIN order_items i ON i.order_id=o.id
                WHERE i.item_status='active'
                  AND o.status NOT IN ('cancelled', 'returned', 'refund_completed')
                ORDER BY o.created_at, o.id, i.id
                """,
            ).fetchall()
        result: List[Dict[str, Any]] = []
        seen = set()
        maximum = max(1, min(int(limit), 1000))
        for row in rows:
            searchable = " ".join(
                f"{row['product_name']} {row['size']}".split()
            ).casefold()
            order_id = int(row["id"])
            if normalized_query not in searchable or order_id in seen:
                continue
            result.append(
                {
                    key: row[key]
                    for key in (
                        "id", "order_number", "customer_name", "status",
                        "cdek_uuid", "cdek_number", "created_at",
                    )
                }
            )
            seen.add(order_id)
            if len(result) >= maximum:
                break
        return result

    def orders_for_cdek_labels(
        self, order_ids: Sequence[int]
    ) -> List[Dict[str, Any]]:
        unique_ids = sorted({int(value) for value in order_ids})
        if not unique_ids:
            return []
        placeholders = ",".join("?" for _ in unique_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, order_number, status, cdek_uuid, cdek_number
                FROM orders
                WHERE id IN ({placeholders})
                  AND cdek_uuid IS NOT NULL AND TRIM(cdek_uuid)<>''
                  AND cdek_number IS NOT NULL AND TRIM(cdek_number)<>''
                ORDER BY order_number
                """,
                unique_ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_all_order_statuses(self, status: str) -> int:
        if status not in ORDER_STATUSES:
            raise ValueError("Неизвестный статус заказа")
        now = int(time.time())
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE status<>?",
                (status, now, status),
            )
        return int(cursor.rowcount)

    def update_item_field(self, item_id: int, field: str, value: str, actor_id: int) -> int:
        if field not in {"product_name", "size"}:
            raise ValueError("Это поле позиции нельзя изменить")
        now = int(time.time())
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT order_id, {field} FROM order_items WHERE id = ?", (item_id,)
            ).fetchone()
            if not row:
                raise ValueError("Позиция не найдена")
            connection.execute(
                f"UPDATE order_items SET {field} = ?, updated_at = ? WHERE id = ?",
                (value.strip(), now, item_id),
            )
            self._event(
                connection, int(row["order_id"]), "item_changed", actor_id,
                {"item_id": item_id, "field": field, "old": row[field], "new": value.strip()}, now,
            )
        return int(row["order_id"])

    def add_order_item(
        self,
        order_id: int,
        product_name: str,
        sku: str,
        size: str,
        quantity: int,
        unit_price_kopecks: int,
        actor_id: int,
    ) -> tuple[int, bool]:
        """Add a manual active item once and record it in the order history."""
        name = str(product_name or "").strip()
        sku_value = str(sku or "").strip()
        size_value = str(size or "").strip()
        if not name:
            raise ValueError("Название товара не должно быть пустым")
        amount = max(1, int(quantity))
        price = max(0, int(unit_price_kopecks))
        now = int(time.time())
        with self.connect() as connection:
            order = connection.execute(
                "SELECT id FROM orders WHERE id=?", (int(order_id),)
            ).fetchone()
            if not order:
                raise ValueError("Заказ не найден")
            existing = connection.execute(
                """
                SELECT id FROM order_items
                WHERE order_id=? AND item_status='active'
                  AND product_name=? AND sku=? AND size=?
                ORDER BY id LIMIT 1
                """,
                (int(order_id), name, sku_value, size_value),
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cursor = connection.execute(
                """
                INSERT INTO order_items (
                    order_id, product_name, sku, size, quantity,
                    unit_price_kopecks, item_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    int(order_id), name, sku_value, size_value, amount, price,
                    now, now,
                ),
            )
            item_id = int(cursor.lastrowid)
            self._event(
                connection,
                int(order_id),
                "item_added",
                int(actor_id),
                {
                    "item_id": item_id,
                    "product_name": name,
                    "sku": sku_value,
                    "size": size_value,
                    "quantity": amount,
                    "unit_price_kopecks": price,
                },
                now,
            )
        return item_id, True

    def apply_order_changes(
        self,
        order_id: int,
        order_changes: Dict[str, str],
        item_changes: Dict[str, Dict[str, str]],
        actor_id: int,
    ) -> None:
        """Atomically saves a reviewed order draft and its audit events."""
        allowed_order_fields = {"customer_name", "phone", "email", "delivery_address"}
        allowed_item_fields = {"product_name", "size"}
        if set(order_changes) - allowed_order_fields:
            raise ValueError("В черновике есть недоступное поле заказа")
        now = int(time.time())
        with self.connect() as connection:
            order = connection.execute(
                "SELECT id FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            if not order:
                raise ValueError("Заказ не найден")
            for field, raw_value in order_changes.items():
                value = str(raw_value).strip()
                if not value:
                    raise ValueError("Значение поля заказа не должно быть пустым")
                old = connection.execute(
                    f"SELECT {field} FROM orders WHERE id=?", (order_id,)
                ).fetchone()
                dirty_sql = ", cdek_address_dirty=1" if field == "delivery_address" else ""
                connection.execute(
                    f"UPDATE orders SET {field}=?, updated_at=?{dirty_sql} WHERE id=?",
                    (value, now, order_id),
                )
                connection.execute(
                    """
                    INSERT INTO order_field_overrides (
                        order_id, field, actor_telegram_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(order_id, field) DO UPDATE SET
                        actor_telegram_id=excluded.actor_telegram_id,
                        created_at=excluded.created_at
                    """,
                    (order_id, field, actor_id, now),
                )
                self._event(
                    connection,
                    order_id,
                    "order_changed",
                    actor_id,
                    {"field": field, "old": old[field], "new": value},
                    now,
                )
            for raw_item_id, changes in item_changes.items():
                if set(changes) - allowed_item_fields:
                    raise ValueError("В черновике есть недоступное поле товара")
                item_id = int(raw_item_id)
                item = connection.execute(
                    "SELECT * FROM order_items WHERE id=? AND order_id=? AND item_status='active'",
                    (item_id, order_id),
                ).fetchone()
                if not item:
                    raise ValueError("Активная позиция заказа не найдена")
                for field, raw_value in changes.items():
                    value = str(raw_value).strip()
                    if not value:
                        raise ValueError("Значение поля товара не должно быть пустым")
                    connection.execute(
                        f"UPDATE order_items SET {field}=?, updated_at=? WHERE id=?",
                        (value, now, item_id),
                    )
                    self._event(
                        connection,
                        order_id,
                        "item_changed",
                        actor_id,
                        {
                            "item_id": item_id,
                            "field": field,
                            "old": item[field],
                            "new": value,
                        },
                        now,
                    )

    def create_exchange(
        self, order_id: int, replacements: Dict[int, Dict[str, str]], actor_id: int,
        reason: str = "",
    ) -> int:
        old_items = self.get_items(list(replacements), active_only=True)
        if len(old_items) != len(replacements):
            raise ValueError("Некоторые позиции уже обработаны или не найдены")
        if any(int(item["order_id"]) != order_id for item in old_items):
            raise ValueError("Позиции относятся к другому заказу")
        now = int(time.time())
        amount = sum(item["unit_price_kopecks"] * item["quantity"] for item in old_items)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO exchanges (order_id, amount_kopecks, reason, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, amount, reason.strip(), actor_id, now),
            )
            exchange_id = int(cursor.lastrowid)
            for old_item in old_items:
                replacement = replacements[int(old_item["id"])]
                connection.execute(
                    "UPDATE order_items SET item_status='exchanged', updated_at=? WHERE id=?",
                    (now, old_item["id"]),
                )
                new_cursor = connection.execute(
                    """
                    INSERT INTO order_items (
                        order_id, product_name, sku, size, quantity,
                        unit_price_kopecks, item_status, original_item_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        order_id, replacement["product_name"], replacement.get("sku", ""),
                        replacement.get("size", ""), old_item["quantity"],
                        old_item["unit_price_kopecks"], old_item["id"], now, now,
                    ),
                )
                connection.execute(
                    "INSERT INTO exchange_items (exchange_id, old_item_id, new_item_id) VALUES (?, ?, ?)",
                    (exchange_id, old_item["id"], int(new_cursor.lastrowid)),
                )
            self._event(
                connection, order_id, "exchange_created", actor_id,
                {"exchange_id": exchange_id, "item_ids": list(replacements), "reason": reason.strip()}, now,
            )
        return exchange_id

    def create_consolidated_exchange(
        self,
        order_id: int,
        old_item_ids: Sequence[int],
        replacement: Dict[str, str],
        actor_id: int,
        reason: str = "",
    ) -> int:
        """Replace multiple old positions with one confirmed replacement item."""
        unique_ids = list(dict.fromkeys(int(value) for value in old_item_ids))
        old_items = self.get_items(unique_ids, active_only=True)
        if not old_items or len(old_items) != len(unique_ids):
            raise ValueError("Некоторые позиции уже обработаны или не найдены")
        if any(int(item["order_id"]) != order_id for item in old_items):
            raise ValueError("Позиции относятся к другому заказу")
        now = int(time.time())
        amount = sum(
            int(item["unit_price_kopecks"]) * int(item["quantity"])
            for item in old_items
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO exchanges (order_id, amount_kopecks, reason, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, amount, reason.strip(), actor_id, now),
            )
            exchange_id = int(cursor.lastrowid)
            for item in old_items:
                connection.execute(
                    "UPDATE order_items SET item_status='exchanged', updated_at=? WHERE id=?",
                    (now, item["id"]),
                )
            new_cursor = connection.execute(
                """
                INSERT INTO order_items (
                    order_id, product_name, sku, size, quantity,
                    unit_price_kopecks, item_status, original_item_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, 'active', ?, ?, ?)
                """,
                (
                    order_id,
                    str(replacement["product_name"]).strip(),
                    str(replacement.get("sku") or "").strip(),
                    str(replacement.get("size") or "").strip(),
                    amount,
                    old_items[0]["id"],
                    now,
                    now,
                ),
            )
            new_item_id = int(new_cursor.lastrowid)
            for item in old_items:
                connection.execute(
                    "INSERT INTO exchange_items (exchange_id, old_item_id, new_item_id) VALUES (?, ?, ?)",
                    (exchange_id, item["id"], new_item_id),
                )
            self._event(
                connection,
                order_id,
                "exchange_created",
                actor_id,
                {
                    "exchange_id": exchange_id,
                    "item_ids": unique_ids,
                    "replacement_item_id": new_item_id,
                    "reason": reason.strip(),
                },
                now,
            )
        return exchange_id

    def create_return(
        self, order_id: int, item_ids: Sequence[int], actor_id: int, reason: str = "",
        amount_kopecks: Optional[int] = None,
    ) -> int:
        items = self.get_items(item_ids, active_only=True)
        if len(items) != len(set(item_ids)):
            raise ValueError("Некоторые позиции уже обработаны или не найдены")
        if any(int(item["order_id"]) != order_id for item in items):
            raise ValueError("Позиции относятся к другому заказу")
        now = int(time.time())
        calculated_amount = sum(
            item["unit_price_kopecks"] * item["quantity"] for item in items
        )
        amount = (
            calculated_amount
            if amount_kopecks is None
            else max(0, int(amount_kopecks))
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO returns (order_id, amount_kopecks, reason, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, amount, reason.strip(), actor_id, now),
            )
            return_id = int(cursor.lastrowid)
            for item in items:
                connection.execute(
                    "UPDATE order_items SET item_status='returned', updated_at=? WHERE id=?",
                    (now, item["id"]),
                )
                connection.execute(
                    "INSERT INTO return_items (return_id, item_id, amount_kopecks) VALUES (?, ?, ?)",
                    (return_id, item["id"], item["unit_price_kopecks"] * item["quantity"]),
                )
            active_count = connection.execute(
                "SELECT COUNT(*) AS count FROM order_items WHERE order_id=? AND item_status='active'",
                (order_id,),
            ).fetchone()["count"]
            connection.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE id=?",
                ("returned" if active_count == 0 else "partially_returned", now, order_id),
            )
            self._event(
                connection, order_id, "return_created", actor_id,
                {"return_id": return_id, "item_ids": list(item_ids), "amount_kopecks": amount, "reason": reason.strip()}, now,
            )
        return return_id

    def create_cancellation(
        self,
        order_id: int,
        item_ids: Sequence[int],
        include_delivery: bool,
        actor_id: int,
        reason: str = "",
        refund_amount_kopecks: Optional[int] = None,
    ) -> Dict[str, int]:
        order = self.get_order(order_id)
        if not order:
            raise ValueError("Заказ не найден")
        unique_ids = list(dict.fromkeys(int(value) for value in item_ids))
        items = self.get_items(unique_ids, active_only=True)
        if not items or len(items) != len(unique_ids):
            raise ValueError("Некоторые позиции уже отменены или не найдены")
        if any(int(item["order_id"]) != order_id for item in items):
            raise ValueError("Позиции относятся к другому заказу")
        items_amount = sum(
            int(item["unit_price_kopecks"]) * int(item["quantity"])
            for item in items
        )
        delivery_amount = (
            int(order.get("delivery_cost_kopecks") or 0) if include_delivery else 0
        )
        total_amount = (
            items_amount + delivery_amount
            if refund_amount_kopecks is None
            else max(0, int(refund_amount_kopecks))
        )
        if refund_amount_kopecks is not None:
            items_amount = max(0, total_amount - delivery_amount)
        now = int(time.time())
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cancellations (
                    order_id, items_amount_kopecks, delivery_amount_kopecks,
                    total_amount_kopecks, reason, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    items_amount,
                    delivery_amount,
                    total_amount,
                    reason.strip(),
                    actor_id,
                    now,
                ),
            )
            cancellation_id = int(cursor.lastrowid)
            for item in items:
                connection.execute(
                    "UPDATE order_items SET item_status='cancelled', updated_at=? WHERE id=?",
                    (now, item["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO cancellation_items (
                        cancellation_id, item_id, amount_kopecks
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        cancellation_id,
                        item["id"],
                        int(item["unit_price_kopecks"]) * int(item["quantity"]),
                    ),
                )
            connection.execute(
                """
                UPDATE orders
                SET status='cancelled', cdek_uuid=NULL, cdek_number=NULL, updated_at=?
                WHERE id=?
                """,
                (now, order_id),
            )
            self._event(
                connection,
                order_id,
                "order_cancelled",
                actor_id,
                {
                    "cancellation_id": cancellation_id,
                    "item_ids": unique_ids,
                    "items_amount_kopecks": items_amount,
                    "delivery_amount_kopecks": delivery_amount,
                    "total_amount_kopecks": total_amount,
                    "track_removed": bool(order.get("cdek_number")),
                    "reason": reason.strip(),
                },
                now,
            )
        return {
            "cancellation_id": cancellation_id,
            "items_amount_kopecks": items_amount,
            "delivery_amount_kopecks": delivery_amount,
            "total_amount_kopecks": total_amount,
        }

    def set_cdek_return_uuid(self, operation: str, operation_id: int, uuid: str) -> None:
        if operation not in {"exchanges", "returns"}:
            raise ValueError("Неизвестная операция")
        with self.connect() as connection:
            connection.execute(
                f"UPDATE {operation} SET cdek_return_uuid=? WHERE id=?", (uuid, operation_id)
            )

    def list_orders_for_cdek_sync(
        self, lookback_days: int = 30, limit: int = 100
    ) -> List[Dict[str, Any]]:
        created_after = int(time.time()) - max(1, lookback_days) * 86400
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, order_number, cdek_uuid, cdek_number, created_at
                FROM orders
                WHERE (cdek_number IS NULL OR TRIM(cdek_number) = '')
                  AND status NOT IN ('returned', 'partially_returned', 'cancelled', 'refund_completed')
                  AND INSTR(COALESCE(delivery_method, ''), 'Самовывоз') = 0
                  AND INSTR(LOWER(COALESCE(delivery_method, '')), 'pickup') = 0
                  AND created_at >= ?
                ORDER BY cdek_last_sync_at ASC, created_at DESC
                LIMIT ?
                """,
                (created_after, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_cdek_sync_attempt(self, order_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE orders SET cdek_last_sync_at=? WHERE id=?",
                (int(time.time()), order_id),
            )

    def save_cdek_order_data(
        self,
        order_id: int,
        cdek_uuid: str,
        cdek_number: str,
        payload: Dict[str, Any],
    ) -> bool:
        """Сохраняет связь с накладной и возвращает True для нового трек-номера."""
        now = int(time.time())
        entity = payload.get("entity") if isinstance(payload, dict) else {}
        entity = entity if isinstance(entity, dict) else {}
        to_location = entity.get("to_location")
        to_location = to_location if isinstance(to_location, dict) else {}
        cdek_address = str(to_location.get("address") or "").strip()
        cdek_city = str(to_location.get("city") or "").strip()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT cdek_uuid, cdek_number FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not current:
                raise ValueError("Заказ не найден")
            old_number = str(current["cdek_number"] or "").strip()
            new_uuid = str(cdek_uuid or current["cdek_uuid"] or "").strip()
            new_number = str(cdek_number or old_number).strip()
            connection.execute(
                """
                UPDATE orders
                SET cdek_uuid = NULLIF(?, ''),
                    cdek_number = NULLIF(?, ''),
                    cdek_payload_json = ?,
                    delivery_address = CASE
                        WHEN TRIM(COALESCE(delivery_address, '')) = '' AND ? <> '' THEN ?
                        ELSE delivery_address
                    END,
                    delivery_city = CASE
                        WHEN TRIM(COALESCE(delivery_city, '')) = '' AND ? <> '' THEN ?
                        ELSE delivery_city
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    new_uuid,
                    new_number,
                    json.dumps(payload, ensure_ascii=False),
                    cdek_address,
                    cdek_address,
                    cdek_city,
                    cdek_city,
                    now,
                    order_id,
                ),
            )
            is_new_number = bool(new_number and new_number != old_number)
            if is_new_number:
                self._event(
                    connection,
                    order_id,
                    "cdek_track_received",
                    None,
                    {"cdek_uuid": new_uuid, "cdek_number": new_number},
                    now,
                )
        return is_new_number

    def clear_cdek_order_data(
        self, order_id: int, actor_id: Optional[int], reason: str
    ) -> None:
        now = int(time.time())
        with self.connect() as connection:
            current = connection.execute(
                "SELECT cdek_uuid, cdek_number FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            if not current:
                raise ValueError("Заказ не найден")
            connection.execute(
                """
                UPDATE orders
                SET cdek_uuid=NULL, cdek_number=NULL, cdek_payload_json='{}',
                    cdek_address_dirty=0, updated_at=?
                WHERE id=?
                """,
                (now, order_id),
            )
            self._event(
                connection,
                order_id,
                "cdek_link_cleared",
                actor_id,
                {
                    "old_uuid": str(current["cdek_uuid"] or ""),
                    "old_number": str(current["cdek_number"] or ""),
                    "reason": str(reason),
                },
                now,
            )

    def save_recreated_cdek_order(
        self,
        order_id: int,
        payload: Dict[str, Any],
        resolved_address: str,
        resolved_city: str,
        actor_id: Optional[int],
    ) -> None:
        entity = payload.get("entity") if isinstance(payload, dict) else {}
        entity = entity if isinstance(entity, dict) else {}
        new_uuid = str(entity.get("uuid") or "").strip()
        new_number = str(entity.get("cdek_number") or "").strip()
        if not new_uuid:
            raise ValueError("СДЭК не вернул UUID новой накладной")
        now = int(time.time())
        with self.connect() as connection:
            old = connection.execute(
                "SELECT cdek_uuid, cdek_number, delivery_address FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if not old:
                raise ValueError("Заказ не найден")
            connection.execute(
                """
                UPDATE orders
                SET cdek_uuid=?, cdek_number=NULLIF(?, ''), cdek_payload_json=?,
                    delivery_address=?, delivery_city=?, cdek_address_dirty=0,
                    cdek_last_sync_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    new_uuid,
                    new_number,
                    json.dumps(payload, ensure_ascii=False),
                    resolved_address.strip(),
                    resolved_city.strip(),
                    now,
                    now,
                    order_id,
                ),
            )
            self._event(
                connection,
                order_id,
                "cdek_order_recreated",
                actor_id,
                {
                    "old_uuid": old["cdek_uuid"],
                    "old_cdek_number": old["cdek_number"],
                    "new_uuid": new_uuid,
                    "new_cdek_number": new_number,
                    "old_address": old["delivery_address"],
                    "new_address": resolved_address.strip(),
                    "city": resolved_city.strip(),
                },
                now,
            )

    def mark_cdek_address_synced(self, order_id: int) -> None:
        """Clear the address-dirty marker after a verified in-place CDEK PATCH."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE orders SET cdek_address_dirty=0 WHERE id=?",
                (int(order_id),),
            )
        if cursor.rowcount != 1:
            raise ValueError("Заказ не найден")

    def dashboard(self) -> Dict[str, Any]:
        with self.connect() as connection:
            base = connection.execute(
                """
                SELECT COUNT(*) AS orders_count,
                       COALESCE(SUM(total_kopecks), 0) AS orders_sum,
                       COALESCE(SUM(CASE WHEN status IN ('returned','partially_returned','cancelled') THEN 1 ELSE 0 END), 0) AS return_or_cancel_count
                FROM orders
                """
            ).fetchone()
            exchanges = connection.execute("SELECT COUNT(*) AS count FROM exchanges").fetchone()["count"]
            returns = connection.execute("SELECT COUNT(*) AS count FROM returns").fetchone()["count"]
            top = connection.execute(
                """
                SELECT product_name, size, SUM(quantity) AS quantity
                FROM order_items
                WHERE original_item_id IS NULL
                GROUP BY product_name, size
                ORDER BY quantity DESC, product_name
                LIMIT 5
                """
            ).fetchall()
        return {
            "orders_count": int(base["orders_count"]),
            "orders_sum": int(base["orders_sum"]),
            "exchange_count": int(exchanges),
            "return_count": int(returns),
            "return_or_cancel_count": int(base["return_or_cancel_count"]),
            "top_items": [dict(row) for row in top],
        }

    def sales_summary(
        self,
        category: str = "all",
        start_at: Optional[int] = None,
        end_at: Optional[int] = None,
    ) -> Dict[str, int]:
        """Return sold-product revenue, excluding delivery and inactive items.

        An order-level discount is allocated proportionally between its active
        product lines. This keeps mixed-category reports additive while delivery
        never leaks into product revenue.
        """
        category_sql, category_parameters = self.sales_category_condition(category)
        time_conditions = []
        parameters: List[Any] = []
        if start_at is not None:
            time_conditions.append("o.created_at >= ?")
            parameters.append(int(start_at))
        if end_at is not None:
            time_conditions.append("o.created_at < ?")
            parameters.append(int(end_at))
        time_sql = " AND " + " AND ".join(time_conditions) if time_conditions else ""
        parameters.extend(category_parameters)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                WITH eligible AS (
                    SELECT i.order_id, i.product_name, i.sku, i.quantity,
                           i.quantity * i.unit_price_kopecks AS gross
                    FROM order_items i
                    JOIN orders o ON o.id=i.order_id
                    WHERE i.item_status='active'
                      AND o.status NOT IN ('new', 'cancelled', 'returned', 'refund_completed')
                      {time_sql}
                ),
                order_gross AS (
                    SELECT order_id, SUM(gross) AS gross
                    FROM eligible GROUP BY order_id
                ),
                selected AS (
                    SELECT e.*, g.gross AS order_gross,
                           o.discount_kopecks
                    FROM eligible e
                    JOIN order_gross g ON g.order_id=e.order_id
                    JOIN orders o ON o.id=e.order_id
                    WHERE {category_sql}
                )
                SELECT COUNT(DISTINCT order_id) AS orders_count,
                       COALESCE(SUM(quantity), 0) AS items_quantity,
                       COALESCE(SUM(gross), 0) AS gross_kopecks,
                       COALESCE(SUM(
                           gross - CASE WHEN order_gross > 0 THEN CAST(
                               MIN(discount_kopecks, order_gross) * gross / order_gross
                               AS INTEGER
                           ) ELSE 0 END
                       ), 0) AS revenue_kopecks
                FROM selected
                """,
                parameters,
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def sales_top_items(
        self,
        category: str = "all",
        start_at: Optional[int] = None,
        end_at: Optional[int] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        category_sql, category_parameters = self.sales_category_condition(category)
        time_conditions = []
        parameters: List[Any] = []
        if start_at is not None:
            time_conditions.append("o.created_at >= ?")
            parameters.append(int(start_at))
        if end_at is not None:
            time_conditions.append("o.created_at < ?")
            parameters.append(int(end_at))
        time_sql = " AND " + " AND ".join(time_conditions) if time_conditions else ""
        parameters.extend(category_parameters)
        parameters.append(max(1, min(int(limit), 100)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                WITH eligible AS (
                    SELECT i.order_id, i.product_name, i.sku, i.size, i.quantity,
                           i.quantity * i.unit_price_kopecks AS gross
                    FROM order_items i
                    JOIN orders o ON o.id=i.order_id
                    WHERE i.item_status='active'
                      AND o.status NOT IN ('new', 'cancelled', 'returned', 'refund_completed')
                      {time_sql}
                ),
                order_gross AS (
                    SELECT order_id, SUM(gross) AS gross
                    FROM eligible GROUP BY order_id
                ),
                selected AS (
                    SELECT e.*, g.gross AS order_gross, o.discount_kopecks
                    FROM eligible e
                    JOIN order_gross g ON g.order_id=e.order_id
                    JOIN orders o ON o.id=e.order_id
                    WHERE {category_sql}
                )
                SELECT product_name, size, SUM(quantity) AS quantity,
                       SUM(gross - CASE WHEN order_gross > 0 THEN CAST(
                           MIN(discount_kopecks, order_gross) * gross / order_gross
                           AS INTEGER
                       ) ELSE 0 END) AS revenue_kopecks
                FROM selected
                GROUP BY product_name, size
                ORDER BY quantity DESC, revenue_kopecks DESC, product_name, size
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_social_followers(
        self,
        platform: str,
        snapshot_date: str,
        followers_count: int,
        source: str,
        captured_at: Optional[int] = None,
    ) -> None:
        platform = str(platform).strip().lower()
        if platform not in {"telegram", "tiktok", "instagram"}:
            raise ValueError("Неизвестная социальная сеть")
        count = int(followers_count)
        if count < 0:
            raise ValueError("Число подписчиков не может быть отрицательным")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO social_follower_snapshots (
                    platform, snapshot_date, followers_count, source, captured_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(platform, snapshot_date) DO UPDATE SET
                    followers_count=excluded.followers_count,
                    source=excluded.source,
                    captured_at=excluded.captured_at
                """,
                (
                    platform,
                    str(snapshot_date),
                    count,
                    str(source),
                    int(captured_at or time.time()),
                ),
            )

    def social_followers_on(
        self, platform: str, snapshot_date: str
    ) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT platform, snapshot_date, followers_count, source, captured_at
                FROM social_follower_snapshots
                WHERE platform=? AND snapshot_date=?
                """,
                (str(platform).strip().lower(), str(snapshot_date)),
            ).fetchone()
        return dict(row) if row else None

    def social_followers_history(
        self, platform: str, end_date: str, limit: int = 35
    ) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT platform, snapshot_date, followers_count, source, captured_at
                FROM social_follower_snapshots
                WHERE platform=? AND snapshot_date<=?
                ORDER BY snapshot_date DESC
                LIMIT ?
                """,
                (
                    str(platform).strip().lower(),
                    str(end_date),
                    max(1, min(int(limit), 366)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def analytics(self) -> Dict[str, Any]:
        with self.connect() as connection:
            orders = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(total_kopecks),0) AS amount FROM orders"
            ).fetchone()
            exchanges = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(amount_kopecks),0) AS amount FROM exchanges"
            ).fetchone()
            returns = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(amount_kopecks),0) AS amount FROM returns"
            ).fetchone()
            top_items = connection.execute(
                """
                SELECT product_name, size, SUM(quantity) AS quantity
                FROM order_items
                WHERE original_item_id IS NULL
                GROUP BY product_name, size
                ORDER BY quantity DESC, product_name, size
                LIMIT 10
                """
            ).fetchall()
            top_cities = connection.execute(
                """
                SELECT TRIM(delivery_city) AS city, COUNT(*) AS count
                FROM orders
                WHERE TRIM(COALESCE(delivery_city, '')) <> ''
                GROUP BY TRIM(delivery_city)
                ORDER BY count DESC, city
                LIMIT 3
                """
            ).fetchall()
            known_city_count = connection.execute(
                "SELECT COUNT(*) AS count FROM orders WHERE TRIM(COALESCE(delivery_city, '')) <> ''"
            ).fetchone()["count"]
            delivery = connection.execute(
                """
                SELECT
                    SUM(CASE
                        WHEN INSTR(COALESCE(delivery_method, ''), 'Самовывоз') > 0
                          OR INSTR(LOWER(COALESCE(delivery_method, '')), 'pickup') > 0
                        THEN 1 ELSE 0 END
                    ) AS pickup_count,
                    SUM(CASE
                        WHEN TRIM(COALESCE(delivery_method, '')) <> ''
                         AND INSTR(COALESCE(delivery_method, ''), 'Самовывоз') = 0
                         AND INSTR(LOWER(COALESCE(delivery_method, '')), 'pickup') = 0
                        THEN 1 ELSE 0 END
                    ) AS delivery_count,
                    SUM(CASE
                        WHEN TRIM(COALESCE(delivery_method, '')) = '' THEN 1 ELSE 0 END
                    ) AS unknown_count,
                    SUM(CASE
                        WHEN UPPER(COALESCE(delivery_method, '')) LIKE '%CDEK%'
                          OR COALESCE(delivery_method, '') LIKE '%СДЭК%'
                        THEN delivery_cost_kopecks ELSE 0 END
                    ) AS cdek_delivery_amount
                FROM orders
                """
            ).fetchone()
            customer_names = connection.execute(
                """
                SELECT customer_name
                FROM orders
                WHERE TRIM(customer_name) <> ''
                GROUP BY CASE
                    WHEN TRIM(phone) <> '' THEN TRIM(phone)
                    ELSE TRIM(customer_name)
                END
                """
            ).fetchall()
        return {
            "orders_count": int(orders["count"]),
            "orders_amount": int(orders["amount"]),
            "exchanges_count": int(exchanges["count"]),
            "exchanges_amount": int(exchanges["amount"]),
            "returns_count": int(returns["count"]),
            "returns_amount": int(returns["amount"]),
            "top_items": [dict(row) for row in top_items],
            "top_cities": [dict(row) for row in top_cities],
            "known_city_count": int(known_city_count),
            "pickup_count": int(delivery["pickup_count"] or 0),
            "delivery_count": int(delivery["delivery_count"] or 0),
            "delivery_unknown_count": int(delivery["unknown_count"] or 0),
            "cdek_delivery_amount": int(delivery["cdek_delivery_amount"] or 0),
            "customer_names": [str(row["customer_name"]) for row in customer_names],
        }

    def _event(
        self, connection: sqlite3.Connection, order_id: int, event_type: str,
        actor_id: Optional[int], details: Dict[str, Any], now: Optional[int] = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO order_events (
                order_id, event_type, actor_telegram_id, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (order_id, event_type, actor_id, json.dumps(details, ensure_ascii=False), now or int(time.time())),
        )
