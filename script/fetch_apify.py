import os
import pyodbc
import re
from apify_client import ApifyClient
from pathlib import Path
import time

print("Testing Azure SQL connection...")

def connect_with_retry(max_attempts=5):
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"Connecting to Azure SQL, attempt {attempt}/{max_attempts}...")

            return pyodbc.connect(
                "DRIVER={ODBC Driver 18 for SQL Server};"
                f"SERVER=tcp:{os.environ['AZURE_SQL_SERVER']},1433;"
                f"DATABASE={os.environ['AZURE_SQL_DATABASE']};"
                f"UID={os.environ['AZURE_SQL_USERNAME']};"
                f"PWD={os.environ['AZURE_SQL_PASSWORD']};"
                "Encrypt=yes;"
                "TrustServerCertificate=no;"
                "Connection Timeout=60;"
            )

        except pyodbc.Error as e:
            last_error = e
            print(f"Connection failed: {e}")
            time.sleep(10)

    raise last_error

conn = connect_with_retry()

cursor = conn.cursor()

cursor.execute("SELECT @@VERSION")

row = cursor.fetchone()

print("Connected successfully!")
print(row[0][:200])

cursor.execute("""
IF OBJECT_ID('race_playtime', 'U') IS NULL
CREATE TABLE race_playtime (
    player_name NVARCHAR(255) NOT NULL PRIMARY KEY,
    last_seen DATETIME2 NULL
);
""")

cursor.execute("""
IF OBJECT_ID('race_levels', 'U') IS NULL
CREATE TABLE race_levels (
    player_name NVARCHAR(255) NOT NULL PRIMARY KEY,
    last_seen DATETIME2 NULL
);
""")

cursor.execute("""
IF OBJECT_ID('map_playtime', 'U') IS NULL
CREATE TABLE map_playtime (
    map_name NVARCHAR(255) NOT NULL PRIMARY KEY,
    minutes_played INT NOT NULL DEFAULT 0,
    last_seen DATETIME2 NULL
);
""")

conn.commit()
print("Tables created or already exist.")

SNAPSHOT_MINUTES = 5

def race_to_column(race_name):
    col = race_name.lower()
    col = re.sub(r"[^a-z0-9]+", "_", col).strip("_")
    return col[:120] if col else "saboteur"


def ensure_column(cursor, table_name, column_name, column_type):
    cursor.execute(f"""
    IF COL_LENGTH('{table_name}', ?) IS NULL
    BEGIN
        ALTER TABLE {table_name}
        ADD [{column_name}] {column_type}
    END
    """, column_name)


def get_valid_race_columns():
    script_dir = Path(__file__).resolve().parent
    race_pages_dir = script_dir / "war3cs2_pages"

    valid_columns = {
        race_to_column(path.stem)
        for path in race_pages_dir.glob("*.txt")
    }

    valid_columns.add("saboteur")
    return valid_columns


def ensure_known_race_columns(cursor):
    valid_columns = get_valid_race_columns()

    for race_col in valid_columns:
        ensure_column(cursor, "race_playtime", race_col, "INT NOT NULL DEFAULT 0")
        ensure_column(cursor, "race_levels", race_col, "INT NULL")

    print(f"Ensured {len(valid_columns)} race columns.")
    return valid_columns

VALID_RACE_COLUMNS = ensure_known_race_columns(cursor)
conn.commit()

def get_embed(item):
    embeds = item.get("embeds") or []
    if not embeds:
        return None
    return embeds[0]


def get_map_name(embed):
    for field in embed.get("fields", []):
        if "Map" in field.get("name", ""):
            value = field.get("value", "")
            # de_ancient (14) -> de_ancient
            return value.split(" ")[0].strip()
    return None


def parse_team_players(embed):
    players = []

    for field in embed.get("fields", []):
        name = field.get("name", "")

        if "Horde" not in name and "Alliance" not in name:
            continue

        value = field.get("value", "")

        for line in value.splitlines():
            line = line.strip()

            # matches:
            # * cold - [Priest | Lvl. 35]
            match = re.match(r"^\*\s*(.+?)\s*-\s*\[(.+?)\s*\|\s*Lvl\.\s*(\d+)\]", line)

            if not match:
                continue

            player_name = match.group(1).strip()
            race_name = match.group(2).strip()
            level = int(match.group(3))

            players.append({
                "player_name": player_name,
                "race_name": race_name,
                "level": level,
            })

    return players

def update_stats(cursor, item):
    embed = get_embed(item)
    if not embed:
        return

    timestamp = embed.get("timestamp") or item.get("timestamp")

    map_name = get_map_name(embed)
    players = parse_team_players(embed)

    if map_name:
        cursor.execute("""
            MERGE map_playtime AS target
            USING (SELECT ? AS map_name) AS source
            ON target.map_name = source.map_name
            WHEN MATCHED THEN
                UPDATE SET
                    minutes_played = minutes_played + ?,
                    last_seen = TRY_CONVERT(DATETIME2, ?)
            WHEN NOT MATCHED THEN
                INSERT (map_name, minutes_played, last_seen)
                VALUES (?, ?, TRY_CONVERT(DATETIME2, ?));
        """, (
            map_name,
            SNAPSHOT_MINUTES,
            timestamp,
            map_name,
            SNAPSHOT_MINUTES,
            timestamp,
        ))

    for player in players:
        race_col = race_to_column(player["race_name"])

        if race_col not in VALID_RACE_COLUMNS:
            race_col = "saboteur"

        cursor.execute(f"""
            MERGE race_playtime AS target
            USING (SELECT ? AS player_name) AS source
            ON target.player_name = source.player_name
            WHEN MATCHED THEN
                UPDATE SET
                    [{race_col}] = [{race_col}] + ?,
                    last_seen = TRY_CONVERT(DATETIME2, ?)
            WHEN NOT MATCHED THEN
                INSERT (player_name, [{race_col}], last_seen)
                VALUES (?, ?, TRY_CONVERT(DATETIME2, ?));
        """, (
            player["player_name"],
            SNAPSHOT_MINUTES,
            timestamp,
            player["player_name"],
            SNAPSHOT_MINUTES,
            timestamp,
        ))

        cursor.execute(f"""
            MERGE race_levels AS target
            USING (SELECT ? AS player_name) AS source
            ON target.player_name = source.player_name
            WHEN MATCHED THEN
                UPDATE SET
                    [{race_col}] = ?,
                    last_seen = TRY_CONVERT(DATETIME2, ?)
            WHEN NOT MATCHED THEN
                INSERT (player_name, [{race_col}], last_seen)
                VALUES (?, ?, TRY_CONVERT(DATETIME2, ?));
        """, (
            player["player_name"],
            player["level"],
            timestamp,
            player["player_name"],
            player["level"],
            timestamp,
        ))


client = ApifyClient(os.environ["APIFY_TOKEN"])

run_input = {
    "token": os.environ["DISCORD_TOKEN"],
    "channelInput": "https://discord.com/channels/1232410706230513814/1240609027470131261",
}

run = client.actor("wUoh2wdO7k9mnzL9d").call(run_input=run_input)

for item in client.dataset(run.default_dataset_id).iterate_items():
    print(item)
    update_stats(cursor, item)

conn.commit()
print("Stats updated.")

cursor.close()
conn.close()
