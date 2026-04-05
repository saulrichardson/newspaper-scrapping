"""Browser-rendered screenshot capture for Newspapers.com image pages."""

from __future__ import annotations

import csv
import json
import random
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from PIL import Image, ImageStat

from newspaper_scrapper.adapters.chrome import cdp
from newspaper_scrapper.adapters.newspapers import image
from newspaper_scrapper.application.auth import launch_browser
from newspaper_scrapper.application.download import (
    error_type_and_message,
    read_manifest,
)
from newspaper_scrapper.config import Settings


Image.MAX_IMAGE_PIXELS = None


RESULT_FIELDNAMES = [
    "run_index",
    "issue_id",
    "issue_date",
    "page_num",
    "preferred_image_id",
    "preferred_image_page_url",
    "status",
    "output_path",
    "selected_strategy",
    "mean_luma",
    "bright240_fraction",
    "natural_width",
    "natural_height",
    "elapsed_seconds",
    "page_attempts",
    "applied_sleep_seconds",
    "probe_seconds",
    "hydrate_seconds",
    "render_seconds",
    "settle_seconds",
    "capture_seconds",
    "validation_seconds",
    "browser_recycled_after_page",
    "error_type",
    "error_message",
]
MANIFEST_FIELDNAMES = [
    "issue_id",
    "issue_date",
    "page_num",
    "preferred_image_id",
    "preferred_image_page_url",
]
PRODUCTION_RESULT_FIELDNAMES = RESULT_FIELDNAMES + [
    "pass_index",
    "pass_name",
]


STRATEGY_AUTO = "auto"
STRATEGY_VIEWER = "viewer_upgraded"
STRATEGY_TILES = "synthetic_tiles"
STRATEGY_FULL = "synthetic_full_image"
STRATEGY_CHOICES = {
    STRATEGY_AUTO,
    STRATEGY_VIEWER,
    STRATEGY_TILES,
    STRATEGY_FULL,
}
MIN_HYDRATED_SIGNED_IMAGES = 4
POST_RENDER_SETTLE_SECONDS = 0.25
FULL_IMAGE_ATTEMPTS = 5
FULL_IMAGE_RETRY_SLEEP_SECONDS = 0.75
TILE_PROBE_WAIT_SECONDS = 4.0
PAGE_CAPTURE_ATTEMPTS = 3
RETRYABLE_PAGE_CAPTURE_MARKERS = (
    "Target crashed",
    "No open Newspapers.com Chrome tab found",
    "WebSocket",
    "websocket",
    "Chrome debug port did not come up",
    "CDP Page.enable timed out",
    "CDP Runtime.enable timed out",
    "CDP Runtime.evaluate timed out",
    "CDP Page.navigate timed out",
    "Page.captureScreenshot timed out",
)
RETRYABLE_VALIDATION_MARKERS = (
    "synthetic_full_image produced",
    "synthetic_full_image did not report",
    "synthetic_tile_canvas did not complete",
)
STOP_REASON_AUTH_REQUIRED = "auth_required"
STOP_REASON_CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
STOP_REASON_BROWSER_UNHEALTHY = "browser_unhealthy"
MAX_CONSECUTIVE_RETRYABLE_FAILURES = 5
AUTH_REQUIRED_MARKERS = (
    "you need a subscription to view this page",
    "try 7 days free",
    "gain immediate access to this page",
    "unlimited access to 1 billion+ pages",
    "chrome session is no longer signed into newspapers.com",
    "sign in to newspapers.com",
)
CHALLENGE_MARKERS = (
    "cloudflare challenge",
    "access denied",
    "just a moment",
    "checking your browser",
    "verify you are human",
    "security check",
)


class ScreenshotRunStopped(RuntimeError):
    def __init__(self, stop_reason: str, message: str) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason


@dataclass
class AdaptiveSleepController:
    enabled: bool
    current_sleep_seconds: float
    min_sleep_seconds: float
    max_sleep_seconds: float
    step_seconds: float
    clean_streak_threshold: int
    slow_page_threshold_seconds: float
    clean_streak: int = 0
    last_adjustment_reason: str = ""

    def record_page_result(
        self,
        *,
        elapsed_seconds: float,
        page_attempts: int,
        had_retryable_error: bool,
    ) -> float:
        if not self.enabled:
            self.last_adjustment_reason = "disabled"
            return self.current_sleep_seconds

        clean_page = (
            not had_retryable_error
            and page_attempts <= 1
            and elapsed_seconds > 0
            and elapsed_seconds <= self.slow_page_threshold_seconds
        )

        if clean_page:
            self.clean_streak += 1
            if self.clean_streak >= self.clean_streak_threshold:
                previous = self.current_sleep_seconds
                self.current_sleep_seconds = max(
                    self.min_sleep_seconds,
                    self.current_sleep_seconds - self.step_seconds,
                )
                self.clean_streak = 0
                self.last_adjustment_reason = (
                    "decreased_after_clean_streak"
                    if self.current_sleep_seconds < previous
                    else "at_min_sleep"
                )
            else:
                self.last_adjustment_reason = "clean_streak_building"
        else:
            previous = self.current_sleep_seconds
            self.clean_streak = 0
            self.current_sleep_seconds = min(
                self.max_sleep_seconds,
                self.current_sleep_seconds + self.step_seconds,
            )
            self.last_adjustment_reason = (
                "increased_after_slow_or_retried_page"
                if self.current_sleep_seconds > previous
                else "at_max_sleep"
            )
        return self.current_sleep_seconds


def _classify_blocking_stop_reason(exc: Exception) -> str:
    message = str(exc).lower()
    if any(marker in message for marker in AUTH_REQUIRED_MARKERS):
        return STOP_REASON_AUTH_REQUIRED
    if any(marker in message for marker in CHALLENGE_MARKERS):
        return STOP_REASON_CLOUDFLARE_CHALLENGE
    return ""


def _find_newspapers_tab_ws(settings: Settings) -> str:
    pages = cdp.list_page_tabs(settings.chrome_debug_base)
    for page in pages:
        if "newspapers.com" in str(page.get("url", "")):
            ws_url = page.get("webSocketDebuggerUrl")
            if ws_url:
                return str(ws_url)
    raise RuntimeError("No open Newspapers.com Chrome tab found")


def _page_size_from_probe(probe: dict[str, Any]) -> tuple[int, int]:
    page_meta = probe.get("pageMeta") or {}
    meta_width = str(page_meta.get("width", "")).strip()
    meta_height = str(page_meta.get("height", "")).strip()
    if meta_width.isdigit() and meta_height.isdigit():
        return int(meta_width), int(meta_height)

    thumbnail = probe.get("thumbnail") or {}
    width = str(thumbnail.get("width", "")).strip()
    height = str(thumbnail.get("height", "")).strip()
    if not width.isdigit() or not height.isdigit():
        raise RuntimeError("Could not determine rendered page size from image page DOM")
    return int(width), int(height)


def _probe_ready(probe: dict[str, Any]) -> bool:
    page_meta = probe.get("pageMeta") or {}
    meta_width = str(page_meta.get("width", "")).strip()
    meta_height = str(page_meta.get("height", "")).strip()
    meta_iat = str(page_meta.get("iat", "")).strip()
    meta_user = str(page_meta.get("user", "")).strip()
    meta_image_id = str(page_meta.get("imageId", "")).strip()
    if (
        meta_width.isdigit()
        and meta_height.isdigit()
        and meta_iat
        and meta_user
        and meta_image_id.isdigit()
    ):
        return True
    tile = probe.get("tile") or {}
    thumbnail = probe.get("thumbnail") or {}
    return bool(tile.get("href")) and bool(thumbnail.get("width")) and bool(
        thumbnail.get("height")
    )


def _enrich_probe(probe: dict[str, Any]) -> dict[str, Any]:
    probe = dict(probe)
    page_meta = image.extract_page_metadata_from_script_texts(
        probe.get("scriptTexts") or [],
        user=str(probe.get("user", "")).strip(),
    )
    probe["pageMeta"] = page_meta
    return probe


def _probe_is_hydrated(probe: dict[str, Any]) -> bool:
    if int(probe.get("signedImageCount") or 0) >= MIN_HYDRATED_SIGNED_IMAGES:
        return True
    page_meta = probe.get("pageMeta") or {}
    return bool(page_meta.get("iat")) and int(probe.get("imageElementCount") or 0) > 0


