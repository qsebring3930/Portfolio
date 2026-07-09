import os
import re
from pathlib import Path
import time
import json
import hashlib
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone

import paramiko
import time

TOKEN_RE = re.compile(r'"([^"]*)"|([{}])')

CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MULTISPACE_RE = re.compile(r"\s+")
TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ \+00:00)"
)

ISSUED_COMMAND_RE = re.compile(
    r"plugin:CS2-SimpleAdmin .*? (?P<admin>.*?) issued command `(?P<command>[^`]+)`"
)

IGNORED_ADMIN_TARGETS = {
    "",
    "@all",
    "all",
    "@ct",
    "ct",
    "@t",
    "t",
    "@spec",
    "spec",
    "spectator",
    "spectators",
    "me",
    "self",
}


def count_curses(message):
    return len(get_curse_hits(message))

def timed_step(label, func, *args, **kwargs):
    start = time.time()
    print(f"START: {label}")
    result = func(*args, **kwargs)
    print(f"END: {label} took {time.time() - start:.2f}s")
    return result

def download_logs_and_round_backups_from_sftp(include_logs=False):
    """
    Opens ONE SFTP connection, downloads:
      1. newest log-all*.txt files
      2. backup_round00.txt through backup_round20.txt

    Then closes the connection.
    """

    local_logs_dir = Path(__file__).resolve().parent / "logs"
    local_logs_dir.mkdir(parents=True, exist_ok=True)

    backups_dir = local_backups_dir()

    required_sftp_vars = [
        "SFTP_HOST",
        "SFTP_USERNAME",
        "SFTP_PASSWORD",
        "SFTP_REMOTE_LOG_DIR",
    ]

    missing = [name for name in required_sftp_vars if not os.environ.get(name)]

    if missing:
        print(f"Missing SFTP environment variables: {', '.join(missing)}")
        print("Skipping SFTP downloads.")
        return []

    host, port, username, password = get_sftp_connection_parts()

    remote_log_dir = os.environ["SFTP_REMOTE_LOG_DIR"].strip()
    remote_backup_dir = os.environ.get("SFTP_REMOTE_BACKUP_DIR", BACKUP_REMOTE_DIR).strip()

    print(f"Connecting to SFTP host={host!r}, port={port}, user={username!r}")
    print(f"Remote log dir={remote_log_dir!r}")
    print(f"Remote backup dir={remote_backup_dir!r}")

    transport = None
    sftp = None
    downloaded_backup_paths = []

    try:
        print("Opening SFTP transport...")
        transport = paramiko.Transport((host, port))
        transport.banner_timeout = 30
        transport.auth_timeout = 30
        transport.handshake_timeout = 30

        print("Authenticating SFTP...")
        transport.connect(username=username, password=password)

        print("Creating SFTP client...")
        sftp = paramiko.SFTPClient.from_transport(transport)

        # ------------------------------------------------------------
        # Download newest log-all files
        # ------------------------------------------------------------
        if include_logs:
            try:
                print("Listing remote log directory...")
                remote_files = sftp.listdir_attr(remote_log_dir)
                print(f"Found {len(remote_files)} remote log-dir files.")
    
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
                    newest_files = sorted(
                        log_all_files,
                        key=lambda f: f.st_mtime,
                        reverse=True
                    )[:2]
    
                    print("Newest log-all files selected:")
                    for selected_file in newest_files:
                        print(
                            f"  {selected_file.filename} "
                            f"(mtime={selected_file.st_mtime}, size={selected_file.st_size} bytes)"
                        )
    
                    for remote_file in newest_files:
                        file_name = remote_file.filename
                        remote_path = f"{remote_log_dir.rstrip('/')}/{file_name}"
                        local_path = local_logs_dir / file_name
    
                        if local_path.exists() and local_path.stat().st_size == remote_file.st_size:
                            print(f"Skipping log because it already exists locally with same size: {file_name}")
                            skipped += 1
                            continue
    
                        print(f"Downloading log-all file: {file_name} ({remote_file.st_size} bytes)...")
                        sftp.get(remote_path, str(local_path))
                        downloaded += 1
    
                print("Log download complete.")
                print(f"Downloaded logs: {downloaded}")
                print(f"Skipped existing logs: {skipped}")
                print(f"Ignored log-dir files: {ignored}")
    
            except FileNotFoundError:
                print(f"Remote log directory missing, skipping logs: {remote_log_dir}")
        else:
            print("Skipping log-all download because include_logs=False.")

        # ------------------------------------------------------------
        # Download backup_round files
        # ------------------------------------------------------------
        print("Downloading round backup files...")

        for file_name in backup_file_names():
            remote_path = f"{remote_backup_dir.rstrip('/')}/{file_name}"
            local_path = backups_dir / file_name

            try:
                sftp.stat(remote_path)
            except FileNotFoundError:
                print(f"Remote backup missing, skipping: {remote_path}")
                continue

            # Always overwrite local backups.
            # Local same-size files may be stale.
            print(f"Downloading backup: {remote_path} -> {local_path}")
            sftp.get(remote_path, str(local_path))
            downloaded_backup_paths.append(local_path)

        print(f"Round backup download complete. Ready to parse: {len(downloaded_backup_paths)}")

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

    return downloaded_backup_paths

BACKUP_ROUND_MIN = 0
BACKUP_ROUND_MAX = 20
BACKUP_REMOTE_DIR = "/game/csgo"

DATA_DIR = Path(__file__).resolve().parent.parent / "assets" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"Warning: {path} was invalid JSON, using default.")
        return default


def save_json(path, data):
    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    temp_path.replace(path)


def append_jsonl(path, row):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path):
    if not path.exists():
        return []

    rows = []

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return rows

def load_round_backup_snapshots():
    rows = load_json(DATA_DIR / "round_backup_snapshots.json", [])

    return {
        row["snapshot_hash"]: row
        for row in rows
        if row.get("snapshot_hash")
    }


def save_round_backup_snapshots(rows_by_hash):
    rows = list(rows_by_hash.values())

    rows.sort(key=lambda row: (
        row.get("backup_timestamp") or "",
        int(row.get("backup_round") or 0),
    ))

    save_json(DATA_DIR / "round_backup_snapshots.json", rows)


def import_round_backup_snapshots_json(backup_paths):
    snapshots = load_round_backup_snapshots()
    processed_paths = []

    for path in sorted(backup_paths):
        file_name = path.name
        round_number = get_backup_file_round_number(file_name)

        if round_number is None:
            continue

        parsed = parse_keyvalues_file(path)
        data = parsed.get("SaveFile", {})

        if not data:
            print(f"No SaveFile root found, skipping snapshot: {file_name}")
            continue

        snapshot_hash = file_sha256(path)

        if snapshot_hash in snapshots:
            print(f"Skipping already snapshotted backup: {file_name}")
            processed_paths.append(path)
            continue

        snapshots[snapshot_hash] = {
            "snapshot_hash": snapshot_hash,
            "source_file": file_name,
            "backup_round": round_number,
            "map_name": data.get("map"),
            "team1_name": data.get("team1"),
            "team2_name": data.get("team2"),
            "backup_timestamp": data.get("timestamp"),
        }

        print(f"Stored round backup snapshot: {file_name}")
        processed_paths.append(path)

    save_round_backup_snapshots(snapshots)

    print(f"Round backup snapshots stored: {len(snapshots)}")
    return processed_paths

from datetime import timedelta


RTV_PATTERNS_TOTAL = [
    "rtv",
    ".rtv",
    "can we rtv",
    "can we rtv please",
    "change map",
    "change the map",
    "map change",
    "map sucks",
    "map trash",
    "map ass",
    "trash map",
    "lets go to",
]


