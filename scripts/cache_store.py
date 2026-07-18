#!/usr/bin/env python3
"""SQLite-backed incremental cache for fund rotation datasets."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


CACHE_SCHEMA_VERSION = 2


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CacheStore:
    def __init__(self, root: Path | str) -> None:
        root = Path(root)
        self.path = root if root.suffix in {".db", ".sqlite", ".sqlite3"} else root / "cache.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=15000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CacheStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterable[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS time_series (
                provider TEXT NOT NULL,
                dataset TEXT NOT NULL,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source_date TEXT,
                fetched_at TEXT NOT NULL,
                quality_hash TEXT NOT NULL,
                PRIMARY KEY (provider, dataset, symbol, trade_date)
            );
            CREATE INDEX IF NOT EXISTS idx_time_series_lookup
                ON time_series(dataset, symbol, trade_date);
            CREATE TABLE IF NOT EXISTS snapshots (
                provider TEXT NOT NULL,
                dataset TEXT NOT NULL,
                scope TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT,
                quality_hash TEXT NOT NULL,
                PRIMARY KEY (provider, dataset, scope, as_of_date)
            );
            CREATE TABLE IF NOT EXISTS fund_profiles (
                provider TEXT NOT NULL,
                fund_code TEXT NOT NULL,
                component TEXT NOT NULL,
                disclosure_date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT,
                quality_hash TEXT NOT NULL,
                PRIMARY KEY (provider, fund_code, component, disclosure_date)
            );
            CREATE TABLE IF NOT EXISTS api_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                dataset TEXT,
                provider TEXT,
                function_name TEXT,
                status TEXT NOT NULL,
                cache_hit INTEGER NOT NULL DEFAULT 0,
                record_count INTEGER NOT NULL DEFAULT 0,
                latency_ms REAL,
                source_date TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weekly_artifacts (
                period_end TEXT NOT NULL,
                holdings_hash TEXT NOT NULL,
                data_revision TEXT NOT NULL,
                completeness TEXT,
                analysis_path TEXT,
                html_path TEXT,
                payload_hash TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (period_end, holdings_hash, data_revision)
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (CACHE_SCHEMA_VERSION, dt.datetime.now().isoformat(timespec="seconds")),
        )
        self.connection.commit()

    @staticmethod
    def _date_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)[:10]
        if len(text) == 8 and text.isdigit():
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        return text

    def upsert_series(
        self,
        provider: str,
        dataset: str,
        symbol: str,
        rows: list[dict[str, Any]],
        *,
        date_keys: tuple[str, ...] = ("trade_date", "日期", "净值日期", "date"),
        source_date: str | None = None,
    ) -> int:
        values = []
        fetched_at = dt.datetime.now().isoformat(timespec="seconds")
        for row in rows:
            raw_date = next((row.get(key) for key in date_keys if row.get(key) is not None), None)
            trade_date = self._date_text(raw_date)
            if not trade_date:
                continue
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            values.append((provider, dataset, str(symbol), trade_date, payload, source_date or trade_date, fetched_at, stable_hash(row)))
        if not values:
            return 0
        with self.transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO time_series(provider, dataset, symbol, trade_date, payload_json, source_date, fetched_at, quality_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, dataset, symbol, trade_date) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    source_date=excluded.source_date,
                    fetched_at=excluded.fetched_at,
                    quality_hash=excluded.quality_hash
                """,
                values,
            )
            return connection.total_changes - before

    def get_series(
        self,
        dataset: str,
        symbol: str,
        *,
        provider: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["dataset=?", "symbol=?"]
        params: list[Any] = [dataset, str(symbol)]
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if start_date:
            clauses.append("trade_date>=?")
            params.append(start_date)
        if end_date:
            clauses.append("trade_date<=?")
            params.append(end_date)
        where = " AND ".join(clauses)
        if provider:
            query = f"SELECT payload_json FROM time_series WHERE {where} ORDER BY trade_date"
        else:
            # A logical series may be recovered by more than one provider. Keep the
            # most recently validated record for each trading day so fallback data
            # cannot inflate sample counts or create duplicate observations.
            query = f"""
                SELECT payload_json FROM (
                    SELECT payload_json, trade_date,
                           ROW_NUMBER() OVER (
                               PARTITION BY trade_date
                               ORDER BY fetched_at DESC, provider ASC
                           ) AS row_rank
                    FROM time_series
                    WHERE {where}
                )
                WHERE row_rank=1
                ORDER BY trade_date
            """
        rows = self.connection.execute(query, params).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def latest_date(self, dataset: str, symbol: str, provider: str | None = None) -> str | None:
        clauses = ["dataset=?", "symbol=?"]
        params: list[Any] = [dataset, str(symbol)]
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        row = self.connection.execute(
            f"SELECT MAX(trade_date) AS latest FROM time_series WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return row["latest"] if row else None

    def list_symbols(self, dataset: str, provider: str | None = None) -> list[str]:
        params: list[Any] = [dataset]
        provider_clause = ""
        if provider:
            provider_clause = " AND provider=?"
            params.append(provider)
        rows = self.connection.execute(
            f"SELECT DISTINCT symbol FROM time_series WHERE dataset=?{provider_clause} ORDER BY symbol",
            params,
        ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def put_snapshot(
        self,
        provider: str,
        dataset: str,
        scope: str,
        as_of_date: str,
        payload: Any,
        *,
        expires_at: str | None = None,
    ) -> None:
        if payload in (None, [], {}):
            return
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO snapshots(provider, dataset, scope, as_of_date, payload_json, fetched_at, expires_at, quality_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, dataset, scope, as_of_date) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    fetched_at=excluded.fetched_at,
                    expires_at=excluded.expires_at,
                    quality_hash=excluded.quality_hash
                """,
                (provider, dataset, scope, as_of_date, encoded, dt.datetime.now().isoformat(timespec="seconds"), expires_at, stable_hash(payload)),
            )

    def get_snapshot(self, dataset: str, scope: str, as_of_date: str, provider: str | None = None) -> Any:
        params: list[Any] = [dataset, scope, as_of_date]
        provider_clause = ""
        if provider:
            provider_clause = " AND provider=?"
            params.append(provider)
        row = self.connection.execute(
            f"SELECT payload_json, expires_at FROM snapshots WHERE dataset=? AND scope=? AND as_of_date=?{provider_clause} ORDER BY fetched_at DESC LIMIT 1",
            params,
        ).fetchone()
        if not row:
            return None
        expires = row["expires_at"]
        if expires:
            try:
                if "T" in str(expires) or " " in str(expires):
                    expires_at = dt.datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                    now = dt.datetime.now(expires_at.tzinfo) if expires_at.tzinfo else dt.datetime.now()
                    if expires_at <= now:
                        return None
                elif self._date_text(expires) < dt.date.today().isoformat():
                    return None
            except ValueError:
                return None
        return json.loads(row["payload_json"])

    def put_profile(self, provider: str, fund_code: str, component: str, disclosure_date: str, payload: Any, expires_at: str | None = None) -> None:
        if payload in (None, [], {}):
            return
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO fund_profiles(provider, fund_code, component, disclosure_date, payload_json, fetched_at, expires_at, quality_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, fund_code, component, disclosure_date) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    fetched_at=excluded.fetched_at,
                    expires_at=excluded.expires_at,
                    quality_hash=excluded.quality_hash
                """,
                (provider, fund_code, component, disclosure_date, encoded, dt.datetime.now().isoformat(timespec="seconds"), expires_at, stable_hash(payload)),
            )

    def record_audit(self, run_id: str, rows: list[dict[str, Any]]) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        values = []
        for row in rows:
            values.append((
                run_id,
                row.get("dataset") or row.get("label"),
                row.get("provider"),
                row.get("function") or row.get("resolved_by"),
                row.get("status") or "unknown",
                1 if row.get("cache_hit") else 0,
                int(row.get("record_count") or 0),
                row.get("latency_ms"),
                row.get("source_date"),
                json.dumps(row.get("reason"), ensure_ascii=False, default=str) if row.get("reason") else None,
                now,
            ))
        if values:
            with self.transaction() as connection:
                connection.executemany(
                    """INSERT INTO api_audit(run_id,dataset,provider,function_name,status,cache_hit,record_count,latency_ms,source_date,reason,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )

    def cache_stats(self, run_id: str | None = None) -> dict[str, Any]:
        where = " WHERE run_id=?" if run_id else ""
        params = [run_id] if run_id else []
        row = self.connection.execute(
            f"""SELECT COUNT(*) AS calls, SUM(cache_hit) AS hits,
                       SUM(CASE WHEN status IN ('ok','fallback_used') THEN 1 ELSE 0 END) AS successful
                FROM api_audit{where}""",
            params,
        ).fetchone()
        calls = int(row["calls"] or 0)
        hits = int(row["hits"] or 0)
        return {
            "calls": calls,
            "hits": hits,
            "hit_rate": hits / calls if calls else None,
            "cacheable_calls": calls,
            "cache_hits": hits,
            "cache_hit_rate": hits / calls if calls else None,
            "successful_calls": int(row["successful"] or 0),
            "database": str(self.path),
        }

    def register_artifact(self, period_end: str, holdings_hash: str, data_revision: str, *, completeness: str | None = None, analysis_path: str | None = None, html_path: str | None = None, payload: Any = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO weekly_artifacts(period_end,holdings_hash,data_revision,completeness,analysis_path,html_path,payload_hash,created_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(period_end,holdings_hash,data_revision) DO UPDATE SET
                    completeness=excluded.completeness,
                    analysis_path=COALESCE(excluded.analysis_path,weekly_artifacts.analysis_path),
                    html_path=COALESCE(excluded.html_path,weekly_artifacts.html_path),
                    payload_hash=COALESCE(excluded.payload_hash,weekly_artifacts.payload_hash),
                    created_at=excluded.created_at
                """,
                (period_end, holdings_hash, data_revision, completeness, analysis_path, html_path, stable_hash(payload) if payload is not None else None, dt.datetime.now().isoformat(timespec="seconds")),
            )
