import argparse
import contextlib
import json
import logging
import os
import sys
from datetime import date, timedelta
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


def save_activity_details(account_name: str, activity_date: date, activity_id: str, details):
    output_dir = (
        ROOT
        / os.getenv("GARMIN_LOCAL_DATA_DIR", "local_data")
        / "garmin_details"
        / account_name
        / activity_date.isoformat()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{activity_id}.json"
    output_path.write_text(
        json.dumps(details, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def as_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def as_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def normalize_hr_zone_seconds(payload: dict[str, Any] | None) -> dict[str, int]:
    if not payload:
        return {}
    zones = payload.get("heartRateZones")
    if not isinstance(zones, list):
        return {}
    normalized = {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        zone_number = zone.get("zoneNumber")
        minutes = zone.get("minutes")
        if zone_number is None or minutes is None:
            continue
        try:
            zone_number = int(zone_number)
            seconds = max(0, round(float(minutes) * 60))
        except (TypeError, ValueError):
            continue
        if 1 <= zone_number <= 5:
            normalized[str(zone_number)] = seconds
    return normalized


def hr_zone_seconds_from_details(
    details: dict[str, Any] | None,
    zone_settings: list[dict[str, Any]] | None,
) -> dict[str, int]:
    if not details or not zone_settings:
        return {}
    settings = next(
        (
            item for item in zone_settings
            if isinstance(item, dict) and item.get("sport") in {"DEFAULT", "RUNNING"}
        ),
        zone_settings[0] if isinstance(zone_settings[0], dict) else None,
    )
    if not settings:
        return {}
    try:
        floors = [float(settings[f"zone{index}Floor"]) for index in range(1, 6)]
    except (KeyError, TypeError, ValueError):
        return {}
    descriptors = details.get("metricDescriptors") or []
    descriptor_indexes = {
        descriptor.get("key"): index
        for index, descriptor in enumerate(descriptors)
        if isinstance(descriptor, dict)
    }
    heart_rate_index = descriptor_indexes.get("directHeartRate")
    timestamp_index = descriptor_indexes.get("directTimestamp")
    metrics = [
        item.get("metrics")
        for item in details.get("activityDetailMetrics", [])
        if isinstance(item, dict) and isinstance(item.get("metrics"), list)
    ]
    if heart_rate_index is None or timestamp_index is None or not metrics:
        return {}

    zone_seconds = {str(index): 0.0 for index in range(1, 6)}
    previous_interval = 1.0
    for position, sample in enumerate(metrics):
        if len(sample) <= max(heart_rate_index, timestamp_index):
            continue
        heart_rate = sample[heart_rate_index]
        timestamp = sample[timestamp_index]
        if heart_rate is None or timestamp is None:
            continue
        try:
            heart_rate = float(heart_rate)
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            continue
        interval = previous_interval
        if position + 1 < len(metrics):
            next_timestamp = metrics[position + 1][timestamp_index]
            try:
                interval = (float(next_timestamp) - timestamp) / 1000
            except (TypeError, ValueError):
                interval = previous_interval
        if not 0 < interval <= 10:
            interval = previous_interval
        previous_interval = interval
        zone_number = max(
            (index for index, floor in enumerate(floors, start=1) if heart_rate >= floor),
            default=1,
        )
        zone_seconds[str(zone_number)] += interval
    return {key: round(value) for key, value in zone_seconds.items() if value > 0}


def pace_by_hr_bucket_from_details(
    details: dict[str, Any] | None,
) -> dict[str, dict[str, float]]:
    warmup_exclude_seconds = 180
    if not details:
        return {}
    descriptors = details.get("metricDescriptors") or []
    descriptor_indexes = {
        descriptor.get("key"): index
        for index, descriptor in enumerate(descriptors)
        if isinstance(descriptor, dict)
    }
    heart_rate_index = descriptor_indexes.get("directHeartRate")
    timestamp_index = descriptor_indexes.get("directTimestamp")
    speed_index = descriptor_indexes.get("directSpeed")
    distance_index = descriptor_indexes.get("sumDistance")
    metrics = [
        item.get("metrics")
        for item in details.get("activityDetailMetrics", [])
        if isinstance(item, dict) and isinstance(item.get("metrics"), list)
    ]
    if heart_rate_index is None or timestamp_index is None or not metrics:
        return {}
    if speed_index is None and distance_index is None:
        return {}

    # bucket floor -> [seconds, distance_m], grouped into 3 bpm-wide buckets
    buckets: dict[str, list[float]] = {}
    previous_interval = 1.0
    previous_distance = None
    start_timestamp = None
    for position, sample in enumerate(metrics):
        required_index = max(
            index for index in (heart_rate_index, timestamp_index, speed_index, distance_index)
            if index is not None
        )
        if len(sample) <= required_index:
            continue
        heart_rate = sample[heart_rate_index]
        timestamp = sample[timestamp_index]
        if heart_rate is None or timestamp is None:
            continue
        try:
            heart_rate = float(heart_rate)
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            continue
        if start_timestamp is None:
            start_timestamp = timestamp
        interval = previous_interval
        if position + 1 < len(metrics):
            next_timestamp = metrics[position + 1][timestamp_index]
            try:
                interval = (float(next_timestamp) - timestamp) / 1000
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
                distance_delta = None
        if distance_delta is None and distance_index is not None:
            try:
                distance = float(sample[distance_index])
            except (TypeError, ValueError):
                distance = None
            if distance is not None:
                if previous_distance is not None:
                    distance_delta = max(0.0, distance - previous_distance)
                previous_distance = distance
        if not distance_delta or distance_delta <= 0:
            continue

        if (timestamp - start_timestamp) / 1000 < warmup_exclude_seconds:
            continue

        bucket = str(int(heart_rate // 3) * 3)
        totals = buckets.setdefault(bucket, [0.0, 0.0])
        totals[0] += interval
        totals[1] += distance_delta

    return {
        bucket: {"seconds": round(seconds, 1), "distance_m": round(distance_m, 1)}
        for bucket, (seconds, distance_m) in buckets.items()
        if seconds > 0 and distance_m > 0
    }


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


def init_garmin(account_name: str) -> Garmin | None:
    account_key = account_name.upper()
    default_tokenstore = "~/.garminconnect" if account_name == "manga" else f"~/.garminconnect/{account_name}"
    tokenstore = os.getenv(f"GARMINTOKENS_{account_key}", default_tokenstore)
    tokenstore_path = Path(tokenstore).expanduser()

    if not (tokenstore_path / "garmin_tokens.json").is_file():
        print(f"No Garmin token file found for {account_name}; skipping Garmin sync for this account.")
        return None

    try:
        garmin = Garmin()
        garmin.login(str(tokenstore_path))
        print("Logged in using saved Garmin tokens.")
        return garmin
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as exc:
        raise RuntimeError(
            f"Saved Garmin token login failed for {account_name}; "
            "password login is disabled. Refresh the token locally."
        ) from exc


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


def upsert_user(
    cur,
    profile: dict[str, Any],
    account_name: str,
    zone_settings: list[dict[str, Any]] | None = None,
) -> str:
    garmin_user_id = str(profile.get("userId") or profile.get("id") or "garmin-user")
    email = profile.get("email") or os.getenv(f"GARMIN_{account_name.upper()}_EMAIL")
    display_name = profile.get("displayName") or profile.get("fullName")
    cur.execute(
        """
        INSERT INTO users (account_name, garmin_user_id, email, display_name, hr_zone_settings)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (garmin_user_id) DO UPDATE SET
            account_name = EXCLUDED.account_name,
            email = COALESCE(EXCLUDED.email, users.email),
            display_name = COALESCE(EXCLUDED.display_name, users.display_name),
            hr_zone_settings = COALESCE(EXCLUDED.hr_zone_settings, users.hr_zone_settings)
        RETURNING id
        """,
        (account_name, garmin_user_id, email, display_name, Json(zone_settings) if zone_settings is not None else None),
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


def upsert_activity(
    cur,
    user_id: str,
    activity: dict[str, Any],
    hr_zone_seconds: dict[str, int] | None = None,
    pace_by_hr_bucket: dict[str, dict[str, float]] | None = None,
) -> tuple[str, str]:
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
            average_cadence, max_cadence, calories, steps, hr_zone_seconds,
            pace_by_hr_bucket, source_json
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
            hr_zone_seconds = EXCLUDED.hr_zone_seconds,
            pace_by_hr_bucket = EXCLUDED.pace_by_hr_bucket,
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
            Json(hr_zone_seconds or {}),
            Json(pace_by_hr_bucket or {}),
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


def sync_account(
    account_name: str,
    start_date: date,
    end_date: date,
    resync: bool = False,
    save_details: bool = False,
):
    garmin = init_garmin(account_name)
    if garmin is None:
        return
    profile = garmin.get_user_profile()
    try:
        zone_settings = garmin.get_heart_rate_zones()
    except Exception as exc:
        logging.warning("Could not fetch Garmin heart-rate zone settings: %s", exc)
        zone_settings = []
    conn = get_connection()
    activity_count = 0
    summary_count = 0
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
            user_id = upsert_user(cur, profile, account_name, zone_settings)
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
                if resync
                or (
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
                    sport = activity_type_key(activity)
                    zone_seconds = normalize_hr_zone_seconds(activity)
                    pace_by_hr_bucket: dict[str, dict[str, float]] = {}
                    if sport == "running":
                        try:
                            details = garmin.get_activity_details(
                                str(activity.get("activityId"))
                            )
                        except Exception as exc:
                            logging.warning(
                                "Could not fetch activity details for activity %s: %s",
                                activity.get("activityId"),
                                exc,
                            )
                        else:
                            if save_details:
                                save_activity_details(
                                    account_name,
                                    current,
                                    str(activity.get("activityId")),
                                    details,
                                )
                            if not zone_seconds:
                                zone_seconds = normalize_hr_zone_seconds(details)
                                if not zone_seconds:
                                    zone_seconds = hr_zone_seconds_from_details(
                                        details, zone_settings
                                    )
                            pace_by_hr_bucket = pace_by_hr_bucket_from_details(details)
                    _, sport = upsert_activity(
                        cur,
                        user_id,
                        activity,
                        hr_zone_seconds=zone_seconds,
                        pace_by_hr_bucket=pace_by_hr_bucket,
                    )
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


def sync_range(
    start_date: date,
    end_date: date,
    resync: bool = False,
    save_details: bool = False,
):
    for account_name in ACCOUNT_NAMES:
        sync_account(
            account_name,
            start_date,
            end_date,
            resync=resync,
            save_details=save_details,
        )


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
    parser.add_argument(
        "--resync",
        action="store_true",
        help="Refresh all dates in the requested range, including finalized historical dates.",
    )
    parser.add_argument(
        "--save-details",
        action="store_true",
        help="Save fetched activity-detail JSON under local_data; opt-in and local-only.",
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
        sync_range(
            start_date,
            end_date,
            resync=args.resync,
            save_details=args.save_details,
        )
    except (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError) as exc:
        print(f"Garmin authentication/rate-limit error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except (GarminConnectConnectionError, psycopg2.Error) as exc:
        print(f"Connection error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
