"""Editable settings for the At a glance report cards.

Available goal card views: "weekly_run_count", "two_week_run_count",
"weekly_run_minutes", "weekly_strength", "weekly_intensity". Use an empty
tuple to hide all goal cards for an account.
"""

from datetime import date

GOAL_CARD_VIEWS = {
    "manga": ("weekly_intensity",),
    "chips": ("two_week_run_count", "weekly_intensity"),
}

# Total weekly training minutes needed to fill the intensity pyramid.
INTENSITY_GOAL_MINUTES = 180

# Start date for the Long Run route progress tracker.
LONG_RUN_EPOCH = date(2026, 9, 15)
