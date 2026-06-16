import os
import pyodbc
import re
from apify_client import ApifyClient
from pathlib import Path
import time
import json
import hashlib

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

CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MULTISPACE_RE = re.compile(r"\s+")
TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ \+00:00)"
)

ISSUED_COMMAND_RE = re.compile(
    r"plugin:CS2-SimpleAdmin .*? (?P<admin>.*?) issued command `(?P<command>[^`]+)`"
)


def clean_text(value):
    value = CONTROL_CHARS_RE.sub("", value)
    value = value.replace("\u200b", "")
    value = MULTISPACE_RE.sub(" ", value)
    return value.strip()


def clean_player_name(name):
    name = clean_text(name)

    name = name.replace("*DEAD*", "")
    name = re.sub(r"\((CT|T|SPEC|SPECTATOR)\)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\[[^\]]+\]", "", name)

    return clean_text(name)


def get_log_timestamp(line):
    match = TIMESTAMP_RE.search(line)
    return match.group("timestamp") if match else None


def make_event_id(source_file, line_number, line):
    raw = f"{source_file}:{line_number}:{line}".encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()


def parse_chat_line(line, source_file, line_number):
    if "plugin:Warcraft 3 Counter-Strike 2" not in line:
        return None

    non_chat_markers = [
        "Fetched Event Data",
        "Fetched Player Rank Data",
        "Fetched Race Rank Data",
        "Successfully added MOTD string",
        "Cleared database",
        "Timer Exception",
        "has disconnected due to timeout",
    ]

    if any(marker in line for marker in non_chat_markers):
        return None

    timestamp = get_log_timestamp(line)

    try:
        body = line.split("plugin:Warcraft 3 Counter-Strike 2", 1)[1]
    except IndexError:
        return None

    body_clean = clean_text(body)

    if ":" not in body_clean:
        return None

    raw_player_name, message = body_clean.split(":", 1)

    player_name = clean_player_name(raw_player_name)
    message = clean_text(message)

    if not player_name or not message:
        return None

    return {
        "event_id": make_event_id(source_file, line_number, line),
        "source_file": source_file,
        "line_number": line_number,
        "timestamp": timestamp,
        "player_name": player_name,
        "raw_player_name": clean_text(raw_player_name),
        "message": message,
        "is_dead": 1 if "*DEAD*" in body else 0,
    }


def parse_admin_action(line, source_file, line_number):
    if "issued command `" not in line:
        return None

    cleaned_line = clean_text(line)
    match = ISSUED_COMMAND_RE.search(cleaned_line)

    if not match:
        return None

    timestamp = get_log_timestamp(line)

    raw_admin_name = clean_text(match.group("admin"))
    admin_name = clean_player_name(raw_admin_name)

    full_command = clean_text(match.group("command"))
    parts = full_command.split(maxsplit=1)

    command = parts[0].lower()
    command_args = parts[1] if len(parts) > 1 else ""

    target_name = None
    amount = None

    if command in {"css_slay", "css_slap"} and command_args:
        target_text = command_args

        if command == "css_slap":
            possible = command_args.rsplit(maxsplit=1)

            if len(possible) == 2 and possible[1].lstrip("-").isdigit():
                target_text = possible[0]
                amount = int(possible[1])

        target_name = clean_player_name(target_text)

    return {
        "event_id": make_event_id(source_file, line_number, line),
        "source_file": source_file,
        "line_number": line_number,
        "timestamp": timestamp,
        "admin_name": admin_name,
        "raw_admin_name": raw_admin_name,
        "command": command,
        "command_args": command_args,
        "target_name": target_name,
        "amount": amount,
    }


def already_processed_log_file(cursor, file_name):
    cursor.execute("""
        SELECT 1
        FROM processed_log_files
        WHERE file_name = ?
    """, file_name)

    return cursor.fetchone() is not None


def mark_log_file_processed(cursor, file_name):
    cursor.execute("""
        INSERT INTO processed_log_files (file_name)
        VALUES (?)
    """, file_name)


