import os
import pyodbc
import re
from pathlib import Path
import time
import json
import hashlib
from difflib import SequenceMatcher

import paramiko

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


def find_existing_match_id_for_backup_group(cursor, backup_group):
    """
    If this is a stale ending chunk from a match we already partially imported,
    attach it to the existing match_id instead of creating a new one.

    Example:
      earlier run imported backup_round00-10
      later run sees stale backup_round11-15
      this links 11-15 back to the same match_id.
    """

    file_rounds = [
        get_backup_file_round_number(file_name)
        for path, file_name, data in backup_group
        if get_backup_file_round_number(file_name) is not None
    ]

    timestamps = [
        data.get("timestamp")
        for path, file_name, data in backup_group
        if data.get("timestamp")
    ]

    if not file_rounds or not timestamps:
        return None

    min_file_round = min(file_rounds)
    min_timestamp = min(timestamps)

    first_data = backup_group[0][2]
    map_name = first_data.get("map")
    team1_name = first_data.get("team1")
    team2_name = first_data.get("team2")

    # If this group starts at round 0, it is almost certainly a new match.
    if min_file_round == 0:
        return None

    cursor.execute("""
        SELECT TOP 1
            match_id
        FROM round_backup_matches
        WHERE map_name = ?
          AND COALESCE(team1_name, '') = COALESCE(?, '')
          AND COALESCE(team2_name, '') = COALESCE(?, '')
          AND current_round < ?
          AND backup_timestamp <= TRY_CONVERT(DATETIME2, ?)
          AND DATEDIFF(
                MINUTE,
                backup_timestamp,
                TRY_CONVERT(DATETIME2, ?)
              ) BETWEEN 0 AND 180
        ORDER BY backup_timestamp DESC, current_round DESC;
    """, (
        map_name,
        team1_name,
        team2_name,
        min_file_round,
        min_timestamp,
        min_timestamp,
    ))

    row = cursor.fetchone()
    return row[0] if row else None


def get_match_id_for_backup_group(cursor, backup_group):
    existing_match_id = find_existing_match_id_for_backup_group(cursor, backup_group)

    if existing_match_id:
        print(f"Attaching stale backup group to existing match_id: {existing_match_id}")
        return existing_match_id

    new_match_id = make_backup_snapshot_match_id(backup_group)
    print(f"Created new backup match_id: {new_match_id}")
    return new_match_id


def already_processed_round_backup_file(cursor, match_id, file_name):
    cursor.execute("""
        SELECT 1
        FROM processed_round_backup_files
        WHERE match_id = ?
          AND file_name = ?
    """, (
        match_id,
        file_name,
    ))

    return cursor.fetchone() is not None


def mark_round_backup_file_processed(cursor, match_id, file_name, file_size, file_hash):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1
            FROM processed_round_backup_files
            WHERE match_id = ?
              AND file_name = ?
        )
        INSERT INTO processed_round_backup_files (
            match_id,
            file_name,
            file_size,
            file_hash
        )
        VALUES (?, ?, ?, ?)
    """, (
        match_id,
        file_name,
        match_id,
        file_name,
        file_size,
        file_hash,
    ))


def file_sha256(path):
    digest = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def download_round_backups_from_sftp(cursor):
    backups_dir = local_backups_dir()
    remote_dir = os.environ.get("SFTP_REMOTE_BACKUP_DIR", BACKUP_REMOTE_DIR).strip()

    host, port, username, password = get_sftp_connection_parts()

    print(f"Connecting to SFTP host={host!r}, port={port}, user={username!r}")
    print(f"Remote backup dir={remote_dir!r}")

    transport = None
    sftp = None
    downloaded_paths = []

    try:
        transport = paramiko.Transport((host, port))
        transport.banner_timeout = 30
        transport.auth_timeout = 30
        transport.handshake_timeout = 30
        transport.connect(username=username, password=password)

        sftp = paramiko.SFTPClient.from_transport(transport)

        for file_name in backup_file_names():
            remote_path = f"{remote_dir.rstrip('/')}/{file_name}"
            local_path = backups_dir / file_name

            try:
                sftp.stat(remote_path)
            except FileNotFoundError:
                print(f"Remote backup missing, skipping: {remote_path}")
                continue

            print(f"Downloading backup: {remote_path} -> {local_path}")
            sftp.get(remote_path, str(local_path))
            downloaded_paths.append(local_path)

    finally:
        if sftp:
            sftp.close()

        if transport:
            transport.close()

    print(f"Round backup download complete. Ready to parse: {len(downloaded_paths)}")
    return downloaded_paths


TOKEN_RE = re.compile(r'"([^"]*)"|([{}])')


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
    "roundsWon": "rounds_won",
    "enemyKs": "enemy_kills",
    "enemyHSs": "enemy_headshots",
    "enemy2Ks": "enemy_2ks",
    "enemy3Ks": "enemy_3ks",
    "enemy4Ks": "enemy_4ks",
    "enemy5Ks": "enemy_5ks",
    "enemyKAg": "enemy_kag",
    "firstKs": "first_kills",
    "clutchKs": "clutch_kills",
    "kills_weapon_pistol": "pistol_kills",
    "kills_weapon_sniper": "sniper_kills",
    "kills_knife": "knife_kills",
    "kills_taser": "taser_kills",
    "enemyDamageDealt": "enemy_damage_dealt",
    "helmet": "helmet",
}


def insert_round_backup_match(cursor, match_id, source_file, data):
    history = data.get("History", {}) or {}
    first_half = data.get("FirstHalfScore", {}) or {}

    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1
            FROM round_backup_matches
            WHERE match_id = ?
              AND source_file = ?
        )
        INSERT INTO round_backup_matches (
            match_id,
            source_file,
            backup_timestamp,
            map_name,
            current_round,
            team1_name,
            team2_name,
            first_half_team1_score,
            first_half_team2_score,
            loser_most_recent_team,
            consecutive_t_loses,
            consecutive_ct_loses,
            spawn_points_cfg
        )
        VALUES (?, ?, TRY_CONVERT(DATETIME2, ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        match_id,
        source_file,
        match_id,
        source_file,
        data.get("timestamp"),
        data.get("map"),
        to_int(data.get("round")),
        data.get("team1"),
        data.get("team2"),
        to_int(first_half.get("team1")),
        to_int(first_half.get("team2")),
        history.get("LoserMostRecentTeam"),
        to_int(history.get("NumConsecutiveTerroristLoses")),
        to_int(history.get("NumConsecutiveCTLoses")),
        to_int(history.get("SpawnPointsCfg")),
    ))


def insert_round_backup_rounds(cursor, match_id, source_file, data):
    round_results = data.get("RoundResults", {}) or {}
    alive_t = data.get("PlayersAliveT", {}) or {}
    alive_ct = data.get("PlayersAliveCT", {}) or {}
    current_round = to_int(data.get("round"), 0) or 0

    for round_number in range(1, 31):
        key = f"round{round_number}"
        players_alive_t = to_int(alive_t.get(key))
        players_alive_ct = to_int(alive_ct.get(key))

        if round_number > current_round:
            players_alive_t = None
            players_alive_ct = None

        cursor.execute("""
            IF NOT EXISTS (
                SELECT 1
                FROM round_backup_rounds
                WHERE match_id = ?
                  AND source_file = ?
                  AND round_number = ?
            )
            INSERT INTO round_backup_rounds (
                match_id,
                source_file,
                round_number,
                result_code,
                players_alive_t,
                players_alive_ct
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            match_id,
            source_file,
            round_number,
            match_id,
            source_file,
            round_number,
            to_int(round_results.get(key)),
            players_alive_t,
            players_alive_ct,
        ))


