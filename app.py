import os
import json
import requests
import anthropic
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, "env.env"))

LINE_TOKEN   = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET  = os.getenv("LINE_CHANNEL_SECRET")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

app = Flask(__name__)
handler = WebhookHandler(LINE_SECRET)
line_config = Configuration(access_token=LINE_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# 每個用戶的對話歷史（重啟後會清空）
user_sessions: dict[str, list] = {}

ET = ZoneInfo("America/New_York")

players = {
    "大谷翔平": {"id": 660271, "team": "Los Angeles Dodgers", "team_id": 119},
    "James Wood": {"id": 695578, "team": "Washington Nationals", "team_id": 120},
}

# ── MLB 查詢工具 ──────────────────────────────────────────

def resolve_date(date_str):
    today = datetime.now(ET).date()
    if not date_str or date_str in ("今天", "today"):
        return str(today)
    if date_str in ("昨天", "yesterday"):
        return str(today - timedelta(days=1))
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d"):
        try:
            d = datetime.strptime(date_str, fmt)
            if fmt == "%m/%d":
                d = d.replace(year=today.year)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return str(today)


def fetch_game_status(team_name, date=None):
    info = next((v for k, v in players.items() if team_name in k or k in team_name), None)
    if not info:
        return f"找不到球員或球隊：{team_name}"
    query_date = resolve_date(date)
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={query_date}&teamId={info['team_id']}"
    res = requests.get(url)
    dates = res.json().get("dates", [])
    if not dates:
        return f"{info['team']} 在 {query_date} 沒有比賽"
    for game in dates[0].get("games", []):
        home = game["teams"]["home"]["team"]
        away = game["teams"]["away"]["team"]
        opponent = away["name"] if home["id"] == info["team_id"] else home["name"]
        return json.dumps({
            "game_pk": game["gamePk"],
            "status": game["status"]["abstractGameState"],
            "opponent": opponent,
            "date": query_date,
            "game_time": game.get("gameDate", "")
        })
    return f"{info['team']} 在 {query_date} 沒有比賽"


def fetch_player_stats(player_name, date=None):
    info = next((v for k, v in players.items() if player_name in k or k in player_name), None)
    if not info:
        return f"找不到球員：{player_name}，目前支援：{', '.join(players.keys())}"
    url = f"https://statsapi.mlb.com/api/v1/people/{info['id']}/stats?stats=gameLog&season=2026"
    res = requests.get(url)
    stats = res.json().get("stats", [])
    logs = stats[0].get("splits", []) if stats else []
    if not logs:
        return f"{player_name} 本賽季尚無出賽紀錄"
    logs.sort(key=lambda x: x["date"], reverse=True)
    if date:
        query_date = resolve_date(date)
        matched = [l for l in logs if l["date"] == query_date]
        if not matched:
            return f"{player_name} 在 {query_date} 沒有出賽紀錄"
        target = matched[0]
    else:
        target = logs[0]
    game_date = target["date"]
    game_id = target["game"]["gamePk"]
    box = requests.get(f"https://statsapi.mlb.com/api/v1/game/{game_id}/boxscore").json()
    pid_key = f"ID{info['id']}"
    player_data = None
    for side in ["home", "away"]:
        team = box["teams"][side]
        if pid_key in team.get("players", {}):
            player_data = team["players"][pid_key]
            break
    if not player_data:
        return f"{player_name}（{game_date}）無出賽紀錄"
    batting = player_data.get("stats", {}).get("batting", {})
    if batting.get("atBats", 0) == 0 and batting.get("plateAppearances", 0) == 0:
        return f"{player_name}（{game_date}）無打席紀錄"

    # 賽季數據
    s_res = requests.get(f"https://statsapi.mlb.com/api/v1/people/{info['id']}/stats?stats=season&season=2026&group=hitting")
    s_stats = s_res.json().get("stats", [])
    s_splits = s_stats[0].get("splits", []) if s_stats else []
    season = s_splits[0].get("stat", {}) if s_splits else {}

    return json.dumps({
        "player": player_name,
        "date": game_date,
        "hits": batting.get("hits", 0),
        "homeRuns": batting.get("homeRuns", 0),
        "rbi": batting.get("rbi", 0),
        "baseOnBalls": batting.get("baseOnBalls", 0),
        "strikeOuts": batting.get("strikeOuts", 0),
        "atBats": batting.get("atBats", 0),
        "season_avg": season.get("avg", "N/A"),
        "season_slg": season.get("slg", "N/A"),
    })


TOOLS = [
    {
        "name": "fetch_game_status",
        "description": "查詢某球隊或球員在指定日期的比賽狀態。不指定日期則查今天。",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {"type": "string", "description": "球員名字或球隊名稱，例如：大谷翔平、James Wood"},
                "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD 或 M/D。不填則查今天。"}
            },
            "required": ["team_name"]
        }
    },
    {
        "name": "fetch_player_stats",
        "description": "查詢球員在指定日期的打擊數據與賽季成績。不指定日期則查最近一場。",
        "input_schema": {
            "type": "object",
            "properties": {
                "player_name": {"type": "string", "description": "球員名字，例如：大谷翔平、James Wood"},
                "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD 或 M/D。不填則查最近一場。"}
            },
            "required": ["player_name"]
        }
    }
]

TOOL_FUNCS = {
    "fetch_game_status": fetch_game_status,
    "fetch_player_stats": fetch_player_stats,
}

SYSTEM_PROMPT = """你是一個 MLB 棒球助手，專門追蹤以下球員的比賽數據：
- 大谷翔平（Shohei Ohtani）：洛杉磯道奇隊
- James Wood：華盛頓國民隊

你可以使用工具查詢任意日期的比賽狀態和球員數據。
今年是 2026 年。回答時請用繁體中文，訊息要簡潔，適合手機閱讀。不要使用 Markdown 格式。"""


def ask_claude(user_id: str, user_message: str) -> str:
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_message})

    # 只保留最近 20 則對話避免 token 超限
    if len(user_sessions[user_id]) > 20:
        user_sessions[user_id] = user_sessions[user_id][-20:]

    messages = user_sessions[user_id]

    while True:
        response = claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    func = TOOL_FUNCS.get(block.name)
                    result = func(**block.input) if func else "未知工具"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            reply = next((b.text for b in response.content if b.type == "text"), "抱歉，無法處理你的問題。")
            user_sessions[user_id].append({"role": "assistant", "content": reply})
            return reply


# ── LINE Webhook ──────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text

    reply = ask_claude(user_id, user_text)

    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )


@app.route("/", methods=["GET"])
def health():
    return "MLB Notify Bot is running!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
