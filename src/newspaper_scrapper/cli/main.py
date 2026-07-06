"""CLI surface for the Newspapers.com scraper."""

from __future__ import annotations

import json
from pathlib import Path

import click

from newspaper_scrapper.application import auth as auth_uc
from newspaper_scrapper.application import catalog as catalog_uc
from newspaper_scrapper.application import discovery as discovery_uc
from newspaper_scrapper.application import download as download_uc
from newspaper_scrapper.application import screenshot as screenshot_uc
from newspaper_scrapper.application import screenshot_workers as screenshot_workers_uc
from newspaper_scrapper.application import search as search_uc
from newspaper_scrapper.application import search_workers as search_workers_uc
from newspaper_scrapper.application import sharding as sharding_uc
from newspaper_scrapper.application import source_manifest as source_manifest_uc
from newspaper_scrapper.application import torch as torch_uc
from newspaper_scrapper.application.auth import launch_browser
from newspaper_scrapper.adapters.chrome import cdp
from newspaper_scrapper.adapters.newspapers import image as image_adapter
from newspaper_scrapper.config import Settings
from newspaper_scrapper.logging_config import configure as configure_logging


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False
    ),
    default="INFO",
)
def cli(log_level: str) -> None:
    configure_logging(log_level.upper())


@cli.command("chrome-launch")
@click.option("--force-new-instance", is_flag=True, default=False)
def chrome_launch_cmd(force_new_instance: bool) -> None:
    settings = Settings()
    result = launch_browser(settings, force_new_instance=force_new_instance)
    click.echo(json.dumps(result, indent=2))


@cli.command("auth-store")
@click.option("--email", default=None, help="Override NEWSCOM_LOGIN_EMAIL.")
@click.option("--password", default=None, help="Override NEWSCOM_LOGIN_PASSWORD.")
@click.option(
    "--output-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the gitignored env file to write.",
)
def auth_store_cmd(email: str | None, password: str | None, output_path: Path | None) -> None:
    settings = Settings()
    email_value = email or settings.newspapers_email
    password_value = password or settings.newspapers_password
    if not email_value or not password_value:
        raise click.ClickException("Missing email/password; pass flags or configure env")
    written = auth_uc.store_credentials(
        settings,
        email=email_value,
        password=password_value,
        output_path=output_path,
    )
    click.echo(str(written))


@cli.command("auth-login")
@click.option("--fill/--no-fill", default=True, show_default=True)
@click.option("--wait-seconds", type=float, default=180.0, show_default=True)
def auth_login_cmd(fill: bool, wait_seconds: float) -> None:
    settings = Settings()
    result = auth_uc.login(settings, fill_credentials=fill, wait_seconds=wait_seconds)
    click.echo(json.dumps(result, indent=2))


@cli.command("auth-status")
def auth_status_cmd() -> None:
    settings = Settings()
    result = auth_uc.auth_status(settings)
    click.echo(json.dumps(result, indent=2))


@cli.command("auth-export-cookies")
@click.option("--output-path", type=click.Path(path_type=Path), required=True)
def auth_export_cookies_cmd(output_path: Path) -> None:
    settings = Settings()
    result = auth_uc.export_cookies(settings, output_path=output_path)
    click.echo(json.dumps(result, indent=2))


