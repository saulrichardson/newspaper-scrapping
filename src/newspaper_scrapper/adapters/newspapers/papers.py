"""Paper discovery via the live Newspapers.com `/papers/` UI."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urlparse


def papers_search_url(query: str) -> str:
    return "https://www.newspapers.com/papers/?titleKeyword=" + quote_plus(query)


def papers_search_expression() -> str:
    return r"""JSON.stringify((() => {
  const body = document.body?.innerText || '';
  const showingMatch = body.match(/Showing\s+(\d+)\s+papers/i);
  const cards = Array.from(document.querySelectorAll('a[href*="/paper/"]')).map((a) => ({
    text: a.innerText.trim().replace(/\s+/g, ' '),
    href: a.href,
  }));
  const unique = [];
  const seen = new Set();
  for (const card of cards) {
    if (!card.href || seen.has(card.href)) continue;
    seen.add(card.href);
    unique.push(card);
  }
  return {
    url: location.href,
    title: document.title,
    bodySnippet: body.slice(0, 2500),
    showing: showingMatch ? Number(showingMatch[1]) : null,
    cards: unique.slice(0, 20),
  };
})())"""


def paper_page_expression() -> str:
    return r"""JSON.stringify((() => {
  const links = Array.from(document.querySelectorAll('a')).map((a) => ({
    text: a.innerText.trim().replace(/\s+/g, ' '),
    href: a.href,
  })).filter((item) => item.href);
  const browseLinks = links.filter((item) =>
    item.href.startsWith('https://www.newspapers.com/browse/')
  );
  return {
    url: location.href,
    title: document.title,
    bodySnippet: (document.body?.innerText || '').slice(0, 2500),
    browseLinks: browseLinks.slice(0, 20),
  };
})())"""


def normalize_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {token for token in tokens if len(token) > 2}


def parse_year_range(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d{4})\s*[–-]\s*(\d{4})", text)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    if end < start:
        return None
    return start, end


def score_paper_card(card: dict[str, str], family_rows: list[dict[str, str]]) -> int:
    score = 0
    text = card["text"].lower()
    sample_row = family_rows[0]
    family_years = [int(row["issue_date"][:4]) for row in family_rows]
    title_text = sample_row["newspaper_display_name"].lower()
    title_tokens = normalize_tokens(sample_row["newspaper_display_name"])
    query_tokens = normalize_tokens(sample_row["search_query"])

    if title_text in text:
        score += 12
    for token in sorted(title_tokens | query_tokens):
        if token in text:
            score += 1
    city = sample_row.get("city", "")
    state = sample_row.get("state", "")
    if city and city.lower() in text:
        score += 5
    if state and state.lower() in text:
        score += 2
    if "also known as" in text:
        score += 1

    year_range = parse_year_range(card["text"])
    if year_range is not None:
        start, end = year_range
        covered_years = sum(start <= year <= end for year in family_years)
        if covered_years == 0:
            score -= 100
        else:
            score += covered_years * 3
            if covered_years == len(family_years):
                score += 8
    return score


def choose_card(
    cards: list[dict[str, str]], family_rows: list[dict[str, str]]
) -> tuple[str, dict[str, str] | None]:
    if not cards:
        return "no_results", None
    scored = [
        (score_paper_card(card, family_rows), idx, card)
        for idx, card in enumerate(cards)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, _, best_card = scored[0]
    if best_score <= 0:
        return "unscored_results", None
    if len(scored) > 1 and scored[1][0] == best_score:
        return "ambiguous_best_score", None
    return "selected", best_card


def choose_browse_base(browse_links: list[dict[str, str]], paper_url: str) -> str | None:
    paper_id = urlparse(paper_url).path.rstrip("/").split("/")[-1]
    candidates = []
    for link in browse_links:
        href = link["href"]
        if f"_{paper_id}/" in href:
            candidates.append(href)
    if not candidates:
        return None
    candidates.sort(key=len, reverse=True)
    return candidates[0].rstrip("/") + "/"


def login_prefill_expression(email: str, password: str) -> str:
    return f"""JSON.stringify((() => {{
  const setValue = (selectors, value) => {{
    for (const selector of selectors) {{
      const el = document.querySelector(selector);
      if (!el) continue;
      el.focus();
      el.value = value;
      el.dispatchEvent(new Event('input', {{ bubbles: true }}));
      el.dispatchEvent(new Event('change', {{ bubbles: true }}));
      return true;
    }}
    return false;
  }};
  const emailOk = setValue(
    ['input[type="email"]', 'input[name="email"]', 'input[name="username"]'],
    {email!r}
  );
  const passwordOk = setValue(
    ['input[type="password"]', 'input[name="password"]'],
    {password!r}
  );
  return {{
    emailOk,
    passwordOk,
    title: document.title,
    url: location.href,
  }};
}})())"""


def auth_status_expression() -> str:
    return r"""JSON.stringify((() => {
  const body = document.body?.innerText || '';
  const text = body.slice(0, 3000);
  const hrefs = Array.from(document.querySelectorAll('a[href]')).map((a) => a.href);
  return {
    title: document.title,
    url: location.href,
    bodySnippet: text,
    hasAccountLink: hrefs.some((href) => href.includes('/account')),
    hasSignInText: /sign in to newspapers\.com/i.test(body),
    hasSignedInSignal: /my account|account settings|subscription/i.test(body),
  };
})())"""
