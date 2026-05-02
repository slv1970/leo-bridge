"""
v1.6.0+ patch2: Цикл синхронизации с предварительной фильтрацией по типу файла.

Изменения:
- БЕЗ скачивания для не-parseable типов (video/audio/image/doc/ppt/zip/etc).
  Записываем только метаданные + ссылку, файл не качаем.
- Парсимые форматы: pdf/docx/txt/md/html/rtf/pptx/xlsx/xls/csv —
  скачиваем, считаем sha256, дедупим, парсим.
- Per-record error handling сохранено.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx

from app.attachment_parser import (
    PARSEABLE_TYPES,
    get_file_type,
    is_parseable,
    parse_blob,
)
from app.html_utils import html_to_plain
from app.respect_db import RespectDBClient


log = logging.getLogger("respect_kb_sync")

ADVISORY_LOCK_KEY = 0x16016016

MAX_PARSE_SIZE = int(os.environ.get("RESPECT_KB_MAX_PARSE_SIZE_MB", "50")) * 1024 * 1024
DOWNLOAD_TIMEOUT = float(os.environ.get("RESPECT_KB_DOWNLOAD_TIMEOUT_S", "60"))
MAX_BATCHES_PER_RUN = int(os.environ.get("RESPECT_KB_MAX_BATCHES_PER_RUN", "10000"))


@dataclass
class SyncStats:
    rows_upserted: int = 0
    rows_deleted: int = 0
    rows_skipped: int = 0
    attachments_new: int = 0          # новые parseable файлы (скачаны+распарсены)
    attachments_cached: int = 0       # parseable, найденные в кеше по sha256
    attachments_link_only: int = 0    # не-parseable: только ссылка, без скачивания
    batches: int = 0


async def _download(url: str, http_client: httpx.AsyncClient) -> bytes | None:
    try:
        r = await http_client.get(url, timeout=DOWNLOAD_TIMEOUT)
        if r.status_code != 200:
            log.warning("download non-200: %s → %s", url, r.status_code)
            return None
        return r.content
    except Exception as e:
        log.warning("download failed: %s → %s", url, e)
        return None


def _url_pseudo_sha(url: str) -> str:
    """Псевдо-sha256 для не-parseable файлов: префикс url: + хеш URL.

    Это НЕ настоящий sha256 содержимого — мы файл не качали.
    Префикс отличает такие записи от реальных sha256 (64 hex без префикса).
    Длина: 4 + 64 = 68 chars.
    """
    return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()


async def _process_attachments(
    *,
    leo_conn: asyncpg.Connection,
    attachments: list[dict],
    http_client: httpx.AsyncClient,
    stats: SyncStats,
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Обработать список attachments.

    Алгоритм:
    1. Определить тип каждого файла по имени/declared_type.
    2. Если тип НЕ parseable — записать только метаданные и ссылку, без скачивания.
    3. Если parseable — скачать, sha256, дедуп, парсить.
    """
    texts: list[str] = []
    links: list[tuple[str, str, str]] = []

    for att in attachments:
        url = att.get("url")
        name = att.get("name") or "unnamed"
        if not url:
            continue

        declared_type = att.get("type")
        ftype = get_file_type(name, declared_type)

        if not is_parseable(ftype):
            # ──── Ветка БЕЗ скачивания ────
            sha = _url_pseudo_sha(url)
            size_hint = att.get("size") if isinstance(att.get("size"), int) else None

            await leo_conn.execute(
                """
                INSERT INTO ai.respect_kb_attachments
                    (sha256, url, file_type, file_name, file_size, parsed_text, parse_error)
                VALUES ($1, $2, $3, $4, $5, NULL, NULL)
                ON CONFLICT (sha256) DO UPDATE SET
                    url = EXCLUDED.url,
                    file_name = EXCLUDED.file_name,
                    file_size = COALESCE(EXCLUDED.file_size, ai.respect_kb_attachments.file_size)
                """,
                sha, url, ftype, name, size_hint,
            )
            stats.attachments_link_only += 1
            links.append((sha, url, name))
            continue

        # ──── Ветка С скачиванием (parseable) ────
        blob = await _download(url, http_client)
        if blob is None:
            # Не получилось скачать — пропускаем этот attachment
            continue

        actual_size = len(blob)
        sha = hashlib.sha256(blob).hexdigest()

        cached = await leo_conn.fetchrow(
            "SELECT parsed_text FROM ai.respect_kb_attachments WHERE sha256 = $1",
            sha,
        )

        if cached is not None:
            stats.attachments_cached += 1
            if cached["parsed_text"]:
                texts.append(cached["parsed_text"])
            await leo_conn.execute(
                """
                UPDATE ai.respect_kb_attachments
                   SET url = $2, file_name = $3, file_size = $4
                 WHERE sha256 = $1
                """,
                sha, url, name, actual_size,
            )
        else:
            stats.attachments_new += 1
            parsed_text: str | None = None
            parse_error: str | None = None

            if actual_size > MAX_PARSE_SIZE:
                parse_error = f"file too large: {actual_size} bytes (limit {MAX_PARSE_SIZE})"
                log.info("skip parse (too large): %s sha=%s size=%d", name, sha[:8], actual_size)
            else:
                try:
                    parsed_text = parse_blob(blob, ftype)
                    if parsed_text:
                        texts.append(parsed_text)
                except Exception as e:
                    parse_error = f"{type(e).__name__}: {e}"
                    log.warning("parse failed: %s ftype=%s err=%s", name, ftype, e)

            await leo_conn.execute(
                """
                INSERT INTO ai.respect_kb_attachments
                    (sha256, url, file_type, file_name, file_size, parsed_text, parse_error)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (sha256) DO NOTHING
                """,
                sha, url, ftype, name, actual_size, parsed_text, parse_error,
            )

        links.append((sha, url, name))

    return texts, links


