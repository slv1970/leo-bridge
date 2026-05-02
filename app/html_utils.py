"""
v1.6.0: HTML → plain text утилита.

Используется в respect_kb_sync для извлечения текста из body_html карточек
КБ Респект.Чата перед FTS-индексацией.

Зависимость: beautifulsoup4 (добавлена в requirements.txt в v1.6.0).
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup


log = logging.getLogger("html_utils")

# Теги, содержимое которых полностью пропускаем (script/style/etc)
_SKIP_TAGS = {"script", "style", "noscript", "head", "meta", "link"}

# Блочные теги — после них вставляем перенос строки для читаемости
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer",
    "table", "thead", "tbody", "ul", "ol", "blockquote",
}


def html_to_plain(html: str) -> str:
    """Преобразовать HTML в plain text.

    - Удаляет script/style и прочий мусор
    - Сохраняет видимый текст
    - Между блочными тегами вставляет переносы строк
    - Сжимает множественные пробелы и переносы

    Возвращает строку без HTML-тегов, пригодную для FTS и preview.

    Возвращает пустую строку если на вход пришёл пустой/None.
    """
    if not html:
        return ""

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        log.warning("html parse failed, falling back to regex strip: %s", e)
        return _regex_strip(html)

    # Удаляем скрипты и прочее
    for tag in soup.find_all(_SKIP_TAGS):
        tag.decompose()

    # Вставляем переносы строк между блоками
    for tag in soup.find_all(_BLOCK_TAGS):
        # Перенос строки ПОСЛЕ блочного тега
        tag.append("\n")

    text = soup.get_text(separator=" ", strip=False)

    # Сжимаем пробелы (но сохраняем переносы строк)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _regex_strip(html: str) -> str:
    """Запасной вариант: грубая очистка регуляркой если bs4 упал."""
    # удаляем скрипты/стили целиком
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>.*?</style>",  "", html, flags=re.DOTALL | re.IGNORECASE)
    # заменяем блочные теги на переносы
    html = re.sub(r"</?(p|div|br|li|tr|td|th|h[1-6])\b[^>]*>", "\n", html, flags=re.IGNORECASE)
    # удаляем оставшиеся теги
    html = re.sub(r"<[^>]+>", "", html)
    # html-сущности
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    # сжатие
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()
