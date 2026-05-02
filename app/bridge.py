"""
Matrix-Letta Bridge — Этап 2.

Минимальная рабочая логика:
- bot принимает invites только от allowed-доменов
- расшифровывает E2EE
- для каждой комнаты создаёт отдельного Letta-агента (lazy)
- отправляет сообщение агенту, отвечает в треде
"""
import asyncio
import logging
import time
import os
import sys
from pathlib import Path
from markdown_it import MarkdownIt

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    MatrixRoom,
    MegolmEvent,
    RoomMemberEvent,
    RoomMessageText,
    SyncResponse,
    ReactionEvent,
    RedactionEvent,
)
from nio import (
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageAudio,
    RoomMessageVideo,
    RoomMessageMedia,
    RoomEncryptedMedia,
)
from app import kb_handler
from app import feedback_handler
from app import leo_handler  # v0.9

from aiohttp import web as aiohttp_web  # v1.1.0 health endpoint
from app.audit import AuditLog
from app.feedback_logger import FeedbackLogger, ResponseRecord  # v0.9
from app.calendar_client import CalendarClient
from app.letta_client import LettaClient, LettaError
from app.reminders import ReminderWatcher
from app.room_mapping import RoomMapping

SYSTEM_PROMPT = """\
Ты Leo — корпоративный AI-ассистент. Отвечаешь по-русски, кратко, дружелюбно. Не знаешь — честно говори.

ЧЕТЫРЕ ГЛАВНЫХ ПРАВИЛА:
1. Загрузка KB = прикрепление файла в чат. CLI/python/kb_ingest не упоминай — их нет.
2. Все твои инструменты работают. Не говори "не настроено" / "не работает".
3. Не выдумывай файлы, ID, команды. Сомневаешься — вызови tool и проверь.
4. ВЫЗЫВАЙ TOOLS, НЕ ОТПРАВЛЯЙ ПОЛЬЗОВАТЕЛЯ ЗА SLASH-КОМАНДАМИ. Если можно ответить через kb_search_personal — вызывай его, не пиши "загляни через /kb_search". У ТЕБЯ есть доступ к личной KB через tools.

ИНСТРУМЕНТЫ:
- calendar_create_event / list_events / find_events / cancel_event / get_timezone — календарь
- internet_search — веб-поиск (курсы, погода, новости, факты)
- kb_search_corporate — корпоративная KB (общие документы)
- kb_search_personal / kb_list_personal / kb_info_personal / kb_delete_personal — личная KB пользователя
- leo_create_file — создать и отправить файл (md/html/docx/pdf/xlsx/pptx). Используй когда юзер просит "сделай отчёт", "создай презентацию", "сформируй PDF/Excel", или когда ответ — длинный структурированный документ который удобнее как файл.
- memory_insert(label="human", content="...") — записать факт О ПОЛЬЗОВАТЕЛЕ в core memory
- memory_replace(label, ...) — заменить устаревший факт в core memory
- archival_memory_insert / archival_memory_search — резервный канал для длинных текстов

ВАЖНО ПРО ПАМЯТЬ:
- Блок "human" — факты о ПОЛЬЗОВАТЕЛЕ (имя, должность, таймзона, предпочтения, проекты). Этот блок ОБЩИЙ для всех твоих агентов одного пользователя.
- Блок "persona" — это про ТЕБЯ (Leo). НЕ записывай факты о пользователе в persona.
- Когда пользователь представляется ("меня зовут X", "я работаю Y", "предпочитаю Z") — ОБЯЗАТЕЛЬНО вызови memory_insert(label="human", content="X — Y, предпочитает Z").
- Когда пользователь упоминает что-то важное о себе мимоходом — тоже сохраняй через memory_insert(label="human", ...).

КАК РАБОТАТЬ С ЛИЧНОЙ KB:
- У ТЕБЯ ЕСТЬ ПРЯМОЙ ДОСТУП через kb_search_personal/list/info/delete.
- Любой вопрос про "мою документацию", "мои заметки", "мою таблицу X", "что у меня в KB" → СРАЗУ вызывай kb_search_personal или kb_list_personal.
- НЕ говори "у меня нет доступа", НЕ говори "загляни через /kb_search" — это твои tools.
- ВСЕГДА передавай matrix_user_id из [from=...] метаданных.

ФОРМАТНЫЕ ИНСТРУКЦИИ В ЛИЧНОЙ KB:
Если пользователь просит специфичный формат вывода — например, "weekly summary", "итоги недели", "отчёт для техсовещания", "еженедельный отчёт" — сначала вызови kb_search_personal с запросом про инструкцию для этого формата (например: "weekly summary instructions" или "инструкция итоги недели"). Если найдена инструкция — следуй ей строго. Если инструкции нет — отформатируй по общим принципам результат-ориентированно (что сделано + следствие), сгруппируй по смысловым блокам, без выдумывания цифр.

КОМБИНИРОВАННЫЕ ЗАПРОСЫ:
Если пользователь хочет ответ из НЕСКОЛЬКИХ источников — вызывай tools последовательно, потом синтезируй ответ.

Примеры:
- "Расскажи про X из моей документации, и найди в интернете лучшие практики" →
  1) kb_search_personal(query="X") — получить содержимое из KB
  2) internet_search(query="X best practices") — получить общие практики
  3) Объединить: краткий пересказ из KB + типовые практики из интернета

- "Запланируй на завтра разбор моей документации" →
  1) kb_list_personal — посмотреть какие документы есть
  2) calendar_create_event — создать встречу с описанием

- "У меня в документации есть Y, найди где это упоминается ещё" →
  1) kb_search_personal(query="Y") — найти все фрагменты в KB
  2) internet_search(query="Y") — поискать в интернете
  3) Сравнить и собрать ответ

КОНТЕКСТ КАЖДОГО СООБЩЕНИЯ:
В начале user-сообщения метаданные:
- [now_utc=...] текущий момент в UTC (ISO 8601)
- [user_tz=Europe/Paris] таймзона отправителя (может быть пустой)
- [today_local=2026-04-30 Thursday (четверг)] СЕГОДНЯШНЯЯ дата + день недели в TZ пользователя
- [upcoming_days=2026-04-30 Thu, 2026-05-01 Fri, ...] карта ближайших 14 дней (TZ пользователя)
- [matrix_room_id=!XXX:server] ID комнаты для calendar_*
- [from=@user:server] кто написал — ВСЕГДА бери MXID отсюда для kb_*_personal

КАЛЕНДАРЬ:
- Все события в UTC. ISO 8601 с +00:00.
- Каждое созданное событие = напоминание (default 15 минут до начала).
- v1.1.0: время напоминания НАСТРАИВАЕТСЯ через параметр remind_minutes_before в calendar_create_event.
  Парси из текста пользователя:
    * "напомни за час" → 60
    * "напомни за 2 часа" → 120
    * "напомни за 30 минут" → 30
    * "напомни за день" → 1440
    * "без напоминания" / "не напоминай" → 0
    * не указано — используй default 15
  Допустимый диапазон: 0..10080 (от 0 минут до недели).
- Длительность по умолчанию 30 мин (НЕ час).
- creator_user_id = MXID из [from=...] всегда передавай в calendar_create_event, calendar_list_events, calendar_find_events, calendar_cancel_event. Это обязательно — без него события не найдутся.
- "Через 13 минут", "завтра в 10" — пересчитай через user_tz, потом в UTC.
- Если user_tz пустой — спроси TZ или явно используй UTC.
- В ответе показывай ВРЕМЯ В TZ ПОЛЬЗОВАТЕЛЯ, не в UTC.
- НЕТ Markdown-таблиц — Element их не отображает. Используй простые строки:
  🗓 Календарь на 25 апреля (Europe/Paris):
  🔹 21:15 – 21:45 — Тест Leo
  Итого: N событий.

ДНИ НЕДЕЛИ (v1.0.5):
- День недели для СЕГОДНЯ — бери из [today_local=...]. Не пересчитывай сам.
- Для ДРУГИХ дат в ближайшие 14 дней (завтра, "1 мая", "следующий вторник"):
  БЕРИ ИЗ [upcoming_days=...]. Там уже есть готовая карта дат → дней недели.
  НЕ ВЫЧИСЛЯЙ САМ — это типичная ошибка LLM. Просто найди дату в списке.
- Для дат за пределами 14 дней — лучше скажи «не знаю точно, проверьте
  сами» чем указать неверно. Или используй internet_search для проверки.
- При составлении таблиц с датами проверяй КАЖДУЮ строку по upcoming_days.

ЛИЧНАЯ KB:
- Документы пользователя: PDF, DOCX, MD, TXT, XLSX, CSV, JSON, до 10 MB.
- Загрузка: пользователь прикрепляет файл в этот чат — система обработает сама.
- Поиск через kb_search_personal — гибридный (точные совпадения для идентификаторов + семантика).
- /kb_help, /kb_list, /kb_search, /kb_info, /kb_delete — slash-команды для пользователя; bridge их обрабатывает сам.

ОТВЕТЫ:
- Кратко. Не показывай вычисления — только результат.
- memory_insert(label="human", content="...") для важных фактов о пользователе. НЕ путай с persona (она про Leo).
- Сообщения от разных людей в одной комнате — разные собеседники.

ПРИМЕРЫ ХОРОШИХ ОТВЕТОВ:

Q: «Как загрузить файлы в KB?»
A: «Просто прикрепи файл в этот чат — я обработаю и запомню. Поддерживаю PDF, DOCX, MD, TXT, XLSX, CSV, JSON, до 10 MB. Команда /kb_help — полная справка.»

Q: «Расскажи про мою таблицу t26041»
A: [вызывает kb_search_personal(query="t26041")] «t26041 — справочник уровней дерева в Эльбазе. По твоей документации это 6 строк (Клиент, Контактное лицо, Направление, Задание, Подзадание, Комментарий). Поле t26047.id26041 ссылается на t26041.idkart...»

Q: «Расскажи мне про t26041 из моей документации, и найди в интернете типовые практики»
A: [вызывает kb_search_personal("t26041") + internet_search("PostgreSQL справочные таблицы практики")] «По твоей KB t26041 — справочник из 6 строк уровней дерева Эльбазы. По типовым практикам PostgreSQL для таких справочников: используют ENUM или таблицу с FK; индексы не нужны при малом размере; кешировать в приложении... [источники]»

Q: «Покажи курсы валют»
A: [вызывает internet_search] «По данным [источник]: USD/RUB X.XX, EUR/RUB Y.YY на DD.MM.YYYY.»

Q: «Покажи мой календарь» / «что у меня на этой неделе» / «какие встречи завтра»
A: [вызывает calendar_list_events(matrix_room_id=<из контекста>, date_from_iso=<сегодня>, date_to_iso=<+14 дней>, creator_user_id=<из [from=...]>)] «Календарь на X апреля — Y мая: ...»
ВАЖНО: calendar_list_events ВСЕГДА вызывать через tool, никогда не отвечать "событий нет" без вызова tool.

Q: «Найди встречу с Петровым»
A: [вызывает calendar_find_events(matrix_room_id=<из контекста>, query="Петров", date_from_iso=<сегодня>, date_to_iso=<+30 дней>, creator_user_id=<из [from=...]>)]
ПУБЛИЧНЫЙ КАЛЕНДАРЬ (CalDAV):
Пользователи могут подключить свой календарь Leo к Apple Calendar, iPhone, Outlook, Android.
Когда пользователь спрашивает про подключение календаря, CalDAV, или просит URL/пароль — давай эту инструкцию:

URL для подключения зависит от комнаты. Формат:
https://cal.respectrb.ru/assistant/room_XXXX/

где room_XXXX — ID текущей Matrix-комнаты без "!" и без ":mtx.respectrb.ru".
Например для комнаты !jSiUOKuhoZWVUfvgRY:mtx.respectrb.ru → room_jSiUOKuhoZWVUfvgRY

Логин для подключения = имя пользователя из его MXID (часть до двоеточия, например @viacheslav:... → viacheslav).
Пароль выдаётся администратором. Если пользователь забыл пароль — скажи что нужно обратиться к администратору.

Инструкция для iPhone/iPad:
Настройки → Календарь → Аккаунты → Добавить аккаунт → Другой → Учётная запись CalDAV
Сервер: cal.respectrb.ru  Имя пользователя: их_логин  Пароль: их_пароль

Для Mac: Системные настройки → Учётные записи интернета → + → CalDAV (Расширенный)
Адрес: https://cal.respectrb.ru/assistant/room_XXXX/  Порт: 443  SSL: вкл.

ВАЖНО: Подключаться нужно именно к URL конкретной комнаты, не к /viacheslav/ — там другая коллекция.

"""