async def _upsert_content(
    *,
    leo_conn: asyncpg.Connection,
    row: dict,
    http_client: httpx.AsyncClient,
    stats: SyncStats,
) -> None:
    cid = row["content_id"]
    title = row.get("title") or ""
    body_html = row.get("body_html") or ""

    body_plain = html_to_plain(body_html)

    texts, links = await _process_attachments(
        leo_conn=leo_conn,
        attachments=row.get("attachments") or [],
        http_client=http_client,
        stats=stats,
    )

    indexable_parts = [body_plain] + texts
    indexable_text = "\n\n".join(p for p in indexable_parts if p).strip()

    await leo_conn.execute(
        """
        INSERT INTO ai.respect_kb (
            content_id, title, body_html, body_plain, indexable_text,
            section_path, cover_image_url, actualized_at, updated_at, synced_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
        ON CONFLICT (content_id) DO UPDATE SET
            title           = EXCLUDED.title,
            body_html       = EXCLUDED.body_html,
            body_plain      = EXCLUDED.body_plain,
            indexable_text  = EXCLUDED.indexable_text,
            section_path    = EXCLUDED.section_path,
            cover_image_url = EXCLUDED.cover_image_url,
            actualized_at   = EXCLUDED.actualized_at,
            updated_at      = EXCLUDED.updated_at,
            synced_at       = now()
        """,
        cid, title, body_html, body_plain, indexable_text,
        row.get("section_path") or [],
        row.get("cover_image_url"),
        row.get("actualized_at"),
        row.get("updated_at") or datetime.now(timezone.utc),
    )

    await leo_conn.execute(
        "DELETE FROM ai.respect_kb_content_attachments WHERE content_id = $1",
        cid,
    )
    if links:
        await leo_conn.executemany(
            """
            INSERT INTO ai.respect_kb_content_attachments
                (content_id, sha256, display_url, display_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT DO NOTHING
            """,
            [(cid, sha, url, name) for sha, url, name in links],
        )

    stats.rows_upserted += 1


