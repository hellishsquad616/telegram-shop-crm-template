"""Independent durable refund journal; never touches CRM order data."""
import sqlite3

def connect_database(path):
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
