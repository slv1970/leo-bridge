"""
Тонкий HTTP-клиент к Letta API.

Используем только то, что нам нужно для bridge:
- create_agent(name, system) → agent_id
- send_message(agent_id, text) → reply_text
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


log = logging.getLogger("letta_client")


class LettaError(Exception):
    """Ошибка от Letta API или таймаут."""


class LettaClient:
    def __init__(
        self,
        base_url: str,
        password: str,
        model_handle: str,
        embedding_handle: str,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.model_handle = model_handle
        self.embedding_handle = embedding_handle
        # v0.9: метрики последнего вызова send_message (читает bridge для feedback)
        self.last_call_meta: dict = {}
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        # v1.4.0b: callback для получения human_block_id по matrix_user_id
        # Тип: Optional[Callable[[str], Awaitable[Optional[str]]]]
        # Bridge устанавливает это после создания LettaClient
        self._human_block_resolver = None

    async def __aenter__(self) -> "LettaClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.password}"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise LettaError("LettaClient must be used inside `async with`")
        return self._client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        reraise=True,
    )
    async def create_agent(self, name: str, system_prompt: str, matrix_user_id: str | None = None) -> str:
        """Создать нового Letta-агента и вернуть его id.

        К новым агентам автоматически прицепляются все calendar_* tools,
        которые есть в Letta на момент создания.
        """
        # Найдём все наши tools заранее
        tool_ids = await self._get_calendar_tool_ids()

        # v1.4.0d: persona создаётся inline, human берётся как block_ids (truly shared)
        # Letta v1_agent корректно работает с shared блоками если они переданы
        # через block_ids в payload create_agent
        memory_blocks = [
            {
                "label": "persona",
                "value": (
                    "Я Leo — корпоративный AI-ассистент Respect.Chat. "
                    "Стиль: коротко, по делу, без воды. "
                    "Помогаю с календарём, базой знаний, поиском в интернете. "
                    "Работаю в Matrix через E2EE."
                ),
                "limit": 2000,
            },
        ]

        # Получаем shared human-block через resolver
        human_block_id = None
        if matrix_user_id and self._human_block_resolver is not None:
            try:
                human_block_id = await self._human_block_resolver(matrix_user_id)
            except Exception as e:
                log.warning("human block resolver failed for %s: %s", matrix_user_id, e)

        payload = {
            "name": name,
            "model": self.model_handle,
            "embedding": self.embedding_handle,
            "system": system_prompt,
            "include_base_tools": True,
            "tool_ids": tool_ids,
            "memory_blocks": memory_blocks,
        }
        # Передаём human как block_ids (shared между агентами одного пользователя)
        if human_block_id:
            payload["block_ids"] = [human_block_id]
        r = await self.client.post("/v1/agents/", json=payload)
        if r.status_code >= 400:
            log.error("create_agent failed %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()
        data = r.json()
        agent_id = data["id"]
        log.info(
            "Created agent %s (name=%s) with %d custom tools",
            agent_id, name, len(tool_ids),
        )

        # Дополнительно: явный attach для гарантии (на случай, если tool_ids
        # в payload не сработал — у разных версий Letta API разное поведение)
        for tool_id in tool_ids:
            try:
                rr = await self.client.patch(
                    f"/v1/agents/{agent_id}/tools/attach/{tool_id}"
                )
                if rr.status_code >= 400:
                    log.debug(
                        "attach tool %s to %s: %s (already attached?)",
                        tool_id, agent_id, rr.status_code,
                    )
            except Exception as e:
                log.debug("attach tool error: %s", e)

        # v1.4.0d: human передан через block_ids — truly shared
        if matrix_user_id and human_block_id:
            log.info(
                "Created agent %s with shared human block %s (user=%s)",
                agent_id, human_block_id, matrix_user_id,
            )

        return agent_id

    # Список наших custom tools, которые автоматически
    # прицепляются ко всем новым агентам.
    # При добавлении новых tools — расширяй этот фильтр
    # ИЛИ функцию _is_our_tool ниже.
    _OUR_TOOL_NAMES = {"internet_search", "leo_create_file"}

    @classmethod
    def _is_our_tool(cls, tool_name: str) -> bool:
        """v0.8.10 / v1.6.0: tool считается «нашим» если:
        - имя начинается с calendar_  (календарные tools)
        - имя начинается с kb_  (KB tools: search_corporate/personal, list/info/delete_personal)
        - имя начинается с respect_  (v1.6.0: КБ Респект.Чата)
        - явно перечислено в _OUR_TOOL_NAMES (internet_search и др.)
        """
        return (
            tool_name.startswith("calendar_")
            or tool_name.startswith("kb_")
            or tool_name.startswith("respect_")
            or tool_name in cls._OUR_TOOL_NAMES
        )

    async def _get_calendar_tool_ids(self) -> list[str]:
        """Найти все tool_ids наших custom tools (calendar_* + internet_search и пр.)."""
        try:
            r = await self.client.get("/v1/tools/")
            if r.status_code >= 400:
                log.warning("Failed to list tools: %s", r.status_code)
                return []
            tools = r.json()
            return [t["id"] for t in tools if self._is_our_tool(t["name"])]
        except Exception as e:
            log.error("Error fetching tool ids: %s", e)
            return []

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(httpx.RequestError),
        reraise=True,
    )
    async def send_message(self, agent_id: str, text: str) -> str:
        """
        Отправить сообщение агенту, дождаться ответа.

        Letta возвращает массив 'messages' с разными типами:
        reasoning_message, tool_call_message, assistant_message и т.д.
        v0.8.9: собираем ВСЕ assistant_message (Letta может вернуть
        несколько при multi-step ответах с tool-calls между ними).
        """
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": text,
                }
            ],
        }
        r = await self.client.post(
            f"/v1/agents/{agent_id}/messages",
            json=payload,
        )
        if r.status_code >= 400:
            log.error(
                "send_message failed agent=%s status=%s body=%s",
                agent_id, r.status_code, r.text[:500],
            )
            raise LettaError(f"Letta returned {r.status_code}: {r.text[:200]}")
        data = r.json()
        # v0.8.9: собираем ВСЕ assistant_message
        # v0.9: дополнительно собираем метрики (tools/steps/context) для feedback
        all_parts: list[str] = []
        tools_called: list[str] = []
        steps_count = 0
        context_tokens: int | None = None
        for msg in data.get("messages", []):
            mtype = msg.get("message_type")
            steps_count += 1
            # v0.9: собираем имена вызванных tools
            if mtype == "tool_call_message":
                tc = msg.get("tool_call") or {}
                tname = tc.get("name") or msg.get("name")
                if tname and tname not in tools_called:
                    tools_called.append(tname)
            if mtype != "assistant_message":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    all_parts.append(content)
            elif isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict) and "text" in c:
                        parts.append(c["text"])
                    elif isinstance(c, str):
                        parts.append(c)
                if parts:
                    joined = "\n".join(parts).strip()
                    if joined:
                        all_parts.append(joined)
        # v0.9: context_tokens — берём из usage если Letta их даёт
        usage = data.get("usage") or {}
        if isinstance(usage, dict):
            context_tokens = (
                usage.get("prompt_tokens")
                or usage.get("context_tokens")
                or usage.get("input_tokens")
            )
        # v0.9: сохраняем метрики для bridge
        self.last_call_meta = {
            "tools_called": tools_called,
            "steps_count": steps_count,
            "context_tokens": context_tokens,
        }
        if all_parts:
            return "\n\n".join(all_parts)

        log.warning("No assistant_message in response. Full response: %s",
                    str(data)[:500])
        return "_(агент не дал текстового ответа)_"

