import os
import pyodbc
import re
from pathlib import Path
import time
import json
import hashlib

import paramiko


def download_logs_from_sftp():
    local_logs_dir = Path(__file__).resolve().parent / "logs"
    local_logs_dir.mkdir(parents=True, exist_ok=True)

    required_sftp_vars = [
        "SFTP_HOST",
        "SFTP_USERNAME",
        "SFTP_PASSWORD",
        "SFTP_REMOTE_LOG_DIR",
    ]

    missing = [name for name in required_sftp_vars if not os.environ.get(name)]

    if missing:
        print(f"Missing SFTP environment variables: {', '.join(missing)}")
        print("Skipping SFTP log download.")
        return

    host = os.environ["SFTP_HOST"].strip()
    port = int(os.environ.get("SFTP_PORT", "22").strip())

    # Allow accidental full URL.
    host = host.replace("sftp://", "").replace("ssh://", "")

    if "@" in host:
        host = host.split("@", 1)[1]

    if "/" in host:
        host = host.split("/", 1)[0]

    if ":" in host and host.count(":") == 1:
        host_part, port_part = host.rsplit(":", 1)
        if port_part.isdigit():
            host = host_part
            port = int(port_part)

    username = os.environ["SFTP_USERNAME"]
    password = os.environ["SFTP_PASSWORD"]
    remote_log_dir = os.environ["SFTP_REMOTE_LOG_DIR"]

    print(f"Connecting to SFTP host={host!r}, port={port}, user={username!r}")
    print(f"Remote log dir={remote_log_dir!r}")

    transport = None
    sftp = None

    try:
        print("Opening SFTP transport...")
        transport = paramiko.Transport((host, port))

        # This prevents infinite stalls.
        transport.banner_timeout = 30
        transport.auth_timeout = 30
        transport.handshake_timeout = 30

        print("Authenticating SFTP...")
        transport.connect(username=username, password=password)

        print("Creating SFTP client...")
        sftp = paramiko.SFTPClient.from_transport(transport)

        print("Listing remote log directory...")
        remote_files = sftp.listdir_attr(remote_log_dir)

        print(f"Found {len(remote_files)} remote files.")

        wanted_prefixes = (
            "log-all",
        )

        downloaded = 0
        skipped = 0
        ignored = 0

        for remote_file in remote_files:
            log_all_files = [
                remote_file
                for remote_file in remote_files
                if remote_file.filename.endswith(".txt")
                and remote_file.filename.startswith("log-all")
            ]
            
            ignored = len(remote_files) - len(log_all_files)
            downloaded = 0
            skipped = 0
            
            if not log_all_files:
                print("No log-all*.txt files found on SFTP.")
            else:
                newest_file = max(log_all_files, key=lambda f: f.st_mtime)
            
                file_name = newest_file.filename
                remote_path = f"{remote_log_dir.rstrip('/')}/{file_name}"
                local_path = local_logs_dir / file_name
            
                print(f"Newest log-all file is: {file_name}")
                print(f"Remote modified time: {newest_file.st_mtime}")
                print(f"Remote size: {newest_file.st_size} bytes")
            
                if local_path.exists() and local_path.stat().st_size == newest_file.st_size:
                    print(f"Skipping newest file because it already exists locally with same size: {file_name}")
                    skipped += 1
                else:
                    print(f"Downloading newest log-all file: {file_name} ({newest_file.st_size} bytes)...")
                    sftp.get(remote_path, str(local_path))
                    downloaded += 1

        print("SFTP download complete.")
        print(f"Downloaded: {downloaded}")
        print(f"Skipped existing: {skipped}")
        print(f"Ignored: {ignored}")

    except Exception as e:
        print(f"SFTP download failed: {type(e).__name__}: {e}")
        raise

    finally:
        if sftp:
            print("Closing SFTP client...")
            sftp.close()

        if transport:
            print("Closing SFTP transport...")
            transport.close()

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
    logs_dir = Path(__file__).resolve().parent / "logs"

    if not logs_dir.exists():
        print(f"No logs folder found at {logs_dir}, skipping server log import.")
        return

    log_files = sorted(logs_dir.glob("log-all*.txt"))

    if not log_files:
        print(f"No log-all*.txt logs found in {logs_dir}, skipping server log import.")
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

    print("Server log import complete.")
    print(f"Logs folder: {logs_dir}")
    print(f"Processed log files: {len(log_files) - skipped_files}")
    print(f"Skipped log files: {skipped_files}")
    print(f"Stored chat messages: {chat_count}")
    print(f"Stored admin actions: {admin_count}")

