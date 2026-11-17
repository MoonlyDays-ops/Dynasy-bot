"""Pure calculations for DYNASTY FAMILY achievements and timed capt reminders.

All achievements are earned from confirmed captain attendance, recorded
capt results and captain-approved clips. No game API is accessed.
"""
from __future__ import annotations

from dataclasses import dataclass

EMBED_GREEN = 0x2ECC71

@dataclass(frozen=True)
class Achievement:
    code: str
    icon: str
    title: str
    criterion: str
    threshold: int
    description: str

ACHIEVEMENTS = (
    Achievement("first_capt", "🎯", "Первый выход", "attended", 1, "Посетить первый капт"),
    Achievement("regular", "⚔️", "В строю", "attended", 5, "Посетить 5 каптов"),
    Achievement("veteran", "🥈", "Ветеран DYNASTY", "attended", 20, "Посетить 20 каптов"),
    Achievement("legend", "🥇", "Легенда состава", "attended", 50, "Посетить 50 каптов"),
    Achievement("first_win", "🏆", "Первая победа", "wins", 1, "Одержать первую победу"),
    Achievement("winner", "🔥", "Победный настрой", "wins", 5, "Одержать 5 побед"),
    Achievement("champion", "👑", "Чемпион Miami", "wins", 20, "Одержать 20 побед"),
    Achievement("first_clip", "🎥", "Первый откат", "approved_clips", 1, "Получить одобрение первого отката"),
    Achievement("operator", "📹", "Оператор семьи", "approved_clips", 5, "Получить одобрение 5 откатов"),
)


def achievement_progress(stats: dict):
    """Return pairs (achievement, current value), ordered by display priority."""
    return [(ach, max(0, int(stats.get(ach.criterion, 0) or 0))) for ach in ACHIEVEMENTS]


def earned_achievements(stats: dict):
    return [ach for ach, current in achievement_progress(stats) if current >= ach.threshold]


def upcoming_achievements(stats: dict):
    return [(ach, current) for ach, current in achievement_progress(stats) if current < ach.threshold]


def progress_bar(current: int, maximum: int, width: int = 10) -> str:
    """Discord-safe visual bar; unlike embed left stripe, this uses colored emoji."""
    if maximum <= 0:
        return "⬜" * width
    filled = min(width, max(0, current) * width // maximum)
    return "🟩" * filled + "⬛" * (width - filled)


def due_reminder_minutes(scheduled_at: int, now: int, grace_seconds: int = 90) -> int | None:
    """Match one 30/15/5-minute slot only shortly after its trigger.

    A delayed restart must NOT blast all missed reminders at once.
    The outer loop polls every ~20-30 seconds.
    """
    remaining = int(scheduled_at) - int(now)
    for minutes in (30, 15, 5):
        if minutes * 60 - grace_seconds <= remaining <= minutes * 60:
            return minutes
    return None
