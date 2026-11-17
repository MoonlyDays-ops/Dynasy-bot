"""Offline validation of v8 Discord UI state and date/time rules.

These tests deliberately load only the selected AST nodes so that the DB and
UI decisions can be tested without opening a Discord connection or installing
Discord in the local build environment. Live Discord integration is separate.
"""
import ast
import asyncio
import time
import unittest
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parents[1] / "family_features.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf8"), filename=str(SOURCE))


class FakeItem:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.view = None
        self.values = []


class FakeView:
    def __init__(self, *, timeout=None):
        self.timeout = timeout
        self.children = []

    def add_item(self, item):
        self.children.append(item)
        item.view = self
        return self

    def clear_items(self):
        self.children.clear()


class FakeModal:
    def __init_subclass__(cls, **kwargs):
        pass

    def __init__(self, **kwargs):
        self.title = kwargs.get("title")


class FakeMember:
    id = 51


class FakeResponse:
    def __init__(self):
        self.modal = None
        self.content = None

    async def send_modal(self, modal):
        self.modal = modal

    async def edit_message(self, **kwargs):
        self.content = kwargs.get("content")


class FakeInteraction:
    def __init__(self):
        self.guild = SimpleNamespace(id=17)
        self.user = FakeMember()
        self.response = FakeResponse()


class FakeDb:
    def capt_settings(self, guild_id):
        return {"timezone": "Europe/Moscow"}

    def capt_event(self, event_id):
        return {"guild_id": 17, "status": "scheduled"}


class FakeBot:
    def __init__(self):
        self.db = FakeDb()


def load_ui():
    class ButtonStyle:
        secondary = 2
        success = 3

    fake_discord = SimpleNamespace(
        ui=SimpleNamespace(View=FakeView, Select=FakeItem, Button=FakeItem,
                           Modal=FakeModal, TextInput=lambda **kw: FakeItem(**kw)),
        SelectOption=lambda **kw: FakeItem(**kw),
        ButtonStyle=ButtonStyle,
        TextStyle=SimpleNamespace(paragraph=2),
    )
    keep = {
        "capt_schedule_timestamp", "CaptCreateModal", "CaptDaySelect", "CaptHourSelect",
        "CaptMinuteSelect", "CaptScheduleNav", "CaptScheduleConfirm", "CaptScheduleView",
        "CaptFinishModal", "CaptResultSelect", "CaptFinishChoiceView",
    }
    relevant = [x for x in TREE.body if isinstance(x, (ast.FunctionDef, ast.ClassDef)) and x.name in keep]
    namespace = {
        "discord": fake_discord, "date": date, "datetime": datetime,
        "dt_time": dt_time, "timedelta": timedelta, "time": time, "ZoneInfo": ZoneInfo,
        "CAPT_DATE_PAGE_SIZE": 25, "CAPT_DATE_MAX_DAYS": 365,
        "RUS_WEEKDAYS": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
        "CAPT_RESULT_NAMES": {"win": "Победа", "loss": "Поражение", "draw": "Ничья"},
        "clean": str, "can_manage_capts": lambda bot, member: True,
    }
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0)] + relevant,
        type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class CaptUiV8Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = load_ui()

    def test_creation_form_does_not_require_typing_date(self):
        modal_class = next(n for n in TREE.body if isinstance(n, ast.ClassDef)
                           and n.name == "CaptCreateModal")
        fields = {n.targets[0].id for n in modal_class.body
                  if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
        self.assertNotIn("time_text", fields)
        self.assertEqual(fields, {"opponent", "location", "format_text", "capacity"})

    def test_result_choices_are_exactly_three(self):
        bot = FakeBot()
        view = self.ui["CaptFinishChoiceView"](bot, 100, FakeMember.id)
        self.assertEqual([o.value for o in view.children[0].options], ["win", "loss", "draw"])
        choice = view.children[0]
        choice.values = ["draw"]
        i = FakeInteraction()
        asyncio.run(choice.callback(i))
        self.assertEqual(i.response.modal.result_code, "draw")
        self.assertEqual(i.response.modal.title, "DYNASTY • Ничья")

    def test_date_picker_contains_25_dates_and_time_menus(self):
        picker = self.ui["CaptScheduleView"](FakeBot(), 51, 17,
            {"opponent":"Enemy", "location":"Area", "format_text":"10x10", "capacity":10})
        self.assertEqual(len(picker.children), 6)
        self.assertEqual(len(picker.children[0].options), 25)
        self.assertEqual(len(picker.children[1].options), 24)
        self.assertEqual(len(picker.children[2].options), 12)
        self.assertIn("Выберите дату", picker.children[0].placeholder)

    def test_date_navigation_and_one_year_limit(self):
        picker = self.ui["CaptScheduleView"](FakeBot(), 51, 17,
            {"opponent":"Enemy", "location":"Area", "format_text":"10x10", "capacity":10})
        i = FakeInteraction()
        asyncio.run(picker.children[4].callback(i))
        self.assertEqual(picker.page_offset, 25)
        self.assertEqual(picker.selected_date, picker.today + timedelta(days=25))
        for _ in range(13):
            asyncio.run(picker.children[4].callback(i))
        self.assertEqual(picker.page_offset, 350)
        self.assertTrue(picker.children[4].disabled)
        self.assertLessEqual(date.fromisoformat(picker.children[0].options[-1].value),
                             picker.today + timedelta(days=365))

    def test_timestamp_validation_and_dst_gap(self):
        f = self.ui["capt_schedule_timestamp"]
        zone = ZoneInfo("Europe/Moscow")
        future = date.today() + timedelta(days=5)
        stamp = f(future, 20, 30, zone, now=0)
        self.assertEqual(datetime.fromtimestamp(stamp, zone).hour, 20)
        with self.assertRaises(ValueError):
            f(date(2020,1,1), 20, 30, zone)
        with self.assertRaises(ValueError):
            f(future, 25, 30, zone, now=0)
        # 2027-03-28 at 03:30 does not exist in Europe/Riga (spring DST).
        with self.assertRaises(ValueError):
            f(date(2027,3,28), 3, 30, ZoneInfo("Europe/Riga"), now=0)

    def test_only_the_owner_can_change_pickers(self):
        picker = self.ui["CaptScheduleView"](FakeBot(), 99, 17,
            {"opponent":"Enemy", "location":"Area", "format_text":"10x10", "capacity":10})
        # The interaction_check method is defined, and its guard compares owner ID.
        node = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "CaptScheduleView")
        check = next(n for n in node.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "interaction_check")
        code = ast.unparse(check)
        self.assertIn("i.user.id != self.owner_id", code)


if __name__ == "__main__":
    unittest.main()
