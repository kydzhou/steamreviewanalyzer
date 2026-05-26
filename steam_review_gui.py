#!/usr/bin/env python3
"""Tkinter GUI for Steam review analysis."""

from __future__ import annotations

import os
import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from tkinter import BooleanVar, IntVar, StringVar, Tk, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from steam_review_analyzer import (
    DEFAULT_OPENAI_BASE_URL,
    fetch_reviews,
    llm_summary,
    local_summary,
    parse_app_id,
    write_jsonl,
)


CONFIG_DIR = Path(os.getenv("APPDATA") or Path.home()) / "SteamReviewAnalyzer"
CONFIG_FILE = CONFIG_DIR / "config.json"
SORT_LABELS = {
    "Steam 默认顺序": "steam",
    "点赞数从高到低": "votes_up_desc",
    "权重分数从高到低": "weighted_score_desc",
    "最新评价优先": "newest",
    "游玩时长从高到低": "playtime_desc",
}
SORT_VALUES_TO_LABELS = {value: label for label, value in SORT_LABELS.items()}


def load_config() -> dict[str, object]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict[str, object]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class SteamReviewApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("Steam 评价中文分析工具")
        self.root.geometry("980x720")
        self.root.minsize(860, 620)

        self.message_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.config = load_config()
        llm_config = self.config.get("llm")
        if not isinstance(llm_config, dict):
            llm_config = {}

        self.link_var = StringVar()
        self.max_reviews_var = IntVar(value=1000)
        self.sleep_var = StringVar(value="0.5")
        self.sort_var = StringVar(
            value=SORT_VALUES_TO_LABELS.get(str(self.config.get("sort_by") or "votes_up_desc"), "点赞数从高到低")
        )
        self.summary_output_var = StringVar(value=str(Path.cwd() / "steam_review_summary.md"))
        self.save_jsonl_var = BooleanVar(value=False)
        self.jsonl_output_var = StringVar(value=str(Path.cwd() / "steam_reviews.jsonl"))
        self.use_llm_var = BooleanVar(value=bool(llm_config.get("use_llm", False)))
        self.save_llm_config_var = BooleanVar(value=True)
        self.api_key_var = StringVar(value=str(llm_config.get("api_key") or os.getenv("OPENAI_API_KEY", "")))
        self.base_url_var = StringVar(value=str(llm_config.get("base_url") or os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL)))
        self.model_var = StringVar(value=str(llm_config.get("model") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")))
        self.llm_max_reviews_var = IntVar(value=int(llm_config.get("llm_max_reviews") or 1200))
        self.chunk_size_var = IntVar(value=int(llm_config.get("chunk_size") or 80))
        self.status_var = StringVar(value="请输入 Steam 游戏链接或 App ID。")

        self._build_ui()
        self._update_llm_state()
        self._poll_queue()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        form = ttk.Frame(self.root, padding=12)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(4, weight=1)

        ttk.Label(form, text="Steam 链接 / App ID").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.link_var).grid(row=0, column=1, columnspan=4, sticky="ew", pady=4)

        ttk.Label(form, text="抓取数量").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Spinbox(form, from_=0, to=10_000_000, textvariable=self.max_reviews_var, width=12).grid(
            row=1, column=1, sticky="w", pady=4
        )
        ttk.Label(form, text="0 表示尽量抓取全部").grid(row=1, column=2, sticky="w", padx=(8, 16), pady=4)
        ttk.Label(form, text="请求间隔秒").grid(row=1, column=3, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.sleep_var, width=10).grid(row=1, column=4, sticky="w", pady=4)

        ttk.Label(form, text="排序方式").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(form, textvariable=self.sort_var, values=list(SORT_LABELS), state="readonly", width=20).grid(
            row=2, column=1, sticky="w", pady=4
        )

        ttk.Label(form, text="总结输出").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.summary_output_var).grid(row=3, column=1, columnspan=3, sticky="ew", pady=4)
        ttk.Button(form, text="选择", command=self._choose_summary_file).grid(row=3, column=4, sticky="w", padx=(8, 0), pady=4)

        ttk.Checkbutton(form, text="保存评价明细 JSONL", variable=self.save_jsonl_var, command=self._update_jsonl_state).grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.jsonl_entry = ttk.Entry(form, textvariable=self.jsonl_output_var, state="disabled")
        self.jsonl_entry.grid(row=4, column=1, columnspan=3, sticky="ew", pady=4)
        self.jsonl_button = ttk.Button(form, text="选择", command=self._choose_jsonl_file, state="disabled")
        self.jsonl_button.grid(row=4, column=4, sticky="w", padx=(8, 0), pady=4)

        llm_box = ttk.LabelFrame(form, text="LLM 总结（可选）", padding=8)
        llm_box.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(8, 4))
        llm_box.columnconfigure(1, weight=1)
        llm_box.columnconfigure(3, weight=1)

        ttk.Checkbutton(llm_box, text="启用 LLM", variable=self.use_llm_var, command=self._update_llm_state).grid(
            row=0, column=0, sticky="w", pady=4
        )
        ttk.Checkbutton(llm_box, text="保存 LLM 参数（包含 API Key，本机明文）", variable=self.save_llm_config_var).grid(
            row=0, column=1, columnspan=3, sticky="w", pady=4
        )
        ttk.Label(llm_box, text="API Key").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.api_key_entry = ttk.Entry(llm_box, textvariable=self.api_key_var, show="*", state="disabled")
        self.api_key_entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(llm_box, text="Base URL").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.base_url_entry = ttk.Entry(llm_box, textvariable=self.base_url_var, state="disabled")
        self.base_url_entry.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(llm_box, text="模型").grid(row=2, column=2, sticky="e", padx=(12, 8), pady=4)
        self.model_entry = ttk.Entry(llm_box, textvariable=self.model_var, state="disabled")
        self.model_entry.grid(row=2, column=3, sticky="ew", pady=4)

        ttk.Label(llm_box, text="送入 LLM 条数").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        self.llm_max_spin = ttk.Spinbox(
            llm_box, from_=0, to=1_000_000, textvariable=self.llm_max_reviews_var, width=12, state="disabled"
        )
        self.llm_max_spin.grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(llm_box, text="每批条数").grid(row=3, column=2, sticky="e", padx=(12, 8), pady=4)
        self.chunk_spin = ttk.Spinbox(llm_box, from_=1, to=1000, textvariable=self.chunk_size_var, width=12, state="disabled")
        self.chunk_spin.grid(row=3, column=3, sticky="w", pady=4)

        actions = ttk.Frame(form)
        actions.grid(row=6, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)
        self.run_button = ttk.Button(actions, text="开始分析", command=self._start)
        self.run_button.grid(row=0, column=1, sticky="e")
        ttk.Button(actions, text="打开总结文件", command=self._open_summary_file).grid(row=0, column=2, sticky="e", padx=(8, 0))

        self.output_text = ScrolledText(self.root, wrap="word", font=("Microsoft YaHei UI", 10))
        self.output_text.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        self.output_text.insert("1.0", "分析结果会显示在这里。\n")

        status = ttk.Label(self.root, textvariable=self.status_var, anchor="w", padding=(12, 6))
        status.grid(row=2, column=0, sticky="ew")

    def _update_jsonl_state(self) -> None:
        state = "normal" if self.save_jsonl_var.get() else "disabled"
        self.jsonl_entry.configure(state=state)
        self.jsonl_button.configure(state=state)

    def _update_llm_state(self) -> None:
        state = "normal" if self.use_llm_var.get() else "disabled"
        for widget in (self.api_key_entry, self.base_url_entry, self.model_entry, self.llm_max_spin, self.chunk_spin):
            widget.configure(state=state)

    def _choose_summary_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="选择总结输出文件",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.summary_output_var.set(path)

    def _choose_jsonl_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="选择评价明细文件",
            defaultextension=".jsonl",
            filetypes=[("JSON Lines", "*.jsonl"), ("All files", "*.*")],
        )
        if path:
            self.jsonl_output_var.set(path)

    def _open_summary_file(self) -> None:
        path = Path(self.summary_output_var.get()).expanduser()
        if not path.exists():
            messagebox.showinfo("提示", "总结文件还不存在。")
            return
        os.startfile(path)  # type: ignore[attr-defined]

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "分析正在运行中。")
            return

        try:
            options = self._read_options()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", "正在分析，请稍候...\n")
        self.status_var.set("正在启动分析...")
        self.run_button.configure(state="disabled")

        self.worker = threading.Thread(target=self._run_analysis, args=(options,), daemon=True)
        self.worker.start()

    def _read_options(self) -> dict[str, object]:
        link = self.link_var.get().strip()
        if not link:
            raise ValueError("请输入 Steam 游戏链接或 App ID。")

        summary_output = self.summary_output_var.get().strip()
        if not summary_output:
            raise ValueError("请选择总结输出文件。")

        try:
            sleep_seconds = float(self.sleep_var.get())
        except ValueError as exc:
            raise ValueError("请求间隔秒必须是数字。") from exc
        if sleep_seconds < 0:
            raise ValueError("请求间隔秒不能小于 0。")

        max_reviews = int(self.max_reviews_var.get())
        if max_reviews < 0:
            raise ValueError("抓取数量不能小于 0。")

        use_llm = self.use_llm_var.get()
        api_key = self.api_key_var.get().strip()
        if use_llm and not api_key:
            raise ValueError("启用 LLM 时需要填写 API Key。")

        return {
            "link": link,
            "max_reviews": max_reviews,
            "sleep_seconds": sleep_seconds,
            "sort_by": SORT_LABELS.get(self.sort_var.get(), "votes_up_desc"),
            "summary_output": summary_output,
            "save_jsonl": self.save_jsonl_var.get(),
            "jsonl_output": self.jsonl_output_var.get().strip(),
            "use_llm": use_llm,
            "save_llm_config": self.save_llm_config_var.get(),
            "api_key": api_key,
            "base_url": self.base_url_var.get().strip() or DEFAULT_OPENAI_BASE_URL,
            "model": self.model_var.get().strip() or "gpt-4o-mini",
            "llm_max_reviews": int(self.llm_max_reviews_var.get()),
            "chunk_size": int(self.chunk_size_var.get()),
        }

    def _run_analysis(self, options: dict[str, object]) -> None:
        try:
            app_id = parse_app_id(str(options["link"]))
            self._save_current_config(options)
            self._send_status(f"识别到 App ID: {app_id}，开始抓取评价...")
            reviews, query_summary = fetch_reviews(
                app_id,
                max_reviews=int(options["max_reviews"]),
                sleep_seconds=float(options["sleep_seconds"]),
                verbose=False,
                sort_by=str(options["sort_by"]),
                progress_callback=self._send_status,
            )
            if not reviews:
                raise RuntimeError("没有抓取到评价。请确认链接/app id 是否正确，或该游戏是否有公开评价。")

            if bool(options["save_jsonl"]):
                jsonl_output = str(options["jsonl_output"])
                if not jsonl_output:
                    raise RuntimeError("已勾选保存 JSONL，但未选择输出文件。")
                write_jsonl(jsonl_output, reviews)
                self._send_status(f"评价明细已保存：{jsonl_output}")

            if bool(options["use_llm"]):
                self._send_status("开始使用 LLM 生成中文总结...")
                summary = llm_summary(
                    app_id,
                    reviews,
                    query_summary,
                    model=str(options["model"]),
                    api_key=str(options["api_key"]),
                    base_url=str(options["base_url"]),
                    chunk_size=int(options["chunk_size"]),
                    max_llm_reviews=int(options["llm_max_reviews"]),
                    max_review_chars=1200,
                    verbose=False,
                    progress_callback=self._send_status,
                )
            else:
                self._send_status("生成本地统计版中文总结...")
                summary = local_summary(app_id, reviews, query_summary)

            summary_output = str(options["summary_output"])
            header = (
                f"<!-- Generated by steam_review_gui.py at "
                f"{datetime.now(timezone.utc).isoformat()} -->\n\n"
            )
            Path(summary_output).write_text(header + summary + "\n", encoding="utf-8")
            self.message_queue.put(("done", summary))
            self._send_status(f"完成，已保存：{summary_output}")
        except Exception as exc:
            self.message_queue.put(("error", str(exc)))

    def _save_current_config(self, options: dict[str, object]) -> None:
        data: dict[str, object] = {"sort_by": options["sort_by"]}
        if bool(options["save_llm_config"]):
            data["llm"] = {
                "use_llm": options["use_llm"],
                "api_key": options["api_key"],
                "base_url": options["base_url"],
                "model": options["model"],
                "llm_max_reviews": options["llm_max_reviews"],
                "chunk_size": options["chunk_size"],
            }
        elif isinstance(self.config.get("llm"), dict):
            data["llm"] = self.config["llm"]
        save_config(data)

    def _send_status(self, message: str) -> None:
        self.message_queue.put(("status", message))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, message = self.message_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(message)
                elif kind == "done":
                    self.output_text.delete("1.0", "end")
                    self.output_text.insert("1.0", message)
                    self.run_button.configure(state="normal")
                    messagebox.showinfo("完成", "分析完成，中文总结已生成。")
                elif kind == "error":
                    self.status_var.set("分析失败。")
                    self.run_button.configure(state="normal")
                    messagebox.showerror("分析失败", message)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_queue)


def main() -> None:
    root = Tk()
    SteamReviewApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
