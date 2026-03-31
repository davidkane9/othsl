import time
import undetected_chromedriver as uc

URL = "https://www.othsl.org/cgi-bin/socman.pl?DATADIR=25f&LDN=v2s"

options = uc.ChromeOptions()
options.add_argument("--window-size=1280,800")
driver = uc.Chrome(options=options, headless=False, version_main=146)
try:
    driver.get(URL)
    for i in range(10):
        time.sleep(3)
        if "just a moment" not in driver.title.lower():
            break
    with open("page.html", "w", encoding="utf-8") as f:
        f.write(driver.page_source)
    print("Saved page.html")
finally:
    driver.quit()
