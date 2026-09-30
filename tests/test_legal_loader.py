"""Тесты загрузчика закона: новые статьи и названия видны с даты вступления.

Запуск из корня репозитория: python -m unittest tests.test_legal_loader
(pytest не нужен; модуль не тянет Gemini и данные бота).
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "data", "legal"))
import legal_loader  # noqa: E402
from legal_loader import LegalCorpus  # noqa: E402

LEGAL_DIR = os.path.join(ROOT, "data", "legal")

ZAKON = """Преамбула.

### Статья 7. Объекты рекламирования
Не допускается реклама:
1) старый пункт.

### Статья 9. Особенности рекламы
1. Текст 9.

### Статья 10. Социальная реклама
1. Текст 10.

### Статья 29.1. Реклама цифровых финансовых активов
1. Текст 29.1.

### Статья 30. Рекламная деятельность
1. Текст 30.
"""

FUTURE = """### Статья 7. Объекты рекламирования, реклама которых не допускается
Вступает в силу с 01.03.2027 (Федеральный закон от 29 декабря 2025 г. № 520-ФЗ).
Не допускается реклама:
1) новый пункт.

### Статья 9.1. Новая статья между 9 и 10
Вступает в силу с 01.09.2026 (учебный закон).
1. Текст 9.1.

### Статья 29.2. Реклама услуг по организации обращения цифровых валют
Вступает в силу с 01.09.2026 (Федеральный закон от 4 августа 2026 г. № 283-ФЗ).
1. Текст 29.2.

### Статья 29.2. Реклама цифровых валют
Вступает в силу с 01.01.2027 (учебный закон 2).
1. Текст 29.2, вторая редакция.

### Статья 30.3. Новая статья в конце
Вступает в силу с 01.09.2026 (учебный закон).
1. Текст 30.3.
"""

BEFORE, SEP, AFTER = date(2026, 8, 31), date(2026, 9, 1), date(2027, 3, 1)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = {}
        for name, text in (("zakon.md", ZAKON), ("zakon_future.md", FUTURE), ("rukovodstva.md", "## ст. 7\nруководство\n")):
            paths[name] = os.path.join(tmp.name, name)
            with open(paths[name], "w", encoding="utf-8") as f:
                f.write(text)
        self.c = LegalCorpus(paths["zakon.md"], paths["rukovodstva.md"], paths["zakon_future.md"])


class TestNewArticle(Base):
    def test_do_daty_net(self):
        self.assertEqual(self.c.order_on(BEFORE), ["ст. 7", "ст. 9", "ст. 10", "ст. 29.1", "ст. 30"])
        self.assertNotIn("29.2", self.c.menu_on(BEFORE))
        self.assertEqual(self.c.law_for(["ст. 29.2"], BEFORE), "")

    def test_s_daty_po_nomeru(self):
        # 9.1 — между 9 и 10 (по строкам ушла бы в конец), 30.3 — в конец
        self.assertEqual(self.c.order_on(SEP),
                         ["ст. 7", "ст. 9", "ст. 9.1", "ст. 10", "ст. 29.1", "ст. 29.2", "ст. 30", "ст. 30.3"])
        menu = self.c.menu_on(SEP).splitlines()
        self.assertEqual(menu[5], "Статья 29.2. Реклама услуг по организации обращения цифровых валют")
        text = self.c.law_for(["ст. 29.2"], SEP)
        self.assertIn("1. Текст 29.2.", text)
        self.assertNotIn("Вступает в силу", text)

    def test_dve_redakcii_novoy_stati(self):
        order = self.c.order_on(AFTER)
        self.assertEqual(order.count("ст. 29.2"), 1)
        self.assertIn("Статья 29.2. Реклама цифровых валют", self.c.menu_on(AFTER).splitlines())
        self.assertIn("вторая редакция", self.c.law_for(["ст. 29.2"], AFTER))


class TestNewTitle(Base):
    def test_nazvanie_s_daty(self):
        self.assertEqual(self.c.menu_on(BEFORE).splitlines()[0], "Статья 7. Объекты рекламирования")
        self.assertEqual(self.c.menu_on(AFTER).splitlines()[0],
                         "Статья 7. Объекты рекламирования, реклама которых не допускается")
        self.assertIn("1) новый пункт.", self.c.law_for(["ст. 7"], AFTER))
        self.assertIn("1) старый пункт.", self.c.law_for(["ст. 7"], BEFORE))


class TestMoscowToday(unittest.TestCase):
    def test_polnoch_po_moskve(self):
        # 31.08 22:30 UTC = 01.09 01:30 МСК: в Москве уже 1 сентября
        fake = datetime(2026, 8, 31, 22, 30, tzinfo=timezone.utc)
        with mock.patch.object(legal_loader, "datetime") as dt:
            dt.now.side_effect = lambda tz=None: fake.astimezone(tz)
            self.assertEqual(legal_loader._today(), date(2026, 9, 1))


class TestRealCorpus(unittest.TestCase):
    """Настоящий корпус до первой даты вступления: всё как было до правки."""

    def test_kak_ranshe(self):
        c = LegalCorpus(os.path.join(LEGAL_DIR, "zakon.md"), os.path.join(LEGAL_DIR, "rukovodstva.md"),
                        os.path.join(LEGAL_DIR, "zakon_future.md"))
        day = min((when for _, when, _ in c.future_sections), default=date(2026, 1, 1)) - timedelta(days=1)
        self.assertEqual(c.order_on(day), c.law_order)
        old_menu = "\n".join(c.law_sections[k].splitlines()[0].replace('### ', '') for k in c.law_order)
        self.assertEqual(c.menu_on(day), old_menu)
        self.assertEqual(c.law_for(["ст. 5", "ст. 7"], day).count("### Статья"), 2)


if __name__ == "__main__":
    unittest.main()
