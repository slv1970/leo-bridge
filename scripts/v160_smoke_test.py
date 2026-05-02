#!/usr/bin/env python3
"""
v1.6.0 smoke-тесты.

Проверяют что после применения патчей:
1. Миграция применена (все 4 таблицы существуют)
2. Новые модули импортируются
3. (опционально, если RESPECT_DATABASE_URL задан) клиент Respect.Chat подключается
4. (опционально) функции kb_get_accessible_content_ids и kb_get_next_content
   существуют на стороне Респект.Чата

Запуск:
    cd /opt/ai/bridge && set -a && source .env && set +a && \\
        venv/bin/python scripts/v160_smoke_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any


PASS = "✅"
FAIL = "❌"
SKIP = "⏭"


def report(status: str, msg: str) -> None:
    print(f"{status} {msg}")


async def test_imports() -> bool:
    try:
        from app.respect_db import RespectDBClient
        from app.respect_kb_search import respect_kb_search
        from app.respect_kb_sync import run_sync_cycle
        from app.html_utils import html_to_plain
        from app.attachment_parser import parse_blob, get_file_type, is_parseable
        from bs4 import BeautifulSoup  # проверка beautifulsoup4
        report(PASS, "Modules import")
        return True
    except Exception as e:
        report(FAIL, f"Modules import: {e}")
        return False


async def test_html_to_plain() -> bool:
    from app.html_utils import html_to_plain
    cases = [
        ("<p>Hello <b>world</b></p>", "Hello world"),
        ("<table><tr><td>A</td><td>B</td></tr></table>", "A"),
        ("<script>evil()</script><p>Safe</p>", "Safe"),
        ("", ""),
    ]
    ok = True
    for html, expected_substr in cases:
        result = html_to_plain(html)
        if expected_substr not in result and expected_substr != "":
            report(FAIL, f"html_to_plain({html!r}) → {result!r} (expected substr {expected_substr!r})")
            ok = False
    if ok:
        report(PASS, "html_to_plain")
    return ok


async def test_file_type_detection() -> bool:
    from app.attachment_parser import get_file_type, is_parseable
    cases = [
        ("doc.pdf", None, "pdf", True),
        ("report.docx", None, "docx", True),
        ("notes.txt", None, "txt", True),
        ("clip.mp4", None, "video", False),
        ("song.mp3", None, "audio", False),
        ("image.png", None, "image", False),
        ("archive.zip", None, "other", False),
        ("file.bin", "video", "video", False),
        ("anything", "pdf", "pdf", True),
    ]
    ok = True
    for name, declared, expected_type, expected_parseable in cases:
        t = get_file_type(name, declared)
        p = is_parseable(t)
        if t != expected_type or p != expected_parseable:
            report(FAIL, f"get_file_type({name!r}, {declared!r}) → {t} parseable={p} (expected {expected_type} {expected_parseable})")
            ok = False
    if ok:
        report(PASS, "file type detection")
    return ok


async def test_db_migration() -> bool:
    """Проверка что миграция применена."""
    import asyncpg

    dsn = os.environ.get("DATABASE_URL_AI")
    if not dsn:
        report(FAIL, "DATABASE_URL_AI not set")
        return False

    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'ai' AND tablename LIKE 'respect_kb%'
            ORDER BY tablename
            """
        )
        names = [r["tablename"] for r in rows]
        expected = {
            "respect_kb",
            "respect_kb_attachments",
            "respect_kb_content_attachments",
            "respect_kb_sync_log",
        }
        missing = expected - set(names)
        if missing:
            report(FAIL, f"DB migration: missing tables {missing}")
            return False
        report(PASS, f"DB migration: tables {names}")
        return True
    finally:
        await conn.close()


async def test_respect_db_optional() -> bool:
    """Проверка подключения к БД Респект.Чата — только если задан RESPECT_DATABASE_URL."""
    if not os.environ.get("RESPECT_DATABASE_URL"):
        report(SKIP, "RESPECT_DATABASE_URL not set — Respect DB tests skipped")
        return True

    from app.respect_db import RespectDBClient
    try:
        async with RespectDBClient.from_env() as client:
            # Пробуем дёрнуть ACL на несуществующего юзера — должен вернуть []
            ids = await client.get_accessible_content_ids("@nonexistent_test_user:respectrb.ru")
            if not isinstance(ids, list):
                report(FAIL, f"Respect ACL: expected list, got {type(ids)}")
                return False
            report(PASS, f"Respect ACL function returns list (len={len(ids)} for nonexistent user)")
        return True
    except Exception as e:
        report(FAIL, f"Respect DB connect: {e}")
        return False


async def main() -> int:
    print("=" * 60)
    print("Leo v1.6.0 — Smoke tests")
    print("=" * 60)

    results = []
    results.append(await test_imports())
    results.append(await test_html_to_plain())
    results.append(await test_file_type_detection())
    results.append(await test_db_migration())
    results.append(await test_respect_db_optional())

    print("=" * 60)
    failed = sum(1 for r in results if not r)
    if failed:
        print(f"{FAIL} {failed}/{len(results)} tests failed")
        return 1
    print(f"{PASS} All {len(results)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
