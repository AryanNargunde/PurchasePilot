"""
selenium_diagnostic.py
-----------------------
Standalone test: opens a VISIBLE (not headless) Chrome browser on an Amazon
product/review page, scrolls it, and SAVES a screenshot + the rendered HTML
to disk so they can be inspected directly, without needing to interpret
anything yourself.

Run with:  python selenium_diagnostic.py "https://www.amazon.in/dp/XXXXXXXXXX"

After running, look in the same folder for:
  - diagnostic_screenshot.png   <- upload this one to Claude
  - diagnostic_page.html        <- (optional, for deeper digging)
"""

import sys
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

if len(sys.argv) < 2:
    print("Usage: python selenium_diagnostic.py <amazon product or review URL>")
    sys.exit(1)

url = sys.argv[1]

options = Options()
options.add_argument("--window-size=1280,2200")

driver = webdriver.Chrome(options=options)
try:
    print(f"Opening: {url}")
    driver.get(url)
    time.sleep(3)

    for i in range(4):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)

    review_blocks = driver.find_elements("css selector", "[data-hook='review-body']")
    print(f"\nFound {len(review_blocks)} review-body elements.")

    driver.save_screenshot("diagnostic_screenshot.png")
    with open("diagnostic_page.html", "w", encoding="utf-8") as f:
        f.write(driver.page_source)

    print("\nSaved diagnostic_screenshot.png and diagnostic_page.html in this folder.")
    print("Upload diagnostic_screenshot.png to Claude in the chat.")

    time.sleep(2)
finally:
    driver.quit()