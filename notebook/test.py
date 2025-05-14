import os
from contextlib import contextmanager

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


class ArchiveBrowser:
    """Thin wrapper around Selenium that logs in once and lets you grab
    authenticated pages. Fill in the CSS selectors marked TODO for your site."""

    def __init__(self, headless: bool = True, timeout: int = 10_000):
        opts = webdriver.ChromeOptions()
        if headless:
            # new headless mode (Chrome >= 109) better mimics regular browsing
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")
        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts,
        )
        # WebDriverWait uses seconds, Playwright uses ms – don't mix them up :-)
        self.wait = WebDriverWait(self.driver, timeout // 1000)

    # ---------------------------------------------------------------------
    # Public helpers
    # ---------------------------------------------------------------------
    def login(self, login_url: str, username: str | None = None, password: str | None = None) -> None:
        """Navigate to *login_url*, fill the form, and block until the user’s
        dashboard (or any post‑login element) is present.

        *username*/*password* default to env vars so you don’t ever hard‑code
        credentials – `export ARCHIVE_USER=...`, `export ARCHIVE_PASS=...`.
        """

        username = username or os.getenv("ARCHIVE_USER")
        password = password or os.getenv("ARCHIVE_PASS")
        if not (username and password):
            raise ValueError("username/password not supplied and env vars missing")

        self.driver.get(login_url)

        # TODO: change selectors below to match the site’s markup ----------------
        self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='username']"))).send_keys(username)
        self.driver.find_element(By.CSS_SELECTOR, "input[name='password']").send_keys(password)
        self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        # -----------------------------------------------------------------------

        # Wait for an element that only exists AFTER you’re logged in – adjust!
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".dashboard, .profile, .logout")))

    def grab_page_html(self, url: str) -> str:
        """Navigate to *url* (must be reachable by a logged‑in user) and return
        the final HTML after JavaScript settles."""
        self.driver.get(url)
        # If the page has dynamic content, wait for a sentinel element instead
        return self.driver.page_source

    def cookies_as_dict(self) -> dict[str, str]:
        """Return Selenium cookies in the format `{'name': 'value', ...}` – handy
        for passing into a `requests.Session`."""
        return {c["name"]: c["value"] for c in self.driver.get_cookies()}

    def quit(self) -> None:
        self.driver.quit()

    # ------------------------------------------------------------------
    # Context‑manager helpers so you can `with ArchiveBrowser() as br:`
    # ------------------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.quit()


# -----------------------------------------------------------------------------
# Convenience: spin up a Requests session with the same cookies – useful once
# you’ve confirmed the calendar API can be queried directly without Selenium.
# -----------------------------------------------------------------------------

def requests_session_from_browser(b: ArchiveBrowser):
    import requests

    s = requests.Session()
    for name, value in b.cookies_as_dict().items():
        s.cookies.set(name, value)
    return s


# -----------------------------------------------------------------------------
# Example CLI usage (execute with `python browser_module.py --help`)
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse, textwrap

    parser = argparse.ArgumentParser(
        description=textwrap.dedent(
            """\
            Log in to the archive site, dump an authenticated page’s HTML to STDOUT.
            All selectors are placeholders – adjust them to your target site.
            """
        )
    )
    parser.add_argument("login_url", help="URL of the sign‑in page")
    parser.add_argument("target_url", help="Page to fetch after login")
    parser.add_argument("--headed", action="store_true", help="open a visible browser window for debugging")
    args = parser.parse_args()

    with ArchiveBrowser(headless=not args.headed) as br:
        br.login(args.login_url)
        print(br.grab_page_html(args.target_url))

