import os
import json
import requests
import anthropic
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, "env.env"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

players = {
    "大谷翔平": {"id": 660271, "team": "Los Angeles Dodgers", "team_id": 119},
    "James Wood": {"id": 695578, "team": "Washington Nationals", "team_id": 120},
}

ET = ZoneInfo("America/New_York")

def resolve_date(date_str: str | None) -> str:
    """將日期字串統一轉為 YYYY-MM-DD（美東時間基準）"""
    today = datetime.now(ET).date()
    if not date_str or date_str in ("今天", "today"):
        return str(today)
    if date_str in ("昨天", "yesterday"):
        return str(today - timedelta(days=1))
    # 支援 M/D 或 MM/DD 格式，自動補年份
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d"):
        try:
            d = datetime.strptime(date_str, fmt)
            if fmt == "%m/%d":
                d = d.replace(year=today.year)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return str(today)


# ── MLB 查詢工具 ──────────────────────────────────────────

def fetch_game_status(team_name: str, date: str = None) -> str:
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
        status = game["status"]["abstractGameState"]
        game_pk = game["gamePk"]
        home = game["teams"]["home"]["team"]
        away = game["teams"]["away"]["team"]
        opponent = away["name"] if home["id"] == info["team_id"] else home["name"]
        game_time = game.get("gameDate", "")
        return json.dumps({
            "game_pk": game_pk,
            "status": status,
            "opponent": opponent,
            "date": query_date,
            "game_time": game_time
        })

    return f"{info['team']} 在 {query_date} 沒有比賽"


def fetch_player_stats(player_name: str, date: str = None) -> str:
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

    # 指定日期就找該場，否則取最近一場
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

    box_res = requests.get(f"https://statsapi.mlb.com/api/v1/game/{game_id}/boxscore")
    box = box_res.json()
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

    return json.dumps({
        "player": player_name,
        "date": game_date,
        "hits": batting.get("hits", 0),
        "homeRuns": batting.get("homeRuns", 0),
        "rbi": batting.get("rbi", 0),
        "baseOnBalls": batting.get("baseOnBalls", 0),
        "strikeOuts": batting.get("strikeOuts", 0),
        "atBats": batting.get("atBats", 0),
    })


# ── Claude 工具定義 ───────────────────────────────────────

TOOLS = [
    {
        "name": "fetch_game_status",
        "description": "查詢某球隊或球員在指定日期的比賽狀態（尚未開始/進行中/已結束）。不指定日期則查今天。",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {"type": "string", "description": "球員名字或球隊名稱，例如：大谷翔平、James Wood"},
                "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD 或 M/D，例如 2026-03-29 或 3/29。不填則查今天。"}
            },
            "required": ["team_name"]
        }
    },
    {
        "name": "fetch_player_stats",
        "description": "查詢球員在指定日期的打擊數據。不指定日期則查最近一場。",
        "input_schema": {
            "type": "object",
            "properties": {
                "player_name": {"type": "string", "description": "球員名字，例如：大谷翔平、James Wood"},
                "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD 或 M/D，例如 2026-03-28 或 3/28。不填則查最近一場。"}
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
用戶說「昨天」「今天」「3/29」等日期表達都能理解。
今年是 2026 年。回答時請用繁體中文，數據盡量簡潔清楚。"""

# ── 主對話迴圈 ────────────────────────────────────────────

def run_tool(tool_name: str, tool_input: dict) -> str:
    func = TOOL_FUNCS.get(tool_name)
    if not func:
        return f"未知工具：{tool_name}"
    return func(**tool_input)


def chat():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    messages = []

    print("⚾ MLB 助手啟動（輸入 exit 結束）")
    print("-" * 40)

    while True:
        user_input = input("你: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "bye"):
            print("掰掰！")
            break

        messages.append({"role": "user", "content": user_input})

        while True:
            response = client.messages.create(
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
                        result = run_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result
                        })
                messages.append({"role": "user", "content": tool_results})
            else:
                reply = next((b.text for b in response.content if b.type == "text"), "")
                messages.append({"role": "assistant", "content": reply})
                print(f"助手: {reply}\n")
                break


if __name__ == "__main__":
    chat()