async def run_sync_cycle(
    *,
    leo_pool: asyncpg.Pool,
    respect_client: RespectDBClient,
) -> SyncStats:
    stats = SyncStats()

    async with leo_pool.acquire() as conn:
        log_id = await conn.fetchval(
            "INSERT INTO ai.respect_kb_sync_log (status) VALUES ('running') RETURNING id",
        )

    error_message: str | None = None
    try:
        async with httpx.AsyncClient(follow_redirects=True) as http_client:
            while stats.batches < MAX_BATCHES_PER_RUN:
                rows = await respect_client.get_next_content_batch()
                if not rows:
                    break

                stats.batches += 1
                for row in rows:
                    cid = row.get("content_id")
                    if cid is None:
                        stats.rows_skipped += 1
                        continue
                    try:
                        async with leo_pool.acquire() as conn:
                            async with conn.transaction():
                                if row.get("is_deleted"):
                                    result = await conn.execute(
                                        "DELETE FROM ai.respect_kb WHERE content_id = $1",
                                        cid,
                                    )
                                    if result.endswith(" 1"):
                                        stats.rows_deleted += 1
                                else:
                                    await _upsert_content(
                                        leo_conn=conn,
                                        row=row,
                                        http_client=http_client,
                                        stats=stats,
                                    )
                    except Exception as e:
                        stats.rows_skipped += 1
                        log.warning("skip content_id=%s: %s: %s", cid, type(e).__name__, e)

                log.info(
                    "batch #%d: rows=%d upserted=%d deleted=%d skipped=%d "
                    "att_new=%d att_cached=%d att_link_only=%d",
                    stats.batches, len(rows),
                    stats.rows_upserted, stats.rows_deleted, stats.rows_skipped,
                    stats.attachments_new, stats.attachments_cached, stats.attachments_link_only,
                )

            if stats.batches >= MAX_BATCHES_PER_RUN:
                error_message = f"hit MAX_BATCHES_PER_RUN={MAX_BATCHES_PER_RUN}"
                log.warning(error_message)

    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        log.exception("sync cycle failed: %s", e)
    finally:
        async with leo_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ai.respect_kb_sync_log
                   SET finished_at = now(),
                       status = $2,
                       rows_upserted = $3,
                       rows_deleted = $4,
                       attachments_new = $5,
                       attachments_cached = $6,
                       error_message = $7
                 WHERE id = $1
                """,
                log_id,
                "ok" if error_message is None else "error",
                stats.rows_upserted,
                stats.rows_deleted,
                stats.attachments_new,
                stats.attachments_cached,
                error_message,
            )

    if error_message:
        raise RuntimeError(error_message)

    return stats


async def _main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    dsn = os.environ.get("DATABASE_URL_AI")
    if not dsn:
        print("ERROR: DATABASE_URL_AI not set", file=sys.stderr)
        return 2

    leo_pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    try:
        async with leo_pool.acquire() as lock_conn:
            got_lock = await lock_conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY,
            )
            if not got_lock:
                log.info("Another sync is running, skip")
                await lock_conn.execute(
                    """
                    INSERT INTO ai.respect_kb_sync_log
                        (started_at, finished_at, status)
                    VALUES (now(), now(), 'skipped_lock')
                    """,
                )
                return 0

            try:
                async with RespectDBClient.from_env() as respect_client:
                    stats = await run_sync_cycle(
                        leo_pool=leo_pool,
                        respect_client=respect_client,
                    )
                log.info(
                    "sync ok: batches=%d upserted=%d deleted=%d skipped=%d "
                    "att_new=%d att_cached=%d att_link_only=%d",
                    stats.batches, stats.rows_upserted, stats.rows_deleted, stats.rows_skipped,
                    stats.attachments_new, stats.attachments_cached, stats.attachments_link_only,
                )
                return 0
            finally:
                await lock_conn.execute(
                    "SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY,
                )
    finally:
        await leo_pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
