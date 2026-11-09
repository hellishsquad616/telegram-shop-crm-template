"""Persistent report categories matched against Tilda order names/externalid."""
import json
import time
from typing import Any, Dict, List, Optional, Sequence


DEFAULT_CATEGORIES = (
    ("puffer", "🧥 Пуховики", ["PUFFER", "пуховик"]),
    ("windbreaker", "🌬 Ветровки", ["WINDBREAKER", "WIND BREAKER", "ветровк"]),
    ("ziphoodie", "🦌 Зип-худи демозип", ["DEMOZIP", "DEMOZIP", "демозип", "демозип", "демозип"]),
)
LEGACY_CATEGORIES = (
    ("longsleeve", "👕 Лонгсливы", ["BASE", "CRYSTAL", "LONGSLEEVE", "лонгслив"]),
)
MODEL_LABELS = {
    "puffer": "DEMO PUFFERS",
    "ziphoodie": "DEMOZIP ZIP",
    "windbreaker": "DEMO WINDBREAKER",
}


def validate_rules(rules: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    if not rules or len(rules) > 30:
        raise ValueError("Укажите от 1 до 30 правил.")
    result = []
    for rule in rules:
        kind = str(rule.get("kind", ""))
        value = str(rule.get("value", "")).strip()
        if kind not in {"contains", "name", "sku"} or not value or len(value) > 200:
            raise ValueError("Правило: часть названия, name:полное название или sku:артикул (до 200 символов).")
        normalized = {"kind": kind, "value": value}
        if normalized not in result:
            result.append(normalized)
    return result


def parse_rules(text: str) -> List[Dict[str, str]]:
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        prefix, separator, value = line.partition(":")
        kind = prefix.lower() if separator and prefix.lower() in {"name", "sku"} else "contains"
        rules.append({"kind": kind, "value": value if kind != "contains" else line})
    return validate_rules(rules)


class SalesCategories:
    def initialize_sales_categories(self, connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS sales_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                rules_json TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        now = int(time.time())
        for key, label, terms in DEFAULT_CATEGORIES + LEGACY_CATEGORIES:
            connection.execute("""
                INSERT OR IGNORE INTO sales_categories
                    (key, label, rules_json, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (key, label, json.dumps([{"kind": "contains", "value": term} for term in terms], ensure_ascii=False), int(key != "longsleeve"), now, now))
        # Apply the requested menu change once; keep historical orders and rules.
        if not connection.execute("SELECT 1 FROM app_settings WHERE key='retired_longsleeve_v1'").fetchone():
            connection.execute("UPDATE sales_categories SET is_active=0, updated_at=? WHERE key='longsleeve'", (now,))
            connection.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('retired_longsleeve_v1', '1', ?)", (now,))

    def sales_model_breakdown(self, start_at: int, end_at: int) -> List[Dict[str, Any]]:
        """Allocate each sold line to one model, across all colors and sizes."""
        categories = self.list_sales_categories()
        categories.sort(key=lambda entry: (entry["key"] not in MODEL_LABELS, entry["id"]))
        cases, rule_parameters = [], []
        for category in categories:
            condition, parameters = self.sales_category_condition(category["key"])
            cases.append(f"WHEN {condition} THEN ?")
            rule_parameters.extend(parameters)
            rule_parameters.append(MODEL_LABELS.get(category["key"], category["label"]))
        model_sql = "CASE " + " ".join(cases) + " ELSE 'Другие изделия' END" if cases else "'Другие изделия'"
        with self.connect() as connection:
            rows = connection.execute(f"""
                WITH eligible AS (
                    SELECT i.order_id, i.product_name, i.sku, i.quantity,
                           i.quantity * i.unit_price_kopecks AS gross
                    FROM order_items i JOIN orders o ON o.id=i.order_id
                    WHERE i.item_status='active'
                      AND o.status NOT IN ('new', 'cancelled', 'returned', 'refund_completed')
                      AND o.created_at >= ? AND o.created_at < ?
                ), order_gross AS (
                    SELECT order_id, SUM(gross) AS gross FROM eligible GROUP BY order_id
                ), classified AS (
                    SELECT {model_sql} AS model, e.quantity,
                           e.gross - CASE WHEN g.gross > 0 THEN CAST(
                               MIN(o.discount_kopecks, g.gross) * e.gross / g.gross AS INTEGER
                           ) ELSE 0 END AS revenue
                    FROM eligible e JOIN order_gross g ON g.order_id=e.order_id
                    JOIN orders o ON o.id=e.order_id
                )
                SELECT model, SUM(quantity) AS quantity, SUM(revenue) AS revenue_kopecks
                FROM classified GROUP BY model ORDER BY quantity DESC, model
            """, [int(start_at), int(end_at), *rule_parameters]).fetchall()
        return [dict(row) for row in rows]

    def list_sales_categories(self, include_hidden: bool = False) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sales_categories "
                + ("" if include_hidden else "WHERE is_active=1 ") + "ORDER BY id"
            ).fetchall()
        return [self._category_row(row) for row in rows]

    @staticmethod
    def _category_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["rules"] = json.loads(value.pop("rules_json"))
        return value

    def get_sales_category(self, category: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM sales_categories WHERE key=?", (category,)).fetchone()
        return self._category_row(row) if row else None

    def save_sales_category(self, label: str, rules: Sequence[Dict[str, str]], key: Optional[str] = None) -> str:
        label = label.strip()
        if not label or len(label) > 60:
            raise ValueError("Название должно содержать от 1 до 60 символов.")
        normalized = validate_rules(rules)
        now = int(time.time())
        with self.connect() as connection:
            if key is None:
                if connection.execute("SELECT COUNT(*) FROM sales_categories").fetchone()[0] >= 50:
                    raise ValueError("Можно создать до 50 категорий. Измените существующую.")
                # Generate a short callback-safe key using the persistent row ID.
                cursor = connection.execute("""
                    INSERT INTO sales_categories (key, label, rules_json, created_at, updated_at)
                    VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?)
                """, (label, json.dumps(normalized, ensure_ascii=False), now, now))
                key = f"c{cursor.lastrowid}"
                connection.execute("UPDATE sales_categories SET key=? WHERE id=?", (key, cursor.lastrowid))
            else:
                cursor = connection.execute("""
                    UPDATE sales_categories SET label=?, rules_json=?, updated_at=? WHERE key=?
                """, (label, json.dumps(normalized, ensure_ascii=False), now, key))
                if not cursor.rowcount:
                    raise ValueError("Категория не найдена.")
        return key

    def set_sales_category_active(self, key: str, active: bool) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE sales_categories SET is_active=?, updated_at=? WHERE key=?",
                (int(active), int(time.time()), key),
            )
            if not cursor.rowcount:
                raise ValueError("Категория не найдена.")

    def sales_category_condition(self, category: str, alias: str = "e"):
        if category == "all":
            return "1=1", []
        entry = self.get_sales_report_group(category)
        if not entry or not entry["is_active"]:
            raise ValueError("Категория скрыта или не найдена.")
        conditions, parameters = [], []
        for rule in entry["rules"]:
            column = "sku" if rule["kind"] == "sku" else "product_name"
            expression = f"CASEFOLD({alias}.{column})"
            conditions.append(
                f"INSTR({expression}, ?) > 0" if rule["kind"] == "contains" else f"{expression} = ?"
            )
            parameters.append(rule["value"].casefold())
        return "(" + " OR ".join(conditions) + ")", parameters

    def get_sales_report_group(self, key: str) -> Optional[Dict[str, Any]]:
        """Resolve a saved category or an exact product without saving settings.

        Product buttons use a persistent order-item ID rather than a transient
        catalog index. Sizes and SKUs with the same product name are grouped.
        """
        if key.startswith("i") and key[1:].isdigit():
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT product_name FROM order_items WHERE id=?",
                    (int(key[1:]),),
                ).fetchone()
            if not row:
                return None
            return {
                "key": key, "label": row["product_name"], "is_active": 1,
                "rules": [{"kind": "name", "value": row["product_name"]}],
            }
        return self.get_sales_category(key)

    def sales_products_catalog(self, query: str = "", limit: int = 9, offset: int = 0) -> List[Dict[str, Any]]:
        """Products received in orders, grouped across sizes and SKU variants."""
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT MIN(id) AS id, product_name
                FROM order_items
                WHERE TRIM(product_name) <> ''
                  AND (INSTR(CASEFOLD(product_name), ?) > 0 OR INSTR(CASEFOLD(sku), ?) > 0)
                GROUP BY CASEFOLD(product_name)
                ORDER BY CASEFOLD(product_name), MIN(id)
                LIMIT ? OFFSET ?
            """, (query.strip().casefold(), query.strip().casefold(),
                  max(1, min(int(limit), 50)), max(0, int(offset)))).fetchall()
        return [dict(row) for row in rows]

    def sales_category_orders(self, category: str, start_at=None, end_at=None, limit: int = 10, offset: int = 0):
        condition, parameters = self.sales_category_condition(category, "i")
        period = []
        if start_at is not None:
            period.append("o.created_at >= ?")
            parameters.append(int(start_at))
        if end_at is not None:
            period.append("o.created_at < ?")
            parameters.append(int(end_at))
        period_sql = " AND " + " AND ".join(period) if period else ""
        with self.connect() as connection:
            rows = connection.execute(f"""
                SELECT o.* FROM orders o
                WHERE o.status NOT IN ('new', 'cancelled', 'returned', 'refund_completed')
                AND EXISTS (SELECT 1 FROM order_items i WHERE i.order_id=o.id
                    AND i.item_status='active' AND {condition}) {period_sql}
                ORDER BY o.created_at DESC, o.id DESC LIMIT ? OFFSET ?
            """, [*parameters, max(1, min(limit, 50)), max(0, offset)]).fetchall()
        return [dict(row) for row in rows]

    def tilda_sales_catalog(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT i.product_name, i.sku, COUNT(*) AS uses
                FROM order_items i JOIN orders o ON o.id=i.order_id
                WHERE o.source='tilda'
                GROUP BY i.product_name, i.sku ORDER BY uses DESC, i.product_name, i.sku
            """).fetchall()
        return [dict(row) for row in rows]
