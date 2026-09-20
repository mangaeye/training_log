import argparse
import contextlib
import logging
import os
import sys
from datetime import date, timedelta
from getpass import getpass
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import Json
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

ROOT = Path(__file__).resolve().parent
logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
ACCOUNT_NAMES = ("manga", "chips")


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


def as_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def as_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def activity_type_key(activity: dict[str, Any]) -> str:
    activity_type = activity.get("activityType") or {}
    return str(
        activity_type.get("typeKey")
        or activity.get("activityTypeKey")
        or "other"
    ).lower()


def category_for_sport(sport: str) -> str:
    if sport in {"running", "cycling", "walking", "swimming"}:
        return "cardio"
    if sport in {"hiking"}:
        return "outdoor"
    if sport in {"strength_training"}:
        return "strength"
    if sport in {"yoga"}:
        return "wellness"
    return "other"


def init_garmin(account_name: str) -> Garmin:
    account_key = account_name.upper()
    default_tokenstore = "~/.garminconnect" if account_name == "manga" else f"~/.garminconnect/{account_name}"
    tokenstore = os.getenv(f"GARMINTOKENS_{account_key}", default_tokenstore)
    tokenstore_path = str(Path(tokenstore).expanduser())

    try:
        garmin = Garmin()
        garmin.login(tokenstore_path)
        print("Logged in using saved Garmin tokens.")
        return garmin
    except (GarminConnectAuthenticationError, GarminConnectConnectionError):
        pass

    email = os.getenv(f"GARMIN_{account_key}_EMAIL") or input(
        f"Garmin {account_name} email: "
    ).strip()
    password = os.getenv(f"GARMIN_{account_key}_PASSWORD") or getpass(
        f"Garmin {account_name} password: "
    )
    garmin = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    garmin.login(tokenstore_path)
    print(f"Garmin login successful. Tokens saved to: {tokenstore_path}")
    return garmin


def get_connection():
    database_url = os.getenv("SUPABASE_DB_URL")
    if not database_url:
        raise RuntimeError("SUPABASE_DB_URL is not set in .env or the process environment.")
    return psycopg2.connect(database_url, sslmode="require")


def ensure_schema(cur):
    cur.execute((ROOT / "schema.sql").read_text())