def insert_round_backup_player(cursor, match_id, source_file, team_side, steam_id, player):
    values = {
        sql_field: to_int(player.get(raw_field))
        for raw_field, sql_field in PLAYER_BASE_FIELDS.items()
    }

    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1
            FROM round_backup_players
            WHERE match_id = ?
              AND source_file = ?
              AND team_side = ?
              AND steam_id = ?
        )
        INSERT INTO round_backup_players (
            match_id, source_file, team_side, steam_id, player_name,
            kills, assists, deaths, mvps, score, cash, rounds_won,
            enemy_kills, enemy_headshots, enemy_2ks, enemy_3ks, enemy_4ks,
            enemy_5ks, enemy_kag, first_kills, clutch_kills, pistol_kills,
            sniper_kills, knife_kills, taser_kills, enemy_damage_dealt, helmet
        )
        VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?
        )
    """, (
        match_id,
        source_file,
        team_side,
        steam_id,
        match_id,
        source_file,
        team_side,
        steam_id,
        player.get("name"),
        values["kills"],
        values["assists"],
        values["deaths"],
        values["mvps"],
        values["score"],
        values["cash"],
        values["rounds_won"],
        values["enemy_kills"],
        values["enemy_headshots"],
        values["enemy_2ks"],
        values["enemy_3ks"],
        values["enemy_4ks"],
        values["enemy_5ks"],
        values["enemy_kag"],
        values["first_kills"],
        values["clutch_kills"],
        values["pistol_kills"],
        values["sniper_kills"],
        values["knife_kills"],
        values["taser_kills"],
        values["enemy_damage_dealt"],
        values["helmet"],
    ))


def insert_round_backup_match_stats(cursor, match_id, source_file, team_side, steam_id, player):
    match_stats = player.get("MatchStats", {}) or {}

    for stat_name, stat_block in match_stats.items():
        if not isinstance(stat_block, dict):
            continue

        if stat_name == "Totals":
            for total_name, raw_value in stat_block.items():
                value = to_int(raw_value)
                if value is None:
                    continue

                cursor.execute("""
                    IF NOT EXISTS (
                        SELECT 1
                        FROM round_backup_player_totals
                        WHERE match_id = ?
                          AND source_file = ?
                          AND team_side = ?
                          AND steam_id = ?
                          AND stat_name = ?
                    )
                    INSERT INTO round_backup_player_totals (
                        match_id, source_file, team_side, steam_id, stat_name, stat_value
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    match_id,
                    source_file,
                    team_side,
                    steam_id,
                    total_name,
                    match_id,
                    source_file,
                    team_side,
                    steam_id,
                    total_name,
                    value,
                ))

            continue

        for round_key, raw_value in stat_block.items():
            round_number = get_round_number(round_key)
            value = to_int(raw_value)

            if round_number is None or value is None:
                continue

            cursor.execute("""
                IF NOT EXISTS (
                    SELECT 1
                    FROM round_backup_player_round_stats
                    WHERE match_id = ?
                      AND source_file = ?
                      AND team_side = ?
                      AND steam_id = ?
                      AND stat_name = ?
                      AND round_number = ?
                )
                INSERT INTO round_backup_player_round_stats (
                    match_id, source_file, team_side, steam_id, stat_name, round_number, stat_value
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                match_id,
                source_file,
                team_side,
                steam_id,
                stat_name,
                round_number,
                match_id,
                source_file,
                team_side,
                steam_id,
                stat_name,
                round_number,
                value,
            ))


def insert_round_backup_weapon_purchases(cursor, match_id, source_file, team_side, steam_id, player):
    purchases = player.get("WeaponPurchases", {}) or {}

    if not isinstance(purchases, dict):
        return

    for def_key, raw_count in purchases.items():
        def_index = get_def_index(def_key)
        purchase_count = to_int(raw_count)

        if def_index is None or purchase_count is None:
            continue

        cursor.execute("""
            IF NOT EXISTS (
                SELECT 1
                FROM round_backup_weapon_purchases
                WHERE match_id = ?
                  AND source_file = ?
                  AND team_side = ?
                  AND steam_id = ?
                  AND def_index = ?
            )
            INSERT INTO round_backup_weapon_purchases (
                match_id, source_file, team_side, steam_id, def_index, purchase_count
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            match_id,
            source_file,
            team_side,
            steam_id,
            def_index,
            match_id,
            source_file,
            team_side,
            steam_id,
            def_index,
            purchase_count,
        ))


def insert_round_backup_players(cursor, match_id, source_file, data):
    team_blocks = [
        ("team1", data.get("PlayersOnTeam1", {}) or {}),
        ("team2", data.get("PlayersOnTeam2", {}) or {}),
    ]

    for team_side, players in team_blocks:
        if not isinstance(players, dict):
            continue

        for steam_id, player in players.items():
            if not isinstance(player, dict):
                continue

            insert_round_backup_player(cursor, match_id, source_file, team_side, steam_id, player)
            insert_round_backup_match_stats(cursor, match_id, source_file, team_side, steam_id, player)
            insert_round_backup_weapon_purchases(cursor, match_id, source_file, team_side, steam_id, player)


