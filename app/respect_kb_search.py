"""
v1.6.0: Поиск в синхронизированной КБ Респект.Чата.

Поток:
1. Получаем список доступных content_id для пользователя:
   kb_get_accessible_content_ids(matrix_user_id) → BIGINT[]
2. Делаем FTS-поиск по ai.respect_kb с фильтром по этим id.
3. Возвращаем результаты с ranking, snippets, attachments, cover_image_url.

ВАЖНО: ACL применяется на стороне Респект.Чата — Leo не знает структуры прав.
Если функция вернула пустой массив — поиск ничего не вернёт (правильное поведение).
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.respect_db import RespectDBClient


log = logging.getLogger("respect_kb_search")


async def respect_kb_search(
    *,
    leo_pool: asyncpg.Pool,
    respect_client: RespectDBClient,
    query: str,
    matrix_user_id: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Поиск в КБ Респект.Чата с учётом прав пользователя.

    Args:
        leo_pool: asyncpg pool к локальной БД ai (для FTS-поиска)
        respect_client: клиент к БД Респект.Чата (для ACL)
        query: текст поискового запроса от LLM
        matrix_user_id: MXID пользователя
        limit: макс. результатов (1..20)

    Returns:
        {
            "count": int,
            "results": [{...}, ...],
            "formatted": str,           -- готовый текст для LLM
        }
    """
    limit = max(1, min(20, int(limit)))

    # 1. ACL — получаем доступные id
    try:
        accessible_ids = await respect_client.get_accessible_content_ids(matrix_user_id)
    except Exception as e:
        log.exception("ACL fetch failed for %s: %s", matrix_user_id, e)
        return {
            "count": 0,
            "results": [],
            "formatted": (
                "Ошибка проверки прав доступа к корпоративной KB Респект.Чата. "
                "Попробуй ещё раз через минуту или обратись к администратору."
            ),
        }

    if not accessible_ids:
        return {
            "count": 0,
            "results": [],
            "formatted": (
                "У тебя нет доступа к корпоративной KB Респект.Чата, "
                "либо твой аккаунт ещё не связан с системой. "
                "Обратись к администратору."
            ),
        }

    # 2. FTS-поиск
    sql = """
        SELECT
            content_id,
            title,
            body_plain,
            section_path,
            cover_image_url,
            actualized_at,
            ts_rank(fts, websearch_to_tsquery('russian', $1)) AS rank,
            ts_headline(
                'russian',
                indexable_text,
                websearch_to_tsquery('russian', $1),
                'MaxFragments=2, MaxWords=30, MinWords=10, ShortWord=3, '
                'StartSel=<<, StopSel=>>'
            ) AS snippet
        FROM ai.respect_kb
        WHERE content_id = ANY($2::bigint[])
          AND fts @@ websearch_to_tsquery('russian', $1)
        ORDER BY rank DESC
        LIMIT $3;
    """
    async with leo_pool.acquire() as conn:
        rows = await conn.fetch(sql, query, accessible_ids, limit)

        if not rows:
            return {
                "count": 0,
                "results": [],
                "formatted": (
                    f"По запросу «{query}» в корпоративной KB ничего не найдено. "
                    "Попробуй переформулировать или искать по более общим терминам."
                ),
            }

        # 3. Подгружаем attachments для найденных карточек
        cids = [r["content_id"] for r in rows]
        att_sql = """
            SELECT
                ca.content_id,
                ca.display_name AS name,
                ca.display_url  AS url,
                a.file_type
            FROM ai.respect_kb_content_attachments ca
            JOIN ai.respect_kb_attachments a USING (sha256)
            WHERE ca.content_id = ANY($1::bigint[])
            ORDER BY ca.content_id, ca.display_name;
        """
        att_rows = await conn.fetch(att_sql, cids)

    # Группируем attachments по content_id
    atts_by_cid: dict[int, list[dict]] = {}
    for ar in att_rows:
        atts_by_cid.setdefault(ar["content_id"], []).append({
            "name": ar["name"],
            "url":  ar["url"],
            "type": ar["file_type"],
        })

    # 4. Сборка результата
    results = []
    for r in rows:
        results.append({
            "content_id":      r["content_id"],
            "title":           r["title"],
            "snippet":         r["snippet"],
            "section_path":    list(r["section_path"]) if r["section_path"] else [],
            "cover_image_url": r["cover_image_url"],
            "actualized_at":   r["actualized_at"].isoformat() if r["actualized_at"] else None,
            "rank":            float(r["rank"]),
            "attachments":     atts_by_cid.get(r["content_id"], []),
        })

    return {
        "count":     len(results),
        "results":   results,
        "formatted": _format_for_llm(query, results),
    }


def _format_for_llm(query: str, results: list[dict]) -> str:
    """Готовый человекочитаемый текст для возврата в Letta-агента.

    Формат:
        Найдено N материалов в корпоративной KB:

        [1] Название материала
            Раздел: КОНКУРЕНТЫ → Экспресс анализ
            Дата актуализации: 2026-04-16
            Фрагмент: ...текст со <<выделенными>> словами...
            Файлы:
              - 📄 Преимущества К+.pdf — https://...
              - 🎬 Семинар.mp4 — https://...

        [2] ...
    """
    if not results:
        return f"По запросу «{query}» ничего не найдено."

    lines = [f"Найдено материалов в корпоративной KB Респект.Чата: {len(results)}", ""]

    icons = {"pdf": "📄", "docx": "📄", "txt": "📄", "md": "📄", "html": "📄",
             "video": "🎬", "audio": "🎵", "image": "🖼", "xlsx": "📊", "other": "📎"}

    for i, r in enumerate(results, 1):
        section = " → ".join(r["section_path"]) if r["section_path"] else "—"
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    Раздел: {section}")
        if r["actualized_at"]:
            # YYYY-MM-DD
            lines.append(f"    Дата актуализации: {r['actualized_at'][:10]}")
        snippet = (r["snippet"] or "").strip().replace("\n", " ")
        if snippet:
            lines.append(f"    Фрагмент: {snippet}")
        if r["attachments"]:
            lines.append("    Файлы:")
            for att in r["attachments"]:
                icon = icons.get((att.get("type") or "").lower(), "📎")
                lines.append(f"      - {icon} {att['name']} — {att['url']}")
        if r["cover_image_url"]:
            lines.append(f"    Превью: {r['cover_image_url']}")
        lines.append("")

    return "\n".join(lines).rstrip()
