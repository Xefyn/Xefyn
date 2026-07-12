#!/usr/bin/env python3
"""Update GitHub statistics embedded in profile hero SVGs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubAPIError(RuntimeError):
    """Raised when GitHub returns an unusable API response."""


class GitHubAPI:
    def __init__(self, token: str = "") -> None:
        self.token = token.strip()

    def get(self, path: str) -> tuple[int, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "xefyn-profile-stats",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(f"{API_ROOT}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            message = body
            try:
                message = json.loads(body).get("message", body)
            except json.JSONDecodeError:
                pass
            raise GitHubAPIError(
                f"GitHub API returned {error.code} for {path}: {message}"
            ) from error
        except urllib.error.URLError as error:
            raise GitHubAPIError(f"Could not reach GitHub API for {path}: {error}") from error


@dataclass(frozen=True)
class ProfileStats:
    repositories: int
    commits: int
    additions: int
    deletions: int
    source_repositories: int
    skipped_repositories: int
    includes_private: bool


def paginated(api: GitHubAPI, path: str) -> list[dict[str, Any]]:
    separator = "&" if "?" in path else "?"
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        status, payload = api.get(f"{path}{separator}per_page=100&page={page}")
        if status != 200 or not isinstance(payload, list):
            raise GitHubAPIError(f"Unexpected paginated response for {path}")
        items.extend(payload)
        if len(payload) < 100:
            return items
        page += 1


def repository_list(username: str, token: str) -> tuple[GitHubAPI, list[dict[str, Any]], bool]:
    """Return owned repositories, including private repos when a matching PAT exists."""
    if token:
        authenticated_api = GitHubAPI(token)
        try:
            _, viewer = authenticated_api.get("/user")
            if str(viewer.get("login", "")).casefold() == username.casefold():
                repos = paginated(
                    authenticated_api,
                    "/user/repos?affiliation=owner&visibility=all&sort=full_name",
                )
                owned = [
                    repo
                    for repo in repos
                    if str(repo.get("owner", {}).get("login", "")).casefold()
                    == username.casefold()
                ]
                return authenticated_api, owned, any(repo.get("private") for repo in owned)
            print(
                "STATS_PAT does not belong to the requested username; using public data.",
                file=sys.stderr,
            )
        except GitHubAPIError as error:
            print(f"Could not use STATS_PAT ({error}); using public data.", file=sys.stderr)

    public_api = GitHubAPI()
    encoded_username = urllib.parse.quote(username)
    repos = paginated(
        public_api,
        f"/users/{encoded_username}/repos?type=owner&sort=full_name",
    )
    return public_api, repos, False


def contributor_activity(
    api: GitHubAPI, full_name: str, username: str
) -> tuple[int, int, int] | None:
    """Return commits, additions, and deletions for one contributor in a repo."""
    path = f"/repos/{urllib.parse.quote(full_name, safe='/')}/stats/contributors"
    payload: Any = None

    # GitHub may return 202 while it computes repository statistics.
    for delay in (0, 2, 4, 8):
        if delay:
            time.sleep(delay)
        status, payload = api.get(path)
        if status == 200:
            break
        if status == 204:
            return 0, 0, 0
        if status != 202:
            return None
    else:
        return None

    if not isinstance(payload, list):
        return None

    commits = additions = deletions = 0
    for contributor in payload:
        author = contributor.get("author") or {}
        if str(author.get("login", "")).casefold() != username.casefold():
            continue
        commits += int(contributor.get("total", 0))
        for week in contributor.get("weeks", []):
            additions += int(week.get("a", 0))
            deletions += int(week.get("d", 0))
    return commits, additions, deletions


def collect_stats(username: str, token: str = "") -> ProfileStats:
    api, repositories, includes_private = repository_list(username, token)
    source_repositories = [repo for repo in repositories if not repo.get("fork")]

    commits = additions = deletions = skipped = 0
    for repo in source_repositories:
        full_name = str(repo["full_name"])
        try:
            activity = contributor_activity(api, full_name, username)
        except GitHubAPIError as error:
            print(f"Skipping {full_name}: {error}", file=sys.stderr)
            skipped += 1
            continue
        if activity is None:
            print(f"Skipping {full_name}: contributor statistics unavailable", file=sys.stderr)
            skipped += 1
            continue
        repo_commits, repo_additions, repo_deletions = activity
        commits += repo_commits
        additions += repo_additions
        deletions += repo_deletions

    if skipped:
        raise GitHubAPIError(
            f"Contributor statistics were unavailable for {skipped} repositories; "
            "keeping the previous card. Run the workflow again after GitHub finishes "
            "computing repository statistics."
        )

    return ProfileStats(
        repositories=len(repositories),
        commits=commits,
        additions=additions,
        deletions=deletions,
        source_repositories=len(source_repositories),
        skipped_repositories=0,
        includes_private=includes_private,
    )


def update_svg(path: Path, stats: ProfileStats, updated_at: datetime) -> None:
    values = {
        "stat-repositories": f"{stats.repositories:,}",
        "stat-commits": f"{stats.commits:,}",
        "stat-additions": f"+{stats.additions:,}",
        "stat-deletions": f"−{stats.deletions:,}",
    }
    scope = "public + private" if stats.includes_private else "public"
    values["stat-scope"] = (
        f"{scope} · {stats.source_repositories} non-fork repos · merges excluded · "
        f"{updated_at.strftime('%d %b %Y')}"
    )
    values["stats-desc"] = (
        f"{stats.repositories:,} repositories, {stats.commits:,} authored commits, "
        f"{stats.additions:,} lines added, and {stats.deletions:,} lines removed."
    )

    svg = path.read_text(encoding="utf-8")
    for element_id, value in values.items():
        pattern = re.compile(
            rf'(<(?:text|desc)\b[^>]*\bid="{re.escape(element_id)}"[^>]*>)(.*?)(</(?:text|desc)>)',
            flags=re.DOTALL,
        )
        svg, count = pattern.subn(rf"\g<1>{escape(value)}\g<3>", svg, count=1)
        if count != 1:
            raise RuntimeError(f"Expected one {element_id!r} element in {path}")
    path.write_text(svg, encoding="utf-8", newline="\n")


def update_readme_cache(path: Path, updated_at: datetime) -> None:
    """Change the hero query string so GitHub does not serve a stale SVG."""
    cache_key = updated_at.strftime("%Y%m%d%H%M%S")
    content = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(profile/hero(?:-mobile)?\.svg)(?:\?v=[A-Za-z0-9_-]+)?"
    )
    content, count = pattern.subn(rf"\g<1>?v={cache_key}", content)
    if count != 2:
        raise RuntimeError(f"Expected two hero image references in {path}; found {count}")
    path.write_text(content, encoding="utf-8", newline="\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="GitHub username")
    parser.add_argument(
        "--svg",
        required=True,
        action="append",
        type=Path,
        help="Existing hero SVG to update; repeat for multiple layouts",
    )
    parser.add_argument(
        "--readme",
        required=True,
        type=Path,
        help="README whose hero URLs receive a cache-busting query string",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("STATS_TOKEN", "")
    stats = collect_stats(args.username, token)
    updated_at = datetime.now(timezone.utc)
    for path in args.svg:
        update_svg(path, stats, updated_at)
    update_readme_cache(args.readme, updated_at)
    print(
        f"Updated {len(args.svg)} hero SVGs: {stats.repositories} repos, "
        f"{stats.commits} commits, +{stats.additions}/-{stats.deletions} lines"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