def import_round_backups(cursor, backup_paths):
    imported = 0
    skipped = 0
    processed_paths = []
    parsed_backups = []
    touched_rounds = []

    for path in sorted(backup_paths):
        file_name = path.name
        parsed = parse_keyvalues_file(path)
        data = parsed.get("SaveFile", {})

        if not data:
            print(f"No SaveFile root found, skipping: {file_name}")
            continue

        parsed_backups.append((path, file_name, data))

    if not parsed_backups:
        print("No parsed round backups found.")
        return [], []

    backup_groups = split_parsed_backups_into_match_groups(parsed_backups)

    print(f"Detected backup match groups: {len(backup_groups)}")

    for group_index, backup_group in enumerate(backup_groups, start=1):
        file_names = [file_name for path, file_name, data in backup_group]
        group_map = backup_group[0][2].get("map")

        print(f"Backup group {group_index}: map={group_map!r}, files={file_names}")

        match_id = get_match_id_for_backup_group(cursor, backup_group)

        for path, file_name, data in backup_group:
            if already_processed_round_backup_file(cursor, match_id, file_name):
                print(f"Skipping already parsed backup: {match_id} / {file_name}")
                skipped += 1
                continue

            round_number = get_backup_file_round_number(file_name)

            print(f"Parsing round backup: {match_id} / {file_name}")

            insert_round_backup_match(cursor, match_id, file_name, data)
            insert_round_backup_rounds(cursor, match_id, file_name, data)
            insert_round_backup_players(cursor, match_id, file_name, data)

            mark_round_backup_file_processed(
                cursor,
                match_id,
                file_name,
                path.stat().st_size,
                file_sha256(path),
            )

            if round_number is not None:
                touched_rounds.append({
                    "match_id": match_id,
                    "source_file": file_name,
                    "round_number": round_number,
                })

            imported += 1
            processed_paths.append(path)

    print("Round backup import complete.")
    print(f"Imported backups: {imported}")
    print(f"Skipped backups: {skipped}")
    print(f"Touched backup rounds: {len(touched_rounds)}")

    return processed_paths, touched_rounds

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


def create_touched_rounds_temp_table(cursor, touched_rounds, include_previous_for_betting=False):
    touched_rounds = get_unique_touched_rounds(touched_rounds)

    rows = []

    for row in touched_rounds:
        rows.append((row["match_id"], row["round_number"]))

        if include_previous_for_betting and row["round_number"] > 0:
            rows.append((row["match_id"], row["round_number"] - 1))

    rows = sorted(set(rows), key=lambda row: (row[0], row[1]))

    cursor.execute("""
        IF OBJECT_ID('tempdb..#touched_rounds') IS NOT NULL
            DROP TABLE #touched_rounds;
    """)

    cursor.execute("""
        CREATE TABLE #touched_rounds (
            match_id NVARCHAR(128) NOT NULL,
            round_number INT NOT NULL,
            PRIMARY KEY (match_id, round_number)
        );
    """)

    if rows:
        cursor.executemany("""
            INSERT INTO #touched_rounds (
                match_id,
                round_number
            )
            VALUES (?, ?);
        """, rows)

    return rows

def rebuild_round_purchase_deltas(cursor, touched_rounds):
    print("Rebuilding touched round purchase deltas...")

    rows = create_touched_rounds_temp_table(cursor, touched_rounds)

    if not rows:
        print("No touched rounds for purchase delta rebuild.")
        return

    cursor.execute("""
        DELETE target
        FROM round_backup_purchase_deltas target
        INNER JOIN #touched_rounds touched
          ON touched.match_id = target.match_id
         AND touched.round_number = target.round_number;
    """)

    cursor.execute("""
        WITH current_purchases AS (
            SELECT
                wp.match_id,
                wp.source_file,
                CAST(REPLACE(REPLACE(wp.source_file, 'backup_round', ''), '.txt', '') AS INT) AS round_number,
                wp.team_side,
                wp.steam_id,
                wp.def_index,
                wp.purchase_count
            FROM round_backup_weapon_purchases wp
            INNER JOIN #touched_rounds touched
              ON touched.match_id = wp.match_id
             AND touched.round_number = CAST(REPLACE(REPLACE(wp.source_file, 'backup_round', ''), '.txt', '') AS INT)
            WHERE wp.source_file LIKE 'backup_round%.txt'
        ),
        previous_purchases AS (
            SELECT
                wp.match_id,
                CAST(REPLACE(REPLACE(wp.source_file, 'backup_round', ''), '.txt', '') AS INT) AS round_number,
                wp.team_side,
                wp.steam_id,
                wp.def_index,
                wp.purchase_count
            FROM round_backup_weapon_purchases wp
            WHERE wp.source_file LIKE 'backup_round%.txt'
        ),
        deltas AS (
            SELECT
                current_purchases.match_id,
                current_purchases.source_file,
                current_purchases.round_number,
                current_purchases.team_side,
                current_purchases.steam_id,
                current_purchases.def_index,
                current_purchases.purchase_count - previous_purchases.purchase_count AS purchase_delta
            FROM current_purchases
            INNER JOIN previous_purchases
              ON previous_purchases.match_id = current_purchases.match_id
             AND previous_purchases.round_number = current_purchases.round_number - 1
             AND previous_purchases.team_side = current_purchases.team_side
             AND previous_purchases.steam_id = current_purchases.steam_id
             AND previous_purchases.def_index = current_purchases.def_index
            WHERE current_purchases.purchase_count > previous_purchases.purchase_count
        )
        INSERT INTO round_backup_purchase_deltas (
            match_id,
            source_file,
            round_number,
            team_side,
            steam_id,
            def_index,
            purchase_delta,
            estimated_spent
        )
        SELECT
            deltas.match_id,
            deltas.source_file,
            deltas.round_number,
            deltas.team_side,
            deltas.steam_id,
            deltas.def_index,
            deltas.purchase_delta,
            deltas.purchase_delta * COALESCE(item_definition_prices.price, 0) AS estimated_spent
        FROM deltas
        LEFT JOIN item_definition_prices
          ON item_definition_prices.def_index = deltas.def_index;
    """)

    print("Touched round purchase deltas rebuilt.")


