"""
Регистрация календарных tools в Letta.

Запуск:
    cd /opt/ai/bridge
    set -a; source .env; set +a
    python scripts/register_tools.py

Идемпотентно — если tool с таким именем уже есть, обновляет его.
"""
from __future__ import annotations

import inspect
import os
import sys

import httpx


LETTA_URL = os.environ["LETTA_URL"]
LETTA_PASS = os.environ["LETTA_SERVER_PASSWORD"]


def calendar_create_event(
    title: str,
    start_iso: str,
    end_iso: str,
    timezone: str,
    matrix_room_id: str,
    creator_user_id: str = "",
    remind_minutes_before: int = 15,
    description: str = "",
    location: str = "",
) -> str:
    """
    Создать событие в календаре текущей Matrix-комнаты.

    Args:
        title: Название встречи. Например "Планёрка отдела".
        start_iso: Начало, ISO 8601 с TZ-смещением. Пример: "2026-04-26T15:00:00+03:00".
        end_iso: Конец, в том же формате. Если не указали длительность — используй +1 час.
        timezone: IANA TZ-имя, например "Europe/Moscow". Должно совпадать с offset в start_iso.
        matrix_room_id: ID Matrix-комнаты, начинается с "!". Возьми из контекста разговора.
        description: Опциональное описание (повестка, ссылки, заметки).
        location: Опциональное место встречи или ссылка на видео.
        creator_user_id: MXID того кто создаёт встречу. Возьми из [from=...] в контексте сообщения. Используется для напоминаний в его таймзоне.

    Returns:
        Подтверждение и UID созданного события (по UID можно потом отменять).

    Когда использовать:
        Когда пользователь просит "запланируй встречу", "создай событие", "добавь в календарь".

    Перед вызовом убедись:
        - Знаешь дату/время. Если пользователь сказал "завтра в 3" — преобразуй в полный ISO.
        - Знаешь TZ комнаты. Если не знаешь — сначала вызови calendar_get_timezone.
    """
    import os
    import httpx

    body = {
        "matrix_room_id": matrix_room_id,
        "room_display_name": "",
        "title": title,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "timezone": timezone,
        "description": description or None,
        "location": location or None,
        "creator_user_id": creator_user_id or None,
        "reminder_minutes": None if remind_minutes_before == 0 else remind_minutes_before,
    }
    r = httpx.post(
        "http://127.0.0.1:8284/calendar/event",
        headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
        json=body,
        timeout=30,
    )
    if r.status_code >= 400:
        return f"Ошибка создания события: {r.status_code} {r.text[:300]}"
    data = r.json()["event"]
    return (
        f"Событие создано.\n"
        f"Название: {data['title']}\n"
        f"Когда: {data['start']} → {data['end']}\n"
        f"UID: {data['uid']}"
    )


def calendar_list_events(
    matrix_room_id: str,
    date_from_iso: str,
    date_to_iso: str,
    creator_user_id: str = "",
) -> str:
    """
    Получить список событий пользователя за период.

    Args:
        matrix_room_id: ID Matrix-комнаты из контекста [matrix_room_id=...], начинается с "!".
        date_from_iso: Начало периода, ISO 8601 с TZ. Пример: "2026-04-26T00:00:00+03:00".
        date_to_iso: Конец периода, ISO 8601 с TZ.
        creator_user_id: MXID пользователя — ОБЯЗАТЕЛЬНО возьми из [from=...] в контексте.
            Например "@viacheslav:mtx.respectrb.ru". Без него события не найдутся.

    Returns:
        Текстовый список событий или сообщение что событий нет.

    Когда использовать:
        Когда пользователь спрашивает "что у нас на этой неделе", "какие встречи завтра",
        "покажи расписание", "покажи мой календарь".
    """
    import os
    import httpx

    body = {
        "matrix_room_id": matrix_room_id,
        "room_display_name": "",
        "date_from_iso": date_from_iso,
        "date_to_iso": date_to_iso,
        "creator_user_id": creator_user_id or None,
    }
    r = httpx.post(
        "http://127.0.0.1:8284/calendar/list",
        headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
        json=body,
        timeout=30,
    )
    if r.status_code >= 400:
        return f"Ошибка получения списка: {r.status_code} {r.text[:300]}"
    data = r.json()
    if data["count"] == 0:
        return f"На период {date_from_iso[:10]} — {date_to_iso[:10]} событий не найдено."
    lines = [f"Период {date_from_iso[:10]} — {date_to_iso[:10]}: найдено {data["count"]} событий"]
    for e in data["events"]:
        lines.append(f"- {e['start']} → {e['end']}: {e['title']} (uid={e['uid'][:8]})")
    return "\n".join(lines)


