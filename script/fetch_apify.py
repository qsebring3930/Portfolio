import os
import json
import requests

token = os.environ["APIFY_TOKEN"]
dataset_id = os.environ["APIFY_DATASET_ID"]

url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"

response = requests.get(url, params={
    "token": token,
    "format": "json",
    "clean": "true"
})

response.raise_for_status()
items = response.json()

os.makedirs("_data", exist_ok=True)

with open("_data/apify_items.json", "w", encoding="utf-8") as f:
    json.dump(items, f, indent=2, ensure_ascii=False)

print(f"Saved {len(items)} items")
