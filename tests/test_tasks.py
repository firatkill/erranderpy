from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from erranderpy.core.db import Database
from erranderpy.core.jobs import Job, daily
from erranderpy.core.registry import load_agents
from erranderpy.core.text import find_unique, phrases_match
from erranderpy.core.tools import ToolContext

TZ = ZoneInfo("Europe/Istanbul")
TUESDAY = datetime(2026, 11, 3, 10, 0, tzinfo=TZ)  # a Tuesday
USER = 7


class Tasks:
    """Drive the tasks agent's tools and jobs at a chosen 'now'."""

    def __init__(self):
        self.db = Database(":memory:")
        self.agent = load_agents(db=self.db)["tasks"]
        self.now = TUESDAY

    def ctx(self):
        return ToolContext(self.db, USER, USER, self.now, self.agent.messages_for("tr"))

    def __call__(self, tool, **args):
        return self.agent.tools[tool].run(self.ctx(), args).text

    def job(self, name):
        return next(j for j in self.agent.jobs if j.name == name).fn(self.ctx())

    def task_count(self):
        return self.db.one("SELECT COUNT(*) AS n FROM tasks")["n"]


@pytest.fixture
def tasks():
    return Tasks()


def test_habit_with_target_goes_on_todays_list(tasks):
    assert tasks("create_habit", name="ingilizce okuma", daily_minutes=30) == (
        "🔁 Yeni alışkanlık: ingilizce okuma, günde 30 dk. Bugünden başlıyoruz."
    )
    assert "⏳ ingilizce okuma · 0/30 dk" in tasks("list_tasks")


def test_habit_without_target_asks_for_minutes_and_can_be_updated(tasks):
    assert "Günde kaç dakika" in tasks("create_habit", name="diksiyon")
    assert tasks("create_habit", name="diksiyon", daily_minutes=15) == "✏️ diksiyon: artık günde 15 dk."
    assert "diksiyon · 0/15 dk" in tasks("list_tasks")
    assert tasks("create_habit", name="diksiyon", daily_minutes=15) == "Bu alışkanlık zaten var: diksiyon."
    tasks("create_habit", name="meditasyon")
    assert tasks("create_habit", name="meditasyon").startswith("👍 Tamam, meditasyon süresiz kalıyor.")


def test_progress_accumulates_and_finishes(tasks):
    tasks("create_habit", name="ingilizce okuma", daily_minutes=30)
    assert tasks("log_progress", habit="ingilizceyi", minutes=20) == "📈 ingilizce okuma: 20/30 dk, 10 dk kaldı."
    assert tasks("log_progress", habit="ingilizce", minutes=15) == "✅ ingilizce okuma: 35/30 dk. Bugünlük tamam!"
    assert "✅ ingilizce okuma · 35/30 dk" in tasks("list_tasks")


def test_progress_on_unknown_habit_lists_the_real_ones(tasks):
    assert "Henüz bir alışkanlığın yok" in tasks("log_progress", habit="koşu", minutes=10)
    tasks("create_habit", name="kitap okuma", daily_minutes=20)
    reply = tasks("log_progress", habit="koşu", minutes=10)
    assert reply == '"koşu" adında bir alışkanlık bulamadım. Alışkanlıkların: kitap okuma.'


def test_days_are_resolved_in_code(tasks):
    assert tasks("add_task", title="faturayı öde", when="tomorrow") == "📌 Ekledim: faturayı öde (yarın)."
    assert tasks("add_task", title="kuaför", when="thursday") == "📌 Ekledim: kuaför (05.11 Perşembe)."
    assert tasks("add_task", title="dişçi", when="Salı") == "📌 Ekledim: dişçi (10.11 Salı)."  # next Tuesday, not today
    assert tasks("add_task", title="kargo", when="2026-11-20") == "📌 Ekledim: kargo (20.11 Cuma)."
    assert "anlayamadım" in tasks("add_task", title="x", when="bir ara")
    assert "Zaten listede" in tasks("add_task", title="Faturayı öde", when="yarın")


