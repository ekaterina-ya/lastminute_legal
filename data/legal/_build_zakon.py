#!/usr/bin/env python3
"""Сборка zakon.md из текста ФЗ "О рекламе".

Вход — txt-конвертация RTF (textutil -convert txt). Берём только главы 1 и 3,
из главы 1 исключаем процедурные ст. 11/12/13, из главы 3 — утратившие силу
ст. 22/23. Вычищаем шум ГАРАНТа и историю изменений. Текст статей — дословный.

Маркер статьи для парсера бота — строка, начинающаяся с "### Статья".
"""
import re
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else \
    "/tmp/legal_txt/Федеральный_закон_от_13_марта_2006_г_N_38_ФЗ_О_рекламе_с_изменениями.txt"
OUT = sys.argv[2] if len(sys.argv) > 2 else \
    "/Users/eya/jupyter_projects/lastminute_legal/data/legal/zakon.md"

ALLOWED = {
    1: {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10"},  # глава 1 без 11/12/13
    3: None,  # глава 3 целиком, кроме утративших силу (см. ниже)
}
CH3_EXCLUDE = {"22", "23"}

NOISE_MARKERS = {"ГАРАНТ:", "Информация об изменениях:"}
RE_CH = re.compile(r"^Глава\s+(\d+)\.\s*(.*)$")
RE_ART = re.compile(r"^Статья\s+(\S+?)\.\s+(.*)$")


def is_noise(line: str) -> bool:
    if line.strip() in NOISE_MARKERS:
        return True
    if line and line[0] in " \t":  # отступ = продолжение блока изменений/ГАРАНТа
        return True
    return False


def main() -> None:
    cur_ch = None          # текущая глава (int) или None
    emit = False           # пишем ли текущую статью
    out: list[str] = ["# ФЗ «О рекламе» — главы 1 и 3 (актуальная редакция)", ""]

    with open(SRC, encoding="utf-8") as f:
        lines = f.read().splitlines()

    started = False
    for line in lines:
        m_ch = RE_CH.match(line)
        if m_ch:
            cur_ch = int(m_ch.group(1))
            emit = False
            if cur_ch in (1, 3):
                started = True
                out += ["", f"## Глава {cur_ch}. {m_ch.group(2)}", ""]
            continue
        if not started:
            continue

        m_art = RE_ART.match(line)
        if m_art:
            num, title = m_art.group(1), m_art.group(2)
            allowed = ALLOWED.get(cur_ch, set())
            ok = cur_ch == 3 and num not in CH3_EXCLUDE or (
                allowed is not None and num in allowed
            )
            ok = ok and not title.lower().startswith("утратил")
            emit = bool(ok)
            if emit:
                out += ["", f"### Статья {num}. {title}"]
            continue

        if emit and not is_noise(line) and line.strip():
            out.append(line)

    # схлопываем тройные пустые строки
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)

    arts = re.findall(r"^### (Статья \S+)", text, re.M)
    print(f"OK: {OUT}\nСтатей: {len(arts)}\n" + ", ".join(arts))


if __name__ == "__main__":
    main()