def rebuild_round_player_economy(cursor, touched_rounds):
    print("Rebuilding touched per-round player economy...")

    rows = create_touched_rounds_temp_table(cursor, touched_rounds)

    if not rows:
        print("No touched rounds for player economy rebuild.")
        return

    cursor.execute("""
        DELETE target
        FROM round_backup_player_economy_rounds target
        INNER JOIN #touched_rounds touched
          ON touched.match_id = target.match_id
         AND touched.round_number = target.round_number;
    """)

    cursor.execute("""
        WITH player_rounds AS (
            SELECT
                prs.match_id,
                prs.source_file,
                prs.team_side,
                prs.steam_id,
                p.player_name,
                prs.round_number,
                MAX(CASE WHEN prs.stat_name = 'CashEarned' THEN prs.stat_value END) AS cash_earned,
                MAX(CASE WHEN prs.stat_name = 'MoneySaved' THEN prs.stat_value END) AS money_saved,
                MAX(CASE WHEN prs.stat_name = 'EquipmentValue' THEN prs.stat_value END) AS equipment_value,
                MAX(CASE WHEN prs.stat_name = 'KillReward' THEN prs.stat_value END) AS kill_reward
            FROM round_backup_player_round_stats prs
            INNER JOIN #touched_rounds touched
              ON touched.match_id = prs.match_id
             AND touched.round_number = prs.round_number
            LEFT JOIN round_backup_players p
              ON p.match_id = prs.match_id
             AND p.source_file = prs.source_file
             AND p.team_side = prs.team_side
             AND p.steam_id = prs.steam_id
            WHERE prs.round_number = CAST(REPLACE(REPLACE(prs.source_file, 'backup_round', ''), '.txt', '') AS INT)
            GROUP BY
                prs.match_id,
                prs.source_file,
                prs.team_side,
                prs.steam_id,
                p.player_name,
                prs.round_number
        ),
        spent AS (
            SELECT
                d.match_id,
                d.source_file,
                d.round_number,
                d.team_side,
                d.steam_id,
                SUM(COALESCE(d.estimated_spent, 0)) AS estimated_spent
            FROM round_backup_purchase_deltas d
            INNER JOIN #touched_rounds touched
              ON touched.match_id = d.match_id
             AND touched.round_number = d.round_number
            GROUP BY
                d.match_id,
                d.source_file,
                d.round_number,
                d.team_side,
                d.steam_id
        )
        INSERT INTO round_backup_player_economy_rounds (
            match_id,
            source_file,
            round_number,
            team_side,
            steam_id,
            player_name,
            round_won,
            result_code,
            cash_earned,
            money_saved,
            equipment_value,
            kill_reward,
            estimated_spent,
            estimated_net
        )
        SELECT
            pr.match_id,
            pr.source_file,
            pr.round_number,
            pr.team_side,
            pr.steam_id,
            pr.player_name,
            CASE
                WHEN r.players_alive_t IS NOT NULL AND r.players_alive_ct IS NOT NULL AND pr.team_side = 'team1' AND r.players_alive_t > r.players_alive_ct THEN 1
                WHEN r.players_alive_t IS NOT NULL AND r.players_alive_ct IS NOT NULL AND pr.team_side = 'team2' AND r.players_alive_ct > r.players_alive_t THEN 1
                WHEN r.players_alive_t IS NOT NULL AND r.players_alive_ct IS NOT NULL AND pr.team_side = 'team1' AND r.players_alive_t < r.players_alive_ct THEN 0
                WHEN r.players_alive_t IS NOT NULL AND r.players_alive_ct IS NOT NULL AND pr.team_side = 'team2' AND r.players_alive_ct < r.players_alive_t THEN 0
                ELSE NULL
            END AS round_won,
            r.result_code,
            pr.cash_earned,
            pr.money_saved,
            pr.equipment_value,
            COALESCE(pr.kill_reward, 0) AS kill_reward,
            COALESCE(s.estimated_spent, 0) AS estimated_spent,
            COALESCE(pr.cash_earned, 0) - COALESCE(pr.money_saved, 0) AS estimated_net
        FROM player_rounds pr
        LEFT JOIN round_backup_rounds r
          ON r.match_id = pr.match_id
         AND r.source_file = pr.source_file
         AND r.round_number = pr.round_number
        LEFT JOIN spent s
          ON s.match_id = pr.match_id
         AND s.source_file = pr.source_file
         AND s.round_number = pr.round_number
         AND s.team_side = pr.team_side
         AND s.steam_id = pr.steam_id;
    """)

    print("Touched per-round player economy rebuilt.")


