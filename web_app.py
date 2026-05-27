#!/usr/bin/env python3
"""FastAPI web app for Steam review analysis."""

from __future__ import annotations

import os
import json
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from steam_review_analyzer import (
    DEFAULT_OPENAI_BASE_URL,
    REVIEW_SORT_CHOICES,
    call_openai_chat,
    fetch_reviews,
    llm_summary,
    local_summary,
    parse_app_id,
)


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
STEAM_TAGS_URL = "https://store.steampowered.com/tagdata/populartags/english"
STEAM_TAGS_ZH_URL = "https://store.steampowered.com/tagdata/populartags/schinese"
STEAM_QUERY_URL = "https://api.steampowered.com/IStoreQueryService/Query/v1/"
STEAMSPY_APP_URL = "https://steamspy.com/api.php"


@dataclass
class AnalysisTask:
    id: str
    status: str = "queued"
    message: str = "等待开始..."
    summary: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def update(self, *, status: str | None = None, message: str | None = None) -> None:
        if status is not None:
            self.status = status
        if message is not None:
            self.message = message
        self.updated_at = datetime.now(timezone.utc).isoformat()


class AnalyzeRequest(BaseModel):
    steam_link_or_app_id: str = Field(min_length=1)
    max_reviews: int = Field(default=1000, ge=0, le=10_000_000)
    sleep_seconds: float = Field(default=0.5, ge=0, le=10)
    sort_by: str = Field(default="votes_up_desc")
    use_llm: bool = False
    llm_api_key: str = ""
    llm_base_url: str = Field(default=DEFAULT_OPENAI_BASE_URL)
    llm_model: str = Field(default="gpt-4o-mini")
    llm_max_reviews: int = Field(default=1200, ge=0, le=1_000_000)
    chunk_size: int = Field(default=80, ge=1, le=1000)
    max_review_chars: int = Field(default=1200, ge=100, le=5000)


class MarketAnalyzeRequest(BaseModel):
    include_tag_ids: list[int] = Field(default_factory=list)
    exclude_tag_ids: list[int] = Field(default_factory=list)
    include_tag_names: list[str] = Field(default_factory=list)
    max_results: int = Field(default=500, ge=1)
    country_code: str = Field(default="US", min_length=2, max_length=2)
    language: str = Field(default="english", min_length=2, max_length=32)
    include_steamspy: bool = False
    include_released: bool = True
    include_unreleased: bool = False


class MarketLlmAnalyzeRequest(BaseModel):
    apps: list[dict[str, Any]] = Field(default_factory=list)
    include_tag_names: list[str] = Field(default_factory=list)
    exclude_tag_names: list[str] = Field(default_factory=list)
    include_released: bool = True
    include_unreleased: bool = False
    llm_api_key: str = ""
    llm_base_url: str = Field(default=DEFAULT_OPENAI_BASE_URL)
    llm_model: str = Field(default="gpt-4o-mini")


TASKS: dict[str, AnalysisTask] = {}
TASKS_LOCK = threading.Lock()

app = FastAPI(title="Steam Review Analyzer")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/steam-review/static", StaticFiles(directory=STATIC_DIR), name="steam-review-static")


def require_password(x_app_password: str | None) -> None:
    if APP_PASSWORD and x_app_password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="访问密码错误")


def get_task(task_id: str) -> AnalysisTask:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def set_task(task: AnalysisTask) -> None:
    with TASKS_LOCK:
        TASKS[task.id] = task


def task_payload(task: AnalysisTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "status": task.status,
        "message": task.message,
        "summary": task.summary,
        "error": task.error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "nav.html").read_text(encoding="utf-8")


@app.get("/steam-review/", response_class=HTMLResponse)
def steam_review_index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/steam-market/", response_class=HTMLResponse)
def steam_market_index() -> str:
    return (STATIC_DIR / "steam-market.html").read_text(encoding="utf-8")


