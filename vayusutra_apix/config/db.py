"""
VayuSutra APIx - High-Performance SQLite WAL Database Layer
Thread-safe connection pooling, schema migrations, and batch operations.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator, List, Dict, Any, Optional

# Default database path inside project data directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "vayusutra_airfare.db")

_thread_local = threading.local()


class DatabaseManager:
    """Manages SQLite WAL-mode connections with proper concurrency and pragma settings."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        """Get or create a thread-local SQLite connection."""
        if not hasattr(_thread_local, "connection") or _thread_local.connection is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
            )
            conn.row_factory = sqlite3.Row
            # Enable WAL mode and performance pragmas
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA cache_size=-64000;")  # 64MB memory cache
            _thread_local.connection = conn
        return _thread_local.connection

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for atomic transaction block."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

    def init_db(self) -> None:
        """Initialize database schema if not already present."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            cursor = conn.cursor()

            # 1. Raw flight quotes ingested from scrapers / market feeds
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS raw_quotes (
                    quote_id TEXT PRIMARY KEY,
                    route_code TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    airline_code TEXT NOT NULL,
                    airline_name TEXT NOT NULL,
                    flight_number TEXT NOT NULL,
                    source_portal TEXT NOT NULL,
                    booking_date TEXT NOT NULL,
                    travel_date TEXT NOT NULL,
                    advance_window TEXT NOT NULL,
                    departure_time TEXT NOT NULL,
                    arrival_time TEXT NOT NULL,
                    base_fare REAL NOT NULL,
                    fuel_surcharge REAL NOT NULL,
                    udf REAL NOT NULL,
                    psf REAL NOT NULL,
                    asf REAL NOT NULL,
                    gst REAL NOT NULL,
                    convenience_fee REAL NOT NULL,
                    total_fare REAL NOT NULL,
                    is_direct INTEGER NOT NULL DEFAULT 1,
                    currency TEXT NOT NULL DEFAULT 'INR',
                    scraped_at TEXT NOT NULL
                );
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_raw_route_date_win 
                ON raw_quotes(route_code, booking_date, advance_window);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_raw_flight_travel 
                ON raw_quotes(flight_number, travel_date);
            """)

            # 2. Cleaned and normalized quotes after outlier rejection & deduplication
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cleaned_quotes (
                    cleaned_id TEXT PRIMARY KEY,
                    raw_quote_id TEXT NOT NULL,
                    route_code TEXT NOT NULL,
                    advance_window TEXT NOT NULL,
                    booking_date TEXT NOT NULL,
                    travel_date TEXT NOT NULL,
                    airline_code TEXT NOT NULL,
                    flight_number TEXT NOT NULL,
                    final_base_fare REAL NOT NULL,
                    final_tax_fee REAL NOT NULL,
                    final_total_fare REAL NOT NULL,
                    outlier_flag INTEGER NOT NULL DEFAULT 0,
                    outlier_reason TEXT,
                    deduplication_kept INTEGER NOT NULL DEFAULT 1,
                    cleaned_at TEXT NOT NULL,
                    FOREIGN KEY (raw_quote_id) REFERENCES raw_quotes(quote_id)
                );
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_cleaned_route_win_date 
                ON cleaned_quotes(route_code, advance_window, booking_date, outlier_flag);
            """)

            # 3. Route elementary indices (Jevons geometric means & price relatives)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS route_indices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calculation_date TEXT NOT NULL,
                    route_code TEXT NOT NULL,
                    advance_window TEXT NOT NULL,
                    sample_size INTEGER NOT NULL,
                    jevons_mean_fare REAL NOT NULL,
                    base_benchmark_fare REAL NOT NULL,
                    price_relative REAL NOT NULL,
                    composite_route_relative REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(calculation_date, route_code, advance_window)
                );
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_route_calc_date 
                ON route_indices(calculation_date, route_code);
            """)

            # 4. National Airfare Price Index & CPI Transmission metrics
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS national_indices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calculation_date TEXT UNIQUE NOT NULL,
                    laspeyres_index REAL NOT NULL,
                    paasche_index REAL NOT NULL,
                    fisher_index REAL NOT NULL,
                    jevons_index REAL NOT NULL,
                    spot_t1_index REAL NOT NULL,
                    daily_pct_change REAL NOT NULL,
                    bps_transport_impact REAL NOT NULL,
                    bps_headline_cpi_impact REAL NOT NULL,
                    observations_count INTEGER NOT NULL,
                    valid_quotes_count INTEGER NOT NULL,
                    outliers_rejected_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_national_calc_date 
                ON national_indices(calculation_date);
            """)

            # 5. DGCA Backtest Validation Records
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS backtest_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_date TEXT NOT NULL,
                    pearson_r REAL NOT NULL,
                    mape REAL NOT NULL,
                    rmse REAL NOT NULL,
                    r2 REAL NOT NULL,
                    sample_days INTEGER NOT NULL,
                    total_quotes_evaluated INTEGER NOT NULL,
                    report_path TEXT NOT NULL,
                    generated_at TEXT NOT NULL
                );
            """)

            conn.commit()
            conn.close()


# Global instance
db_manager = DatabaseManager()


def get_db_connection() -> sqlite3.Connection:
    """Convenience helper to get current thread's connection."""
    return db_manager.get_connection()


def init_db(db_path: str = DB_PATH) -> None:
    """Explicitly initialize schema."""
    global db_manager
    db_manager = DatabaseManager(db_path)