@cli.command("auth-import-cookies")
@click.option("--cookies-json", type=click.Path(path_type=Path, exists=True), required=True)
@click.option(
    "--navigate-url",
    default="https://www.newspapers.com/account/",
    show_default=True,
)
@click.option("--wait-seconds", type=float, default=None)
@click.option("--force-new-instance/--no-force-new-instance", default=False, show_default=True)
def auth_import_cookies_cmd(
    cookies_json: Path,
    navigate_url: str,
    wait_seconds: float | None,
    force_new_instance: bool,
) -> None:
    settings = Settings()
    result = auth_uc.import_cookies(
        settings,
        cookies_json=cookies_json,
        navigate_url=navigate_url,
        wait_seconds=wait_seconds,
        force_new_instance=force_new_instance,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("papers-search")
@click.option("--query", required=True)
@click.option("--wait-seconds", type=float, default=None)
def papers_search_cmd(query: str, wait_seconds: float | None) -> None:
    from newspaper_scrapper.adapters.newspapers import papers as papers_adapter

    settings = Settings()
    launch_browser(settings)
    pages = cdp.list_page_tabs(settings.chrome_debug_base)
    target_ws = None
    for page in pages:
        if "newspapers.com" in str(page.get("url", "")):
            target_ws = page["webSocketDebuggerUrl"]
            break
    if not target_ws:
        raise click.ClickException("No open Newspapers.com Chrome tab found")
    cdp.navigate(target_ws, papers_adapter.papers_search_url(query))
    import time

    time.sleep(wait_seconds if wait_seconds is not None else settings.papers_search_wait_seconds)
    state = cdp.evaluate_json(target_ws, papers_adapter.papers_search_expression())
    click.echo(json.dumps(state, indent=2))


@cli.command("search-content")
@click.option("--keyword", required=True, help="Page-content keyword search term.")
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--date", default=None, help="Optional Newspapers.com date filter value.")
@click.option("--date-start", default=None, help="Optional YYYY-MM-DD search range start.")
@click.option("--date-end", default=None, help="Optional YYYY-MM-DD search range end.")
@click.option(
    "--location",
    default=None,
    help="Optional Newspapers.com location filter value if supported by URL.",
)
@click.option(
    "--entity-types",
    default="page",
    show_default=True,
    help="Comma-separated Newspapers.com entity types for search API calls.",
)
@click.option(
    "--max-pages",
    type=int,
    default=1,
    show_default=True,
    help="Maximum search API pages to fetch.",
)
@click.option(
    "--count-per-request",
    type=click.IntRange(1, 100),
    default=100,
    show_default=True,
    help="Number of search records to request per API page.",
)
@click.option("--page-load-seconds", type=float, default=None)
@click.option(
    "--navigate-search-results/--no-navigate-search-results",
    default=False,
    show_default=True,
    help="Whether to load the visual search results page before API pagination.",
)
@click.option("--sleep-between-requests", type=float, default=2.0, show_default=True)
@click.option("--max-api-retries", type=int, default=4, show_default=True)
@click.option("--api-backoff-seconds", type=float, default=30.0, show_default=True)
@click.option("--start-token", default=None, help="Override the search API start token.")
@click.option("--resume/--no-resume", default=True, show_default=True)
def search_content_cmd(
    keyword: str,
    output_dir: Path,
    date: str | None,
    date_start: str | None,
    date_end: str | None,
    location: str | None,
    entity_types: str,
    max_pages: int,
    count_per_request: int,
    page_load_seconds: float | None,
    navigate_search_results: bool,
    sleep_between_requests: float,
    max_api_retries: int,
    api_backoff_seconds: float,
    start_token: str | None,
    resume: bool,
) -> None:
    if date and (date_start or date_end):
        raise click.ClickException("Use either --date or --date-start/--date-end, not both.")
    if bool(date_start) != bool(date_end):
        raise click.ClickException("--date-start and --date-end must be provided together.")
    settings = Settings()
    result = search_uc.search_content(
        settings,
        keyword=keyword,
        output_dir=output_dir,
        date=date,
        date_start=date_start,
        date_end=date_end,
        location=location,
        entity_types=entity_types,
        max_pages=max_pages,
        count_per_request=count_per_request,
        page_load_seconds=(
            settings.papers_search_wait_seconds
            if page_load_seconds is None
            else page_load_seconds
        ),
        sleep_between_requests=sleep_between_requests,
        start_token=start_token,
        resume=resume,
        max_api_retries=max_api_retries,
        api_backoff_seconds=api_backoff_seconds,
        navigate_search_results=navigate_search_results,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("plan-search-workers")
@click.option("--keyword", required=True, help="Page-content keyword to harvest.")
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--workers", type=click.IntRange(1), default=10, show_default=True)
@click.option("--start-year", type=int, default=1800, show_default=True)
@click.option("--end-year", type=int, default=2026, show_default=True)
@click.option("--max-pages", type=int, default=10000, show_default=True)
@click.option(
    "--count-per-request",
    type=click.IntRange(1, 100),
    default=100,
    show_default=True,
)
@click.option("--sleep-between-requests", type=float, default=1.0, show_default=True)
@click.option("--max-api-retries", type=int, default=6, show_default=True)
@click.option("--api-backoff-seconds", type=float, default=60.0, show_default=True)
@click.option(
    "--shard-preset",
    type=click.Choice(["uniform", "density-aware"], case_sensitive=False),
    default="uniform",
    show_default=True,
)
@click.option(
    "--sleep-scale",
    type=float,
    default=1.0,
    show_default=True,
    help="Multiplier for density-aware per-shard sleeps.",
)
@click.option(
    "--entity-types",
    default="page",
    show_default=True,
    help="Comma-separated Newspapers.com entity types for search API calls.",
)
@click.option("--location", default=None)
@click.option("--base-debug-port", type=int, default=9401, show_default=True)
@click.option("--profile-root", type=click.Path(path_type=Path), default=None)
@click.option("--cookies-json", type=click.Path(path_type=Path), default=None)
def plan_search_workers_cmd(
    keyword: str,
    output_dir: Path,
    workers: int,
    start_year: int,
    end_year: int,
    max_pages: int,
    count_per_request: int,
    sleep_between_requests: float,
    max_api_retries: int,
    api_backoff_seconds: float,
    shard_preset: str,
    sleep_scale: float,
    entity_types: str,
    location: str | None,
    base_debug_port: int,
    profile_root: Path | None,
    cookies_json: Path | None,
) -> None:
    result = search_workers_uc.plan_search_workers(
        keyword=keyword,
        output_dir=output_dir,
        worker_count=workers,
        start_year=start_year,
        end_year=end_year,
        max_pages=max_pages,
        count_per_request=count_per_request,
        sleep_between_requests=sleep_between_requests,
        max_api_retries=max_api_retries,
        api_backoff_seconds=api_backoff_seconds,
        shard_preset=shard_preset,
        sleep_scale=sleep_scale,
        entity_types=entity_types,
        location=location,
        base_debug_port=base_debug_port,
        profile_root=profile_root,
        cookies_json=cookies_json,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("run-search-workers")
@click.option("--plan-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option(
    "--max-concurrent-workers",
    type=click.IntRange(1),
    default=2,
    show_default=True,
)
@click.option("--worker-stagger-seconds", type=float, default=120.0, show_default=True)
@click.option("--retry-cooldown-seconds", type=float, default=300.0, show_default=True)
@click.option("--max-worker-attempts", type=int, default=100, show_default=True)
@click.option("--cookies-json", type=click.Path(path_type=Path), default=None)
@click.option("--poll-seconds", type=float, default=5.0, show_default=True)
def run_search_workers_cmd(
    plan_csv: Path,
    output_dir: Path,
    max_concurrent_workers: int,
    worker_stagger_seconds: float,
    retry_cooldown_seconds: float,
    max_worker_attempts: int,
    cookies_json: Path | None,
    poll_seconds: float,
) -> None:
    result = search_workers_uc.run_search_workers(
        plan_csv=plan_csv,
        output_dir=output_dir,
        max_concurrent_workers=max_concurrent_workers,
        worker_stagger_seconds=worker_stagger_seconds,
        retry_cooldown_seconds=retry_cooldown_seconds,
        max_worker_attempts=max_worker_attempts,
        cookies_json=cookies_json,
        poll_seconds=poll_seconds,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("merge-search-workers")
@click.option("--workers-root", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
def merge_search_workers_cmd(
    workers_root: Path,
    output_dir: Path,
) -> None:
    result = search_workers_uc.merge_search_workers(
        workers_root=workers_root,
        output_dir=output_dir,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("plan-screenshot-workers")
@click.option("--manifest-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--workers", type=click.IntRange(1), required=True)
@click.option(
    "--grouping-mode",
    type=click.Choice(["issue", "page"], case_sensitive=False),
    default="issue",
    show_default=True,
    help="Keep pages from the same issue on one worker or shard at page granularity.",
)
@click.option("--base-debug-port", type=int, default=9701, show_default=True)
@click.option("--profile-root", type=click.Path(path_type=Path), default=None)
@click.option("--cookies-json", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strategy",
    type=click.Choice(
        sorted(screenshot_uc.STRATEGY_CHOICES - {screenshot_uc.STRATEGY_AUTO}),
        case_sensitive=False,
    ),
    default=screenshot_uc.STRATEGY_TILES,
    show_default=True,
)
@click.option("--page-load-seconds", type=float, default=6.0, show_default=True)
@click.option("--render-wait-seconds", type=float, default=8.0, show_default=True)
@click.option("--sleep-between-pages", type=float, default=0.0, show_default=True)
@click.option("--sleep-jitter-seconds", type=float, default=0.0, show_default=True)
@click.option("--adaptive-sleep/--fixed-sleep", default=False, show_default=True)
@click.option("--min-sleep-between-pages", type=float, default=0.0, show_default=True)
@click.option("--max-sleep-between-pages", type=float, default=None)
@click.option("--sleep-step-seconds", type=float, default=0.25, show_default=True)
@click.option("--clean-streak-threshold", type=int, default=3, show_default=True)
@click.option("--slow-page-threshold-seconds", type=float, default=12.0, show_default=True)
@click.option(
    "--post-render-settle-seconds",
    type=float,
    default=screenshot_uc.POST_RENDER_SETTLE_SECONDS,
    show_default=True,
)
@click.option("--recycle-browser-every-pages", type=int, default=0, show_default=True)
@click.option("--max-passes", type=int, default=3, show_default=True)
@click.option("--pass-page-load-increment", type=float, default=0.75, show_default=True)
@click.option("--pass-render-wait-increment", type=float, default=2.0, show_default=True)
@click.option("--stop-on-stall/--allow-stall", default=True, show_default=True)
@click.option(
    "--restart-browser-before-run/--reuse-browser-before-run",
    default=True,
    show_default=True,
)
@click.option(
    "--restart-browser-each-pass/--reuse-browser-each-pass",
    default=True,
    show_default=True,
)
def plan_screenshot_workers_cmd(
    manifest_csv: Path,
    output_dir: Path,
    workers: int,
    grouping_mode: str,
    base_debug_port: int,
    profile_root: Path | None,
    cookies_json: Path | None,
    strategy: str,
    page_load_seconds: float,
    render_wait_seconds: float,
    sleep_between_pages: float,
    sleep_jitter_seconds: float,
    adaptive_sleep: bool,
    min_sleep_between_pages: float,
    max_sleep_between_pages: float | None,
    sleep_step_seconds: float,
    clean_streak_threshold: int,
    slow_page_threshold_seconds: float,
    post_render_settle_seconds: float,
    recycle_browser_every_pages: int,
    max_passes: int,
    pass_page_load_increment: float,
    pass_render_wait_increment: float,
    stop_on_stall: bool,
    restart_browser_before_run: bool,
    restart_browser_each_pass: bool,
) -> None:
    result = screenshot_workers_uc.plan_screenshot_workers(
        manifest_csv=manifest_csv,
        output_dir=output_dir,
        worker_count=workers,
        grouping_mode=grouping_mode,
        base_debug_port=base_debug_port,
        profile_root=profile_root,
        cookies_json=cookies_json,
        strategy=strategy,
        page_load_seconds=page_load_seconds,
        render_wait_seconds=render_wait_seconds,
        sleep_between_pages=sleep_between_pages,
        sleep_jitter_seconds=sleep_jitter_seconds,
        adaptive_sleep=adaptive_sleep,
        min_sleep_between_pages=min_sleep_between_pages,
        max_sleep_between_pages=max_sleep_between_pages,
        sleep_step_seconds=sleep_step_seconds,
        clean_streak_threshold=clean_streak_threshold,
        slow_page_threshold_seconds=slow_page_threshold_seconds,
        post_render_settle_seconds=post_render_settle_seconds,
        recycle_browser_every_pages=recycle_browser_every_pages,
        max_passes=max_passes,
        pass_page_load_increment=pass_page_load_increment,
        pass_render_wait_increment=pass_render_wait_increment,
        stop_on_stall=stop_on_stall,
        restart_browser_before_run=restart_browser_before_run,
        restart_browser_each_pass=restart_browser_each_pass,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("run-screenshot-workers")
@click.option("--plan-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option(
    "--max-concurrent-workers",
    type=click.IntRange(1),
    default=2,
    show_default=True,
)
@click.option("--worker-stagger-seconds", type=float, default=120.0, show_default=True)
@click.option("--retry-cooldown-seconds", type=float, default=300.0, show_default=True)
@click.option("--max-worker-attempts", type=int, default=10, show_default=True)
@click.option("--cookies-json", type=click.Path(path_type=Path), default=None)
@click.option("--poll-seconds", type=float, default=5.0, show_default=True)
@click.option(
    "--retry-on-cloudflare-challenge/--stop-on-cloudflare-challenge",
    default=False,
    show_default=True,
)
def run_screenshot_workers_cmd(
    plan_csv: Path,
    output_dir: Path,
    max_concurrent_workers: int,
    worker_stagger_seconds: float,
    retry_cooldown_seconds: float,
    max_worker_attempts: int,
    cookies_json: Path | None,
    poll_seconds: float,
    retry_on_cloudflare_challenge: bool,
) -> None:
    result = screenshot_workers_uc.run_screenshot_workers(
        plan_csv=plan_csv,
        output_dir=output_dir,
        max_concurrent_workers=max_concurrent_workers,
        worker_stagger_seconds=worker_stagger_seconds,
        retry_cooldown_seconds=retry_cooldown_seconds,
        max_worker_attempts=max_worker_attempts,
        cookies_json=cookies_json,
        poll_seconds=poll_seconds,
        retry_on_cloudflare_challenge=retry_on_cloudflare_challenge,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("merge-screenshot-workers")
@click.option("--workers-root", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
def merge_screenshot_workers_cmd(
    workers_root: Path,
    output_dir: Path,
) -> None:
    result = screenshot_workers_uc.merge_screenshot_workers(
        workers_root=workers_root,
        output_dir=output_dir,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("discover-issues")
@click.option("--base-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--confirmed-csv", type=click.Path(path_type=Path), default=None)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--family-limit", type=int, default=8, show_default=True)
@click.option("--family-offset", type=int, default=0, show_default=True)
@click.option("--page-load-seconds", type=float, default=None)
@click.option("--sleep-between-families", type=float, default=8.0, show_default=True)
@click.option("--max-api-retries", type=int, default=4, show_default=True)
@click.option("--api-backoff-seconds", type=float, default=20.0, show_default=True)
def discover_issues_cmd(
    base_csv: Path,
    confirmed_csv: Path | None,
    output_dir: Path,
    family_limit: int,
    family_offset: int,
    page_load_seconds: float | None,
    sleep_between_families: float,
    max_api_retries: int,
    api_backoff_seconds: float,
) -> None:
    settings = Settings()
    result = discovery_uc.discover_issues_via_papers(
        settings,
        base_csv=base_csv,
        confirmed_csv=confirmed_csv,
        output_dir=output_dir,
        family_limit=family_limit,
        family_offset=family_offset,
        page_load_seconds=(
            page_load_seconds if page_load_seconds is not None else settings.papers_search_wait_seconds
        ),
        sleep_between_families=sleep_between_families,
        max_api_retries=max_api_retries,
        api_backoff_seconds=api_backoff_seconds,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("catalog-issue-pages")
@click.option("--confirmed-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--target-page-manifest", type=click.Path(path_type=Path), default=None)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--sleep-seconds", type=float, default=0.0, show_default=True)
@click.option("--max-retries", type=int, default=6, show_default=True)
@click.option("--retry-backoff-seconds", type=float, default=15.0, show_default=True)
def catalog_issue_pages_cmd(
    confirmed_csv: Path,
    target_page_manifest: Path | None,
    output_dir: Path,
    sleep_seconds: float,
    max_retries: int,
    retry_backoff_seconds: float,
) -> None:
    result = catalog_uc.catalog_issue_pages(
        confirmed_csv=confirmed_csv,
        output_dir=output_dir,
        target_page_manifest=target_page_manifest,
        sleep_seconds=sleep_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("probe-image")
@click.option("--image-page-url", required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--wait-seconds", type=float, default=None)
def probe_image_cmd(image_page_url: str, output_dir: Path, wait_seconds: float | None) -> None:
    from newspaper_scrapper.adapters.chrome import applescript
    import time

    settings = Settings()
    launch_browser(settings)
    applescript.navigate_front_tab(settings.chrome_app_name, image_page_url)
    time.sleep(wait_seconds if wait_seconds is not None else settings.page_load_seconds)
    probe = image_adapter.evaluate_live_probe(
        chrome_debug_base=settings.chrome_debug_base,
        chrome_app_name=settings.chrome_app_name,
        target_url=image_page_url,
    )
    full_image_url = image_adapter.build_full_image_url(probe)
    output_dir.mkdir(parents=True, exist_ok=True)
    image_id = image_page_url.rstrip("/").split("/")[-1]
    output_path = output_dir / f"{image_id}.jpg"
    meta = image_adapter.download_binary(full_image_url, output_path)
    click.echo(
        json.dumps(
            {
                "image_page_url": image_page_url,
                "full_image_url": full_image_url,
                "output_path": str(output_path),
                "download": meta,
                "probe": probe,
            },
            indent=2,
        )
    )


@cli.command("capture-viewer-screenshot")
@click.option("--image-page-url", required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--page-load-seconds", type=float, default=None)
@click.option("--render-wait-seconds", type=float, default=6.0, show_default=True)
@click.option(
    "--strategy",
    type=click.Choice(sorted(screenshot_uc.STRATEGY_CHOICES), case_sensitive=False),
    default=screenshot_uc.STRATEGY_AUTO,
    show_default=True,
)
def capture_viewer_screenshot_cmd(
    image_page_url: str,
    output_dir: Path,
    page_load_seconds: float | None,
    render_wait_seconds: float,
    strategy: str,
) -> None:
    settings = Settings()
    result = screenshot_uc.capture_viewer_screenshot(
        settings,
        image_page_url=image_page_url,
        output_dir=output_dir,
        page_load_seconds=(
            settings.page_load_seconds if page_load_seconds is None else page_load_seconds
        ),
        render_wait_seconds=render_wait_seconds,
        strategy=strategy,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("screenshot-pages")
@click.option("--manifest-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--page-load-seconds", type=float, default=None)
@click.option("--render-wait-seconds", type=float, default=6.0, show_default=True)
@click.option("--sleep-between-pages", type=float, default=None)
@click.option("--sleep-jitter-seconds", type=float, default=0.0, show_default=True)
@click.option("--adaptive-sleep/--fixed-sleep", default=False, show_default=True)
@click.option("--min-sleep-between-pages", type=float, default=0.0, show_default=True)
@click.option("--max-sleep-between-pages", type=float, default=None)
@click.option("--sleep-step-seconds", type=float, default=0.25, show_default=True)
@click.option("--clean-streak-threshold", type=int, default=3, show_default=True)
@click.option("--slow-page-threshold-seconds", type=float, default=12.0, show_default=True)
@click.option(
    "--post-render-settle-seconds",
    type=float,
    default=screenshot_uc.POST_RENDER_SETTLE_SECONDS,
    show_default=True,
)
@click.option("--recycle-browser-every-pages", type=int, default=0, show_default=True)
@click.option("--limit", type=int, default=None)
@click.option("--start-offset", type=int, default=0, show_default=True)
@click.option(
    "--strategy",
    type=click.Choice(sorted(screenshot_uc.STRATEGY_CHOICES), case_sensitive=False),
    default=screenshot_uc.STRATEGY_AUTO,
    show_default=True,
)
@click.option("--continue-on-error/--stop-on-error", default=True, show_default=True)
def screenshot_pages_cmd(
    manifest_csv: Path,
    output_dir: Path,
    page_load_seconds: float | None,
    render_wait_seconds: float,
    sleep_between_pages: float | None,
    sleep_jitter_seconds: float,
    adaptive_sleep: bool,
    min_sleep_between_pages: float,
    max_sleep_between_pages: float | None,
    sleep_step_seconds: float,
    clean_streak_threshold: int,
    slow_page_threshold_seconds: float,
    post_render_settle_seconds: float,
    recycle_browser_every_pages: int,
    limit: int | None,
    start_offset: int,
    strategy: str,
    continue_on_error: bool,
) -> None:
    settings = Settings()
    result = screenshot_uc.capture_pages_from_manifest(
        settings,
        manifest_csv=manifest_csv,
        output_dir=output_dir,
        page_load_seconds=(
            settings.page_load_seconds if page_load_seconds is None else page_load_seconds
        ),
        render_wait_seconds=render_wait_seconds,
        sleep_between_pages=(
            settings.sleep_between_downloads
            if sleep_between_pages is None
            else sleep_between_pages
        ),
        sleep_jitter_seconds=sleep_jitter_seconds,
        adaptive_sleep=adaptive_sleep,
        min_sleep_between_pages=min_sleep_between_pages,
        max_sleep_between_pages=max_sleep_between_pages,
        sleep_step_seconds=sleep_step_seconds,
        clean_streak_threshold=clean_streak_threshold,
        slow_page_threshold_seconds=slow_page_threshold_seconds,
        post_render_settle_seconds=post_render_settle_seconds,
        recycle_browser_every_pages=recycle_browser_every_pages,
        limit=limit,
        start_offset=start_offset,
        strategy=strategy,
        continue_on_error=continue_on_error,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("screenshot-pages-production")
@click.option("--manifest-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--page-load-seconds", type=float, default=None)
@click.option("--render-wait-seconds", type=float, default=8.0, show_default=True)
@click.option("--sleep-between-pages", type=float, default=0.0, show_default=True)
@click.option("--sleep-jitter-seconds", type=float, default=0.0, show_default=True)
@click.option("--adaptive-sleep/--fixed-sleep", default=False, show_default=True)
@click.option("--min-sleep-between-pages", type=float, default=0.0, show_default=True)
@click.option("--max-sleep-between-pages", type=float, default=None)
@click.option("--sleep-step-seconds", type=float, default=0.25, show_default=True)
@click.option("--clean-streak-threshold", type=int, default=3, show_default=True)
@click.option("--slow-page-threshold-seconds", type=float, default=12.0, show_default=True)
@click.option(
    "--post-render-settle-seconds",
    type=float,
    default=screenshot_uc.POST_RENDER_SETTLE_SECONDS,
    show_default=True,
)
@click.option("--recycle-browser-every-pages", type=int, default=0, show_default=True)
@click.option("--limit", type=int, default=None)
@click.option("--start-offset", type=int, default=0, show_default=True)
@click.option(
    "--strategy",
    type=click.Choice(sorted(screenshot_uc.STRATEGY_CHOICES - {screenshot_uc.STRATEGY_AUTO}), case_sensitive=False),
    default=screenshot_uc.STRATEGY_TILES,
    show_default=True,
)
@click.option("--max-passes", type=int, default=3, show_default=True)
@click.option(
    "--pass-page-load-increment",
    type=float,
    default=0.75,
    show_default=True,
)
@click.option(
    "--pass-render-wait-increment",
    type=float,
    default=2.0,
    show_default=True,
)
@click.option("--stop-on-stall/--allow-stall", default=True, show_default=True)
@click.option(
    "--restart-browser-before-run/--reuse-browser-before-run",
    default=True,
    show_default=True,
)
@click.option(
    "--restart-browser-each-pass/--reuse-browser-each-pass",
    default=True,
    show_default=True,
)
def screenshot_pages_production_cmd(
    manifest_csv: Path,
    output_dir: Path,
    page_load_seconds: float | None,
    render_wait_seconds: float,
    sleep_between_pages: float,
    sleep_jitter_seconds: float,
    adaptive_sleep: bool,
    min_sleep_between_pages: float,
    max_sleep_between_pages: float | None,
    sleep_step_seconds: float,
    clean_streak_threshold: int,
    slow_page_threshold_seconds: float,
    post_render_settle_seconds: float,
    recycle_browser_every_pages: int,
    limit: int | None,
    start_offset: int,
    strategy: str,
    max_passes: int,
    pass_page_load_increment: float,
    pass_render_wait_increment: float,
    stop_on_stall: bool,
    restart_browser_before_run: bool,
    restart_browser_each_pass: bool,
) -> None:
    settings = Settings()
    result = screenshot_uc.capture_pages_production(
        settings,
        manifest_csv=manifest_csv,
        output_dir=output_dir,
        page_load_seconds=(
            settings.page_load_seconds if page_load_seconds is None else page_load_seconds
        ),
        render_wait_seconds=render_wait_seconds,
        sleep_between_pages=sleep_between_pages,
        sleep_jitter_seconds=sleep_jitter_seconds,
        adaptive_sleep=adaptive_sleep,
        min_sleep_between_pages=min_sleep_between_pages,
        max_sleep_between_pages=max_sleep_between_pages,
        sleep_step_seconds=sleep_step_seconds,
        clean_streak_threshold=clean_streak_threshold,
        slow_page_threshold_seconds=slow_page_threshold_seconds,
        post_render_settle_seconds=post_render_settle_seconds,
        recycle_browser_every_pages=recycle_browser_every_pages,
        limit=limit,
        start_offset=start_offset,
        strategy=strategy,
        max_passes=max_passes,
        pass_page_load_increment=pass_page_load_increment,
        pass_render_wait_increment=pass_render_wait_increment,
        stop_on_stall=stop_on_stall,
        restart_browser_before_run=restart_browser_before_run,
        restart_browser_each_pass=restart_browser_each_pass,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("download-pages")
@click.option("--manifest-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--page-load-seconds", type=float, default=None)
@click.option("--sleep-between-pages", type=float, default=None)
@click.option("--sleep-jitter-seconds", type=float, default=0.0, show_default=True)
@click.option("--limit", type=int, default=None)
@click.option("--start-offset", type=int, default=0, show_default=True)
def download_pages_cmd(
    manifest_csv: Path,
    output_dir: Path,
    page_load_seconds: float | None,
    sleep_between_pages: float | None,
    sleep_jitter_seconds: float,
    limit: int | None,
    start_offset: int,
) -> None:
    settings = Settings()
    result = download_uc.download_pages_from_manifest(
        settings,
        manifest_csv=manifest_csv,
        output_dir=output_dir,
        page_load_seconds=(
            settings.page_load_seconds if page_load_seconds is None else page_load_seconds
        ),
        sleep_between_pages=(
            settings.sleep_between_downloads
            if sleep_between_pages is None
            else sleep_between_pages
        ),
        sleep_jitter_seconds=sleep_jitter_seconds,
        limit=limit,
        start_offset=start_offset,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("download-issue")
@click.option("--exact-issue-url", required=True)
@click.option("--issue-id", required=True)
@click.option("--issue-date", required=True)
@click.option("--newspaper-display-name", required=True)
@click.option("--matched-paper-url", default="", show_default=True)
@click.option("--pages", default="all", show_default=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--page-load-seconds", type=float, default=None)
@click.option("--sleep-between-pages", type=float, default=None)
@click.option("--sleep-jitter-seconds", type=float, default=0.0, show_default=True)
@click.option("--max-retries", type=int, default=6, show_default=True)
@click.option("--retry-backoff-seconds", type=float, default=15.0, show_default=True)
def download_issue_cmd(
    exact_issue_url: str,
    issue_id: str,
    issue_date: str,
    newspaper_display_name: str,
    matched_paper_url: str,
    pages: str,
    output_dir: Path,
    page_load_seconds: float | None,
    sleep_between_pages: float | None,
    sleep_jitter_seconds: float,
    max_retries: int,
    retry_backoff_seconds: float,
) -> None:
    settings = Settings()
    result = download_uc.download_issue(
        settings,
        exact_issue_url=exact_issue_url,
        issue_id=issue_id,
        issue_date=issue_date,
        newspaper_display_name=newspaper_display_name,
        matched_paper_url=matched_paper_url,
        output_dir=output_dir,
        pages=pages,
        page_load_seconds=(
            settings.page_load_seconds if page_load_seconds is None else page_load_seconds
        ),
        sleep_between_pages=(
            settings.sleep_between_downloads
            if sleep_between_pages is None
            else sleep_between_pages
        ),
        sleep_jitter_seconds=sleep_jitter_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("shard-manifest")
@click.option("--manifest-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--num-shards", type=click.IntRange(1, None), required=True)
@click.option(
    "--strategy",
    type=click.Choice(["by_issue", "round_robin"], case_sensitive=False),
    default="by_issue",
    show_default=True,
)
def shard_manifest_cmd(
    manifest_csv: Path,
    output_dir: Path,
    num_shards: int,
    strategy: str,
) -> None:
    result = sharding_uc.shard_manifest(
        manifest_csv=manifest_csv,
        output_dir=output_dir,
        num_shards=num_shards,
        strategy=strategy,
    )
    click.echo(json.dumps(result, indent=2))


@cli.command("build-source-artifact-manifest")
@click.option("--input-csv", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--output-jsonl", type=click.Path(path_type=Path), required=True)
@click.option(
    "--image-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional base directory used when image paths are relative or absent.",
)
@click.option(
    "--image-path-field",
    default="output_path",
    show_default=True,
    help="CSV column containing the acquired image path.",
)
@click.option("--source-system", default="newspapers.com", show_default=True)
@click.option(
    "--include-status",
    multiple=True,
    help="Only include rows with this status. Repeatable. If omitted, include all rows.",
)
@click.option(
    "--require-files/--allow-missing-files",
    default=False,
    show_default=True,
    help="Require every emitted image path to exist and include checksums.",
)
def build_source_artifact_manifest_cmd(
    input_csv: Path,
    output_jsonl: Path,
    image_root: Path | None,
    image_path_field: str,
    source_system: str,
    include_status: tuple[str, ...],
    require_files: bool,
) -> None:
    result = source_manifest_uc.write_source_artifact_manifest(
        input_csv=input_csv,
        output_jsonl=output_jsonl,
        image_root=image_root,
        image_path_field=image_path_field,
        source_system=source_system,
        include_statuses={status for status in include_status if status},
        require_files=require_files,
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@cli.command("validate-source-artifact-manifest")
@click.option("--input-jsonl", type=click.Path(path_type=Path, exists=True), required=True)
@click.option(
    "--require-files/--allow-missing-files",
    default=False,
    show_default=True,
    help="Fail when image_path does not point to an existing file.",
)
@click.option(
    "--require-checksums/--allow-missing-checksums",
    default=False,
    show_default=True,
    help="Fail when checksum_sha256 is empty.",
)
@click.option(
    "--verify-checksums/--trust-checksums",
    default=False,
    show_default=True,
    help="Recompute file SHA-256 values and compare them to checksum_sha256.",
)
@click.option(
    "--warnings-as-errors/--allow-warnings",
    default=False,
    show_default=True,
    help="Return a failing status when validation emits warnings.",
)
@click.option(
    "--output-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional path to write the validation report JSON.",
)
def validate_source_artifact_manifest_cmd(
    input_jsonl: Path,
    require_files: bool,
    require_checksums: bool,
    verify_checksums: bool,
    warnings_as_errors: bool,
    output_json: Path | None,
) -> None:
    report = source_manifest_uc.validate_source_artifact_manifest(
        input_jsonl=input_jsonl,
        require_files=require_files,
        require_checksums=require_checksums,
        verify_checksums=verify_checksums,
        warnings_are_errors=warnings_as_errors,
    )
    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    click.echo(json.dumps(report, indent=2, sort_keys=True))
    if report["status"] == "error":
        raise click.ClickException("source artifact manifest validation failed")


@cli.command("torch-check")
@click.option("--host", default="torch", show_default=True)
def torch_check_cmd(host: str) -> None:
    result = torch_uc.torch_check(host=host)
    click.echo(json.dumps(result, indent=2))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
