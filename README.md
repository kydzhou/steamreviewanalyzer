# Steam Review Analyzer

输入 Steam 游戏商店链接或 App ID，抓取该游戏所有语言的用户评价，并输出中文优点/缺点总结。

## 功能

- 自动从 Steam 链接解析 App ID。
- 使用 Steam 官方 `appreviews` 接口分页抓取评价，`language=all` 覆盖所有语言。
- 可保存清洗后的评价 JSONL，方便后续分析。
- 默认无需依赖，输出本地统计版中文总结。
- 配置 OpenAI 兼容 API 后，可跨语言归纳更自然的中文优缺点报告。

## Web 版快速开始

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

启动 Web 服务：

```powershell
python -m uvicorn web_app:app --host 127.0.0.1 --port 8000
```

浏览器打开：

```text
http://127.0.0.1:8000
```

如果要启用 LLM，在网页里手动填写 API Key。网页的所有表单配置都会自动缓存在当前浏览器本地；没有缓存时使用默认配置。

如果要给网页加访问密码，可以设置环境变量：

```powershell
$env:APP_PASSWORD="网页访问密码"
python -m uvicorn web_app:app --host 127.0.0.1 --port 8000
```

## 阿里云 ECS 部署

服务器建议使用 Ubuntu 22.04。进入服务器后：

```bash
apt update
apt install -y python3 python3-venv python3-pip git nginx
mkdir -p /opt/steam-review
cd /opt/steam-review
git clone https://github.com/kydzhou/steamreviewanalyzer.git .
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

创建环境变量文件：

```bash
nano /opt/steam-review/.env
```

示例：

```env
APP_PASSWORD=你的网页访问密码
```

创建 systemd 服务：

```bash
nano /etc/systemd/system/steam-review.service
```

内容：

```ini
[Unit]
Description=Steam Review Analyzer Web
After=network.target

[Service]
WorkingDirectory=/opt/steam-review
EnvironmentFile=-/opt/steam-review/.env
ExecStart=/opt/steam-review/.venv/bin/uvicorn web_app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
systemctl daemon-reload
systemctl enable steam-review
systemctl start steam-review
systemctl status steam-review
```

Nginx 反向代理：

```bash
nano /etc/nginx/sites-available/steam-review
```

内容：

```nginx
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

启用：

```bash
ln -s /etc/nginx/sites-available/steam-review /etc/nginx/sites-enabled/steam-review
nginx -t
systemctl reload nginx
```

然后访问 ECS 公网 IP。

## 桌面版快速开始

启动图形界面：

```powershell
python .\steam_review_gui.py
```

在界面里输入 Steam 链接或 App ID，设置抓取数量和排序方式，然后点击“开始分析”。默认排序是“点赞数从高到低”。

命令行用法：

```powershell
python .\steam_review_analyzer.py "https://store.steampowered.com/app/730/CounterStrike_2/" --verbose
```

运行结束后会生成：

- `steam_review_summary.md`：中文总结。

如需保存评价明细：

```powershell
python .\steam_review_analyzer.py "https://store.steampowered.com/app/730/CounterStrike_2/" --output-jsonl reviews.jsonl --verbose
```

## 使用 LLM 生成中文总结

本地统计总结无法真正翻译和理解所有语言的长文本。推荐配置 OpenAI 兼容接口：

在 GUI 中勾选“启用 LLM”，填写 API Key、Base URL 和模型名即可。GUI 默认会保存 LLM 参数到本机用户配置目录，下次打开自动读取；API Key 会以明文保存，请只在自己的电脑上使用。

```powershell
$env:OPENAI_API_KEY="你的 API Key"
python .\steam_review_analyzer.py "https://store.steampowered.com/app/730/CounterStrike_2/" --llm --verbose
```

如使用兼容服务：

```powershell
$env:OPENAI_API_KEY="你的 API Key"
$env:OPENAI_BASE_URL="https://你的服务地址/v1"
$env:OPENAI_MODEL="你的模型名"
python .\steam_review_analyzer.py 730 --llm --verbose
```

## 常用参数

- `--max-reviews 1000`：最多抓取 1000 条评价。默认 `0`，表示尽量抓取全部。
- `--summary-output result.md`：指定总结输出文件。
- `--output-jsonl reviews.jsonl`：保存评价明细。
- `--sort-by votes_up_desc`：抓取后按点赞数从高到低排序。其他值包括 `steam`、`weighted_score_desc`、`newest`、`playtime_desc`。
- `--llm`：启用 LLM 总结。
- `--llm-max-reviews 1200`：最多送入 LLM 的评价数。设为 `0` 表示送入全部评价，可能耗时且费用很高。
- `--chunk-size 80`：LLM 分批总结时每批评价数量。
- `--sleep 0.5`：Steam 分页请求间隔，避免请求过快。

## 注意

热门游戏可能有几十万甚至上百万条评价，完整抓取会花很久。第一次试跑建议使用 `--max-reviews 1000`，确认效果后再扩大范围。
