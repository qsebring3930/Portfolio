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

cursor.close()
conn.close()

client = ApifyClient(os.environ["APIFY_TOKEN"])

run_input = {
    "token": os.environ["DISCORD_TOKEN"],
    "channelInput": "https://discord.com/channels/1232410706230513814/1240609027470131261",
}

run = client.actor("wUoh2wdO7k9mnzL9d").call(run_input=run_input)

for item in client.dataset(run.default_dataset_id).iterate_items():
    print(item)