RTV_PATTERNS_MAP_COMPLAINTS = [
    "rtv",
    ".rtv",
    "can we rtv",
    "can we rtv please",
    "change map",
    "change the map",
    "map change",
]


def parse_timestamp_for_rtv(value):
    if not value:
        return None

    value = str(value).strip().replace("Z", "+00:00")

    parsed = None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        pass

    if parsed is None:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f %z")
        except ValueError:
            return None

    # Normalize everything to naive UTC so comparisons do not crash.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)

    return parsed


def is_rtv_message(message, patterns):
    text = clean_text(message or "").lower()

    if not text:
        return False

    if text in {"rtv", ".rtv"}:
        return True

    return any(pattern in text for pattern in patterns)


def find_active_map_for_timestamp(message_time, snapshots):
    if message_time is None:
        return None

    best_snapshot = None
    best_time = None

    for snapshot in snapshots:
        map_name = snapshot.get("map_name")
        backup_time = parse_timestamp_for_rtv(snapshot.get("backup_timestamp"))

        if not map_name or backup_time is None:
            continue

        if backup_time > message_time:
            continue

        if backup_time < message_time - timedelta(hours=3):
            continue

        if best_time is None or backup_time > best_time:
            best_time = backup_time
            best_snapshot = snapshot

    return best_snapshot.get("map_name") if best_snapshot else None


def update_map_complaint_counts_from_private_logs():
    chat_messages = read_jsonl(DATA_DIR / "_chat_messages.jsonl")
    snapshots = load_json(DATA_DIR / "_round_backup_snapshots.json", [])

    map_complaints = {}

    for chat in chat_messages:
        message = chat.get("message", "")

        if is_rtv_message(message, RTV_PATTERNS_MAP_COMPLAINTS):
            message_time = parse_timestamp_for_rtv(chat.get("timestamp"))
            map_name = find_active_map_for_timestamp(message_time, snapshots)

            if map_name:
                map_complaints[map_name] = map_complaints.get(map_name, 0) + 1

    map_rows = load_json(DATA_DIR / "map_playtime.json", [])

    for row in map_rows:
        map_name = row.get("map_name")
        row["complaint_count"] = map_complaints.get(map_name, row.get("complaint_count", 0))

    save_json(DATA_DIR / "map_playtime.json", map_rows)

    print(f"Updated complaint counts for {len(map_complaints)} maps.")


def iter_backup_players(data):
    for team_side, players in [
        ("team1", data.get("PlayersOnTeam1", {}) or {}),
        ("team2", data.get("PlayersOnTeam2", {}) or {}),
    ]:
        if not isinstance(players, dict):
            continue

        for steam_id, player in players.items():
            if isinstance(player, dict):
                yield team_side, str(steam_id), player


def get_stat_total(player, stat_name):
    match_stats = player.get("MatchStats", {}) or {}
    stat_block = match_stats.get(stat_name, {}) or {}

    if not isinstance(stat_block, dict):
        return 0

    total = 0

    for round_key, raw_value in stat_block.items():
        if get_round_number(round_key) is not None:
            total += to_int(raw_value, 0) or 0

    return total


def get_stat_round(player, stat_name, round_number):
    match_stats = player.get("MatchStats", {}) or {}
    stat_block = match_stats.get(stat_name, {}) or {}

    if not isinstance(stat_block, dict):
        return None

    return to_int(stat_block.get(f"round{round_number}"))


def get_purchase_count(player, def_key):
    purchases = player.get("WeaponPurchases", {}) or {}

    if not isinstance(purchases, dict):
        return 0

    return to_int(purchases.get(def_key), 0) or 0


def get_player_snapshot_values(player):
    return {
        "player_name": clean_export_name(player.get("name")),
        "kills": to_int(player.get("kills"), 0) or 0,
        "deaths": to_int(player.get("deaths"), 0) or 0,
        "assists": to_int(player.get("assists"), 0) or 0,
        "mvps": to_int(player.get("mvps"), 0) or 0,
        "score": to_int(player.get("score"), 0) or 0,
        "damage": get_stat_total(player, "Damage"),
        "pistol_kills": to_int(player.get("kills_weapon_pistol"), 0) or 0,
        "sniper_kills": to_int(player.get("kills_weapon_sniper"), 0) or 0,
        "knife_kills": to_int(player.get("kills_knife"), 0) or 0,
        "taser_kills": to_int(player.get("kills_taser"), 0) or 0,
    }


def positive_delta(current, previous, key):
    return max(0, int(current.get(key, 0) or 0) - int(previous.get(key, 0) or 0))


def backup_file_names():
    return [
        f"backup_round{round_number:02d}.txt"
        for round_number in range(BACKUP_ROUND_MIN, BACKUP_ROUND_MAX + 1)
    ]


def local_backups_dir():
    path = Path(__file__).resolve().parent / "round_backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_sftp_connection_parts():
    required_sftp_vars = [
        "SFTP_HOST",
        "SFTP_USERNAME",
        "SFTP_PASSWORD",
    ]

    missing = [name for name in required_sftp_vars if not os.environ.get(name)]

    if missing:
        raise RuntimeError(f"Missing SFTP environment variables: {', '.join(missing)}")

    host = os.environ["SFTP_HOST"].strip()
    port = int(os.environ.get("SFTP_PORT", "22").strip())

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

    return host, port, os.environ["SFTP_USERNAME"], os.environ["SFTP_PASSWORD"]


