#!/usr/bin/env python3
import argparse
import datetime as dt
import os
import sys
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

GITHUB_API = "https://api.github.com/search/repositories"


def build_query(language: str, days: int, use_created: bool) -> str:
    """Search APIで“擬似トレンド”を作るためのクエリを生成"""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    date_field = "created" if use_created else "pushed"
    # 例: language:Python + created:>=2025-09-02
    return f"language:{language} {date_field}:>={since}"


def format_markdown(items: List[Dict[str, Any]], top: int, title: str) -> str:
    md = [
        f"# {title}",
        "",
        f"実行日時: {dt.datetime.now().isoformat(timespec='seconds')}",
    ]
    md.append("")
    for i, it in enumerate(items[:top], 1):
        name = it["full_name"]
        url = it["html_url"]
        stars = it["stargazers_count"]
        desc = (it.get("description") or "").strip().replace("\n", " ")
        lang = it.get("language") or "-"
        created = it["created_at"]
        pushed = it["pushed_at"]
        md.extend(
            [
                f"## {i}. [{name}]({url})  ★{stars}",
                f"- 言語: `{lang}`",
                f"- created: `{created}` / pushed: `{pushed}`",
                f"- 概要: {desc or '—'}",
                "",
            ]
        )
    return "\n".join(md)


def post_slack(webhook: str, text: str) -> None:
    payload = {"text": text}
    r = requests.post(webhook, json=payload, timeout=20)
    r.raise_for_status()


@retry(wait=wait_exponential(min=2, max=20), stop=stop_after_attempt(3))
def search_repos(token: str, query: str, per_page: int) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "trend-daily-script",
    }
    params = {"q": query, "sort": "stars", "order": "desc", "per_page": per_page}
    r = requests.get(GITHUB_API, headers=headers, params=params, timeout=30)
    # Rate limit 時はリトライ
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise RuntimeError("GitHub API rate limited")
    r.raise_for_status()
    return r.json()


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Fetch pseudo-trending repos via GitHub Search API."
    )
    parser.add_argument(
        "--language", default="Python", help="言語フィルタ（例: Python, TypeScript）"
    )
    parser.add_argument(
        "--days", type=int, default=3, help="何日前までを対象にするか（作成/更新日）"
    )
    parser.add_argument("--top", type=int, default=5, help="上位何件を表示するか")
    parser.add_argument(
        "--per-page", type=int, default=30, help="APIから取得する最大件数（上位抽出用）"
    )
    parser.add_argument(
        "--use-created",
        action="store_true",
        help="pushed ではなく created を基準にする",
    )
    parser.add_argument(
        "--markdown-out", default="", help="Markdownを書き出すパス（例: out.md）"
    )
    parser.add_argument(
        "--slack",
        action="store_true",
        help="Slackに投稿する（SLACK_WEBHOOK_URL が必要）",
    )
    parser.add_argument(
        "--title", default="今日のGitHubトレンド", help="Markdown/Slack見出し"
    )
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print(
            "ERROR: GITHUB_TOKEN が未設定です。 .env に設定してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    query = build_query(args.language, args.days, args.use_created)
    data = search_repos(token, query, args.per_page)
    items = data.get("items", [])

    # 出力（コンソール）
    for i, it in enumerate(items[: args.top], 1):
        print(f"{i}. {it['full_name']}  ★{it['stargazers_count']}  {it['html_url']}")

    # Markdown
    if args.markdown_out:
        md = format_markdown(items, args.top, args.title)
        with open(args.markdown_out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"\n[INFO] Markdown を書き出しました -> {args.markdown_out}")

    # Slack
    if args.slack:
        webhook = os.getenv("SLACK_WEBHOOK_URL")
        if not webhook:
            print(
                "WARN: SLACK_WEBHOOK_URL が未設定のため Slack 投稿をスキップしました。",
                file=sys.stderr,
            )
        else:
            text_lines = [args.title, ""]
            for i, it in enumerate(items[: args.top], 1):
                line = f"""{i}. {it["full_name"]} ★{it["stargazers_count"]}
    {it.get("description") or ""}
    {it["html_url"]}"""
                text_lines.append(line)
            post_slack(webhook, "\n".join(text_lines))
            print("[INFO] Slack に投稿しました。")


if __name__ == "__main__":
    main()
