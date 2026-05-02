"""
v1.6.0+: Клиент к БД Респект.Чата (read-only) с защитой от NULL/скалярных полей.

Тонкая обёртка вокруг двух PG-функций:
- kb_get_accessible_content_ids(matrix_user_id) → BIGINT[]
- kb_get_next_content() → таблица записей, или 0 строк когда данные закончились

Подключение через отдельный asyncpg pool: DSN в env RESPECT_DATABASE_URL.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import asyncpg


log = logging.getLogger("respect_db")


def _normalize_section_path(value: Any) -> list[str]:
    """Нормализовать section_path к list[str].

    Респект.Чата может вернуть: list, None, scalar string, JSON-строку.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        # Может быть JSON
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed if v is not None]
            except json.JSONDecodeError:
                pass
        # Скаляр — оборачиваем
        return [s]
    return [str(value)]


def _normalize_attachments(value: Any) -> list[dict]:
    """Нормализовать attachments к list[dict].

    Принимает: list, dict (один объект), JSON-строку, None.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [a for a in value if isinstance(a, dict)]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() == "null":
            return []
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError as e:
            log.warning("attachments JSON decode failed: %s", e)
            return []
        if isinstance(parsed, list):
            return [a for a in parsed if isinstance(a, dict)]
        if isinstance(parsed, dict):
            return [parsed]
        return []
    log.warning("attachments unexpected type: %r", type(value))
    return []


def _normalize_row(row: dict) -> dict:
    """Привести asyncpg-row (dict) к нашему ожидаемому формату.

    Защищает от:
    - section_path: NULL или скаляр → list[str]
    - attachments: NULL, dict, JSON-строка → list[dict]
    - is_deleted: NULL → False
    - title/body_html: NULL → пустая строка
    """
    return {
        "content_id":      row.get("content_id"),
        "title":           row.get("title") or "",
        "body_html":       row.get("body_html") or "",
        "section_path":    _normalize_section_path(row.get("section_path")),
        "cover_image_url": row.get("cover_image_url"),
        "attachments":     _normalize_attachments(row.get("attachments")),
        "actualized_at":   row.get("actualized_at"),
        "updated_at":      row.get("updated_at"),
        "is_deleted":      bool(row.get("is_deleted")) if row.get("is_deleted") is not None else False,
    }


class RespectDBClient:
    """Read-only клиент к БД Респект.Чата."""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 4) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self._pool: asyncpg.Pool | None = None

    @classmethod
    def from_env(cls) -> "RespectDBClient":
        dsn = os.environ.get("RESPECT_DATABASE_URL")
        if not dsn:
            raise RuntimeError(
                "RESPECT_DATABASE_URL not set in env. "
                "Required for v1.6.0 KB sync."
            )
        return cls(dsn)

    async def __aenter__(self) -> "RespectDBClient":
        self._pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("RespectDBClient must be used inside `async with`")
        return self._pool

    # -------------------------------------------------------------------------
    async def get_accessible_content_ids(self, matrix_user_id: str) -> list[int]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT kb_get_accessible_content_ids($1) AS ids",
                matrix_user_id,
            )
            ids = row["ids"] if row else None
            if not ids:
                return []
            return list(ids)

    # -------------------------------------------------------------------------
    async def get_next_content_batch(self) -> list[dict]:
        """Получить следующую порцию записей.

        v1.6.0+: запрос приводит массивные/JSONB поля к TEXT через cast,
        чтобы избежать ошибок asyncpg на стадии bind_execute если функция
        вернула что-то нестандартное. Парсим в Python через _normalize_*.
        """
        # cast на стороне SQL: section_path → TEXT (как Postgres-литерал массива),
        # attachments → TEXT (JSON-строка). Это безопаснее, чем доверять auto-mapping
        # asyncpg, который ломается на скалярах в TEXT[]-столбцах.
        sql = """
            SELECT
                content_id,
                title,
                body_html,
                section_path::text   AS section_path,
                cover_image_url,
                attachments::text    AS attachments,
                actualized_at,
                updated_at,
                is_deleted
            FROM kb_get_next_content()
        """
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(sql)
            except Exception as e:
                log.exception("kb_get_next_content() failed: %s", e)
                raise

        normalized: list[dict] = []
        for r in rows:
            d = dict(r)
            # section_path сейчас TEXT в формате '{a,b,c}' — парсим
            sp = d.get("section_path")
            if isinstance(sp, str):
                d["section_path"] = _parse_pg_array(sp)
            # attachments — TEXT с JSON
            d["attachments"] = _normalize_attachments(d.get("attachments"))
            normalized.append(_normalize_row(d))
        return normalized


def _parse_pg_array(s: str) -> list[str]:
    """Парсить строковое представление PG-массива.

    Postgres TEXT[] cast в TEXT даёт что-то вроде:
        {КОНКУРЕНТЫ,Анализ,"Сравнение с Гарантом"}
        {NULL}
        NULL                ← если массив был NULL
        {}                  ← если массив был пустой
    """
    if not s or s.lower() in ("null", "{}"):
        return []
    s = s.strip()
    if not (s.startswith("{") and s.endswith("}")):
        # Что-то странное — возвращаем как single-element
        return [s]
    inner = s[1:-1]
    if not inner:
        return []
    # Простой парсер: разделяем по запятым, учитываем кавычки
    result: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == '"' and (i == 0 or inner[i-1] != "\\"):
            in_quotes = not in_quotes
            i += 1
            continue
        if ch == "," and not in_quotes:
            item = "".join(buf).strip()
            if item.upper() != "NULL":
                # Снимаем кавычки если были по краям
                if item.startswith('"') and item.endswith('"'):
                    item = item[1:-1]
                result.append(item)
            buf = []
            i += 1
            continue
        if ch == "\\" and i + 1 < len(inner):
            buf.append(inner[i+1])
            i += 2
            continue
        buf.append(ch)
        i += 1
    # Последний элемент
    item = "".join(buf).strip()
    if item.upper() != "NULL":
        if item.startswith('"') and item.endswith('"'):
            item = item[1:-1]
        result.append(item)
    return result


# -----------------------------------------------------------------------------
_shared_client: RespectDBClient | None = None


@asynccontextmanager
async def shared_respect_db():
    global _shared_client
    if _shared_client is None:
        _shared_client = RespectDBClient.from_env()
        await _shared_client.__aenter__()
    try:
        yield _shared_client
    finally:
        pass


async def close_shared_respect_db() -> None:
    global _shared_client
    if _shared_client is not None:
        await _shared_client.__aexit__(None, None, None)
        _shared_client = None