def _wait_for_hydrated_probe(
    ws_url: str,
    *,
    initial_probe: dict[str, Any],
    wait_seconds: float,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    probe = initial_probe
    if _probe_is_hydrated(probe):
        return probe

    deadline = time.time() + wait_seconds
    while True:
        if time.time() >= deadline:
            return probe
        time.sleep(min(poll_seconds, max(deadline - time.time(), 0)))
        probe = _enrich_probe(cdp.evaluate_json(ws_url, image.page_probe_expression()))
        title = str(probe.get("title", ""))
        body = str(probe.get("bodySnippet", ""))
        if "Cloudflare" in title or "Access denied" in title or "Cloudflare" in body:
            raise RuntimeError("Cloudflare challenge while waiting for viewer hydration")
        if _probe_is_hydrated(probe):
            return probe


def _wait_for_page_probe(
    ws_url: str,
    *,
    wait_seconds: float,
    poll_seconds: float = 0.25,
) -> dict[str, Any]:
    deadline = time.time() + wait_seconds
    last_probe: dict[str, Any] = {}
    while True:
        probe = _enrich_probe(cdp.evaluate_json(ws_url, image.page_probe_expression()))
        last_probe = probe
        title = str(probe.get("title", ""))
        body = str(probe.get("bodySnippet", ""))
        if "Cloudflare" in title or "Access denied" in title or "Cloudflare" in body:
            raise RuntimeError("Cloudflare challenge while waiting for image page DOM")
        if _probe_ready(probe):
            return probe
        if time.time() >= deadline:
            break
        time.sleep(min(poll_seconds, max(deadline - time.time(), 0)))
    raise RuntimeError(
        "Timed out waiting for image page DOM to expose signed image metadata: "
        f"{json.dumps(last_probe)[:500]}"
    )


def _navigate_and_probe(
    ws_url: str,
    *,
    image_page_url: str,
    page_load_seconds: float,
    render_wait_seconds: float,
) -> dict[str, Any]:
    cdp.navigate(ws_url, image_page_url)
    probe = _wait_for_page_probe(ws_url, wait_seconds=page_load_seconds)
    return _wait_for_hydrated_probe(
        ws_url,
        initial_probe=probe,
        wait_seconds=render_wait_seconds,
    )


def _wait_for_tile_probe(
    ws_url: str,
    *,
    initial_probe: dict[str, Any],
    wait_seconds: float,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    probe = initial_probe
    if (probe.get("tile") or {}).get("href"):
        return probe

    deadline = time.time() + wait_seconds
    while True:
        if time.time() >= deadline:
            return probe
        time.sleep(min(poll_seconds, max(deadline - time.time(), 0)))
        probe = _enrich_probe(cdp.evaluate_json(ws_url, image.page_probe_expression()))
        title = str(probe.get("title", ""))
        body = str(probe.get("bodySnippet", ""))
        if "Cloudflare" in title or "Access denied" in title or "Cloudflare" in body:
            raise RuntimeError("Cloudflare challenge while waiting for signed tile probe")
        if (probe.get("tile") or {}).get("href"):
            return probe


def _expand_viewer_expression(page_width: int, page_height: int) -> str:
    return f"""JSON.stringify((() => {{
  const href = (el) => el.getAttribute('href') || el.getAttributeNS('http://www.w3.org/1999/xlink', 'href') || '';
  const hide = (sel) => document.querySelectorAll(sel).forEach((el) => {{
    el.style.display = 'none';
  }});
  hide('main');
  hide('#pagination-pane');
  hide('#viewer-pagination-bar');
  hide('#viewer-information-pane');

  document.documentElement.style.margin = '0';
  document.body.style.margin = '0';
  document.body.style.overflow = 'hidden';
  document.body.style.background = '#ffffff';
  document.documentElement.style.background = '#ffffff';

  const viewer = document.getElementById('viewer');
  const svg = document.getElementById('svg-viewer');
  const signed = Array.from(document.querySelectorAll('#svg-viewer image'))
    .map((el) => {{
      return {{
        x: Number(el.getAttribute('x') || 0),
        y: Number(el.getAttribute('y') || 0),
        w: Number(el.getAttribute('width') || 0),
        h: Number(el.getAttribute('height') || 0),
        href: href(el),
      }};
    }})
    .filter((item) => item.href.startsWith('https://img.newspapers.com/img/img?'));

  const minX = signed.length ? Math.min(...signed.map((item) => item.x)) : 0;
  const minY = signed.length ? Math.min(...signed.map((item) => item.y)) : 0;
  const maxX = signed.length ? Math.max(...signed.map((item) => item.x + item.w)) : 0;
  const maxY = signed.length ? Math.max(...signed.map((item) => item.y + item.h)) : 0;

  if (viewer) {{
    viewer.style.position = 'absolute';
    viewer.style.left = '0px';
    viewer.style.top = '0px';
    viewer.style.width = '{page_width}px';
    viewer.style.height = '{page_height}px';
    viewer.style.margin = '0';
    viewer.style.background = '#ffffff';
    viewer.scrollTop = 0;
    viewer.scrollLeft = 0;
  }}
  if (svg) {{
    svg.setAttribute('viewBox', `${{minX}} ${{minY}} ${{Math.max(maxX - minX, 1)}} ${{Math.max(maxY - minY, 1)}}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('width', '{page_width}');
    svg.setAttribute('height', '{page_height}');
    svg.style.width = '{page_width}px';
    svg.style.height = '{page_height}px';
    svg.style.background = '#ffffff';
  }}

  return {{
    strategy: 'expand_viewer',
    signedCount: signed.length,
    extents: [minX, minY, maxX, maxY],
    viewBox: svg ? svg.getAttribute('viewBox') : null,
    devicePixelRatio: window.devicePixelRatio,
  }};
}})())"""


def _upgrade_tiles_expression(page_width: int, page_height: int) -> str:
    return f"""JSON.stringify((() => {{
  const href = (el) => el.getAttribute('href') || el.getAttributeNS('http://www.w3.org/1999/xlink', 'href') || '';
  const setHref = (el, value) => {{
    el.setAttribute('href', value);
    el.setAttributeNS('http://www.w3.org/1999/xlink', 'href', value);
  }};
  const viewer = document.getElementById('viewer');
  const svg = document.getElementById('svg-viewer');
  const signed = Array.from(document.querySelectorAll('#svg-viewer image'))
    .filter((el) => href(el).startsWith('https://img.newspapers.com/img/img?'));

  for (const el of signed) {{
    const url = new URL(href(el));
    const crop = (url.searchParams.get('crop') || '').split(',').map((value) => Number(value || 0));
    const cropX = crop[0] || 0;
    const cropY = crop[1] || 0;
    const cropW = crop[2] || 0;
    const cropH = crop[3] || 0;
    if (cropW > 0 && cropH > 0) {{
      url.searchParams.set('width', String(cropW));
      url.searchParams.set('height', String(cropH));
      setHref(el, url.toString());
      el.setAttribute('x', String(cropX));
      el.setAttribute('y', String(cropY));
      el.setAttribute('width', String(cropW));
      el.setAttribute('height', String(cropH));
    }}
  }}

  if (viewer) {{
    viewer.style.width = '{page_width}px';
    viewer.style.height = '{page_height}px';
  }}
  if (svg) {{
    svg.setAttribute('width', '{page_width}');
    svg.setAttribute('height', '{page_height}');
    svg.style.width = '{page_width}px';
    svg.style.height = '{page_height}px';
  }}

  const upgraded = Array.from(document.querySelectorAll('#svg-viewer image'))
    .filter((el) => href(el).startsWith('https://img.newspapers.com/img/img?'));
  return {{
    strategy: 'upgrade_tiles',
    signedCount: upgraded.length,
    sample: upgraded.slice(0, 6).map((el) => {{
      return {{
        href: href(el),
        x: el.getAttribute('x') || '',
        y: el.getAttribute('y') || '',
        width: el.getAttribute('width') || '',
        height: el.getAttribute('height') || '',
      }};
    }}),
  }};
}})())"""


def _tile_state_expression() -> str:
    return r"""JSON.stringify((() => {
  const href = (el) => el.getAttribute('href') || el.getAttributeNS('http://www.w3.org/1999/xlink', 'href') || '';
  const signed = Array.from(document.querySelectorAll('#svg-viewer image'))
    .map((el) => ({
      x: Number(el.getAttribute('x') || 0),
      y: Number(el.getAttribute('y') || 0),
      w: Number(el.getAttribute('width') || 0),
      h: Number(el.getAttribute('height') || 0),
      href: href(el),
    }))
    .filter((item) => item.href.startsWith('https://img.newspapers.com/img/img?'));
  const minX = signed.length ? Math.min(...signed.map((item) => item.x)) : 0;
  const minY = signed.length ? Math.min(...signed.map((item) => item.y)) : 0;
  const maxX = signed.length ? Math.max(...signed.map((item) => item.x + item.w)) : 0;
  const maxY = signed.length ? Math.max(...signed.map((item) => item.y + item.h)) : 0;
  return {
    signedCount: signed.length,
    extents: [minX, minY, maxX, maxY],
  };
})())"""


def _normalize_image_size(output_path: Path, *, page_width: int, page_height: int) -> dict[str, Any]:
    with Image.open(output_path) as captured:
        actual_width, actual_height = captured.size
        if (actual_width, actual_height) == (page_width, page_height):
            return {
                "normalized": False,
                "pixel_width": actual_width,
                "pixel_height": actual_height,
            }
        width_ratio = actual_width / page_width
        height_ratio = actual_height / page_height
        if abs(width_ratio - height_ratio) < 0.01 and width_ratio > 1.0:
            resized = captured.resize((page_width, page_height), Image.Resampling.LANCZOS)
            resized.save(output_path)
            return {
                "normalized": True,
                "pixel_width": page_width,
                "pixel_height": page_height,
                "original_pixel_width": actual_width,
                "original_pixel_height": actual_height,
            }
        return {
            "normalized": False,
            "pixel_width": actual_width,
            "pixel_height": actual_height,
        }


def _image_metrics(image_path: Path) -> dict[str, float]:
    with Image.open(image_path) as captured:
        grayscale = captured.convert("L")
        stats = ImageStat.Stat(grayscale)
        histogram = grayscale.histogram()
        total = float(sum(histogram))
        bright240 = float(sum(histogram[240:])) / total if total else 1.0
        width, height = grayscale.size
        quartile_luma: list[float] = []
        for quartile_index in range(4):
            y0 = round(quartile_index * height / 4)
            y1 = round((quartile_index + 1) * height / 4)
            band = grayscale.crop((0, y0, width, y1))
            quartile_luma.append(float(ImageStat.Stat(band).mean[0]))
        column_luma: list[float] = []
        for quartile_index in range(4):
            x0 = round(quartile_index * width / 4)
            x1 = round((quartile_index + 1) * width / 4)
            band = grayscale.crop((x0, 0, x1, height))
            column_luma.append(float(ImageStat.Stat(band).mean[0]))
        return {
            "mean_luma": float(stats.mean[0]),
            "stddev_luma": float(stats.stddev[0]),
            "bright240_fraction": bright240,
            "quartile_luma": quartile_luma,
            "quartile_spread": max(quartile_luma) - min(quartile_luma),
            "column_luma": column_luma,
            "column_spread": max(column_luma) - min(column_luma),
        }


def _wait_for_tiles(
    ws_url: str,
    *,
    wait_seconds: float,
    poll_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    deadline = time.time() + wait_seconds
    while True:
        state = cdp.evaluate_json(ws_url, _tile_state_expression())
        state["timestamp"] = time.time()
        states.append(state)
        if time.time() >= deadline:
            break
        time.sleep(min(poll_seconds, max(deadline - time.time(), 0)))
    return states


def _tile_grid_from_probe(probe: dict[str, Any]) -> list[dict[str, Any]]:
    tile = probe.get("tile") or {}
    thumbnail = probe.get("thumbnail") or {}
    tile_href = str(tile.get("href", "")).strip()
    full_width = str(thumbnail.get("width", "")).strip()
    full_height = str(thumbnail.get("height", "")).strip()
    if not tile_href:
        raise RuntimeError("No signed tile href found in the live page DOM")
    if not full_width.isdigit() or not full_height.isdigit():
        raise RuntimeError("Could not determine full page size from the page thumbnail")

    parsed = urlparse(tile_href)
    query = parse_qs(parsed.query)
    crop = [int(part or 0) for part in query.get("crop", ["0,0,0,0"])[0].split(",")]
    if len(crop) != 4 or crop[2] <= 0 or crop[3] <= 0:
        raise RuntimeError(f"Could not parse tile crop from signed tile href: {tile_href}")

    crop_width = crop[2]
    crop_height = crop[3]
    page_width = int(full_width)
    page_height = int(full_height)
    base_query = {
        "id": query["id"][0],
        "user": query["user"][0],
        "iat": query["iat"][0],
        "brightness": query.get("brightness", ["0"])[0],
        "contrast": query.get("contrast", ["0"])[0],
        "invert": query.get("invert", ["0"])[0],
        "ts": query.get("ts", ["1"])[0],
        "cacheable": query.get("cacheable", ["1"])[0],
    }

    tiles: list[dict[str, Any]] = []
    y = 0
    while y < page_height:
        tile_h = min(crop_height, page_height - y)
        x = 0
        while x < page_width:
            tile_w = min(crop_width, page_width - x)
            params = dict(base_query)
            params["width"] = str(tile_w)
            params["height"] = str(tile_h)
            params["crop"] = f"{x},{y},{tile_w},{tile_h}"
            href = f"https://img.newspapers.com/img/img?{urlencode(params)}"
            tiles.append(
                {
                    "href": href,
                    "x": x,
                    "y": y,
                    "width": tile_w,
                    "height": tile_h,
                }
            )
            x += crop_width
        y += crop_height
    return tiles


def _render_tile_canvas_expression(
    *,
    tiles: list[dict[str, Any]],
    page_width: int,
    page_height: int,
) -> str:
    payload = {
        "pageWidth": page_width,
        "pageHeight": page_height,
        "tiles": tiles,
    }
    return f"""JSON.stringify((() => {{
  const payload = {json.dumps(payload)};
  window.__newscomScreenshotState = {{
    strategy: 'synthetic_tile_canvas',
    expected: payload.tiles.length,
    loaded: 0,
    failed: 0,
    complete: false,
  }};
  const maybeComplete = () => {{
    const state = window.__newscomScreenshotState;
    if (state.loaded + state.failed < state.expected) {{
      return;
    }}
    requestAnimationFrame(() => requestAnimationFrame(() => {{
      state.complete = true;
    }}));
  }};
  document.documentElement.innerHTML = '<head><meta charset="utf-8"><style>html,body{{margin:0;padding:0;background:#fff;overflow:hidden}}#page{{position:relative;background:#fff}}#page canvas{{display:block;margin:0;padding:0;border:0;background:#fff}}</style></head><body><div id="page"><canvas id="page-canvas"></canvas></div></body>';
  const page = document.getElementById('page');
  const canvas = document.getElementById('page-canvas');
  const ctx = canvas.getContext('2d', {{ alpha: false }});
  page.style.width = `${{payload.pageWidth}}px`;
  page.style.height = `${{payload.pageHeight}}px`;
  canvas.width = payload.pageWidth;
  canvas.height = payload.pageHeight;
  canvas.style.width = `${{payload.pageWidth}}px`;
  canvas.style.height = `${{payload.pageHeight}}px`;
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, payload.pageWidth, payload.pageHeight);
  for (const tile of payload.tiles) {{
    const img = document.createElement('img');
    img.loading = 'eager';
    img.decoding = 'sync';
    img.src = tile.href;
    img.addEventListener('load', () => {{
      try {{
        ctx.drawImage(img, tile.x, tile.y, tile.width, tile.height);
      }} catch (error) {{
        window.__newscomScreenshotState.failed += 1;
        maybeComplete();
        return;
      }}
      window.__newscomScreenshotState.loaded += 1;
      maybeComplete();
    }});
    img.addEventListener('error', () => {{
      window.__newscomScreenshotState.failed += 1;
      maybeComplete();
    }});
  }}
  return window.__newscomScreenshotState;
}})())"""


def _render_full_image_expression(
    *,
    full_image_url: str,
    page_width: int,
    page_height: int,
) -> str:
    payload = {
        "pageWidth": page_width,
        "pageHeight": page_height,
        "fullImageUrl": full_image_url,
    }
    return f"""JSON.stringify((() => {{
  const payload = {json.dumps(payload)};
  window.__newscomScreenshotState = {{
    strategy: 'synthetic_full_image',
    expected: 1,
    expectedWidth: payload.pageWidth,
    expectedHeight: payload.pageHeight,
    loaded: 0,
    failed: 0,
    naturalWidth: 0,
    naturalHeight: 0,
    complete: false,
  }};
  document.documentElement.innerHTML = '<head><meta charset="utf-8"><style>html,body{{margin:0;padding:0;background:#fff;overflow:hidden}}#page{{position:relative;background:#fff}}#page img{{display:block;margin:0;padding:0;border:0}}</style></head><body><div id="page"><img id="full-image"></div></body>';
  const page = document.getElementById('page');
  page.style.width = `${{payload.pageWidth}}px`;
  page.style.height = `${{payload.pageHeight}}px`;
  const img = document.getElementById('full-image');
  img.style.width = `${{payload.pageWidth}}px`;
  img.style.height = `${{payload.pageHeight}}px`;
  img.decoding = 'sync';
  img.loading = 'eager';
  const syncState = () => {{
    window.__newscomScreenshotState.naturalWidth = Number(img.naturalWidth || 0);
    window.__newscomScreenshotState.naturalHeight = Number(img.naturalHeight || 0);
    if (
      Number(img.naturalWidth || 0) === payload.pageWidth &&
      Number(img.naturalHeight || 0) === payload.pageHeight &&
      Boolean(img.complete)
    ) {{
      window.__newscomScreenshotState.complete = true;
    }}
  }};
  const finalize = async () => {{
    window.__newscomScreenshotState.loaded = 1;
    syncState();
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
    syncState();
  }};
  img.addEventListener('load', () => {{
    finalize();
  }});
  img.addEventListener('error', () => {{
    window.__newscomScreenshotState.failed = 1;
    syncState();
    window.__newscomScreenshotState.complete = true;
  }});
  img.src = payload.fullImageUrl;
  return window.__newscomScreenshotState;
}})())"""


def _screenshot_render_state_expression() -> str:
    return r"""JSON.stringify((() => {
  const state = window.__newscomScreenshotState || null;
  if (!state) {
    return {
      present: false,
    };
  }
  const img = state.strategy === 'synthetic_full_image'
    ? document.getElementById('full-image')
    : null;
  const naturalWidth = img ? Number(img.naturalWidth || 0) : Number(state.naturalWidth || 0);
  const naturalHeight = img ? Number(img.naturalHeight || 0) : Number(state.naturalHeight || 0);
  const expectedWidth = Number(state.expectedWidth || 0);
  const expectedHeight = Number(state.expectedHeight || 0);
  const loaded = img && img.complete && naturalWidth > 0
    ? Math.max(Number(state.loaded || 0), 1)
    : Number(state.loaded || 0);
  const complete = state.strategy === 'synthetic_full_image'
    ? (
        expectedWidth > 0 &&
        expectedHeight > 0 &&
        naturalWidth === expectedWidth &&
        naturalHeight === expectedHeight &&
        Boolean(img && img.complete)
      ) || Boolean(state.complete)
    : Boolean(state.complete);
  return {
    present: true,
    strategy: state.strategy || '',
    expected: Number(state.expected || 0),
    loaded,
    failed: Number(state.failed || 0),
    naturalWidth,
    naturalHeight,
    complete,
  };
})())"""


def _wait_for_render_completion(
    ws_url: str,
    *,
    wait_seconds: float,
    poll_seconds: float = 0.5,
) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    deadline = time.time() + wait_seconds
    while True:
        state = cdp.evaluate_json(ws_url, _screenshot_render_state_expression())
        state["timestamp"] = time.time()
        states.append(state)
        if state.get("complete"):
            break
        if time.time() >= deadline:
            break
        time.sleep(min(poll_seconds, max(deadline - time.time(), 0)))
    return states


def _capture_attempt(
    ws_url: str,
    *,
    output_path: Path,
    page_width: int,
    page_height: int,
    capture_scale: float = 1.0,
) -> dict[str, Any]:
    capture = cdp.capture_screenshot(
        ws_url,
        output_path=str(output_path),
        clip={
            "x": 0,
            "y": 0,
            "width": page_width,
            "height": page_height,
            "scale": capture_scale,
        },
        capture_beyond_viewport=True,
        image_format="png",
    )
    normalized = _normalize_image_size(
        output_path, page_width=page_width, page_height=page_height
    )
    metrics = _image_metrics(output_path)
    return {
        "path": str(output_path),
        "capture": capture,
        "normalized_image": normalized,
        "metrics": metrics,
    }


def _validate_strategy_attempt(
    strategy_run: dict[str, Any],
    *,
    page_width: int,
    page_height: int,
) -> None:
    render_state = strategy_run.get("render_state") or {}
    render_progress = strategy_run.get("render_progress") or []
    attempt = strategy_run.get("attempt") or {}
    metrics = attempt.get("metrics") or {}
    strategy_name = str(render_state.get("strategy") or strategy_run.get("strategy") or "")

    if metrics.get("mean_luma", 255.0) >= 250.0 and metrics.get("bright240_fraction", 1.0) >= 0.995:
        raise RuntimeError(f"{strategy_name} produced an almost-blank capture")
    if (
        strategy_name == STRATEGY_FULL
        and metrics.get("mean_luma", 255.0) >= 240.0
        and metrics.get("bright240_fraction", 1.0) >= 0.85
    ):
        raise RuntimeError("synthetic_full_image produced a washed-out capture")
    quartile_luma = [
        float(value)
        for value in (metrics.get("quartile_luma") or [])
        if isinstance(value, (int, float))
    ]
    column_luma = [
        float(value)
        for value in (metrics.get("column_luma") or [])
        if isinstance(value, (int, float))
    ]
    if (
        strategy_name == STRATEGY_FULL
        and len(quartile_luma) == 4
        and float(metrics.get("quartile_spread", 0.0)) >= 60.0
        and max(quartile_luma) >= 235.0
        and min(quartile_luma) <= 190.0
    ):
        raise RuntimeError("synthetic_full_image produced a vertically inconsistent capture")
    if (
        strategy_name == STRATEGY_FULL
        and len(column_luma) == 4
        and float(metrics.get("column_spread", 0.0)) >= 40.0
        and max(column_luma) >= 250.0
        and min(column_luma) <= 230.0
    ):
        raise RuntimeError("synthetic_full_image produced a laterally inconsistent capture")

    if not render_progress:
        return
    last_progress = render_progress[-1]
    if strategy_name == STRATEGY_FULL:
        if not last_progress.get("complete"):
            raise RuntimeError("synthetic_full_image did not report a fully loaded image")
        if int(last_progress.get("naturalWidth") or 0) != page_width:
            raise RuntimeError("synthetic_full_image naturalWidth did not match page width")
        if int(last_progress.get("naturalHeight") or 0) != page_height:
            raise RuntimeError("synthetic_full_image naturalHeight did not match page height")
    if strategy_name == STRATEGY_TILES and not last_progress.get("complete"):
        raise RuntimeError("synthetic_tile_canvas did not complete tile loading")


def _execute_strategy_attempt(
    ws_url: str,
    *,
    strategy_name: str,
    output_stem: str,
    render_expression: str,
    page_width: int,
    page_height: int,
    output_dir: Path,
    render_wait_seconds: float,
    post_render_settle_seconds: float,
    capture_scale: float = 1.0,
) -> dict[str, Any]:
    strategy_run = _run_strategy(
        ws_url,
        strategy_name=output_stem,
        render_expression=render_expression,
        page_width=page_width,
        page_height=page_height,
        output_dir=output_dir,
        render_wait_seconds=render_wait_seconds,
        post_render_settle_seconds=post_render_settle_seconds,
        capture_scale=capture_scale,
    )
    strategy_run["strategy"] = strategy_name
    strategy_run["output_stem"] = output_stem
    validation_started_at = time.time()
    try:
        _validate_strategy_attempt(
            strategy_run,
            page_width=page_width,
            page_height=page_height,
        )
        strategy_run["validation_error"] = ""
    except Exception as exc:
        strategy_run["validation_error"] = str(exc)
    strategy_run.setdefault("timings", {})["validation_seconds"] = (
        time.time() - validation_started_at
    )
    return strategy_run


def _run_strategy_with_retries(
    ws_url: str,
    *,
    strategy_name: str,
    output_prefix: str,
    render_expression_factory,
    page_width: int,
    page_height: int,
    output_dir: Path,
    render_wait_seconds: float,
    post_render_settle_seconds: float,
    max_attempts: int,
    capture_scale: float,
    sleep_between_attempts: float = 0.0,
) -> list[dict[str, Any]]:
    strategy_runs: list[dict[str, Any]] = []
    for attempt_index in range(1, max_attempts + 1):
        strategy_runs.append(
            _execute_strategy_attempt(
                ws_url,
                strategy_name=strategy_name,
                output_stem=f"{output_prefix}_attempt_{attempt_index}",
                render_expression=render_expression_factory(),
                page_width=page_width,
                page_height=page_height,
                output_dir=output_dir,
                render_wait_seconds=render_wait_seconds,
                post_render_settle_seconds=post_render_settle_seconds,
                capture_scale=capture_scale,
            )
        )
        if not strategy_runs[-1].get("validation_error"):
            break
        if attempt_index < max_attempts and sleep_between_attempts > 0:
            time.sleep(sleep_between_attempts)
    return strategy_runs


def _run_strategy(
    ws_url: str,
    *,
    strategy_name: str,
    render_expression: str,
    page_width: int,
    page_height: int,
    output_dir: Path,
    render_wait_seconds: float,
    post_render_settle_seconds: float = POST_RENDER_SETTLE_SECONDS,
    capture_scale: float = 1.0,
) -> dict[str, Any]:
    timings: dict[str, float] = {}

    started_at = time.time()
    render_state = cdp.evaluate_json(ws_url, render_expression)
    timings["render_state_seconds"] = time.time() - started_at

    started_at = time.time()
    render_progress = _wait_for_render_completion(
        ws_url, wait_seconds=render_wait_seconds
    )
    timings["render_seconds"] = time.time() - started_at

    timings["settle_seconds"] = max(post_render_settle_seconds, 0.0)
    if post_render_settle_seconds > 0:
        time.sleep(post_render_settle_seconds)

    started_at = time.time()
    output_path = output_dir / f"{Path(strategy_name).stem}.png"
    attempt = _capture_attempt(
        ws_url,
        output_path=output_path,
        page_width=page_width,
        page_height=page_height,
        capture_scale=capture_scale,
    )
    timings["capture_seconds"] = time.time() - started_at
    return {
        "strategy": strategy_name,
        "render_state": render_state,
        "render_progress": render_progress,
        "attempt": attempt,
        "timings": timings,
    }


def capture_viewer_screenshot(
    settings: Settings,
    *,
    image_page_url: str,
    output_dir: Path,
    page_load_seconds: float,
    render_wait_seconds: float,
    post_render_settle_seconds: float = POST_RENDER_SETTLE_SECONDS,
    strategy: str = STRATEGY_AUTO,
    existing_ws_url: str | None = None,
    existing_target_id: str = "",
) -> dict[str, Any]:
    if strategy not in STRATEGY_CHOICES:
        raise ValueError(f"Unsupported screenshot strategy {strategy!r}")

    started_at = time.time()
    launch_browser(settings)
    if existing_ws_url:
        ws_url = existing_ws_url
        target_id = existing_target_id
    else:
        page = cdp.create_page(settings.chrome_debug_base, image_page_url)
        ws_url = str(page["webSocketDebuggerUrl"])
        target_id = str(page.get("id", "") or "")
    timings: dict[str, float] = {}
    try:
        cdp.call_method(ws_url, "Page.enable")
        cdp.call_method(ws_url, "Runtime.enable")
        navigate_started_at = time.time()
        cdp.navigate(ws_url, image_page_url)
        timings["navigate_seconds"] = time.time() - navigate_started_at

        probe_started_at = time.time()
        probe = _wait_for_page_probe(ws_url, wait_seconds=page_load_seconds)
        timings["probe_seconds"] = time.time() - probe_started_at

        hydrate_started_at = time.time()
        probe = _wait_for_hydrated_probe(
            ws_url,
            initial_probe=probe,
            wait_seconds=render_wait_seconds,
        )
        timings["hydrate_seconds"] = time.time() - hydrate_started_at
        page_width, page_height = _page_size_from_probe(probe)
        output_dir.mkdir(parents=True, exist_ok=True)
        image_id = image_page_url.rstrip("/").split("/")[-1]

        cdp.set_device_metrics(
            ws_url,
            width=page_width + 42,
            height=page_height + 42,
            device_scale_factor=1,
        )
        expand_state = cdp.evaluate_json(
            ws_url, _expand_viewer_expression(page_width, page_height)
        )
        strategy_runs: list[dict[str, Any]] = []
        tile_growth_states: list[dict[str, Any]] = []
        upgrade_state: dict[str, Any] = {}

        if strategy == STRATEGY_FULL:
            strategy_runs.extend(
                _run_strategy_with_retries(
                    ws_url,
                    strategy_name=STRATEGY_FULL,
                    output_prefix=f"{image_id}_{STRATEGY_FULL}",
                    render_expression_factory=lambda: _render_full_image_expression(
                        full_image_url=image.build_full_image_url(probe),
                        page_width=page_width,
                        page_height=page_height,
                    ),
                    page_width=page_width,
                    page_height=page_height,
                    output_dir=output_dir,
                    render_wait_seconds=render_wait_seconds,
                    post_render_settle_seconds=post_render_settle_seconds,
                    max_attempts=FULL_IMAGE_ATTEMPTS,
                    capture_scale=0.5,
                    sleep_between_attempts=FULL_IMAGE_RETRY_SLEEP_SECONDS,
                )
            )
        elif strategy == STRATEGY_TILES:
            probe = _wait_for_tile_probe(
                ws_url,
                initial_probe=probe,
                wait_seconds=min(render_wait_seconds, TILE_PROBE_WAIT_SECONDS),
            )
            strategy_runs.extend(
                _run_strategy_with_retries(
                    ws_url,
                    strategy_name=STRATEGY_TILES,
                    output_prefix=f"{image_id}_{STRATEGY_TILES}",
                    render_expression_factory=lambda: _render_tile_canvas_expression(
                        tiles=_tile_grid_from_probe(probe),
                        page_width=page_width,
                        page_height=page_height,
                    ),
                    page_width=page_width,
                    page_height=page_height,
                    output_dir=output_dir,
                    render_wait_seconds=render_wait_seconds,
                    post_render_settle_seconds=post_render_settle_seconds,
                    max_attempts=1,
                    capture_scale=0.5,
                )
            )
        elif strategy == STRATEGY_VIEWER:
            tile_growth_states = _wait_for_tiles(ws_url, wait_seconds=render_wait_seconds)
            upgrade_state = cdp.evaluate_json(
                ws_url, _upgrade_tiles_expression(page_width, page_height)
            )

            viewer_attempts: list[dict[str, Any]] = []
            for attempt_index in range(1, 4):
                time.sleep(render_wait_seconds)
                candidate_path = output_dir / f"{image_id}_viewer_attempt_{attempt_index}.png"
                attempt = _capture_attempt(
                    ws_url,
                    output_path=candidate_path,
                    page_width=page_width,
                    page_height=page_height,
                )
                viewer_attempts.append(
                    {
                        "attempt_index": attempt_index,
                        **attempt,
                    }
                )
                if attempt["metrics"]["bright240_fraction"] < 0.82:
                    break

            best_viewer_attempt = min(
                viewer_attempts,
                key=lambda item: (
                    item["metrics"]["bright240_fraction"],
                    item["metrics"]["mean_luma"],
                ),
            )
            viewer_run = {
                "strategy": STRATEGY_VIEWER,
                "render_state": {
                    "strategy": STRATEGY_VIEWER,
                    "expand_state": expand_state,
                    "upgrade_state": upgrade_state,
                },
                "render_progress": tile_growth_states,
                "attempt": best_viewer_attempt,
                "all_attempts": viewer_attempts,
                "validation_error": "",
            }
            _validate_strategy_attempt(
                viewer_run,
                page_width=page_width,
                page_height=page_height,
            )
            strategy_runs.append(
                viewer_run
            )
        else:
            strategy_runs.extend(
                _run_strategy_with_retries(
                    ws_url,
                    strategy_name=STRATEGY_FULL,
                    output_prefix=f"{image_id}_{STRATEGY_FULL}",
                    render_expression_factory=lambda: _render_full_image_expression(
                        full_image_url=image.build_full_image_url(probe),
                        page_width=page_width,
                        page_height=page_height,
                    ),
                    page_width=page_width,
                    page_height=page_height,
                    output_dir=output_dir,
                    render_wait_seconds=render_wait_seconds,
                    post_render_settle_seconds=post_render_settle_seconds,
                    max_attempts=FULL_IMAGE_ATTEMPTS,
                    capture_scale=0.5,
                    sleep_between_attempts=FULL_IMAGE_RETRY_SLEEP_SECONDS,
                )
            )

            if not any(not item.get("validation_error") for item in strategy_runs):
                try:
                    probe = _navigate_and_probe(
                        ws_url,
                        image_page_url=image_page_url,
                        page_load_seconds=page_load_seconds,
                        render_wait_seconds=render_wait_seconds,
                    )
                    probe = _wait_for_tile_probe(
                        ws_url,
                        initial_probe=probe,
                        wait_seconds=min(render_wait_seconds, TILE_PROBE_WAIT_SECONDS),
                    )
                    strategy_runs.extend(
                        _run_strategy_with_retries(
                            ws_url,
                            strategy_name=STRATEGY_TILES,
                            output_prefix=f"{image_id}_{STRATEGY_TILES}",
                            render_expression_factory=lambda: _render_tile_canvas_expression(
                                tiles=_tile_grid_from_probe(probe),
                                page_width=page_width,
                                page_height=page_height,
                            ),
                            page_width=page_width,
                            page_height=page_height,
                            output_dir=output_dir,
                            render_wait_seconds=render_wait_seconds,
                            post_render_settle_seconds=post_render_settle_seconds,
                            max_attempts=1,
                            capture_scale=0.5,
                        )
                    )
                except Exception as exc:
                    strategy_runs.append(
                        {
                            "strategy": STRATEGY_TILES,
                            "output_stem": f"{image_id}_{STRATEGY_TILES}",
                            "validation_error": str(exc),
                        }
                    )

            if not any(not item.get("validation_error") for item in strategy_runs):
                probe = _navigate_and_probe(
                    ws_url,
                    image_page_url=image_page_url,
                    page_load_seconds=page_load_seconds,
                    render_wait_seconds=render_wait_seconds,
                )
                tile_growth_states = _wait_for_tiles(ws_url, wait_seconds=render_wait_seconds)
                upgrade_state = cdp.evaluate_json(
                    ws_url, _upgrade_tiles_expression(page_width, page_height)
                )

                viewer_attempts: list[dict[str, Any]] = []
                for attempt_index in range(1, 4):
                    time.sleep(render_wait_seconds)
                    candidate_path = output_dir / f"{image_id}_viewer_attempt_{attempt_index}.png"
                    attempt = _capture_attempt(
                        ws_url,
                        output_path=candidate_path,
                        page_width=page_width,
                        page_height=page_height,
                    )
                    viewer_attempts.append(
                        {
                            "attempt_index": attempt_index,
                            **attempt,
                        }
                    )
                    if attempt["metrics"]["bright240_fraction"] < 0.82:
                        break

                best_viewer_attempt = min(
                    viewer_attempts,
                    key=lambda item: (
                        item["metrics"]["bright240_fraction"],
                        item["metrics"]["mean_luma"],
                    ),
                )
                viewer_run = {
                    "strategy": STRATEGY_VIEWER,
                    "render_state": {
                        "strategy": STRATEGY_VIEWER,
                        "expand_state": expand_state,
                        "upgrade_state": upgrade_state,
                    },
                    "render_progress": tile_growth_states,
                    "attempt": best_viewer_attempt,
                    "all_attempts": viewer_attempts,
                    "validation_error": "",
                }
                _validate_strategy_attempt(
                    viewer_run,
                    page_width=page_width,
                    page_height=page_height,
                )
                strategy_runs.append(viewer_run)

        best_strategy = None
        for candidate in strategy_runs:
            if not candidate.get("validation_error"):
                best_strategy = candidate
                break
        if best_strategy is None:
            raise RuntimeError(
                "; ".join(
                    str(item.get("validation_error") or item.get("strategy"))
                    for item in strategy_runs
                )
            )
        output_path = output_dir / f"{image_id}_viewer.png"
        if Path(best_strategy["attempt"]["path"]) != output_path:
            shutil.copyfile(best_strategy["attempt"]["path"], output_path)
    finally:
        try:
            cdp.clear_device_metrics(ws_url)
        except Exception:
            pass
        if target_id and not existing_ws_url:
            try:
                cdp.close_page(settings.chrome_debug_base, target_id)
            except Exception:
                pass

    summary = {
        "image_page_url": image_page_url,
        "output_path": str(output_path),
        "page_width": page_width,
        "page_height": page_height,
        "probe": probe,
        "expand_state": expand_state,
        "tile_growth_states": tile_growth_states,
        "upgrade_state": upgrade_state,
        "strategy_runs": strategy_runs,
        "selected_strategy": best_strategy["strategy"],
        "selected_output_stem": best_strategy.get("output_stem", ""),
        "timings": timings,
        "elapsed_seconds": time.time() - started_at,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _load_completed_keys(results_csv: Path) -> set[tuple[str, str]]:
    if not results_csv.exists():
        return set()
    with results_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        completed: set[tuple[str, str]] = set()
        for row in reader:
            if row.get("status") == "captured":
                completed.add((row["issue_id"], row["page_num"]))
        return completed


def _append_result(results_csv: Path, row: dict[str, str]) -> None:
    write_header = not results_csv.exists()
    with results_csv.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def _append_production_result(results_csv: Path, row: dict[str, str]) -> None:
    write_header = not results_csv.exists()
    with results_csv.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PRODUCTION_RESULT_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.write_text(json.dumps(summary, indent=2, sort_keys=True))


def _read_result_rows(results_csv: Path) -> list[dict[str, str]]:
    if not results_csv.exists():
        return []
    with results_csv.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _manifest_row_to_dict(row: ManifestRow) -> dict[str, str]:
    return {
        "issue_id": row.issue_id,
        "issue_date": row.issue_date,
        "page_num": row.page_num,
        "preferred_image_id": row.preferred_image_id,
        "preferred_image_page_url": row.preferred_image_page_url,
    }


def _write_manifest_rows(rows: list[ManifestRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(_manifest_row_to_dict(row))


def _build_failed_manifest_rows(
    manifest_rows: list[ManifestRow],
    result_rows: list[dict[str, str]],
) -> list[ManifestRow]:
    latest_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for row in result_rows:
        latest_by_key[(row["issue_id"], row["page_num"])] = row

    remaining: list[ManifestRow] = []
    for manifest_row in manifest_rows:
        result = latest_by_key.get((manifest_row.issue_id, manifest_row.page_num))
        if result and result.get("status") == "captured":
            continue
        remaining.append(manifest_row)
    return remaining


def _merge_production_rows(
    manifest_rows: list[ManifestRow],
    pass_rows: list[tuple[int, str, list[dict[str, str]]]],
) -> list[dict[str, str]]:
    history_by_key: dict[tuple[str, str], list[dict[str, str]]] = {}
    for pass_index, pass_name, rows in pass_rows:
        for row in rows:
            enriched = dict(row)
            enriched["pass_index"] = str(pass_index)
            enriched["pass_name"] = pass_name
            history_by_key.setdefault((row["issue_id"], row["page_num"]), []).append(enriched)

    merged: list[dict[str, str]] = []
    for manifest_row in manifest_rows:
        key = (manifest_row.issue_id, manifest_row.page_num)
        history = history_by_key.get(key, [])
        selected = None
        for row in reversed(history):
            if row.get("status") == "captured":
                selected = row
                break
        if selected is None and history:
            selected = history[-1]
        if selected is None:
            selected = {
                "run_index": "",
                "issue_id": manifest_row.issue_id,
                "issue_date": manifest_row.issue_date,
                "page_num": manifest_row.page_num,
                "preferred_image_id": manifest_row.preferred_image_id,
                "preferred_image_page_url": manifest_row.preferred_image_page_url,
                "status": "failed",
                "output_path": "",
                "selected_strategy": "",
                "mean_luma": "",
                "bright240_fraction": "",
                "natural_width": "",
                "natural_height": "",
                "elapsed_seconds": "",
                "error_type": "not_attempted",
                "error_message": "No production pass result was recorded for this page",
                "pass_index": "",
                "pass_name": "",
            }
        merged.append(selected)
    return merged


def _page_output_dir(base_dir: Path, issue_id: str, page_num: str, image_id: str) -> Path:
    return base_dir / issue_id / f"{page_num.zfill(4)}__{image_id}"


def _is_retryable_page_capture_error(exc: Exception) -> bool:
    message = str(exc)
    return any(marker in message for marker in RETRYABLE_PAGE_CAPTURE_MARKERS) or any(
        marker in message for marker in RETRYABLE_VALIDATION_MARKERS
    )


def _open_reusable_browser_page(
    settings: Settings,
    *,
    force_new_instance: bool,
) -> tuple[str, str]:
    launch_browser(settings, force_new_instance=force_new_instance)
    page = cdp.create_page(settings.chrome_debug_base, settings.home_url)
    return str(page["webSocketDebuggerUrl"]), str(page.get("id", "") or "")


def capture_pages_from_manifest(
    settings: Settings,
    *,
    manifest_csv: Path,
    output_dir: Path,
    page_load_seconds: float,
    render_wait_seconds: float,
    sleep_between_pages: float,
    sleep_jitter_seconds: float,
    adaptive_sleep: bool = False,
    min_sleep_between_pages: float = 0.0,
    max_sleep_between_pages: float | None = None,
    sleep_step_seconds: float = 0.25,
    clean_streak_threshold: int = 3,
    slow_page_threshold_seconds: float = 12.0,
    post_render_settle_seconds: float = POST_RENDER_SETTLE_SECONDS,
    recycle_browser_every_pages: int = 0,
    limit: int | None = None,
    start_offset: int = 0,
    strategy: str = STRATEGY_AUTO,
    continue_on_error: bool = True,
    reusable_ws_url: str | None = None,
    reusable_target_id: str = "",
) -> dict[str, Any]:
    manifest_rows = read_manifest(manifest_csv)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_csv = output_dir / "results.csv"
    summary_json = output_dir / "summary.json"

    completed = _load_completed_keys(results_csv)
    remaining = [
        row for row in manifest_rows if (row.issue_id, row.page_num) not in completed
    ]
    if start_offset:
        remaining = remaining[start_offset:]
    if limit is not None:
        remaining = remaining[:limit]

    summary: dict[str, Any] = {
        "manifest_csv": str(manifest_csv),
        "output_dir": str(output_dir),
        "total_manifest_rows": len(manifest_rows),
        "already_captured_rows": len(completed),
        "run_candidate_rows": len(remaining),
        "captured_this_run": 0,
        "failed_this_run": 0,
        "stopped_reason": "",
        "last_issue_id": "",
        "last_page_num": "",
        "page_load_seconds": page_load_seconds,
        "render_wait_seconds": render_wait_seconds,
        "sleep_between_pages": sleep_between_pages,
        "sleep_jitter_seconds": sleep_jitter_seconds,
        "adaptive_sleep": adaptive_sleep,
        "current_sleep_between_pages": sleep_between_pages,
        "min_sleep_between_pages": min_sleep_between_pages,
        "max_sleep_between_pages": max(
            sleep_between_pages if max_sleep_between_pages is None else max_sleep_between_pages,
            min_sleep_between_pages,
        ),
        "sleep_step_seconds": sleep_step_seconds,
        "clean_streak_threshold": clean_streak_threshold,
        "slow_page_threshold_seconds": slow_page_threshold_seconds,
        "post_render_settle_seconds": post_render_settle_seconds,
        "recycle_browser_every_pages": recycle_browser_every_pages,
        "browser_recycles": 0,
        "strategy": strategy,
        "continue_on_error": continue_on_error,
    }
    _write_summary(summary_json, summary)
    effective_max_sleep_between_pages = (
        sleep_between_pages
        if max_sleep_between_pages is None
        else max(max_sleep_between_pages, min_sleep_between_pages)
    )
    current_reusable_ws_url = reusable_ws_url
    current_reusable_target_id = reusable_target_id
    consecutive_retryable_failures = 0
    captured_since_recycle = 0
    adaptive_sleep_controller = AdaptiveSleepController(
        enabled=adaptive_sleep,
        current_sleep_seconds=sleep_between_pages,
        min_sleep_seconds=min_sleep_between_pages,
        max_sleep_seconds=effective_max_sleep_between_pages,
        step_seconds=sleep_step_seconds,
        clean_streak_threshold=max(clean_streak_threshold, 1),
        slow_page_threshold_seconds=slow_page_threshold_seconds,
    )

    for run_index, row in enumerate(remaining, start=1):
        per_page_dir = _page_output_dir(
            output_dir, row.issue_id, row.page_num, row.preferred_image_id
        )
        try:
            last_exc: Exception | None = None
            page_summary = None
            page_attempts = 0
            for page_attempt in range(1, PAGE_CAPTURE_ATTEMPTS + 1):
                page_attempts = page_attempt
                try:
                    attempt_page_load_seconds = page_load_seconds + (
                        0.75 * (page_attempt - 1)
                    )
                    attempt_render_wait_seconds = render_wait_seconds + (
                        2.0 * (page_attempt - 1)
                    )
                    page_summary = capture_viewer_screenshot(
                        settings,
                        image_page_url=row.preferred_image_page_url,
                        output_dir=per_page_dir,
                        page_load_seconds=attempt_page_load_seconds,
                        render_wait_seconds=attempt_render_wait_seconds,
                        post_render_settle_seconds=post_render_settle_seconds,
                        strategy=strategy,
                        existing_ws_url=current_reusable_ws_url,
                        existing_target_id=current_reusable_target_id,
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    if (
                        page_attempt < PAGE_CAPTURE_ATTEMPTS
                        and _is_retryable_page_capture_error(exc)
                    ):
                        try:
                            current_reusable_ws_url, current_reusable_target_id = (
                                _open_reusable_browser_page(
                                    settings,
                                    force_new_instance=True,
                                )
                            )
                        except Exception:
                            current_reusable_ws_url = None
                            current_reusable_target_id = ""
                        time.sleep(1.5)
                        continue
                    raise
            if page_summary is None and last_exc is not None:
                raise last_exc
            consecutive_retryable_failures = 0
            selected_strategy_name = ""
            natural_width = ""
            natural_height = ""
            selected_run = None
            selected_output_stem = str(page_summary.get("selected_output_stem", "") or "")
            for run in page_summary.get("strategy_runs", []):
                if selected_output_stem and run.get("output_stem") == selected_output_stem:
                    selected_run = run
                    break
            if selected_run is None:
                for run in page_summary.get("strategy_runs", []):
                    if run.get("strategy") == page_summary.get("selected_strategy"):
                        selected_run = run
                        break
            if selected_run is not None:
                selected_strategy_name = str(
                    selected_run.get("render_state", {}).get(
                        "strategy", selected_run.get("strategy", "")
                    )
                )
                progress = selected_run.get("render_progress") or []
                if progress:
                    last_progress = progress[-1]
                    natural_width = str(last_progress.get("naturalWidth", "") or "")
                    natural_height = str(last_progress.get("naturalHeight", "") or "")
                metrics = selected_run.get("attempt", {}).get("metrics", {})
            else:
                metrics = {}
                progress = []

            probe_timings = page_summary.get("timings", {}) if page_summary else {}
            strategy_timings = selected_run.get("timings", {}) if selected_run else {}
            current_sleep_seconds = adaptive_sleep_controller.record_page_result(
                elapsed_seconds=float(page_summary.get("elapsed_seconds", 0.0) or 0.0),
                page_attempts=page_attempts,
                had_retryable_error=page_attempts > 1,
            )
            sleep_seconds = current_sleep_seconds + (
                random.uniform(0.0, sleep_jitter_seconds)
                if sleep_jitter_seconds > 0
                else 0.0
            )
            browser_recycled_after_page = "false"

            result = {
                "run_index": str(run_index),
                "issue_id": row.issue_id,
                "issue_date": row.issue_date,
                "page_num": row.page_num,
                "preferred_image_id": row.preferred_image_id,
                "preferred_image_page_url": row.preferred_image_page_url,
                "status": "captured",
                "output_path": str(page_summary["output_path"]),
                "selected_strategy": selected_strategy_name,
                "mean_luma": str(metrics.get("mean_luma", "")),
                "bright240_fraction": str(metrics.get("bright240_fraction", "")),
                "natural_width": natural_width,
                "natural_height": natural_height,
                "elapsed_seconds": str(page_summary.get("elapsed_seconds", "")),
                "page_attempts": str(page_attempts),
                "applied_sleep_seconds": str(round(sleep_seconds, 3)),
                "probe_seconds": str(round(float(probe_timings.get("probe_seconds", 0.0) or 0.0), 3)),
                "hydrate_seconds": str(round(float(probe_timings.get("hydrate_seconds", 0.0) or 0.0), 3)),
                "render_seconds": str(round(float(strategy_timings.get("render_seconds", 0.0) or 0.0), 3)),
                "settle_seconds": str(round(float(strategy_timings.get("settle_seconds", 0.0) or 0.0), 3)),
                "capture_seconds": str(round(float(strategy_timings.get("capture_seconds", 0.0) or 0.0), 3)),
                "validation_seconds": str(round(float(strategy_timings.get("validation_seconds", 0.0) or 0.0), 3)),
                "browser_recycled_after_page": browser_recycled_after_page,
                "error_type": "",
                "error_message": "",
            }
            summary["captured_this_run"] += 1
            # Clear any stale non-blocking stop marker from an earlier page failure.
            summary["stopped_reason"] = ""
            summary["current_sleep_between_pages"] = current_sleep_seconds
            summary["adaptive_sleep_reason"] = adaptive_sleep_controller.last_adjustment_reason
            summary["last_issue_id"] = row.issue_id
            summary["last_page_num"] = row.page_num
            captured_since_recycle += 1
            if (
                recycle_browser_every_pages > 0
                and captured_since_recycle >= recycle_browser_every_pages
            ):
                try:
                    current_reusable_ws_url, current_reusable_target_id = (
                        _open_reusable_browser_page(
                            settings,
                            force_new_instance=True,
                        )
                    )
                    summary["browser_recycles"] += 1
                    captured_since_recycle = 0
                    browser_recycled_after_page = "true"
                    result["browser_recycled_after_page"] = browser_recycled_after_page
                except Exception as exc:
                    summary["last_browser_recycle_error"] = str(exc)
                    current_reusable_ws_url = None
                    current_reusable_target_id = ""
                    captured_since_recycle = 0
            _append_result(results_csv, result)
            _write_summary(summary_json, summary)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except Exception as exc:
            err_type, err_msg = error_type_and_message(exc)
            blocking_stop_reason = _classify_blocking_stop_reason(exc)
            if blocking_stop_reason:
                err_type = blocking_stop_reason
            result = {
                "run_index": str(run_index),
                "issue_id": row.issue_id,
                "issue_date": row.issue_date,
                "page_num": row.page_num,
                "preferred_image_id": row.preferred_image_id,
                "preferred_image_page_url": row.preferred_image_page_url,
                "status": "failed",
                "output_path": "",
                "selected_strategy": "",
                "mean_luma": "",
                "bright240_fraction": "",
                "natural_width": "",
                "natural_height": "",
                "elapsed_seconds": "",
                "page_attempts": str(page_attempts if "page_attempts" in locals() else ""),
                "applied_sleep_seconds": "",
                "probe_seconds": "",
                "hydrate_seconds": "",
                "render_seconds": "",
                "settle_seconds": "",
                "capture_seconds": "",
                "validation_seconds": "",
                "browser_recycled_after_page": "false",
                "error_type": err_type,
                "error_message": err_msg,
            }
            _append_result(results_csv, result)
            summary["failed_this_run"] += 1
            summary["stopped_reason"] = blocking_stop_reason or err_type
            summary["current_sleep_between_pages"] = adaptive_sleep_controller.current_sleep_seconds
            summary["last_issue_id"] = row.issue_id
            summary["last_page_num"] = row.page_num
            _write_summary(summary_json, summary)
            if blocking_stop_reason:
                raise ScreenshotRunStopped(blocking_stop_reason, err_msg) from exc
            if _is_retryable_page_capture_error(exc):
                consecutive_retryable_failures += 1
                try:
                    current_reusable_ws_url, current_reusable_target_id = (
                        _open_reusable_browser_page(
                            settings,
                            force_new_instance=True,
                        )
                    )
                except Exception:
                    current_reusable_ws_url = None
                    current_reusable_target_id = ""
                if consecutive_retryable_failures >= MAX_CONSECUTIVE_RETRYABLE_FAILURES:
                    summary["stopped_reason"] = STOP_REASON_BROWSER_UNHEALTHY
                    _write_summary(summary_json, summary)
                    raise ScreenshotRunStopped(
                        STOP_REASON_BROWSER_UNHEALTHY,
                        f"Repeated retryable browser/CDP failures: {err_msg}",
                    ) from exc
            else:
                consecutive_retryable_failures = 0
            if not continue_on_error:
                raise

    summary["stopped_reason"] = "completed_run"
    _write_summary(summary_json, summary)
    return summary


def capture_pages_production(
    settings: Settings,
    *,
    manifest_csv: Path,
    output_dir: Path,
    page_load_seconds: float,
    render_wait_seconds: float,
    sleep_between_pages: float,
    sleep_jitter_seconds: float,
    adaptive_sleep: bool = False,
    min_sleep_between_pages: float = 0.0,
    max_sleep_between_pages: float | None = None,
    sleep_step_seconds: float = 0.25,
    clean_streak_threshold: int = 3,
    slow_page_threshold_seconds: float = 12.0,
    post_render_settle_seconds: float = POST_RENDER_SETTLE_SECONDS,
    recycle_browser_every_pages: int = 0,
    limit: int | None,
    start_offset: int,
    strategy: str = STRATEGY_TILES,
    max_passes: int = 3,
    pass_page_load_increment: float = 0.75,
    pass_render_wait_increment: float = 2.0,
    stop_on_stall: bool = True,
    restart_browser_before_run: bool = True,
    restart_browser_each_pass: bool = True,
) -> dict[str, Any]:
    if strategy == STRATEGY_AUTO:
        raise ValueError(
            "Production screenshot runs should use an explicit strategy; "
            "prefer synthetic_full_image"
        )
    if max_passes < 1:
        raise ValueError("max_passes must be at least 1")

    manifest_rows = read_manifest(manifest_csv)
    if start_offset:
        manifest_rows = manifest_rows[start_offset:]
    if limit is not None:
        manifest_rows = manifest_rows[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    run_manifest_csv = output_dir / "run_manifest.csv"
    manifests_dir = output_dir / "manifests"
    passes_dir = output_dir / "passes"
    final_results_csv = output_dir / "final_results.csv"
    remaining_failures_csv = output_dir / "remaining_failures_manifest.csv"
    production_summary_json = output_dir / "summary.json"
    _write_manifest_rows(manifest_rows, run_manifest_csv)

    pass_summaries: list[dict[str, Any]] = []
    pass_rows: list[tuple[int, str, list[dict[str, str]]]] = []
    remaining_rows = manifest_rows
    stop_reason = "completed_run"
    previous_remaining_count: int | None = None
    effective_max_sleep_between_pages = (
        sleep_between_pages
        if max_sleep_between_pages is None
        else max(max_sleep_between_pages, min_sleep_between_pages)
    )

    launch_browser(
        settings,
        force_new_instance=restart_browser_before_run,
    )

    for pass_index in range(1, max_passes + 1):
        if not remaining_rows:
            break

        if pass_index > 1:
            launch_browser(
                settings,
                force_new_instance=restart_browser_each_pass,
            )

        pass_name = f"pass_{pass_index:02d}"
        pass_manifest_csv = manifests_dir / f"{pass_name}_input.csv"
        pass_output_dir = passes_dir / pass_name
        _write_manifest_rows(remaining_rows, pass_manifest_csv)
        reusable_page = cdp.create_page(settings.chrome_debug_base, settings.home_url)
        reusable_ws_url = str(reusable_page["webSocketDebuggerUrl"])
        reusable_target_id = str(reusable_page.get("id", "") or "")

        attempt_page_load_seconds = page_load_seconds + (
            pass_page_load_increment * (pass_index - 1)
        )
        attempt_render_wait_seconds = render_wait_seconds + (
            pass_render_wait_increment * (pass_index - 1)
        )
        started_at = time.time()
        blocking_stop_reason = ""
        blocking_stop_message = ""
        try:
            pass_summary = capture_pages_from_manifest(
                settings,
                manifest_csv=pass_manifest_csv,
                output_dir=pass_output_dir,
                page_load_seconds=attempt_page_load_seconds,
                render_wait_seconds=attempt_render_wait_seconds,
                sleep_between_pages=sleep_between_pages,
                sleep_jitter_seconds=sleep_jitter_seconds,
                adaptive_sleep=adaptive_sleep,
                min_sleep_between_pages=min_sleep_between_pages,
                max_sleep_between_pages=effective_max_sleep_between_pages,
                sleep_step_seconds=sleep_step_seconds,
                clean_streak_threshold=clean_streak_threshold,
                slow_page_threshold_seconds=slow_page_threshold_seconds,
                post_render_settle_seconds=post_render_settle_seconds,
                recycle_browser_every_pages=recycle_browser_every_pages,
                limit=None,
                start_offset=0,
                strategy=strategy,
                continue_on_error=True,
                reusable_ws_url=reusable_ws_url,
                reusable_target_id=reusable_target_id,
            )
        except ScreenshotRunStopped as exc:
            blocking_stop_reason = exc.stop_reason
            blocking_stop_message = str(exc)
            pass_summary_path = pass_output_dir / "summary.json"
            if pass_summary_path.exists():
                pass_summary = json.loads(pass_summary_path.read_text())
            else:
                pass_summary = {}
        finally:
            if reusable_target_id:
                try:
                    cdp.close_page(settings.chrome_debug_base, reusable_target_id)
                except Exception:
                    pass
        elapsed_seconds = time.time() - started_at
        result_rows = _read_result_rows(pass_output_dir / "results.csv")
        pass_rows.append((pass_index, pass_name, result_rows))

        next_remaining_rows = _build_failed_manifest_rows(remaining_rows, result_rows)
        failed_manifest_csv = manifests_dir / f"{pass_name}_failed.csv"
        _write_manifest_rows(next_remaining_rows, failed_manifest_csv)

        captured_count = sum(row.get("status") == "captured" for row in result_rows)
        failed_count = len(next_remaining_rows)
        pass_summary_record = {
            "pass_index": pass_index,
            "pass_name": pass_name,
            "input_manifest_csv": str(pass_manifest_csv),
            "output_dir": str(pass_output_dir),
            "results_csv": str(pass_output_dir / "results.csv"),
            "failed_manifest_csv": str(failed_manifest_csv),
            "input_rows": len(remaining_rows),
            "captured_rows": captured_count,
            "remaining_failed_rows": failed_count,
            "page_load_seconds": attempt_page_load_seconds,
            "render_wait_seconds": attempt_render_wait_seconds,
            "elapsed_seconds": elapsed_seconds,
            "runner_summary": pass_summary,
            "blocking_stop_reason": blocking_stop_reason,
            "blocking_stop_message": blocking_stop_message,
        }
        pass_summaries.append(pass_summary_record)

        if blocking_stop_reason:
            remaining_rows = next_remaining_rows
            stop_reason = blocking_stop_reason
            break

        if not next_remaining_rows:
            remaining_rows = next_remaining_rows
            break
        if (
            stop_on_stall
            and previous_remaining_count is not None
            and len(next_remaining_rows) >= previous_remaining_count
        ):
            remaining_rows = next_remaining_rows
            stop_reason = "stalled_remaining_failures"
            break
        previous_remaining_count = len(next_remaining_rows)
        remaining_rows = next_remaining_rows
    else:
        if remaining_rows:
            stop_reason = "exhausted_max_passes"

    merged_rows = _merge_production_rows(manifest_rows, pass_rows)
    with final_results_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PRODUCTION_RESULT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(merged_rows)
    _write_manifest_rows(remaining_rows, remaining_failures_csv)

    captured_total = sum(row.get("status") == "captured" for row in merged_rows)
    failed_total = len(remaining_rows)
    pages_per_minute = None
    successful_elapsed = [
        float(row["elapsed_seconds"])
        for row in merged_rows
        if row.get("status") == "captured" and row.get("elapsed_seconds")
    ]
    if successful_elapsed:
        mean_elapsed = sum(successful_elapsed) / len(successful_elapsed)
        pages_per_minute = 60.0 / mean_elapsed if mean_elapsed > 0 else None

    summary = {
        "manifest_csv": str(manifest_csv),
        "run_manifest_csv": str(run_manifest_csv),
        "output_dir": str(output_dir),
        "strategy": strategy,
        "max_passes": max_passes,
        "stop_on_stall": stop_on_stall,
        "restart_browser_before_run": restart_browser_before_run,
        "restart_browser_each_pass": restart_browser_each_pass,
        "sleep_between_pages": sleep_between_pages,
        "sleep_jitter_seconds": sleep_jitter_seconds,
        "adaptive_sleep": adaptive_sleep,
        "min_sleep_between_pages": min_sleep_between_pages,
        "max_sleep_between_pages": effective_max_sleep_between_pages,
        "sleep_step_seconds": sleep_step_seconds,
        "clean_streak_threshold": clean_streak_threshold,
        "slow_page_threshold_seconds": slow_page_threshold_seconds,
        "post_render_settle_seconds": post_render_settle_seconds,
        "recycle_browser_every_pages": recycle_browser_every_pages,
        "initial_page_load_seconds": page_load_seconds,
        "initial_render_wait_seconds": render_wait_seconds,
        "pass_page_load_increment": pass_page_load_increment,
        "pass_render_wait_increment": pass_render_wait_increment,
        "subset_rows": len(manifest_rows),
        "captured_rows": captured_total,
        "failed_rows": failed_total,
        "final_results_csv": str(final_results_csv),
        "remaining_failures_manifest_csv": str(remaining_failures_csv),
        "pass_summaries": pass_summaries,
        "stopped_reason": stop_reason,
        "pages_per_minute_estimate": pages_per_minute,
    }
    _write_summary(production_summary_json, summary)
    return summary