# Markdown-рендерер для Leo-ответов: code blocks, таблицы, жирный, ссылки.
# Без html: True — чтобы не пропускать сырой HTML от LLM (защита от инъекций).
_MD = MarkdownIt("commonmark", {"breaks": True, "html": False, "linkify": True}).enable("table")

# ---------- Конфигурация ----------
class Config:
    def __init__(self) -> None:
        self.homeserver = self._req("MATRIX_HOMESERVER_URL")
        self.user_id = self._req("BRIDGE_USER_ID")
        self.device_id = self._req("BRIDGE_DEVICE_ID")
        self.access_token = self._req("BRIDGE_ACCESS_TOKEN")
        self.data_dir = Path(os.environ.get("BRIDGE_DATA_DIR", "/opt/ai/bridge/data"))
        self.allowed_domains = set(
            d.strip() for d in os.environ.get("BRIDGE_ALLOWED_DOMAINS", "").split(",") if d.strip()
        )
        self.mention_names = set(
            n.strip().lower() for n in os.environ.get("BRIDGE_MENTION_NAMES", "leo").split(",") if n.strip()
        )

        self.letta_url = self._req("LETTA_URL")
        self.letta_password = self._req("LETTA_SERVER_PASSWORD")
        self.letta_model = self._req("LETTA_MODEL")
        self.letta_embedding = self._req("LETTA_EMBEDDING")

        # Календарь / напоминания
        self.radicale_url = self._req("RADICALE_URL")
        self.radicale_user = self._req("RADICALE_USER")
        self.radicale_password = self._req("RADICALE_PASSWORD")
        self.database_url_ai = self._req("DATABASE_URL_AI")

        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    @staticmethod
    def _req(key: str) -> str:
        v = os.environ.get(key)
        if not v:
            sys.stderr.write(f"FATAL: {key} not set in environment\n")
            sys.exit(1)
        return v


