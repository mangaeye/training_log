import html
import os
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "site" / "index.html"


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


def format_duration(seconds):
    if seconds is None:
        return "-"
    total_minutes = int((float(seconds) + 30) // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}"


def format_date(day):
    return day.strftime("%a %d %b")


def format_distance(meters):
    if meters is None:
        return "-"
    return f"{float(meters) / 1000:.2f}"


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


def fetch_rows(connection):
    query = """
        SELECT
            ds.summary_date,
            ds.total_distance_meters,
            ds.sedentary_seconds,
            COALESCE(dst.activity_count, 0) AS run_activities,
            COALESCE(dst.total_distance_meters, 0) AS run_distance_meters,
            COALESCE(dst.total_duration_seconds, 0) AS run_duration_seconds,
            COALESCE(activity_days.activities, '[]'::jsonb) AS activities
        FROM daily_summaries AS ds
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
                        )
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
        duration = format_duration(activity.get("duration_seconds", 0))
        items.append(f"<span class=\"activity\">{name}<small>{distance} km · {duration}</small></span>")
    return "".join(items)


def week_calendar(rows, start_date, end_date):
    row_by_date = {row[0]: row for row in rows if start_date <= row[0] <= end_date}
    days = [start_date + timedelta(days=index) for index in range(7)]
    totals = period_totals(rows, start_date, end_date)
    headers = "".join(f"<th>{html.escape(format_date(day))}</th>" for day in days)
    activity_cells = []
    running_activity_count = 0
    for day in days:
        row = row_by_date.get(day)
        activities = [
            activity for activity in (row[6] if row else [])
            if str(activity.get("sport", "")).lower() == "running"
        ]
        running_activity_count += len(activities)
        activity_cells.append(f"<td class=\"calendar-activities\">{activity_markup(activities)}</td>")
    week_total = (
        f"<strong>{running_activity_count} runs</strong>"
        f"<small>{format_distance(totals['run_distance'])} km</small>"
        f"<small>{format_duration(totals['run_time'])}</small>"
    )
    activity_row = "<tr><th>Runs</th>" + "".join(activity_cells) + f"<td class=\"calendar-total\">{week_total}</td></tr>"
    return (
        "<div class=\"calendar-wrap\"><table class=\"calendar\"><thead><tr>"
        "<th>Running activities</th>" + headers + "<th>Week totals</th></tr></thead><tbody>"
        + activity_row
        + "</tbody></table></div>"
    )


def render(rows):
    today = date.today()
    current_monday = today - timedelta(days=today.weekday())
    last_week_monday = current_monday - timedelta(days=7)
    summary_cards = []
    for label, (start_date, end_date) in period_ranges(today).items():
        totals = period_totals(rows, start_date, end_date)
        average_lines = ""
        if label in {"Current week", "Last week"}:
            average_lines = (
                f"<div><dt>Avg run distance/day</dt><dd>{format_distance(totals['average_run_distance'])} km</dd></div>"
                f"<div><dt>Avg run time/day</dt><dd>{format_duration(totals['average_run_time'])}</dd></div>"
            )
        summary_cards.append(
            "<article class=\"summary-card\">"
            f"<h3>{html.escape(label)}</h3>"
            f"<p class=\"summary-period\">{format_date(start_date)}"
            f"{f' to {format_date(end_date)}' if start_date != end_date else ''}</p>"
            "<dl>"
            f"<div><dt>Run distance</dt><dd>{format_distance(totals['run_distance'])} km</dd></div>"
            f"<div><dt>Run time</dt><dd>{format_duration(totals['run_time'])}</dd></div>"
            f"<div><dt>Total distance</dt><dd>{format_distance(totals['total_distance'])} km</dd></div>"
            f"<div><dt>Avg sedentary/day</dt><dd>{format_duration(totals['average_sedentary'])}</dd></div>"
            f"{average_lines}</dl></article>"
        )

    grouped = OrderedDict()
    for row in rows:
        monday = row[0] - timedelta(days=row[0].weekday())
        grouped.setdefault(monday, []).append(row)

    sections = []
    for monday, week_rows in grouped.items():
        label = week_label(monday)
        body = []
        for day, total_distance, sedentary, run_count, run_distance, run_time, _activities in week_rows:
            body.append(
                "<tr>"
                f"<td>{html.escape(format_date(day))}</td>"
                f"<td>{html.escape(format_distance(total_distance))}</td>"
                f"<td>{html.escape(format_duration(sedentary))}</td>"
                f"<td>{int(run_count or 0)}</td>"
                f"<td>{html.escape(format_distance(run_distance))}</td>"
                f"<td>{html.escape(format_duration(run_time))}</td>"
                "</tr>"
            )
        sections.append(
            f"<details class=\"week-group\"><summary>{html.escape(label)}</summary>"
            "<div class=\"table-wrap\"><table><thead><tr>"
            "<th>Date</th><th>Total distance (km)</th><th>Sedentary time</th>"
            "<th>Run activities</th><th>Run distance (km)</th><th>Run time</th>"
            "</tr></thead><tbody>"
            + "".join(body)
            + "</tbody></table></div></details>"
        )

    calendar_section = (
        "<section class=\"calendar-section\"><div class=\"section-heading\">"
        "<div><p class=\"eyebrow\">Calendar view</p><h2>Last week and this week</h2></div></div>"
        f"<article class=\"calendar-card\"><h3>Week {last_week_monday.isocalendar().week} · Last week</h3>"
        f"{week_calendar(rows, last_week_monday, current_monday - timedelta(days=1))}</article>"
        f"<article class=\"calendar-card\"><h3>Week {current_monday.isocalendar().week} · Current week</h3>"
        f"{week_calendar(rows, current_monday, current_monday + timedelta(days=6))}</article></section>"
    )

    latest_data_date = max((row[0] for row in rows), default=None)
    generated = "No successful Garmin data fetch recorded" if latest_data_date is None else f"Data through {format_date(latest_data_date)}"
    summary = "<section class=\"summary-section\"><div class=\"section-heading\">"
    summary += "<p class=\"eyebrow\">At a glance</p><h2>Training totals</h2>"
    summary += "</div><div class=\"summary-grid\">" + "".join(summary_cards) + "</div></section>"
    content = "\n".join(sections) or "<p>No Garmin data has been synced yet.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
        header {{ border-bottom: 2px solid var(--ink); margin-bottom: 2.8rem; padding-bottom: 2rem; }}
        .eyebrow {{ color: var(--accent); font: 700 0.72rem/1.2 Arial, sans-serif; letter-spacing: 0.16em; margin: 0 0 0.85rem; text-transform: uppercase; }}
        h1 {{ font-family: "Space Grotesk", sans-serif; font-size: clamp(3rem, 9vw, 7.5rem); font-weight: 600; letter-spacing: -0.055em; line-height: 0.88; margin: 0; max-width: 9ch; }}
        h2 {{ font-family: "Space Grotesk", sans-serif; font-size: clamp(1.5rem, 3vw, 2.2rem); font-weight: 600; margin: 0; }}
        h3 {{ font-family: "Space Grotesk", sans-serif; font-size: 1.2rem; font-weight: 600; margin: 0; }}
        .updated {{ color: var(--muted); font: 0.72rem Arial, sans-serif; letter-spacing: 0.04em; margin: 1.25rem 0 0; text-transform: uppercase; }}
        section {{ margin: 2.8rem 0; }}
        .section-heading {{ align-items: end; display: flex; justify-content: space-between; margin-bottom: 1rem; }}
        .summary-grid {{ display: grid; gap: 1rem; grid-template-columns: repeat(4, minmax(0, 1fr)); }}
        .summary-card {{ background: var(--card); border: 1px solid var(--ink); border-top: 5px solid var(--teal); padding: 1.15rem; }}
        .summary-card:nth-child(2) {{ border-top-color: var(--accent); }}
        .summary-card:nth-child(3) {{ border-top-color: #d28a32; }}
        .summary-card:nth-child(4) {{ border-top-color: #755d8a; }}
        .summary-period {{ color: var(--muted); font: 0.68rem Arial, sans-serif; margin: 0.35rem 0 1rem; }}
        dl {{ margin: 0; }}
        dl div {{ border-top: 1px solid var(--line); display: flex; gap: 0.5rem; justify-content: space-between; padding: 0.55rem 0; }}
        dt {{ color: var(--muted); font: 0.78rem Arial, sans-serif; }}
        dd {{ font-size: 1rem; margin: 0; text-align: right; }}
        .week-section {{ background: var(--card); border: 1px solid var(--line); border-left: 5px solid var(--accent); padding: 1rem; }}
        .week-group {{ background: var(--card); border: 1px solid var(--line); margin: 0.75rem 0; }}
        .week-group summary {{ cursor: pointer; font-family: "Space Grotesk", sans-serif; font-size: 1.15rem; font-weight: 600; list-style-position: inside; padding: 1rem; }}
        .week-group[open] summary {{ border-bottom: 1px solid var(--line); }}
        .week-group .table-wrap {{ padding: 0 1rem 1rem; }}
        .calendar-card {{ background: var(--card); border: 1px solid var(--line); margin: 1rem 0; padding: 1rem; }}
        .calendar-card h3 {{ border-bottom: 1px solid var(--line); padding-bottom: 0.8rem; }}
        .calendar-wrap {{ overflow-x: auto; }}
        .calendar {{ min-width: 920px; }}
        .calendar th, .calendar td {{ min-width: 105px; vertical-align: top; }}
        .calendar th:first-child, .calendar td:first-child {{ min-width: 115px; }}
        .calendar thead th {{ background: var(--ink); color: var(--paper); }}
        .calendar .calendar-total {{ background: var(--accent-soft); font-weight: 600; min-width: 120px; }}
        .calendar-total strong, .calendar-total small {{ display: block; }}
        .calendar-total small {{ font-size: 0.75rem; font-weight: 400; margin-top: 0.35rem; }}
        .calendar-activities {{ min-height: 5rem; text-align: left; white-space: normal; }}
        .activity {{ background: #e3eee8; border-left: 3px solid var(--teal); display: block; margin: 0 0 0.4rem; padding: 0.35rem; }}
        .activity small {{ color: var(--muted); display: block; font-size: 0.7rem; margin-top: 0.15rem; }}
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
            .week-section {{ margin-left: -0.25rem; margin-right: -0.25rem; padding: 0.75rem; }}
        }}
  </style>
</head>
<body>
    <main class="page">
        <header>
            <p class="eyebrow">Training Log · Garmin Connect</p>
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


def main():
    load_env_file()
    database_url = os.getenv("SUPABASE_DB_URL")
    if not database_url:
        raise RuntimeError("SUPABASE_DB_URL is not configured")
    connection = psycopg2.connect(database_url, sslmode="require")
    try:
        report = render(fetch_rows(connection))
    finally:
        connection.close()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(report, encoding="utf-8")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