def calendar_find_events(
    matrix_room_id: str,
    query: str,
    date_from_iso: str,
    date_to_iso: str,
    creator_user_id: str = "",
) -> str:
    """
    Найти событие по тексту в названии, описании или месте проведения.

    Args:
        matrix_room_id: ID Matrix-комнаты из контекста [matrix_room_id=...].
        query: Что искать (подстрока, регистр не важен). Например "планёрка" или "Иванов".
        date_from_iso: Начало диапазона.
        date_to_iso: Конец диапазона.
        creator_user_id: MXID пользователя из [from=...] в контексте. ОБЯЗАТЕЛЬНО передавай.

    Returns:
        Список найденных событий с UID или сообщение что не нашлось.

    Когда использовать:
        Когда пользователь спрашивает "когда у нас встреча с Петровым", "найди событие про релиз".
        Если нужен конкретный UID для отмены — сначала найди событие, потом отменяй.
    """
    import os
    import httpx

    body = {
        "matrix_room_id": matrix_room_id,
        "room_display_name": "",
        "query": query,
        "date_from_iso": date_from_iso,
        "date_to_iso": date_to_iso,
        "creator_user_id": creator_user_id or None,
    }
    r = httpx.post(
        "http://127.0.0.1:8284/calendar/find",
        headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
        json=body,
        timeout=30,
    )
    if r.status_code >= 400:
        return f"Ошибка поиска: {r.status_code} {r.text[:300]}"
    data = r.json()
    if data["count"] == 0:
        return f"По запросу «{query}» событий не найдено."
    lines = [f"Найдено по «{query}»: {data['count']}"]
    for e in data["events"]:
        loc = f" · место: {e['location']}" if e.get("location") else ""
        lines.append(f"- {e['start']}: {e['title']}{loc} (uid={e['uid']})")
    return "\n".join(lines)


def calendar_cancel_event(matrix_room_id: str, uid: str, creator_user_id: str = "") -> str:
    """
    Удалить событие из календаря по его UID.

    Args:
        matrix_room_id: ID Matrix-комнаты из контекста [matrix_room_id=...].
        uid: Полный UID события (получен из calendar_find_events или calendar_create_event).
        creator_user_id: MXID пользователя из [from=...] в контексте. ОБЯЗАТЕЛЬНО передавай.

    Returns:
        Подтверждение удаления или сообщение что не нашлось.

    Когда использовать:
        Когда пользователь просит "отмени встречу", "удали событие".
        Если не знаешь UID — сначала найди событие через calendar_find_events.
    """
    import os
    import httpx

    body = {
        "matrix_room_id": matrix_room_id,
        "room_display_name": "",
        "uid": uid,
        "creator_user_id": creator_user_id or None,
    }
    r = httpx.post(
        "http://127.0.0.1:8284/calendar/delete",
        headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
        json=body,
        timeout=30,
    )
    if r.status_code >= 400:
        return f"Ошибка удаления: {r.status_code} {r.text[:300]}"
    data = r.json()
    if data["deleted"]:
        return f"Событие удалено (uid={uid[:8]})."
    return f"Событие с UID {uid} не найдено."


