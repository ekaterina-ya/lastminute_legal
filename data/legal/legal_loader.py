"""Парсер zakon.md / rukovodstva.md для обогащения Stage 2.

Используется обоими экспериментальными вариантами бота:
- вариант A (инжект-целиком) — берёт whole-версии;
- вариант B (роутинг) — берёт подмножества по списку статей.

Контракт разметки:
- закон: статья начинается со строки `### Статья N.` (N = «5», «28.1» и т.п.);
- руководства: блок статьи начинается со строки `## ст. N` (подразделы ст. 5
  оформлены как `### ст. 5 ч. N` и остаются внутри блока `## ст. 5`).
"""
from __future__ import annotations

import difflib
import re
from datetime import date, datetime, timedelta, timezone

_LAW_ART_RE = re.compile(r"^### Статья (\S+?)\.\s")
_GUIDE_ART_RE = re.compile(r"^## ст\.\s*(\S+)")
_ART_NUM_RE = re.compile(r"ст\.?\s*(\d+(?:\.\d+)?)")

# Первая строка тела отложенной редакции в zakon_future.md. Формат — контракт
# со сборщиком в fas-pipeline, менять только с ним вместе.
_FUTURE_DATE_RE = re.compile(r"^Вступает в силу с (\d{1,2})\.(\d{1,2})\.(\d{4})")

# Насколько далеко вперёд смотрим. Норма, до которой ещё год, в заключении
# только шумит: столько креативы не живут.
FUTURE_HORIZON_DAYS = 180

# «Сегодня» — по Москве: даты вступления в силу московские, а контейнер бота
# живёт в UTC (проверено 30.09.2026) — иначе с 00:00 до 03:00 МСК в день
# вступления бот отвечал бы по вчерашней редакции. Перехода на летнее время в
# России нет с 2014 года, поэтому хватает фиксированного сдвига.
MSK = timezone(timedelta(hours=3))


def _today() -> date:
    return datetime.now(MSK).date()


# Руководства по понятию рекламы (классификация «реклама / не реклама»)
# в этой фиче не используются — это отдельная будущая задача.
GUIDE_EXCLUDE = {"ст. 2", "ст. 3"}


def normalize_article(raw: str) -> str | None:
    """«п. 2 ч. 3 ст. 5» / «ст. 28.1» → «ст. 5» / «ст. 28.1». None, если статьи нет."""
    m = _ART_NUM_RE.search(raw or "")
    return f"ст. {m.group(1)}" if m else None


def _article_num(key: str) -> tuple:
    """«ст. 29.2» → (29, 2): порядок статей по номеру."""
    return tuple(int(p) for p in key.split(" ", 1)[1].split("."))


def _parse_sections(text: str, header_re: re.Pattern) -> tuple[str, dict, list]:
    """Возвращает (преамбула, {ключ: текст_секции}, порядок_ключей)."""
    preamble: list[str] = []
    sections: dict[str, str] = {}
    order: list[str] = []
    cur_key: str | None = None
    cur_buf: list[str] = []
    for line in text.splitlines():
        m = header_re.match(line)
        if m:
            if cur_key is not None:
                sections[cur_key] = "\n".join(cur_buf).rstrip()
            cur_key = f"ст. {m.group(1)}"
            cur_buf = [line]
            order.append(cur_key)
        elif cur_key is None:
            preamble.append(line)
        else:
            cur_buf.append(line)
    if cur_key is not None:
        sections[cur_key] = "\n".join(cur_buf).rstrip()
    return "\n".join(preamble).strip(), sections, order


def _future_date(section: str) -> date | None:
    """Дата вступления из первой непустой строки тела секции."""
    for line in section.splitlines()[1:]:
        if not line.strip():
            continue
        m = _FUTURE_DATE_RE.match(line.strip())
        if not m:
            return None
        d, mth, y = (int(x) for x in m.groups())
        try:
            return date(y, mth, d)
        except ValueError:
            return None
    return None


