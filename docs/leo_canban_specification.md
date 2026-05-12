# Создание тикетов через Leo: спецификация для реализации

**Версия:** 1.3 от 07.05.2026

---

## Что делаем

Переносим создание тикетов из модальной формы канбана в Leo. Два сценария: задача и баг-репорт.

Поток: Letta agent ведёт диалог → собирает поля → показывает сводку → подтверждение → tool вызывает endpoint в bridge → bridge резолвит matrix_user в id24101 (с кешированием на стороне Python) → bridge вызывает pl/pgsql-функцию → INSERT в `board_tasks`.

---

## Ключевые решения

### Архитектурно
- **Два разных tool** (`create_canban_task`, `create_canban_bugreport`) и **два разных endpoint** (`POST /canban/create_task`, `POST /canban/create_bugreport`). Не объединяем — каждый имеет свой docstring и своё поведение, модели легче выбирать.
- **Подтверждение перед вызовом tool — обязательно.** Agent показывает сводку, ждёт «да», только потом вызывает. Это правило живёт в SYSTEM_PROMPT, не в docstring.
- **Префикс `(AI) - `** в заголовке тикета — добавляется на стороне SQL-функции.

### Что определяется автоматически (НЕ спрашиваем у пользователя)
- **column_id** — `board_columns.sys = true` для выбранной доски. Если не найдено → ошибка `NO_SYS_COLUMN`.
- **template_id** — дефолт по сценарию для подразделения разработки: задача → 1, баг-репорт → 4. Если пользователь создаёт задачу в другое подразделение, дефолт берётся из публичных шаблонов соответствующего подразделения. Пользователь может выбрать другой шаблон через `get_canban_public_templates`.
- **owner_subdivision** — берётся из `board_tasks_template.owner_subdivision`. На тикете отдельно не хранится.
- **author** — текущий пользователь Leo через маппинг matrix_user → canban_user.
- **producer_list, performer_list, watchers_list** — `[author]`, `[]`, `[]`.

### Что выбирает пользователь через диалог
- Содержательные поля задачи/баг-репорта
- **project** — id из `board_task_work_environment` (12 значений, термин для пользователя — «проект»)
- **board_id** — из `get_canban_user_boards`
- (опционально) **template_id** — если явно хочет другой шаблон

### Терминология
- В коде / у нас в обсуждениях — `work_environment`
- В диалоге с пользователем — «проект»

---

## Tools (8 штук)

Полные параметры — в SQL-функциях и docstrings (передам отдельно). Здесь — только сигнатуры и назначение.

| Tool | Назначение | Когда вызывает agent |
|---|---|---|
| `create_canban_task(...)` | Создать задачу | После сбора полей и подтверждения |
| `create_canban_bugreport(...)` | Создать баг-репорт | После сбора полей и подтверждения |
| `get_canban_projects()` | Список проектов | Этап выбора проекта |
| `get_canban_user_boards()` | Доступные пользователю доски | После выбора проекта |
| `get_canban_public_templates()` | Публичные шаблоны | Только если пользователь хочет другой шаблон |
| `get_canban_type_errors()` | Справочник типов ошибок | В баг-репорте |
| `get_canban_period_errors()` | Справочник частоты | В баг-репорте |
| `get_canban_version_browsers()` | Справочник браузеров | В баг-репорте |

**Замечание по справочникам.** Все справочники возвращают пары `(id, title)`. Поля баг-репорта `type_error`, `period_error`, `version_browser` в `board_tasks` хранятся как `integer` (idkart). Соответственно agent показывает пользователю title, а в tool передаёт id.

---

## Endpoints в bridge

`POST /canban/create_task` и `POST /canban/create_bugreport`:

**Headers:** `X-Internal-Token`
**Body:**
```json
{
  "matrix_user": "@elena:mtx.respectrb.ru",
  "task_data": { ...поля задачи/баг-репорта... }
}
```

**Response success:**
```json
{"success": true, "idkart": 4521, "url": "https://canban.respectrb.ru/click_task/?short=4521"}
```

**Response error:**
```json
{"success": false, "error_code": "NO_SYS_COLUMN", "error_message": "..."}
```

**Коды ошибок:**