def calendar_get_timezone(matrix_room_id: str, matrix_user_id: str = "") -> str:
    """
    Узнать таймзону текущей комнаты.

    Args:
        matrix_room_id: ID Matrix-комнаты.
        matrix_user_id: Опционально — MXID пользователя (для попытки прочесть его профиль).

    Returns:
        Имя таймзоны (например "Europe/Moscow") или сообщение, что не задана.

    Когда использовать:
        Перед созданием события, если в разговоре не упомянута TZ.
        Если функция вернула "не задана" — спроси пользователя в чате, в какой TZ работаем,
        и сохрани через calendar_set_timezone.
    """
    import os
    import httpx

    body = {
        "matrix_room_id": matrix_room_id,
        "matrix_user_id": matrix_user_id or None,
    }
    r = httpx.post(
        "http://127.0.0.1:8284/calendar/timezone",
        headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
        json=body,
        timeout=10,
    )
    if r.status_code >= 400:
        return f"Ошибка получения TZ: {r.status_code} {r.text[:300]}"
    tz = r.json().get("timezone")
    if tz:
        return f"Таймзона комнаты: {tz}"
    return (
        "Таймзона для этой комнаты не задана. "
        "Спроси пользователя, в какой TZ они работают (например, Europe/Moscow), "
        "и сохрани через calendar_set_timezone."
    )


def calendar_set_timezone(matrix_room_id: str, timezone: str) -> str:
    """
    Установить таймзону комнаты (override). После установки все будущие события
    в этой комнате создаются в этой TZ по умолчанию.

    Args:
        matrix_room_id: ID Matrix-комнаты.
        timezone: IANA имя таймзоны. Примеры: "Europe/Moscow", "Asia/Yekaterinburg",
                  "Asia/Novosibirsk", "Europe/Kaliningrad".

    Returns:
        Подтверждение или ошибка валидации.

    Когда использовать:
        Когда пользователь явно сказал "мы в Москве" / "у нас Екатеринбург" /
        "часовой пояс Калининград".
    """
    import os
    import httpx

    body = {
        "matrix_room_id": matrix_room_id,
        "timezone": timezone,
    }
    r = httpx.post(
        "http://127.0.0.1:8284/calendar/timezone/set",
        headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
        json=body,
        timeout=10,
    )
    if r.status_code == 400:
        return f"Невалидная таймзона: {timezone}. Используй IANA формат: Europe/Moscow."
    if r.status_code >= 400:
        return f"Ошибка установки TZ: {r.status_code} {r.text[:300]}"
    return f"Таймзона комнаты установлена: {timezone}."

def internet_search(query: str, max_results: int = 5) -> str:
    """
    Поиск в интернете для актуальной информации: новости, цены, погода,
    документация, факты, события. Используй когда нужны свежие данные,
    которых нет в твоей памяти, или информация может устареть.

    Args:
        query: Поисковый запрос на любом языке. Будь конкретным.
               Например: "курс евро к рублю сегодня", "погода Париж",
               "новости Anthropic Claude 4.7", "Python asyncio sleep документация".
        max_results: Сколько результатов вернуть (1-10). По умолчанию 5.

    Returns:
        Текстовая сводка: краткий ответ + список источников с заголовками,
        URL и сниппетами.

    Когда использовать:
        - Любые вопросы про "сейчас", "сегодня", "недавно"
        - Цены, курсы, погода
        - Новости, события, релизы
        - Информация о людях, компаниях, продуктах вне твоих знаний
        - Когда сомневаешься в актуальности своего ответа

    Когда НЕ использовать:
        - Пользователь спрашивает что-то из истории чата (используй archival_memory)
        - Корпоративные документы (используй kb_search_corporate когда появится)
        - Чисто математические/логические задачи
    """
    import os
    import httpx

    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return "Ошибка: TAVILY_API_KEY не настроен в окружении."

    max_results = max(1, min(10, max_results))

    try:
        r = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": True,
            },
            timeout=20,
        )
    except Exception as e:
        return f"Ошибка соединения с Tavily: {e}"

    if r.status_code == 401:
        return "Ошибка: Tavily API ключ невалиден."
    if r.status_code == 429:
        return "Превышен лимит запросов Tavily на этот месяц."
    if r.status_code >= 400:
        return f"Ошибка Tavily: {r.status_code} {r.text[:200]}"

    data = r.json()
    answer = (data.get("answer") or "").strip()
    results = data.get("results", []) or []

    parts = []
    if answer:
        parts.append(f"Краткий ответ: {answer}")
    if results:
        parts.append("\nИсточники:")
        for i, res in enumerate(results, 1):
            title = (res.get("title") or "").strip()[:120]
            url = res.get("url", "")
            content = (res.get("content") or "").strip()[:300]
            parts.append(f"\n{i}. {title}\n   URL: {url}\n   {content}")

    if not parts:
        return f"По запросу '{query}' ничего не найдено."

    return "\n".join(parts)

