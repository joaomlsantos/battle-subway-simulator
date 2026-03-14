import urllib.request
import urllib.parse
import os
import time
from bs4 import BeautifulSoup

BASE_URL = "https://bulbapedia.bulbagarden.net"
CACHE_DIR = "C:/Workspace/battle-subway-simulator/cache"
MAIN_PAGE = "https://bulbapedia.bulbagarden.net/wiki/List_of_Battle_Subway_Trainers"


def url_to_filename(url: str) -> str:
    """Convert a URL to a safe filename."""
    path = url.replace(BASE_URL, "").replace("https://", "")
    path = urllib.parse.unquote(path)  # decode %XX encodings
    safe = path.replace("/", "_").replace("#", "__").strip("_")
    return safe + ".html"


def fetch_and_save(url: str) -> str:
    filepath = os.path.join(CACHE_DIR, url_to_filename(url))

    if os.path.exists(filepath):
        print(f"  [cached] {url}")
        return filepath

    print(f"  [fetch]  {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req).read()

    with open(filepath, "wb") as f:
        f.write(html)

    time.sleep(1)
    return filepath


def collect_hrefs(main_html_path: str) -> set:
    """Parse the main page cache file and collect all unique trainer hrefs (without anchor)."""
    with open(main_html_path, "rb") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    hrefs = set()
    for a in soup.select("table a[href*='List_of_Battle_Subway_Trainers/']"):
        href = a.get("href", "")
        # strip anchor, keep only the page URL
        page = href.split("#")[0]
        if page:
            hrefs.add(BASE_URL + page)
    return hrefs


if __name__ == "__main__":
    os.makedirs(CACHE_DIR, exist_ok=True)

    print("Fetching main page...")
    main_path = fetch_and_save(MAIN_PAGE)

    print("\nCollecting trainer subpage URLs...")
    subpage_urls = collect_hrefs(main_path)
    print(f"Found {len(subpage_urls)} unique subpages")

    print("\nFetching subpages...")
    for url in sorted(subpage_urls):
        fetch_and_save(url)

    print(f"\nDone. All pages saved to '{CACHE_DIR}/'")