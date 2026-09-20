import os
from pathlib import Path

try:
    import psycopg2
except Exception:
    psycopg2 = None


def load_env_file():
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key] = value


load_env_file()
DATABASE_URL = os.getenv("SUPABASE_DB_URL")


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("SUPABASE_DB_URL is not set. Create a .env file with your database URL.")
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed. Run: pip install psycopg2-binary")
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as exc:
        raise RuntimeError(f"Could not connect to Supabase Postgres: {exc}") from exc


def create_tables(cur):
    sql = Path("schema.sql").read_text()
    cur.execute(sql)


def main():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            create_tables(cur)
        conn.commit()
        print("Tables created successfully.")
    except Exception as exc:
        conn.rollback()
        print(f"Database error: {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
