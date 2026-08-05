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

import re

_LAW_ART_RE = re.compile(r"^### Статья (\S+?)\.\s")
_GUIDE_ART_RE = re.compile(r"^## ст\.\s*(\S+)")
_ART_NUM_RE = re.compile(r"ст\.?\s*(\d+(?:\.\d+)?)")

# Руководства по понятию рекламы (классификация «реклама / не реклама»)
# в этой фиче не используются — это отдельная будущая задача.
GUIDE_EXCLUDE = {"ст. 2", "ст. 3"}


def normalize_article(raw: str) -> str | None:
    """«п. 2 ч. 3 ст. 5» / «ст. 28.1» → «ст. 5» / «ст. 28.1». None, если статьи нет."""
    m = _ART_NUM_RE.search(raw or "")
    return f"ст. {m.group(1)}" if m else None


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


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class LegalCorpus:
    """Загруженные и распарсенные закон + руководства."""

    def __init__(self, law_path: str, guidelines_path: str):
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
    def law_for(self, articles: list[str]) -> str:
        keys = [a for a in articles if a in self.law_sections]
        if not keys:
            return ""
        body = "\n\n".join(self.law_sections[k] for k in keys)
        return f"{self.law_preamble}\n\n{body}".strip()

    def guidelines_for(self, articles: list[str]) -> str:
        keys = [a for a in articles if a in self.guide_sections]
        if not keys:
            return ""
        body = "\n\n".join(self.guide_sections[k] for k in keys)
        return f"{self.guide_preamble}\n\n{body}".strip()


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
