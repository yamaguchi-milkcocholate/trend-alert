#!/usr/bin/env python3
import argparse
import datetime as dt
import os
import sys
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from langchain.output_parsers import PydanticOutputParser
from langchain.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

GITHUB_API = "https://api.github.com/search/repositories"


class RepoDigest(BaseModel):
    summary: str = Field(description="技術の要点を2-3文で")
    why_care: str = Field(description="今使う価値を一言で")
    use_cases: list[str] = Field(default_factory=list, description="具体用途 最大3")
    setup: list[str] = Field(default_factory=list, description="最短手順 2-4行")
    difficulty: int = Field(ge=1, le=5, description="1=易 5=重")


def build_chain():
    parser = PydanticOutputParser(pydantic_object=RepoDigest)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "あなたは優秀なMLエンジニアの編集者。出力は必ずJSONのみ。"),
            (
                "user",
                """次のGitHubリポを、実務エンジニアが“今日触るか決める”ための最小情報に要約。
入力:
- name: {name}
- url: {url}
- description: {desc}
- meta:
  Language: {lang}
  Stars: {stars}

要件:
- summary: 2-3文
- why_care: 一言
- use_cases: 最大3（名詞句）
- setup: 2-4行（箇条書き/最短手順）
- difficulty: 1-5（1=超簡単）

{format_instructions}""",
            ),
        ]
    ).partial(format_instructions=parser.get_format_instructions())

    llm = ChatOpenAI(
        model=os.getenv("OPENAI_MODEL"), openai_api_key=os.getenv("OPENAI_API_KEY")
    )
    return prompt | llm | parser


def summarize_with_langchain(repo: Dict[str, Any], chain) -> RepoDigest:
    return chain.invoke(
        {
            "name": repo["full_name"],
            "url": repo["html_url"],
            "desc": (repo.get("description") or "").strip(),
            "lang": repo.get("language") or "-",
            "stars": repo.get("stargazers_count", 0),
        }
    )


def slack_block_with_digest(i: int, it: Dict[str, Any], d: RepoDigest) -> str:
    uc = " ・".join(d.use_cases[:3]) if d.use_cases else "—"
    setup = "\n".join([f"   - {s}" for s in d.setup[:4]]) if d.setup else ""
    return (
        f"{i}. {it['full_name']} ★{it['stargazers_count']}\n"
        f"   {it['html_url']}\n"
        f"   {d.summary}\n"
        f"   🧠 {d.why_care} / 難易度★{d.difficulty}\n"
        f"   使いどころ: {uc}\n"
        f"{setup}\n"
    )


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

    token = os.getenv("TREND_READ_GITHUB_TOKEN")
    if not token:
        print(
            "ERROR: TREND_READ_GITHUB_TOKEN が未設定です。 .env に設定してください。",
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
            chain = build_chain()
            text_lines = [args.title, ""]

            for i, it in enumerate(items[: args.top], 1):
                d = summarize_with_langchain(it, chain)
                text_lines.append(slack_block_with_digest(i, it, d))

                text_lines.append(f"   {(it.get('description') or '').strip()}")

            post_slack(webhook, "\n".join(text_lines))
            print("[INFO] Slack に投稿しました。")


if __name__ == "__main__":
    main()