@app.get("/steam-market/api/tags")
def steam_market_tags() -> dict[str, Any]:
    english_tags = fetch_steam_tags(STEAM_TAGS_URL)
    chinese_tags = fetch_steam_tags(STEAM_TAGS_ZH_URL)
    chinese_by_id = {tag["id"]: tag["name"] for tag in chinese_tags}
    merged = [
        {
            "id": tag["id"],
            "name": tag["name"],
            "nameZh": chinese_by_id.get(tag["id"], ""),
        }
        for tag in english_tags
    ]
    merged.sort(key=lambda tag: tag["name"].lower())
    return {
        "source": {"english": STEAM_TAGS_URL, "schinese": STEAM_TAGS_ZH_URL},
        "count": len(merged),
        "tags": merged,
    }


def fetch_steam_tags(url: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SteamMarketAnalyzer/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        tags = json.loads(response.read().decode("utf-8"))
    return [
        {"id": int(item["tagid"]), "name": str(item["name"])}
        for item in tags
        if item.get("tagid") and item.get("name")
    ]


@app.post("/steam-market/api/analyze")
def steam_market_analyze(request: MarketAnalyzeRequest) -> dict[str, Any]:
    if not request.include_tag_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个包含 tag")
    if not request.include_released and not request.include_unreleased:
        raise HTTPException(status_code=400, detail="请至少选择已发售或未发售其中一种")
    apps, total = fetch_market_apps(request)
    if request.include_steamspy:
        enrich_with_steamspy(apps)
    return {
        "source": {
            "steam": STEAM_QUERY_URL,
            "steamspy": STEAMSPY_APP_URL if request.include_steamspy else None,
        },
        "total_matching_records": total,
        "analyzed_count": len(apps),
        "apps": apps,
    }


@app.post("/steam-market/api/count")
def steam_market_count(request: MarketAnalyzeRequest) -> dict[str, Any]:
    if not request.include_tag_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个包含 tag")
    if not request.include_released and not request.include_unreleased:
        raise HTTPException(status_code=400, detail="请至少选择已发售或未发售其中一种")
    response = fetch_market_page(request, start=0, count=1).get("response", {})
    metadata = response.get("metadata", {})
    total = int(metadata.get("total_matching_records") or 0)
    return {"total_matching_records": total}


@app.post("/steam-market/api/llm-analysis")
def steam_market_llm_analysis(request: MarketLlmAnalyzeRequest) -> dict[str, str]:
    if not request.apps:
        raise HTTPException(status_code=400, detail="请先完成一次市场分析")
    if not request.llm_api_key.strip():
        raise HTTPException(status_code=400, detail="请输入 LLM API Key")

    prompt_payload = build_market_llm_payload(request)
    system = (
        "你是资深中文游戏市场分析师。"
        "你会基于用户提供的 Steam 官方数据做赛道分析，避免臆测，不把样本外事实当成结论。"
        "输出必须使用简体中文，结构清晰，重点给出可执行的市场判断。"
    )
    user = (
        "请基于以下 Steam tag 赛道分析数据，输出中文市场分析报告。\n"
        "请包含：1) 赛道概览；2) 价格带与评论量结构；3) 头部产品特征；"
        "4) 竞争强度判断；5) 新产品切入建议；6) 需要谨慎解读的数据限制。\n\n"
        f"数据 JSON：{json.dumps(prompt_payload, ensure_ascii=False)}"
    )
    analysis = call_openai_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=request.llm_model.strip() or "gpt-4o-mini",
        api_key=request.llm_api_key.strip(),
        base_url=request.llm_base_url.strip() or DEFAULT_OPENAI_BASE_URL,
        timeout=180,
    )
    return {"analysis": analysis}


@app.get("/steam-review/api/config")
def config() -> dict[str, Any]:
    return {
        "password_required": bool(APP_PASSWORD),
        "llm_available": True,
        "default_base_url": DEFAULT_OPENAI_BASE_URL,
        "default_model": "gpt-4o-mini",
        "sort_choices": REVIEW_SORT_CHOICES,
    }


