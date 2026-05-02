"""
Внутренний HTTP API для Letta-tools.

Слушает 127.0.0.1:8284, доступен только локально.
Защищён shared secret из BRIDGE_INTERNAL_TOKEN.

Endpoints:
- POST /calendar/event              — создать событие
- POST /calendar/list               — список событий
- POST /calendar/find               — поиск
- POST /calendar/delete             — удалить
- POST /calendar/timezone           — узнать TZ комнаты
- POST /calendar/timezone/set       — установить TZ
"""
from __future__ import annotations

import logging
import re
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.calendar_client import CalendarClient
from app.timezone_resolver import TimezoneResolver
# v1.6.0: интеграция с КБ Респект.Чата
from app.respect_db import RespectDBClient
from app.respect_kb_search import respect_kb_search as _respect_kb_search_impl



log = logging.getLogger("internal_api")


# ----- Модели запросов/ответов -----
class CreateEventReq(BaseModel):
    matrix_room_id: str
    room_display_name: str = ""
    title: str
    start_iso: str = Field(..., description="ISO datetime со смещением")
    end_iso: str
    timezone: str
    description: str | None = None
    location: str | None = None
    creator_user_id: str | None = None
    reminder_minutes: int | None = 15

class ListEventsReq(BaseModel):
    matrix_room_id: str
    room_display_name: str = ""
    date_from_iso: str
    date_to_iso: str
    creator_user_id: str | None = None
    creator_user_id: str | None = None


class FindEventsReq(ListEventsReq):
    query: str


class DeleteEventReq(BaseModel):
    matrix_room_id: str
    room_display_name: str = ""
    uid: str
    creator_user_id: str | None = None
    creator_user_id: str | None = None


class TZGetReq(BaseModel):
    matrix_room_id: str
    matrix_user_id: str | None = None


class TZSetReq(BaseModel):
    matrix_room_id: str
    timezone: str

class NotifyReq(BaseModel):
    matrix_room_id: str
    text: str

class CreateFileReq(BaseModel):
    """v1.5.0: запрос на создание и отправку файла в Matrix."""
    matrix_room_id: str
    creator_user_id: str | None = None
    filename: str
    format: str  # "md"|"html"|"docx"|"pdf"|"xlsx"|"pptx"
    content_md: str | None = None
    content_json: str | None = None
    title: str | None = None


# ----- Auth -----
class KbSearchCorporateReq(BaseModel):
    query: str = Field(..., description="Текст поискового запроса от LLM")
    matrix_room_id: str | None = Field(
        None, description="Если задан — также ищем чанки с access_room_id = этой комнаты"
    )
    limit: int = Field(5, ge=1, le=20)


class KbSearchPersonalReq(BaseModel):
    query: str
    matrix_user_id: str = Field(..., description="MXID владельца знаний")
    limit: int = Field(5, ge=1, le=20)

class KbPersonalListReq(BaseModel):
    matrix_user_id: str = Field(..., description="MXID владельца KB")


class KbPersonalDeleteReq(BaseModel):
    matrix_user_id: str
    source: str = Field(..., description="Имя файла (case-insensitive поиск)")


class KbPersonalClearReq(BaseModel):
    matrix_user_id: str

class KbPersonalInfoReq(BaseModel):
    matrix_user_id: str
    source: str = Field(..., description="Имя файла (case-insensitive)")


def check_auth(x_internal_token: str = Header(...)) -> None:
    expected = os.environ.get("BRIDGE_INTERNAL_TOKEN")
    if not expected or x_internal_token != expected:
        raise HTTPException(status_code=401, detail="Invalid internal token")


# ----- Lifespan: подключаем CalDAV и Postgres -----
class State:
    cal: CalendarClient | None = None
    pg_pool: asyncpg.Pool | None = None
    tz_resolver: TimezoneResolver | None = None
    matrix_send: Any = None  # callback из bridge для отправки в комнату
    respect_client: RespectDBClient | None = None  # v1.6.0: КБ Респект.Чата

