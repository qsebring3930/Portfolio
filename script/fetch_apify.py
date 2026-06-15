import os
import pyodbc
import re
from apify_client import ApifyClient

print("Testing Azure SQL connection...")

conn = pyodbc.connect(
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER=tcp:{os.environ['AZURE_SQL_SERVER']},1433;"
    f"DATABASE={os.environ['AZURE_SQL_DATABASE']};"
    f"UID={os.environ['AZURE_SQL_USERNAME']};"
    f"PWD={os.environ['AZURE_SQL_PASSWORD']};"
    "Encrypt=yes;"
    "TrustServerCertificate=no;"
)

cursor = conn.cursor()

cursor.execute("SELECT @@VERSION")

row = cursor.fetchone()

print("Connected successfully!")
print(row[0][:200])

cursor.execute("""
IF OBJECT_ID('race_playtime', 'U') IS NULL
CREATE TABLE race_playtime (
    player_name NVARCHAR(255) NOT NULL,
    race_name NVARCHAR(255) NOT NULL,
    minutes_played INT NOT NULL DEFAULT 0,
    last_seen DATETIME2 NULL,
    PRIMARY KEY (player_name, race_name)
);
""")

cursor.execute("""
IF OBJECT_ID('race_levels', 'U') IS NULL
CREATE TABLE race_levels (
    player_name NVARCHAR(255) NOT NULL,
    race_name NVARCHAR(255) NOT NULL,
    level INT NOT NULL,
    last_seen DATETIME2 NULL,
    PRIMARY KEY (player_name, race_name)
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
        cursor.execute("""
            MERGE race_playtime AS target
            USING (
                SELECT ? AS player_name, ? AS race_name
            ) AS source
            ON target.player_name = source.player_name
            AND target.race_name = source.race_name
            WHEN MATCHED THEN
                UPDATE SET
                    minutes_played = minutes_played + ?,
                    last_seen = TRY_CONVERT(DATETIME2, ?)
            WHEN NOT MATCHED THEN
                INSERT (player_name, race_name, minutes_played, last_seen)
                VALUES (?, ?, ?, TRY_CONVERT(DATETIME2, ?));
        """, (
            player["player_name"],
            player["race_name"],
            SNAPSHOT_MINUTES,
            timestamp,
            player["player_name"],
            player["race_name"],
            SNAPSHOT_MINUTES,
            timestamp,
        ))

        cursor.execute("""
            MERGE race_levels AS target
            USING (
                SELECT ? AS player_name, ? AS race_name
            ) AS source
            ON target.player_name = source.player_name
            AND target.race_name = source.race_name
            WHEN MATCHED THEN
                UPDATE SET
                    level = ?,
                    last_seen = TRY_CONVERT(DATETIME2, ?)
            WHEN NOT MATCHED THEN
                INSERT (player_name, race_name, level, last_seen)
                VALUES (?, ?, ?, TRY_CONVERT(DATETIME2, ?));
        """, (
            player["player_name"],
            player["race_name"],
            player["level"],
            timestamp,
            player["player_name"],
            player["race_name"],
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

cursor.execute("""
DROP TABLE race_playtime;
DROP TABLE race_levels;
);
""")

conn.commit()
print("Stats updated.")

cursor.close()
conn.close()
