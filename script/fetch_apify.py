import os
import pyodbc
from apify_client import ApifyClient

print("Testing Azure SQL connection...")

conn = pyodbc.connect(
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER={os.environ['AZURE_SQL_SERVER']},1433;"
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
cursor.close()
conn.close()

print("Tables created or already exist.")

client = ApifyClient(os.environ["APIFY_TOKEN"])

run_input = {
    "token": os.environ["DISCORD_TOKEN"],
    "channelInput": "https://discord.com/channels/1232410706230513814/1240609027470131261",
}

run = client.actor("wUoh2wdO7k9mnzL9d").call(run_input=run_input)

for item in client.dataset(run.default_dataset_id).iterate_items():
    print(item)