def import_server_logs(cursor):
    logs_dir = Path("logs")

    if not logs_dir.exists():
        print("No logs folder found, skipping server log import.")
        return

    log_files = sorted(logs_dir.glob("*.txt"))

    if not log_files:
        print("No .txt logs found, skipping server log import.")
        return

    chat_count = 0
    admin_count = 0
    skipped_files = 0

    for log_file in log_files:
        if already_processed_log_file(cursor, log_file.name):
            skipped_files += 1
            print(f"Skipping already processed log: {log_file.name}")
            continue

        print(f"Processing log file: {log_file.name}")

        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            for line_number, line in enumerate(f, start=1):
                chat = parse_chat_line(line, log_file.name, line_number)

                if chat:
                    cursor.execute("""
                        IF NOT EXISTS (
                            SELECT 1 FROM chat_messages WHERE event_id = ?
                        )
                        INSERT INTO chat_messages (
                            event_id,
                            source_file,
                            line_number,
                            timestamp,
                            player_name,
                            raw_player_name,
                            message,
                            is_dead
                        )
                        VALUES (
                            ?,
                            ?,
                            ?,
                            TRY_CONVERT(DATETIME2, ?),
                            ?,
                            ?,
                            ?,
                            ?
                        )
                    """, (
                        chat["event_id"],
                        chat["event_id"],
                        chat["source_file"],
                        chat["line_number"],
                        chat["timestamp"],
                        chat["player_name"],
                        chat["raw_player_name"],
                        chat["message"],
                        chat["is_dead"],
                    ))

                    chat_count += 1
                    continue

                admin_action = parse_admin_action(line, log_file.name, line_number)

                if admin_action:
                    cursor.execute("""
                        IF NOT EXISTS (
                            SELECT 1 FROM admin_actions WHERE event_id = ?
                        )
                        INSERT INTO admin_actions (
                            event_id,
                            source_file,
                            line_number,
                            timestamp,
                            admin_name,
                            raw_admin_name,
                            command,
                            command_args,
                            target_name,
                            amount
                        )
                        VALUES (
                            ?,
                            ?,
                            ?,
                            TRY_CONVERT(DATETIME2, ?),
                            ?,
                            ?,
                            ?,
                            ?,
                            ?,
                            ?
                        )
                    """, (
                        admin_action["event_id"],
                        admin_action["event_id"],
                        admin_action["source_file"],
                        admin_action["line_number"],
                        admin_action["timestamp"],
                        admin_action["admin_name"],
                        admin_action["raw_admin_name"],
                        admin_action["command"],
                        admin_action["command_args"],
                        admin_action["target_name"],
                        admin_action["amount"],
                    ))

                    admin_count += 1

        mark_log_file_processed(cursor, log_file.name)

    print(f"Server log import complete.")
    print(f"Processed log files: {len(log_files) - skipped_files}")
    print(f"Skipped log files: {skipped_files}")
    print(f"Stored chat messages: {chat_count}")
    print(f"Stored admin actions: {admin_count}")


client = ApifyClient(os.environ["APIFY_TOKEN"])

run_input = {
    "token": os.environ["DISCORD_TOKEN"],
    "channelInput": "https://discord.com/channels/1232410706230513814/1240609027470131261",
}

run = client.actor("wUoh2wdO7k9mnzL9d").call(run_input=run_input)

for item in client.dataset(run.default_dataset_id).iterate_items():
    update_stats(cursor, item)

import_server_logs(cursor)

os.makedirs("assets/data", exist_ok=True)
for table in [
    "map_playtime",
    "race_playtime",
    "race_levels",
    "chat_messages",
    "admin_actions",
]:
    cursor.execute(f"SELECT * FROM {table}")

    columns = [c[0] for c in cursor.description]

    rows = [
        dict(zip(columns, row))
        for row in cursor.fetchall()
    ]

    with open(f"assets/data/{table}.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str)

conn.commit()
print("Stats updated.")

cursor.close()
conn.close()