state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.cal = CalendarClient(
        os.environ["RADICALE_URL"],
        os.environ["RADICALE_USER"],
        os.environ["RADICALE_PASSWORD"],
    )
    state.pg_pool = await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL_AI"],
        min_size=1,
        max_size=4,
    )
    state.tz_resolver = TimezoneResolver(
        pg_pool=state.pg_pool,
        matrix_homeserver=os.environ["MATRIX_HOMESERVER_URL"],
        matrix_token=os.environ["BRIDGE_ACCESS_TOKEN"],
    )
    await state.tz_resolver.__aenter__()
    # v1.6.0: КБ Респект.Чата (опционально — если RESPECT_DATABASE_URL задан)
    if os.environ.get("RESPECT_DATABASE_URL"):
        try:
            state.respect_client = RespectDBClient.from_env()
            await state.respect_client.__aenter__()
            log.info("Respect.Chat KB client initialized")
        except Exception as e:
            log.warning("Respect.Chat KB client init failed: %s", e)
            state.respect_client = None
    else:
        log.info("RESPECT_DATABASE_URL not set, /respect_kb/search disabled")

    log.info("Internal API started")
    yield
    if state.tz_resolver:
        await state.tz_resolver.__aexit__(None, None, None)
    if state.respect_client:
        try:
            await state.respect_client.__aexit__(None, None, None)
        except Exception:
            pass

    if state.pg_pool:
        await state.pg_pool.close()
    log.info("Internal API stopped")


app = FastAPI(lifespan=lifespan)


# ----- Endpoints -----

# v0.8.6: распознавание идентификаторов для гибридного поиска
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,40}$")


def _is_identifier_query(q: str) -> bool:
    """Запрос состоит из одного «слова», похожего на код/идентификатор?

    Примеры True:  t26041, tr26043_iu_bef, idkart, my_table, MyClass
    Примеры False: "что такое t26041", "поиск t26041", "t26041 trigger"
    """
    return bool(_IDENTIFIER_RE.match(q.strip()))


@app.post("/calendar/event")
async def create_event(req: CreateEventReq, _: None = Depends(check_auth)) -> dict[str, Any]:
    assert state.cal
    start = datetime.fromisoformat(req.start_iso)
    end = datetime.fromisoformat(req.end_iso)
    log.info('create_event: creator=%s reminder=%s', req.creator_user_id, req.reminder_minutes)
    ev = await state.cal.create_event(
        matrix_room_id=req.matrix_room_id,
        room_display_name=req.room_display_name,
        title=req.title,
        start=start,
        end=end,
        timezone=req.timezone,
        description=req.description,
        location=req.location,
        creator_user_id=req.creator_user_id,
    )
    return {"event": ev.to_dict()}


@app.post("/calendar/list")
async def list_events(req: ListEventsReq, _: None = Depends(check_auth)) -> dict[str, Any]:
    assert state.cal
    df = datetime.fromisoformat(req.date_from_iso)
    dt = datetime.fromisoformat(req.date_to_iso)
    # v1.3.0: автоподстановка creator_user_id из ai.user_rooms если не передан
    creator = req.creator_user_id
    if not creator and state.pg_pool:
        try:
            async with state.pg_pool.acquire() as _pg:
                creator = await _pg.fetchval(
                    "SELECT matrix_user_id FROM ai.user_rooms "
                    "WHERE matrix_room_id = $1 ORDER BY last_seen_at DESC LIMIT 1",
                    req.matrix_room_id,
                )
        except Exception:
            pass
    log.info('list_events room=%s creator=%s', req.matrix_room_id[:16], creator)
    events = await state.cal.list_events(
        req.matrix_room_id, req.room_display_name, df, dt,
        creator_user_id=creator,
    )
    return {"events": [e.to_dict() for e in events], "count": len(events)}


@app.post("/calendar/find")
async def find_events(req: FindEventsReq, _: None = Depends(check_auth)) -> dict[str, Any]:
    assert state.cal
    df = datetime.fromisoformat(req.date_from_iso)
    dt = datetime.fromisoformat(req.date_to_iso)
    # v1.3.0: автоподстановка creator_user_id из ai.user_rooms если не передан
    creator = req.creator_user_id
    if not creator and state.pg_pool:
        try:
            async with state.pg_pool.acquire() as _pg:
                creator = await _pg.fetchval(
                    "SELECT matrix_user_id FROM ai.user_rooms "
                    "WHERE matrix_room_id = $1 ORDER BY last_seen_at DESC LIMIT 1",
                    req.matrix_room_id,
                )
        except Exception:
            pass
    events = await state.cal.find_events(
        req.matrix_room_id, req.room_display_name, req.query, df, dt,
        creator_user_id=creator,
    )
    return {"events": [e.to_dict() for e in events], "count": len(events)}


