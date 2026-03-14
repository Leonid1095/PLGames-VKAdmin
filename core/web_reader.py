"""Web reader — fetch and extract text from URLs (web pages, GitHub, etc.)."""

import html
import re
import logging
from urllib.parse import urlparse

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


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
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; VKAdminBot/1.0)"
            })
            resp.raise_for_status()

        html = resp.text

        # Remove script/style tags
        html = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", " ", html)
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


async def read_github_commits(owner: str, repo: str, since_days: int = 7) -> str:
    """Fetch commits from the last N days for patch notes generation."""
    from datetime import datetime, timezone, timedelta

    since_dt = datetime.now(timezone.utc) - timedelta(days=since_days)

    # Try GitHub API first, fallback to Atom feed if API is blocked
    commits_text = await _fetch_commits_api(owner, repo, since_dt)
    if commits_text is None:
        commits_text = await _fetch_commits_atom(owner, repo, since_dt)

    return commits_text


async def _fetch_commits_api(owner: str, repo: str, since_dt) -> str | None:
    """Fetch commits via GitHub API. Returns None if unreachable."""
    headers = _github_headers()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                f"{_GITHUB_API}/repos/{owner}/{repo}/commits",
                headers=headers,
                params={"since": since_dt.isoformat(), "per_page": 100},
            )
            if resp.status_code != 200:
                return None

            commits = resp.json()
            if not commits:
                days = ((__import__('datetime').datetime.now(__import__('datetime').timezone.utc) - since_dt).days)
                return f"Нет коммитов за последние {days} дней."

            lines = [f"Коммиты ({len(commits)} шт.):\n"]
            for c in commits:
                sha = c.get("sha", "")[:7]
                msg = c.get("commit", {}).get("message", "").split("\n")[0]
                author = c.get("commit", {}).get("author", {}).get("name", "?")
                lines.append(f"- [{sha}] {author}: {msg}")

            return "\n".join(lines)[:5000]
    except Exception as e:
        logger.warning(f"GitHub API unreachable, falling back to Atom: {e}")
        return None


async def _fetch_commits_atom(owner: str, repo: str, since_dt) -> str:
    """Fetch commits via GitHub Atom feed (works when API is blocked)."""
    from datetime import datetime, timezone
    url = f"https://github.com/{owner}/{repo}/commits/main.atom"

    atom_headers = {"User-Agent": "Mozilla/5.0 (compatible; VKAdminBot/1.0)"}
    if settings.GITHUB_TOKEN:
        atom_headers["Authorization"] = f"token {settings.GITHUB_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers=atom_headers)
            if resp.status_code != 200:
                return f"Ошибка загрузки коммитов: HTTP {resp.status_code}"

        # Parse Atom XML
        text = resp.text
        entries = re.findall(
            r'<entry>.*?<link[^>]*href="([^"]*?/commit/([a-f0-9]+))".*?'
            r'<title>\s*(.*?)\s*</title>.*?'
            r'<updated>(\d{4}-\d{2}-\d{2}T[\d:]+Z)</updated>.*?'
            r'<name>(.*?)</name>',
            text, re.DOTALL,
        )

        if not entries:
            return "Нет коммитов (не удалось распарсить Atom feed)."

        commits = []
        for link, sha, title, date_str, author in entries:
            try:
                commit_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if commit_date < since_dt:
                    continue
            except ValueError:
                continue
            title = html.unescape(title.strip())
            commits.append(f"- [{sha[:7]}] {author}: {title}")

        if not commits:
            days = (datetime.now(timezone.utc) - since_dt).days
            return f"Нет коммитов за последние {days} дней."

        lines = [f"Коммиты ({len(commits)} шт.):\n"] + commits
        return "\n".join(lines)[:5000]

    except Exception as e:
        logger.error(f"GitHub Atom feed error: {e}")
        return f"Ошибка загрузки коммитов: {e}"