def rebuild_inferred_betting_money(cursor, touched_rounds):
    print("Rebuilding touched inferred betting money...")

    rows = create_touched_rounds_temp_table(
        cursor,
        touched_rounds,
        include_previous_for_betting=True,
    )

    if not rows:
        print("No touched rounds for inferred betting rebuild.")
        return

    cursor.execute(f"""
        WITH calculated AS (
            SELECT
                target.match_id,
                target.source_file,
                target.round_number,
                target.team_side,
                target.steam_id,

                m.map_name,

                target.money_saved AS start_cash,
                COALESCE(target.estimated_spent, 0) AS estimated_spent,
                target.cash_earned AS actual_current_cash,
                COALESCE(target.kill_reward, 0) AS kill_reward,
                target.result_code,
                target.round_won,

                CASE
                    -- Casual bomb/defuse maps.
                    -- Winner gets 2700.
                    WHEN m.map_name LIKE 'de[_]%' AND target.round_won = 1
                    THEN 2700

                    -- Casual hostage maps.
                    -- T win: 2000.
                    WHEN m.map_name LIKE 'cs[_]%' AND target.round_won = 1 AND target.team_side = 'team1'
                    THEN 2000

                    -- Casual hostage maps.
                    -- CT win default: 2300.
                    -- True hostage rescue can be 3000, but keep 2300 until we verify exact result_code.
                    WHEN m.map_name LIKE 'cs[_]%' AND target.round_won = 1 AND target.team_side = 'team2'
                    THEN 2300

                    -- Casual loser money is flat 2400.
                    WHEN target.round_won = 0
                    THEN 2400

                    ELSE 0
                END AS base_round_income,

                CASE
                    -- Casual bomb planted loser bonus.
                    -- team1 is T in your existing code.
                    -- result_code 3 is the one Zickzii's controlled test hit.
                    WHEN m.map_name LIKE 'de[_]%'
                     AND target.round_won = 0
                     AND target.team_side = 'team1'
                     AND target.result_code = 3
                    THEN 200

                    ELSE 0
                END AS objective_bonus

            FROM round_backup_player_economy_rounds target
            INNER JOIN #touched_rounds touched
              ON touched.match_id = target.match_id
             AND touched.round_number = target.round_number
            INNER JOIN round_backup_matches m
              ON m.match_id = target.match_id
             AND m.source_file = target.source_file
        ),
        final_calc AS (
            SELECT
                calculated.*,

                calculated.base_round_income
                + calculated.objective_bonus
                + calculated.kill_reward AS normal_round_income,

                calculated.start_cash
                - calculated.estimated_spent AS cash_after_buy,

                CASE
                    WHEN calculated.start_cash IS NULL THEN NULL
                    WHEN calculated.actual_current_cash IS NULL THEN NULL
                    WHEN calculated.start_cash
                         - calculated.estimated_spent
                         + calculated.base_round_income
                         + calculated.objective_bonus
                         + calculated.kill_reward > {MONEY_CAP}
                    THEN {MONEY_CAP}
                    ELSE calculated.start_cash
                         - calculated.estimated_spent
                         + calculated.base_round_income
                         + calculated.objective_bonus
                         + calculated.kill_reward
                END AS expected_cash_without_betting

            FROM calculated
        ),
        betting_calc AS (
            SELECT
                final_calc.*,

                final_calc.actual_current_cash
                - final_calc.expected_cash_without_betting AS betting_delta

            FROM final_calc
        )
        UPDATE target
        SET
            start_cash = betting_calc.start_cash,
            cash_after_buy = betting_calc.cash_after_buy,

            -- Misnamed old column, but now this stores expected normal income:
            -- base round payout + objective bonus + kill reward.
            known_round_income = betting_calc.normal_round_income,

            -- Misnamed old column, but now this stores actual current/end cash.
            actual_next_cash = betting_calc.actual_current_cash,

            -- Expected cash after spending and normal CS2 casual payout, before betting.
            expected_next_cash = betting_calc.expected_cash_without_betting,

            inferred_extra_money = betting_calc.betting_delta,

            inferred_betting_money =
                CASE
                    WHEN betting_calc.start_cash IS NULL THEN NULL
                    WHEN betting_calc.actual_current_cash IS NULL THEN NULL
                    ELSE betting_calc.betting_delta
                END,
            
            economy_note =
                CASE
                    WHEN betting_calc.start_cash IS NULL THEN 'Missing start cash'
                    WHEN betting_calc.actual_current_cash IS NULL THEN 'Missing current cash'
                    WHEN betting_calc.betting_delta > 0 THEN 'Positive unexplained money; likely betting win'
                    WHEN betting_calc.betting_delta < 0 THEN 'Negative unexplained money; likely betting loss'
                    ELSE 'No inferred betting money'
                END
        FROM round_backup_player_economy_rounds target
        INNER JOIN betting_calc
          ON betting_calc.match_id = target.match_id
         AND betting_calc.source_file = target.source_file
         AND betting_calc.round_number = target.round_number
         AND betting_calc.team_side = target.team_side
         AND betting_calc.steam_id = target.steam_id;
    """)

    print("Touched inferred betting money rebuilt.")

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


def already_processed_log_file(cursor, file_name):
    cursor.execute("""
        SELECT 1
        FROM processed_log_files
        WHERE file_name = ?
    """, file_name)

    return cursor.fetchone() is not None


