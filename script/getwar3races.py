import json
import re
import time
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://war3cs2.wiki.gg"

PAGE_PREFIXES = ["Races/", "Champions/"]


def fetch_json(params):
    url = BASE + "/api.php?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "War3CS2RacePageDumper/1.0"})

    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def safe_filename(title):
    name = title

    for prefix in PAGE_PREFIXES:
        name = name.replace(prefix, "")

    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name + ".txt"


def get_pages_from_category(category_name):
    pages = []
    cont = None

    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": category_name,
            "cmlimit": "500",
            "format": "json"
        }

        if cont:
            params["cmcontinue"] = cont

        data = fetch_json(params)

        for item in data["query"]["categorymembers"]:
            title = item["title"]

            if any(title.startswith(prefix) for prefix in PAGE_PREFIXES):
                pages.append(title)

        cont = data.get("continue", {}).get("cmcontinue")

        if not cont:
            break

    return pages


def get_all_pages():
    pages = []

    pages.extend(get_pages_from_category("Category:Races"))
    pages.extend(get_pages_from_category("Category:Champions"))

    return sorted(set(pages))


def get_page_text(title):
    data = fetch_json({
        "action": "query",
        "prop": "revisions",
        "titles": title,
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
        "formatversion": "2"
    })

    page = data["query"]["pages"][0]

    if "revisions" not in page:
        return ""

    return page["revisions"][0]["slots"]["main"]["content"]


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, "war3cs2_pages")
    os.makedirs(output_dir, exist_ok=True)

    print("Saving pages to:")
    print(output_dir)

    pages = get_all_pages()
    print(f"\nFound {len(pages)} race/champion pages\n")

    downloaded = 0
    skipped = 0
    failed = 0

    for i, title in enumerate(pages, start=1):
        filename = safe_filename(title)
        path = os.path.join(output_dir, filename)

        if os.path.exists(path):
            print(f"[{i}/{len(pages)}] Skipping existing: {title}")
            skipped += 1
            continue

        print(f"[{i}/{len(pages)}] Downloading: {title}")

        try:
            text = get_page_text(title)

            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

            downloaded += 1

        except Exception as e:
            print(f"Failed: {title} - {e}")
            failed += 1

        time.sleep(0.15)

    print("\nDone saving pages.")
    print(f"Downloaded: {downloaded}")
    print(f"Skipped existing: {skipped}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    main()
