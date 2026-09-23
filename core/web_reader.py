"""Web reader — fetch and extract text from URLs (web pages, GitHub, etc.)."""

import asyncio
import html
import re
import socket
import ipaddress
import logging
from urllib.parse import urljoin, urlparse

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


def is_safe_public_url(url: str) -> bool:
    """SSRF guard: allow only http(s) URLs that resolve to public IPs.

    Blocks loopback/private/link-local/reserved ranges so an admin-supplied
    (or forged-token-supplied) URL can't reach 127.0.0.1, 169.254.169.254
    (cloud metadata), or internal services. Checks ONE url — to fetch
    user-supplied URLs use safe_get(), which re-checks every redirect hop.
    """
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except Exception:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


class UnsafeURLError(Exception):
    """URL (или шаг его редиректа) ведёт во внутреннюю сеть."""


def _http_client(timeout: float) -> httpx.AsyncClient:
    # Редиректы — только вручную в safe_get, с проверкой каждого шага.
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


async def safe_get(
    url: str, *, headers: dict | None = None, timeout: float = 15, max_redirects: int = 5,
) -> httpx.Response:
    """GET пользовательского URL с SSRF-защитой на каждом шаге редиректа.

    httpx с follow_redirects=True проверку обходил: внешний адрес отвечал
    302 → http://127.0.0.1:<порт>/ и бот читал внутренний сервис.
    Бросает UnsafeURLError; остаточный риск — DNS rebinding между проверкой
    и соединением.
    """
    async with _http_client(timeout) as client:
        for _ in range(max_redirects + 1):
            if not await asyncio.to_thread(is_safe_public_url, url):
                raise UnsafeURLError(url)
            resp = await client.get(url, headers=headers)
            if not resp.is_redirect:
                return resp
            url = urljoin(str(resp.url), resp.headers.get("location", ""))
    raise UnsafeURLError(f"too many redirects: {url}")


def _github_headers() -> dict:
    """Build GitHub API headers, with auth token if available."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "VKAdminBot/1.0",
    }
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"token {settings.GITHUB_TOKEN}"
    return headers


async def read_url(url: str) -> str:
    """
    Fetch a URL and return extracted text content.
    Automatically detects GitHub repos and uses API for better data.
    Returns plain text, max ~5000 chars.
    """
    parsed = urlparse(url)

    if parsed.hostname in ("github.com", "www.github.com"):
        return await _read_github(parsed.path.strip("/"))

    return await _read_webpage(url)


async def _read_webpage(url: str) -> str:
    """Fetch a web page and extract readable text."""
    if not is_safe_public_url(url):
        logger.warning(f"Blocked non-public/unsafe URL fetch: {url}")
        return "Ошибка: ссылка недоступна (недопустимый или внутренний адрес)."
    try:
        resp = await safe_get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; VKAdminBot/1.0)"
        })
        resp.raise_for_status()

        raw_html = resp.text

        # Remove script/style tags
        raw_html = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", " ", raw_html)
        # Clean up whitespace
        text = re.sub(r"\s+", " ", text).strip()
        # Decode HTML entities
        text = html.unescape(text)

        return text[:5000]
    except Exception as e:
        logger.error(f"Failed to read webpage {url}: {e}")
        return f"Ошибка загрузки страницы: {e}"


async def _read_github(repo_path: str) -> str:
    """
    Read GitHub repo data via API.
    Supports:
      - owner/repo → recent commits + description
      - owner/repo/releases → releases
      - owner/repo/commits → commits
    """
    parts = repo_path.split("/")
    if len(parts) < 2:
        return await _read_webpage(f"https://github.com/{repo_path}")

    owner, repo = parts[0], parts[1]
    # Remove .git suffix if present
    repo = repo.removesuffix(".git")
    sub = parts[2] if len(parts) > 2 else ""

    headers = _github_headers()

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # Always get repo info
            repo_resp = await client.get(f"{_GITHUB_API}/repos/{owner}/{repo}", headers=headers)
            repo_data = repo_resp.json() if repo_resp.status_code == 200 else {}

            result_parts = []

            if repo_data:
                result_parts.append(
                    f"Репозиторий: {repo_data.get('full_name', repo_path)}\n"
                    f"Описание: {repo_data.get('description', 'нет')}\n"
                    f"Звёзды: {repo_data.get('stargazers_count', 0)}, "
                    f"Форки: {repo_data.get('forks_count', 0)}\n"
                    f"Язык: {repo_data.get('language', '?')}"
                )

            if sub == "releases" or not sub:
                # Fetch releases
                rel_resp = await client.get(
                    f"{_GITHUB_API}/repos/{owner}/{repo}/releases",
                    headers=headers, params={"per_page": 5},
                )
                if rel_resp.status_code == 200:
                    releases = rel_resp.json()
                    if releases:
                        result_parts.append("\n--- Последние релизы ---")
                        for rel in releases[:5]:
                            tag = rel.get("tag_name", "?")
                            name = rel.get("name", tag)
                            body = rel.get("body", "")[:500]
                            date = rel.get("published_at", "")[:10]
                            result_parts.append(f"\n[{tag}] {name} ({date})\n{body}")

            # Always fetch recent commits
            commits_resp = await client.get(
                f"{_GITHUB_API}/repos/{owner}/{repo}/commits",
                headers=headers, params={"per_page": 20},
            )
            if commits_resp.status_code == 200:
                commits = commits_resp.json()
                if commits:
                    result_parts.append("\n--- Последние коммиты ---")
                    for c in commits[:20]:
                        sha = c.get("sha", "")[:7]
                        msg = c.get("commit", {}).get("message", "").split("\n")[0]
                        date = c.get("commit", {}).get("author", {}).get("date", "")[:10]
                        author = c.get("commit", {}).get("author", {}).get("name", "?")
                        result_parts.append(f"[{sha}] {date} {author}: {msg}")

            return "\n".join(result_parts)[:5000]

    except Exception as e:
        logger.error(f"GitHub API error for {repo_path}: {e}")
        return await _read_webpage(f"https://github.com/{repo_path}")