# ---------- Логика бота ----------
class Bridge:
    def __init__(
        self,
        cfg: Config,
        letta: LettaClient,
        rooms: RoomMapping,
        audit: AuditLog,
    ) -> None:
        self.cfg = cfg
        self.letta = letta
        self.rooms = rooms
        self.audit = audit
        # v1.4.0b: подключаем shared human block resolver
        self.letta._human_block_resolver = self._resolve_human_block
        # v0.9: feedback logger (заполняется в run() после создания pg_pool)
        self.feedback: FeedbackLogger | None = None
        # v1.0.1 health endpoint state
        self._started_at = time.monotonic()
        self._started_at_iso: str | None = None
        self._health_runner: aiohttp_web.AppRunner | None = None

        store_path = cfg.data_dir / "store"
        store_path.mkdir(parents=True, exist_ok=True)

        client_config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
        )
        self.client = AsyncClient(
            homeserver=cfg.homeserver,
            user=cfg.user_id,
            device_id=cfg.device_id,
            store_path=str(store_path),
            config=client_config,
        )
        self.client.access_token = cfg.access_token
        self.client.user_id = cfg.user_id
        self.client.device_id = cfg.device_id

        # Колбэки
        self.client.add_event_callback(self._on_message, RoomMessageText)
        # v0.8: file uploads -> personal KB
        self.client.add_event_callback(self._on_file_message, RoomMessageMedia)
        # v0.8.1: то же для encrypted media (E2EE-комнаты)
        self.client.add_event_callback(self._on_file_message, RoomEncryptedMedia)
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        # v1.4.1: отслеживаем leave/kick — если комната опустеет, выходим
        self.client.add_event_callback(self._on_member, RoomMemberEvent)
        self.client.add_event_callback(self._on_megolm_undecrypted, MegolmEvent)
        # v0.9: callbacks для feedback (реакции и их отмена)
        self.client.add_event_callback(self._on_reaction, ReactionEvent)
        self.client.add_event_callback(self._on_redaction, RedactionEvent)
        self.client.add_response_callback(self._after_sync, SyncResponse)

        self.log = logging.getLogger("bridge")
        self.startup_token: str | None = None

        # Reminder watcher — будет запущен из run() как фоновая задача
        self.reminders: ReminderWatcher | None = None
        self._reminder_task: asyncio.Task | None = None

    # ---------- helpers ----------
    @staticmethod
    def _domain_of(mxid: str) -> str:
        if ":" not in mxid:
            return ""
        return mxid.split(":", 1)[1]

    def _is_allowed_user(self, mxid: str) -> bool:
        return self._domain_of(mxid) in self.cfg.allowed_domains

    def _is_addressed_to_bot(self, body: str, room: MatrixRoom) -> bool:
        members = [u for u in room.users if u != self.cfg.user_id]
        is_dm = len(members) <= 1
        if is_dm:
            return True
        text_lower = body.lower()
        if self.cfg.user_id.lower() in text_lower:
            return True
        for name in self.cfg.mention_names:
            if (
                f"@{name}" in text_lower
                or f"{name}:" in text_lower
                or text_lower.startswith(f"{name} ")
                or text_lower == name
            ):
                return True
        return False

    def _strip_mention(self, body: str) -> str:
        """Убрать упоминание бота из начала сообщения, чтобы не сбивать LLM."""
        result = body.strip()
        for name in self.cfg.mention_names:
            for prefix in (f"@{name}", f"{name}:", f"{name},", name):
                if result.lower().startswith(prefix.lower()):
                    result = result[len(prefix):].lstrip(" ,:;!?")
                    break
        # MXID
        if result.lower().startswith(self.cfg.user_id.lower()):
            result = result[len(self.cfg.user_id):].lstrip(" ,:;!?")
        return result or body  # если осталось пусто — вернём оригинал

    async def _get_user_tz(self, user_id: str) -> str | None:
        """
        Прочитать TZ пользователя из Matrix-профиля через MSC4175.
        Возвращает IANA-имя ('Europe/Paris') или None.
        """
        try:
            import httpx
            url = (
                f"{self.cfg.homeserver}/_matrix/client/v3/profile/"
                f"{user_id}/us.cloke.msc4175.tz"
            )
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    url,
                    headers={"Authorization": f"Bearer {self.cfg.access_token}"},
                )
                if r.status_code != 200:
                    return None
                data = r.json()
                tz = data.get("tz") or data.get("us.cloke.msc4175.tz")
                if isinstance(tz, str) and "/" in tz:
                    return tz
        except Exception as e:
            self.log.debug("Failed to read user TZ for %s: %s", user_id, e)
        return None

    # ---------- callbacks ----------
    async def _after_sync(self, response: SyncResponse) -> None:
        # Trust all devices on allowed domains, чтобы E2EE работало
        for user_id, devices in self.client.device_store.items():
            if not self._is_allowed_user(user_id):
                continue
            for device in devices.values():
                if not device.verified:
                    self.client.verify_device(device)

        if self.startup_token is None:
            self.startup_token = response.next_batch
            self.log.info(
                "Sync established. Initial batch=%s. Processing live messages.",
                self.startup_token,
            )

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        if event.state_key != self.cfg.user_id:
            return
        inviter = event.sender
        if not self._is_allowed_user(inviter):
            self.log.warning(
                "Invite from disallowed domain: room=%s inviter=%s",
                room.room_id, inviter,
            )
            return
        self.log.info("Joining room %s by invite from %s", room.room_id, inviter)
        await self.client.join(room.room_id)

    async def _on_member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        """
        v1.4.1: отслеживаем leave/kick events.
        Если в комнате остался только Leo — выходим из неё и удаляем агента.
        """
        # Реагируем только на leave/ban не самого Leo
        if event.sender == self.cfg.user_id:
            return
        if event.membership not in ("leave", "ban"):
            return

        # Подсчитываем активных участников комнаты (кроме Leo)
        try:
            others = [
                uid for uid, member in room.users.items()
                if uid != self.cfg.user_id
            ]
            if others:
                # В комнате ещё есть люди — ничего не делаем
                return

            self.log.info(
                "[%s] Room is now empty (only Leo left), cleaning up...",
                room.room_id[:12],
            )
            await self._cleanup_empty_room(room.room_id, room.display_name)
        except Exception as e:
            self.log.warning(
                "[%s] _on_member cleanup check failed: %s",
                room.room_id[:12], e,
            )

    async def _cleanup_empty_room(self, room_id: str, room_name: str | None = None) -> None:
        """
        v1.4.1: Leo покидает пустую комнату.
        - Удаляет Letta-агента
        - Чистит mapping в bridge.sqlite
        - Делает room_leave + room_forget
        """
        try:
            # 1. Удалить Letta-агента (если есть)
            agent_id, _ = await self.rooms.get_state(room_id)
            if agent_id:
                try:
                    r = await self.letta.client.delete(f"/v1/agents/{agent_id}")
                    self.log.info(
                        "[%s] cleanup: deleted agent %s -> %s",
                        room_id[:12], agent_id, r.status_code,
                    )
                except Exception as e:
                    self.log.warning("[%s] cleanup: delete agent failed: %s", room_id[:12], e)

            # 2. Удалить mapping room_agents
            try:
                await self.rooms.delete_room(room_id)
                self.log.info("[%s] cleanup: removed from room_agents", room_id[:12])
            except Exception as e:
                self.log.warning("[%s] cleanup: delete_room failed: %s", room_id[:12], e)

            # 3. Покинуть комнату
            try:
                await self.client.room_leave(room_id)
                self.log.info("[%s] cleanup: left the room", room_id[:12])
            except Exception as e:
                self.log.warning("[%s] cleanup: room_leave failed: %s", room_id[:12], e)

            # 4. Забыть комнату (чтобы не возвращалась в синке)
            try:
                await self.client.room_forget(room_id)
                self.log.info("[%s] cleanup: room_forget OK", room_id[:12])
            except Exception as e:
                self.log.warning("[%s] cleanup: room_forget failed: %s", room_id[:12], e)
        except Exception as e:
            self.log.exception("[%s] _cleanup_empty_room failed: %s", room_id[:12], e)

    async def _on_megolm_undecrypted(
        self, room: MatrixRoom, event: MegolmEvent
    ) -> None:
        self.log.debug(
            "Undecrypted in room=%s sender=%s session=%s",
            room.room_id, event.sender, event.session_id,
        )

    async def _start_health_server(self, pg_pool) -> None:
        """v1.0.1: запустить aiohttp /health endpoint на 127.0.0.1:9090."""
        app = aiohttp_web.Application()
        # Передаём ссылки в request через app context
        app["bridge"] = self
        app["pg_pool"] = pg_pool
        app.router.add_get("/health", self._handle_health)
        # v1.5.0: file upload endpoint (вызывается из internal_api)
        app.router.add_post("/internal/files/upload", self._handle_file_upload)
        runner = aiohttp_web.AppRunner(app, access_log=None)
        await runner.setup()
        site = aiohttp_web.TCPSite(runner, host="127.0.0.1", port=9090)
        await site.start()
        self._health_runner = runner
        self.log.info("Health endpoint listening on http://127.0.0.1:9090/health")

    async def _stop_health_server(self) -> None:
        if self._health_runner is not None:
            try:
                await self._health_runner.cleanup()
            except Exception as e:
                self.log.warning("health server cleanup failed: %s", e)

    async def _handle_health(self, request) -> "aiohttp_web.Response":
        """v1.0.1: GET /health → JSON со статусом."""
        bridge = request.app["bridge"]
        pg_pool = request.app["pg_pool"]
        uptime = int(time.monotonic() - bridge._started_at)
        result: dict = {
            "status": "ok",
            "version": "v1.6.0",
            "uptime_seconds": uptime,
            "started_at": bridge._started_at_iso,
            "letta_reachable": False,
            "pg_reachable": False,
            "agents_count": None,
            "responses_last_hour": None,
            "responses_last_24h": None,
        }
        problems = []

        # 1. Postgres ping + counts
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute("SELECT 1")
                result["pg_reachable"] = True
                row = await conn.fetchrow(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM feedback.responses
                         WHERE created_at > NOW() - INTERVAL '1 hour') AS hour,
                        (SELECT COUNT(*) FROM feedback.responses
                         WHERE created_at > NOW() - INTERVAL '24 hours') AS day
                    """
                )
                result["responses_last_hour"] = row["hour"]
                result["responses_last_24h"] = row["day"]
        except Exception as e:
            problems.append(f"pg: {e}")

        # 2. Letta reachable + agents count
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{bridge.cfg.letta_url}/v1/agents/",
                    headers={"Authorization": f"Bearer {bridge.cfg.letta_password}"},
                )
                if r.status_code == 200:
                    result["letta_reachable"] = True
                    agents = r.json()
                    if isinstance(agents, list):
                        result["agents_count"] = len(agents)
                else:
                    problems.append(f"letta: HTTP {r.status_code}")
        except Exception as e:
            problems.append(f"letta: {e}")

        # 3. Determine status
        if not result["pg_reachable"] or not result["letta_reachable"]:
            result["status"] = "fail"
        elif problems:
            result["status"] = "degraded"
            result["problems"] = problems

        # 4. HTTP code: 200 if ok, 503 otherwise
        http_code = 200 if result["status"] == "ok" else 503
        return aiohttp_web.json_response(result, status=http_code)

    async def _handle_file_upload(self, request) -> "aiohttp_web.Response":
        """
        v1.5.0: загрузить файл в Matrix-комнату.

        POST /internal/files/upload
        Body: {
            "file_path": "/tmp/leo_test/file.docx",
            "room_id": "!XXXX:server",
            "filename": "report.docx",
            "delete_after": true  # удалять файл после загрузки
        }
        Returns: {"event_id": "$...", "size": 12345}
        """
        try:
            body = await request.json()
            file_path = body.get("file_path")
            room_id = body.get("room_id")
            filename = body.get("filename") or "file"
            delete_after = bool(body.get("delete_after", True))

            if not file_path or not room_id:
                return aiohttp_web.json_response(
                    {"error": "file_path and room_id required"}, status=400,
                )

            from pathlib import Path
            fp = Path(file_path)
            if not fp.exists():
                return aiohttp_web.json_response(
                    {"error": f"file not found: {file_path}"}, status=404,
                )

            file_size = fp.stat().st_size
            mime_type = self._guess_mime(filename)

            # Найдём комнату
            room = self.client.rooms.get(room_id)
            if room is None:
                return aiohttp_web.json_response(
                    {"error": f"room not joined: {room_id}"}, status=404,
                )

            # Загружаем в Synapse media (с E2EE если комната зашифрована)
            encrypt = bool(room.encrypted)
            with open(fp, "rb") as f:
                resp, decryption_keys = await self.client.upload(
                    lambda *_: f,
                    content_type=mime_type,
                    filename=filename,
                    encrypt=encrypt,
                    filesize=file_size,
                )

            if not hasattr(resp, "content_uri"):
                self.log.error("upload failed: %s", resp)
                return aiohttp_web.json_response(
                    {"error": f"upload failed: {resp}"}, status=502,
                )

            content_uri = resp.content_uri
            self.log.info(
                "[%s] file uploaded: %s -> %s (%d bytes, encrypt=%s)",
                room_id[:12], filename, content_uri, file_size, encrypt,
            )

            # Формируем m.room.message с msgtype=m.file
            msg_content = {
                "msgtype": "m.file",
                "body": filename,
                "info": {"size": file_size, "mimetype": mime_type},
            }
            if encrypt and decryption_keys:
                msg_content["file"] = {
                    "url": content_uri,
                    **decryption_keys,
                }
            else:
                msg_content["url"] = content_uri

            send_resp = await self.client.room_send(
                room_id,
                message_type="m.room.message",
                content=msg_content,
                ignore_unverified_devices=True,
            )

            event_id = getattr(send_resp, "event_id", None)
            self.log.info(
                "[%s] file message sent: event_id=%s",
                room_id[:12], event_id,
            )

            if delete_after:
                try:
                    fp.unlink()
                except Exception as e:
                    self.log.warning("cleanup tmp file failed: %s", e)

            return aiohttp_web.json_response({
                "ok": True,
                "event_id": event_id,
                "content_uri": content_uri,
                "size": file_size,
                "encrypted": encrypt,
            })
        except Exception as e:
            self.log.exception("_handle_file_upload failed: %s", e)
            return aiohttp_web.json_response(
                {"error": str(e)}, status=500,
            )

    @staticmethod
    def _guess_mime(filename: str) -> str:
        """Простой mime-mapping по расширению."""
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return {
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pdf":  "application/pdf",
            "html": "text/html",
            "md":   "text/markdown",
            "txt":  "text/plain",
        }.get(ext, "application/octet-stream")


    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if event.sender == self.cfg.user_id:
            return
        if self.startup_token is None:
            return
        if not self._is_allowed_user(event.sender):
            self.log.warning("Message from disallowed user ignored: %s", event.sender)
            await self.client.room_leave(room.room_id)
            return
        if not self._is_addressed_to_bot(event.body, room):
            return

        if event.body.startswith("👋 Привет"):
            return

        clean_text = self._strip_mention(event.body)
        self.log.info(
            "[%s] %s: %s", room.room_id[:12], event.sender, clean_text[:80]
        )

        # v0.8: slash-commands KB are handled locally, before Letta
        if await kb_handler.try_handle_slash(self, room, event, clean_text):
            return
        # v0.9: feedback slash-commands (/feedback, /leo_dashboard)
        if await feedback_handler.try_handle_slash(self, room, event, clean_text):
            return
        # v1.2.0: leo slash-commands (/leo_help, /leo_reset)
        if await leo_handler.try_handle_slash(self, room, event, clean_text):
            return

        thread_root = self._get_thread_root(event)

        await self.client.room_typing(room.room_id, typing_state=True, timeout=30000)

        # Замер времени для аудита
        t_start = time.monotonic()
        agent_id: str | None = None
        reply_text: str = ""
        error_text: str | None = None

        try:
            agent_id = await self._ensure_agent(
                room.room_id, room.display_name or room.room_id,
                matrix_user_id=event.sender,
            )
            # v1.4.0b: проверить контекст и сделать auto-compact если ≥60k
            agent_id = await self._maybe_auto_compact(
                agent_id, room.room_id,
                room.display_name or room.room_id,
                event.sender,
            )
            # v1.3.0: записываем user→room маппинг для ReminderWatcher
            try:
                if self.audit._pool:
                    async with self.audit._pool.acquire() as _pg:
                        await _pg.execute(
                            """
                            INSERT INTO ai.user_rooms (matrix_user_id, matrix_room_id, last_seen_at)
                            VALUES ($1, $2, NOW())
                            ON CONFLICT (matrix_user_id, matrix_room_id) DO UPDATE
                                SET last_seen_at = NOW()
                            """,
                            event.sender, room.room_id,
                        )
            except Exception as _e:
                self.log.debug("user_rooms upsert failed: %r", _e)
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            now_utc = now.isoformat()

            # Читаем TZ отправителя из его Matrix-профиля (MSC4175)
            user_tz = await self._get_user_tz(event.sender)

            # v1.0.4: вычисляем today_local + день недели для пользователя.
            # v1.0.5: дополнительно — карта 14 дней вперёд для надёжности.
            # Это убирает «датный баг» — Leo больше не считает дни недели сам.
            today_local = ""
            upcoming_days = ""
            try:
                if user_tz:
                    from zoneinfo import ZoneInfo
                    local_now = now.astimezone(ZoneInfo(user_tz))
                else:
                    local_now = now
                weekday_en = local_now.strftime("%A")
                weekday_ru_map = {
                    "Monday": "понедельник", "Tuesday": "вторник",
                    "Wednesday": "среда", "Thursday": "четверг",
                    "Friday": "пятница", "Saturday": "суббота",
                    "Sunday": "воскресенье",
                }
                weekday_ru = weekday_ru_map.get(weekday_en, weekday_en)
                today_local = f"{local_now.strftime('%Y-%m-%d')} {weekday_en} ({weekday_ru})"
                # v1.0.5: карта 14 дней вперёд
                from datetime import timedelta as _td
                days_list = []
                for i in range(14):
                    d = local_now + _td(days=i)
                    short_wd = d.strftime("%a")  # Mon, Tue, Wed...
                    days_list.append(f"{d.strftime('%Y-%m-%d')} {short_wd}")
                upcoming_days = ", ".join(days_list)
            except Exception:
                pass

            user_msg = (
                f"[now_utc={now_utc}] "
                f"[user_tz={user_tz or ''}] "
                f"[today_local={today_local}] "
                f"[upcoming_days={upcoming_days}] "
                f"[matrix_room_id={room.room_id}] "
                f"[from={event.sender}] {clean_text}"
            )
            reply_text = await self.letta.send_message(agent_id, user_msg)
        except LettaError as e:
            self.log.error("Letta error: %s", e)
            reply_text = "⚠️ Не получилось получить ответ от LLM. Попробуйте ещё раз."
            error_text = f"LettaError: {e}"
        except Exception as e:
            self.log.exception("Unexpected error processing message")
            reply_text = f"⚠️ Внутренняя ошибка: {type(e).__name__}"
            error_text = f"{type(e).__name__}: {e}"
        finally:
            await self.client.room_typing(room.room_id, typing_state=False)

        latency_ms = int((time.monotonic() - t_start) * 1000)

        # Записываем в audit (не блокирует ответ)
        await self.audit.log(
            room_id=room.room_id,
            user_id=event.sender,
            agent_id=agent_id,
            user_message=clean_text,
            assistant_answer=reply_text if not error_text else None,
            model_name=self.cfg.letta_model,
            latency_ms=latency_ms,
            error_text=error_text,
        )

        # Ответ в тред (новый или продолжение существующего)
        # Конвертируем markdown → HTML для красивой подсветки кода, таблиц,
        # жирного текста и кликабельных ссылок в Element и других клиентах
        # v0.9: сохраняем оригинал до postprocess для feedback.postprocess_hit
        _reply_before_pp = reply_text
        reply_text = self._postprocess_reply(reply_text)
        html_body = _MD.render(reply_text)
        content = {
            "msgtype": "m.text",
            "body": reply_text,                          # plain-text fallback
            "format": "org.matrix.custom.html",
            "formatted_body": html_body,                 # отрендеренный HTML
        }
        # v1.3.1: тред только в групповых комнатах (>2 участников)
        # ИЛИ если пользователь сам пишет в существующем треде
        is_dm = len(room.users) <= 2
        user_in_thread = (event.source.get("content", {})
                          .get("m.relates_to", {})
                          .get("rel_type") == "m.thread")
        if not is_dm or user_in_thread:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_root,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": event.event_id},
            }

        # v0.9: захватываем event_id ответа Leo для feedback
        resp = await self.client.room_send(
            room.room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True,
        )
        leo_event_id = getattr(resp, "event_id", None)

        # v0.9: feedback logging (никогда не блокирует основной flow)
        if self.feedback is not None and leo_event_id:
            try:
                meta = getattr(self.letta, "last_call_meta", {}) or {}
                rec = ResponseRecord(
                    matrix_user_id=event.sender,
                    room_id=room.room_id,
                    user_event_id=event.event_id,
                    leo_event_id=leo_event_id,
                    user_message=clean_text,
                    leo_reply=reply_text,
                    agent_id=agent_id,
                    tools_called=meta.get("tools_called", []) or [],
                    steps_count=meta.get("steps_count"),
                    context_tokens=meta.get("context_tokens"),
                    response_time_ms=latency_ms,
                    postprocess_hit=(reply_text != _reply_before_pp),
                    is_slash_command=False,
                )
                await self.feedback.log_response(rec)
            except Exception as e:
                self.log.warning("feedback.log_response failed: %s", e)


    async def _on_reaction(self, room, event) -> None:
        """v0.9: обработчик m.reaction events для feedback panel.
        nio передаёт реакции как ReactionEvent с m.relates_to в source.content.
        """
        try:
            # Игнорируем реакции от самого бота
            if event.sender == self.cfg.user_id:
                return
            if self.feedback is None:
                return
            content = event.source.get("content", {}) or {}
            relates = content.get("m.relates_to") or {}
            target_event_id = relates.get("event_id")
            reaction_key = relates.get("key")
            if not target_event_id or not reaction_key:
                return
            # log_reaction сам проверит что target — это leo_event_id
            inserted = await self.feedback.log_reaction(
                leo_event_id=target_event_id,
                matrix_user_id=event.sender,
                room_id=room.room_id,
                reaction=reaction_key,
                reaction_event_id=event.event_id,
            )
            if inserted:
                self.log.info(
                    "[%s] reaction %s by %s on %s",
                    room.room_id[:12], reaction_key, event.sender,
                    target_event_id[:20],
                )
        except Exception as e:
            self.log.warning("_on_reaction failed: %s", e)

    async def _on_redaction(self, room, event) -> None:
        """v0.9: обработчик m.redaction для удаления реакций.
        Когда пользователь убирает свою реакцию в Element — приходит
        RedactionEvent с .redacts == ID удалённого m.reaction event.
        """
        try:
            if self.feedback is None:
                return
            redacts_id = getattr(event, "redacts", None)
            if not redacts_id:
                return
            removed = await self.feedback.remove_reaction(redacts_id)
            if removed:
                self.log.info(
                    "[%s] reaction removed (event %s)",
                    room.room_id[:12], redacts_id[:20],
                )
        except Exception as e:
            self.log.warning("_on_redaction failed: %s", e)

    async def _on_file_message(self, room, event) -> None:
        """v0.8: handler for file uploads to personal KB."""
        await kb_handler.handle_file_message(self, room, event)

    @staticmethod
    def _postprocess_reply(text: str) -> str:
        """
        v0.8.5: умный postprocess. Ловит галлюцинации Leo, но игнорирует
        опровержения галлюцинаций (когда Leo сам говорит "не нужно kb_ingest").
        """
        lower = text.lower()

        # Red-flags для CLI-галлюцинаций — только КОНКРЕТНЫЕ конструкции,
        # не одиночные слова
        cli_flags = [
            "python kb_",                 # команда
            "загрузка через cli",         # утверждение
            "через cli на сервер",        # утверждение
            "по cli на сервер",
            "уточни команду у администратор",
            "уточни у администратор",
            "точную команду уточни",
            "загружаешь свои документы (pdf",
            "грузи через cli",
            "загрузи через cli",
            "kb_ingest.py --user",        # конкретная команда
            "kb_ingest --user",
        ]
        # Tavily-галлюцинации
        tavily_flags = [
            "tavily api key не настроен",
            "tavily api не настроен",
            "ключ tavily не настроен",
            "интернет-поиск сейчас не работает",
            "веб-поиск сейчас не работает",
            "интернет-поиск не работает",
            "поиск не настроен",
        ]

        cli_hit = any(f in lower for f in cli_flags)
        tavily_hit = any(f in lower for f in tavily_flags)

        # Negation-context: если рядом с red-flag есть отрицание/опровержение,
        # это опровержение галлюцинации, не сама галлюцинация
        if cli_hit:
            negation_markers = [
                "не нужен", "не нужно", "не упоминай",
                "устарел", "устаревш", "больше нет",
                "не используй", "не существует",
                "не через cli", "не нужен kb_ingest",
                "не нужно kb_ingest",
            ]
            if any(neg in lower for neg in negation_markers):
                cli_hit = False  # это опровержение

        if not cli_hit and not tavily_hit:
            return text

        import logging
        log = logging.getLogger("bridge")
        log.warning(
            "POSTPROCESS: replacing reply (cli=%s, tavily=%s). Original: %s",
            cli_hit, tavily_hit, text[:500],
        )

        if cli_hit and tavily_hit:
            return (
                "Извини, я запутался. Уточняю:\n\n"
                "📁 **Загрузка файлов в личную KB:** просто прикрепи файл в этот чат, "
                "я обработаю. Поддерживаю PDF, DOCX, MD, TXT, XLSX, CSV, JSON. /kb_help — справка.\n\n"
                "🌐 **Интернет-поиск:** работает. Спроси меня что нужно найти."
            )
        if cli_hit:
            return (
                "Загрузка файлов в твою личную KB — просто **прикрепи файл прямо в этот чат**, "
                "я его обработаю и запомню. Поддерживаемые форматы: PDF, DOCX, MD, TXT, XLSX, CSV, JSON, до 10 MB.\n\n"
                "Полная справка по KB — команда `/kb_help`."
            )
        # tavily_hit
        return (
            "Интернет-поиск работает. Скажи что именно нужно найти — "
            "новости, курсы, погоду, факты — и я поищу."
        )

    @staticmethod
    def _get_thread_root(event: RoomMessageText) -> str:
        """
        Если сообщение в треде → вернуть root этого треда.
        Если в основной ленте → вернуть собственный event_id (новый тред).
        """
        relates = event.source.get("content", {}).get("m.relates_to", {})
        if relates.get("rel_type") == "m.thread":
            return relates.get("event_id") or event.event_id
        return event.event_id

    async def _resolve_human_block(self, matrix_user_id: str) -> str | None:
        """
        v1.4.0b: вернуть human_block_id для пользователя.
        Если блока нет в ai.user_memory — создаёт новый через Letta API,
        записывает в БД, возвращает его id.
        Используется LettaClient.create_agent для shared memory между агентами.
        """
        if not matrix_user_id or not self.audit._pool:
            return None
        try:
            async with self.audit._pool.acquire() as pg:
                # Ищем существующий блок
                existing = await pg.fetchval(
                    "SELECT human_block_id FROM ai.user_memory WHERE matrix_user_id = $1",
                    matrix_user_id,
                )
                if existing:
                    self.log.debug("Reusing human block %s for %s", existing, matrix_user_id)
                    return existing

                # Создаём новый блок через Letta API
                # v1.4.0b: пустой блок чтобы LLM не путал шаблон с фактами
                initial_value = ""
                r = await self.letta.client.post(
                    "/v1/blocks/",
                    json={"label": "human", "value": initial_value, "limit": 2000},
                )
                if r.status_code >= 400:
                    self.log.warning(
                        "create human block failed for %s: %s %s",
                        matrix_user_id, r.status_code, r.text[:200],
                    )
                    return None
                block_id = r.json()["id"]

                # Записываем в БД
                await pg.execute(
                    "INSERT INTO ai.user_memory (matrix_user_id, human_block_id) "
                    "VALUES ($1, $2) "
                    "ON CONFLICT (matrix_user_id) DO UPDATE "
                    "SET human_block_id = EXCLUDED.human_block_id, updated_at = NOW()",
                    matrix_user_id, block_id,
                )
                self.log.info("Created human block %s for %s", block_id, matrix_user_id)
                return block_id
        except Exception as e:
            self.log.warning("_resolve_human_block failed for %s: %s", matrix_user_id, e)
            return None

    async def _compact_agent(
        self,
        room_id: str,
        room_name: str,
        matrix_user_id: str | None = None,
        reason: str = "auto",
    ) -> str | None:
        """
        v1.4.0b: пересоздать агента — удалить старый + создать новый.
        Используется как для /leo_reset (manual), так и для auto-compact (60k+).

        Сохраняет: shared human-block (через resolver), persona создаётся новый.
        Теряет: message buffer (исторический диалог).

        Возвращает новый agent_id или None при ошибке.
        """
        try:
            old_agent_id, _ = await self.rooms.get_state(room_id)
            if not old_agent_id:
                self.log.warning("[%s] _compact_agent: no agent found", room_id[:12])
                return None

            # 1. Удалить старого
            try:
                r = await self.letta.client.delete(f"/v1/agents/{old_agent_id}")
                self.log.info(
                    "[%s] _compact_agent (%s): deleted %s -> %s",
                    room_id[:12], reason, old_agent_id, r.status_code,
                )
            except Exception as e:
                self.log.warning(
                    "[%s] _compact_agent: delete failed (continuing): %s",
                    room_id[:12], e,
                )

            # 2. Создать нового с актуальным SYSTEM_PROMPT
            short = room_id[1:9].replace(":", "_")
            safe = "".join(
                c if c.isalnum() else "_" for c in (room_name or room_id)
            )[:30] or "room"
            suffix = "_r" if reason == "manual" else "_c"  # _c = auto-compact
            new_name = f"leo_{short}_{safe}{suffix}"

            new_agent_id = await self.letta.create_agent(
                name=new_name,
                system_prompt=SYSTEM_PROMPT,
                matrix_user_id=matrix_user_id,
            )
            self.log.info(
                "[%s] _compact_agent (%s): created %s",
                room_id[:12], reason, new_agent_id,
            )

            # 3. Обновить mapping
            await self.rooms.set_agent(room_id, new_agent_id)
            return new_agent_id
        except Exception as e:
            self.log.exception(
                "[%s] _compact_agent (%s) failed: %s",
                room_id[:12], reason, e,
            )
            return None

    # Защита от частых auto-compact: храним last_compact_at в памяти процесса
    _last_compact_at: dict[str, float] = {}
    _COMPACT_THRESHOLD = 60000          # триггер при ≥60k токенов
    _COMPACT_COOLDOWN_SEC = 60          # не чаще раза в минуту на комнату

    async def _maybe_auto_compact(
        self,
        agent_id: str,
        room_id: str,
        room_name: str,
        matrix_user_id: str,
    ) -> str:
        """
        v1.4.0b: auto-compact при ≥60k токенов с тихим уведомлением.
        v1.4.2: orphan-detect — если у агента не привязан shared block, делаем
        silent compact (без уведомления). Реальный кейс: legacy-агенты созданы
        до v1.4.0 и имеют per-agent шаблонный human-блок.

        Возвращает agent_id (старый или новый если был compact).
        """
        # Cooldown — не чаще раза в минуту на одну комнату
        now = time.monotonic()
        last = self._last_compact_at.get(room_id, 0.0)
        if now - last < self._COMPACT_COOLDOWN_SEC:
            return agent_id

        try:
            # 1. Получаем данные агента и shared block_id ожидаемый для юзера
            r_agent = await self.letta.client.get(f"/v1/agents/{agent_id}")
            if r_agent.status_code >= 400:
                return agent_id
            agent_data = r_agent.json()

            # human-блок этого агента
            agent_human_block = None
            for b in (agent_data.get("memory") or {}).get("blocks") or []:
                if b.get("label") == "human":
                    agent_human_block = b
                    break

            shared_block_id = await self._resolve_human_block(matrix_user_id)
            agent_human_id = agent_human_block.get("id") if agent_human_block else None

            # 2. v1.4.2: orphan detection — agent не привязан к shared
            is_orphan = (
                shared_block_id
                and agent_human_id
                and shared_block_id != agent_human_id
            )

            if is_orphan:
                # Silent compact — пересоздаём агента с правильным shared блоком
                self.log.info(
                    "[%s] orphan detected: agent_human=%s shared=%s — silent compact",
                    room_id[:12], agent_human_id, shared_block_id,
                )

                # Сливаем ценные факты из per-agent блока в shared (если не шаблон)
                try:
                    old_value = (agent_human_block.get("value") or "").strip()
                    is_template = (
                        not old_value
                        or old_value.startswith("Здесь записываю важные факты")
                    )
                    if not is_template:
                        # Читаем shared
                        rb = await self.letta.client.get(f"/v1/blocks/{shared_block_id}")
                        if rb.status_code < 400:
                            curr = rb.json().get("value", "")
                            if old_value not in curr:
                                merged = (curr + "\n" + old_value).strip()
                                await self.letta.client.patch(
                                    f"/v1/blocks/{shared_block_id}",
                                    json={"value": merged},
                                )
                                self.log.info(
                                    "[%s] orphan: merged %d chars into shared block",
                                    room_id[:12], len(old_value),
                                )
                except Exception as e:
                    self.log.warning(
                        "[%s] orphan: merge facts failed: %s", room_id[:12], e,
                    )

                self._last_compact_at[room_id] = now
                new_agent_id = await self._compact_agent(
                    room_id, room_name, matrix_user_id, reason="orphan",
                )
                return new_agent_id or agent_id

            # 3. Обычная проверка контекста на 60k
            r_ctx = await self.letta.client.get(f"/v1/agents/{agent_id}/context")
            if r_ctx.status_code >= 400:
                return agent_id
            ctx = r_ctx.json()
            current = ctx.get("context_window_size_current", 0)
            if current < self._COMPACT_THRESHOLD:
                return agent_id

            self.log.info(
                "[%s] auto-compact triggered: ctx=%d msgs=%d",
                room_id[:12], current, ctx.get("num_messages", 0),
            )
            self._last_compact_at[room_id] = now

            new_agent_id = await self._compact_agent(
                room_id, room_name, matrix_user_id, reason="auto",
            )
            if not new_agent_id:
                return agent_id

            # Тихое сообщение пользователю
            try:
                notice = (
                    "🧠 Освежил память для скорости — "
                    "контекст разговора сброшен, но всё важное о тебе помню."
                )
                from markdown_it import MarkdownIt
                md = MarkdownIt("commonmark", {"html": False, "linkify": True})
                content = {
                    "msgtype": "m.text",
                    "body": notice,
                    "format": "org.matrix.custom.html",
                    "formatted_body": md.render(notice),
                }
                await self.client.room_send(
                    room_id,
                    message_type="m.room.message",
                    content=content,
                    ignore_unverified_devices=True,
                )
            except Exception as e:
                self.log.warning("auto-compact notice failed: %s", e)

            return new_agent_id
        except Exception as e:
            self.log.warning("[%s] auto-compact check failed: %s", room_id[:12], e)
            return agent_id

    async def _ensure_agent(
        self,
        room_id: str,
        room_name: str,
        matrix_user_id: str | None = None,
    ) -> str:
        """Найти или создать Letta-агента для комнаты.
        v1.4.0b: matrix_user_id пробрасывается в create_agent для shared human block.
        """
        agent_id, _ = await self.rooms.get_state(room_id)
        if agent_id is not None:
            return agent_id

        short = room_id[1:9].replace(":", "_")
        safe = "".join(c if c.isalnum() else "_" for c in room_name)[:30] or "room"
        agent_name = f"leo_{short}_{safe}"

        agent_id = await self.letta.create_agent(
            name=agent_name,
            system_prompt=SYSTEM_PROMPT,
            matrix_user_id=matrix_user_id,
        )
        await self.rooms.set_agent(room_id, agent_id)
        return agent_id

    # ---------- запуск ----------
    async def run(self) -> None:
        self.log.info("Starting sync as %s on %s", self.cfg.user_id, self.cfg.homeserver)
        self.client.load_store()
        if self.client.should_upload_keys:
            await self.client.keys_upload()
        if self.client.should_query_keys:
            await self.client.keys_query()

        # Поднимаем reminder-watcher в той же event-loop
        cal = CalendarClient(
            self.cfg.radicale_url,
            self.cfg.radicale_user,
            self.cfg.radicale_password,
        )
        import asyncpg
        pg_pool = await asyncpg.create_pool(
            dsn=self.cfg.database_url_ai,
            min_size=1,
            max_size=2,
        )
        # v0.9: инициализируем feedback logger на том же pool
        self.feedback = FeedbackLogger(pg_pool)
        self.log.info("FeedbackLogger initialized")

        # v1.0.1: HTTP health endpoint on 127.0.0.1:9090
        from datetime import datetime, timezone as _tz
        self._started_at_iso = datetime.now(_tz.utc).astimezone().isoformat()
        await self._start_health_server(pg_pool)

        sqlite_path = str(self.cfg.data_dir / "bridge.sqlite")
        self.reminders = ReminderWatcher(
            cal=cal,
            pg_pool=pg_pool,
            client=self.client,
            bridge_sqlite_path=sqlite_path,
        )
        # Запускаем watcher параллельно с sync
        self._reminder_task = asyncio.create_task(
            self.reminders.run_forever(), name="reminders"
        )
        self.log.info("ReminderWatcher started in background")

        try:
            await self.client.sync_forever(timeout=30000, full_state=True)
        finally:
            # Корректное завершение
            if self.reminders:
                self.reminders.stop()
            if self._reminder_task:
                self._reminder_task.cancel()
                try:
                    await self._reminder_task
                except asyncio.CancelledError:
                    pass
            # v1.0.1: stop health server
            await self._stop_health_server()
            await pg_pool.close()

def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=getattr(logging, level, logging.INFO),
    )
    logging.getLogger("nio").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

async def main() -> None:
    cfg = Config()
    setup_logging(cfg.log_level)

    db_path = cfg.data_dir / "bridge.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    pg_dsn = os.environ.get("DATABASE_URL_AI")
    if not pg_dsn:
        sys.stderr.write("FATAL: DATABASE_URL_AI not set\n")
        sys.exit(1)

    async with (
        LettaClient(
            cfg.letta_url,
            cfg.letta_password,
            cfg.letta_model,
            cfg.letta_embedding,
        ) as letta,
        RoomMapping(db_path) as rooms,
        AuditLog(pg_dsn) as audit,
    ):
        bridge = Bridge(cfg, letta, rooms, audit)
        try:
            await bridge.run()
        finally:
            await bridge.client.close()

if __name__ == "__main__":
    asyncio.run(main())