def kb_search_corporate(
    query: str,
    matrix_room_id: str = "",
    limit: int = 5,
) -> str:
    """
    Поиск в корпоративной базе знаний компании. Используй когда вопрос пользователя
    касается внутренних документов: регламенты, политики, должностные инструкции,
    описания процессов, технические спецификации, инструкции для сотрудников.

    Args:
        query: Поисковый запрос на естественном языке. Будь конкретным.
               Например: "процедура согласования отпуска", "DLP политика", "как оформить командировку".
        matrix_room_id: ID Matrix-комнаты, начинается с "!". Если задан, поиск также вернёт
                        приватные документы этой комнаты. Возьми из контекста разговора.
                        Если не знаешь — оставь пустым, тогда ищем только по общедоступным.
        limit: Сколько фрагментов вернуть (1-20). По умолчанию 5.

    Returns:
        Текстовая сводка найденных фрагментов с источниками и оценкой релевантности.
        Если ничего не найдено — соответствующее сообщение.

    Когда использовать:
        - Вопросы про корпоративные правила, регламенты, процедуры
        - "У нас есть документ про X?", "Как у нас оформляется Y?"
        - Технические вопросы про внутренние системы и процессы
        - Когда пользователь явно просит "посмотри в базе знаний"

    Когда НЕ использовать:
        - Общие вопросы (используй свои знания или internet_search)
        - Вопросы про календарь (используй calendar_*)
        - Личные заметки пользователя (используй kb_search_personal)
        - Свежие новости / актуальные данные (используй internet_search)
    """
    import os
    import httpx

    body = {
        "query": query,
        "matrix_room_id": matrix_room_id or None,
        "limit": max(1, min(20, limit)),
    }
    try:
        r = httpx.post(
            "http://127.0.0.1:8284/kb/search_corporate",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json=body,
            timeout=30,
        )
    except Exception as e:
        return f"Ошибка соединения с KB: {e}"

    if r.status_code >= 400:
        return f"Ошибка поиска в KB: {r.status_code} {r.text[:300]}"

    data = r.json()
    return data.get("formatted") or "По запросу ничего не найдено."


def kb_search_personal(
    query: str,
    matrix_user_id: str,
    limit: int = 5,
) -> str:
    """
    Поиск в ЛИЧНОЙ базе знаний пользователя — заметки, факты, контакты,
    которые он сам просил запомнить. Только для запрашивающего пользователя
    (один пользователь не видит личные знания другого).

    Args:
        query: Поисковый запрос. Например: "телефон Иванова", "дата рождения жены",
               "проект Альфа дедлайн".
        matrix_user_id: MXID пользователя, чью личную базу ищем. Возьми из [from=...]
                        в контексте сообщения. Обязательное поле.
        limit: Сколько фрагментов вернуть (1-20). По умолчанию 5.

    Returns:
        Текстовая сводка найденных фрагментов или сообщение что ничего не найдено.

    Когда использовать:
        - Пользователь спрашивает что-то про себя ("какой у меня день рождения у Петрова?",
          "что я просил запомнить про проект X?")
        - Пользователь явно ссылается на свою память ("я тебе говорил про...")

    Когда НЕ использовать:
        - Корпоративные документы (используй kb_search_corporate)
        - История текущего диалога (это уже в твоей памяти / archival_memory)
        - Если matrix_user_id неизвестен — лучше переспроси или используй другой инструмент
    """
    import os
    import httpx

    if not matrix_user_id:
        return "Ошибка: для поиска в личной базе нужен matrix_user_id."

    body = {
        "query": query,
        "matrix_user_id": matrix_user_id,
        "limit": max(1, min(20, limit)),
    }
    try:
        r = httpx.post(
            "http://127.0.0.1:8284/kb/search_personal",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json=body,
            timeout=30,
        )
    except Exception as e:
        return f"Ошибка соединения с KB: {e}"

    if r.status_code >= 400:
        return f"Ошибка поиска: {r.status_code} {r.text[:300]}"

    data = r.json()
    return data.get("formatted") or "В вашей личной базе ничего не найдено."