def mark_log_file_processed(cursor, file_name):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1
            FROM processed_log_files
            WHERE file_name = ?
        )
        INSERT INTO processed_log_files (file_name)
        VALUES (?)
    """, (
        file_name,
        file_name,
    ))
  
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

def import_server_logs(cursor):
    logs_dir = Path(__file__).resolve().parent / "logs"

    if not logs_dir.exists():
        print(f"No logs folder found at {logs_dir}, skipping server log import.")
        return []

    log_files = sorted(logs_dir.glob("log-all*.txt"))

    if not log_files:
        print(f"No log-all*.txt logs found in {logs_dir}, skipping server log import.")
        return []

    chat_count = 0
    admin_count = 0
    skipped_files = 0
    processed_files_to_delete = []

    for log_file in log_files:
        if already_processed_log_file(cursor, log_file.name):
            skipped_files += 1
            print(f"Skipping already processed log: {log_file.name}")
            append_log_for_deletion(processed_files_to_delete, log_file)
            continue

        print(f"Processing log file: {log_file.name}")

        for line_number, line in iter_log_entries(log_file):
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
        append_log_for_deletion(processed_files_to_delete, log_file)

    print("Server log import complete.")
    print(f"Logs folder: {logs_dir}")
    print(f"Processed log files: {len(processed_files_to_delete)}")
    print(f"Skipped log files: {skipped_files}")
    print(f"Stored chat messages: {chat_count}")
    print(f"Stored admin actions: {admin_count}")

    return processed_files_to_delete

def write_json(path, rows):
    with path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str)


def rows_to_dicts(cursor):
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

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


def build_top_curse_users(cursor):
    cursor.execute("""
        SELECT
            player_name,
            message
        FROM chat_messages
        WHERE message IS NOT NULL
          AND message <> '';
    """)

    curse_counts = {}

    for player_name, message in cursor.fetchall():
        hits = get_curse_hits(message)

        if not hits:
            continue

        if player_name not in curse_counts:
            curse_counts[player_name] = {
                "player_name": player_name,
                "curse_count": 0,
                "curse_messages": 0,
                "matched_words": {},
            }

        curse_counts[player_name]["curse_count"] += len(hits)
        curse_counts[player_name]["curse_messages"] += 1

        for hit in hits:
            curse_counts[player_name]["matched_words"][hit] = (
                curse_counts[player_name]["matched_words"].get(hit, 0) + 1
            )

    rows = list(curse_counts.values())

    for row in rows:
        row["matched_words"] = [
            {
                "word": word,
                "count": count,
            }
            for word, count in sorted(
                row["matched_words"].items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]

    rows.sort(key=lambda row: row["curse_count"], reverse=True)

    return rows

IGNORED_ADMIN_TARGETS = {
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
}


def normalize_target_name(name):
    if not name:
        return ""

    name = clean_export_name(name)
    name = name.strip()

    return name


def get_chat_player_profiles(cursor):
    cursor.execute("""
        SELECT
            player_name,
            COUNT(*) AS message_count
        FROM chat_messages
        WHERE player_name IS NOT NULL
          AND player_name <> ''
        GROUP BY player_name;
    """)

    profiles_by_name = {}

    for player_name, message_count in cursor.fetchall():
        clean_name = clean_export_name(player_name)

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

        profiles_by_name[key]["message_count"] += int(message_count or 0)

        # Prefer the longer/prettier version of the name.
        if len(clean_name) > len(profiles_by_name[key]["player_name"]):
            profiles_by_name[key]["player_name"] = clean_name
            profiles_by_name[key]["tokens"] = get_name_tokens(clean_name)

    return list(profiles_by_name.values())


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


def export_round_backup_jsons(cursor):
    data_dir = Path(__file__).resolve().parent.parent / "assets" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    print("Exporting all-time round backup JSON files...")

    cursor.execute("""
        SELECT TOP 1
            match_id,
            source_file,
            backup_timestamp,
            map_name,
            current_round,
            team1_name,
            team2_name,
            first_half_team1_score,
            first_half_team2_score,
            loser_most_recent_team,
            consecutive_t_loses,
            consecutive_ct_loses,
            spawn_points_cfg
        FROM round_backup_matches
        ORDER BY backup_timestamp DESC, current_round DESC;
    """)
    write_json(data_dir / "round_match_summary.json", rows_to_dicts(cursor))

    cursor.execute("""
        WITH latest_files AS (
            SELECT
                match_id,
                source_file,
                current_round,
                ROW_NUMBER() OVER (
                    PARTITION BY match_id
                    ORDER BY current_round DESC, backup_timestamp DESC, source_file DESC
                ) AS rn
            FROM round_backup_matches
        ),
        damage_totals AS (
            SELECT
                prs.match_id,
                prs.source_file,
                prs.team_side,
                prs.steam_id,
                SUM(prs.stat_value) AS damage
            FROM round_backup_player_round_stats prs
            INNER JOIN latest_files lf
              ON lf.match_id = prs.match_id
             AND lf.source_file = prs.source_file
             AND lf.rn = 1
            WHERE prs.stat_name = 'Damage'
            GROUP BY prs.match_id, prs.source_file, prs.team_side, prs.steam_id
        ),
        player_totals AS (
            SELECT
                p.player_name,
                p.steam_id,
                SUM(COALESCE(p.kills, 0)) AS kills,
                SUM(COALESCE(p.deaths, 0)) AS deaths,
                SUM(COALESCE(p.assists, 0)) AS assists,
                SUM(COALESCE(p.score, 0)) AS score,
                SUM(COALESCE(p.mvps, 0)) AS mvps,
                SUM(COALESCE(p.enemy_headshots, 0)) AS enemy_headshots,
                SUM(COALESCE(p.enemy_2ks, 0)) AS enemy_2ks,
                SUM(COALESCE(p.enemy_3ks, 0)) AS enemy_3ks,
                SUM(COALESCE(p.enemy_4ks, 0)) AS enemy_4ks,
                SUM(COALESCE(p.enemy_5ks, 0)) AS enemy_5ks,
                SUM(COALESCE(p.first_kills, 0)) AS first_kills,
                SUM(COALESCE(p.clutch_kills, 0)) AS clutch_kills,
                SUM(COALESCE(p.pistol_kills, 0)) AS pistol_kills,
                SUM(COALESCE(p.sniper_kills, 0)) AS sniper_kills,
                SUM(COALESCE(p.knife_kills, 0)) AS knife_kills,
                SUM(COALESCE(p.taser_kills, 0)) AS taser_kills,
                SUM(COALESCE(d.damage, 0)) AS damage,
                SUM(COALESCE(lf.current_round, 0)) AS rounds_played
            FROM round_backup_players p
            INNER JOIN latest_files lf
              ON lf.match_id = p.match_id
             AND lf.source_file = p.source_file
             AND lf.rn = 1
            LEFT JOIN damage_totals d
              ON d.match_id = p.match_id
             AND d.source_file = p.source_file
             AND d.team_side = p.team_side
             AND d.steam_id = p.steam_id
            GROUP BY p.player_name, p.steam_id
        )
        SELECT
            player_name,
            steam_id,
            kills,
            assists,
            deaths,
            mvps,
            score,
            damage,
            CAST(CASE WHEN rounds_played > 0 THEN 1.0 * damage / rounds_played ELSE 0 END AS DECIMAL(10, 2)) AS adr,
            CAST(CASE WHEN deaths > 0 THEN 1.0 * kills / deaths ELSE kills END AS DECIMAL(10, 2)) AS kd_ratio,
            CAST(CASE WHEN deaths > 0 THEN 1.0 * (kills + assists) / deaths ELSE kills + assists END AS DECIMAL(10, 2)) AS kad_ratio,
            enemy_headshots,
            CAST(CASE WHEN kills > 0 THEN 100.0 * enemy_headshots / kills ELSE 0 END AS DECIMAL(10, 2)) AS headshot_percent,
            enemy_2ks,
            enemy_3ks,
            enemy_4ks,
            enemy_5ks,
            first_kills,
            clutch_kills,
            pistol_kills,
            sniper_kills,
            knife_kills,
            taser_kills,
            (
                COALESCE(damage, 0)
                + COALESCE(kills, 0) * 100
                + COALESCE(assists, 0) * 40
                + COALESCE(first_kills, 0) * 75
                + COALESCE(clutch_kills, 0) * 100
                + COALESCE(mvps, 0) * 150
            ) AS impact_score
        FROM player_totals
        ORDER BY impact_score DESC, damage DESC, kills DESC;
    """)
    write_json(data_dir / "round_scoreboard.json", rows_to_dicts(cursor))

    cursor.execute("""
        SELECT
            player_name,
            steam_id,
            SUM(COALESCE(estimated_spent, 0)) AS money_spent,
            SUM(COALESCE(inferred_betting_money, 0)) AS net_betting,
            SUM(CASE WHEN inferred_betting_money > 0 THEN inferred_betting_money ELSE 0 END) AS betting_won,
            SUM(CASE WHEN inferred_betting_money < 0 THEN inferred_betting_money ELSE 0 END) AS betting_lost
        FROM round_backup_player_economy_rounds
        GROUP BY player_name, steam_id
        ORDER BY net_betting DESC;
    """)
    write_json(data_dir / "round_player_money_summary.json", rows_to_dicts(cursor))

    cursor.execute("""
        SELECT
            player_name,
            steam_id,
            SUM(CASE WHEN economy_note <> 'Impossible extra money; ignored' THEN COALESCE(inferred_extra_money, 0) ELSE 0 END) AS total_net_bet_winnings,
            SUM(CASE WHEN inferred_extra_money > 0 AND economy_note <> 'Impossible extra money; ignored' THEN inferred_extra_money ELSE 0 END) AS total_positive_bet_winnings,
            SUM(CASE WHEN inferred_extra_money < 0 AND economy_note <> 'Impossible extra money; ignored' THEN inferred_extra_money ELSE 0 END) AS total_negative_bet_winnings,
            COUNT(CASE WHEN inferred_extra_money <> 0 AND economy_note <> 'Impossible extra money; ignored' THEN 1 END) AS rounds_with_unexplained_money
        FROM round_backup_player_economy_rounds
        GROUP BY player_name, steam_id
        ORDER BY total_net_bet_winnings DESC;
    """)
    write_json(data_dir / "round_player_betting_summary.json", rows_to_dicts(cursor))

    cursor.execute("""
        SELECT
            player_name,
            steam_id,
            match_id,
            source_file,
            round_number,
            start_cash,
            estimated_spent,
            cash_after_buy,
            known_round_income,
            expected_next_cash,
            actual_next_cash,
            inferred_extra_money,
            inferred_betting_money,
            economy_note
        FROM round_backup_player_economy_rounds
        WHERE inferred_betting_money <> 0
        ORDER BY inferred_betting_money DESC, round_number ASC;
    """)
    write_json(data_dir / "round_betting_money.json", rows_to_dicts(cursor))

    cursor.execute("""
        SELECT
            player_name,
            steam_id,
            match_id,
            source_file,
            round_number,
            round_won,
            result_code,
            start_cash,
            estimated_spent,
            cash_after_buy,
            known_round_income,
            cash_earned,
            money_saved,
            equipment_value,
            expected_next_cash,
            actual_next_cash,
            inferred_extra_money,
            inferred_betting_money,
            economy_note
        FROM round_backup_player_economy_rounds
        ORDER BY match_id ASC, round_number ASC, player_name ASC;
    """)
    write_json(data_dir / "round_economy_rounds.json", rows_to_dicts(cursor))

    cursor.execute("""
        SELECT
            p.match_id,
            p.source_file,
            p.round_number,
            p.team_side,
            p.steam_id,
            e.player_name,
            p.def_index,
            COALESCE(i.item_name, CONCAT('DefIndex_', p.def_index)) AS item_name,
            i.item_category,
            i.price,
            p.purchase_delta,
            p.estimated_spent,
            e.round_won
        FROM round_backup_purchase_deltas p
        LEFT JOIN item_definition_prices i
          ON i.def_index = p.def_index
        LEFT JOIN round_backup_player_economy_rounds e
          ON e.match_id = p.match_id
         AND e.source_file = p.source_file
         AND e.round_number = p.round_number
         AND e.team_side = p.team_side
         AND e.steam_id = p.steam_id
        ORDER BY p.match_id ASC, p.round_number ASC, e.player_name ASC, i.item_name ASC;
    """)
    write_json(data_dir / "round_weapon_purchases.json", rows_to_dicts(cursor))

    cursor.execute("""
        SELECT
            p.def_index,
            COALESCE(i.item_name, CONCAT('DefIndex_', p.def_index)) AS item_name,
            i.item_category,
            i.price,
            SUM(p.purchase_delta) AS total_bought,
            SUM(CASE WHEN e.round_won = 1 THEN p.purchase_delta ELSE 0 END) AS bought_in_wins,
            SUM(CASE WHEN e.round_won = 0 THEN p.purchase_delta ELSE 0 END) AS bought_in_losses,
            CAST(
                100.0 * SUM(CASE WHEN e.round_won = 1 THEN p.purchase_delta ELSE 0 END)
                / NULLIF(SUM(p.purchase_delta), 0)
                AS DECIMAL(10, 2)
            ) AS win_buy_percent,
            SUM(COALESCE(p.estimated_spent, 0)) AS total_spent
        FROM round_backup_purchase_deltas p
        LEFT JOIN item_definition_prices i
          ON i.def_index = p.def_index
        LEFT JOIN round_backup_player_economy_rounds e
          ON e.match_id = p.match_id
         AND e.source_file = p.source_file
         AND e.round_number = p.round_number
         AND e.team_side = p.team_side
         AND e.steam_id = p.steam_id
        GROUP BY p.def_index, i.item_name, i.item_category, i.price
        ORDER BY total_bought DESC, total_spent DESC;
    """)
    write_json(data_dir / "round_weapon_meta.json", rows_to_dicts(cursor))

    cursor.execute("""
        WITH player_rounds AS (
            SELECT
                player_name,
                steam_id,
                COUNT(*) AS ct_rounds_played
            FROM round_backup_player_economy_rounds
            WHERE team_side = 'team2'
            GROUP BY player_name, steam_id
        ),
        defuse_buys AS (
            SELECT
                e.player_name,
                e.steam_id,
                SUM(p.purchase_delta) AS defuse_kits_bought
            FROM round_backup_purchase_deltas p
            INNER JOIN round_backup_player_economy_rounds e
              ON e.match_id = p.match_id
             AND e.source_file = p.source_file
             AND e.round_number = p.round_number
             AND e.team_side = p.team_side
             AND e.steam_id = p.steam_id
            WHERE p.def_index = 55
              AND p.team_side = 'team2'
              AND e.team_side = 'team2'
            GROUP BY e.player_name, e.steam_id
        )
        SELECT
            pr.player_name,
            pr.steam_id,
            COALESCE(d.defuse_kits_bought, 0) AS defuse_kits_bought,
            pr.ct_rounds_played AS rounds_played,
            CAST(
                100.0 * COALESCE(d.defuse_kits_bought, 0)
                / NULLIF(pr.ct_rounds_played, 0)
                AS DECIMAL(10, 2)
            ) AS defuse_purchase_round_percent
        FROM player_rounds pr
        LEFT JOIN defuse_buys d
          ON d.steam_id = pr.steam_id
         AND d.player_name = pr.player_name
        ORDER BY defuse_purchase_round_percent DESC, defuse_kits_bought DESC;
    """)
    write_json(data_dir / "round_defuse_purchase_rate.json", rows_to_dicts(cursor))

    cursor.execute("""
        WITH latest_files AS (
            SELECT
                match_id,
                source_file,
                ROW_NUMBER() OVER (
                    PARTITION BY match_id
                    ORDER BY current_round DESC, backup_timestamp DESC, source_file DESC
                ) AS rn
            FROM round_backup_matches
        ),
        unpivoted AS (
            SELECT p.player_name, p.steam_id, 'Pistol' AS weapon, COALESCE(p.pistol_kills, 0) AS kills
            FROM round_backup_players p
            INNER JOIN latest_files lf ON lf.match_id = p.match_id AND lf.source_file = p.source_file AND lf.rn = 1
            UNION ALL
            SELECT p.player_name, p.steam_id, 'Sniper' AS weapon, COALESCE(p.sniper_kills, 0) AS kills
            FROM round_backup_players p
            INNER JOIN latest_files lf ON lf.match_id = p.match_id AND lf.source_file = p.source_file AND lf.rn = 1
            UNION ALL
            SELECT p.player_name, p.steam_id, 'Knife' AS weapon, COALESCE(p.knife_kills, 0) AS kills
            FROM round_backup_players p
            INNER JOIN latest_files lf ON lf.match_id = p.match_id AND lf.source_file = p.source_file AND lf.rn = 1
            UNION ALL
            SELECT p.player_name, p.steam_id, 'Taser' AS weapon, COALESCE(p.taser_kills, 0) AS kills
            FROM round_backup_players p
            INNER JOIN latest_files lf ON lf.match_id = p.match_id AND lf.source_file = p.source_file AND lf.rn = 1
        ),
        totals AS (
            SELECT weapon, player_name, steam_id, SUM(kills) AS kills
            FROM unpivoted
            GROUP BY weapon, player_name, steam_id
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (PARTITION BY weapon ORDER BY kills DESC, player_name ASC) AS rn
            FROM totals
            WHERE kills > 0
        )
        SELECT weapon, kills, player_name, steam_id
        FROM ranked
        WHERE rn = 1
        ORDER BY kills DESC;
    """)
    write_json(data_dir / "round_top_weapon_category_kills.json", rows_to_dicts(cursor))

    print("Round backup JSON exports complete.")

def export_log_jsons(cursor):
    data_dir = Path(__file__).resolve().parent.parent / "assets" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    player_profiles = get_chat_player_profiles(cursor)
    print(f"Loaded {len(player_profiles)} chat player profiles for alias matching.")

    print(f"Exporting log JSON files to {data_dir}")

    # Top chatters
    cursor.execute("""
        SELECT
            player_name,
            COUNT(*) AS message_count
        FROM chat_messages
        GROUP BY player_name
        ORDER BY message_count DESC;
    """)
    write_json(data_dir / "top_chatters.json", rows_to_dicts(cursor))

    # Top curse users.
    curse_rows = build_top_curse_users(cursor)
    write_json(data_dir / "top_curse_users.json", curse_rows)
  
    # Most slain players
    cursor.execute("""
        SELECT
            target_name AS player_name,
            COUNT(*) AS slain_count
        FROM admin_actions
        WHERE command = 'css_slay'
          AND target_name IS NOT NULL
          AND target_name <> ''
        GROUP BY target_name;
    """)
    
    slain_rows = rows_to_dicts(cursor)
    slain_rows = consolidate_admin_target_rows(
        slain_rows,
        player_profiles,
        "slain_count",
    )
    write_json(data_dir / "most_slain_players.json", slain_rows)

    # Most slapped players
    cursor.execute("""
        SELECT
            target_name AS player_name,
            COUNT(*) AS slapped_count,
            SUM(COALESCE(amount, 0)) AS total_slap_damage
        FROM admin_actions
        WHERE command = 'css_slap'
          AND target_name IS NOT NULL
          AND target_name <> ''
        GROUP BY target_name;
    """)
    
    slapped_rows = rows_to_dicts(cursor)
    slapped_rows = consolidate_admin_target_rows(
        slapped_rows,
        player_profiles,
        "slapped_count",
        extra_sum_fields=["total_slap_damage"],
    )
    write_json(data_dir / "most_slapped_players.json", slapped_rows)

    #Admin total command usage
    cursor.execute("""
        SELECT
            admin_name,
            command,
            COUNT(*) AS command_count
        FROM admin_actions
        GROUP BY admin_name, command;
    """)
    
    admin_rows = rows_to_dicts(cursor)
    
    grouped = {}
    
    for row in admin_rows:
        admin_name = clean_export_name(row["admin_name"])
        command = row["command"]
        count = int(row["command_count"] or 0)
    
        key = (admin_name, command)
    
        if key not in grouped:
            grouped[key] = {
                "admin_name": admin_name,
                "command": command,
                "command_count": 0,
            }
    
        grouped[key]["command_count"] += count
    
    admin_rows = list(grouped.values())
    admin_rows.sort(key=lambda row: row["command_count"], reverse=True)
    
    write_json(data_dir / "admin_command_usage.json", admin_rows)

    export_round_backup_jsons(cursor)

if __name__ == "__main__":
    # ------------------------------------------------------------
    # 1. Download files first.
    # This opens SFTP, downloads logs/backups, then closes SFTP.
    # No SQL connection is open yet.
    # ------------------------------------------------------------
    include_logs = os.environ.get("INCLUDE_LOGS", "0").strip() == "1"
    backup_paths = download_logs_and_round_backups_from_sftp(include_logs=include_logs)

    # ------------------------------------------------------------
    # 2. Now connect to SQL after SFTP is done.
    # ------------------------------------------------------------
    conn = connect_with_retry()
    cursor = conn.cursor()
    cursor.execute("SELECT @@VERSION")

    try:
        # ------------------------------------------------------------
        # 3. Import local downloaded files into SQL.
        # ------------------------------------------------------------
        if include_logs:
            processed_log_files = import_server_logs(cursor)
        else:
            print("Skipping server log import because INCLUDE_LOGS is not 1.")
            processed_log_files = []
        processed_backup_files, touched_backup_rounds = import_round_backups(cursor, backup_paths)

        # ------------------------------------------------------------
        # 4. Rebuild derived economy/betting tables.
        # ------------------------------------------------------------
        rebuild_round_purchase_deltas(cursor, touched_backup_rounds)
        rebuild_round_player_economy(cursor, touched_backup_rounds)
        rebuild_inferred_betting_money(cursor, touched_backup_rounds)

        # ------------------------------------------------------------
        # 5. Commit SQL before deleting local files.
        # ------------------------------------------------------------
        conn.commit()

        # ------------------------------------------------------------
        # 6. Delete only successfully imported local files.
        # ------------------------------------------------------------
        delete_imported_logs(processed_log_files)
        delete_imported_round_backups(processed_backup_files)

        # ------------------------------------------------------------
        # 7. Export JSON after SQL commit.
        # ------------------------------------------------------------
        export_log_jsons(cursor)

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()