def existing_summary_statuses(
    cur, user_id: str, start_date: date, end_date: date
) -> dict[date, bool]:
    cur.execute(
        """
        SELECT summary_date, is_final
        FROM daily_summaries
        WHERE user_id = %s
          AND summary_date BETWEEN %s AND %s
        """,
        (user_id, start_date, end_date),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def upsert_user(cur, profile: dict[str, Any], account_name: str) -> str:
    garmin_user_id = str(profile.get("userId") or profile.get("id") or "garmin-user")
    email = profile.get("email") or os.getenv(f"GARMIN_{account_name.upper()}_EMAIL")
    display_name = profile.get("displayName") or profile.get("fullName")
    cur.execute(
        """
        INSERT INTO users (account_name, garmin_user_id, email, display_name)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (garmin_user_id) DO UPDATE SET
            account_name = EXCLUDED.account_name,
            email = COALESCE(EXCLUDED.email, users.email),
            display_name = COALESCE(EXCLUDED.display_name, users.display_name)
        RETURNING id
        """,
        (account_name, garmin_user_id, email, display_name),
    )
    return str(cur.fetchone()[0])


def get_sport_type_id(cur, sport: str) -> int:
    cur.execute(
        """
        INSERT INTO sport_types (name, category)
        VALUES (%s, %s)
        ON CONFLICT (name) DO UPDATE SET category = EXCLUDED.category
        RETURNING id
        """,
        (sport, category_for_sport(sport)),
    )
    return cur.fetchone()[0]


def upsert_daily_summary(
    cur,
    user_id: str,
    summary_date: date,
    summary: dict[str, Any],
    is_final: bool,
):
    cur.execute(
        """
        INSERT INTO daily_summaries (
            user_id, summary_date, steps, active_calories, total_calories,
            total_distance_meters, active_seconds, sedentary_seconds,
            floors_climbed, resting_heart_rate, is_final, source_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, summary_date) DO UPDATE SET
            steps = EXCLUDED.steps,
            active_calories = EXCLUDED.active_calories,
            total_calories = EXCLUDED.total_calories,
            total_distance_meters = EXCLUDED.total_distance_meters,
            active_seconds = EXCLUDED.active_seconds,
            sedentary_seconds = EXCLUDED.sedentary_seconds,
            floors_climbed = EXCLUDED.floors_climbed,
            resting_heart_rate = EXCLUDED.resting_heart_rate,
            is_final = EXCLUDED.is_final,
            source_json = EXCLUDED.source_json
        """,
        (
            user_id,
            summary_date,
            as_int(summary.get("totalSteps")),
            as_int(summary.get("activeKilocalories")),
            as_int(summary.get("totalKilocalories")),
            as_float(summary.get("totalDistanceMeters")),
            as_int(summary.get("activeSeconds")),
            as_int(summary.get("sedentarySeconds")),
            as_int(summary.get("floorsAscended")),
            as_int(summary.get("restingHeartRate")),
            is_final,
            Json(summary),
        ),
    )


def upsert_activity(cur, user_id: str, activity: dict[str, Any]) -> tuple[str, str]:
    garmin_activity_id = str(activity.get("activityId"))
    sport = activity_type_key(activity)
    sport_type_id = get_sport_type_id(cur, sport)
    cur.execute(
        """
        INSERT INTO activities (
            user_id, garmin_activity_id, sport_type_id, activity_name,
            start_time, activity_type, elapsed_duration_seconds,
            moving_duration_seconds, distance_meters, elevation_meters,
            average_speed, max_speed, average_heart_rate, max_heart_rate,
            average_cadence, max_cadence, calories, steps, source_json
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (garmin_activity_id) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            sport_type_id = EXCLUDED.sport_type_id,
            activity_name = EXCLUDED.activity_name,
            start_time = EXCLUDED.start_time,
            activity_type = EXCLUDED.activity_type,
            elapsed_duration_seconds = EXCLUDED.elapsed_duration_seconds,
            moving_duration_seconds = EXCLUDED.moving_duration_seconds,
            distance_meters = EXCLUDED.distance_meters,
            elevation_meters = EXCLUDED.elevation_meters,
            average_speed = EXCLUDED.average_speed,
            max_speed = EXCLUDED.max_speed,
            average_heart_rate = EXCLUDED.average_heart_rate,
            max_heart_rate = EXCLUDED.max_heart_rate,
            average_cadence = EXCLUDED.average_cadence,
            max_cadence = EXCLUDED.max_cadence,
            calories = EXCLUDED.calories,
            steps = EXCLUDED.steps,
            source_json = EXCLUDED.source_json
        RETURNING id
        """,
        (
            user_id,
            garmin_activity_id,
            sport_type_id,
            activity.get("activityName"),
            activity.get("startTimeLocal") or activity.get("startTimeGMT"),
            sport,
            as_int(activity.get("elapsedDuration")),
            as_int(activity.get("duration")),
            as_float(activity.get("distance")),
            as_float(activity.get("elevationGain")),
            as_float(activity.get("averageSpeed")),
            as_float(activity.get("maxSpeed")),
            as_int(activity.get("averageHR")),
            as_int(activity.get("maxHR")),
            as_float(activity.get("averageRunningCadence")),
            as_float(activity.get("maxRunningCadence")),
            as_int(activity.get("calories")),
            as_int(activity.get("steps")),
            Json(activity),
        ),
    )
    return str(cur.fetchone()[0]), sport


def upsert_daily_sport_total(
    cur,
    user_id: str,
    summary_date: date,
    sport: str,
    activity_count: int,
    distance_meters: float,
    duration_seconds: int,
    calories: int,
):
    sport_type_id = get_sport_type_id(cur, sport)
    cur.execute(
        """
        INSERT INTO daily_sport_totals (
            user_id, summary_date, sport_type_id, activity_count,
            total_distance_meters, total_duration_seconds, total_calories
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, summary_date, sport_type_id) DO UPDATE SET
            activity_count = EXCLUDED.activity_count,
            total_distance_meters = EXCLUDED.total_distance_meters,
            total_duration_seconds = EXCLUDED.total_duration_seconds,
            total_calories = EXCLUDED.total_calories
        """,
        (
            user_id,
            summary_date,
            sport_type_id,
            activity_count,
            distance_meters,
            duration_seconds,
            calories,
        ),
    )


def sync_account(account_name: str, start_date: date, end_date: date):
    garmin = init_garmin(account_name)
    profile = garmin.get_user_profile()
    conn = get_connection()
    activity_count = 0
    summary_count = 0
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
            user_id = upsert_user(cur, profile, account_name)
            stored_statuses = existing_summary_statuses(
                cur, user_id, start_date, end_date
            )
            today = date.today()
            yesterday = today - timedelta(days=1)
            refresh_dates = {
                refresh_date
                for refresh_date in (today, yesterday)
                if start_date <= refresh_date <= end_date
                and (
                    refresh_date == today
                    or
                    refresh_date not in stored_statuses
                    or not stored_statuses[refresh_date]
                )
            }
            dates_to_sync = [
                start_date + timedelta(days=offset)
                for offset in range((end_date - start_date).days + 1)
                if (
                    start_date + timedelta(days=offset) not in stored_statuses
                    or start_date + timedelta(days=offset) in refresh_dates
                )
            ]
            skipped_count = len(stored_statuses) - len(refresh_dates)
            if not dates_to_sync:
                print(
                    f"No dates to sync from {start_date} through {end_date}; "
                    f"skipped {skipped_count} stored historical days."
                )
                conn.commit()
                return

            for current in dates_to_sync:
                date_string = current.isoformat()
                summary = garmin.get_user_summary(date_string) or {}
                upsert_daily_summary(
                    cur,
                    user_id,
                    current,
                    summary,
                    is_final=current < today,
                )
                summary_count += 1

                activities = garmin.get_activities_by_date(
                    date_string,
                    date_string,
                    sortorder="asc",
                ) or []
                aggregates: dict[str, list[float]] = {}
                for activity in activities:
                    _, sport = upsert_activity(cur, user_id, activity)
                    activity_count += 1
                    values = aggregates.setdefault(sport, [0, 0, 0, 0])
                    values[0] += 1
                    values[1] += float(activity.get("distance", 0) or 0)
                    values[2] += float(
                        activity.get("duration", activity.get("elapsedDuration", 0)) or 0
                    )
                    values[3] += float(activity.get("calories", 0) or 0)

                for sport, values in aggregates.items():
                    upsert_daily_sport_total(
                        cur,
                        user_id,
                        current,
                        sport,
                        int(values[0]),
                        values[1],
                        int(values[2]),
                        int(values[3]),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(
        f"[{account_name}] Synced {summary_count} daily summaries and "
        f"{activity_count} activities from {start_date} through {end_date}; "
        f"skipped {skipped_count} finalized days; "
        "today remains live and yesterday is finalized once."
    )


def sync_range(start_date: date, end_date: date):
    for account_name in ACCOUNT_NAMES:
        sync_account(account_name, start_date, end_date)


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description="Sync Garmin Connect data to Supabase.")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument(
        "--days",
        type=int,
        help="Sync this many days ending on --end-date; overrides the default start date.",
    )
    args = parser.parse_args()

    end_date = args.end_date or date.today()
    if args.start_date and args.days is not None:
        parser.error("use either --start-date or --days, not both")
    if args.days is not None:
        if args.days < 1:
            parser.error("--days must be at least 1")
        start_date = end_date - timedelta(days=args.days - 1)
    else:
        start_date = args.start_date or date(2026, 9, 1)
    if start_date > end_date:
        parser.error("--start-date must be on or before --end-date")

    try:
        sync_range(start_date, end_date)
    except (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError) as exc:
        print(f"Garmin authentication/rate-limit error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except (GarminConnectConnectionError, psycopg2.Error) as exc:
        print(f"Connection error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
