"""Chrome DevTools Protocol helpers for a live Chrome session."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

import websockets


CDP_CONNECT_TIMEOUT_SECONDS = 10.0
CDP_RESPONSE_TIMEOUT_SECONDS = 30.0
CDP_SCREENSHOT_TIMEOUT_SECONDS = 120.0


def chrome_json(debug_base: str, path: str) -> Any:
    with urlopen(f"{debug_base.rstrip('/')}{path}", timeout=30) as response:
        return json.load(response)


def list_page_tabs(debug_base: str) -> list[dict[str, Any]]:
    return [
        page
        for page in chrome_json(debug_base, "/json/list")
        if page.get("type") == "page"
    ]


def find_page_ws_url(debug_base: str, target_url: str) -> str:
    pages = list_page_tabs(debug_base)
    exact_match = None
    for page in pages:
        if page.get("url") == target_url:
            exact_match = page
            break
    if exact_match is None:
        partial_matches = [
            page for page in pages if target_url in str(page.get("url", ""))
        ]
        if not partial_matches:
            raise RuntimeError(f"Could not find an open Chrome tab for {target_url}")
        exact_match = partial_matches[0]
    ws_url = exact_match.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError(f"No webSocketDebuggerUrl for {target_url}")
    return ws_url


def create_page(debug_base: str, target_url: str) -> dict[str, Any]:
    encoded_url = quote(target_url, safe="")
    request = Request(
        f"{debug_base.rstrip('/')}/json/new?{encoded_url}",
        data=b"",
        method="PUT",
    )
    with urlopen(request, timeout=30) as response:
        page = json.load(response)
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError(f"No webSocketDebuggerUrl returned for fresh tab {target_url}")
    return page


def create_page_tab(debug_base: str, target_url: str) -> str:
    page = create_page(debug_base, target_url)
    return str(page["webSocketDebuggerUrl"])


def close_page(debug_base: str, target_id: str) -> None:
    encoded_id = quote(target_id, safe="")
    request = Request(
        f"{debug_base.rstrip('/')}/json/close/{encoded_id}",
        method="GET",
    )
    with urlopen(request, timeout=30):
        pass


async def _cdp_evaluate_json(
    ws_url: str,
    expression: str,
    *,
    await_promise: bool = False,
) -> Any:
    async with websockets.connect(
        ws_url,
        max_size=2**27,
        open_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
        close_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
        ping_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": await_promise,
                    },
                }
            )
        )
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    ws.recv(),
                    timeout=CDP_RESPONSE_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"CDP Runtime.evaluate timed out after {CDP_RESPONSE_TIMEOUT_SECONDS:.0f}s"
                ) from exc
            message = json.loads(raw_message)
            if message.get("id") != 1:
                continue
            result = message.get("result", {}).get("result", {})
            if "value" not in result:
                raise RuntimeError(
                    f"CDP evaluation returned no value: {json.dumps(message)[:500]}"
                )
            value = result["value"]
            if isinstance(value, str):
                return json.loads(value)
            return value


def evaluate_json(ws_url: str, expression: str, *, await_promise: bool = False) -> Any:
    return asyncio.run(
        _cdp_evaluate_json(ws_url, expression, await_promise=await_promise)
    )


async def _cdp_call(
    ws_url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = CDP_RESPONSE_TIMEOUT_SECONDS,
) -> Any:
    async with websockets.connect(
        ws_url,
        max_size=2**27,
        open_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
        close_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
        ping_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": method,
                    "params": params or {},
                }
            )
        )
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    ws.recv(),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"CDP {method} timed out after {timeout_seconds:.0f}s"
                ) from exc
            message = json.loads(raw_message)
            if message.get("id") != 1:
                continue
            if "error" in message:
                raise RuntimeError(
                    f"CDP {method} failed: {json.dumps(message['error'])[:500]}"
                )
            return message.get("result", {})


def call_method(
    ws_url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = CDP_RESPONSE_TIMEOUT_SECONDS,
) -> Any:
    return asyncio.run(
        _cdp_call(ws_url, method, params, timeout_seconds=timeout_seconds)
    )


async def _cdp_navigate(ws_url: str, target_url: str) -> None:
    async with websockets.connect(
        ws_url,
        max_size=2**27,
        open_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
        close_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
        ping_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
    ) as ws:
        await ws.send(
            json.dumps(
                {"id": 1, "method": "Page.navigate", "params": {"url": target_url}}
            )
        )
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    ws.recv(),
                    timeout=CDP_RESPONSE_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"CDP Page.navigate timed out after {CDP_RESPONSE_TIMEOUT_SECONDS:.0f}s"
                ) from exc
            message = json.loads(raw_message)
            if message.get("id") == 1:
                return


def navigate(ws_url: str, target_url: str) -> None:
    asyncio.run(_cdp_navigate(ws_url, target_url))


def set_device_metrics(
    ws_url: str,
    *,
    width: int,
    height: int,
    device_scale_factor: float = 1.0,
    mobile: bool = False,
) -> None:
    call_method(
        ws_url,
        "Emulation.setDeviceMetricsOverride",
        {
            "width": width,
            "height": height,
            "deviceScaleFactor": device_scale_factor,
            "mobile": mobile,
        },
    )


def clear_device_metrics(ws_url: str) -> None:
    call_method(ws_url, "Emulation.clearDeviceMetricsOverride")


def capture_screenshot(
    ws_url: str,
    *,
    output_path: str | None = None,
    clip: dict[str, Any] | None = None,
    capture_beyond_viewport: bool = True,
    image_format: str = "png",
) -> dict[str, Any]:
    result = call_method(
        ws_url,
        "Page.captureScreenshot",
        {
            "format": image_format,
            "captureBeyondViewport": capture_beyond_viewport,
            **({"clip": clip} if clip else {}),
        },
        timeout_seconds=CDP_SCREENSHOT_TIMEOUT_SECONDS,
    )
    data = result.get("data")
    if not isinstance(data, str):
        raise RuntimeError("CDP Page.captureScreenshot returned no data")
    binary = base64.b64decode(data)
    if output_path is not None:
        with open(output_path, "wb") as handle:
            handle.write(binary)
    return {
        "byte_count": len(binary),
        "output_path": output_path or "",
        "format": image_format,
    }