@app.post("/calendar/delete")
async def delete_event(req: DeleteEventReq, _: None = Depends(check_auth)) -> dict[str, Any]:
    assert state.cal
    # v1.3.0: автоподстановка creator_user_id
    del_creator = req.creator_user_id
    if not del_creator and state.pg_pool:
        try:
            async with state.pg_pool.acquire() as _pg:
                del_creator = await _pg.fetchval(
                    "SELECT matrix_user_id FROM ai.user_rooms "
                    "WHERE matrix_room_id = $1 ORDER BY last_seen_at DESC LIMIT 1",
                    req.matrix_room_id,
                )
        except Exception:
            pass
    ok = await state.cal.delete_event(req.matrix_room_id, req.room_display_name, req.uid, creator_user_id=del_creator)
    return {"deleted": ok}

@app.post("/calendar/timezone")
async def get_timezone(req: TZGetReq, _: None = Depends(check_auth)) -> dict[str, Any]:
    assert state.tz_resolver
    # Сначала пробуем override
    tz = await state.tz_resolver.get_room_override(req.matrix_room_id)
    if tz:
        return {"timezone": tz, "source": "override"}
    # Потом MSC4175 — и сохраняем в БД для консистентности с reminder-watcher
    if req.matrix_user_id:
        tz = await state.tz_resolver.get_user_tz_from_profile(req.matrix_user_id)
        if tz:
            try:
                await state.tz_resolver.set_room_override(req.matrix_room_id, tz)
                log.info(
                    "Cached TZ %s for room %s from user profile %s",
                    tz, req.matrix_room_id[:20], req.matrix_user_id,
                )
            except Exception as e:
                log.warning("Failed to cache TZ: %s", e)
            return {"timezone": tz, "source": "profile"}
    return {"timezone": None, "source": None}

@app.post("/calendar/timezone/set")
async def set_timezone(req: TZSetReq, _: None = Depends(check_auth)) -> dict[str, Any]:
    assert state.tz_resolver
    try:
        await state.tz_resolver.set_room_override(req.matrix_room_id, req.timezone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "timezone": req.timezone}