@app.post("/steam-review/api/analyze")
def analyze(request: AnalyzeRequest, x_app_password: str | None = Header(default=None)) -> dict[str, str]:
    require_password(x_app_password)
    if request.sort_by not in REVIEW_SORT_CHOICES:
        raise HTTPException(status_code=400, detail=f"未知排序方式：{request.sort_by}")
    if request.use_llm and not request.llm_api_key.strip():
        raise HTTPException(status_code=400, detail="启用 LLM 时需要填写 API Key")

    task = AnalysisTask(id=str(uuid.uuid4()))
    set_task(task)
    worker = threading.Thread(target=run_analysis, args=(task.id, request), daemon=True)
    worker.start()
    return {"task_id": task.id}


@app.get("/steam-review/api/tasks/{task_id}")
def task_status(task_id: str, x_app_password: str | None = Header(default=None)) -> dict[str, Any]:
    require_password(x_app_password)
    return task_payload(get_task(task_id))


def run_analysis(task_id: str, request: AnalyzeRequest) -> None:
    task = get_task(task_id)

    def progress(message: str) -> None:
        task.update(status="running", message=message)

    try:
        task.update(status="running", message="正在解析 Steam 链接...")
        app_id = parse_app_id(request.steam_link_or_app_id)
        progress(f"识别到 App ID: {app_id}，开始抓取评价...")
        reviews, query_summary = fetch_reviews(
            app_id,
            max_reviews=request.max_reviews,
            sleep_seconds=request.sleep_seconds,
            verbose=False,
            sort_by=request.sort_by,
            progress_callback=progress,
        )
        if not reviews:
            raise RuntimeError("没有抓取到评价。请确认链接/app id 是否正确，或该游戏是否有公开评价。")

        if request.use_llm:
            progress("开始使用 LLM 生成中文总结...")
            summary = llm_summary(
                app_id,
                reviews,
                query_summary,
                model=request.llm_model.strip() or "gpt-4o-mini",
                api_key=request.llm_api_key.strip(),
                base_url=request.llm_base_url.strip() or DEFAULT_OPENAI_BASE_URL,
                chunk_size=request.chunk_size,
                max_llm_reviews=request.llm_max_reviews,
                max_review_chars=request.max_review_chars,
                verbose=False,
                progress_callback=progress,
            )
        else:
            progress("生成本地统计版中文总结...")
            summary = local_summary(app_id, reviews, query_summary)

        task.summary = summary
        task.update(status="done", message="分析完成")
    except Exception as exc:
        task.error = str(exc)
        task.update(status="error", message="分析失败")


def fetch_market_apps(request: MarketAnalyzeRequest) -> tuple[list[dict[str, Any]], int]:
    apps: list[dict[str, Any]] = []
    seen_appids: set[int] = set()
    total = 0
    start = 0
    while len(apps) < request.max_results:
        count = min(100, request.max_results - len(apps))
        response = fetch_market_page(request, start=start, count=count).get("response", {})
        metadata = response.get("metadata", {})
        total = int(metadata.get("total_matching_records") or total or 0)
        items = response.get("store_items") or []
        if not items:
            break
        normalized_items = [
            normalize_market_item(item, request.include_tag_names)
            for item in items
            if item.get("appid")
        ]
        for item in normalized_items:
            appid = parse_int(item.get("appid"))
            if appid is None or appid in seen_appids:
                continue
            if not market_item_matches_release_filter(item, request):
                continue
            seen_appids.add(appid)
            apps.append(item)
        start += len(items)
        if start >= total:
            break
    return apps[: request.max_results], total


def market_item_matches_release_filter(item: dict[str, Any], request: MarketAnalyzeRequest) -> bool:
    if item["isComingSoon"]:
        return request.include_unreleased
    return request.include_released