def kb_list_personal(matrix_user_id: str) -> str:
    """
    Получить список документов в личной базе знаний пользователя.

    Args:
        matrix_user_id: MXID пользователя. Возьми из [from=...] в контексте.

    Returns:
        Текстовый список документов с количеством фрагментов и датой загрузки,
        либо сообщение что база пуста.

    Когда использовать:
        - Пользователь спрашивает «что у меня в KB?», «какие у меня документы?»
        - Нужно показать содержимое личной базы перед удалением
        - Перед использованием kb_search_personal можно сказать пользователю что искать есть смысл
    """
    import os
    import httpx

    if not matrix_user_id:
        return "Не могу показать список — неизвестно от кого запрос (MXID не задан)."

    try:
        r = httpx.post(
            "http://127.0.0.1:8284/kb/personal/list",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json={"matrix_user_id": matrix_user_id},
            timeout=20,
        )
    except Exception as e:
        return f"Ошибка соединения с KB: {e}"

    if r.status_code >= 400:
        return f"Ошибка API: {r.status_code} {r.text[:200]}"

    data = r.json()
    docs = data.get("documents", [])
    if not docs:
        return "В твоей личной базе пока ничего нет. Чтобы добавить — приложи файл в чат."

    lines = [f"В твоей личной KB: {data.get('count', 0)} документов, "
             f"{data.get('total_chunks', 0)} фрагментов."]
    for d in docs:
        lines.append(f"  - {d['source']}: {d['chunks']} фрагментов")
    return "\n".join(lines)
def kb_delete_personal(matrix_user_id: str, source: str) -> str:
    """
    Удалить документ из ЛИЧНОЙ базы знаний пользователя.

    Args:
        matrix_user_id: MXID владельца KB. Возьми из [from=...] в контексте.
        source: Имя файла (case-insensitive). Например "notes.pdf".

    Returns:
        Подтверждение удаления или сообщение что файл не найден.

    Когда использовать:
        - Пользователь просит "забудь файл X", "удали из памяти X", "выкини документ X"
        - Перед удалением желательно подтвердить с пользователем имя файла,
          особенно если он назвал файл неточно (можно сначала kb_list_personal)
        - Деструктивная операция — без чёткого указания пользователя НЕ ВЫЗЫВАТЬ
    """
    import os
    import httpx

    if not matrix_user_id or not source:
        return "Не могу удалить — нужны matrix_user_id и source (имя файла)."

    try:
        r = httpx.post(
            "http://127.0.0.1:8284/kb/personal/delete",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json={"matrix_user_id": matrix_user_id, "source": source},
            timeout=20,
        )
    except Exception as e:
        return f"Ошибка соединения с KB: {e}"

    if r.status_code >= 400:
        return f"Ошибка API: {r.status_code} {r.text[:200]}"

    data = r.json()
    deleted = data.get("deleted", 0)
    if deleted == 0:
        return f"Документа '{source}' в твоей KB не найдено."
    matched = data.get("matched_source", source)
    return f"Удалил '{matched}' — {deleted} фрагментов."