Ниже — коды, которые на старте реализованы в pl/pgsql-функциях. Дополнительные коды (`BOARD_NOT_ACCESSIBLE`, `INVALID_PROJECT`, `INVALID_TEMPLATE`, `VALIDATION_ERROR`, `INVALID_TYPE_ERROR`, `INVALID_PERIOD_ERROR`, `INVALID_VERSION_BROWSER`) реализуются на стороне bridge через валидацию входных данных до вызова функции.

| `error_code` | HTTP | Когда возникает | Источник |
|---|---|---|---|
| `BOARD_NOT_FOUND` | 404 | Доска не существует или удалена | pl/pgsql |
| `NO_SYS_COLUMN` | 409 | На доске нет колонки с `sys = true` | pl/pgsql |
| `USER_NOT_MAPPED` | 404 | Пользователь Leo не связан с сотрудником Эльбазы | pl/pgsql |
| `BOARD_NOT_ACCESSIBLE` | 403 | Пользователь не имеет доступа к доске | bridge |
| `VALIDATION_ERROR` | 400 | Не пройдена валидация обязательных полей | bridge |
| `DB_ERROR` | 500 | Внутренняя ошибка БД | bridge |

**Логика endpoint:**
1. Проверка токена.
2. Резолв `matrix_user` → `id24101` через `ai_resolve_matrix_user` (с кешированием на стороне Python). Если null → 404 `USER_NOT_MAPPED`.
3. Извлечь из `task_data` поля `board_id` и `template_id` — они передаются в pl/pgsql отдельными аргументами, а не как часть `_data jsonb`.
4. Валидация обязательных полей.
5. Вызов pl/pgsql-функции `canban.ai_create_task_ticket(_data, _author_id24101, _board_id, _template_id, _source := 'leo')` или `canban.ai_create_bugreport_ticket(...)`.
6. Возврат idkart и URL.
7. Запись в `ai.ai_chat_log` с тегом `task_created`.

**GET endpoints для справочников.** Без тела, авторизация по `X-Internal-Token`. Для `user_boards` нужен `matrix_user` (header или query).

| Endpoint | Назначение |
|---|---|
| `GET /canban/projects` | Список рабочих сред (12 значений) |
| `GET /canban/user_boards` | Доски, доступные текущему пользователю |
| `GET /canban/public_templates` | Публичные шаблоны (`is_public = true`) |
| `GET /canban/type_errors` | Справочник типов ошибок (4 значения) |
| `GET /canban/period_errors` | Справочник частоты появления ошибки (3 значения) |
| `GET /canban/version_browsers` | Справочник браузеров (7 значений) |

---

## pl/pgsql-функции

Сигнатуры — для понимания, что вызывать:

```sql
canban.ai_resolve_matrix_user(_matrix_user text) returns integer
       -- резолв matrix_user → id24101, вызывается bridge один раз, кешируется

canban.ai_create_task_ticket(_data jsonb, _author_id24101 int, _board_id int,
                              _template_id int, _source text) returns integer

canban.ai_create_bugreport_ticket(_data jsonb, _author_id24101 int, _board_id int,
                                   _template_id int, _source text) returns integer

canban.ai_get_projects() returns table(id int, title text)
canban.ai_get_user_boards(_elbaza_user int)
       returns table(id int, title text, group_id int, group_title text, is_public boolean)
canban.ai_get_public_templates()
       returns table(id int, title text, subdivision_id int, subdivision_name text)
canban.ai_get_type_errors() returns table(id int, title text)
canban.ai_get_period_errors() returns table(id int, title text)
canban.ai_get_version_browsers() returns table(id int, title text)
```

Все функции фильтруют по `dttmcl IS NULL`. Функции для создания тикетов делают `RAISE EXCEPTION` с понятным текстом — bridge мапит в `error_code`.

---

## Открытые вопросы

1. **Получение `matrix_user` в Letta sandbox** — env / аргумент / context? Какой паттерн в существующих tools?

---

## Что дальше

- pl/pgsql-функции созданы в схеме `canban`.
- Тексты docstring tools и фрагмент SYSTEM_PROMPT передаются вместе с этим документом отдельными файлами.
- Тесты (секция LEO-CANBAN в `leo_test_map.md`) подготовлю параллельно с реализацией Python-кода tools.

Вопросы и уточнения — в личке.
