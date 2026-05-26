#!/usr/bin/env python3
"""Analyze all-language Steam reviews and summarize pros/cons in Chinese."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


STEAM_REVIEWS_URL = "https://store.steampowered.com/appreviews/{app_id}"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
REVIEW_SORT_CHOICES = (
    "steam",
    "votes_up_desc",
    "weighted_score_desc",
    "newest",
    "playtime_desc",
)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


@dataclass
class Review:
    review_id: str
    language: str
    voted_up: bool
    text: str
    timestamp_created: int | None
    votes_up: int
    weighted_vote_score: float
    playtime_forever_minutes: int | None


def parse_app_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return value

    match = re.search(r"(?:store\.steampowered\.com/)?app/(\d+)", value)
    if match:
        return match.group(1)

    raise ValueError("无法从输入中识别 Steam app id。请传入商店链接或纯数字 app id。")


def http_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
    retries: int = 3,
) -> dict[str, Any]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data = None
    request_headers = {
        "User-Agent": "SteamReviewAnalyzer/1.0",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, data=data, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return json.loads(response.read().decode(charset))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == retries - 1:
                break
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"HTTP 请求失败: {last_error}")


def normalize_review(raw: dict[str, Any]) -> Review:
    author = raw.get("author") or {}
    return Review(
        review_id=str(raw.get("recommendationid") or ""),
        language=str(raw.get("language") or "unknown"),
        voted_up=bool(raw.get("voted_up")),
        text=str(raw.get("review") or "").strip(),
        timestamp_created=raw.get("timestamp_created"),
        votes_up=int(raw.get("votes_up") or 0),
        weighted_vote_score=float(raw.get("weighted_vote_score") or 0.0),
        playtime_forever_minutes=author.get("playtime_forever"),
    )


def sort_reviews(reviews: list[Review], sort_by: str) -> list[Review]:
    if sort_by == "steam":
        return reviews
    if sort_by == "votes_up_desc":
        return sorted(reviews, key=lambda review: review.votes_up, reverse=True)
    if sort_by == "weighted_score_desc":
        return sorted(reviews, key=lambda review: review.weighted_vote_score, reverse=True)
    if sort_by == "newest":
        return sorted(reviews, key=lambda review: review.timestamp_created or 0, reverse=True)
    if sort_by == "playtime_desc":
        return sorted(reviews, key=lambda review: review.playtime_forever_minutes or 0, reverse=True)
    raise ValueError(f"未知排序方式：{sort_by}")


def fetch_reviews(
    app_id: str,
    *,
    max_reviews: int,
    sleep_seconds: float,
    verbose: bool,
    sort_by: str = "steam",
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[list[Review], dict[str, Any]]:
    cursor = "*"
    seen_cursors: set[str] = set()
    reviews: list[Review] = []
    query_summary: dict[str, Any] = {}

    while True:
        page_size = 100
        if max_reviews > 0:
            page_size = max(1, min(page_size, max_reviews - len(reviews)))
        params = {
            "json": 1,
            "filter": "all",
            "language": "all",
            "purchase_type": "all",
            "num_per_page": page_size,
            "cursor": cursor,
        }
        data = http_json(STEAM_REVIEWS_URL.format(app_id=app_id), params=params)
        if not data.get("success"):
            raise RuntimeError(f"Steam API 返回失败: {data}")

        query_summary = data.get("query_summary") or query_summary
        page_reviews = [normalize_review(item) for item in data.get("reviews", [])]
        reviews.extend(page_reviews)

        if verbose:
            total_hint = query_summary.get("total_reviews")
            total_text = f"/{total_hint}" if total_hint else ""
            print(f"已抓取 {len(reviews)}{total_text} 条评价", file=sys.stderr)
        if progress_callback:
            total_hint = query_summary.get("total_reviews")
            total_text = f"/{total_hint}" if total_hint else ""
            progress_callback(f"已抓取 {len(reviews)}{total_text} 条评价")

        if max_reviews > 0 and len(reviews) >= max_reviews:
            reviews = reviews[:max_reviews]
            break
        if not page_reviews:
            break

        next_cursor = str(data.get("cursor") or "")
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        cursor = next_cursor
        time.sleep(sleep_seconds)

    return sort_reviews(reviews, sort_by), query_summary


def write_jsonl(path: str, reviews: list[Review]) -> None:
    with open(path, "w", encoding="utf-8") as file:
        for review in reviews:
            file.write(json.dumps(review.__dict__, ensure_ascii=False) + "\n")


def pct(value: int, total: int) -> str:
    if total == 0:
        return "0.0%"
    return f"{value / total * 100:.1f}%"


def minutes_to_hours(minutes: int | None) -> float | None:
    if minutes is None:
        return None
    return minutes / 60


ASPECT_KEYWORDS = {
    "画面 / 美术": [
        "graphics",
        "visual",
        "art",
        "beautiful",
        "画面",
        "美术",
        "建模",
        "場景",
        "グラフィック",
    ],
    "剧情 / 叙事": [
        "story",
        "plot",
        "writing",
        "narrative",
        "剧情",
        "故事",
        "文本",
        "ストーリー",
    ],
    "玩法 / 手感": [
        "gameplay",
        "combat",
        "controls",
        "mechanic",
        "fun",
        "玩法",
        "战斗",
        "手感",
        "操作",
        "ゲームプレイ",
    ],
    "性能 / 优化": [
        "performance",
        "fps",
        "stutter",
        "crash",
        "bug",
        "optimization",
        "性能",
        "优化",
        "卡顿",
        "闪退",
        "崩溃",
        "バグ",
    ],
    "内容量 / 重复度": [
        "content",
        "repetitive",
        "grind",
        "short",
        "内容",
        "重复",
        "肝",
        "时长",
        "ボリューム",
    ],
    "价格 / 性价比": [
        "price",
        "worth",
        "sale",
        "expensive",
        "cheap",
        "价格",
        "性价比",
        "打折",
        "値段",
    ],
    "联机 / 社区": [
        "multiplayer",
        "server",
        "online",
        "coop",
        "matchmaking",
        "联机",
        "服务器",
        "匹配",
        "マルチ",
    ],
}


def local_summary(app_id: str, reviews: list[Review], query_summary: dict[str, Any]) -> str:
    total = len(reviews)
    positive = sum(1 for item in reviews if item.voted_up)
    negative = total - positive
    languages = Counter(item.language for item in reviews)
    hours = [hours for item in reviews if (hours := minutes_to_hours(item.playtime_forever_minutes)) is not None]

    positive_aspects: Counter[str] = Counter()
    negative_aspects: Counter[str] = Counter()
    for review in reviews:
        text = review.text.lower()
        target = positive_aspects if review.voted_up else negative_aspects
        for aspect, keywords in ASPECT_KEYWORDS.items():
            if any(keyword.lower() in text for keyword in keywords):
                target[aspect] += 1

    lines = [
        f"# Steam 评价中文总结 - App {app_id}",
        "",
        "## 数据概览",
        f"- 本次抓取评价数：{total}",
        f"- 好评：{positive}（{pct(positive, total)}）",
        f"- 差评：{negative}（{pct(negative, total)}）",
    ]
    if query_summary.get("total_reviews"):
        lines.append(f"- Steam 返回的总评价提示：{query_summary.get('total_reviews')}")
    if languages:
        language_text = "、".join(f"{lang} {count}" for lang, count in languages.most_common(8))
        lines.append(f"- 主要语言分布：{language_text}")
    if hours:
        lines.append(f"- 评价者游玩时长中位数：{statistics.median(hours):.1f} 小时")

    lines.extend(
        [
            "",
            "## 优点",
            *aspect_lines(positive_aspects, total, positive=True),
            "",
            "## 缺点",
            *aspect_lines(negative_aspects, total, positive=False),
            "",
            "## 说明",
            "- 当前未配置 LLM API Key，因此这是基于评价倾向、语言分布、游玩时长和多语言关键词的本地统计总结。",
            "- 若要得到更自然、能跨语言归纳具体观点的中文总结，请配置 `OPENAI_API_KEY` 后使用 `--llm`。",
        ]
    )
    return "\n".join(lines)


def aspect_lines(counter: Counter[str], total: int, *, positive: bool) -> list[str]:
    if not counter:
        text = "未从关键词统计中提取到足够集中的优点。" if positive else "未从关键词统计中提取到足够集中的缺点。"
        return [f"- {text}"]
    return [
        f"- {aspect}：约 {count} 条评价提及，占本次样本 {pct(count, total)}。"
        for aspect, count in counter.most_common(6)
    ]


def review_weight(review: Review) -> float:
    return review.weighted_vote_score + math.log1p(review.votes_up)


def chunked(items: list[Review], size: int) -> list[list[Review]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def compact_review(review: Review, max_chars: int) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", review.text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "..."
    return {
        "language": review.language,
        "recommended": review.voted_up,
        "votes_up": review.votes_up,
        "playtime_hours": minutes_to_hours(review.playtime_forever_minutes),
        "text": text,
    }


def prepare_llm_reviews(
    reviews: list[Review],
    *,
    max_reviews: int,
) -> list[Review]:
    if max_reviews <= 0 or len(reviews) <= max_reviews:
        return reviews

    positives = [item for item in reviews if item.voted_up]
    negatives = [item for item in reviews if not item.voted_up]
    positives.sort(key=review_weight, reverse=True)
    negatives.sort(key=review_weight, reverse=True)

    half = max_reviews // 2
    selected = positives[:half] + negatives[: max_reviews - half]
    remaining = [item for item in reviews if item not in selected]
    random.Random(42).shuffle(remaining)
    selected.extend(remaining[: max_reviews - len(selected)])
    return selected[:max_reviews]


def call_openai_chat(
    messages: list[dict[str, str]],
    *,
    model: str,
    api_key: str,
    base_url: str,
    timeout: int,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    data = http_json(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        body={
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        },
        timeout=timeout,
        retries=2,
    )
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM 响应格式异常: {data}") from exc


def llm_summary(
    app_id: str,
    reviews: list[Review],
    query_summary: dict[str, Any],
    *,
    model: str,
    api_key: str,
    base_url: str,
    chunk_size: int,
    max_llm_reviews: int,
    max_review_chars: int,
    verbose: bool,
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    selected_reviews = prepare_llm_reviews(reviews, max_reviews=max_llm_reviews)
    chunks = chunked(selected_reviews, chunk_size)
    partials: list[str] = []

    system = (
        "你是严谨的中文游戏评价分析助手。"
        "你会阅读 Steam 多语言用户评价，提炼可验证的共同观点，避免臆测。"
        "输出必须使用简体中文。"
    )

    for index, group in enumerate(chunks, start=1):
        if verbose:
            print(f"正在让 LLM 总结第 {index}/{len(chunks)} 批评价", file=sys.stderr)
        if progress_callback:
            progress_callback(f"正在让 LLM 总结第 {index}/{len(chunks)} 批评价")
        review_payload = [compact_review(item, max_review_chars) for item in group]
        user = (
            "请总结这批 Steam 用户评价中反复出现的优点和缺点。"
            "所有语言都要纳入判断。请按 JSON 返回："
            '{"pros":["..."],"cons":["..."],"notable_quotes_or_patterns":["..."]}\n'
            f"App ID: {app_id}\n"
            f"评价 JSON: {json.dumps(review_payload, ensure_ascii=False)}"
        )
        partials.append(
            call_openai_chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=model,
                api_key=api_key,
                base_url=base_url,
                timeout=120,
            )
        )

    total = len(reviews)
    positive = sum(1 for item in reviews if item.voted_up)
    language_text = "、".join(
        f"{lang} {count}" for lang, count in Counter(item.language for item in reviews).most_common(10)
    )
    stats = {
        "app_id": app_id,
        "fetched_reviews": total,
        "positive_reviews": positive,
        "negative_reviews": total - positive,
        "positive_rate": pct(positive, total),
        "steam_total_reviews_hint": query_summary.get("total_reviews"),
        "top_languages": language_text,
        "llm_review_count": len(selected_reviews),
    }

    final_user = (
        "下面是多批 Steam 评价的局部总结和整体统计。"
        "请综合成一份中文报告，重点回答这个游戏被玩家认为的优点和缺点。"
        "要求：\n"
        "1. 先给一句总体判断。\n"
        "2. 分别列出 5-8 条优点和 5-8 条缺点，每条说明依据。\n"
        "3. 单独说明争议点、适合人群、不适合人群。\n"
        "4. 不要编造游戏名称或事实；无法确认就写 App ID。\n"
        "5. 如果输入评价没有覆盖所有 Steam 总评价，请明确说明 LLM 实际分析的评价数。\n\n"
        f"整体统计：{json.dumps(stats, ensure_ascii=False)}\n\n"
        f"局部总结：{json.dumps(partials, ensure_ascii=False)}"
    )
    return call_openai_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": final_user}],
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout=180,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="输入 Steam 游戏链接或 app id，抓取所有语言用户评价并输出中文优缺点总结。"
    )
    parser.add_argument("steam_link_or_app_id", help="Steam 商店链接，例如 https://store.steampowered.com/app/730/CounterStrike_2/")
    parser.add_argument("--max-reviews", type=int, default=0, help="最多抓取多少条评价；0 表示尽量抓取全部。")
    parser.add_argument("--sleep", type=float, default=0.5, help="Steam 分页请求之间的等待秒数。")
    parser.add_argument("--output-jsonl", help="保存原始清洗后评价到 JSONL 文件。")
    parser.add_argument("--summary-output", default="steam_review_summary.md", help="中文总结输出文件。")
    parser.add_argument("--llm", action="store_true", help="使用 OpenAI 兼容接口做跨语言中文总结。")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), help="LLM 模型名。")
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL), help="OpenAI 兼容 API 地址。")
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY"), help="OpenAI 兼容 API Key；也可用 OPENAI_API_KEY 环境变量。")
    parser.add_argument("--llm-max-reviews", type=int, default=1200, help="送入 LLM 的最多评价数；0 表示全部送入，会产生较高成本。")
    parser.add_argument("--chunk-size", type=int, default=80, help="每批送入 LLM 的评价条数。")
    parser.add_argument("--max-review-chars", type=int, default=1200, help="单条评价送入 LLM 前保留的最大字符数。")
    parser.add_argument(
        "--sort-by",
        choices=REVIEW_SORT_CHOICES,
        default="steam",
        help="评价抓取后排序方式；votes_up_desc 表示按点赞数从高到低。",
    )
    parser.add_argument("--verbose", action="store_true", help="打印抓取进度。")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_arg_parser().parse_args(argv)
    try:
        app_id = parse_app_id(args.steam_link_or_app_id)
        reviews, query_summary = fetch_reviews(
            app_id,
            max_reviews=args.max_reviews,
            sleep_seconds=args.sleep,
            verbose=args.verbose,
            sort_by=args.sort_by,
        )
        if not reviews:
            print("没有抓取到评价。请确认链接/app id 是否正确，或该游戏是否有公开评价。", file=sys.stderr)
            return 2

        if args.output_jsonl:
            write_jsonl(args.output_jsonl, reviews)

        if args.llm:
            if not args.api_key:
                raise ValueError("使用 --llm 需要提供 --api-key 或设置 OPENAI_API_KEY。")
            summary = llm_summary(
                app_id,
                reviews,
                query_summary,
                model=args.model,
                api_key=args.api_key,
                base_url=args.base_url,
                chunk_size=args.chunk_size,
                max_llm_reviews=args.llm_max_reviews,
                max_review_chars=args.max_review_chars,
                verbose=args.verbose,
            )
        else:
            summary = local_summary(app_id, reviews, query_summary)

        header = (
            f"<!-- Generated by steam_review_analyzer.py at "
            f"{datetime.now(timezone.utc).isoformat()} -->\n\n"
        )
        with open(args.summary_output, "w", encoding="utf-8") as file:
            file.write(header + summary + "\n")
        print(f"已生成总结：{args.summary_output}")
        return 0
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