def kb_info_personal(matrix_user_id: str, source: str) -> str:
    """
    Получить детали одного документа в личной KB: размер, фрагменты, sha256, даты.

    Args:
        matrix_user_id: MXID владельца. Возьми из [from=...] в контексте.
        source: Имя файла (case-insensitive). Например "notes.pdf".

    Returns:
        Текст с деталями документа или сообщение что файл не найден.

    Когда использовать:
        - Пользователь спрашивает "когда я загрузил X?", "что за файл X?", "детали по X"
        - "Сколько у меня фрагментов в Y?"
    """
    import os
    import httpx

    if not matrix_user_id or not source:
        return "Не могу получить детали — нужны matrix_user_id и source."

    try:
        r = httpx.post(
            "http://127.0.0.1:8284/kb/personal/info",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json={"matrix_user_id": matrix_user_id, "source": source},
            timeout=20,
        )
    except Exception as e:
        return f"Ошибка соединения с KB: {e}"

    if r.status_code >= 400:
        return f"Ошибка API: {r.status_code} {r.text[:200]}"

    data = r.json()
    if not data.get("found"):
        return f"Документа '{source}' в твоей KB нет."

    chunks = data.get("chunks", 0)
    chars = data.get("total_chars", 0)
    sha = (data.get("sha256") or "")[:12] + "..."
    first = data.get("first_added", "?")
    last = data.get("last_added", "?")

    return (
        f"Документ '{data.get('source')}':\n"
        f"  - фрагментов: {chunks}\n"
        f"  - размер текста: ~{chars} символов\n"
        f"  - sha256: {sha}\n"
        f"  - первая загрузка: {first}\n"
        f"  - последняя: {last}"
    )



def respect_kb_search(
    query: str,
    matrix_user_id: str,
    limit: int = 5,
) -> str:
    """
    Поиск в КОРПОРАТИВНОЙ базе знаний Респект.Чата с учётом прав пользователя.

    Это основная корпоративная KB компании: методические материалы, инфоповоды,
    экспертные материалы, сравнения с конкурентами, обучающие материалы.
    Все материалы структурированы по разделам и имеют разные уровни доступа —
    каждый пользователь видит только то, к чему у него есть доступ в Респект.Чате.

    К материалам могут быть прикреплены файлы (PDF, DOCX), видео и аудио —
    Leo вернёт прямые ссылки на них.

    Args:
        query: Поисковый запрос на русском языке. Будь конкретным:
               "сравнение Гарант Консультант", "инструкция по командировкам",
               "методичка для новых сотрудников".
        matrix_user_id: MXID пользователя — ОБЯЗАТЕЛЬНО возьми из [from=...]
                        в контексте сообщения. Без него поиск работать не будет
                        (нужен для проверки прав доступа).
        limit: Сколько материалов вернуть (1-20). По умолчанию 5.

    Returns:
        Текстовая сводка найденных материалов с разделами, фрагментами,
        ссылками на прикреплённые файлы. Либо сообщение что ничего не найдено,
        либо что у пользователя нет доступа.

    Когда использовать:
        - Вопросы про корпоративные материалы Респект.Чата
        - "Что у нас есть про X?", "Где почитать про Y?", "Найди материалы по Z"
        - Сравнения с конкурентами (Гарант, КонсультантПлюс, Актион и др.)
        - Методические материалы, инфоповоды, экспертные материалы
        - Обучающие материалы для сотрудников и пользователей

    Когда НЕ использовать:
        - Личные заметки пользователя (kb_search_personal)
        - Старая корпоративная KB на pgvector (kb_search_corporate)
        - Свежие новости / актуальные данные из интернета (internet_search)
        - Календарь (calendar_*)
    """
    import os
    import httpx

    if not matrix_user_id:
        return "Не могу искать в корпоративной KB — нужен MXID пользователя (matrix_user_id)."

    body = {
        "query": query,
        "matrix_user_id": matrix_user_id,
        "limit": max(1, min(20, limit)),
    }
    try:
        r = httpx.post(
            "http://127.0.0.1:8284/respect_kb/search",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json=body,
            timeout=30,
        )
    except Exception as e:
        return f"Ошибка соединения с КБ Респект.Чата: {e}"

    if r.status_code == 503:
        return "Корпоративная KB Респект.Чата сейчас не подключена. Обратись к администратору."
    if r.status_code >= 400:
        return f"Ошибка поиска: HTTP {r.status_code}: {r.text[:300]}"

    data = r.json()
    return data.get("formatted") or "По запросу ничего не найдено."