def build_market_llm_payload(request: MarketLlmAnalyzeRequest) -> dict[str, Any]:
    apps = request.apps
    released_label = []
    if request.include_released:
        released_label.append("已发售")
    if request.include_unreleased:
        released_label.append("未发售")
    review_values = [value for app_item in apps if (value := parse_int(app_item.get("reviewCount"))) is not None]
    price_values = [
        float(value)
        for app_item in apps
        if isinstance((value := app_item.get("price")), int | float)
    ]
    top_apps = sorted(
        (compact_market_app(app_item) for app_item in apps if parse_int(app_item.get("reviewCount")) and parse_int(app_item.get("reviewCount")) > 10000),
        key=lambda item: item["reviewCount"] or 0,
        reverse=True,
    )[:300]
    return {
        "filters": {
            "includeTags": request.include_tag_names,
            "excludeTags": request.exclude_tag_names,
            "releaseStatus": released_label,
        },
        "summary": {
            "analyzedCount": len(apps),
            "freeCount": sum(1 for app_item in apps if app_item.get("price") == 0),
            "medianReviews": median_number(review_values),
            "medianPrice": median_number(price_values),
            "topGameCountOver10000Reviews": len(top_apps),
        },
        "reviewDistribution": build_distribution(apps, "reviewCount", REVIEW_BUCKETS),
        "priceDistribution": build_distribution(apps, "price", PRICE_BUCKETS),
        "positiveRateDistribution": build_distribution(apps, "percentPositive", POSITIVE_BUCKETS),
        "priceReviewMatrix": build_price_review_matrix(apps),
        "topAppsOver10000Reviews": top_apps,
    }


PRICE_BUCKETS = [
    ("$0", lambda value: value == 0),
    ("$0.01 - 9.99", lambda value: value > 0 and value <= 9.99),
    ("$10 - 19.99", lambda value: value >= 10 and value <= 19.99),
    ("$20 - 29.99", lambda value: value >= 20 and value <= 29.99),
    ("$30 - 39.99", lambda value: value >= 30 and value <= 39.99),
    ("$39.99+", lambda value: value >= 40),
]

REVIEW_BUCKETS = [
    ("0-999", lambda value: value is None or (value >= 0 and value <= 999)),
    ("1000-4999", lambda value: value is not None and value >= 1000 and value <= 4999),
    ("5000-9999", lambda value: value is not None and value >= 5000 and value <= 9999),
    ("10000+", lambda value: value is not None and value >= 10000),
]

POSITIVE_BUCKETS = [
    ("0-49%", lambda value: value is not None and value > 0 and value < 50),
    ("50-69%", lambda value: value is not None and value >= 50 and value <= 69),
    ("70-84%", lambda value: value is not None and value >= 70 and value <= 84),
    ("85-94%", lambda value: value is not None and value >= 85 and value <= 94),
    ("95%+", lambda value: value is not None and value >= 95),
]

MATRIX_REVIEW_BUCKETS = [
    ("< 1000", lambda value: value is not None and value < 1000),
    ("1000 - 4999", lambda value: value is not None and value >= 1000 and value <= 4999),
    ("5000 - 9999", lambda value: value is not None and value >= 5000 and value <= 9999),
    (">= 10000", lambda value: value is not None and value >= 10000),
]


