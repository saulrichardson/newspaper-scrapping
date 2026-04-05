"""Signed image extraction and download helpers."""

from __future__ import annotations

import re
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from newspaper_scrapper.adapters.chrome import applescript, cdp
from newspaper_scrapper.domain.models import USER_AGENT


_PAGE_BLOCK_RE = re.compile(
    r'"page":\{"image":\{(?P<image_block>.*?)\},"articles":',
    re.DOTALL,
)
_IAT_RE = re.compile(r'"iat":"(?P<iat>[^"]+)"')
_DOWNLOAD_FCF_RE = re.compile(
    r'"Download":\{[^}]*"fcfToken":"(?P<fcf>[^"]+)"',
    re.DOTALL,
)


def _pick(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1) if match else ""


def _pick_bool(pattern: str, text: str) -> bool | None:
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    return match.group(1) == "true"


def extract_page_metadata_from_script_texts(
    script_texts: list[str],
    *,
    user: str = "",
) -> dict[str, Any]:
    """Extract authoritative page metadata embedded in the inline app payload."""

    raw = "\n".join(script_texts)
    normalized = raw.replace(r"\\\"", '"').replace(r"\"", '"')
    page_block_match = _PAGE_BLOCK_RE.search(normalized)
    image_block = page_block_match.group("image_block") if page_block_match else ""

    metadata = {
        "imageId": _pick(r'"imageId":(\d+)', image_block) or _pick(r'"imageId":(\d+)', normalized),
        "width": _pick(r'"width":(\d+)', image_block) or _pick(r'"width":(\d+)', normalized),
        "height": _pick(r'"height":(\d+)', image_block) or _pick(r'"height":(\d+)', normalized),
        "publicationId": _pick(r'"publicationId":(\d+)', image_block),
        "canView": _pick_bool(r'"canView":(true|false)', image_block),
        "reasonCanView": _pick(r'"reasonCanView":"([^"]+)"', image_block),
        "iat": "",
        "downloadFcfToken": "",
        "user": user,
    }

    iat_match = _IAT_RE.search(normalized)
    if iat_match:
        metadata["iat"] = iat_match.group("iat")

    fcf_match = _DOWNLOAD_FCF_RE.search(normalized)
    if fcf_match:
        metadata["downloadFcfToken"] = fcf_match.group("fcf")

    return metadata


def page_probe_expression() -> str:
    return r"""JSON.stringify((() => {
  const toHref = (el) => el.getAttribute('href') || el.getAttributeNS('http://www.w3.org/1999/xlink', 'href') || '';
  const images = Array.from(document.querySelectorAll('image')).map((img) => ({
    href: toHref(img),
    width: img.getAttribute('width') || '',
    height: img.getAttribute('height') || '',
    x: img.getAttribute('x') || '',
    y: img.getAttribute('y') || '',
  }));
  const tile = images.find((img) => img.href.includes('https://img.newspapers.com/img/img?')) || null;
  const thumbnail = images.find((img) => img.href.includes('/img/thumbnail/')) || null;
  const signedImageCount = images.filter((img) => img.href.includes('https://img.newspapers.com/img/img?')).length;
  const scripts = Array.from(document.scripts)
    .map((script) => script.textContent || '')
    .filter((text) =>
      text.includes('imageId') ||
      text.includes('fcfToken') ||
      text.includes('reasonCanView') ||
      text.includes('canView') ||
      text.includes('"iat"')
    );
  return {
    url: location.href,
    title: document.title,
    bodySnippet: (document.body?.innerText || '').slice(0, 1200),
    tile,
    thumbnail,
    imageElementCount: images.length,
    signedImageCount,
    scriptTexts: scripts,
    user: window.ncom?.user ? String(window.ncom.user) : '',
  };
})())"""


def evaluate_live_probe(
    *,
    chrome_debug_base: str,
    chrome_app_name: str,
    target_url: str,
) -> dict[str, Any]:
    try:
        ws_url = cdp.find_page_ws_url(chrome_debug_base, target_url)
        probe = cdp.evaluate_json(ws_url, page_probe_expression())
    except Exception:
        raw = applescript.execute_front_tab_javascript(
            chrome_app_name, page_probe_expression()
        )
        probe = __import__("json").loads(raw)
    probe = dict(probe)
    probe["pageMeta"] = extract_page_metadata_from_script_texts(
        probe.get("scriptTexts") or [],
        user=str(probe.get("user", "")).strip(),
    )
    return probe


def build_full_image_url(probe: dict[str, Any]) -> str:
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
        full_query = {
            "id": meta_image_id,
            "user": meta_user,
            "iat": meta_iat,
            "brightness": "0",
            "contrast": "0",
            "invert": "0",
            "width": meta_width,
            "height": meta_height,
        }
        return f"https://img.newspapers.com/img/img?{urlencode(full_query)}"

    tile = probe.get("tile") or {}
    thumbnail = probe.get("thumbnail") or {}
    tile_href = str(tile.get("href", "")).strip()
    if not tile_href:
        raise RuntimeError("No signed tile href found in the live page DOM")
    full_width = str(thumbnail.get("width", "")).strip()
    full_height = str(thumbnail.get("height", "")).strip()
    if not full_width.isdigit() or not full_height.isdigit():
        raise RuntimeError(
            "Could not extract full page width/height from the page thumbnail element"
        )

    parsed = urlparse(tile_href)
    query = parse_qs(parsed.query)
    required = ("id", "user", "iat")
    missing = [key for key in required if not query.get(key)]
    if missing:
        raise RuntimeError(
            f"Signed tile URL is missing required params {missing}: {tile_href}"
        )

    full_query = {
        "id": query["id"][0],
        "user": query["user"][0],
        "iat": query["iat"][0],
        "brightness": query.get("brightness", ["0"])[0],
        "contrast": query.get("contrast", ["0"])[0],
        "invert": query.get("invert", ["0"])[0],
        "width": full_width,
        "height": full_height,
    }
    return f"https://img.newspapers.com/img/img?{urlencode(full_query)}"


def download_binary(url: str, output_path: Path) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    context = ssl.create_default_context()
    with urlopen(request, context=context, timeout=60) as response:
        data = response.read()
        content_type = response.headers.get("Content-Type", "")
        output_path.write_bytes(data)
        return {
            "status": response.status,
            "content_type": content_type,
            "byte_count": len(data),
        }
