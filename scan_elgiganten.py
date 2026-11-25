import json
import os
import re
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
NTFY_TOPIC = os.getenv("NTFY_TOPIC") or "elgiganten-glitch-73915"  # <-- Your ntfy topic
MIN_DISCOUNT_PCT = 80.0     # Alert on 80%+ off (change to 90.0 for 90%+ only)
MIN_ORIGINAL_PRICE = 500    # Ignore cheap cables/cases under 500 kr.

# Categories to scan every 5 minutes (add or remove Elgiganten category URLs)
URLS_TO_SCAN = [
    "https://www.elgiganten.dk/tilbud",
    "https://www.elgiganten.dk/computer-kontor/computere/barbar-computer",
    "https://www.elgiganten.dk/tv-lyd-smart-home/tv-tilbehor/tv",
    "https://www.elgiganten.dk/gaming/pc-gaming/gaming-baerbar",
    "https://www.elgiganten.dk/mobil-tablet-smartwatch/mobiltelefon",
]

ALERTED_FILE = "alerted_skus.json"


def load_alerted():
    if os.path.exists(ALERTED_FILE):
        with open(ALERTED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_alerted(alerted_set):
    with open(ALERTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(alerted_set)), f, indent=2)


def send_ntfy(title, message, click_url):
    import urllib.request
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "urgent",
            "Tags": "rotating_light,money_with_wings",
            "Click": click_url,
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


def parse_dkk(text):
    """Extracts the main consumer price (incl. VAT) in DKK from card text."""
    # Matches patterns like '2.499.-' or '599.-'
    matches = re.findall(r"(\d{1,3}(?:\.\d{3})*)\.-", text)
    if not matches:
        return []
    return [int(m.replace(".", "")) for m in matches]


def scan():
    alerted = load_alerted()
    new_alerts = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="da-DK",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        for url in URLS_TO_SCAN:
            print(f"Scanning: {url}", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)  # Let product cards finish hydrating

                # Extract product cards containing links to /product/
                cards = page.evaluate("""() => {
                    const results = [];
                    const links = document.querySelectorAll('a[href*="/product/"]');
                    const seen = new Set();
                    for (const a of links) {
                        const href = a.href;
                        if (seen.has(href)) continue;
                        // Walk up to the product tile container
                        let card = a.closest('article, li, [data-testid*="product"], div');
                        while (card && card.innerText && card.innerText.length < 80 && card.parentElement) {
                            card = card.parentElement;
                        }
                        if (!card || !card.innerText) continue;
                        const text = card.innerText;
                        if (text.includes('Førpris') && text.includes('.-')) {
                            seen.add(href);
                            results.push({ href, text });
                        }
                    }
                    return results;
                }""")

                print(f"  Found {len(cards)} discounted cards.", flush=True)

                for item in cards:
                    href = item["href"]
                    text = item["text"]

                    # Skip third-party Marketplace sellers (often fake 90% off accessories)
                    if "Sælges af " in text and "Elgiganten" not in text:
                        continue

                    # Extract SKU from URL (trailing digits)
                    sku_match = re.search(r"/(\d+)(?:$|\?)", href)
                    sku = sku_match.group(1) if sku_match else href

                    # Split into current price block vs Førpris block
                    parts = text.split("Førpris:")
                    if len(parts) < 2:
                        continue

                    now_prices = parse_dkk(parts[0])
                    before_prices = parse_dkk(parts[1])

                    if not now_prices or not before_prices:
                        continue

                    # Elgiganten shows incl. VAT first, excl. VAT second
                    now_price = now_prices[0]
                    before_price = before_prices[0]

                    if before_price < MIN_ORIGINAL_PRICE or now_price >= before_price:
                        continue

                    discount_pct = ((before_price - now_price) / before_price) * 100

                    if discount_pct >= MIN_DISCOUNT_PCT:
                        title_line = text.strip().splitlines()[0][:80]
                        print(
                            f"  [GLITCH DEAL] {title_line}: {now_price} kr. (Før {before_price} kr., -{discount_pct:.1f}%)",
                            flush=True,
                        )
                        if sku not in alerted:
                            send_ntfy(
                                title=f"-{discount_pct:.0f}% GLITCH: {now_price} kr. (Før {before_price} kr.)",
                                message=f"{title_line}\nNu: {now_price} kr. | Før: {before_price} kr.\nTryk for at åbne Elgiganten.",
                                click_url=href,
                            )
                            alerted.add(sku)
                            new_alerts += 1

            except Exception as e:
                print(f"  Error scanning {url}: {e}", flush=True)

        browser.close()

    if new_alerts > 0:
        save_alerted(alerted)
    print(f"Done. New glitch alerts sent: {new_alerts}", flush=True)


if __name__ == "__main__":
    scan()