def build_distribution(apps: list[dict[str, Any]], field: str, buckets: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    values = [normalize_number(app_item.get(field)) for app_item in apps]
    return [{"label": label, "count": sum(1 for value in values if test(value))} for label, test in buckets]


def build_price_review_matrix(apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matrix = []
    for price_label, price_test in PRICE_BUCKETS:
        column_apps = [
            app_item
            for app_item in apps
            if (price := normalize_number(app_item.get("price"))) is not None and price_test(price)
        ]
        rows = []
        for review_label, review_test in MATRIX_REVIEW_BUCKETS:
            count = sum(1 for app_item in column_apps if review_test(normalize_number(app_item.get("reviewCount"))))
            rows.append({
                "reviewRange": review_label,
                "count": count,
                "percentOfPriceBucket": round((count / len(column_apps)) * 100, 2) if column_apps else 0,
            })
        matrix.append({"priceRange": price_label, "total": len(column_apps), "rows": rows})
    return matrix


def compact_market_app(app_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "appid": app_item.get("appid"),
        "name": app_item.get("name"),
        "reviewCount": parse_int(app_item.get("reviewCount")),
        "percentPositive": parse_int(app_item.get("percentPositive")),
        "price": normalize_number(app_item.get("price")),
        "releaseDate": app_item.get("releaseDate") or "",
        "isComingSoon": bool(app_item.get("isComingSoon")),
        "steamUrl": app_item.get("steamUrl") or "",
    }


def median_number(values: list[int | float]) -> int | float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2


def normalize_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int | float):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_market_page(request: MarketAnalyzeRequest, *, start: int, count: int) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "tagids_must_match": [{"tagids": [str(tag_id)]} for tag_id in request.include_tag_ids],
        "type_filters": {"include_games": True},
    }
    if request.exclude_tag_ids:
        filters["tagids_exclude"] = request.exclude_tag_ids
    payload = {
        "query": {"start": start, "count": count, "filters": filters},
        "context": {
            "language": request.language,
            "country_code": request.country_code.upper(),
            "steam_realm": "1",
        },
        "data_request": {
            "include_basic_info": True,
            "include_reviews": True,
            "include_release": True,
            "include_assets": False,
        },
    }
    url = f"{STEAM_QUERY_URL}?{urllib.parse.urlencode({'input_json': json.dumps(payload)})}"
    http_request = urllib.request.Request(
        url,
        headers={"User-Agent": "SteamMarketAnalyzer/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(http_request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_market_item(item: dict[str, Any], selected_tags: list[str]) -> dict[str, Any]:
    reviews = (item.get("reviews") or {}).get("summary_filtered") or {}
    purchase = item.get("best_purchase_option") or {}
    release = item.get("release") or {}
    final_cents = parse_int(purchase.get("final_price_in_cents"))
    original_cents = parse_int(purchase.get("original_price_in_cents"))
    discount = parse_int(purchase.get("discount_pct"))
    if discount is None and original_cents and final_cents is not None and original_cents > 0:
        discount = round((1 - final_cents / original_cents) * 100)
    appid = item.get("appid")
    return {
        "appid": appid,
        "name": item.get("name") or "",
        "selectedTags": selected_tags,
        "reviewCount": parse_int(reviews.get("review_count")),
        "percentPositive": parse_int(reviews.get("percent_positive")),
        "reviewScoreLabel": reviews.get("review_score_label") or "",
        "price": 0 if item.get("is_free") else (final_cents / 100 if final_cents is not None else None),
        "formattedPrice": purchase.get("formatted_final_price") or ("Free" if item.get("is_free") else ""),
        "discount": discount,
        "releaseDate": release.get("steam_release_date") or release.get("custom_release_date_message") or "",
        "isComingSoon": bool(item.get("is_coming_soon") or release.get("is_coming_soon")),
        "steamUrl": f"https://store.steampowered.com/{item.get('store_url_path', f'app/{appid}')}",
        "steamdbUrl": f"https://steamdb.info/app/{appid}/",
    }


def enrich_with_steamspy(apps: list[dict[str, Any]]) -> None:
    for index, app_item in enumerate(apps):
        try:
            params = urllib.parse.urlencode({"request": "appdetails", "appid": app_item["appid"]})
            request = urllib.request.Request(
                f"{STEAMSPY_APP_URL}?{params}",
                headers={"User-Agent": "SteamMarketAnalyzer/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            app_item["steamspy"] = {
                "owners": data.get("owners") or "",
                "averageForever": parse_int(data.get("average_forever")),
                "medianForever": parse_int(data.get("median_forever")),
                "ccu": parse_int(data.get("ccu")),
                "positive": parse_int(data.get("positive")),
                "negative": parse_int(data.get("negative")),
                "tags": data.get("tags") or {},
            }
        except Exception as exc:
            app_item["steamspyError"] = str(exc)
        if index < len(apps) - 1:
            time.sleep(0.15)


def parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