def get_backup_file_round_number(file_name):
    match = re.search(r"backup_round(\d+)\.txt$", file_name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def get_backup_group_key(data):
    return (
        data.get("map") or "",
        data.get("team1") or "",
        data.get("team2") or "",
    )


def make_backup_snapshot_match_id(parsed_backups):
    maps = sorted({
        data.get("map") or ""
        for path, file_name, data in parsed_backups
        if data.get("map")
    })

    timestamps = sorted({
        data.get("timestamp") or ""
        for path, file_name, data in parsed_backups
        if data.get("timestamp")
    })

    teams = sorted({
        value
        for path, file_name, data in parsed_backups
        for value in [data.get("team1"), data.get("team2")]
        if value
    })

    file_rounds = sorted({
        get_backup_file_round_number(file_name)
        for path, file_name, data in parsed_backups
        if get_backup_file_round_number(file_name) is not None
    })

    raw = "|".join([
        maps[0] if maps else "",
        timestamps[0] if timestamps else "",
        ",".join(teams),
        str(file_rounds[0]) if file_rounds else "",
    ])

    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def split_parsed_backups_into_match_groups(parsed_backups):
    """
    Split downloaded backup_roundXX files into safe match groups.

    This protects against stale files like:
      backup_round00-03 = new map
      backup_round11-15 = old map

    Those should not share one match_id.
    """

    decorated = []

    for path, file_name, data in parsed_backups:
        file_round = get_backup_file_round_number(file_name)

        if file_round is None:
            continue

        decorated.append((file_round, path, file_name, data))

    decorated.sort(key=lambda row: row[0])

    groups = []
    current_group = []
    current_key = None
    previous_round = None

    for file_round, path, file_name, data in decorated:
        group_key = get_backup_group_key(data)

        should_start_new_group = False

        if not current_group:
            should_start_new_group = True
        elif group_key != current_key:
            should_start_new_group = True
        elif previous_round is not None and file_round != previous_round + 1:
            should_start_new_group = True

        if should_start_new_group:
            if current_group:
                groups.append(current_group)

            current_group = []
            current_key = group_key

        current_group.append((path, file_name, data))
        previous_round = file_round

    if current_group:
        groups.append(current_group)

    return groups


def file_sha256(path):
    digest = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def parse_keyvalues_file(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    tokens = []

    for match in TOKEN_RE.finditer(text):
        if match.group(1) is not None:
            tokens.append(("string", match.group(1)))
        else:
            tokens.append((match.group(2), match.group(2)))

    index = 0

    def parse_object():
        nonlocal index
        obj = {}

        while index < len(tokens):
            token_type, token_value = tokens[index]

            if token_type == "}":
                index += 1
                break

            if token_type != "string":
                index += 1
                continue

            key = token_value
            index += 1

            if index < len(tokens) and tokens[index][0] == "{":
                index += 1
                obj[key] = parse_object()
            elif index < len(tokens) and tokens[index][0] == "string":
                obj[key] = tokens[index][1]
                index += 1
            else:
                obj[key] = None

        return obj

    if not tokens:
        return {}

    root_key_type, root_key = tokens[index]
    index += 1

    if index < len(tokens) and tokens[index][0] == "{":
        index += 1
        return {root_key: parse_object()}

    return {root_key: None}


def to_int(value, default=None):
    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_round_number(round_key):
    match = re.search(r"round(\d+)", str(round_key))
    return int(match.group(1)) if match else None


def get_def_index(def_key):
    match = re.search(r"DefIndex_(\d+)", str(def_key))
    return int(match.group(1)) if match else None


PLAYER_BASE_FIELDS = {
    "kills": "kills",
    "assists": "assists",
    "deaths": "deaths",
    "mvps": "mvps",
    "score": "score",
    "cash": "cash",
    "enemyKs": "enemy_kills",
    "enemyHSs": "enemy_headshots",
    "enemyKAg": "enemy_kag",
    "kills_weapon_pistol": "pistol_kills",
    "kills_weapon_sniper": "sniper_kills",
    "kills_knife": "knife_kills",
    "kills_taser": "taser_kills",
    "enemyDamageDealt": "enemy_damage_dealt",
}

NEEDED_ROUND_STATS = {
    "Damage",
    "CashEarned",
    "MoneySaved",
    "KillReward",
}










def import_server_logs_json():
    logs_dir = Path(__file__).resolve().parent / "logs"

    if not logs_dir.exists():
        print(f"No logs folder found at {logs_dir}, skipping server log import.")
        return []

    log_files = sorted(logs_dir.glob("log-all*.txt"))

    if not log_files:
        print(f"No log-all*.txt logs found in {logs_dir}, skipping server log import.")
        return []

    processed_path = DATA_DIR / "_processed_log_files.json"
    processed_log_files = set(load_json(processed_path, []))

    chat_path = DATA_DIR / "_chat_messages.jsonl"
    admin_path = DATA_DIR / "_admin_actions.jsonl"

    existing_chat_ids = {
        row.get("event_id")
        for row in read_jsonl(chat_path)
        if row.get("event_id")
    }

    existing_admin_ids = {
        row.get("event_id")
        for row in read_jsonl(admin_path)
        if row.get("event_id")
    }

    processed_files_to_delete = []
    chat_count = 0
    admin_count = 0
    skipped_files = 0

    for log_file in log_files:
        if log_file.name in processed_log_files:
            skipped_files += 1
            print(f"Skipping already processed log: {log_file.name}")
            append_log_for_deletion(processed_files_to_delete, log_file)
            continue

        print(f"Processing log file: {log_file.name}")

        for line_number, line in iter_log_entries(log_file):
            chat = parse_chat_line(line, log_file.name, line_number)

            if chat:
                if chat["event_id"] not in existing_chat_ids:
                    append_jsonl(chat_path, chat)
                    existing_chat_ids.add(chat["event_id"])

                    add_to_counter_rows(
                        DATA_DIR / "top_chatters.json",
                        "player_name",
                        "message_count",
                        chat["player_name"],
                        1
                    )

                    add_curse_message_public(
                        chat["player_name"],
                        chat["message"]
                    )

                    if is_rtv_message(chat["message"], RTV_PATTERNS_TOTAL):
                        increment_single_value_json(
                            DATA_DIR / "rtv_requests.json",
                            "rtv_count",
                            1
                        )

                    chat_count += 1

                continue

            admin_action = parse_admin_action(line, log_file.name, line_number)

            if admin_action:
                if admin_action["event_id"] not in existing_admin_ids:
                    append_jsonl(admin_path, admin_action)
                    existing_admin_ids.add(admin_action["event_id"])

                    command = clean_text(admin_action.get("command") or "").lower()

                    if command == "css_slay" and admin_action.get("target_name"):
                        add_admin_target_counter_public(
                            DATA_DIR / "most_slain_players.json",
                            "slain_count",
                            admin_action["target_name"],
                            1
                        )

                    if command == "css_slap" and admin_action.get("target_name"):
                        add_admin_target_counter_public(
                            DATA_DIR / "most_slapped_players.json",
                            "slapped_count",
                            admin_action["target_name"],
                            1,
                            extra_updates={
                                "total_slap_damage": int(admin_action.get("amount") or 0)
                            }
                        )

                    add_admin_command_usage_public(
                        admin_action["admin_name"],
                        command
                    )

                    admin_count += 1

        processed_log_files.add(log_file.name)
        append_log_for_deletion(processed_files_to_delete, log_file)

    save_json(processed_path, sorted(processed_log_files))

    print("Server log JSON import complete.")
    print(f"Processed log files: {len(processed_files_to_delete)}")
    print(f"Skipped log files: {skipped_files}")
    print(f"Stored chat messages: {chat_count}")
    print(f"Stored admin actions: {admin_count}")

    return processed_files_to_delete

ITEM_PRICES = {
    # pistols
    1: 700,    # Desert Eagle
    2: 300,    # Dual Berettas
    3: 500,    # Five-SeveN
    4: 200,    # Glock-18
    30: 500,   # Tec-9
    32: 200,   # P2000
    36: 300,   # P250
    61: 200,   # USP-S
    63: 500,   # CZ75-Auto
    64: 600,   # R8 Revolver

    # rifles
    7: 2700,   # AK-47
    8: 3300,   # AUG
    10: 1950,  # FAMAS
    13: 1800,  # Galil AR
    16: 2900,  # M4A4
    39: 3000,  # SG 553
    60: 2900,  # M4A1-S

    # snipers
    9: 4750,   # AWP
    11: 5000,  # G3SG1
    38: 5000,  # SCAR-20
    40: 1700,  # SSG 08

    # mid-tier / SMGs / heavy
    14: 5200,  # M249
    17: 1050,  # MAC-10
    19: 2350,  # P90
    23: 1400,  # MP5-SD
    24: 1200,  # UMP-45
    25: 2000,  # XM1014
    26: 1400,  # PP-Bizon
    27: 1300,  # MAG-7
    28: 5000,  # Negev
    29: 1100,  # Sawed-Off
    33: 1400,  # MP7
    34: 1250,  # MP9
    35: 1050,  # Nova

    # grenades
    43: 200,   # Flashbang
    44: 300,   # HE Grenade
    45: 300,   # Smoke Grenade
    46: 400,   # Molotov
    47: 50,    # Decoy Grenade
    48: 500,   # Incendiary Grenade

    # equipment
    31: 200,   # Zeus x27
    50: 650,   # Kevlar Vest
    51: 1000,  # Kevlar Vest + Helmet
    55: 400,   # Defuse Kit
    57: 0,     # Healthshot
}

def load_dict_json(path, key_field):
    rows = load_json(path, [])
    return {
        str(row[key_field]): row
        for row in rows
        if row.get(key_field) is not None
    }


def save_dict_json(path, rows_by_key, sort_field):
    rows = list(rows_by_key.values())
    rows.sort(key=lambda row: str(row.get(sort_field, "")).lower())
    save_json(path, rows)


def positive_delta_values(current_value, previous_value):
    return max(0, int(current_value or 0) - int(previous_value or 0))


def get_player_round_values(player):
    return {
        "player_name": clean_export_name(player.get("name")),
        "kills": to_int(player.get("kills"), 0) or 0,
        "deaths": to_int(player.get("deaths"), 0) or 0,
        "assists": to_int(player.get("assists"), 0) or 0,
        "mvps": to_int(player.get("mvps"), 0) or 0,
        "score": to_int(player.get("score"), 0) or 0,
        "damage": get_stat_total(player, "Damage"),
        "pistol_kills": to_int(player.get("kills_weapon_pistol"), 0) or 0,
        "sniper_kills": to_int(player.get("kills_weapon_sniper"), 0) or 0,
        "knife_kills": to_int(player.get("kills_knife"), 0) or 0,
        "taser_kills": to_int(player.get("kills_taser"), 0) or 0,
    }


def iter_weapon_kill_fields(player):
    for key, value in player.items():
        if key.startswith("kills_weapon_"):
            weapon = key.replace("kills_weapon_", "")
            yield weapon, to_int(value, 0) or 0

    yield "knife", to_int(player.get("kills_knife"), 0) or 0
    yield "taser", to_int(player.get("kills_taser"), 0) or 0


def get_purchase_delta_money(current_player, previous_player):
    current_purchases = current_player.get("WeaponPurchases", {}) or {}
    previous_purchases = previous_player.get("WeaponPurchases", {}) or {}

    money_spent = 0
    defuse_delta = 0

    if not isinstance(current_purchases, dict):
        return 0, 0

    for def_key, raw_current in current_purchases.items():
        def_index = get_def_index(def_key)

        if def_index is None:
            continue

        current_count = to_int(raw_current, 0) or 0
        previous_count = to_int(previous_purchases.get(def_key), 0) or 0
        purchase_delta = current_count - previous_count

        if purchase_delta <= 0:
            continue

        money_spent += purchase_delta * ITEM_PRICES.get(def_index, 0)

        if def_index == 55:
            defuse_delta += purchase_delta

    return money_spent, defuse_delta

def import_round_backup_aggregates_json(backup_paths):
    processed_hashes_path = DATA_DIR / "_processed_round_backup_hashes.json"
    processed_hashes = set(load_json(processed_hashes_path, []))

    round_snapshots = load_dict_json(
        DATA_DIR / "_round_backup_snapshots.json",
        "snapshot_hash"
    )

    player_agg = load_dict_json(
        DATA_DIR / "_round_player_aggregate.json",
        "steam_id"
    )

    money_agg = load_dict_json(
        DATA_DIR / "_round_money_aggregate.json",
        "steam_id"
    )

    weapon_agg = load_json(DATA_DIR / "_weapon_kills_aggregate.json", [])
    weapon_agg_by_key = {
        f"{row.get('weapon')}|{row.get('player_name')}": row
        for row in weapon_agg
    }

    parsed_backups = []
    processed_paths = []

    for path in sorted(backup_paths):
        file_name = path.name
        round_number = get_backup_file_round_number(file_name)

        if round_number is None:
            continue

        parsed = parse_keyvalues_file(path)
        data = parsed.get("SaveFile", {})

        if not data:
            print(f"No SaveFile root found, skipping: {file_name}")
            continue

        parsed_backups.append((path, file_name, data))

    if not parsed_backups:
        print("No parsed round backups for JSON aggregate import.")
        return []

    backup_groups = split_parsed_backups_into_match_groups(parsed_backups)

    print(f"JSON aggregate backup groups: {len(backup_groups)}")

    for backup_group in backup_groups:
        group_rows = []

        for path, file_name, data in backup_group:
            round_number = get_backup_file_round_number(file_name)

            if round_number is None:
                continue

            group_rows.append((round_number, path, file_name, data))

        group_rows.sort(key=lambda row: row[0])

        previous_players = {}

        for round_number, path, file_name, data in group_rows:
            snapshot_hash = file_sha256(path)

            current_players = {}

            for team_side, steam_id, player in iter_backup_players(data):
                current_players[(team_side, steam_id)] = player

            # Always store map snapshot if new.
            if snapshot_hash not in round_snapshots:
                round_snapshots[snapshot_hash] = {
                    "snapshot_hash": snapshot_hash,
                    "source_file": file_name,
                    "backup_round": round_number,
                    "map_name": data.get("map"),
                    "team1_name": data.get("team1"),
                    "team2_name": data.get("team2"),
                    "backup_timestamp": data.get("timestamp"),
                }

            # If already aggregated, keep as baseline only.
            if snapshot_hash in processed_hashes:
                print(f"Skipping already aggregated backup snapshot: {file_name}")
                previous_players = current_players
                processed_paths.append(path)
                continue

            print(f"Aggregating backup snapshot JSON: {file_name}")

            round_results = data.get("RoundResults", {}) or {}
            alive_t = data.get("PlayersAliveT", {}) or {}
            alive_ct = data.get("PlayersAliveCT", {}) or {}

            result_code = to_int(round_results.get(f"round{round_number}"))
            players_alive_t = to_int(alive_t.get(f"round{round_number}"))
            players_alive_ct = to_int(alive_ct.get(f"round{round_number}"))
            map_name = data.get("map") or ""

            for key, current_player in current_players.items():
                team_side, steam_id = key
                previous_player = previous_players.get(key)

                # Round 0 / first seen snapshot is baseline only.
                if not previous_player:
                    continue

                current_values = get_player_round_values(current_player)
                previous_values = get_player_round_values(previous_player)
                player_name = current_values["player_name"]

                if not player_name:
                    continue

                deltas = {
                    field: positive_delta_values(
                        current_values.get(field),
                        previous_values.get(field),
                    )
                    for field in [
                        "kills",
                        "deaths",
                        "assists",
                        "mvps",
                        "score",
                        "damage",
                        "pistol_kills",
                        "sniper_kills",
                        "knife_kills",
                        "taser_kills",
                    ]
                }

                player_row = player_agg.setdefault(steam_id, {
                    "steam_id": steam_id,
                    "player_name": player_name,
                    "kills": 0,
                    "deaths": 0,
                    "assists": 0,
                    "mvps": 0,
                    "score": 0,
                    "damage": 0,
                    "rounds_played": 0,
                    "pistol_kills": 0,
                    "sniper_kills": 0,
                    "knife_kills": 0,
                    "taser_kills": 0,
                })

                player_row["player_name"] = player_name

                for field, delta in deltas.items():
                    player_row[field] = int(player_row.get(field) or 0) + delta

                player_row["rounds_played"] = int(player_row.get("rounds_played") or 0) + 1
                add_player_stat_deltas_public(player_name, steam_id, deltas)

                # Weapon kill deltas.
                previous_weapon_counts = dict(iter_weapon_kill_fields(previous_player))

                for weapon, current_count in iter_weapon_kill_fields(current_player):
                    previous_count = previous_weapon_counts.get(weapon, 0)
                    kill_delta = positive_delta_values(current_count, previous_count)

                    if kill_delta <= 0:
                        continue

                    weapon_key = f"{weapon}|{player_name}"

                    weapon_row = weapon_agg_by_key.setdefault(weapon_key, {
                        "weapon": weapon,
                        "player_name": player_name,
                        "kills": 0,
                    })

                    weapon_row["kills"] = int(weapon_row.get("kills") or 0) + kill_delta
                    add_weapon_kill_public(weapon, player_name, steam_id, kill_delta)

                # Money / defuse / betting.
                money_spent, defuse_delta = get_purchase_delta_money(
                    current_player,
                    previous_player,
                )

                start_cash = get_stat_round(current_player, "MoneySaved", round_number)
                actual_cash = get_stat_round(current_player, "CashEarned", round_number)
                kill_reward = get_stat_round(current_player, "KillReward", round_number) or 0

                round_won = None

                if players_alive_t is not None and players_alive_ct is not None:
                    if team_side == "team1":
                        if players_alive_t > players_alive_ct:
                            round_won = 1
                        elif players_alive_t < players_alive_ct:
                            round_won = 0
                    elif team_side == "team2":
                        if players_alive_ct > players_alive_t:
                            round_won = 1
                        elif players_alive_ct < players_alive_t:
                            round_won = 0

                base_income = 0

                if map_name.startswith("de_") and round_won == 1:
                    base_income = 2700
                elif map_name.startswith("cs_") and round_won == 1 and team_side == "team1":
                    base_income = 2000
                elif map_name.startswith("cs_") and round_won == 1 and team_side == "team2":
                    base_income = 2300
                elif round_won == 0:
                    base_income = 2400

                objective_bonus = 0

                if (
                    map_name.startswith("de_")
                    and round_won == 0
                    and team_side == "team1"
                    and result_code == 3
                ):
                    objective_bonus = 200

                betting_delta = 0

                if start_cash is not None and actual_cash is not None:
                    expected_cash = start_cash - money_spent + base_income + objective_bonus + kill_reward
                    expected_cash = min(MONEY_CAP, expected_cash)
                    betting_delta = actual_cash - expected_cash

                money_row = money_agg.setdefault(steam_id, {
                    "steam_id": steam_id,
                    "player_name": player_name,
                    "money_spent": 0,
                    "net_betting": 0,
                    "betting_won": 0,
                    "betting_lost": 0,
                    "defuse_kits_bought": 0,
                    "defuse_rounds_played": 0,
                })

                money_row["player_name"] = player_name
                money_row["money_spent"] = int(money_row.get("money_spent") or 0) + money_spent
                money_row["net_betting"] = int(money_row.get("net_betting") or 0) + betting_delta
                money_row["betting_won"] = int(money_row.get("betting_won") or 0) + max(0, betting_delta)
                money_row["betting_lost"] = int(money_row.get("betting_lost") or 0) + abs(min(0, betting_delta))
                money_row["defuse_kits_bought"] = int(money_row.get("defuse_kits_bought") or 0) + defuse_delta
                money_row["defuse_rounds_played"] = int(money_row.get("defuse_rounds_played") or 0) + 1
    
                add_money_deltas_public(
                    player_name,
                    steam_id,
                    money_spent,
                    betting_delta,
                    defuse_delta
                )

            processed_hashes.add(snapshot_hash)
            previous_players = current_players
            processed_paths.append(path)

    save_json(processed_hashes_path, sorted(processed_hashes))

    save_dict_json(
        DATA_DIR / "_round_backup_snapshots.json",
        round_snapshots,
        "backup_timestamp",
    )

    save_dict_json(
        DATA_DIR / "_round_player_aggregate.json",
        player_agg,
        "player_name",
    )

    save_dict_json(
        DATA_DIR / "_round_money_aggregate.json",
        money_agg,
        "player_name",
    )

    weapon_rows = list(weapon_agg_by_key.values())
    weapon_rows.sort(key=lambda row: int(row.get("kills") or 0), reverse=True)
    save_json(DATA_DIR / "_weapon_kills_aggregate.json", weapon_rows)

    return processed_paths









def delete_imported_round_backups(backup_files):
    if not backup_files:
        print("No imported round backups to delete.")
        return

    deleted = 0

    for backup_file in backup_files:
        try:
            if backup_file.exists() and re.match(r"backup_round\d{2}\.txt$", backup_file.name):
                print(f"Deleting imported round backup: {backup_file}")
                backup_file.unlink()
                deleted += 1
        except Exception as e:
            print(f"Failed to delete backup file {backup_file}: {type(e).__name__}: {e}")

    print(f"Deleted imported round backups: {deleted}")

MONEY_CAP = 30000
MAX_BET_AMOUNT = 10000

def get_unique_touched_rounds(touched_rounds):
    unique = {}

    for row in touched_rounds or []:
        match_id = row.get("match_id")
        round_number = row.get("round_number")

        if not match_id or round_number is None:
            continue

        unique[(match_id, int(round_number))] = {
            "match_id": match_id,
            "round_number": int(round_number),
        }

    return list(unique.values())

def clean_export_name(name):
    if not name:
        return ""

    name = clean_text(name)
    name = re.sub(r"^\s*-\s*", "", name)
    name = re.sub(r"\s*-\s*$", "", name)
    return clean_text(name)

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

    # Remove dangling separator dashes like:
    # "Princess Ben -"
    # "- Princess Ben"
    name = re.sub(r"^\s*-\s*", "", name)
    name = re.sub(r"\s*-\s*$", "", name)

    # Collapse whitespace again after cleanup
    name = clean_text(name)

    return name


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

def delete_imported_logs(log_files):
    if not log_files:
        print("No imported log files to delete.")
        return

    deleted = 0

    for log_file in log_files:
        try:
            if log_file.exists():
                print(f"Deleting imported log file: {log_file}")
                log_file.unlink()
                deleted += 1
        except Exception as e:
            print(f"Failed to delete log file {log_file}: {type(e).__name__}: {e}")

    print(f"Deleted imported log files: {deleted}")

def append_log_for_deletion(processed_files_to_delete, log_file):
    if not log_file.exists():
        print(f"Not marking for deletion because file no longer exists: {log_file}")
        return

    if not log_file.is_file():
        print(f"Not marking for deletion because path is not a file: {log_file}")
        return

    if not log_file.name.startswith("log-all") or not log_file.name.endswith(".txt"):
        print(f"Not marking for deletion because it is not a log-all txt file: {log_file}")
        return

    print(f"Marking log for deletion after SQL commit: {log_file.name}")
    processed_files_to_delete.append(log_file)

def iter_log_entries(log_file):
    current_start_line = None
    current_parts = []

    with log_file.open("r", encoding="utf-8", errors="replace") as f:
        for physical_line_number, raw_line in enumerate(f, start=1):
            line = raw_line.rstrip("\r\n")

            # New real log entry starts with timestamp.
            if TIMESTAMP_RE.match(line):
                if current_parts:
                    combined = " ".join(
                        part.strip()
                        for part in current_parts
                        if part.strip()
                    )
                    yield current_start_line, combined

                current_start_line = physical_line_number
                current_parts = [line]
            else:
                # Continuation line. This catches split chat messages like:
                # timestamp/plugin line
                # *DEAD*
                # Player: message
                if current_parts:
                    current_parts.append(line)

        if current_parts:
            combined = " ".join(
                part.strip()
                for part in current_parts
                if part.strip()
            )
            yield current_start_line, combined

def write_json(path, rows):
    with path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str)

def get_name_tokens(name):
    name = clean_export_name(name).lower()
    return [
        token
        for token in re.split(r"[^a-z0-9]+", name)
        if token
    ]

def normalize_name_for_match(name):
    if not name:
        return ""

    name = clean_export_name(name)
    name = name.lower()
    name = re.sub(r"[^a-z0-9]+", "", name)
    return name.strip()


def names_are_probably_same(a, b):
    a_clean = clean_export_name(a)
    b_clean = clean_export_name(b)

    a_norm = normalize_name_for_match(a_clean)
    b_norm = normalize_name_for_match(b_clean)

    if not a_norm or not b_norm:
        return False

    if a_norm == b_norm:
        return True

    shorter = min(a_norm, b_norm, key=len)
    longer = max(a_norm, b_norm, key=len)

    longer_original = a_clean if len(a_norm) >= len(b_norm) else b_clean
    longer_tokens = get_name_tokens(longer_original)

    # Never fuzzy merge tiny names.
    # Only allow 3-letter names if they are an exact token in a longer multi-word name.
    if len(shorter) <= 3:
        return shorter in longer_tokens and len(longer_tokens) >= 2

    # Exact token match:
    # "ben" -> "Princess Ben"
    # "cum" -> "cum kisses"
    # "princess" -> "Princess Ben"
    if shorter in longer_tokens:
        return True

    # Prefix match only for 4+ chars:
    # "logi" -> "logickal"
    # "logic" -> "logickal"
    # NOT random internal substring matches.
    if len(shorter) >= 4 and longer.startswith(shorter):
        return True

    # Fuzzy match only for reasonably long names.
    if len(shorter) >= 6:
        ratio = SequenceMatcher(None, a_norm, b_norm).ratio()
        if ratio >= 0.88:
            return True

    return False


def choose_best_name(names):
    best = max(names, key=lambda name: len(normalize_name_for_match(name)))
    return clean_export_name(best)


def consolidate_player_rows(rows, count_field, extra_sum_fields=None):
    if extra_sum_fields is None:
        extra_sum_fields = []

    groups = []

    for row in rows:
        player_name = clean_export_name(row.get("player_name"))

        if not player_name:
            continue

        matched_group = None

        for group in groups:
            canonical = choose_best_name(group["aliases"])

            if names_are_probably_same(player_name, canonical):
                matched_group = group
                break

        if matched_group is None:
            matched_group = {
                "aliases": set(),
                count_field: 0,
            }

            for field in extra_sum_fields:
                matched_group[field] = 0

            groups.append(matched_group)

        matched_group["aliases"].add(player_name)
        matched_group[count_field] += int(row.get(count_field) or 0)

        for field in extra_sum_fields:
            matched_group[field] += int(row.get(field) or 0)

    consolidated = []

    for group in groups:
        aliases = sorted(
            {
                clean_export_name(name)
                for name in group["aliases"]
                if clean_export_name(name)
            },
            key=lambda name: normalize_name_for_match(name),
        )

        output = {
            "player_name": choose_best_name(aliases),
            count_field: group[count_field],
            "aliases": aliases,
        }

        for field in extra_sum_fields:
            output[field] = group[field]

        consolidated.append(output)

    consolidated.sort(key=lambda row: row[count_field], reverse=True)

    return consolidated

CURSE_PATTERNS = [
    # fuck variants
    ("fuck", r"\bf+u+c+k+\w*\b"),
    ("fuck", r"\bf+u+k+\w*\b"),
    ("fuck", r"\bf+ck+\w*\b"),
    ("fuck", r"\bf+\*+k+\w*\b"),
    ("wtf", r"\bwtf+\b"),
    ("mf", r"\bmf+\b"),
    ("motherfucker", r"\bmotherfucker\w*\b"),
    ("mofo", r"\bmofo\w*\b"),

    # shit variants
    ("shit", r"\bs+h+i+t+\w*\b"),
    ("shit", r"\bs+h+\*+t+\w*\b"),
    ("shat", r"\bshat\b"),
    ("shit", r"\bshitty\b"),
    ("shit", r"\bshithead\w*\b"),
    ("shit", r"\bshitbag\w*\b"),
    ("bullshit", r"\bbullshit+\w*\b"),
    ("bullshit", r"\bbullshitting\b"),

    # ass variants
    ("asshole", r"\basshole\w*\b"),
    ("asshat", r"\basshat\w*\b"),
    ("asswipe", r"\basswipe\w*\b"),
    ("dumbass", r"\bdumbass\w*\b"),
    ("badass", r"\bbadass\b"),
    ("ass", r"\bass\b"),

    # bitch variants
    ("bitch", r"\bb+i+t+c+h+\w*\b"),
    ("bitch", r"\bb+\*+t+c+h+\w*\b"),
    ("bitch", r"\bbish+\w*\b"),
    ("bitch", r"\bbitchass\w*\b"),

    # dick/cock/balls/etc
    ("dick", r"\bd+i+c+k+\w*\b"),
    ("dick", r"\bdickhead\w*\b"),
    ("cock", r"\bcock\w*\b"),
    ("ballsack", r"\bballsack\w*\b"),
    ("nutsack", r"\bnutsack\w*\b"),
    ("prick", r"\bprick\w*\b"),

    # pussy/cunt/etc
    ("pussy", r"\bp+u+s+s+y+\w*\b"),
    ("cunt", r"\bc+u+n+t+\w*\b"),
    ("twat", r"\btwat\w*\b"),

    # damn/hell
    ("damn", r"\bdamn+\w*\b"),
    ("goddamn", r"\bgoddamn+\w*\b"),
    ("hell", r"\bhell\b"),
    ("hella", r"\bhella\b"),

    # piss
    ("piss", r"\bpiss+\w*\b"),

    # cum
    ("cum", r"\bc+u+m+\w*\b"),

    # bastard
    ("bastard", r"\bbastard\w*\b"),

    # common gaming rage stuff
    ("dogshit", r"\bdogshit\b"),
    ("shit", r"\bshitcan\w*\b"),

    # retard variants
    ("retard", r"\br+e+t+a+r+d+\w*\b"),
    ("retard", r"\br+t+a+r+d+\w*\b"),
]

CURSE_REGEXES = [
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in CURSE_PATTERNS
]


def get_curse_hits(message):
    hits = []

    if not message:
        return hits

    for label, regex in CURSE_REGEXES:
        matches = list(regex.finditer(message))

        for _ in matches:
            hits.append(label)

    return hits


def normalize_target_name(name):
    if not name:
        return ""

    name = clean_export_name(name)
    name = name.strip()

    return name


def score_target_against_player(target_name, player_profile):
    target_clean = normalize_target_name(target_name)
    target_norm = normalize_name_for_match(target_clean)

    if not target_norm:
        return None

    player_norm = player_profile["normalized"]
    player_tokens = player_profile["tokens"]

    if not player_norm:
        return None

    # Exact full-name match.
    if target_norm == player_norm:
        return 100

    # Exact token inside a multi-word real username.
    # Examples:
    # ben -> Princess Ben
    # princess -> Princess Ben
    # cum -> cum kisses
    if len(target_norm) >= 3 and len(player_tokens) >= 2 and target_norm in player_tokens:
        return 120 + len(player_norm) / 100

    # Prefix match for single-word names.
    # Examples:
    # logi -> Logickal
    # logic -> Logickal
    # logick -> Logickal
    if len(target_norm) >= 4 and player_norm.startswith(target_norm):
        return 90 + len(target_norm)

    # Conservative fuzzy match for longer names only.
    if len(target_norm) >= 6:
        ratio = SequenceMatcher(None, target_norm, player_norm).ratio()

        if ratio >= 0.90:
            return 80 + (ratio * 10)

    return None


def resolve_target_to_chat_player(target_name, player_profiles):
    target_clean = normalize_target_name(target_name)

    if not target_clean:
        return None

    if target_clean.lower() in IGNORED_ADMIN_TARGETS:
        return None

    best_profile = None
    best_score = None

    for profile in player_profiles:
        score = score_target_against_player(target_clean, profile)

        if score is None:
            continue

        if best_score is None:
            best_profile = profile
            best_score = score
            continue

        # Tie-breakers:
        # 1. higher match score
        # 2. longer real username
        # 3. more chat messages
        if (
            score > best_score
            or (
                score == best_score
                and len(profile["normalized"]) > len(best_profile["normalized"])
            )
            or (
                score == best_score
                and len(profile["normalized"]) == len(best_profile["normalized"])
                and profile["message_count"] > best_profile["message_count"]
            )
        ):
            best_profile = profile
            best_score = score

    if best_profile:
        return best_profile["player_name"]

    # No safe match found. Keep the cleaned admin target as its own name.
    return target_clean


def consolidate_admin_target_rows(rows, player_profiles, count_field, extra_sum_fields=None):
    if extra_sum_fields is None:
        extra_sum_fields = []

    grouped = {}

    for row in rows:
        raw_name = row.get("player_name")
        clean_name = normalize_target_name(raw_name)

        if not clean_name:
            continue

        if clean_name.lower() in IGNORED_ADMIN_TARGETS:
            continue

        resolved_name = resolve_target_to_chat_player(clean_name, player_profiles)

        if not resolved_name:
            continue

        key = normalize_name_for_match(resolved_name)

        if key not in grouped:
            grouped[key] = {
                "player_name": resolved_name,
                count_field: 0,
                "aliases": set(),
            }

            for field in extra_sum_fields:
                grouped[key][field] = 0

        grouped[key][count_field] += int(row.get(count_field) or 0)
        grouped[key]["aliases"].add(clean_name)

        if clean_name != resolved_name:
            grouped[key]["aliases"].add(resolved_name)

        for field in extra_sum_fields:
            grouped[key][field] += int(row.get(field) or 0)

    output_rows = []

    for group in grouped.values():
        output = {
            "player_name": group["player_name"],
            count_field: group[count_field],
            "aliases": sorted(group["aliases"], key=lambda name: normalize_name_for_match(name)),
        }

        for field in extra_sum_fields:
            output[field] = group[field]

        output_rows.append(output)

    output_rows.sort(key=lambda row: row[count_field], reverse=True)

    return output_rows

def load_processed_log_files():
    return set(load_json(DATA_DIR / "processed_log_files.json", []))


def save_processed_log_files(processed):
    save_json(DATA_DIR / "processed_log_files.json", sorted(processed))

def add_to_counter_rows(path, name_field, count_field, name, amount=1):
    rows = load_json(path, [])
    clean_name = clean_export_name(name)

    if not clean_name:
        return

    found = None

    for row in rows:
        existing_name = clean_export_name(row.get(name_field))

        if existing_name.lower() == clean_name.lower():
            found = row
            break

    if found is None:
        found = {
            name_field: clean_name,
            count_field: 0,
        }
        rows.append(found)

    found[count_field] = int(found.get(count_field) or 0) + amount

    rows.sort(key=lambda row: int(row.get(count_field) or 0), reverse=True)
    save_json(path, rows)


def increment_single_value_json(path, field, amount=1):
    row = load_json(path, {})
    row[field] = int(row.get(field) or 0) + amount
    save_json(path, row)

def add_to_named_multi_counter(path, name_field, name, updates):
    rows = load_json(path, [])
    clean_name = clean_export_name(name)

    if not clean_name:
        return

    found = None

    for row in rows:
        existing_name = clean_export_name(row.get(name_field))

        if existing_name.lower() == clean_name.lower():
            found = row
            break

    if found is None:
        found = {name_field: clean_name}
        rows.append(found)

    for field, amount in updates.items():
        found[field] = int(found.get(field) or 0) + int(amount or 0)

    first_field = next(iter(updates.keys()))
    rows.sort(key=lambda row: int(row.get(first_field) or 0), reverse=True)

    save_json(path, rows)


def add_admin_command_usage_public(admin_name, command):
    rows = load_json(DATA_DIR / "admin_command_usage.json", [])

    clean_admin = clean_export_name(admin_name)
    command = clean_text(command or "").lower()

    if not clean_admin or not command:
        return

    found = None

    for row in rows:
        if (
            clean_export_name(row.get("admin_name")).lower() == clean_admin.lower()
            and clean_text(row.get("command") or "").lower() == command
        ):
            found = row
            break

    if found is None:
        found = {
            "admin_name": clean_admin,
            "command": command,
            "command_count": 0,
        }
        rows.append(found)

    found["command_count"] = int(found.get("command_count") or 0) + 1

    rows.sort(key=lambda row: int(row.get("command_count") or 0), reverse=True)
    save_json(DATA_DIR / "admin_command_usage.json", rows)
  


# ----------------------------------------------------------------------
# Public JSON updates matching the old SQL export file formats.
# These are intentionally named/written to the same files as the SQL exporter:
#   top_chatters.json
#   top_curse_users.json
#   most_slain_players.json
#   most_slapped_players.json
#   admin_command_usage.json
#   round_scoreboard.json
#   round_top_weapon_category_kills.json
#   round_player_money_summary.json
#   round_defuse_purchase_rate.json
# ----------------------------------------------------------------------

def build_player_profiles_from_top_chatters():
    rows = load_json(DATA_DIR / "top_chatters.json", [])
    profiles_by_name = {}

    for row in rows:
        clean_name = clean_export_name(row.get("player_name"))
        if not clean_name:
            continue

        key = normalize_name_for_match(clean_name)
        if not key:
            continue

        if key not in profiles_by_name:
            profiles_by_name[key] = {
                "player_name": clean_name,
                "normalized": key,
                "tokens": get_name_tokens(clean_name),
                "message_count": 0,
            }

        profiles_by_name[key]["message_count"] += int(row.get("message_count") or 0)

        if len(clean_name) > len(profiles_by_name[key]["player_name"]):
            profiles_by_name[key]["player_name"] = clean_name
            profiles_by_name[key]["tokens"] = get_name_tokens(clean_name)

    return list(profiles_by_name.values())


def normalize_matched_words(value):
    if isinstance(value, dict):
        return {
            str(word): int(count or 0)
            for word, count in value.items()
        }

    if isinstance(value, list):
        out = {}
        for row in value:
            if not isinstance(row, dict):
                continue
            word = row.get("word")
            if not word:
                continue
            out[str(word)] = out.get(str(word), 0) + int(row.get("count") or 0)
        return out

    return {}


def add_curse_message_public(player_name, message):
    hits = get_curse_hits(message)
    if not hits:
        return

    rows = load_json(DATA_DIR / "top_curse_users.json", [])
    clean_name = clean_export_name(player_name)

    if not clean_name:
        return

    found = None

    for row in rows:
        if clean_export_name(row.get("player_name")).lower() == clean_name.lower():
            found = row
            break

    if found is None:
        found = {
            "player_name": clean_name,
            "curse_count": 0,
            "curse_messages": 0,
            "matched_words": [],
        }
        rows.append(found)

    matched_words = normalize_matched_words(found.get("matched_words"))
    found["curse_count"] = int(found.get("curse_count") or 0) + len(hits)
    found["curse_messages"] = int(found.get("curse_messages") or 0) + 1

    for hit in hits:
        matched_words[hit] = matched_words.get(hit, 0) + 1

    found["matched_words"] = [
        {"word": word, "count": count}
        for word, count in sorted(
            matched_words.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]

    rows.sort(key=lambda row: int(row.get("curse_count") or 0), reverse=True)
    save_json(DATA_DIR / "top_curse_users.json", rows)


def add_admin_target_counter_public(path, count_field, target_name, amount=1, extra_updates=None):
    if extra_updates is None:
        extra_updates = {}

    clean_target = normalize_target_name(target_name)

    if not clean_target or clean_target.lower() in IGNORED_ADMIN_TARGETS:
        return

    player_profiles = build_player_profiles_from_top_chatters()
    resolved_name = resolve_target_to_chat_player(clean_target, player_profiles)

    if not resolved_name:
        return

    rows = load_json(path, [])
    resolved_key = normalize_name_for_match(resolved_name)
    target_key = normalize_name_for_match(clean_target)

    found = None

    for row in rows:
        row_name = clean_export_name(row.get("player_name"))
        row_key = normalize_name_for_match(row_name)
        aliases = row.get("aliases") or []
        alias_keys = {normalize_name_for_match(alias) for alias in aliases}

        if row_key == resolved_key or target_key in alias_keys or resolved_key in alias_keys:
            found = row
            break

    if found is None:
        found = {
            "player_name": resolved_name,
            count_field: 0,
            "aliases": [],
        }
        rows.append(found)

    aliases = {
        clean_export_name(alias)
        for alias in (found.get("aliases") or [])
        if clean_export_name(alias)
    }
    aliases.add(clean_target)
    aliases.add(resolved_name)

    found["player_name"] = resolved_name
    found[count_field] = int(found.get(count_field) or 0) + int(amount or 0)
    found["aliases"] = sorted(aliases, key=lambda name: normalize_name_for_match(name))

    for field, value in extra_updates.items():
        found[field] = int(found.get(field) or 0) + int(value or 0)

    rows.sort(key=lambda row: int(row.get(count_field) or 0), reverse=True)
    save_json(path, rows)


def add_player_stat_deltas_public(player_name, steam_id, deltas):
    rows = load_json(DATA_DIR / "round_scoreboard.json", [])
    clean_name = clean_export_name(player_name)

    if not clean_name:
        return

    found = None

    for row in rows:
        if str(row.get("steam_id") or "") == str(steam_id):
            found = row
            break

    if found is None:
        found = {
            "player_name": clean_name,
            "steam_id": str(steam_id),
            "kills": 0,
            "assists": 0,
            "deaths": 0,
            "mvps": 0,
            "score": 0,
            "damage": 0,
            "rounds_played": 0,
            "adr": 0,
            "kd_ratio": 0,
            "kad_ratio": 0,
        }
        rows.append(found)

    found["player_name"] = clean_name
    found["steam_id"] = str(steam_id)

    for field in ["kills", "assists", "deaths", "mvps", "score", "damage"]:
        found[field] = int(found.get(field) or 0) + int(deltas.get(field, 0) or 0)

    found["rounds_played"] = int(found.get("rounds_played") or 0) + 1

    kills = int(found.get("kills") or 0)
    deaths = int(found.get("deaths") or 0)
    assists = int(found.get("assists") or 0)
    damage = int(found.get("damage") or 0)
    rounds_played = int(found.get("rounds_played") or 0)

    found["adr"] = round(damage / rounds_played, 2) if rounds_played else 0
    found["kd_ratio"] = round(kills / deaths, 2) if deaths else kills
    found["kad_ratio"] = round((kills + assists) / deaths, 2) if deaths else kills + assists

    rows.sort(
        key=lambda row: (
            int(row.get("kills") or 0),
            int(row.get("damage") or 0),
        ),
        reverse=True,
    )
    save_json(DATA_DIR / "round_scoreboard.json", rows)


def weapon_to_old_category(weapon):
    weapon = str(weapon or "").strip().lower()

    mapping = {
        "pistol": "Pistol",
        "sniper": "Sniper",
        "knife": "Knife",
        "taser": "Taser",
    }

    return mapping.get(weapon)


def add_weapon_kill_public(weapon, player_name, steam_id, kills):
    if kills <= 0:
        return

    weapon_category = weapon_to_old_category(weapon)
    clean_name = clean_export_name(player_name)

    if not weapon_category or not clean_name:
        return

    rows = load_json(DATA_DIR / "round_top_weapon_category_kills.json", [])
    found = None

    for row in rows:
        if (
            str(row.get("steam_id") or "") == str(steam_id)
            and str(row.get("weapon_category") or "") == weapon_category
        ):
            found = row
            break

    if found is None:
        found = {
            "weapon_category": weapon_category,
            "player_name": clean_name,
            "steam_id": str(steam_id),
            "kills": 0,
        }
        rows.append(found)

    found["player_name"] = clean_name
    found["steam_id"] = str(steam_id)
    found["weapon_category"] = weapon_category
    found["kills"] = int(found.get("kills") or 0) + int(kills)

    rows.sort(key=lambda row: int(row.get("kills") or 0), reverse=True)
    save_json(DATA_DIR / "round_top_weapon_category_kills.json", rows)


def add_money_deltas_public(player_name, steam_id, money_spent, betting_delta, defuse_delta):
    clean_name = clean_export_name(player_name)

    if not clean_name:
        return

    # round_player_money_summary.json
    money_rows = load_json(DATA_DIR / "round_player_money_summary.json", [])
    money_found = None

    for row in money_rows:
        if str(row.get("steam_id") or "") == str(steam_id):
            money_found = row
            break

    if money_found is None:
        money_found = {
            "player_name": clean_name,
            "steam_id": str(steam_id),
            "money_spent": 0,
            "net_betting": 0,
            "betting_won": 0,
            "betting_lost": 0,
        }
        money_rows.append(money_found)

    money_found["player_name"] = clean_name
    money_found["steam_id"] = str(steam_id)
    money_found["money_spent"] = int(money_found.get("money_spent") or 0) + int(money_spent or 0)
    money_found["net_betting"] = int(money_found.get("net_betting") or 0) + int(betting_delta or 0)

    if betting_delta > 0:
        money_found["betting_won"] = int(money_found.get("betting_won") or 0) + int(betting_delta)
    elif betting_delta < 0:
        money_found["betting_lost"] = int(money_found.get("betting_lost") or 0) + abs(int(betting_delta))

    money_rows.sort(key=lambda row: int(row.get("net_betting") or 0), reverse=True)
    save_json(DATA_DIR / "round_player_money_summary.json", money_rows)

    # round_defuse_purchase_rate.json
    defuse_rows = load_json(DATA_DIR / "round_defuse_purchase_rate.json", [])
    defuse_found = None

    for row in defuse_rows:
        if str(row.get("steam_id") or "") == str(steam_id):
            defuse_found = row
            break

    if defuse_found is None:
        defuse_found = {
            "player_name": clean_name,
            "steam_id": str(steam_id),
            "defuse_kits_bought": 0,
            "rounds_played": 0,
            "defuse_purchase_rate": 0,
        }
        defuse_rows.append(defuse_found)

    defuse_found["player_name"] = clean_name
    defuse_found["steam_id"] = str(steam_id)
    defuse_found["defuse_kits_bought"] = int(defuse_found.get("defuse_kits_bought") or 0) + int(defuse_delta or 0)
    defuse_found["rounds_played"] = int(defuse_found.get("rounds_played") or 0) + 1

    rounds_played = int(defuse_found.get("rounds_played") or 0)
    kits = int(defuse_found.get("defuse_kits_bought") or 0)
    defuse_found["defuse_purchase_rate"] = round((kits / rounds_played) * 100, 2) if rounds_played else 0

    defuse_rows.sort(
        key=lambda row: (
            float(row.get("defuse_purchase_rate") or 0),
            int(row.get("defuse_kits_bought") or 0),
        ),
        reverse=True,
    )
    save_json(DATA_DIR / "round_defuse_purchase_rate.json", defuse_rows)

if __name__ == "__main__":
    include_logs = os.environ.get("INCLUDE_LOGS", "0").strip() == "1"
    print(f"INCLUDE_LOGS={include_logs}", flush=True)

    backup_paths = timed_step(
        "SFTP download logs/backups",
        download_logs_and_round_backups_from_sftp,
        include_logs=include_logs
    )

    if include_logs:
        processed_log_files = timed_step(
            "Import server logs JSON",
            import_server_logs_json
        )
    else:
        print("Skipping server log import because INCLUDE_LOGS is not 1.", flush=True)
        processed_log_files = []

    processed_backup_files = timed_step(
        "Import round backup aggregates JSON",
        import_round_backup_aggregates_json,
        backup_paths
    )

    timed_step(
        "Update map complaint counts",
        update_map_complaint_counts_from_private_logs
    )

    timed_step("Delete imported logs", delete_imported_logs, processed_log_files)
    timed_step("Delete imported round backups", delete_imported_round_backups, processed_backup_files)