def write_json(path, rows):
    with path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str)


def rows_to_dicts(cursor):
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def export_log_jsons(cursor):
    data_dir = Path(__file__).resolve().parent.parent / "assets" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"Exporting log JSON files to {data_dir}")

    # Top chatters
    cursor.execute("""
        SELECT TOP 50
            player_name,
            COUNT(*) AS message_count
        FROM chat_messages
        GROUP BY player_name
        ORDER BY message_count DESC;
    """)
    write_json(data_dir / "top_chatters.json", rows_to_dicts(cursor))

    # Top curse users.
    # Add/remove words here however you want.
    cursor.execute("""
        SELECT TOP 50
            player_name,
            COUNT(*) AS curse_count
        FROM chat_messages
        WHERE
            LOWER(message) LIKE '%fuck%'
            OR LOWER(message) LIKE '%shit%'
            OR LOWER(message) LIKE '%bitch%'
            OR LOWER(message) LIKE '%asshole%'
            OR LOWER(message) LIKE '%damn%'
            OR LOWER(message) LIKE '%cunt%'
            OR LOWER(message) LIKE '%dick%'
            OR LOWER(message) LIKE '%pussy%'
        GROUP BY player_name
        ORDER BY curse_count DESC;
    """)
    write_json(data_dir / "top_curse_users.json", rows_to_dicts(cursor))

    # Most slain players
    cursor.execute("""
        SELECT TOP 50
            target_name AS player_name,
            COUNT(*) AS slain_count
        FROM admin_actions
        WHERE command = 'css_slay'
          AND target_name IS NOT NULL
          AND target_name <> ''
        GROUP BY target_name
        ORDER BY slain_count DESC;
    """)
    write_json(data_dir / "most_slain_players.json", rows_to_dicts(cursor))

    # Most slapped players
    cursor.execute("""
        SELECT TOP 50
            target_name AS player_name,
            COUNT(*) AS slapped_count,
            SUM(COALESCE(amount, 0)) AS total_slap_damage
        FROM admin_actions
        WHERE command = 'css_slap'
          AND target_name IS NOT NULL
          AND target_name <> ''
        GROUP BY target_name
        ORDER BY slapped_count DESC;
    """)
    write_json(data_dir / "most_slapped_players.json", rows_to_dicts(cursor))

    # Optional: admin command leaderboard
    cursor.execute("""
        SELECT TOP 50
            admin_name,
            command,
            COUNT(*) AS command_count
        FROM admin_actions
        GROUP BY admin_name, command
        ORDER BY command_count DESC;
    """)
    write_json(data_dir / "admin_command_usage.json", rows_to_dicts(cursor))

    print("Finished exporting log JSON files.")

if __name__ == "__main__":
    # download_logs_from_sftp()

    print("Testing Azure SQL connection...")
    conn = connect_with_retry()
    cursor = conn.cursor()

    cursor.execute("SELECT @@VERSION")
    row = cursor.fetchone()

    print("Connected successfully!")
    print(row[0][:200])

    import_server_logs(cursor)
    conn.commit()

    export_log_jsons(cursor)

    cursor.close()
    conn.close()

    print("Imported logs into SQL and exported JSON files.")
