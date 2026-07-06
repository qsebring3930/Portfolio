import os
import pyodbc
import re
from pathlib import Path
import time
import json
import hashlib
import requests

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

cursor.execute("""
IF OBJECT_ID('processed_snapshots', 'U') IS NULL
CREATE TABLE processed_snapshots (
    snapshot_id NVARCHAR(100) NOT NULL PRIMARY KEY,
    processed_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
""")

cursor.execute("""
IF OBJECT_ID('processed_log_files', 'U') IS NULL
CREATE TABLE processed_log_files (
    file_name NVARCHAR(255) NOT NULL PRIMARY KEY,
    processed_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
""")

cursor.execute("""
IF OBJECT_ID('chat_messages', 'U') IS NULL
CREATE TABLE chat_messages (
    event_id NVARCHAR(64) NOT NULL PRIMARY KEY,
    source_file NVARCHAR(255) NOT NULL,
    line_number INT NOT NULL,
    timestamp DATETIME2 NULL,
    player_name NVARCHAR(255) NOT NULL,
    raw_player_name NVARCHAR(255) NULL,
    message NVARCHAR(MAX) NOT NULL,
    is_dead BIT NOT NULL DEFAULT 0
);
""")

cursor.execute("""
IF OBJECT_ID('admin_actions', 'U') IS NULL
CREATE TABLE admin_actions (
    event_id NVARCHAR(64) NOT NULL PRIMARY KEY,
    source_file NVARCHAR(255) NOT NULL,
    line_number INT NOT NULL,
    timestamp DATETIME2 NULL,
    admin_name NVARCHAR(255) NOT NULL,
    raw_admin_name NVARCHAR(255) NULL,
    command NVARCHAR(100) NOT NULL,
    command_args NVARCHAR(MAX) NULL,
    target_name NVARCHAR(255) NULL,
    amount INT NULL
);
""")

cursor.execute("""
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes 
    WHERE name = 'idx_chat_messages_player_name'
)
CREATE INDEX idx_chat_messages_player_name
ON chat_messages(player_name);
""")

cursor.execute("""
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes 
    WHERE name = 'idx_admin_actions_command'
)
CREATE INDEX idx_admin_actions_command
ON admin_actions(command);
""")

cursor.execute("""
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes 
    WHERE name = 'idx_admin_actions_target_name'
)
CREATE INDEX idx_admin_actions_target_name
ON admin_actions(target_name);
""")

conn.commit()
print("Tables created or already exist.")

SNAPSHOT_MINUTES = 1

def normalize_player_name(player_name):
    name = player_name.strip()

    if name.upper().startswith("BOT "):
        return "BOT"

    return name

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

            player_name = normalize_player_name(match.group(1))
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
    message_id = str(item.get("messageId") or item.get("id"))
    snapshot_id = f"{message_id}:{timestamp}"

    cursor.execute("""
    SELECT 1 FROM processed_snapshots
    WHERE snapshot_id = ?
    """, snapshot_id)

    if cursor.fetchone():
        print(f"Skipping already processed snapshot: {message_id}")
        return

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
    cursor.execute("""
    INSERT INTO processed_snapshots (snapshot_id)
    VALUES (?)
    """, snapshot_id)

def retrieve_messages(channel_id, limit=100):
    token = os.environ["DISCORD_TOKEN"].strip()

    headers = {
        "Authorization": token,
        "User-Agent": "War3CS2StatsBot/1.0",
    }

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"

    response = requests.get(
        url,
        headers=headers,
        params={"limit": min(limit, 100)},
        timeout=30,
    )

    print("Discord status:", response.status_code)

    if response.status_code != 200:
        print(response.text)
        response.raise_for_status()

    messages = response.json()
    print(f"Retrieved {len(messages)} Discord messages.")

    return messages

discord_channel_id = os.environ.get("DISCORD_CHANNEL_ID", "1240609027470131261")
discord_message_limit = int(os.environ.get("DISCORD_MESSAGE_LIMIT", "1"))

