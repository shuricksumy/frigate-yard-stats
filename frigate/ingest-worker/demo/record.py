import os
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8911"
API_KEY = "demo-key"
OUT_DIR = os.path.join(os.path.dirname(__file__), "recording")
SIZE = {"width": 1280, "height": 800}


def pause(seconds):
    time.sleep(seconds)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport=SIZE, record_video_dir=OUT_DIR, record_video_size=SIZE)
        page = context.new_page()

        page.goto(f"{BASE}/ui/index.html")
        page.fill('input[type="password"]', API_KEY)
        page.click('button:has-text("Continue")')
        page.wait_for_selector(".grid .card")
        pause(2.5)

        # -- Visits tab (default view): open the silver SUV visit -- animated preview first --
        page.click(".card.clickable >> nth=0")
        page.wait_for_selector(".modal.lightbox")
        pause(3.0)
        video_btn = page.locator('.lightbox-toggle button:has-text("Video")')
        if video_btn.count() and video_btn.is_visible():
            video_btn.click()
            pause(2.5)
        page.click(".modal .close")
        pause(1.0)

        # -- Events tab --
        page.click('.view-toggle button:has-text("Events")')
        page.wait_for_selector(".grid .card")
        pause(2.5)
        page.click(".card.clickable >> nth=0")
        page.wait_for_selector(".modal.lightbox")
        pause(3.0)
        page.click(".modal .close")
        pause(1.0)

        # -- Connected events / back-navigation on the visit with two grouped det_ids --
        page.click('.view-toggle button:has-text("Visits")')
        page.wait_for_selector(".grid .card")
        pause(1.0)
        # The driveway visit has event_count > 1 -- open it and show "Connected events".
        page.click('.card:has-text("car")')
        page.wait_for_selector(".modal.lightbox")
        pause(2.0)
        if page.locator(".connected-event-card").count() > 0:
            page.locator(".connected-event-card").nth(1).click()
            pause(2.5)
            page.click(".lightbox-back-bar button")
            pause(2.0)
        page.click(".modal .close")
        pause(1.0)

        # -- Search tab --
        page.click('.view-toggle button:has-text("Search")')
        pause(1.0)
        page.fill('input[type="text"]', "package delivery at the front door")
        page.click('button[type="submit"]')
        page.wait_for_selector(".grid .card")
        pause(2.5)
        page.click(".card.clickable >> nth=0")
        page.wait_for_selector(".modal.lightbox")
        pause(3.0)
        page.click(".modal .close")
        pause(1.0)

        # -- Admin dashboard --
        page.goto(f"{BASE}/ui/admin")
        page.wait_for_selector(".admin-card")
        pause(2.0)
        page.click('button:has-text("Check now")')
        pause(2.0)
        page.mouse.wheel(0, 500)
        pause(2.0)
        page.mouse.wheel(0, 700)
        pause(2.5)
        page.mouse.wheel(0, 700)
        pause(2.5)

        context.close()
        browser.close()
        print("recording done")


if __name__ == "__main__":
    main()
