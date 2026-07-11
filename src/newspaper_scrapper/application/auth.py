"""Browser launch and authentication flows."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from newspaper_scrapper.adapters.chrome import applescript, cdp, process
from newspaper_scrapper.adapters.newspapers import papers
from newspaper_scrapper.config import Settings


def _tab_is_connectable(ws_url: str) -> bool:
    try:
        cdp.evaluate_json(
            ws_url,
            "JSON.stringify({href: location.href, title: document.title})",
        )
        return True
    except Exception:
        return False


def launch_browser(settings: Settings, *, force_new_instance: bool = False) -> dict[str, str]:
    process.launch_real_chrome(
        settings,
        start_url=settings.home_url,
        force_new_instance=force_new_instance,
    )
    return {
        "chrome_debug_base": settings.chrome_debug_base,
        "chrome_profile_dir": str(settings.chrome_profile_dir),
        "chrome_app_name": settings.chrome_app_name,
    }


def store_credentials(
    settings: Settings,
    *,
    email: str,
    password: str,
    output_path: Path | None = None,
) -> Path:
    path = output_path or settings.auth_env_file
    path.write_text(
        "\n".join(
            [
                f"NEWSCOM_LOGIN_EMAIL={email}",
                f"NEWSCOM_LOGIN_PASSWORD={password}",
                "",
            ]
        )
    )
    path.chmod(0o600)
    return path


def _find_or_open_newspapers_tab(settings: Settings) -> dict[str, str]:
    pages = cdp.list_page_tabs(settings.chrome_debug_base)
    for page in pages:
        if "newspapers.com" in str(page.get("url", "")):
            ws_url = str(page.get("webSocketDebuggerUrl", "") or "")
            if not ws_url or not _tab_is_connectable(ws_url):
                continue
            return {
                "url": str(page.get("url", "")),
                "ws_url": ws_url,
            }
    applescript.navigate_front_tab(settings.chrome_app_name, settings.home_url)
    time.sleep(settings.page_load_seconds)
    pages = cdp.list_page_tabs(settings.chrome_debug_base)
    for page in pages:
        if "newspapers.com" not in str(page.get("url", "")):
            continue
        ws_url = str(page.get("webSocketDebuggerUrl", "") or "")
        if not ws_url or not _tab_is_connectable(ws_url):
            continue
        return {"url": str(page.get("url", "")), "ws_url": ws_url}
    ws_url = cdp.find_page_ws_url(settings.chrome_debug_base, settings.home_url)
    return {"url": settings.home_url, "ws_url": ws_url}


def login(
    settings: Settings,
    *,
    fill_credentials: bool = True,
    wait_seconds: float = 180.0,
) -> dict[str, str]:
    launch_browser(settings)
    tab = _find_or_open_newspapers_tab(settings)
    cdp.navigate(tab["ws_url"], settings.login_url)
    time.sleep(settings.page_load_seconds)

    prefill_state: dict[str, object] = {}
    if fill_credentials and settings.newspapers_email and settings.newspapers_password:
        prefill_state = cdp.evaluate_json(
            tab["ws_url"],
            papers.login_prefill_expression(
                settings.newspapers_email,
                settings.newspapers_password,
            ),
        )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        status = auth_status(settings, navigate=False)
        if status["signed_in"] == "true":
            return {
                "status": "signed_in",
                "prefill_email": str(prefill_state.get("emailOk", False)).lower(),
                "prefill_password": str(prefill_state.get("passwordOk", False)).lower(),
                "account_url": status["url"],
            }
        time.sleep(settings.login_poll_seconds)

    return {
        "status": "timed_out",
        "prefill_email": str(prefill_state.get("emailOk", False)).lower(),
        "prefill_password": str(prefill_state.get("passwordOk", False)).lower(),
        "account_url": "",
    }


def auth_status(settings: Settings, *, navigate: bool = True) -> dict[str, str]:
    launch_browser(settings)
    tab = _find_or_open_newspapers_tab(settings)
    target_url = "https://www.newspapers.com/account/"
    if navigate:
        cdp.navigate(tab["ws_url"], target_url)
        time.sleep(settings.page_load_seconds)
    state = cdp.evaluate_json(tab["ws_url"], papers.auth_status_expression())
    title = str(state.get("title", ""))
    url = str(state.get("url", ""))
    body = str(state.get("bodySnippet", ""))
    loginish = (
        "/login" in url
        or "Sign in to Newspapers.com" in title
        or "Sign in to Newspapers.com" in body
        or bool(state.get("hasSignInText"))
    )
    positive = (
        "/account" in url
        or bool(state.get("hasAccountLink"))
        or bool(state.get("hasSignedInSignal"))
    )
    signed_in = positive and not loginish
    return {
        "signed_in": "true" if signed_in else "false",
        "title": title,
        "url": url,
        "has_account_link": "true" if state.get("hasAccountLink") else "false",
        "has_signed_in_signal": "true" if state.get("hasSignedInSignal") else "false",
    }


COOKIE_FIELD_NAMES = [
    "name",
    "value",
    "domain",
    "path",
    "secure",
    "httpOnly",
    "sameSite",
    "expires",
]


def export_cookies(
    settings: Settings,
    *,
    output_path: Path,
    urls: list[str] | None = None,
) -> dict[str, Any]:
    launch_browser(settings)
    tab = _find_or_open_newspapers_tab(settings)
    cdp.call_method(tab["ws_url"], "Network.enable")
    result = cdp.call_method(
        tab["ws_url"],
        "Network.getCookies",
        {
            "urls": urls
            or [
                "https://www.newspapers.com/",
                "https://img.newspapers.com/",
            ]
        },
    )
    cookies = []
    for raw in result.get("cookies", []):
        cookies.append({field: raw.get(field) for field in COOKIE_FIELD_NAMES if field in raw})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"cookies": cookies}, indent=2))
    return {
        "output_path": str(output_path),
        "cookie_count": len(cookies),
    }


def import_cookies(
    settings: Settings,
    *,
    cookies_json: Path,
    navigate_url: str = "https://www.newspapers.com/account/",
    wait_seconds: float | None = None,
    force_new_instance: bool = False,
) -> dict[str, Any]:
    launch_browser(settings, force_new_instance=force_new_instance)
    tab = _find_or_open_newspapers_tab(settings)
    payload = json.loads(cookies_json.read_text())
    cookies = payload.get("cookies", [])
    cdp.call_method(tab["ws_url"], "Network.enable")
    cdp.call_method(tab["ws_url"], "Page.enable")
    cdp.call_method(tab["ws_url"], "Runtime.enable")
    cdp.call_method(tab["ws_url"], "Network.setCookies", {"cookies": cookies})
    cdp.navigate(tab["ws_url"], navigate_url)
    time.sleep(wait_seconds if wait_seconds is not None else settings.page_load_seconds)
    status = auth_status(settings, navigate=False)
    return {
        "cookies_json": str(cookies_json),
        "cookie_count": len(cookies),
        "navigate_url": navigate_url,
        "auth_status": status,
        "force_new_instance": force_new_instance,
    }