@app.post("/matrix/notify")
async def matrix_notify(req: NotifyReq, _: None = Depends(check_auth)) -> dict[str, Any]:
    """
    Отправить текстовое уведомление в Matrix-комнату от имени бота.
    Использует HTTP Matrix Client API напрямую (без E2EE — для системных уведомлений).
    Если комната E2EE — сообщение придёт как зашифрованное (Matrix server обработает).
    """
    import httpx
    homeserver = os.environ["MATRIX_HOMESERVER_URL"].rstrip("/")
    token = os.environ["BRIDGE_ACCESS_TOKEN"]

    # Используем простой text-message без треда
    txn_id = f"reminder-{os.urandom(8).hex()}"
    url = (
        f"{homeserver}/_matrix/client/v3/rooms/{req.matrix_room_id}"
        f"/send/m.room.message/{txn_id}"
    )
    body = {"msgtype": "m.text", "body": req.text}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.put(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        if r.status_code >= 400:
            log.error("Matrix notify failed: %s %s", r.status_code, r.text[:200])
            raise HTTPException(
                status_code=502, detail=f"Matrix returned {r.status_code}"
            )
        data = r.json()
        return {"event_id": data.get("event_id")}

async def _get_query_embedding(query: str) -> list[float]:
    """Получить embedding для поискового запроса. Используется в обоих kb_search_*."""
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = await client.embeddings.create(
        model="text-embedding-3-small",
        input=[query],
    )
    return resp.data[0].embedding


def _vector_literal(emb: list[float]) -> str:
    """pgvector ожидает строку '[v1,v2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in emb) + "]"


def _format_kb_results(rows: list[dict]) -> str:
    """v0.8.7: форматирование результатов KB-поиска для отправки пользователю.

    Для exact-результатов показывает (N упоминаний).
    Для semantic — релевантность 0.XX.
    Пропускает chunk_index если он None (personal KB).
    """
    if not rows:
        return ""
    lines = [f"Найдено фрагментов: {len(rows)}"]
    for i, r in enumerate(rows, 1):
        source = r.get("source", "?")
        chunk_idx = r.get("chunk_index")
        match_type = r.get("match_type", "semantic")
        match_count = r.get("match_count")

        # Заголовок фрагмента
        if match_type == "exact" and match_count is not None and match_count > 0:
            cnt = int(match_count)
            label = f"{cnt} упоминание" if cnt == 1 else (
                f"{cnt} упоминания" if cnt < 5 else f"{cnt} упоминаний"
            )
            chunk_part = f"chunk {chunk_idx}, " if chunk_idx is not None else ""
            header = f"[{i}] {source} ({chunk_part}{label})"
        else:
            sim = r.get("sim", 0)
            chunk_part = f"chunk {chunk_idx}, " if chunk_idx is not None else ""
            header = f"[{i}] {source} ({chunk_part}релевантность {sim:.2f})"

        # Контент — обрезка до 600 chars для краткости в чате
        content = (r.get("content") or "").strip()
        if len(content) > 600:
            content = content[:600] + "..."

        lines.append(header + ":")
        lines.append("")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


@app.post("/kb/search_corporate")
async def kb_search_corporate(
    req: KbSearchCorporateReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """
    v0.8.6: гибридный поиск по корпоративной KB.

    - Если query похож на идентификатор (t26041, my_func) — сначала ILIKE,
      потом семантический для дополнения.
    - Иначе — только семантический.

    Логика доступа:
    - Чанки с access_room_id IS NULL — общедоступные
    - Чанки с access_room_id = req.matrix_room_id — видны только в этой комнате
    """
    assert state.pg_pool
    emb = await _get_query_embedding(req.query)
    emb_lit = _vector_literal(emb)

    use_exact = _is_identifier_query(req.query)
    exact_rows: list = []
    excluded_ids: set = set()

    async with state.pg_pool.acquire() as conn:
        # 1. ILIKE-prefetch для идентификаторов
        if use_exact:
            # v0.8.7: считаем число вхождений строки в content
            # match_count = (length(content) - length(replace(content_lower, query_lower, ''))) / length(query)
            exact_sql = """
                SELECT
                    id::text AS id,
                    source,
                    title,
                    content,
                    chunk_index,
                    access_room_id,
                    (LENGTH(content) - LENGTH(REPLACE(LOWER(content), LOWER($2), '')))::float8
                        / GREATEST(LENGTH($2), 1) AS match_count,
                    'exact'::text AS match_type
                FROM ai.ai_knowledge
                WHERE (access_room_id IS NULL OR access_room_id = $1)
                  AND content ILIKE '%' || $2 || '%'
                ORDER BY match_count DESC, length(content) ASC
                LIMIT $3;
            """
            exact_rows = await conn.fetch(
                exact_sql, req.matrix_room_id, req.query, req.limit
            )
            excluded_ids = {r["id"] for r in exact_rows}

        # 2. Семантический поиск (исключая уже найденные)
        remaining = max(req.limit - len(exact_rows), 0)
        semantic_rows: list = []
        if remaining > 0:
            if excluded_ids:
                sem_sql = """
                    SELECT
                        id::text AS id,
                        source,
                        title,
                        content,
                        chunk_index,
                        access_room_id,
                        1 - (embedding <=> $1::vector) AS sim,
                        NULL::float8 AS match_count,
                        'semantic'::text AS match_type
                    FROM ai.ai_knowledge
                    WHERE (access_room_id IS NULL OR access_room_id = $2)
                      AND id::text != ALL($4::text[])
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3;
                """
                semantic_rows = await conn.fetch(
                    sem_sql, emb_lit, req.matrix_room_id, remaining, list(excluded_ids)
                )
            else:
                sem_sql = """
                    SELECT
                        id::text AS id,
                        source,
                        title,
                        content,
                        chunk_index,
                        access_room_id,
                        1 - (embedding <=> $1::vector) AS sim,
                        NULL::float8 AS match_count,
                        'semantic'::text AS match_type
                    FROM ai.ai_knowledge
                    WHERE access_room_id IS NULL OR access_room_id = $2
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3;
                """
                semantic_rows = await conn.fetch(
                    sem_sql, emb_lit, req.matrix_room_id, remaining
                )

    rows_dict = [dict(r) for r in list(exact_rows) + list(semantic_rows)]
    return {
        "count": len(rows_dict),
        "exact_count": len(exact_rows),
        "results": rows_dict,
        "formatted": _format_kb_results(rows_dict),
    }


@app.post("/kb/search_personal")
async def kb_search_personal(
    req: KbSearchPersonalReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """
    v0.8.6: гибридный поиск по личной KB.
    Если query похож на идентификатор — сначала ILIKE, потом семантика.
    """
    assert state.pg_pool
    emb = await _get_query_embedding(req.query)
    emb_lit = _vector_literal(emb)

    use_exact = _is_identifier_query(req.query)
    exact_rows: list = []
    excluded_ids: set = set()

    async with state.pg_pool.acquire() as conn:
        if use_exact:
            exact_sql = """
                SELECT
                    id::text AS id,
                    source,
                    NULL::text AS title,
                    content,
                    NULL::int AS chunk_index,
                    NULL::text AS access_room_id,
                    (LENGTH(content) - LENGTH(REPLACE(LOWER(content), LOWER($2), '')))::float8
                        / GREATEST(LENGTH($2), 1) AS match_count,
                    'exact'::text AS match_type
                FROM ai.ai_knowledge_personal
                WHERE matrix_user_id = $1
                  AND content ILIKE '%' || $2 || '%'
                ORDER BY match_count DESC, length(content) ASC
                LIMIT $3;
            """
            exact_rows = await conn.fetch(
                exact_sql, req.matrix_user_id, req.query, req.limit
            )
            excluded_ids = {r["id"] for r in exact_rows}

        remaining = max(req.limit - len(exact_rows), 0)
        semantic_rows: list = []
        if remaining > 0:
            if excluded_ids:
                sem_sql = """
                    SELECT
                        id::text AS id,
                        source,
                        NULL::text AS title,
                        content,
                        NULL::int AS chunk_index,
                        NULL::text AS access_room_id,
                        1 - (embedding <=> $1::vector) AS sim,
                        NULL::float8 AS match_count,
                        'semantic'::text AS match_type
                    FROM ai.ai_knowledge_personal
                    WHERE matrix_user_id = $2
                      AND id::text != ALL($4::text[])
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3;
                """
                semantic_rows = await conn.fetch(
                    sem_sql, emb_lit, req.matrix_user_id, remaining, list(excluded_ids)
                )
            else:
                sem_sql = """
                    SELECT
                        id::text AS id,
                        source,
                        NULL::text AS title,
                        content,
                        NULL::int AS chunk_index,
                        NULL::text AS access_room_id,
                        1 - (embedding <=> $1::vector) AS sim,
                        NULL::float8 AS match_count,
                        'semantic'::text AS match_type
                    FROM ai.ai_knowledge_personal
                    WHERE matrix_user_id = $2
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3;
                """
                semantic_rows = await conn.fetch(
                    sem_sql, emb_lit, req.matrix_user_id, remaining
                )

    rows_dict = [dict(r) for r in list(exact_rows) + list(semantic_rows)]
    return {
        "count": len(rows_dict),
        "exact_count": len(exact_rows),
        "results": rows_dict,
        "formatted": _format_kb_results(rows_dict),
    }
@app.post("/kb/personal/list")
async def kb_personal_list(
    req: KbPersonalListReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """
    Список документов в personal KB пользователя.
    Группирует чанки по source — один документ = одна строка вывода.
    """
    assert state.pg_pool
    sql = """
        SELECT
            source,
            COUNT(*) AS chunks,
            MAX(created_at) AS last_added,
            COALESCE(SUM(LENGTH(content)), 0) AS total_chars
        FROM ai.ai_knowledge_personal
        WHERE matrix_user_id = $1
        GROUP BY source
        ORDER BY MAX(created_at) DESC;
    """
    async with state.pg_pool.acquire() as conn:
        rows = await conn.fetch(sql, req.matrix_user_id)

    documents = [
        {
            "source": r["source"],
            "chunks": r["chunks"],
            "last_added": r["last_added"].isoformat() if r["last_added"] else None,
            "total_chars": r["total_chars"],
        }
        for r in rows
    ]
    return {
        "count": len(documents),
        "total_chunks": sum(d["chunks"] for d in documents),
        "documents": documents,
    }


@app.post("/kb/personal/delete")
async def kb_personal_delete(
    req: KbPersonalDeleteReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """
    Удалить документ из personal KB по имени (case-insensitive).
    Возвращает кол-во удалённых чанков и список s3_key для последующей очистки S3.
    """
    assert state.pg_pool
    async with state.pg_pool.acquire() as conn:
        # Сначала найдём что удаляем (для ответа и S3 cleanup)
        rows = await conn.fetch(
            """
            SELECT source, s3_key
            FROM ai.ai_knowledge_personal
            WHERE matrix_user_id = $1 AND LOWER(source) = LOWER($2);
            """,
            req.matrix_user_id, req.source,
        )
        if not rows:
            return {"deleted": 0, "matched_source": None, "s3_keys": []}

        actual_source = rows[0]["source"]
        s3_keys = list({r["s3_key"] for r in rows if r["s3_key"]})

        result = await conn.execute(
            """
            DELETE FROM ai.ai_knowledge_personal
            WHERE matrix_user_id = $1 AND LOWER(source) = LOWER($2);
            """,
            req.matrix_user_id, req.source,
        )
        # asyncpg возвращает строку "DELETE N"
        deleted = int(result.split()[-1])

    return {
        "deleted": deleted,
        "matched_source": actual_source,
        "s3_keys": s3_keys,
    }


@app.post("/kb/personal/clear")
async def kb_personal_clear(
    req: KbPersonalClearReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """
    Очистить ВСЮ personal KB пользователя.
    Деструктивная операция — bridge должен реализовать двухшаговое подтверждение.
    """
    assert state.pg_pool
    async with state.pg_pool.acquire() as conn:
        # Получим список s3_key до удаления
        rows = await conn.fetch(
            "SELECT DISTINCT s3_key FROM ai.ai_knowledge_personal WHERE matrix_user_id = $1;",
            req.matrix_user_id,
        )
        s3_keys = [r["s3_key"] for r in rows if r["s3_key"]]

        result = await conn.execute(
            "DELETE FROM ai.ai_knowledge_personal WHERE matrix_user_id = $1;",
            req.matrix_user_id,
        )
        deleted = int(result.split()[-1])

    return {"deleted": deleted, "s3_keys": s3_keys}
@app.post("/kb/personal/info")
async def kb_personal_info(
    req: KbPersonalInfoReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """
    Детали одного документа: чанки, размер, даты, sha256, s3_key.
    """
    assert state.pg_pool
    sql = """
        SELECT
            source,
            sha256,
            s3_key,
            COUNT(*) AS chunks,
            COALESCE(SUM(LENGTH(content)), 0) AS total_chars,
            MIN(created_at) AS first_added,
            MAX(created_at) AS last_added
        FROM ai.ai_knowledge_personal
        WHERE matrix_user_id = $1 AND LOWER(source) = LOWER($2)
        GROUP BY source, sha256, s3_key;
    """
    async with state.pg_pool.acquire() as conn:
        row = await conn.fetchrow(sql, req.matrix_user_id, req.source)

    if not row:
        return {"found": False}

    return {
        "found": True,
        "source": row["source"],
        "sha256": row["sha256"],
        "s3_key": row["s3_key"],
        "chunks": row["chunks"],
        "total_chars": row["total_chars"],
        "first_added": row["first_added"].isoformat() if row["first_added"] else None,
        "last_added": row["last_added"].isoformat() if row["last_added"] else None,
    }


@app.post("/files/create")
async def create_file(req: CreateFileReq, _: None = Depends(check_auth)) -> dict[str, Any]:
    """
    v1.5.0: сгенерировать файл и отправить в Matrix-комнату.

    Поток:
    1. Валидация формата
    2. Генерация файла во временной директории
    3. POST /internal/files/upload на bridge:9090 — загрузка в Matrix
    4. Удаление временного файла (через delete_after=true в bridge handler)
    5. Возврат статуса в Letta
    """
    import httpx
    import uuid
    from pathlib import Path
    from app.file_generators import GENERATORS

    fmt = req.format.lower().strip()
    if fmt not in GENERATORS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported format: {fmt}. Available: {list(GENERATORS.keys())}",
        )

    # Подготовка имени файла
    filename = req.filename
    if not filename.lower().endswith(f".{fmt}"):
        filename = f"{filename}.{fmt}"

    # Workdir (shared между bridge и internal_api)
    workdir = Path("/opt/ai/bridge/data/files_tmp")
    workdir.mkdir(parents=True, exist_ok=True)

    # Уникальное имя на диске чтобы не было гонок
    tmp_name = f"{uuid.uuid4().hex}.{fmt}"
    tmp_path = workdir / tmp_name

    try:
        # 1. Генерация
        try:
            GENERATORS[fmt](
                tmp_path,
                content_md=req.content_md,
                content_json=req.content_json,
                title=req.title,
            )
        except Exception as e:
            log.exception("generation failed for fmt=%s: %s", fmt, e)
            raise HTTPException(status_code=500, detail=f"generation failed: {e}")

        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="generated file is empty")

        size = tmp_path.stat().st_size

        # 2. Отправка в bridge для upload в Matrix
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "http://127.0.0.1:9090/internal/files/upload",
                    json={
                        "file_path": str(tmp_path),
                        "room_id": req.matrix_room_id,
                        "filename": filename,
                        "delete_after": True,
                    },
                )
                if r.status_code >= 400:
                    log.error("bridge upload failed: %s %s", r.status_code, r.text[:200])
                    raise HTTPException(
                        status_code=502,
                        detail=f"bridge upload returned {r.status_code}",
                    )
                upload_data = r.json()
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"bridge unreachable: {e}")

        log.info(
            "create_file ok: fmt=%s name=%s size=%d event=%s",
            fmt, filename, size, upload_data.get("event_id"),
        )

        return {
            "ok": True,
            "filename": filename,
            "format": fmt,
            "size_bytes": size,
            "event_id": upload_data.get("event_id"),
            "encrypted": upload_data.get("encrypted"),
        }
    except HTTPException:
        # Cleanup on error
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise
    except Exception as e:
        log.exception("create_file unexpected error: %s", e)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))



# =============================================================================
# v1.6.0: КБ Респект.Чата
# =============================================================================
class RespectKbSearchReq(BaseModel):
    query: str = Field(..., description="Текст поискового запроса")
    matrix_user_id: str = Field(..., description="MXID пользователя для проверки прав")
    limit: int = Field(5, ge=1, le=20)


@app.post("/respect_kb/search")
async def respect_kb_search_endpoint(
    req: RespectKbSearchReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """v1.6.0: поиск в КБ Респект.Чата с учётом прав пользователя.

    ACL применяется на стороне Респект.Чата через PG-функцию
    kb_get_accessible_content_ids(matrix_user_id).
    FTS-поиск делается локально по синхронизированной копии (ai.respect_kb).
    """
    if state.respect_client is None or state.pg_pool is None:
        raise HTTPException(
            status_code=503,
            detail="Respect.Chat KB integration is not configured (RESPECT_DATABASE_URL missing)",
        )
    return await _respect_kb_search_impl(
        leo_pool=state.pg_pool,
        respect_client=state.respect_client,
        query=req.query,
        matrix_user_id=req.matrix_user_id,
        limit=req.limit,
    )


def serve() -> None:
    uvicorn.run(
        "app.internal_api:app",
        host="127.0.0.1",
        port=8284,
        log_level="info",
    )


if __name__ == "__main__":
    serve()
