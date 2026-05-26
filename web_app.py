#!/usr/bin/env python3
"""FastAPI web app for Steam review analysis."""

from __future__ import annotations

import os
import threading
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
    fetch_reviews,
    llm_summary,
    local_summary,
    parse_app_id,
)


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()


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


TASKS: dict[str, AnalysisTask] = {}
TASKS_LOCK = threading.Lock()

app = FastAPI(title="Steam Review Analyzer")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "password_required": bool(APP_PASSWORD),
        "llm_available": True,
        "default_base_url": DEFAULT_OPENAI_BASE_URL,
        "default_model": "gpt-4o-mini",
        "sort_choices": REVIEW_SORT_CHOICES,
    }


@app.post("/api/analyze")
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


@app.get("/api/tasks/{task_id}")
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
