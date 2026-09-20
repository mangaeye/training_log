import html
import os
from collections import OrderedDict
from datetime import date, datetime, timedelta
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
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_distance(meters):
    if meters is None:
        return "-"
    return f"{float(meters) / 1000:.2f}"


def week_label(day):
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    return f"Week of {monday.isoformat()} to {sunday.isoformat()}"


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
    return {
        "run_distance": sum(float(row[4] or 0) for row in selected),
        "run_time": sum(int(row[5] or 0) for row in selected),
        "total_distance": sum(float(row[1] or 0) for row in selected),
        "sedentary": sum(int(row[2] or 0) for row in selected),
    }


def fetch_rows(connection):
    query = """
        SELECT
            ds.summary_date,
            ds.total_distance_meters,
            ds.sedentary_seconds,
            COALESCE(dst.activity_count, 0) AS run_activities,
            COALESCE(dst.total_distance_meters, 0) AS run_distance_meters,
            COALESCE(dst.total_duration_seconds, 0) AS run_duration_seconds
        FROM daily_summaries AS ds
        LEFT JOIN daily_sport_totals AS dst
            ON dst.user_id = ds.user_id
           AND dst.summary_date = ds.summary_date
           AND dst.sport_type_id = (
               SELECT id FROM sport_types WHERE name = 'running'
           )
        ORDER BY ds.summary_date DESC
    """
    with connection.cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchall()