messages = retrieve_messages(discord_channel_id, limit=discord_message_limit)

for item in messages:
    update_stats(cursor, item)

os.makedirs("assets/data", exist_ok=True)

def fetch_rows(cursor, query):
    cursor.execute(query)
    columns = [c[0] for c in cursor.description]
    return [
        dict(zip(columns, row))
        for row in cursor.fetchall()
    ]


def get_map_complaint_counts(cursor):
    cursor.execute("""
    WITH complaint_messages AS (
        SELECT
            cm.event_id,
            cm.timestamp
        FROM chat_messages cm
        WHERE cm.timestamp IS NOT NULL
          AND (
               LOWER(cm.message) = 'rtv'
            OR LOWER(cm.message) = '.rtv'
            OR LOWER(cm.message) LIKE 'rtv %'
            OR LOWER(cm.message) LIKE '.rtv %'
            OR LOWER(cm.message) LIKE '% rtv'
            OR LOWER(cm.message) LIKE '% rtv %'
            OR LOWER(cm.message) LIKE '% .rtv'
            OR LOWER(cm.message) LIKE '% .rtv %'
            OR LOWER(cm.message) LIKE '%can we rtv%'
            OR LOWER(cm.message) LIKE '%can we rtv please%'
            OR LOWER(cm.message) LIKE '%change map%'
            OR LOWER(cm.message) LIKE '%change the map%'
            OR LOWER(cm.message) LIKE '%map change%'
          )
    )
    SELECT
        active_map.map_name,
        COUNT(*) AS complaint_count
    FROM complaint_messages cm
    CROSS APPLY (
        SELECT TOP 1
            prs.map_name
        FROM processed_round_backup_snapshots prs
        WHERE prs.backup_timestamp IS NOT NULL
          AND prs.map_name IS NOT NULL
          AND prs.backup_timestamp <= cm.timestamp
          AND prs.backup_timestamp >= DATEADD(hour, -3, cm.timestamp)
        ORDER BY
            prs.backup_timestamp DESC,
            prs.backup_round DESC
    ) AS active_map
    GROUP BY active_map.map_name;
    """)

    return {
        row.map_name: int(row.complaint_count or 0)
        for row in cursor.fetchall()
    }


map_complaint_counts = get_map_complaint_counts(cursor)

for table in [
    "map_playtime",
    "race_playtime",
    "race_levels",
]:
    rows = fetch_rows(cursor, f"SELECT * FROM {table}")

    if table == "map_playtime":
        for row in rows:
            map_name = row.get("map_name")
            row["complaint_count"] = map_complaint_counts.get(map_name, 0)

    with open(f"assets/data/{table}.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str)

cursor.execute("""
SELECT COUNT(*) AS rtv_count
FROM chat_messages
WHERE LOWER(message) LIKE 'rtv'
   OR LOWER(message) LIKE '.rtv'
   OR LOWER(message) LIKE 'rtv %'
   OR LOWER(message) LIKE '.rtv %'
   OR LOWER(message) LIKE '% rtv'
   OR LOWER(message) LIKE '% rtv %'
   OR LOWER(message) LIKE '% .rtv'
   OR LOWER(message) LIKE '% .rtv %'
   OR LOWER(message) LIKE '%can we rtv%'
   OR LOWER(message) LIKE '%can we rtv please%'
   OR LOWER(message) LIKE '%change map%'
   OR LOWER(message) LIKE '%change the map%'
   OR LOWER(message) LIKE '%map change%'
""")

rtv_count = cursor.fetchone()[0] or 0

with open("assets/data/rtv_requests.json", "w", encoding="utf-8") as f:
    json.dump({"rtv_count": rtv_count}, f, indent=2)

print(f"Exported RTV request count: {rtv_count}")

conn.commit()
print("Stats updated.")

cursor.close()
conn.close()
