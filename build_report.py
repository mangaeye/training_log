import html
import json
import math
import os
import argparse
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import psycopg2

from report_config import GOAL_CARD_VIEWS, INTENSITY_GOAL_MINUTES, LONG_RUN_EPOCH

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "site" / "index.html"
ROUTE_KML = ROOT / "route.kml"
ACCOUNT_NAMES = ("manga", "chips")
# Zone stacks for the intensity pyramid: bottom stack fills up from the base,
# top stack fills down from the apex. Each tuple is (label, color), ordered
# from the stack's starting edge outward.
INTENSITY_BOTTOM_STACK = (
    ("Easy Running (Below VT1/LT1)", "#3f8f7a"),
    ("Tempo Running", "#e0a545"),
)
INTENSITY_TOP_STACK = (
    ("Above Threshold (VT2/LT2)", "#a53f30"),
    ("Threshold Running", "#d2691e"),
)
TRAIL_DAYS = 28
TRAIL_EPOCH = date(2026, 9, 15)
RUN_CADENCE_DAYS = 2
STRENGTH_CADENCE_DAYS = 4
HEALING_SESSION_COUNT = 2
STRENGTH_PYRAMID_SEGMENTS = 16
STRENGTH_PYRAMID_KEEP_DAYS = 6
MAX_HISTORY_WEEKS = 6
REPORT_TIMEZONE = ZoneInfo("Australia/Melbourne")


def load_env_file():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


def ensure_schema(connection):
    with connection.cursor() as cursor:
        cursor.execute((ROOT / "schema.sql").read_text())
    connection.commit()