def render(rows):
    today = date.today()
    summary_cards = []
    for label, (start_date, end_date) in period_ranges(today).items():
        totals = period_totals(rows, start_date, end_date)
        summary_cards.append(
            "<article class=\"summary-card\">"
            f"<h3>{html.escape(label)}</h3>"
            f"<p class=\"summary-period\">{start_date.isoformat()}"
            f"{f' to {end_date.isoformat()}' if start_date != end_date else ''}</p>"
            "<dl>"
            f"<div><dt>Run distance</dt><dd>{format_distance(totals['run_distance'])} km</dd></div>"
            f"<div><dt>Run time</dt><dd>{format_duration(totals['run_time'])}</dd></div>"
            f"<div><dt>Total distance</dt><dd>{format_distance(totals['total_distance'])} km</dd></div>"
            f"<div><dt>Sedentary time</dt><dd>{format_duration(totals['sedentary'])}</dd></div>"
            "</dl></article>"
        )

    grouped = OrderedDict()
    for row in rows:
        day = row[0]
        grouped.setdefault(week_label(day), []).append(row)

    sections = []
    for label, week_rows in grouped.items():
        body = []
        for day, total_distance, sedentary, run_count, run_distance, run_time in week_rows:
            body.append(
                "<tr>"
                f"<td>{html.escape(day.isoformat())}</td>"
                f"<td>{html.escape(format_distance(total_distance))}</td>"
                f"<td>{html.escape(format_duration(sedentary))}</td>"
                f"<td>{int(run_count or 0)}</td>"
                f"<td>{html.escape(format_distance(run_distance))}</td>"
                f"<td>{html.escape(format_duration(run_time))}</td>"
                "</tr>"
            )
        sections.append(
            f"<section class=\"week-section\"><h2>{html.escape(label)}</h2>"
            "<div class=\"table-wrap\"><table><thead><tr>"
            "<th>Date</th><th>Total distance (km)</th><th>Sedentary time</th>"
            "<th>Run activities</th><th>Run distance (km)</th><th>Run time</th>"
            "</tr></thead><tbody>"
            + "".join(body)
            + "</tbody></table></div></section>"
        )

    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    summary = "<section class=\"summary-section\"><div class=\"section-heading\">"
    summary += "<p class=\"eyebrow\">At a glance</p><h2>Training totals</h2>"
    summary += "</div><div class=\"summary-grid\">" + "".join(summary_cards) + "</div></section>"
    content = "\n".join(sections) or "<p>No Garmin data has been synced yet.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Garmin Activity Report</title>
  <style>
        :root {{
            color-scheme: light;
            --ink: #17211f;
            --muted: #66736e;
            --line: #d8e0da;
            --paper: #f6f8f3;
            --card: #ffffff;
            --accent: #ba4d2f;
            --accent-soft: #f4e2d8;
            --teal: #176b67;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            background: radial-gradient(circle at top left, #e6f0e8 0, transparent 34rem), var(--paper);
            color: var(--ink);
            font-family: Georgia, "Times New Roman", serif;
            line-height: 1.45;
            margin: 0;
        }}
        .page {{ max-width: 1180px; margin: 0 auto; padding: clamp(1.5rem, 4vw, 4rem) clamp(1rem, 4vw, 2.5rem); }}
        header {{ border-bottom: 1px solid var(--line); margin-bottom: 2rem; padding-bottom: 1.5rem; }}
        .eyebrow {{ color: var(--accent); font: 700 0.75rem/1.2 Arial, sans-serif; letter-spacing: 0.12em; margin: 0 0 0.7rem; text-transform: uppercase; }}
        h1 {{ font-size: clamp(2.2rem, 6vw, 4.8rem); font-weight: 400; letter-spacing: -0.03em; line-height: 0.98; margin: 0; max-width: 8ch; }}
        h2 {{ font-size: clamp(1.5rem, 3vw, 2.2rem); font-weight: 400; margin: 0; }}
        h3 {{ font-size: 1.2rem; font-weight: 400; margin: 0; }}
        .updated {{ color: var(--muted); font: 0.82rem Arial, sans-serif; margin: 1rem 0 0; }}
        section {{ margin: 2.8rem 0; }}
        .section-heading {{ align-items: end; display: flex; justify-content: space-between; margin-bottom: 1rem; }}
        .summary-grid {{ display: grid; gap: 1rem; grid-template-columns: repeat(4, minmax(0, 1fr)); }}
        .summary-card {{ background: var(--card); border: 1px solid var(--line); border-top: 4px solid var(--teal); box-shadow: 0 8px 24px rgba(23, 33, 31, 0.06); padding: 1.15rem; }}
        .summary-card:nth-child(2) {{ border-top-color: var(--accent); }}
        .summary-card:nth-child(3) {{ border-top-color: #d28a32; }}
        .summary-card:nth-child(4) {{ border-top-color: #755d8a; }}
        .summary-period {{ color: var(--muted); font: 0.72rem Arial, sans-serif; margin: 0.35rem 0 1rem; }}
        dl {{ margin: 0; }}
        dl div {{ border-top: 1px solid var(--line); display: flex; gap: 0.5rem; justify-content: space-between; padding: 0.55rem 0; }}
        dt {{ color: var(--muted); font: 0.78rem Arial, sans-serif; }}
        dd {{ font-size: 1rem; margin: 0; text-align: right; }}
        .week-section {{ background: rgba(255, 255, 255, 0.65); border-left: 4px solid var(--accent); padding: 1rem; }}
        table {{ border-collapse: collapse; min-width: 720px; width: 100%; }}
        .table-wrap {{ overflow-x: auto; }}
        th, td {{ border-bottom: 1px solid var(--line); padding: 0.75rem 0.65rem; text-align: right; white-space: nowrap; }}
        th:first-child, td:first-child {{ text-align: left; }}
        th {{ background: var(--accent-soft); color: var(--ink); font: 700 0.72rem Arial, sans-serif; letter-spacing: 0.03em; text-transform: uppercase; }}
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
            <p class="eyebrow">Personal movement log</p>
            <h1>Garmin Activity Report</h1>
            <p class="updated">Generated {html.escape(generated)}</p>
        </header>
        {summary}
        <section>
            <div class="section-heading"><div><p class="eyebrow">Daily detail</p><h2>Activity history</h2></div></div>
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