def test_complete_task_and_overdue(tasks):
    tasks("add_task", title="kargoyu al", when="today")
    tasks.now += timedelta(days=2)  # two days later, not done
    listing = tasks("list_tasks")
    assert "⚠️ Gecikenler:" in listing and "☐ kargoyu al (03.11)" in listing
    assert tasks("complete_task", title="kargo") == "✅ Tamamlandı: kargoyu al."
    assert "Gecikenler" not in tasks("list_tasks")


def test_completing_a_habit_fills_its_target(tasks):
    tasks("create_habit", name="kitap okuma", daily_minutes=20)
    assert tasks("complete_task", title="kitap okuma") == "✅ Tamamlandı: kitap okuma."
    assert "✅ kitap okuma · 20/20 dk" in tasks("list_tasks")


def test_stop_and_restart_habit(tasks):
    tasks("create_habit", name="sabah koşusu", daily_minutes=20)
    assert tasks("stop_habit", name="koşu").startswith("⏹️ sabah koşusu")
    assert tasks("list_tasks") == "📋 Bugün için bir şey yok 🎉"
    assert tasks("create_habit", name="sabah koşusu") == "🔁 Tekrar başladık: sabah koşusu."


def test_daily_job_is_idempotent(tasks):
    tasks("create_habit", name="ingilizce", daily_minutes=30)
    tasks("create_habit", name="diksiyon", daily_minutes=15)
    tasks.now += timedelta(days=1)
    for _ in range(3):  # midnight job runs, then a tool runs, then the job again...
        tasks.job("create_daily_habit_tasks")
        tasks("list_tasks")
    assert tasks.task_count() == 4  # 2 habits × 2 days, never more


def test_evening_summary(tasks):
    assert tasks.job("evening_summary") is None  # nothing to report: no message
    tasks("create_habit", name="ingilizce", daily_minutes=30)
    tasks("add_task", title="faturayı öde")
    tasks("log_progress", habit="ingilizce", minutes=30)
    summary = tasks.job("evening_summary")
    assert summary.startswith("🌙 Günün özeti · 03.11 Salı")
    assert "✅ ingilizce · 30/30 dk" in summary and "☐ faturayı öde" in summary
    assert "1/2 tamamlandı." in summary


def test_job_time_can_be_overridden_or_turned_off():
    job = Job("x", lambda ctx: None, at="21:00", env="SUMMARY_TIME")
    assert job.time_of_day({}) == time(21, 0)
    assert job.time_of_day({"SUMMARY_TIME": "20:30"}) == time(20, 30)
    assert job.time_of_day({"SUMMARY_TIME": "off"}) is None
    with pytest.raises(ValueError):
        daily(at="9pm")


def test_loose_matching_handles_turkish_suffixes_but_never_guesses():
    assert phrases_match("ingilizceyi", "ingilizce okuma")
    assert not phrases_match("kitap okuma", "ingilizce okuma")
    names = [("zeytinyağı", 1), ("ayçiçek yağı", 2), ("ekmek", 3)]
    assert find_unique("Zeytinyağı", names) == 1
    assert find_unique("zeytin", names) == 1
    assert find_unique("yağ", names) is None  # two candidates: don't guess
    assert find_unique("ekmeği", names) == 3


def test_both_agents_load_and_the_job_is_found():
    agents = load_agents()
    assert {"shopping", "tasks"} <= set(agents)
    assert {j.name for j in agents["tasks"].jobs} == {"create_daily_habit_tasks", "evening_summary"}
    assert all(j.agent == "tasks" for j in agents["tasks"].jobs)


def test_resolve_day_never_returns_today_for_a_weekday_name():
    from erranderpy_agents.tasks.tools import resolve_day

    tuesday = date(2026, 11, 3)
    assert resolve_day("tuesday", tuesday) == date(2026, 11, 10)
    assert resolve_day("Perşembe", tuesday) == date(2026, 11, 5)


def test_consonant_softening():
    assert phrases_match("kitabı", "kitap okuma")
    assert phrases_match("ekmeği", "ekmek")
    assert not phrases_match("su", "sucuk")
    assert find_unique("su", [("sucuk", 1)]) is None
    assert find_unique("yağ", [("zeytinyağı", 1)]) == 1
