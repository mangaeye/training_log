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
            f"<section><h2>{html.escape(label)}</h2>"
            "<table><thead><tr>"
            "<th>Date</th><th>Total distance (km)</th><th>Sedentary time</th>"
            "<th>Run activities</th><th>Run distance (km)</th><th>Run time</th>"
            "</tr></thead><tbody>"
            + "".join(body)
            + "</tbody></table></section>"
        )

    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    content = "\n".join(sections) or "<p>No Garmin data has been synced yet.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Garmin Activity Report</title>
  <style>
    body {{ font-family: Georgia, serif; line-height: 1.4; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
    h1 {{ margin-bottom: 0.25rem; }}
    .updated {{ color: #666; margin-top: 0; }}
    section {{ margin: 2rem 0; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #999; padding: 0.5rem 0.65rem; text-align: right; white-space: nowrap; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ background: #eee; }}
    @media (max-width: 700px) {{ body {{ margin: 1rem auto; }} table {{ font-size: 0.85rem; }} th, td {{ padding: 0.35rem; }} }}
  </style>
</head>
<body>
  <h1>Garmin Activity Report</h1>
  <p class="updated">Generated {html.escape(generated)}</p>
  {content}
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
