import os
import re
import json
import requests
from pathlib import Path
from datetime import datetime

DATA_DIR = Path("assets/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SNAPSHOT_MINUTES = 1

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


def rows_to_dict(rows, key):
    return {
        row[key]: row
        for row in rows
        if row.get(key)
    }


def normalize_player_name(player_name):
    name = player_name.strip()

    if name.upper().startswith("BOT "):
        return "BOT"

    return name


def race_to_column(race_name):
    col = race_name.lower()
    col = re.sub(r"[^a-z0-9]+", "_", col).strip("_")
    return col[:120] if col else "saboteur"


def get_valid_race_columns():
    script_dir = Path(__file__).resolve().parent
    race_pages_dir = script_dir / "war3cs2_pages"

    valid_columns = {
        race_to_column(path.stem)
        for path in race_pages_dir.glob("*.txt")
    }

    valid_columns.add("saboteur")
    return valid_columns


VALID_RACE_COLUMNS = get_valid_race_columns()


def get_embed(item):
    embeds = item.get("embeds") or []
    if not embeds:
        return None
    return embeds[0]


def get_map_name(embed):
    for field in embed.get("fields", []):
        if "Map" in field.get("name", ""):
            value = field.get("value", "")
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

            match = re.match(
                r"^\*\s*(.+?)\s*-\s*\[(.+?)\s*\|\s*Lvl\.\s*(\d+)\]",
                line
            )

            if not match:
                continue

            players.append({
                "player_name": normalize_player_name(match.group(1)),
                "race_name": match.group(2).strip(),
                "level": int(match.group(3)),
            })

    return players

def update_stats_json(item, state):
    embed = get_embed(item)
    if not embed:
        return False

    timestamp = embed.get("timestamp") or item.get("timestamp")
    message_id = str(item.get("messageId") or item.get("id"))
    snapshot_id = f"{message_id}:{timestamp}"

    processed_snapshots = state["processed_snapshots"]

    if snapshot_id in processed_snapshots:
        print(f"Skipping already processed snapshot: {message_id}")
        return False

    map_playtime = state["map_playtime"]
    race_playtime = state["race_playtime"]
    race_levels = state["race_levels"]

    map_name = get_map_name(embed)
    players = parse_team_players(embed)

    if map_name:
        row = map_playtime.setdefault(map_name, {
            "map_name": map_name,
            "minutes_played": 0,
            "last_seen": None,
            "complaint_count": 0,
        })

        row["minutes_played"] = int(row.get("minutes_played") or 0) + SNAPSHOT_MINUTES
        row["last_seen"] = timestamp

    for player in players:
        player_name = player["player_name"]
        race_col = race_to_column(player["race_name"])

        if race_col not in VALID_RACE_COLUMNS:
            race_col = "saboteur"

        playtime_row = race_playtime.setdefault(player_name, {
            "player_name": player_name,
            "last_seen": None,
        })

        level_row = race_levels.setdefault(player_name, {
            "player_name": player_name,
            "last_seen": None,
        })

        playtime_row[race_col] = int(playtime_row.get(race_col) or 0) + SNAPSHOT_MINUTES
        playtime_row["last_seen"] = timestamp

        level_row[race_col] = player["level"]
        level_row["last_seen"] = timestamp

    processed_snapshots.add(snapshot_id)
    return True

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


def load_state():
    map_rows = load_json(DATA_DIR / "map_playtime.json", [])
    race_playtime_rows = load_json(DATA_DIR / "race_playtime.json", [])
    race_level_rows = load_json(DATA_DIR / "race_levels.json", [])

    processed_snapshots = set(load_json(
        DATA_DIR / "processed_snapshots.json",
        []
    ))

    return {
        "map_playtime": rows_to_dict(map_rows, "map_name"),
        "race_playtime": rows_to_dict(race_playtime_rows, "player_name"),
        "race_levels": rows_to_dict(race_level_rows, "player_name"),
        "processed_snapshots": processed_snapshots,
    }


def save_state(state):
    save_json(
        DATA_DIR / "map_playtime.json",
        sorted(state["map_playtime"].values(), key=lambda r: r["map_name"].lower())
    )

    save_json(
        DATA_DIR / "race_playtime.json",
        sorted(state["race_playtime"].values(), key=lambda r: r["player_name"].lower())
    )

    save_json(
        DATA_DIR / "race_levels.json",
        sorted(state["race_levels"].values(), key=lambda r: r["player_name"].lower())
    )

    save_json(
        DATA_DIR / "processed_snapshots.json",
        sorted(state["processed_snapshots"])
    )


discord_channel_id = os.environ.get("DISCORD_CHANNEL_ID", "1240609027470131261")
discord_message_limit = int(os.environ.get("DISCORD_MESSAGE_LIMIT", "1"))

state = load_state()
messages = retrieve_messages(discord_channel_id, limit=discord_message_limit)

changed = False

# Oldest first, so totals are applied chronologically.
for item in reversed(messages):
    if update_stats_json(item, state):
        changed = True

if changed:
    save_state(state)
    print("Stats updated.")
else:
    print("No new snapshots.")