# ============================================================
# Регистрация в Letta
# ============================================================

def leo_create_file(
    matrix_room_id: str,
    creator_user_id: str,
    filename: str,
    format: str,
    content_md: str = "",
    content_json: str = "",
    title: str = "",
) -> str:
    """Создать файл (md/html/docx/pdf/xlsx/pptx) и отправить в Matrix-чат.

    Args:
        matrix_room_id: ID Matrix-комнаты куда отправить файл.
        creator_user_id: MXID отправителя из контекста.
        filename: имя файла без расширения.
        format: формат файла, один из md html docx pdf xlsx pptx.
        content_md: содержимое в Markdown для текстовых форматов.
        content_json: JSON-строка для xlsx и pptx.
        title: опциональный заголовок документа.

    Returns:
        Строка с подтверждением или описанием ошибки.
    """
    import os
    import json
    import httpx

    base = os.environ.get("BRIDGE_INTERNAL_URL", "http://127.0.0.1:8284")
    token = os.environ.get("BRIDGE_INTERNAL_TOKEN", "")

    if format not in ("md", "html", "docx", "pdf", "xlsx", "pptx"):
        return f"Ошибка: формат {format!r} не поддерживается. Используй: md, html, docx, pdf, xlsx, pptx."

    if format in ("xlsx", "pptx"):
        if not content_json:
            return f"Ошибка: для формата {format} нужен content_json."
        try:
            json.loads(content_json)
        except Exception as e:
            return f"Ошибка: content_json не валиден JSON: {e}"
    else:
        if not content_md:
            return f"Ошибка: для формата {format} нужен content_md (markdown)."

    payload = {
        "matrix_room_id": matrix_room_id,
        "creator_user_id": creator_user_id,
        "filename": filename,
        "format": format,
        "content_md": content_md or None,
        "content_json": content_json or None,
        "title": title or None,
    }

    try:
        r = httpx.post(
            f"{base}/files/create",
            headers={"X-Internal-Token": token},
            json=payload,
            timeout=120,
        )
    except Exception as e:
        return f"Ошибка соединения: {e}"

    if r.status_code >= 400:
        return f"Ошибка создания файла: HTTP {r.status_code}: {r.text[:300]}"

    data = r.json()
    size_kb = data.get("size_bytes", 0) / 1024
    return (
        f"Файл {data.get('filename')} ({size_kb:.1f} KB, {format.upper()}) "
        f"отправлен в чат."
    )


TOOLS = [
    calendar_create_event,
    calendar_list_events,
    calendar_find_events,
    calendar_cancel_event,
    calendar_get_timezone,
    calendar_set_timezone,
    internet_search,
    kb_search_corporate,
    kb_search_personal,
    kb_list_personal,
    kb_delete_personal,
    kb_info_personal,
    leo_create_file,
    respect_kb_search,
]


def main() -> None:
    headers = {"Authorization": f"Bearer {LETTA_PASS}"}

    r = httpx.get(f"{LETTA_URL}/v1/tools/", headers=headers, timeout=30)
    r.raise_for_status()
    existing = {t["name"]: t["id"] for t in r.json()}

    for fn in TOOLS:
        name = fn.__name__
        source = inspect.getsource(fn)
        payload = {
            "source_code": source,
            "source_type": "python",
        }

        if name in existing:
            tool_id = existing[name]
            r = httpx.patch(
                f"{LETTA_URL}/v1/tools/{tool_id}",
                headers=headers,
                json=payload,
                timeout=30,
            )
            print(f"Updated  {name:30s} -> {tool_id} [{r.status_code}]")
            if r.status_code >= 400:
                print(f"   Body: {r.text[:300]}")
                sys.exit(1)
        else:
            r = httpx.post(
                f"{LETTA_URL}/v1/tools/",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if r.status_code >= 400:
                print(f"FAILED   {name}: {r.status_code} {r.text[:500]}")
                sys.exit(1)
            tool_id = r.json()["id"]
            print(f"Created  {name:30s} -> {tool_id}")


if __name__ == "__main__":
    main()

