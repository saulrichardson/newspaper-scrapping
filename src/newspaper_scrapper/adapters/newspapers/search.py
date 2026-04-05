"""Page-content search helpers for Newspapers.com."""

from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import urlencode


DEFAULT_ENTITY_TYPES = "page"
DEFAULT_SORT = "score-desc"


def build_search_results_url(
    *,
    keyword: str,
    date: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    location: str | None = None,
    page: int | None = None,
) -> str:
    params: dict[str, str] = {"keyword": keyword}
    if date:
        params["date"] = date
    if date_start:
        params["date-start"] = date_start
    if date_end:
        params["date-end"] = date_end
    if location:
        params["location"] = location
    if page and page > 1:
        params["page"] = str(page)
    return "https://www.newspapers.com/search/results/?" + urlencode(params)


def build_search_api_url(
    *,
    keyword: str,
    start: str = "*",
    count: int = 100,
    date: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    location: str | None = None,
    entity_types: str = DEFAULT_ENTITY_TYPES,
    sort: str = DEFAULT_SORT,
) -> str:
    params: dict[str, str] = {
        "keyword": keyword,
        "entity-types": entity_types,
        "sort": sort,
        "start": start,
        "count": str(count),
    }
    if date:
        params["date"] = date
    if date_start:
        params["date-start"] = date_start
    if date_end:
        params["date-end"] = date_end
    if location:
        params["location"] = location
    return "https://www.newspapers.com/api/search/query?" + urlencode(params)


def search_api_fetch_expression(api_url: str) -> str:
    url_js = json.dumps(api_url)
    return """(async () => {{
  const res = await fetch({url_js}, {{credentials: 'include'}});
  const text = await res.text();
  let payload = null;
  try {{
    payload = JSON.parse(text);
  }} catch (err) {{
    payload = null;
  }}
  return {{
    status: res.status,
    ok: res.ok,
    url: res.url,
    textSnippet: text.slice(0, 4000),
    payload,
  }};
}})()""".format(url_js=url_js)


def canonical_image_page_url(image_id: str | int) -> str:
    return f"https://www.newspapers.com/image/{image_id}/"


def slugify_publication_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode(
        "ascii"
    )
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug or "publication"


def build_search_issue_id(
    *,
    publication_name: str,
    issue_date: str,
    publication_canonical_id: str | int | None,
) -> str:
    slug = slugify_publication_name(publication_name)
    canonical = str(publication_canonical_id or "").strip()
    if canonical:
        return f"{slug}__{issue_date}__pub{canonical}"
    return f"{slug}__{issue_date}"


def search_results_expression() -> str:
    return r"""JSON.stringify((() => {
  const body = document.body?.innerText || '';
  const totalMatch = body.match(/([\d,]+)\s+matches/i);
  const cards = Array.from(document.querySelectorAll('div[class*="SearchResult_ArticleResult__"]')).map((card) => {
    const imageLink = card.querySelector('a[href*="/image/"]');
    const title = card.querySelector('h2');
    const metaSpans = Array.from(card.querySelectorAll('a[href*="/image/"] span')).map((el) => (el.innerText || '').trim()).filter(Boolean);
    const detailTexts = Array.from(card.querySelectorAll('span[class*="ArticleResultDetails_DetailLabel__"], span[class*="ArticleResultDetails_DetailLink__"]'))
      .map((el) => (el.innerText || '').trim())
      .filter(Boolean);
    return {
      imagePageUrl: imageLink ? imageLink.href : '',
      headline: title ? (title.innerText || '').trim() : '',
      resultDate: metaSpans[0] || '',
      resultLocation: metaSpans[1] || '',
      snippet: detailTexts.slice(0, 16).join(' | '),
      text: (card.innerText || '').trim().replace(/\s+/g, ' '),
    };
  });
  const nextLink = Array.from(document.querySelectorAll('a[href]')).find((a) => (a.innerText || '').trim() === 'Next');
  const prevLink = Array.from(document.querySelectorAll('a[href]')).find((a) => (a.innerText || '').trim() === 'Previous');
  return {
    title: document.title,
    url: location.href,
    totalMatchesText: totalMatch ? totalMatch[1] : '',
    resultCards: cards,
    nextPageUrl: nextLink ? nextLink.href : '',
    previousPageUrl: prevLink ? prevLink.href : '',
    bodySnippet: body.slice(0, 4000),
  };
})())"""
