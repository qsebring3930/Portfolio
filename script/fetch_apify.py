import os
from apify_client import ApifyClient

client = ApifyClient(os.environ["APIFY_TOKEN"])

run_input = {
    "token": os.environ["DISCORD_TOKEN"],
    "channelInput": "869237470565392388",
    "afterDate": "2026-01-25 01:00",
    "beforeDate": "2026-01-26",
    "messageFilter": "from:Tyrrrz has:image",
    "includeThreads": "none",
}

run = client.actor("wUoh2wdO7k9mnzL9d").call(run_input=run_input)

for item in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(item)
