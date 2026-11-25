import json
import os
import re
import urllib.request
from playwright.sync_api import sync_playwright

NTFY_TOPIC = os.getenv("NTFY_TOPIC") or "elgiganten-glitch-73915"
MIN_DISCOUNT_PCT = 80.0     # Alert on 80%+ off (change to 90.0 for 90%+ only)
MIN_ORIGINAL_PRICE = 500    # Ignore cheap accessories under 500 kr.

URLS_TO_SCAN = [
    "https://www.elgiganten.dk/tilbud",
    "https://www.elgiganten.dk/computer-kontor/computere/barbar-computer",
    "https://www.elgiganten.dk/tv-lyd-smart-home/tv-tilbehor/tv",
    "https://www.elgiganten.dk/gaming/gaming-pc",
    "https://www.elgiganten.dk/gaming/hardware",
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
    matches = re.findall(r"(\d{1,3}(?:\.\d{3})*)\.-", text)
    return [int(m.replace(".", "")) for m in matches]


def scan():
    alerted = load_alerted()
    new_alerts = 0
    top_deal = (0.0, "", 0, 0)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            locale="da-DK",
            timezone_id="Europe/Copenhagen",
            viewport={"width": 1366, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2}", lambda route: route.abort())

        for idx, url in enumerate(URLS_TO_SCAN):
            if idx > 0:
                page.wait_for_timeout(2000)  # Polite delay to avoid HTTP 429
            print(f"Scanning: {url}", flush=True)
            try:
                resp = page.goto(url, wait_until="commit", timeout=15000)
                page.wait_for_timeout(4000)
                status = resp.status if resp else "No response"
                print(f"  HTTP {status} | Title: '{page.title()}'", flush=True)

                cards = page.evaluate("""() => {
                    const results = [];
                    const links = document.querySelectorAll('a[href*="/product/"]');
                    const seen = new Set();
                    for (const a of links) {
                        const href = a.href;
                        if (seen.has(href)) continue;
                        let card = a.parentElement;
                        for (let i = 0; i < 6 && card; i++) {
                            if (card.innerText && card.innerText.includes('Førpris') && card.innerText.length < 1500) {
                                break;
                            }
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

                    if "Sælges af " in text and "Elgiganten" not in text:
                        continue

                    sku_match = re.search(r"/(\d+)(?:$|\?)", href)
                    sku = sku_match.group(1) if sku_match else href

                    parts = text.split("Førpris:")
                    if len(parts) < 2:
                        continue

                    now_prices = parse_dkk(parts[0])
                    before_prices = parse_dkk(parts[1])
                    if not now_prices or not before_prices:
                        continue

                    now_price = now_prices[0]
                    before_price = before_prices[0]
                    if before_price < MIN_ORIGINAL_PRICE or now_price >= before_price:
                        continue

                    discount_pct = ((before_price - now_price) / before_price) * 100

                    # Extract clean product name from the URL slug if first line is a promo badge
                    slug_match = re.search(r"/([^/]+)/\d+(?:$|\?)", href)
                    slug_name = slug_match.group(1).replace("-", " ").title() if slug_match else ""
                    first_line = text.strip().splitlines()[0][:70]
                    title_line = slug_name if (first_line.startswith("*") or len(first_line) < 10) and slug_name else first_line

                    if discount_pct > top_deal[0]:
                        top_deal = (discount_pct, title_line, now_price, before_price)

                    if discount_pct >= MIN_DISCOUNT_PCT:
                        print(f"  [GLITCH DEAL] {title_line}: {now_price} kr. (Før {before_price} kr., -{discount_pct:.1f}%)", flush=True)
                        if sku not in alerted:
                            send_ntfy(
                                title=f"-{discount_pct:.0f}% GLITCH: {now_price} kr. (Før {before_price} kr.)",
                                message=f"{title_line}\nNu: {now_price} kr. | Før: {before_price} kr.\n{href}",
                                click_url=href,
                            )
                            alerted.add(sku)
                            new_alerts += 1

            except Exception as e:
                print(f"  Error scanning {url}: {e}", flush=True)

        browser.close()

    if new_alerts > 0:
        save_alerted(alerted)
    if top_deal[0] > 0:
        print(f"Highest discount seen this run: -{top_deal[0]:.1f}% ({top_deal[1]}: {top_deal[2]} kr. vs {top_deal[3]} kr.)", flush=True)
    print(f"Done. New glitch alerts sent: {new_alerts}", flush=True)


if __name__ == "__main__":
    scan()
