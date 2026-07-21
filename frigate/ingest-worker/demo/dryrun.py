import os

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8911"
API_KEY = "demo-key"
SHOT_DIR = os.path.join(os.path.dirname(__file__), "shots")
SIZE = {"width": 1280, "height": 800}

os.makedirs(SHOT_DIR, exist_ok=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=SIZE)

        page.goto(f"{BASE}/ui/index.html")
        page.fill('input[type="password"]', API_KEY)
        page.click('button:has-text("Continue")')
        page.wait_for_selector(".grid .card")
        page.screenshot(path=f"{SHOT_DIR}/1_visits.png")

        page.click(".card.clickable >> nth=0")
        page.wait_for_selector(".modal.lightbox")
        page.wait_for_timeout(500)
        page.screenshot(path=f"{SHOT_DIR}/2_visit_lightbox.png")
        print("video toggle count:", page.locator('.lightbox-toggle button:has-text("Video")').count())
        page.click(".modal .close")

        page.click('.view-toggle button:has-text("Events")')
        page.wait_for_selector(".grid .card")
        page.screenshot(path=f"{SHOT_DIR}/3_events.png")
        page.click(".card.clickable >> nth=0")
        page.wait_for_selector(".modal.lightbox")
        page.wait_for_timeout(500)
        page.screenshot(path=f"{SHOT_DIR}/4_event_lightbox.png")
        page.click(".modal .close")

        page.click('.view-toggle button:has-text("Visits")')
        page.wait_for_selector(".grid .card")
        page.click(".card:has-text(\"car\")")
        page.wait_for_selector(".modal.lightbox")
        page.wait_for_timeout(500)
        print("connected events count:", page.locator(".connected-event-card").count())
        page.screenshot(path=f"{SHOT_DIR}/5_visit_connected.png")
        if page.locator(".connected-event-card").count() > 0:
            page.locator(".connected-event-card").nth(1).click()
            page.wait_for_timeout(500)
            page.screenshot(path=f"{SHOT_DIR}/6_connected_event.png")
            print("back bar count:", page.locator(".lightbox-back-bar button").count())
            page.click(".lightbox-back-bar button")
            page.wait_for_timeout(500)
            page.screenshot(path=f"{SHOT_DIR}/7_back_to_visit.png")
        page.click(".modal .close")

        page.click('.view-toggle button:has-text("Search")')
        page.wait_for_timeout(300)
        page.fill('input[type="text"]', "package delivery at the front door")
        page.click('button[type="submit"]')
        page.wait_for_selector(".grid .card")
        page.screenshot(path=f"{SHOT_DIR}/8_search_results.png")
        page.click(".card.clickable >> nth=0")
        page.wait_for_selector(".modal.lightbox")
        page.wait_for_timeout(500)
        page.screenshot(path=f"{SHOT_DIR}/9_search_lightbox.png")
        page.click(".modal .close")

        page.goto(f"{BASE}/ui/admin")
        page.wait_for_selector(".admin-card")
        page.wait_for_timeout(500)
        page.screenshot(path=f"{SHOT_DIR}/10_admin.png", full_page=True)

        browser.close()
        print("dry run done")


if __name__ == "__main__":
    main()