def format_duration(seconds):
    if seconds is None:
        return "-"
    total_minutes = int((float(seconds) + 30) // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}"


def format_run_duration(seconds):
    if seconds is None:
        return "-"
    total_minutes = int((float(seconds) + 30) // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} h {minutes:02d} m" if hours else f"{minutes} m"


def custom_zone_seconds(zone_seconds):
    if not isinstance(zone_seconds, dict):
        return {}
    native = {}
    for key, value in zone_seconds.items():
        if str(key) not in {"1", "2", "3", "4", "5"}:
            continue
        try:
            native[str(key)] = max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
    return {
        "easy": native.get("1", 0) + native.get("2", 0),
        "tempo": native.get("3", 0),
        "threshold": native.get("4", 0),
        "above": native.get("5", 0),
    }


def weekly_zone_minutes(rows, week_start, today):
    zone_totals = {key: 0 for key, _, _ in RUN_ZONE_PRESENTATION}
    for row in rows:
        if not week_start <= row[0] <= today:
            continue
        for activity in row[6]:
            if str(activity.get("sport", "")).lower() != "running":
                continue
            for key, seconds in custom_zone_seconds(
                activity.get("hr_zone_seconds")
            ).items():
                zone_totals[key] += seconds
    return {key: int((seconds + 30) // 60) for key, seconds in zone_totals.items()}


RUN_ZONE_PRESENTATION = (
    ("easy", "Easy", INTENSITY_BOTTOM_STACK[0][1]),
    ("tempo", "Tempo", INTENSITY_BOTTOM_STACK[1][1]),
    ("threshold", "Threshold", INTENSITY_TOP_STACK[1][1]),
    ("above", "Above threshold", INTENSITY_TOP_STACK[0][1]),
)


def format_hours_minutes(seconds):
    total_minutes = int((float(seconds) + 30) // 60)
    hours, minutes = divmod(total_minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hr" if hours == 1 else f"{hours} hrs")
    if minutes or not parts:
        parts.append(f"{minutes} min" if minutes == 1 else f"{minutes} mins")
    return " ".join(parts)


def recent_intensity_markup(rows, today=None):
    today = today or date.today()
    start_date = today - timedelta(days=41)
    weekly_totals = {}
    for row in rows:
        if not start_date <= row[0] <= today:
            continue
        week_start = row[0] - timedelta(days=row[0].weekday())
        week = weekly_totals.setdefault(
            week_start,
            {
                "has_run": False,
                "run_seconds": 0,
                **{key: 0 for key, _, _ in RUN_ZONE_PRESENTATION},
            },
        )
        for activity in row[6]:
            if str(activity.get("sport", "")).lower() != "running":
                continue
            week["has_run"] = True
            week["run_seconds"] += max(
                0, int(activity.get("duration_seconds", 0) or 0)
            )
            for key, seconds in custom_zone_seconds(
                activity.get("hr_zone_seconds")
            ).items():
                week[key] += seconds

    longest_week_seconds = max(
        (
            week["run_seconds"]
            for week in weekly_totals.values()
            if week["has_run"]
        ),
        default=0,
    )
    items = []
    for week_start in sorted(weekly_totals, reverse=True):
        week = weekly_totals[week_start]
        if not week["has_run"]:
            continue
        bar_width = (
            week["run_seconds"] / longest_week_seconds * 100
            if longest_week_seconds
            else 100
        )
        zone_seconds = {key: week[key] for key, _, _ in RUN_ZONE_PRESENTATION}
        total_seconds = sum(zone_seconds.values())
        if total_seconds:
            segments = []
            labels = []
            for key, label, color in RUN_ZONE_PRESENTATION:
                seconds = zone_seconds[key]
                if not seconds:
                    continue
                percentage = seconds / total_seconds * 100
                segments.append(
                    f'<span class="zone-segment" style="background: {color}; width: {percentage:.2f}%" '
                    f'title="{label}: {format_run_duration(seconds)} ({percentage:.1f}%)"></span>'
                )
                labels.append(f"{label}: {format_run_duration(seconds)}")
            hour_marker_width = min(100, 3600 / total_seconds * 100)
            bar = (
                f'<span class="zone-bar" role="img" aria-label="Week '
                f'{week_start.isocalendar().week} heart-rate zones: '
                f'{html.escape("; ".join(labels))}" '
                f'style="width: {bar_width:.2f}%">'
                f'{"".join(segments)}'
                f'<span class="zone-hour-lines" aria-hidden="true" '
                f'style="background-size: {hour_marker_width:.2f}% 100%"></span></span>'
            )
        else:
            bar = (
                '<span class="zone-bar zone-bar-unavailable" '
                'title="Heart-rate zone data unavailable" '
                f'aria-label="Heart-rate zone data unavailable" '
                f'style="width: {bar_width:.2f}%"></span>'
            )
        items.append(
            f'<div class="recent-intensity-week">'
            f'<strong>{"Current week" if week_start == today - timedelta(days=today.weekday()) else f"Week {week_start.isocalendar().week}"}</strong>{bar}'
            f'<small class="recent-intensity-total">{format_hours_minutes(week["run_seconds"])}</small>'
            f'</div>'
        )

    if not items:
        items.append('<span class="empty-day">No running data</span>')
    return (
        '<article class="summary-card recent-intensity-card">'
        "<h3>Recent Intensity &amp; Volume</h3>"
        f'<div class="recent-intensity-list">{"".join(items)}</div>'
        "</article>"
    )


CYCLING_PRESENTATION = (
    ("cycling", "Cycling", "#1b6c68"),
    ("ebiking", "eBiking", "#ef6546"),
)


def cycling_activity_kind(sport):
    sport = str(sport or "").lower()
    if "e_bike" in sport or "ebike" in sport or "e-bike" in sport:
        return "ebiking"
    if "cycl" in sport or "bik" in sport or "ride" in sport:
        return "cycling"
    return None


def recent_cycling_markup(rows, today=None):
    today = today or date.today()
    start_date = today - timedelta(days=41)
    weekly_totals = {}
    for row in rows:
        if not start_date <= row[0] <= today:
            continue
        week_start = row[0] - timedelta(days=row[0].weekday())
        week = weekly_totals.setdefault(
            week_start, {"has_activity": False, "cycling": 0, "ebiking": 0}
        )
        for activity in row[6]:
            kind = cycling_activity_kind(activity.get("sport"))
            if kind is None:
                continue
            week["has_activity"] = True
            week[kind] += max(0, int(activity.get("duration_seconds", 0) or 0))

    longest_week_seconds = max(
        (
            week["cycling"] + week["ebiking"]
            for week in weekly_totals.values()
            if week["has_activity"]
        ),
        default=0,
    )
    items = []
    for week_start in sorted(weekly_totals, reverse=True):
        week = weekly_totals[week_start]
        if not week["has_activity"]:
            continue
        total_seconds = week["cycling"] + week["ebiking"]
        bar_width = (
            total_seconds / longest_week_seconds * 100
            if longest_week_seconds
            else 100
        )
        if total_seconds:
            segments = []
            labels = []
            for key, label, color in CYCLING_PRESENTATION:
                seconds = week[key]
                if not seconds:
                    continue
                percentage = seconds / total_seconds * 100
                segments.append(
                    f'<span class="zone-segment" style="background: {color}; width: {percentage:.2f}%" '
                    f'title="{label}: {format_run_duration(seconds)} ({percentage:.1f}%)"></span>'
                )
                labels.append(f"{label}: {format_run_duration(seconds)}")
            bar = (
                f'<span class="zone-bar" role="img" aria-label="Week '
                f'{week_start.isocalendar().week} cycling split: '
                f'{html.escape("; ".join(labels))}" '
                f'style="width: {bar_width:.2f}%">'
                f'{"".join(segments)}</span>'
            )
        else:
            bar = (
                '<span class="zone-bar zone-bar-unavailable" '
                'title="Cycling data unavailable" '
                f'aria-label="Cycling data unavailable" '
                f'style="width: {bar_width:.2f}%"></span>'
            )
        items.append(
            f'<div class="recent-intensity-week">'
            f'<strong>Week {week_start.isocalendar().week}</strong>{bar}'
            f'<small class="recent-intensity-total">{int((total_seconds + 30) // 60)} min</small>'
            f'</div>'
        )

    if not items:
        items.append('<span class="empty-day">No cycling data</span>')
    return (
        '<article class="summary-card recent-intensity-card">'
        "<h3>Recent Cycling &amp; eBiking</h3>"
        f'<div class="recent-intensity-list">{"".join(items)}</div>'
        "</article>"
    )


def run_scatter_markup(rows, today):
    points = []
    start_date = today - timedelta(days=27)
    for row in rows:
        if not start_date <= row[0] <= today:
            continue
        for activity in row[6]:
            if str(activity.get("sport", "")).lower() != "running":
                continue
            try:
                distance_km = float(activity.get("distance_meters", 0) or 0) / 1000
                average_hr = float(activity["average_heart_rate"])
            except (KeyError, TypeError, ValueError):
                continue
            if distance_km <= 0 or average_hr <= 0:
                continue
            points.append((distance_km, average_hr, row[0], activity.get("name", "Run")))

    if not points:
        return (
            '<article class="summary-card run-scatter-card">'
            "<h3>Last 28 days - Run Distance vs Average HR</h3>"
            '<p class="goal-message">No running distance and heart-rate data in the last 28 days.</p>'
            "</article>"
        )

    width, height = 340, 200
    left, right, top, bottom = 42, 14, 14, 32
    plot_width = width - left - right
    plot_height = height - top - bottom
    max_distance = max(point[0] for point in points)
    safe_long_run_km = max_distance * 1.1
    min_hr = min(point[1] for point in points)
    max_hr = max(point[1] for point in points)
    distance_scale = max(max_distance * 1.1, 1)
    hr_padding = max((max_hr - min_hr) * 0.15, 3)
    hr_min = max(0, min_hr - hr_padding)
    hr_max = max_hr + hr_padding

    def point_position(distance_km, average_hr):
        x = left + distance_km / distance_scale * plot_width
        y = top + (hr_max - average_hr) / (hr_max - hr_min) * plot_height
        return x, y

    grid = []
    for value in (hr_min, hr_max):
        y = top + (hr_max - value) / (hr_max - hr_min) * plot_height
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            'stroke="#cfd1c6" stroke-width="1"></line>'
            f'<text x="{left - 7}" y="{y + 3:.1f}" text-anchor="end">{value:.0f}</text>'
        )
    circles = []
    for distance_km, average_hr, activity_date, name in points:
        x, y = point_position(distance_km, average_hr)
        is_longest = distance_km == max_distance
        radius = 6 if is_longest else 4
        title = html.escape(
            f"Run date: {activity_date.strftime('%d %b %Y')}; "
            f"Distance: {distance_km:.2f} km; Average HR: {average_hr:.0f} bpm"
        )
        point_markup = (
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" '
            f'fill="{"#1b6c68" if is_longest else "#ef6546"}" stroke="#171b19" stroke-width="1">'
            f'<title>{title}</title></circle>'
        )
        circles.append(point_markup)
    svg = (
        f'<svg class="run-scatter" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Running distance versus average heart rate for the last 28 days">'
        f'{"".join(grid)}'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#171b19"></line>'
        f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#171b19"></line>'
        f'<text x="{left}" y="{height - 8}">0 km</text>'
        f'<text x="{width - right}" y="{height - 8}" text-anchor="end">{distance_scale:.1f} km</text>'
        f'<text x="{width / 2}" y="{height - 8}" text-anchor="middle">Distance</text>'
        f'<text x="12" y="{height / 2}" text-anchor="middle" transform="rotate(-90 12 {height / 2})">Average HR (bpm)</text>'
        f'{"".join(circles)}</svg>'
    )
    return (
        '<article class="summary-card run-scatter-card">'
        "<h3>Last 28 days - Run Distance vs Average HR</h3>"
        f'<div class="run-scatter-shell">{svg}'
        f'<aside class="longest-run-note"><strong>Longest Run</strong>'
        f'<span>{max_distance:.2f} km</span></aside></div>'
        f'<p class="goal-message"><strong>{safe_long_run_km:.2f} kms is the longest run</strong> '
        'you can safely do while minimising injury risk. This is the longest run from the last 28 days '
        '+ 10%, based on Frandsen et al., British Journal of Sports Medicine, 2025. A 5,205-runner, '
        '18-month, 588,071-session prospective cohort study - the largest dataset ever used to analyse '
        'running-load spikes and injury risk.</p>'
        "</article>"
    )


def format_pace(seconds_per_km):
    if seconds_per_km is None or seconds_per_km <= 0:
        return "-"
    total_seconds = int(round(seconds_per_km))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d} /km"


LOCAL_YEARS = (
    ("2023", 2023, "#1b6c68"),
    ("2024", 2024, "#d2691e"),
    ("2025", 2025, "#755d8a"),
    ("2026", 2026, "#3d6fb6"),
)
MIN_PACE_SECONDS_PER_KM = 2 * 60
MAX_PACE_SECONDS_PER_KM = 13 * 60
RUNNING_CADENCE_ENTER = 140
RUNNING_CADENCE_EXIT = 130
RUNNING_STATE_CONFIRM_SECONDS = 10


def local_annual_pace_points(data_root, lag_seconds=0):
    series_by_account = {account: {label: {} for label, _, _ in LOCAL_YEARS} for account in ACCOUNT_NAMES}
    details_root = data_root / "garmin_details"
    for account in ACCOUNT_NAMES:
        account_root = details_root / account
        if not account_root.is_dir():
            continue
        for date_dir in account_root.iterdir():
            if not date_dir.is_dir():
                continue
            try:
                activity_date = date.fromisoformat(date_dir.name)
            except ValueError:
                continue
            year = next(
                (label for label, year_value, _ in LOCAL_YEARS
                 if activity_date.year == year_value),
                None,
            )
            if year is None:
                continue
            for detail_path in date_dir.glob("*.json"):
                try:
                    details = json.loads(detail_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                descriptors = details.get("metricDescriptors") or []
                indexes = {
                    descriptor.get("key"): index
                    for index, descriptor in enumerate(descriptors)
                    if isinstance(descriptor, dict)
                }
                hr_index = indexes.get("directHeartRate")
                timestamp_index = indexes.get("directTimestamp")
                speed_index = indexes.get("directSpeed")
                cadence_index = indexes.get("directDoubleCadence")
                distance_index = indexes.get("sumDistance")
                metrics = [
                    item.get("metrics")
                    for item in details.get("activityDetailMetrics", [])
                    if isinstance(item, dict) and isinstance(item.get("metrics"), list)
                ]
                if hr_index is None or timestamp_index is None or not metrics:
                    continue
                if speed_index is None and distance_index is None:
                    continue
                running_state_by_timestamp = {}
                running_state = False
                cadence_above_seconds = 0.0
                cadence_below_seconds = 0.0
                for position, sample in enumerate(metrics):
                    if len(sample) <= max(index for index in (timestamp_index, cadence_index) if index is not None):
                        continue
                    try:
                        timestamp = float(sample[timestamp_index])
                    except (TypeError, ValueError):
                        continue
                    interval = 1.0
                    if position + 1 < len(metrics):
                        try:
                            interval = (float(metrics[position + 1][timestamp_index]) - timestamp) / 1000
                        except (TypeError, ValueError):
                            interval = 1.0
                    if not 0 < interval <= 10:
                        interval = 1.0
                    cadence = None
                    if cadence_index is not None:
                        try:
                            cadence = float(sample[cadence_index])
                        except (TypeError, ValueError):
                            cadence = None
                    if cadence is not None and cadence >= RUNNING_CADENCE_ENTER:
                        cadence_above_seconds += interval
                        cadence_below_seconds = 0.0
                        if cadence_above_seconds >= RUNNING_STATE_CONFIRM_SECONDS:
                            running_state = True
                    elif cadence is not None and cadence < RUNNING_CADENCE_EXIT:
                        cadence_below_seconds += interval
                        cadence_above_seconds = 0.0
                        if cadence_below_seconds >= RUNNING_STATE_CONFIRM_SECONDS:
                            running_state = False
                    try:
                        heart_rate = float(sample[hr_index])
                    except (TypeError, ValueError):
                        continue
                    running_state_by_timestamp[timestamp] = (heart_rate, running_state)
                buckets = series_by_account[account][year]
                previous_interval = 1.0
                previous_distance = None
                start_timestamp = None
                for position, sample in enumerate(metrics):
                    required = max(
                        index for index in (
                            hr_index,
                            timestamp_index,
                            speed_index,
                            cadence_index,
                            distance_index,
                        )
                        if index is not None
                    )
                    if len(sample) <= required:
                        continue
                    try:
                        heart_rate = float(sample[hr_index])
                        timestamp = float(sample[timestamp_index])
                    except (TypeError, ValueError):
                        continue
                    if start_timestamp is None:
                        start_timestamp = timestamp
                    interval = previous_interval
                    if position + 1 < len(metrics):
                        try:
                            interval = (float(metrics[position + 1][timestamp_index]) - timestamp) / 1000
                        except (TypeError, ValueError):
                            interval = previous_interval
                    if not 0 < interval <= 10:
                        interval = previous_interval
                    previous_interval = interval
                    distance_delta = None
                    if speed_index is not None:
                        try:
                            distance_delta = float(sample[speed_index]) * interval
                        except (TypeError, ValueError):
                            pass
                    if distance_delta is None and distance_index is not None:
                        try:
                            distance = float(sample[distance_index])
                        except (TypeError, ValueError):
                            distance = None
                        if distance is not None:
                            if previous_distance is not None:
                                distance_delta = max(0.0, distance - previous_distance)
                            previous_distance = distance
                    pace_seconds_per_km = 1000 / distance_delta if distance_delta else 0
                    running_state = running_state_by_timestamp.get(timestamp, (heart_rate, False))[1]
                    if (
                        not distance_delta
                        or distance_delta <= 0
                        or (timestamp - start_timestamp) / 1000 < 180
                        or not MIN_PACE_SECONDS_PER_KM <= pace_seconds_per_km <= MAX_PACE_SECONDS_PER_KM
                    ):
                        continue
                    if not running_state:
                        continue
                    if lag_seconds:
                        future_timestamp = next(
                            (
                                future
                                for future in running_state_by_timestamp
                                if future >= timestamp + lag_seconds * 1000
                                and running_state_by_timestamp[future][1]
                            ),
                            None,
                        )
                        if future_timestamp is None:
                            continue
                        heart_rate = running_state_by_timestamp[future_timestamp][0]
                    bucket = int(heart_rate // 3) * 3
                    totals = buckets.setdefault(bucket, [0.0, 0.0])
                    totals[0] += interval
                    totals[1] += distance_delta
    return {
        account: {
            year: [(bucket + 1.5, seconds / (distance / 1000)) for bucket, (seconds, distance) in buckets.items() if distance > 0]
            for year, buckets in years.items()
        }
        for account, years in series_by_account.items()
    }


    stats = {
        account: {
            label: {"seconds": 0.0, "distance_m": 0.0, "hr_by_pace": {}}
            for label, _, _ in LOCAL_YEARS
        }
        for account in ACCOUNT_NAMES
    }
    for account in ACCOUNT_NAMES:
        for detail_path in (data_root / "garmin_details" / account).glob("*/*.json"):
            try:
                activity_date = date.fromisoformat(detail_path.parent.name)
                details = json.loads(detail_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            year = next(
                (label for label, year_value, _ in LOCAL_YEARS if activity_date.year == year_value),
                None,
            )
            if year is None:
                continue
            indexes = {
                item.get("key"): index
                for index, item in enumerate(details.get("metricDescriptors", []))
                if isinstance(item, dict)
            }
            hr_index = indexes.get("directHeartRate")
            timestamp_index = indexes.get("directTimestamp")
            speed_index = indexes.get("directSpeed")
            cadence_index = indexes.get("directDoubleCadence")
            if None in (hr_index, timestamp_index, speed_index):
                continue
            metrics = [
                item.get("metrics")
                for item in details.get("activityDetailMetrics", [])
                if isinstance(item, dict) and isinstance(item.get("metrics"), list)
            ]
            running = False
            above_seconds = below_seconds = 0.0
            start_timestamp = previous_timestamp = None
            year_stats = stats[account][year]
            for sample in metrics:
                required = max(hr_index, timestamp_index, speed_index, cadence_index or 0)
                if len(sample) <= required:
                    continue
                try:
                    timestamp = float(sample[timestamp_index])
                    heart_rate = float(sample[hr_index])
                    speed = float(sample[speed_index])
                except (TypeError, ValueError):
                    continue
                if start_timestamp is None:
                    start_timestamp = timestamp
                interval = (
                    1.0
                    if previous_timestamp is None
                    else (timestamp - previous_timestamp) / 1000
                )
                if not 0 < interval <= 10:
                    interval = 1.0
                previous_timestamp = timestamp
                cadence = None
                if cadence_index is not None:
                    try:
                        cadence = float(sample[cadence_index])
                    except (TypeError, ValueError):
                        pass
                if cadence is not None and cadence >= RUNNING_CADENCE_ENTER:
                    above_seconds += interval
                    below_seconds = 0.0
                    if above_seconds >= RUNNING_STATE_CONFIRM_SECONDS:
                        running = True
                elif cadence is not None and cadence < RUNNING_CADENCE_EXIT:
                    below_seconds += interval
                    above_seconds = 0.0
                    if below_seconds >= RUNNING_STATE_CONFIRM_SECONDS:
                        running = False
                if not running or speed <= 0 or heart_rate <= 0:
                    continue
                pace_seconds = 1000 / speed
                if (
                    (timestamp - start_timestamp) / 1000 < 180
                    or not MIN_PACE_SECONDS_PER_KM <= pace_seconds <= MAX_PACE_SECONDS_PER_KM
                ):
                    continue
                pace_band = int(pace_seconds // 10) * 10
                band = year_stats["hr_by_pace"].setdefault(pace_band, [0.0, 0.0])
                band[0] += heart_rate * interval
                band[1] += interval
                year_stats["seconds"] += interval
                year_stats["distance_m"] += speed * interval
    return stats


def local_annual_summary(data_root, account_name=None):
    summaries = {
        account: {str(year): {"runs": 0, "seconds": 0.0, "distance_m": 0.0}
                  for _, year, _ in LOCAL_YEARS}
        for account in ACCOUNT_NAMES
    }
    accounts = (account_name,) if account_name else ACCOUNT_NAMES
    for account in accounts:
        for detail_path in (data_root / "garmin_details" / account).glob("*/*.json"):
            try:
                activity_date = date.fromisoformat(detail_path.parent.name)
                details = json.loads(detail_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            year = str(activity_date.year)
            if year not in summaries[account]:
                continue
            indexes = {
                item.get("key"): index
                for index, item in enumerate(details.get("metricDescriptors", []))
                if isinstance(item, dict)
            }
            duration_index = indexes.get("sumDuration")
            distance_index = indexes.get("sumDistance")
            speed_index = indexes.get("directSpeed")
            cadence_index = indexes.get("directDoubleCadence")
            metrics = [
                item.get("metrics")
                for item in details.get("activityDetailMetrics", [])
                if isinstance(item, dict) and isinstance(item.get("metrics"), list)
            ]
            if duration_index is None or distance_index is None or not metrics:
                continue
            duration = distance = 0.0
            has_running_cadence = False
            for sample in metrics:
                required = max(duration_index, distance_index, speed_index or 0, cadence_index or 0)
                if len(sample) <= required:
                    continue
                try:
                    duration = max(duration, float(sample[duration_index]))
                    distance = max(distance, float(sample[distance_index]))
                except (TypeError, ValueError):
                    continue
                if cadence_index is not None:
                    try:
                        if float(sample[cadence_index]) >= RUNNING_CADENCE_ENTER:
                            has_running_cadence = True
                    except (TypeError, ValueError):
                        pass
            pace = 1000 * duration / distance if distance > 0 else 0
            if (
                not has_running_cadence
                or duration < 180
                or not MIN_PACE_SECONDS_PER_KM <= pace <= MAX_PACE_SECONDS_PER_KM
            ):
                continue
            summary = summaries[account][year]
            summary["runs"] += 1
            summary["seconds"] += duration
            summary["distance_m"] += distance
    return summaries


def annual_summary_markup(data_root, rows, account_name):
    raw_summaries = local_annual_summary(data_root, account_name)[account_name]
    resting_by_year = {str(year): [] for _, year, _ in LOCAL_YEARS}
    for row in rows:
        year = str(row[0].year)
        if year in resting_by_year and row[7] is not None:
            try:
                resting_by_year[year].append(float(row[7]))
            except (TypeError, ValueError):
                pass
    cells = []
    for label, year, _ in LOCAL_YEARS:
        summary = raw_summaries[str(year)]
        resting = resting_by_year[str(year)]
        values = (
            str(summary["runs"]) if summary["runs"] else "No data",
            format_hours_minutes(summary["seconds"]) if summary["runs"] else "No data",
            f'{summary["distance_m"] / 1000:.2f} km' if summary["runs"] else "No data",
            f"{sum(resting) / len(resting):.0f} bpm" if resting else "No data",
        )
        cells.append(values)
    metric_labels = ("Total number of runs", "Total time running", "Total kms ran", "Average resting HR")
    rows_markup = "".join(
        f'<tr><th scope="row">{metric}</th>{"".join(f"<td>{html.escape(cells[index][metric_index])}</td>" for index in range(len(cells)))}</tr>'
        for metric_index, metric in enumerate(metric_labels)
    )
    return (
        '<article class="summary-card local-annual-card">'
        "<h3>Annual Summary</h3>"
        '<div class="table-wrap annual-table-wrap"><table class="annual-table">'
        '<thead><tr><th scope="col">Metric</th>'
        + "".join(f'<th scope="col">{label}</th>' for label, _, _ in LOCAL_YEARS)
        + f"</tr></thead><tbody>{rows_markup}</tbody></table></div></article>"
    )


def efficiency_summary_markup(efficiency_stats):
    def pace_label(seconds):
        return format_pace(seconds).replace(" /km", "/km")

    year_values = {}
    for label, _, _ in LOCAL_YEARS:
        item = efficiency_stats[label]
        year_values[label] = (
            item["seconds"] / (item["distance_m"] / 1000)
            if item["distance_m"]
            else None
        )
    changes = []
    for first, second in zip(LOCAL_YEARS, LOCAL_YEARS[1:]):
        first_value = year_values[first[0]]
        second_value = year_values[second[0]]
        if first_value is not None and second_value is not None:
            changes.append(
                f"{first[0]} to {second[0]}: "
                f"{abs(first_value - second_value):.0f} seconds/km "
                f"{'faster' if second_value < first_value else 'slower'}"
            )
    first_year = efficiency_stats[LOCAL_YEARS[0][0]]["hr_by_pace"]
    latest_year = efficiency_stats[LOCAL_YEARS[-1][0]]["hr_by_pace"]
    improvements = []
    for pace_band in set(first_year) & set(latest_year):
        first_hr = first_year[pace_band][0] / first_year[pace_band][1]
        latest_hr = latest_year[pace_band][0] / latest_year[pace_band][1]
        improvements.append((first_hr - latest_hr, pace_band))
    best = sorted(improvements, reverse=True)[:5]
    best_markup = "".join(
        f"<li>{pace_label(pace_band)}: <strong>{change:.1f} bpm lower</strong></li>"
        for change, pace_band in best
        if change > 0
    ) or "<li>No lower-HR pace bands yet</li>"
    year_markup = "".join(
        f"<li>{label}: <strong>{pace_label(value) if value else 'No data'}</strong></li>"
        for label, value in year_values.items()
    )
    return (
        '<article class="summary-card local-efficiency-card">'
        "<h3>Running Efficiency</h3>"
        '<p class="summary-period">Lower heart rate at the same pace indicates improvement.</p>'
        '<div class="efficiency-columns">'
        f"<div><strong>Average pace</strong><ul>{year_markup}</ul></div>"
        f"<div><strong>2023 to 2026 HR improvements</strong><ul>{best_markup}</ul></div>"
        f'<p class="goal-message">{" · ".join(changes) if changes else "Not enough annual data yet."}</p>'
        "</div>"
        "</article>"
    )


def pace_by_hr_markup(
    rows,
    today,
    zone_settings,
    annual_points=None,
    title="Pace vs Heart Rate",
):
    settings = next(
        (
            item for item in (zone_settings or [])
            if isinstance(item, dict) and item.get("sport") in {"DEFAULT", "RUNNING"}
        ),
        next((item for item in (zone_settings or []) if isinstance(item, dict)), None),
    )
    try:
        floors = [int(settings[f"zone{index}Floor"]) for index in range(1, 6)]
        maximum = int(settings["maxHeartRateUsed"])
    except (KeyError, TypeError, ValueError):
        return (
            '<article class="summary-card pace-hr-card">'
            f"<h3>{html.escape(title)}</h3>"
            '<p class="goal-message">Garmin zone settings are not available yet.</p>'
            "</article>"
        )

    if annual_points is None:
        start_date = today - timedelta(days=27)
        bucket_totals: dict[int, list[float]] = {}
        for row in rows:
            if not start_date <= row[0] <= today:
                continue
            for activity in row[6]:
                if str(activity.get("sport", "")).lower() != "running":
                    continue
                for bucket_key, values in (activity.get("pace_by_hr_bucket") or {}).items():
                    if not isinstance(values, dict):
                        continue
                    try:
                        bucket = int(bucket_key)
                        seconds = float(values.get("seconds", 0) or 0)
                        distance_m = float(values.get("distance_m", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if seconds <= 0 or distance_m <= 0:
                        continue
                    totals = bucket_totals.setdefault(bucket, [0.0, 0.0])
                    totals[0] += seconds
                    totals[1] += distance_m
        series = [
            (
                "Last 28 days",
                [
                    (bucket + 1.5, seconds / (distance / 1000))
                    for bucket, (seconds, distance) in bucket_totals.items()
                    if MIN_PACE_SECONDS_PER_KM <= seconds / (distance / 1000) <= MAX_PACE_SECONDS_PER_KM
                ],
                "#1b6c68",
            )
        ]
    else:
        series = [
            (
                label,
                [
                    (heart_rate, pace)
                    for heart_rate, pace in points
                    if MIN_PACE_SECONDS_PER_KM <= pace <= MAX_PACE_SECONDS_PER_KM
                ],
                color,
            )
            for label, _, color in LOCAL_YEARS
            if (points := annual_points.get(label))
        ]
    points = [point for _, series_points, _ in series for point in series_points]

    if not points:
        return (
            '<article class="summary-card pace-hr-card">'
            f"<h3>{html.escape(title)}</h3>"
            '<p class="goal-message">No detailed running pace/heart-rate data yet. '
            "Run a <code>--resync</code> backfill to populate historical activities.</p>"
            "</article>"
        )

    width, height = 340, 200
    left, right, top, bottom = 50, 14, 14, 32
    plot_width = width - left - right
    plot_height = height - top - bottom
    data_hr_values = [hr for hr, _ in points]
    hr_min = min(floors[0], min(data_hr_values)) - 2
    hr_max = max(maximum, max(data_hr_values)) + 2
    pace_min, pace_max = MIN_PACE_SECONDS_PER_KM, MAX_PACE_SECONDS_PER_KM

    def point_position(hr, pace_seconds_per_km):
        x = left + (hr - hr_min) / (hr_max - hr_min) * plot_width
        y = top + (pace_seconds_per_km - pace_min) / (pace_max - pace_min) * plot_height
        return x, y

    def hr_to_x(value):
        return left + (value - hr_min) / (hr_max - hr_min) * plot_width

    zone_bands = []
    for color, band_start, band_end in (
        (INTENSITY_BOTTOM_STACK[0][1], floors[0], floors[2]),
        (INTENSITY_BOTTOM_STACK[1][1], floors[2], floors[3]),
        (INTENSITY_TOP_STACK[1][1], floors[3], floors[4]),
        (INTENSITY_TOP_STACK[0][1], floors[4], maximum),
    ):
        x_start = hr_to_x(band_start)
        x_end = hr_to_x(band_end)
        zone_bands.append(
            f'<rect x="{x_start:.1f}" y="{top}" width="{max(x_end - x_start, 0):.1f}" '
            f'height="{plot_height:.1f}" fill="{color}" fill-opacity="0.3"></rect>'
        )

    grid = []
    for value in list(floors) + [maximum]:
        x = left + (value - hr_min) / (hr_max - hr_min) * plot_width
        grid.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{height - bottom}" '
            'stroke="#cfd1c6" stroke-width="1"></line>'
            f'<text x="{x:.1f}" y="{height - bottom + 14}" text-anchor="middle">{value}</text>'
        )
    for pace_value in range(MIN_PACE_SECONDS_PER_KM, MAX_PACE_SECONDS_PER_KM + 1, 30):
        y = top + (pace_value - pace_min) / (pace_max - pace_min) * plot_height
        pace_label = format_pace(pace_value).replace(" /km", "")
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            'stroke="#cfd1c6" stroke-width="1"></line>'
            f'<text x="{left - 7}" y="{y + 3:.1f}" text-anchor="end">{pace_label}</text>'
        )

    circles = []
    for series_index, (label, series_points, color) in enumerate(series):
        series_id = f"pace-series-{series_index}"
        for hr, pace_seconds_per_km in sorted(series_points):
            x, y = point_position(hr, pace_seconds_per_km)
            tooltip_title = html.escape(
                f"{label}; HR: {hr:.0f} bpm; Pace: {format_pace(pace_seconds_per_km)}"
            )
            circles.append(
                f'<circle data-series="{series_id}" cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}" stroke="#171b19" stroke-width="1">'
                f'<title>{tooltip_title}</title></circle>'
            )

    svg = (
        f'<svg class="run-scatter" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Average running pace versus heart rate for the last 28 days">'
        f'{"".join(zone_bands)}'
        f'{"".join(grid)}'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#171b19"></line>'
        f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#171b19"></line>'
        f'<text x="{width / 2}" y="{height - 8}" text-anchor="middle">Heart rate (bpm)</text>'
        f'<text x="12" y="{height / 2}" text-anchor="middle" transform="rotate(-90 12 {height / 2})">Pace</text>'
        f'{"".join(circles)}</svg>'
    )
    legend = ""
    if annual_points is not None:
        legend_items = [
            f'<label><input type="checkbox" data-series-toggle="pace-series-{index}" checked>'
            f'<i style="background:{color}"></i>{html.escape(label)}</label>'
            for index, (label, _, color) in enumerate(series)
        ]
        legend = f'<div class="pace-hr-legend">{"".join(legend_items)}</div>'
    card_class = "summary-card pace-hr-card lagged-pace-hr-card" if annual_points is not None else "summary-card pace-hr-card"
    return (
        f'<article class="{card_class}">'
        f"<h3>{html.escape(title)}</h3>"
        f'<div class="run-scatter-shell">{svg}</div>'
        '<p class="goal-message">Average pace per 3 bpm heart-rate bucket, with zone floors marked on the horizontal axis.</p>'
        + legend
        + "</article>"
    )


def zone_legend_markup(zone_settings):
    settings = next(
        (
            item for item in (zone_settings or [])
            if isinstance(item, dict) and item.get("sport") in {"DEFAULT", "RUNNING"}
        ),
        next((item for item in (zone_settings or []) if isinstance(item, dict)), None),
    )
    if not settings:
        return (
            '<article class="summary-card zone-legend-card">'
            "<h3>Zone mapping</h3>"
            '<p class="goal-message">Garmin zone settings are not available yet.</p>'
            "</article>"
        )
    try:
        floors = [int(settings[f"zone{index}Floor"]) for index in range(1, 6)]
        maximum = int(settings["maxHeartRateUsed"])
    except (KeyError, TypeError, ValueError):
        return (
            '<article class="summary-card zone-legend-card">'
            "<h3>Zone mapping</h3>"
            '<p class="goal-message">Garmin zone settings are incomplete.</p>'
            "</article>"
        )

    mappings = (
        ("Easy", "Garmin Zones 1 + 2", INTENSITY_BOTTOM_STACK[0][1], floors[0], floors[2] - 1),
        ("Tempo", "Garmin Zone 3", INTENSITY_BOTTOM_STACK[1][1], floors[2], floors[3] - 1),
        ("Threshold", "Garmin Zone 4", INTENSITY_TOP_STACK[1][1], floors[3], floors[4] - 1),
        ("VO2max", "Garmin Zone 5", INTENSITY_TOP_STACK[0][1], floors[4], maximum),
    )
    items = []
    for label, source, color, floor, ceiling in mappings:
        items.append(
            f'<div class="zone-legend-row">'
            f'<span class="intensity-swatch" style="background: {color}"></span>'
            f'<strong>{label}</strong><small>{source} · {floor}-{ceiling} bpm</small></div>'
        )
    return (
        '<article class="summary-card zone-legend-card">'
        "<h3>Zone mapping</h3>"
        f'<div class="zone-legend-list">{"".join(items)}</div>'
        "</article>"
    )


def format_date(day):
    return day.strftime("%a %d %b")


def format_distance(meters):
    if meters is None:
        return "-"
    return f"{float(meters) / 1000:.2f}"


def format_whole_distance(meters):
    if meters is None:
        return "-"
    return str(int(float(meters) / 1000 + 0.5))


def route_coordinates():
    if not ROUTE_KML.exists():
        return []
    root = ElementTree.parse(ROUTE_KML).getroot()
    namespace = "{http://www.opengis.net/kml/2.2}"
    points = []
    for coordinates in root.findall(f".//{namespace}LineString/{namespace}coordinates"):
        for value in coordinates.text.split():
            longitude, latitude, *_ = (float(part) for part in value.split(","))
            points.append((latitude, longitude))
    return points


def segment_distance_meters(first, second):
    latitude_1, longitude_1 = map(math.radians, first)
    latitude_2, longitude_2 = map(math.radians, second)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = longitude_2 - longitude_1
    haversine = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(delta_longitude / 2) ** 2
    )
    return 2 * 6371000 * math.asin(math.sqrt(haversine))


def route_distance_meters(points=None):
    points = route_coordinates() if points is None else points
    return sum(
        segment_distance_meters(first, second)
        for first, second in zip(points, points[1:])
    )


def route_progress_point(points, distance_meters):
    if not points:
        return None
    remaining = max(distance_meters, 0)
    for first, second in zip(points, points[1:]):
        segment = segment_distance_meters(first, second)
        if remaining <= segment:
            fraction = remaining / segment if segment else 0
            return [
                first[0] + (second[0] - first[0]) * fraction,
                first[1] + (second[1] - first[1]) * fraction,
            ]
        remaining -= segment
    return list(points[-1])


def sampled_route(points, maximum_points=1200):
    if len(points) <= maximum_points:
        return points
    step = math.ceil((len(points) - 1) / (maximum_points - 1))
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def long_run_markup(
    rows, today, account_name, completed_by_account, completed_by_account_yesterday
):
    epoch = LONG_RUN_EPOCH
    route_points = route_coordinates()
    total_race_distance = route_distance_meters(route_points)
    completed_rows = [row for row in rows if epoch <= row[0] <= today]
    completed_meters = sum(
        max(0.0, float(row[4] or 0))
        for row in completed_rows
    )
    elapsed_days = max((today - epoch).days + 1, 1)
    average_meters_per_day = completed_meters / elapsed_days
    remaining_meters = max(total_race_distance - completed_meters, 0)
    days_remaining = (
        math.ceil(remaining_meters / average_meters_per_day)
        if average_meters_per_day > 0 else None
    )
    arrival_date = today + timedelta(days=days_remaining) if days_remaining is not None else None
    finished = completed_meters >= total_race_distance
    completed_value = (
        f"FINISHED! ({completed_meters / 1000:.1f} km)"
        if finished
        else f"{completed_meters / 1000:.1f} km"
    )
    arrival_value = "ARRIVED!" if finished else (arrival_date.strftime('%a %d %b %Y') if arrival_date else '-')
    map_id = "long-run-map-" + "".join(
        character for character in f"{account_name}-{today}" if character.isalnum()
    )
    route_json = json.dumps([[latitude, longitude] for latitude, longitude in sampled_route(route_points)])
    marker_scripts = []
    marker_names = {"manga": "m", "chips": "c"}
    for marker_account, marker_name in marker_names.items():
        marker_point = route_progress_point(
            route_points, completed_by_account.get(marker_account, 0)
        )
        yesterday_point = route_progress_point(
            route_points, completed_by_account_yesterday.get(marker_account, 0)
        )
        completed_km = completed_by_account.get(marker_account, 0) / 1000
        other_account = next(
            account for account in marker_names if account != marker_account
        )
        difference_km = (
            completed_by_account.get(marker_account, 0)
            - completed_by_account.get(other_account, 0)
        ) / 1000
        if abs(difference_km) < 0.05:
            comparison = "level with the other user"
        elif difference_km > 0:
            comparison = f"{difference_km:.1f} km ahead of {other_account.title()}"
        else:
            comparison = f"{abs(difference_km):.1f} km behind {other_account.title()}"
        tooltip = (
            f"{marker_account.title()}: {completed_km:.1f} km completed; "
            f"{comparison}"
        )
        current_icon = (
            "{icon: L.divIcon({className: 'runner-marker runner-marker-"
            + marker_account
            + "', html: '<span>"
            + marker_name
            + "</span>', iconSize: [28, 28], iconAnchor: [14, 14]})}"
        )
        shadow_icon = (
            "{icon: L.divIcon({className: 'runner-marker runner-marker-"
            + marker_account
            + " runner-marker-shadow', html: '<span>"
            + marker_name
            + "</span>', iconSize: [28, 28], iconAnchor: [14, 14]})}"
        )
        if marker_point:
            marker_scripts.append(
                f"const marker{marker_name.upper()} = L.marker({json.dumps(marker_point)}, "
                f"{current_icon}).addTo(map).bindTooltip({json.dumps(tooltip)});"
            )
        if yesterday_point:
            marker_scripts.append(
                f"L.marker({json.dumps(yesterday_point)}, "
                f"{shadow_icon}).addTo(map).bindTooltip({json.dumps(tooltip + ' (yesterday position)')});"
            )
    marker_script = "".join(marker_scripts)
    current_marker_name = marker_names.get(account_name, "m").upper()
    map_markup = ""
    if route_points:
        map_markup = (
            f'<div class="long-run-map-shell"><div class="long-run-map-actions">'
            f'<button type="button" data-map-action="current">Current location</button>'
            f'<button type="button" data-map-action="all">Zoom to all</button>'
            f'<button type="button" data-map-action="out">Zoom out</button></div>'
            f'<div class="long-run-map" id="{map_id}"></div>'
            '<div class="long-run-map-key"><span><i class="route-key"></i>Race route</span>'
            '<span><i class="runner-key runner-key-manga"></i>m</span>'
            '<span><i class="runner-key runner-key-chips"></i>c</span>'
            '<span><i class="runner-key runner-key-shadow"></i>Yesterday\'s location</span></div></div>'
            f"<script>(function() {{"
            f"const map = L.map('{map_id}', {{scrollWheelZoom: false}});"
            "const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', "
            "{attribution: '&copy; OpenStreetMap contributors'});"
            "const satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', "
            "{attribution: 'Tiles &copy; Esri'});"
            "osm.addTo(map);"
            f"const route = L.polyline({route_json}, {{color: '#ef6546', weight: 4, opacity: 0.9}}).addTo(map);"
            f"{marker_script}"
            "const runnerMarkers = [markerM, markerC];"
            "map.fitBounds(L.featureGroup(runnerMarkers).getBounds(), {padding: [48, 48], maxZoom: 15});"
            "L.control.layers({'OpenStreetMap': osm, 'Esri satellite': satellite}, {}).addTo(map);"
            f"const mapShell = document.getElementById('{map_id}').parentElement;"
            f"mapShell.querySelector('[data-map-action=current]').addEventListener('click', () => map.setView(marker{current_marker_name}.getLatLng(), 14));"
            "mapShell.querySelector('[data-map-action=all]').addEventListener('click', () => map.fitBounds(L.featureGroup(runnerMarkers).getBounds(), {padding: [48, 48], maxZoom: 15}));"
            "mapShell.querySelector('[data-map-action=out]').addEventListener('click', () => map.fitBounds(route.getBounds(), {padding: [18, 18]}));"
            f"document.getElementById('{map_id}')._leafletMap = map;"
            "})();</script>"
        )
    return (
        "<details class=\"long-run\" open><summary>The Long Run</summary>"
        "<div class=\"long-run-content\"><dl class=\"long-run-grid\">"
        f"<div><dt>Start date</dt><dd>{epoch.strftime('%a %d %b %Y')}</dd></div>"
        f"<div><dt>Total race distance</dt><dd>{total_race_distance / 1000:.1f} km</dd></div>"
        f"<div><dt>Km completed</dt><dd>{completed_value}</dd></div>"
        f"<div><dt>Days remaining</dt><dd>{days_remaining if days_remaining is not None else '-'} </dd></div>"
        f"<div><dt>Estimated day of arrival</dt><dd>{arrival_value}</dd></div>"
        f"</dl>{map_markup}</div>"
        "</details>"
    )


def week_label(day):
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    week_number = monday.isocalendar().week
    return f"Week {week_number} · {format_date(monday)} to {format_date(sunday)}"


def period_ranges(today):
    current_monday = today - timedelta(days=today.weekday())
    last_week_monday = current_monday - timedelta(days=7)
    return OrderedDict(
        [
            ("Yesterday", (today - timedelta(days=1), today - timedelta(days=1))),
            ("Last 3 days", (today - timedelta(days=3), today - timedelta(days=1))),
            ("Current week", (current_monday, today)),
            ("Last week", (last_week_monday, current_monday - timedelta(days=1))),
        ]
    )


def period_totals(rows, start_date, end_date):
    selected = [row for row in rows if start_date <= row[0] <= end_date]
    available_days = len(selected)
    return {
        "run_distance": sum(float(row[4] or 0) for row in selected),
        "run_time": sum(int(row[5] or 0) for row in selected),
        "total_distance": sum(float(row[1] or 0) for row in selected),
        "sedentary": sum(int(row[2] or 0) for row in selected),
        "days": available_days,
        "average_run_distance": (
            sum(float(row[4] or 0) for row in selected) / available_days
            if available_days else 0
        ),
        "average_run_time": (
            sum(int(row[5] or 0) for row in selected) / available_days
            if available_days else 0
        ),
        "average_sedentary": (
            sum(int(row[2] or 0) for row in selected) / available_days
            if available_days else 0
        ),
    }


def weekly_run_goal_markup(rows, today):
    week_start = today - timedelta(days=today.weekday())
    runs_completed = sum(
        int(row[3] or 0)
        for row in rows
        if week_start <= row[0] <= today
    )
    goal = 4
    progress = min(runs_completed / goal, 1)
    remaining = max(goal - runs_completed, 0)
    message = (
        "Goal complete - strong week!"
        if runs_completed >= goal
        else f"{remaining} more run{'s' if remaining != 1 else ''} to reach your goal"
    )
    return (
        '<article class="summary-card goal-card">'
        "<h3>Weekly run goal</h3>"
        f"<p class=\"goal-count\"><strong>{runs_completed}</strong> / {goal} runs</p>"
        f"<div class=\"goal-track\" role=\"progressbar\" aria-label=\"Weekly run goal\" "
        f"aria-valuenow=\"{min(runs_completed, goal)}\" aria-valuemin=\"0\" aria-valuemax=\"{goal}\">"
        f"<span style=\"width: {progress:.0%}\"></span></div>"
        f"<p class=\"goal-message\">{message}</p>"
        "</article>"
    )


def two_week_run_goal_markup(rows, today):
    week_start = today - timedelta(days=today.weekday())
    two_week_start = week_start - timedelta(days=7)
    runs_completed = sum(
        int(row[3] or 0)
        for row in rows
        if two_week_start <= row[0] <= today
    )
    goal = 7
    progress = min(runs_completed / goal, 1)
    remaining = max(goal - runs_completed, 0)
    message = (
        "Goal complete - strong fortnight!"
        if runs_completed >= goal
        else f"{remaining} more run{'s' if remaining != 1 else ''} to reach your goal"
    )
    return (
        '<article class="summary-card goal-card">'
        "<h3>Two week run goal</h3>"
        f"<p class=\"goal-count\"><strong>{runs_completed}</strong> / {goal} runs</p>"
        f"<div class=\"goal-track\" role=\"progressbar\" aria-label=\"Two week run goal\" "
        f"aria-valuenow=\"{min(runs_completed, goal)}\" aria-valuemin=\"0\" aria-valuemax=\"{goal}\">"
        f"<span style=\"width: {progress:.0%}\"></span></div>"
        f"<p class=\"goal-message\">{message}</p>"
        "</article>"
    )


def weekly_run_minutes_goal_markup(rows, today):
    week_start = today - timedelta(days=today.weekday())
    minutes_completed = int(
        (
            sum(
                float(row[5] or 0)
                for row in rows
                if week_start <= row[0] <= today
            )
            + 30
        )
        // 60
    )
    goal = 150
    progress = min(minutes_completed / goal, 1)
    remaining = max(goal - minutes_completed, 0)
    message = (
        "Goal complete - strong week!"
        if minutes_completed >= goal
        else f"{remaining} more minute{'s' if remaining != 1 else ''} to reach your goal"
    )
    return (
        '<article class="summary-card goal-card">'
        "<h3>Weekly run goal</h3>"
        f"<p class=\"goal-count\"><strong>{minutes_completed}</strong> / {goal} min</p>"
        f"<div class=\"goal-track\" role=\"progressbar\" aria-label=\"Weekly 150-minute run goal\" "
        f"aria-valuenow=\"{min(minutes_completed, goal)}\" aria-valuemin=\"0\" aria-valuemax=\"{goal}\">"
        f"<span style=\"width: {progress:.0%}\"></span></div>"
        f"<p class=\"goal-message\">{message}</p>"
        "</article>"
    )


def weekly_strength_goal_markup(rows, today):
    week_start = today - timedelta(days=today.weekday())
    strength_completed = sum(
        sum(
            1 for activity in row[6]
            if str(activity.get("sport", "")).lower()
            in {"strength", "strength_training"}
        )
        for row in rows
        if week_start <= row[0] <= today
    )
    goal = 2
    progress = min(strength_completed / goal, 1)
    remaining = max(goal - strength_completed, 0)
    message = (
        "Goal complete - strength built!"
        if strength_completed >= goal
        else f"{remaining} more strength session{'s' if remaining != 1 else ''} to reach your goal"
    )
    return (
        '<article class="summary-card goal-card strength-goal-card">'
        "<h3>Weekly strength goal</h3>"
        f"<p class=\"goal-count\"><strong>{strength_completed}</strong> / {goal} sessions</p>"
        f"<div class=\"goal-track\" role=\"progressbar\" aria-label=\"Weekly strength goal\" "
        f"aria-valuenow=\"{min(strength_completed, goal)}\" aria-valuemin=\"0\" aria-valuemax=\"{goal}\">"
        f"<span style=\"width: {progress:.0%}\"></span></div>"
        f"<p class=\"goal-message\">{message}</p>"
        "</article>"
    )


def goal_cards_markup(rows, today, account_name):
    card_builders = {
        "weekly_run_count": weekly_run_goal_markup,
        "two_week_run_count": two_week_run_goal_markup,
        "weekly_run_minutes": weekly_run_minutes_goal_markup,
        "weekly_strength": weekly_strength_goal_markup,
        "weekly_intensity": lambda card_rows, card_today: weekly_intensity_markup(
            card_rows, card_today, account_name
        ),
    }
    return [
        card_builders[view](rows, today)
        for view in GOAL_CARD_VIEWS.get(account_name, ())
    ]


def intensity_grid_rows(goal_minutes):
    """Rows top (apex) to bottom (base). A triangle subdivided into n equal
    rows always has n**2 small triangles, so n is the closest fit to the
    configured goal; row i (1-indexed from the apex) holds 2i-1 triangles.
    """
    row_count = max(1, round(math.sqrt(goal_minutes))) if goal_minutes else 1
    rows = []
    for index in range(1, row_count + 1):
        rows.append(
            {
                "y_top": (index - 1) / row_count * 100,
                "y_bottom": index / row_count * 100,
                "capacity": 2 * index - 1,
            }
        )
    return rows, row_count * row_count


def intensity_zigzag_lines(row_index, row_count):
    y_top = (row_index - 1) / row_count * 100
    y_bottom = row_index / row_count * 100
    half_top = y_top / 2
    half_bottom = y_bottom / 2
    bottom_points = [
        (50 - half_bottom + step * (2 * half_bottom / row_index), y_bottom)
        for step in range(row_index + 1)
    ]
    top_points = (
        [(50 - half_top + step * (2 * half_top / (row_index - 1)), y_top) for step in range(row_index)]
        if row_index > 1
        else [(50.0, y_top)]
    )
    lines = []
    for step, (tx, ty) in enumerate(top_points):
        bx1, by1 = bottom_points[step]
        bx2, by2 = bottom_points[step + 1]
        lines.append((tx, ty, bx1, by1))
        lines.append((tx, ty, bx2, by2))
    return lines


def allocate_intensity_stack(rows, stack_minutes):
    """Assign each stack zone's minutes to row capacity, one small triangle
    per minute, starting from rows[0] and consuming remaining capacity."""
    allocations = [[] for _ in rows]
    remaining_by_zone = list(stack_minutes)
    for row_position, row in enumerate(rows):
        capacity_left = row["capacity"]
        for zone_index, remaining in enumerate(remaining_by_zone):
            if capacity_left <= 0 or remaining <= 0:
                continue
            used = min(remaining, capacity_left)
            allocations[row_position].append((zone_index, used))
            remaining_by_zone[zone_index] -= used
            capacity_left -= used
    return allocations


def intensity_trapezoid_points(y_top, y_bottom):
    half_top = y_top / 2
    half_bottom = y_bottom / 2
    return (
        f"{50 - half_top:.2f},{y_top:.2f} {50 + half_top:.2f},{y_top:.2f} "
        f"{50 + half_bottom:.2f},{y_bottom:.2f} {50 - half_bottom:.2f},{y_bottom:.2f}"
    )


def weekly_intensity_markup(rows, today, account_name):
    week_start = today - timedelta(days=today.weekday())
    minutes_completed = int(
        (
            sum(
                float(row[5] or 0)
                for row in rows
                if week_start <= row[0] <= today
            )
            + 30
        )
        // 60
    )
    goal = INTENSITY_GOAL_MINUTES
    grid_rows, grid_total = intensity_grid_rows(goal)
    row_count = len(grid_rows)

    zone_minutes = weekly_zone_minutes(rows, week_start, today)
    easy_minutes = zone_minutes["easy"]
    tempo_minutes = zone_minutes["tempo"]
    threshold_minutes = zone_minutes["threshold"]
    above_threshold_minutes = zone_minutes["above"]

    easy_clamped = min(easy_minutes, grid_total)
    tempo_clamped = min(tempo_minutes, grid_total - easy_clamped)
    bottom_used = easy_clamped + tempo_clamped
    above_clamped = min(above_threshold_minutes, grid_total - bottom_used)
    threshold_clamped = min(threshold_minutes, grid_total - bottom_used - above_clamped)

    bottom_allocation = allocate_intensity_stack(
        list(reversed(grid_rows)), (easy_clamped, tempo_clamped)
    )
    bottom_allocation = list(reversed(bottom_allocation))
    top_allocation = allocate_intensity_stack(grid_rows, (above_clamped, threshold_clamped))

    clip_defs = []
    row_groups = []
    mesh_lines = []
    for position, row in enumerate(grid_rows):
        y_top, y_bottom = row["y_top"], row["y_bottom"]
        capacity = row["capacity"]
        clip_id = f"intensity-{account_name}-row-{position}"
        clip_defs.append(
            f'<clipPath id="{clip_id}"><polygon points="'
            f'{intensity_trapezoid_points(y_top, y_bottom)}"/></clipPath>'
        )
        fills = [f'<rect x="0" y="0" width="100" height="100" fill="#ffffff"></rect>']
        cursor_from_bottom = 0.0
        for zone_index, used in bottom_allocation[position]:
            fraction = used / capacity
            rect_bottom = y_bottom - cursor_from_bottom / capacity * (y_bottom - y_top)
            rect_top = rect_bottom - fraction * (y_bottom - y_top)
            color = INTENSITY_BOTTOM_STACK[zone_index][1]
            fills.append(
                f'<rect x="0" y="{rect_top:.2f}" width="100" height="{rect_bottom - rect_top:.2f}" fill="{color}"></rect>'
            )
            cursor_from_bottom += used
        cursor_from_top = 0.0
        for zone_index, used in top_allocation[position]:
            fraction = used / capacity
            rect_top = y_top + cursor_from_top / capacity * (y_bottom - y_top)
            rect_bottom = rect_top + fraction * (y_bottom - y_top)
            color = INTENSITY_TOP_STACK[zone_index][1]
            fills.append(
                f'<rect x="0" y="{rect_top:.2f}" width="100" height="{rect_bottom - rect_top:.2f}" fill="{color}"></rect>'
            )
            cursor_from_top += used
        row_groups.append(f'<g clip-path="url(#{clip_id})">{"".join(fills)}</g>')

        for x1, y1, x2, y2 in intensity_zigzag_lines(position + 1, row_count):
            mesh_lines.append(
                f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                'stroke="#171b19" stroke-width="0.5" stroke-opacity="0.55"></line>'
            )
        if position > 0:
            half = y_top / 2
            mesh_lines.append(
                f'<line x1="{50 - half:.2f}" y1="{y_top:.2f}" x2="{50 + half:.2f}" y2="{y_top:.2f}" '
                'stroke="#171b19" stroke-width="0.7"></line>'
            )

    outline = '<polygon points="50,0 100,100 0,100" fill="none" stroke="#171b19" stroke-width="2"></polygon>'
    svg = (
        '<svg class="intensity-pyramid" viewBox="0 0 100 100" role="img" '
        f'aria-label="Weekly intensity pyramid: {minutes_completed} of {goal} minutes">'
        f'<defs>{"".join(clip_defs)}</defs>'
        f'{"".join(row_groups)}{"".join(mesh_lines)}{outline}'
        "</svg>"
    )

    label_items = [
        f'<div class="intensity-label">'
        f'<span class="intensity-swatch" style="background: {color}"></span>{html.escape(label)}</div>'
        for label, color in (*INTENSITY_TOP_STACK, *reversed(INTENSITY_BOTTOM_STACK))
    ]

    remaining = max(goal - minutes_completed, 0)
    message = (
        "Goal complete - great intensity balance!"
        if minutes_completed >= goal
        else f"{remaining} more minute{'s' if remaining != 1 else ''} to fill this week's pyramid"
    )
    return (
        '<article class="summary-card intensity-card">'
        "<h3>This week's intensity</h3>"
        f'<p class="goal-count"><strong>{minutes_completed}</strong> / {goal} min</p>'
        '<div class="intensity-shell">'
        f"{svg}"
        f'<div class="intensity-labels">{"".join(label_items)}</div>'
        "</div>"
        f'<p class="goal-message">{message}</p>'
        "</article>"
    )


def strength_pyramid_markup(rows, today):
    session_dates = sorted(
        row[0]
        for row in rows
        for activity in row[6]
        if row[0] <= today
        and str(activity.get("sport", "")).lower()
        in {"strength", "strength_training"}
    )
    filled_segments = 0
    previous_session = None
    for session_day in session_dates:
        if previous_session is not None:
            gap_days = (session_day - previous_session).days
            if gap_days >= STRENGTH_PYRAMID_KEEP_DAYS:
                filled_segments = max(
                    filled_segments - gap_days // STRENGTH_PYRAMID_KEEP_DAYS, 0
                )
        filled_segments = min(filled_segments + 1, STRENGTH_PYRAMID_SEGMENTS)
        previous_session = session_day

    if previous_session is None:
        days_remaining = STRENGTH_PYRAMID_KEEP_DAYS
        due_date = today + timedelta(days=days_remaining)
    else:
        elapsed_days = (today - previous_session).days
        decay_count = elapsed_days // STRENGTH_PYRAMID_KEEP_DAYS
        filled_segments = max(filled_segments - decay_count, 0)
        days_remaining = STRENGTH_PYRAMID_KEEP_DAYS - (elapsed_days % STRENGTH_PYRAMID_KEEP_DAYS)
        due_date = today + timedelta(days=days_remaining)

    segments = []
    for position in range(STRENGTH_PYRAMID_SEGMENTS):
        segment_number = STRENGTH_PYRAMID_SEGMENTS - position
        filled = segment_number <= filled_segments
        width = (position + 1) / STRENGTH_PYRAMID_SEGMENTS * 100
        state = " filled" if filled else ""
        segments.append(
            f'<span class="pyramid-segment{state}" style="width: {width:.2f}%" '
            f'aria-label="Strength pyramid segment {segment_number}: '
            f'{"filled" if filled else "empty"}"></span>'
        )
    return (
        '<article class="summary-card pyramid-card">'
        "<h3>Strength progress pyramid</h3>"
        f'<div class="strength-pyramid" role="img" aria-label="{filled_segments} of {STRENGTH_PYRAMID_SEGMENTS} strength pyramid segments filled">'
        f"{''.join(segments)}</div>"
        f"<p class=\"pyramid-note\">Log a strength workout by {due_date.strftime('%a %d %b')} to keep all your strength.</p>"
        "</article>"
    )


def adaptive_trail_markup(rows, today):
    start_date = today - timedelta(days=TRAIL_DAYS - 1)
    row_by_date = {row[0]: row for row in rows}
    days = [start_date + timedelta(days=index) for index in range(TRAIL_DAYS)]
    sessions_by_day = {}
    for day in days:
        row = row_by_date.get(day)
        activities = row[6] if row else []
        run_count = int(row[3] or 0) if row else 0
        strength_count = sum(
            1 for activity in activities
            if str(activity.get("sport", "")).lower()
            in {"strength", "strength_training"}
        )
        sessions_by_day[day] = run_count + strength_count

    squares = []
    warning_count = 0
    healed_count = 0
    for day in days:
        row = row_by_date.get(day)
        activities = row[6] if row else []
        run_count = int(row[3] or 0) if row else 0
        strength_count = sum(
            1 for activity in activities
            if str(activity.get("sport", "")).lower()
            in {"strength", "strength_training"}
        )
        day_index = (day - TRAIL_EPOCH).days
        scheduled = (
            day_index >= 0
            and (day_index % RUN_CADENCE_DAYS == 0 or day_index % STRENGTH_CADENCE_DAYS == 0)
        )
        if run_count and strength_count:
            state, label = "bloom", "Run and strength session"
        elif run_count:
            state, label = "run", "Run session"
        elif strength_count:
            state, label = "strength", "Strength session"
        elif scheduled and day < today:
            later_sessions = sum(sessions_by_day[later_day] for later_day in days if later_day > day)
            if later_sessions >= HEALING_SESSION_COUNT:
                state, label = "healed", "Restored by later sessions"
                healed_count += 1
            else:
                state, label = "warning", "Waiting for a future session"
                warning_count += 1
        else:
            state, label = "rest", "Recovery day"
        squares.append(
            f'<span class="trail-seed trail-seed-{state}" title="{html.escape(format_date(day))}: {label}" '
            f'aria-label="{html.escape(format_date(day))}: {label}"></span>'
        )

    total_sessions = sum(sessions_by_day.values())
    if warning_count:
        message = "A few seeds are waiting for care. Your next sessions can restore them."
    elif healed_count:
        message = "Your follow-through restored earlier seeds. Momentum is growing."
    elif total_sessions:
        message = "Your trail is growing steadily. Keep nurturing the rhythm."
    else:
        message = "Every session plants a seed. Begin whenever you are ready."
    return (
        '<article class="summary-card trail-card">'
        "<h3>Progress trail</h3>"
        f"<p class=\"trail-message\">{message}</p>"
        f'<div class="trail-grid" role="img" aria-label="Training trail for the last {TRAIL_DAYS} days">'
        f"{''.join(squares)}</div>"
        '<div class="trail-key"><span><i class="trail-seed trail-seed-run"></i>Run</span>'
        '<span><i class="trail-seed trail-seed-strength"></i>Strength</span>'
        '<span><i class="trail-seed trail-seed-warning"></i>Needs care</span>'
        '<span><i class="trail-seed trail-seed-healed"></i>Restored</span></div>'
        "</article>"
    )
def fetch_rows(connection):
    query = """
        SELECT
            u.account_name,
            ds.summary_date,
            ds.total_distance_meters,
            ds.sedentary_seconds,
            COALESCE(dst.activity_count, 0) AS run_activities,
            COALESCE(dst.total_distance_meters, 0) AS run_distance_meters,
            COALESCE(dst.total_duration_seconds, 0) AS run_duration_seconds,
            COALESCE(activity_days.activities, '[]'::jsonb) AS activities,
            COALESCE(u.hr_zone_settings, '[]'::jsonb) AS hr_zone_settings,
            ds.resting_heart_rate
        FROM daily_summaries AS ds
        JOIN users AS u
            ON u.id = ds.user_id
        LEFT JOIN daily_sport_totals AS dst
            ON dst.user_id = ds.user_id
           AND dst.summary_date = ds.summary_date
           AND dst.sport_type_id = (
               SELECT id FROM sport_types WHERE name = 'running'
           )
        LEFT JOIN (
            SELECT
                user_id,
                start_time::date AS activity_date,
                jsonb_agg(
                    jsonb_build_object(
                        'name', COALESCE(activity_name, activity_type, 'Activity'),
                        'sport', COALESCE(activity_type, 'other'),
                        'distance_meters', COALESCE(distance_meters, 0),
                        'duration_seconds', COALESCE(
                            elapsed_duration_seconds,
                            moving_duration_seconds,
                            0
                        ),
                        'average_heart_rate', average_heart_rate,
                        'hr_zone_seconds', COALESCE(hr_zone_seconds, '{}'::jsonb),
                        'pace_by_hr_bucket', COALESCE(pace_by_hr_bucket, '{}'::jsonb)
                    ) ORDER BY start_time
                ) AS activities
            FROM activities
            GROUP BY user_id, start_time::date
        ) AS activity_days
            ON activity_days.user_id = ds.user_id
           AND activity_days.activity_date = ds.summary_date
        ORDER BY ds.summary_date DESC
    """
    with connection.cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchall()


def activity_markup(activities):
    if not activities:
        return "<span class=\"empty-day\">-</span>"
    items = []
    for activity in activities:
        name = html.escape(str(activity.get("name", "Activity")))
        distance = format_distance(activity.get("distance_meters", 0))
        duration = format_run_duration(activity.get("duration_seconds", 0))
        sport = str(activity.get("sport", "")).lower()
        is_strength = sport in {"strength", "strength_training"}
        activity_class = "activity activity-strength" if is_strength else "activity activity-running"
        details = "" if sport in {"strength", "strength_training"} else f"<small>{distance} km · {duration}</small>"
        strength_icon = ""
        zone_bar = ""
        if sport == "running":
            zone_seconds = custom_zone_seconds(activity.get("hr_zone_seconds"))
            total_zone_seconds = sum(zone_seconds.values())
            if total_zone_seconds:
                segments = []
                labels = []
                for key, label, color in RUN_ZONE_PRESENTATION:
                    seconds = zone_seconds[key]
                    if not seconds:
                        continue
                    percentage = seconds / total_zone_seconds * 100
                    segments.append(
                        f'<span class="zone-segment" style="background: {color}; width: {percentage:.2f}%" '
                        f'title="{label}: {format_run_duration(seconds)}"></span>'
                    )
                    labels.append(f"{label}: {format_run_duration(seconds)}")
                zone_bar = (
                    f'<span class="zone-bar" role="img" aria-label="Heart-rate zones: '
                    f'{html.escape("; ".join(labels))}">'
                    f'{"".join(segments)}</span>'
                )
            else:
                zone_bar = '<span class="zone-bar zone-bar-unavailable" title="Heart-rate zone data unavailable" aria-label="Heart-rate zone data unavailable"></span>'
        items.append(f"<span class=\"{activity_class}\">{strength_icon}<strong>{name}</strong>{zone_bar}{details}</span>")
    return "".join(items)


def week_calendar(rows, start_date, end_date):
    row_by_date = {row[0]: row for row in rows if start_date <= row[0] <= end_date}
    days = [start_date + timedelta(days=index) for index in range(7)]
    totals = period_totals(rows, start_date, end_date)
    day_cards = []
    running_activity_count = 0
    strength_activity_count = 0
    for day in days:
        row = row_by_date.get(day)
        activities = [
            activity for activity in (row[6] if row else [])
            if str(activity.get("sport", "")).lower() in {"running", "strength", "strength_training"}
        ]
        running_activity_count += sum(
            1 for activity in activities
            if str(activity.get("sport", "")).lower() == "running"
        )
        strength_activity_count += sum(
            1 for activity in activities
            if str(activity.get("sport", "")).lower() in {"strength", "strength_training"}
        )
        day_cards.append(
            f"<article class=\"calendar-day\"><h4>{html.escape(day.strftime('%a'))}<br>{html.escape(day.strftime('%d %b'))}</h4>"
            f"<div class=\"calendar-activities\">{activity_markup(activities)}</div></article>"
        )
    week_total = (
        f"<strong>{running_activity_count} runs · {strength_activity_count} strength</strong>"
        f"<small>Run distance: {format_distance(totals['run_distance'])} km</small>"
        f"<small>Run time: {format_run_duration(totals['run_time'])}</small>"
        f"<small>Total distance: {format_distance(totals['total_distance'])} km</small>"
        # f"<small>Sedentary: {format_duration(totals['sedentary'])}</small>"
    )
    return (
        f"<div class=\"calendar-grid\">{''.join(day_cards)}"
        f"<aside class=\"calendar-total\"><span>Week totals</span>{week_total}</aside></div>"
    )


def _render_single_user(
    rows,
    account_name,
    zone_settings,
    completed_by_account,
    completed_by_account_yesterday,
    generated_at,
    annual_points=None,
    lagged_annual_points=None,
    efficiency_stats=None,
):
    today = generated_at.date()
    current_monday = today - timedelta(days=today.weekday())
    last_week_monday = current_monday - timedelta(days=7)
    summary_cards = []
    for label, (start_date, end_date) in period_ranges(today).items():
        if label in {"Yesterday", "Last 3 days", "Current week", "Last week"}:
            continue
        totals = period_totals(rows, start_date, end_date)
        summary_cards.append(
            "<article class=\"summary-card\">"
            f"<h3>{html.escape(label)}</h3>"
            f"<p class=\"summary-period\">{format_date(start_date)}"
            f"{f' to {format_date(end_date)}' if start_date != end_date else ''}</p>"
            "<dl>"
            f"<div><dt>Total distance</dt><dd>{format_distance(totals['total_distance'])} km</dd></div>"
            # f"<div><dt>Avg sedentary/day</dt><dd>{format_duration(totals['average_sedentary'])}</dd></div>"
            "</dl></article>"
        )
    summary_cards.extend(goal_cards_markup(rows, today, account_name))
    summary_cards.append(recent_intensity_markup(rows, today))
    summary_cards.append(recent_cycling_markup(rows, today))
    summary_cards.append(run_scatter_markup(rows, today))
    if annual_points is None:
        summary_cards.append(pace_by_hr_markup(rows, today, zone_settings))
    if lagged_annual_points is not None:
        summary_cards.append(
            pace_by_hr_markup(
                rows,
                today,
                zone_settings,
                lagged_annual_points,
                title="Pace vs Heart Rate",
            )
        )
    if annual_points is not None:
        summary_cards.append(annual_summary_markup(ROOT / "local_data", rows, account_name))
    if efficiency_stats is not None:
        summary_cards.append(efficiency_summary_markup(efficiency_stats))
    summary_cards.append(zone_legend_markup(zone_settings))
    summary_cards.append(strength_pyramid_markup(rows, today))

    grouped = OrderedDict()
    for row in rows:
        monday = row[0] - timedelta(days=row[0].weekday())
        grouped.setdefault(monday, []).append(row)
    grouped.pop(current_monday, None)

    sections = []
    for monday in list(grouped)[:MAX_HISTORY_WEEKS]:
        label = week_label(monday)
        week_end = monday + timedelta(days=6)
        totals = period_totals(rows, monday, week_end)
        summary_totals = (
            f"<span class=\"week-history-totals\">"
            f"{format_distance(totals['run_distance'])} km · "
            f"{format_run_duration(totals['run_time'])}</span>"
        )
        sections.append(
            f"<details class=\"week-group\"><summary>{html.escape(label)}{summary_totals}</summary>"
            f"{week_calendar(rows, monday, week_end)}</details>"
        )

    calendar_section = (
        "<section class=\"calendar-section\"><div class=\"section-heading\">"
        "<div><p class=\"eyebrow\">Calendar view</p><h2>Current week</h2></div></div>"
        f"<article class=\"calendar-card\"><h3>Week {current_monday.isocalendar().week} · Current week</h3>"
        f"{week_calendar(rows, current_monday, current_monday + timedelta(days=6))}</article>"
        f"{long_run_markup(rows, today, account_name, completed_by_account, completed_by_account_yesterday)}</section>"
    )

    latest_data_date = max((row[0] for row in rows), default=None)
    generated_time = generated_at.strftime("%a %d %b %Y %H:%M %Z")
    generated = (
        f"No successful Garmin data fetch recorded · Updated {generated_time}"
        if latest_data_date is None
        else f"Data updated {generated_time}"
    )
    summary = "<section class=\"summary-section\"><div class=\"section-heading\">"
    summary += "<p class=\"eyebrow\">At a glance</p><h2>Training totals</h2>"
    summary += "</div><div class=\"summary-grid\">" + "".join(summary_cards) + "</div></section>"
    content = "\n".join(sections) or "<p>No Garmin data has been synced yet.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <title>Training Log</title>
  <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');

        :root {{
            color-scheme: light;
            --ink: #171b19;
            --muted: #68706b;
            --line: #cfd1c6;
            --paper: #f1eee5;
            --card: #fbfaf5;
            --accent: #ef6546;
            --accent-soft: #f7d7ca;
            --teal: #1b6c68;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            background: var(--ink);
            color: var(--ink);
            font-family: "DM Sans", sans-serif;
            line-height: 1.45;
            margin: 0;
        }}
        .page {{ background: var(--paper); max-width: 1180px; margin: 0 auto; min-height: 100vh; padding: clamp(1.5rem, 4vw, 4rem) clamp(1rem, 4vw, 2.5rem); }}
        header {{ border-bottom: 2px solid var(--ink); margin-bottom: 2.8rem; padding-bottom: 2rem; position: relative; }}
        .eyebrow {{ color: var(--accent); font: 700 0.72rem/1.2 Arial, sans-serif; letter-spacing: 0.16em; margin: 0 0 0.85rem; text-transform: uppercase; }}
        h1 {{ font-family: "Space Grotesk", sans-serif; font-size: clamp(2rem, 5vw, 4rem); font-weight: 600; letter-spacing: -0.055em; line-height: 0.92; margin: 0; max-width: 12ch; }}
        h2 {{ font-family: "Space Grotesk", sans-serif; font-size: clamp(1.5rem, 3vw, 2.2rem); font-weight: 600; margin: 0; }}
        .summary-section h2 {{ font-size: clamp(1.15rem, 2vw, 1.6rem); }}
        h3 {{ font-family: "Space Grotesk", sans-serif; font-size: 1.2rem; font-weight: 600; margin: 0; }}
        .updated {{ color: var(--muted); font: 0.72rem Arial, sans-serif; letter-spacing: 0.04em; margin: 1.25rem 0 0; text-transform: uppercase; }}
        .account-switch {{ align-items: center; border-bottom: 1px solid var(--line); display: flex; gap: 0.5rem; margin-bottom: 1.5rem; padding-bottom: 1rem; }}
        .account-switch-label {{ color: var(--muted); font: 700 0.68rem Arial, sans-serif; letter-spacing: 0.08em; margin-right: 0.35rem; text-transform: uppercase; }}
        .account-button {{ background: transparent; border: 1px solid var(--line); color: var(--muted); cursor: pointer; font: 600 0.78rem "DM Sans", sans-serif; padding: 0.45rem 0.8rem; text-transform: capitalize; }}
        .account-button.active, .account-button:hover {{ background: var(--ink); border-color: var(--ink); color: var(--paper); }}
        .user-panel.hidden {{ display: none; }}
        section {{ margin: 2.8rem 0; }}
        .section-heading {{ align-items: end; display: flex; justify-content: space-between; margin-bottom: 1rem; }}
        .summary-grid {{ display: grid; gap: 1rem; grid-template-columns: repeat(4, minmax(0, 1fr)); }}
        .summary-card {{ background: var(--card); border: 1px solid var(--ink); border-top: 5px solid var(--teal); padding: 1.15rem; }}
        .summary-card:nth-child(2) {{ border-top-color: var(--accent); }}
        .summary-card:nth-child(3) {{ border-top-color: #d28a32; }}
        .summary-card:nth-child(4) {{ border-top-color: #755d8a; }}
        .goal-card {{ border-top-color: var(--teal) !important; }}
        .strength-goal-card {{ border-top-color: #c8872d !important; }}
        .pyramid-card {{ border-top-color: var(--ink) !important; }}
        .strength-pyramid {{ align-items: center; display: flex; flex-direction: column; gap: 0.12rem; margin: 0.8rem auto; width: min(100%, 12rem); }}
        .pyramid-segment {{ background: #fff; border: 1px solid var(--ink); display: block; height: 0.42rem; }}
        .pyramid-segment.filled {{ background: #c8872d; }}
        .pyramid-note {{ color: var(--muted); font-size: 0.78rem; line-height: 1.35; margin: 0.7rem 0 0; }}
        .goal-count {{ font-size: 1.15rem; margin: 1rem 0 0.7rem; }}
        .goal-count strong {{ font-size: 1.8rem; }}
        .goal-track {{ background: var(--line); height: 0.55rem; overflow: hidden; }}
        .goal-track span {{ background: var(--teal); display: block; height: 100%; }}
        .goal-message {{ color: var(--muted); font-size: 0.78rem; margin: 0.7rem 0 0; }}
        .intensity-card {{ border-top-color: #a53f30 !important; grid-column: span 2; }}
        .intensity-shell {{ align-items: stretch; display: flex; gap: 1rem; margin-top: 0.6rem; }}
        .intensity-pyramid {{ flex: 0 0 auto; height: 9rem; width: 9rem; }}
        .intensity-labels {{ display: flex; flex: 1; flex-direction: column; gap: 0.5rem; justify-content: center; }}
        .intensity-label {{ align-items: center; display: flex; font: 600 0.72rem "DM Sans", sans-serif; gap: 0.4rem; }}
        .intensity-swatch {{ border: 1px solid var(--ink); display: inline-block; flex: 0 0 auto; height: 0.6rem; width: 0.6rem; }}
        .recent-intensity-card {{ grid-column: span 2; }}
        .recent-intensity-list {{ display: grid; gap: 0.6rem; margin-top: 0.8rem; }}
        .recent-intensity-week {{ align-items: center; display: grid; gap: 0.7rem; grid-template-columns: 4.2rem minmax(0, 1fr) auto; }}
        .recent-intensity-week strong {{ font: 600 0.72rem "Space Grotesk", sans-serif; white-space: nowrap; }}
        .recent-intensity-total {{ color: var(--muted); font-size: 0.68rem; white-space: nowrap; }}
        .zone-legend-card {{ grid-column: span 1; padding: 0.85rem; }}
        .zone-legend-list {{ display: grid; gap: 0.28rem; margin-top: 0.55rem; }}
        .zone-legend-row {{ align-items: center; display: grid; gap: 0.35rem; grid-template-columns: 0.55rem 4.4rem minmax(0, 1fr); }}
        .zone-legend-row strong {{ font: 600 0.68rem "Space Grotesk", sans-serif; }}
        .zone-legend-row small {{ color: var(--muted); font-size: 0.64rem; }}
        .run-scatter-card {{ grid-column: span 2; }}
        .pace-hr-card {{ grid-column: 1 / -1; }}
        .lagged-pace-hr-card .run-scatter-shell {{ display: block; }}
        .lagged-pace-hr-card .run-scatter {{ display: block; max-width: 38rem; width: 100%; }}
        .lagged-pace-hr-card .pace-hr-legend {{ font-size: 0.82rem; gap: 0.9rem 1.2rem; margin-top: 0.8rem; }}
        .lagged-pace-hr-card .pace-hr-legend label {{ cursor: pointer; min-height: 1.6rem; }}
        .lagged-pace-hr-card .pace-hr-legend input {{ height: 1rem; width: 1rem; }}
        .chart-touch-tooltip {{ background: var(--ink); border: 1px solid var(--card); border-radius: 3px; color: var(--card); display: none; font-size: 0.72rem; max-width: min(18rem, 80vw); padding: 0.45rem 0.6rem; position: fixed; z-index: 20; }}
        .pace-hr-legend {{ display: flex; flex-wrap: wrap; gap: 0.7rem; margin-top: 0.55rem; }}
        .pace-hr-legend label {{ align-items: center; display: inline-flex; font-size: 0.68rem; gap: 0.3rem; }}
        .pace-hr-legend input {{ accent-color: var(--teal); margin: 0; }}
        .pace-hr-legend i {{ border: 1px solid var(--ink); display: inline-block; height: 0.55rem; width: 0.55rem; }}
        .local-efficiency-card {{ grid-column: 1 / -1; }}
        .local-annual-card {{ grid-column: 1 / -1; }}
        .annual-table-wrap {{ margin-top: 0.7rem; }}
        .annual-table {{ min-width: 50rem; }}
        .annual-table th:first-child, .annual-table td:first-child {{ min-width: 12rem; }}
        .efficiency-columns {{ display: grid; gap: 1.2rem; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 0.8rem; }}
        .efficiency-columns ul {{ list-style: none; margin: 0.35rem 0 0; padding: 0; }}
        .efficiency-columns li {{ font-size: 0.78rem; margin: 0.18rem 0; }}
        .run-scatter-shell {{ align-items: center; display: flex; gap: 0.8rem; margin-top: 0.7rem; }}
        .run-scatter {{ flex: 1 1 auto; min-width: 0; }}
        .run-scatter text {{ fill: var(--muted); font: 0.62rem "DM Sans", sans-serif; }}
        .longest-run-note {{ border-left: 2px solid var(--teal); display: grid; gap: 0.15rem; padding-left: 0.7rem; white-space: nowrap; }}
        .longest-run-note strong {{ color: var(--teal); font: 600 0.72rem "Space Grotesk", sans-serif; }}
        .longest-run-note span {{ font: 700 1.1rem "Space Grotesk", sans-serif; }}
        .trail-card {{ border-top-color: #517d43 !important; grid-column: span 2; }}
        .trail-message {{ color: var(--muted); font-size: 0.82rem; margin: 0.7rem 0; min-height: 2.4em; }}
        .trail-grid {{ display: grid; gap: 0.08rem; grid-template-columns: repeat(42, minmax(0, 1fr)); }}
        .trail-seed {{ aspect-ratio: 1; background: #d9d8cf; display: inline-block; min-height: 0.7rem; }}
        .trail-seed-run {{ background: #517d43; }}
        .trail-seed-strength {{ background: #c8872d; }}
        .trail-seed-bloom {{ background: #1b6c68; box-shadow: inset 0 0 0 2px #d5ece8; }}
        .trail-seed-warning {{ background: #ef8b47; }}
        .trail-seed-healed {{ background: #8ab66b; box-shadow: inset 0 0 0 2px #e5f0dc; }}
        .trail-key {{ display: flex; flex-wrap: wrap; font: 0.68rem Arial, sans-serif; gap: 0.55rem; margin-top: 0.7rem; }}
        .trail-key span {{ align-items: center; display: inline-flex; gap: 0.25rem; }}
        .trail-key .trail-seed {{ height: 0.65rem; min-height: 0; width: 0.65rem; }}
        .summary-period {{ color: var(--muted); font: 0.68rem Arial, sans-serif; margin: 0.35rem 0 1rem; }}
        dl {{ margin: 0; }}
        dl div {{ border-top: 1px solid var(--line); display: flex; gap: 0.5rem; justify-content: space-between; padding: 0.55rem 0; }}
        dt {{ color: var(--muted); font: 0.78rem Arial, sans-serif; }}
        dd {{ font-size: 1rem; margin: 0; text-align: right; }}
        .week-section {{ background: var(--card); border: 1px solid var(--line); border-left: 5px solid var(--accent); padding: 1rem; }}
        .week-group {{ background: var(--card); border: 1px solid var(--line); margin: 0.75rem 0; }}
        .week-group summary {{ cursor: pointer; font-family: "Space Grotesk", sans-serif; font-size: 1.15rem; font-weight: 600; list-style-position: inside; padding: 1rem; }}
        .week-history-totals {{ color: var(--muted); float: right; font: 0.72rem "DM Sans", sans-serif; margin: 0.25rem 0.3rem 0 0; }}
        .week-group[open] summary {{ border-bottom: 1px solid var(--line); }}
        .week-group .calendar-grid {{ padding: 1rem; }}
        .calendar-card {{ background: var(--card); border: 1px solid var(--line); margin: 1rem 0; padding: 1rem; }}
        .calendar-card h3 {{ border-bottom: 1px solid var(--line); padding-bottom: 0.8rem; }}
        .long-run {{ background: var(--card); border: 1px solid var(--accent); margin: 1rem 0; padding: 0 1rem; }}
        .long-run summary {{ cursor: pointer; font-family: "Space Grotesk", sans-serif; font-size: 1.15rem; font-weight: 600; list-style-position: inside; padding: 1rem 0; }}
        .long-run[open] summary {{ border-bottom: 1px solid var(--line); }}
        .long-run-content {{ display: grid; gap: 1rem; grid-template-columns: minmax(12rem, 1fr) minmax(0, 3fr); margin: 0 0 1rem; }}
        .long-run-grid {{ background: #dcebef; border: 1px solid #4c8791; display: grid; gap: 0; grid-template-columns: 1fr; margin: 0; padding: 0.45rem; }}
        .long-run-grid div {{ align-items: baseline; border-top: 1px solid rgba(76, 135, 145, 0.4); display: flex; gap: 0.5rem; justify-content: space-between; padding: 0.55rem 0.7rem; }}
        .long-run-grid div:first-child {{ border-top: 0; }}
        .long-run-grid dt {{ color: var(--muted); font: 600 0.72rem/1.2 "DM Sans", sans-serif; letter-spacing: 0.04em; text-align: left; text-transform: uppercase; }}
        .long-run-grid dd {{ font: 600 0.9rem/1.2 "Space Grotesk", sans-serif; margin: 0; text-align: right; }}
        .long-run-map-shell {{ border-left: 1px solid var(--line); padding-left: 1rem; position: relative; }}
        .long-run-map-actions {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.6rem; }}
        .long-run-map-actions button {{ background: var(--paper); border: 1px solid var(--ink); color: var(--ink); cursor: pointer; font: 600 0.68rem "DM Sans", sans-serif; padding: 0.35rem 0.55rem; }}
        .long-run-map-actions button:hover {{ background: var(--ink); color: var(--paper); }}
        .long-run-map {{ height: 19rem; min-height: 19rem; width: 100%; }}
        .long-run-map-key {{ background: rgba(251, 250, 245, 0.94); border: 1px solid var(--line); bottom: 1.5rem; display: flex; flex-wrap: wrap; font: 0.68rem Arial, sans-serif; gap: 0.75rem; padding: 0.45rem 0.6rem; position: absolute; right: 1.5rem; z-index: 500; }}
        .long-run-map-key span {{ align-items: center; display: inline-flex; gap: 0.3rem; }}
        .long-run-map-key i {{ display: inline-block; height: 0.55rem; width: 1rem; }}
        .route-key {{ background: var(--accent); height: 0.2rem !important; }}
        .runner-key {{ background: var(--accent); border: 2px solid var(--ink); border-radius: 50%; height: 0.6rem !important; width: 0.6rem !important; }}
        .runner-marker {{ align-items: center; border: 2px solid var(--ink); border-radius: 50%; color: var(--paper); display: flex !important; font: 700 0.72rem/1 "Space Grotesk", sans-serif; height: 28px !important; justify-content: center; width: 28px !important; }}
        .runner-marker-manga {{ background: var(--teal); }}
        .runner-marker-chips {{ background: #3d6fb6; }}
        .runner-marker-shadow {{ opacity: 0.42; }}
        .runner-key-manga {{ background: var(--teal); }}
        .runner-key-chips {{ background: #3d6fb6; }}
        .runner-key-shadow {{ background: #68706b; opacity: 0.55; }}
        .leaflet-control-layers {{ border: 1px solid var(--ink) !important; border-radius: 0 !important; font: 0.72rem Arial, sans-serif; }}
        .calendar-grid {{ display: grid; gap: 0.6rem; grid-template-columns: repeat(7, minmax(0, 1fr)); }}
        .calendar-day {{ background: var(--card); border: 1px solid var(--line); min-height: 8.5rem; padding: 0.6rem; }}
        .calendar-day h4 {{ border-bottom: 1px solid var(--line); font: 600 0.8rem "Space Grotesk", sans-serif; margin: 0 0 0.6rem; padding-bottom: 0.45rem; }}
        .calendar-activities {{ min-height: 5rem; text-align: left; }}
        .activity {{ background: #e3eee8; border-left: 3px solid var(--teal); display: block; margin: 0 0 0.4rem; padding: 0.35rem; }}
        .activity-strength {{ background: #f7e5c8; border-left-color: #c8872d; }}
        .activity strong {{ display: block; font-size: 0.78rem; overflow-wrap: anywhere; }}
        .zone-bar {{ background: var(--line); display: flex; height: 0.42rem; margin: 0.35rem 0 0.2rem; overflow: hidden; position: relative; width: 100%; }}
        .zone-segment {{ display: block; height: 100%; min-width: 1px; }}
        .zone-hour-lines {{ background-image: repeating-linear-gradient(to right, transparent 0, transparent calc(100% - 1px), rgba(23, 27, 25, 0.55) 100%); inset: 0; pointer-events: none; position: absolute; }}
        .zone-bar-unavailable {{ background: repeating-linear-gradient(135deg, #d9d8cf 0, #d9d8cf 3px, #c1c2ba 3px, #c1c2ba 6px); }}
        .activity small {{ color: var(--muted); display: block; font-size: 0.68rem; margin-top: 0.15rem; }}
        .calendar-total {{ align-items: center; background: var(--accent-soft); border: 1px solid var(--accent); display: flex; gap: 0.75rem; grid-column: 1 / -1; justify-content: space-between; padding: 0.8rem 1rem; }}
        .calendar-total > span {{ font: 700 0.72rem Arial, sans-serif; letter-spacing: 0.08em; text-transform: uppercase; }}
        .calendar-total strong, .calendar-total small {{ display: block; }}
        .calendar-total small {{ display: inline-block; font-size: 0.78rem; font-weight: 400; margin-left: 0.75rem; }}
        .empty-day {{ color: var(--muted); }}
        table {{ border-collapse: collapse; min-width: 720px; width: 100%; }}
        .table-wrap {{ overflow-x: auto; }}
        th, td {{ border-bottom: 1px solid var(--line); padding: 0.75rem 0.65rem; text-align: right; white-space: nowrap; }}
        th:first-child, td:first-child {{ text-align: left; }}
        th {{ background: var(--accent-soft); color: var(--ink); font: 700 0.68rem Arial, sans-serif; letter-spacing: 0.06em; text-transform: uppercase; }}
        td {{ font-size: 0.95rem; }}
        tbody tr:hover {{ background: #fffaf5; }}
        @media (max-width: 900px) {{ .summary-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
        @media (max-width: 520px) {{
            .page {{ padding-top: 1.5rem; }}
            .summary-grid {{ grid-template-columns: 1fr; }}
            .section-heading {{ align-items: start; flex-direction: column; gap: 0.35rem; }}
            .summary-card {{ padding: 1rem; }}
            .intensity-card {{ grid-column: auto; }}
            .recent-intensity-card {{ grid-column: auto; }}
            .zone-legend-card {{ grid-column: auto; padding: 0.85rem; }}
            .run-scatter-card {{ grid-column: auto; }}
            .pace-hr-card {{ grid-column: 1 / -1; }}
            .lagged-pace-hr-card .run-scatter {{ max-width: 100%; }}
            .local-efficiency-card {{ grid-column: 1 / -1; }}
            .local-annual-card {{ grid-column: 1 / -1; padding: 0.8rem; }}
            .annual-table {{ min-width: 42rem; }}
            .annual-table th, .annual-table td {{ padding: 0.55rem 0.45rem; }}
            .account-switch {{ align-items: stretch; flex-wrap: wrap; }}
            .account-switch-label {{ flex-basis: 100%; margin-right: 0; }}
            .account-button {{ flex: 1 1 8rem; }}
            .pace-hr-legend {{ gap: 0.45rem 0.8rem; }}
            .efficiency-columns {{ grid-template-columns: 1fr; }}
            .run-scatter-shell {{ align-items: stretch; flex-direction: column; }}
            .longest-run-note {{ border-left: 0; border-top: 2px solid var(--teal); padding: 0.5rem 0 0; }}
            .intensity-pyramid {{ height: 7rem; width: 7rem; }}
            .trail-card {{ grid-column: auto; }}
            .trail-grid {{ gap: 0.06rem; }}
            .week-section {{ margin-left: -0.25rem; margin-right: -0.25rem; padding: 0.75rem; }}
            .week-history-totals {{ float: none; margin-left: 0.35rem; }}
            .long-run-content {{ grid-template-columns: 1fr; }}
            .long-run-map-shell {{ border-left: 0; border-top: 1px solid var(--line); padding: 1rem 0 0; }}
            .long-run-map {{ height: 15rem; min-height: 15rem; }}
            .calendar-grid {{ grid-template-columns: repeat(7, minmax(0, 1fr)); }}
            .calendar-day {{ min-height: 6rem; padding: 0.3rem; }}
            .calendar-day h4 {{ font-size: 0.58rem; margin-bottom: 0.35rem; overflow: hidden; padding-bottom: 0.3rem; text-overflow: ellipsis; white-space: nowrap; }}
            .calendar-activities {{ min-height: 3.5rem; }}
            .calendar-activities .activity {{ margin-bottom: 0.25rem; padding: 0.25rem 0.15rem; text-align: center; }}
            .calendar-activities .activity strong {{ display: none; }}
            .calendar-activities .activity small {{ font-size: 0.52rem; line-height: 1.15; margin-top: 0; overflow-wrap: anywhere; }}
            .calendar-activities .zone-bar {{ height: 0.28rem; margin: 0.2rem 0 0.15rem; }}
            .calendar-total {{ align-items: flex-start; flex-direction: column; gap: 0.35rem; }}
            .calendar-total small {{ margin-left: 0.4rem; }}
        }}
  </style>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>
    <main class="page">
        <header>
            <p class="eyebrow">Training Log · {html.escape(account_name)}</p>
            <h1>Training Log</h1>
            <p class="updated">{html.escape(generated)}</p>
        </header>
        {summary}
        {calendar_section}
        <section>
            <div class="section-heading"><div><p class="eyebrow">Daily detail</p><h2>Activity history by week</h2></div></div>
            {content}
        </section>
    </main>
</body>
</html>
"""


def render(rows, generated_at=None, local_raw_root=None):
    generated_at = generated_at or datetime.now(REPORT_TIMEZONE)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=REPORT_TIMEZONE)
    else:
        generated_at = generated_at.astimezone(REPORT_TIMEZONE)
        rows_by_account = OrderedDict((account, []) for account in ACCOUNT_NAMES)
        zone_settings_by_account = {}
        for row in rows:
            account_name = row[0] or "manga"
            rows_by_account.setdefault(account_name, []).append(row[1:8] + (row[9],))
            zone_settings_by_account.setdefault(
                account_name, row[8] if len(row) > 8 else []
            )

        epoch = LONG_RUN_EPOCH
        today = generated_at.date()
        completed_by_account = {
            account_name: sum(
                float(row[1] or 0)
                for row in account_rows
                if epoch <= row[0] <= today
            )
            for account_name, account_rows in rows_by_account.items()
        }
        yesterday = today - timedelta(days=1)
        completed_by_account_yesterday = {
            account_name: sum(
                float(row[1] or 0)
                for row in account_rows
                if epoch <= row[0] <= yesterday
            )
            for account_name, account_rows in rows_by_account.items()
        }

        local_points = (
            local_annual_pace_points(local_raw_root)
            if local_raw_root is not None
            else {}
        )
        local_lagged_points = (
            local_annual_pace_points(local_raw_root, lag_seconds=10)
            if local_raw_root is not None
            else {}
        )
        documents = [
            _render_single_user(
                account_rows,
                account_name,
                zone_settings_by_account.get(account_name, []),
                completed_by_account,
                completed_by_account_yesterday,
                generated_at,
                local_points.get(account_name),
                local_lagged_points.get(account_name),
                None,
            )
                for account_name, account_rows in rows_by_account.items()
        ]
        first_document = documents[0]
        head, _body = first_document.split("<body>", 1)
        _body_content, tail = first_document.split("</body>", 1)
        panels = []
        for index, (account_name, document) in enumerate(zip(rows_by_account, documents)):
                panel_content = document.split('<main class="page">', 1)[1].split("</main>", 1)[0]
                hidden = "" if index == 0 else " hidden"
                panels.append(
                        f'<section class="user-panel{hidden}" data-account="{html.escape(account_name)}">'
                        f"{panel_content}</section>"
                )
        buttons = "".join(
                f'<button class="account-button{" active" if index == 0 else ""}" '
                f'data-account="{html.escape(account_name)}" aria-pressed="{"true" if index == 0 else "false"}">{html.escape(account_name)}</button>'
                for index, account_name in enumerate(rows_by_account)
        )
        switcher = (
                '<nav class="account-switch" aria-label="Select training account">'
                '<span class="account-switch-label">View data for</span>'
                f"{buttons}</nav>"
        )
        script = """
<script>
    const chartTouchTooltip = document.createElement('div');
    chartTouchTooltip.className = 'chart-touch-tooltip';
    chartTouchTooltip.setAttribute('role', 'status');
    document.body.appendChild(chartTouchTooltip);

    function hideChartTouchTooltip() {
        chartTouchTooltip.style.display = 'none';
    }

    document.addEventListener('pointerdown', (event) => {
        const point = event.target.closest?.('.run-scatter circle');
        if (!point) {
            hideChartTouchTooltip();
            return;
        }
        const title = point.querySelector('title');
        if (!title) return;
        chartTouchTooltip.textContent = title.textContent;
        chartTouchTooltip.style.display = 'block';
        const pointRect = point.getBoundingClientRect();
        const tooltipRect = chartTouchTooltip.getBoundingClientRect();
        chartTouchTooltip.style.left = `${Math.max(8, Math.min(
            window.innerWidth - tooltipRect.width - 8,
            pointRect.left + pointRect.width / 2 - tooltipRect.width / 2
        ))}px`;
        chartTouchTooltip.style.top = `${Math.max(8, pointRect.top - tooltipRect.height - 8)}px`;
        event.stopPropagation();
    });

    document.addEventListener('change', (event) => {
        const toggle = event.target.closest?.('[data-series-toggle]');
        if (!toggle) return;
        const card = toggle.closest('.pace-hr-card');
        if (!card) return;
        const series = toggle.dataset.seriesToggle;
        card.querySelectorAll(`[data-series="${series}"]`).forEach((point) => {
            point.style.display = toggle.checked ? '' : 'none';
        });
    });

    document.querySelectorAll('.account-button').forEach((button) => {
        button.addEventListener('click', () => {
            const account = button.dataset.account;
            document.querySelectorAll('.account-button').forEach((item) => {
                const active = item === button;
                item.classList.toggle('active', active);
                item.setAttribute('aria-pressed', active ? 'true' : 'false');
            });
            document.querySelectorAll('.user-panel').forEach((panel) => {
                panel.classList.toggle('hidden', panel.dataset.account !== account);
                if (panel.dataset.account === account) {
                    panel.querySelectorAll('.long-run-map').forEach((element) => {
                        if (element._leafletMap) element._leafletMap.invalidateSize();
                    });
                }
            });
        });
    });
</script>
"""
        return (
                head
                + "<body><main class=\"page\">"
                + switcher
                + "".join(panels)
                + "</main>"
                + script
                + "</body>"
                + tail
        )


def main():
    parser = argparse.ArgumentParser(description="Build the Garmin report from Supabase.")
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        help="Write the generated report to this path instead of site/index.html.",
    )
    parser.add_argument(
        "--local-raw",
        action="store_true",
        help="Use local_data raw detail files for annual pace data.",
    )
    args = parser.parse_args()
    load_env_file()
    database_url = os.getenv("SUPABASE_DB_URL")
    if not database_url:
        raise RuntimeError("SUPABASE_DB_URL is not configured")
    connection = psycopg2.connect(database_url, sslmode="require")
    try:
        ensure_schema(connection)
        report = render(
            fetch_rows(connection),
            local_raw_root=ROOT / "local_data" if args.local_raw else None,
        )
    finally:
        connection.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