def _parse_future(text: str) -> list[tuple[str, date, str]]:
    """Отложенные редакции: (ключ статьи, дата вступления, текст секции).

    Список, а не словарь: у одной статьи может быть несколько будущих редакций,
    и по ключу «ст. N» они бы затёрли друг друга. Секция без разборчивой даты
    отбрасывается — лучше промолчать, чем показать редакцию без срока.
    """
    out: list[tuple[str, date, str]] = []
    cur_key: str | None = None
    cur_buf: list[str] = []

    def flush() -> None:
        if cur_key is None:
            return
        body = "\n".join(cur_buf).rstrip()
        when = _future_date(body)
        if when is not None:
            out.append((cur_key, when, body))

    for line in text.splitlines():
        m = _LAW_ART_RE.match(line)
        if m:
            flush()
            cur_key = f"ст. {m.group(1)}"
            cur_buf = [line]
        elif cur_key is not None:
            cur_buf.append(line)
    flush()
    return out


def _changed_lines(current: str, future: str) -> list[str]:
    """Строки будущей редакции, которых нет в действующей.

    В законе одна строка — одна часть или пункт, поэтому построчная разница и
    есть перечень изменившихся норм. Целиком будущую статью не подаём: она
    повторяет действующую на 86–99% (замер на ст. 10 и ст. 27), и заключение
    подорожало бы вдвое ради пары изменившихся абзацев.
    """
    cur, fut = current.splitlines(), future.splitlines()
    out: list[str] = []
    for tag, _, _, b1, b2 in difflib.SequenceMatcher(None, cur, fut).get_opcodes():
        if tag in ("replace", "insert"):
            out.extend(line for line in fut[b1:b2] if line.strip())
    return out


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class LegalCorpus:
    """Загруженные и распарсенные закон + руководства."""

    def __init__(self, law_path: str, guidelines_path: str, future_law_path: str | None = None):
        # Отложенные редакции живут отдельным файлом: в zakon.md две редакции
        # одной статьи схлопнулись бы по ключу, да и подавать будущее вместе
        # с действующим нельзя — модель начнёт считать его нарушением.
        try:
            self.future_sections = _parse_future(_read(future_law_path)) if future_law_path else []
        except FileNotFoundError:
            self.future_sections = []
        law_text = _read(law_path)
        self.law_preamble, self.law_sections, self.law_order = _parse_sections(
            law_text, _LAW_ART_RE
        )
        guide_text = _read(guidelines_path)
        self.guide_preamble, guide_sections, guide_order = _parse_sections(
            guide_text, _GUIDE_ART_RE
        )
        # отфильтровываем понятие рекламы (ст. 2 / ст. 3)
        self.guide_sections = {
            k: v for k, v in guide_sections.items() if k not in GUIDE_EXCLUDE
        }
        self.guide_order = [k for k in guide_order if k not in GUIDE_EXCLUDE]

    # --- whole (вариант A) ---
    def law_whole(self) -> str:
        body = "\n\n".join(self.law_sections[k] for k in self.law_order)
        return f"{self.law_preamble}\n\n{body}".strip()

    def guidelines_whole(self) -> str:
        body = "\n\n".join(self.guide_sections[k] for k in self.guide_order)
        return f"{self.guide_preamble}\n\n{body}".strip()

    # --- routed (вариант B) ---
    def _in_force_section(self, key: str, now: date) -> str:
        """Текст статьи на дату: наступившая отложенная редакция важнее zakon.md.

        zakon.md собирается на дату сборки и сам в срок не обновится — пока
        корпус не пересобрали, действующую редакцию берём из zakon_future.md.
        Иначе бот молча анализировал бы по недействующему тексту.
        """
        best: tuple[date, str] | None = None
        for k, when, text in self.future_sections:
            if k == key and when <= now and (best is None or when >= best[0]):
                best = (when, text)
        if best is None:
            return self.law_sections.get(key, "")
        head, _stamp, *rest = best[1].splitlines()  # штамп «Вступает в силу с …» снимаем: уже действует
        while rest and not rest[0].strip():
            rest.pop(0)
        return "\n".join([head, *rest]).rstrip()

    def stale_editions(self, today: date | None = None) -> list[str]:
        """Статьи, чья отложенная редакция уже действует, а zakon.md ещё старый."""
        now = today or _today()
        return sorted({k for k, when, _ in self.future_sections if when <= now})

    def order_on(self, today: date | None = None) -> list[str]:
        """Статьи, действующие на дату, в порядке закона.

        Новая статья, которой ещё нет в zakon.md, появляется с даты вступления
        своей отложенной редакции — на своё место по номеру, без пересборки.
        """
        now = today or _today()
        order = list(self.law_order)
        new = sorted({k for k, when, _ in self.future_sections if when <= now and k not in self.law_sections},
                     key=_article_num)
        for key in new:
            pos = next((i for i, k in enumerate(order) if _article_num(k) > _article_num(key)), len(order))
            order.insert(pos, key)
        return order

    def menu_on(self, today: date | None = None) -> str:
        """Меню роутера на дату: «Статья N. Название» по действующим редакциям."""
        now = today or _today()
        return "\n".join(self._in_force_section(k, now).splitlines()[0].replace('### ', '')
                         for k in self.order_on(now))

    def law_for(self, articles: list[str], today: date | None = None) -> str:
        now = today or _today()
        in_force = set(self.order_on(now))
        keys = [a for a in articles if a in in_force]
        if not keys:
            return ""
        body = "\n\n".join(self._in_force_section(k, now) for k in keys)
        return f"{self.law_preamble}\n\n{body}".strip()

    def guidelines_for(self, articles: list[str]) -> str:
        keys = [a for a in articles if a in self.guide_sections]
        if not keys:
            return ""
        body = "\n\n".join(self.guide_sections[k] for k in keys)
        return f"{self.guide_preamble}\n\n{body}".strip()

    def future_law_for(self, articles: list[str], today: date | None = None) -> str:
        """Отложенные редакции по тем же статьям, что выбрал роутер.

        Отдаём заголовок, дату вступления и только изменившиеся нормы — см.
        _changed_lines. Преамбулу не добавляем: она уже уехала с law_for.
        Редакции, дата которых наступила, не отдаём: если корпус не пересобрали
        вовремя, промолчать лучше, чем сказать «вступает в силу» о том, что уже
        действует.
        """
        now = today or _today()
        limit = now + timedelta(days=FUTURE_HORIZON_DAYS)
        wanted = set(articles)
        body = []
        for key, when, text in self.future_sections:
            if key not in wanted or not now < when <= limit:
                continue
            head, stamp, *rest = text.splitlines()
            # Сравниваем с редакцией, действующей накануне вступления, а не с
            # сегодняшней: если у статьи цепочка редакций, каждая должна
            # показывать только свою правку и со своей датой.
            # У совсем новой статьи действующей редакции нет — тогда изменилось всё.
            current = "\n".join(self._in_force_section(key, when - timedelta(days=1)).splitlines()[1:])
            changed = _changed_lines(current, "\n".join(rest))
            if changed:
                body.append("\n".join([head, stamp, *changed]))
        return "\n\n".join(body)


if __name__ == "__main__":  # smoke-тест
    import sys

    law = sys.argv[1] if len(sys.argv) > 1 else "zakon.md"
    guide = sys.argv[2] if len(sys.argv) > 2 else "rukovodstva.md"
    c = LegalCorpus(law, guide)
    print("LAW статей:", len(c.law_sections), "→", ", ".join(c.law_order))
    print("GUIDE статей (после фильтра ст.2/3):", len(c.guide_sections), "→", ", ".join(c.guide_order))
    print("law_whole символов:", len(c.law_whole()))
    print("guidelines_whole символов:", len(c.guidelines_whole()))
    sub = ["ст. 5", "ст. 28"]
    print(f"\nlaw_for({sub}) символов:", len(c.law_for(sub)))
    print(f"guidelines_for({sub}) символов:", len(c.guidelines_for(sub)))
    print("normalize_article('п. 2 ч. 3 ст. 5') =", normalize_article("п. 2 ч. 3 ст. 5"))
