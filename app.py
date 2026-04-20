import os
import json
import time
import threading
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
    ReplyMessageRequest, PushMessageRequest, BroadcastRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, "env.env"))

LINE_TOKEN    = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET   = os.getenv("LINE_CHANNEL_SECRET")
LINE_USER_ID  = os.getenv("LINE_USER_ID")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

app = Flask(__name__)
handler = WebhookHandler(LINE_SECRET)
line_config = Configuration(access_token=LINE_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# 每個用戶的對話歷史（重啟後會清空）
user_sessions: dict[str, list] = {}

# 每位球員今天是否已通知（格式：{"大谷翔平": "2026-03-31"}）
NOTIFIED_FILE = os.path.join(BASE_DIR, "notified_today.json")

def load_notified():
    try:
        with open(NOTIFIED_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_notified(data):
    with open(NOTIFIED_FILE, "w") as f:
        json.dump(data, f)

notified_today: dict = load_notified()

ET = ZoneInfo("America/New_York")

players = {
    "大谷翔平": {"id": 660271, "team": "Los Angeles Dodgers", "team_id": 119, "en_last_name": "Ohtani"},
    "James Wood": {"id": 695578, "team": "Washington Nationals", "team_id": 120, "en_last_name": "Wood"},
    "Mason Miller": {"id": 695243, "team": "San Diego Padres", "team_id": 135, "en_last_name": "Miller", "is_pitcher": True},
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
    query_date = resolve_date(date)
    # 若指定日期找不到，往前最多找 7 天
    for days_back in range(8):
        check_date = str((datetime.strptime(query_date, "%Y-%m-%d") - timedelta(days=days_back)).date())
        sched = requests.get(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={check_date}&teamId={info['team_id']}").json()
        dates = sched.get("dates", [])
        if not dates:
            continue
        game = None
        for g in dates[0].get("games", []):
            if g["status"]["abstractGameState"] == "Final":
                game = g
                break
        if game:
            game_date = check_date
            game_id = game["gamePk"]
            break
    else:
        return f"{player_name} 最近 7 天沒有已結束的比賽紀錄"

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

    if info.get("is_pitcher"):
        pitching = player_data.get("stats", {}).get("pitching", {})
        ip = pitching.get("inningsPitched", "0.0")
        if not pitching or (str(ip) in ("0.0", "0") and pitching.get("strikeOuts", 0) == 0):
            return f"{player_name}（{game_date}）無投球紀錄"

        s_res = requests.get(f"https://statsapi.mlb.com/api/v1/people/{info['id']}/stats?stats=season&season=2026&group=pitching")
        s_stats = s_res.json().get("stats", [])
        s_splits = s_stats[0].get("splits", []) if s_stats else []
        season = s_splits[0].get("stat", {}) if s_splits else {}

        return json.dumps({
            "player": player_name,
            "date": game_date,
            "inningsPitched": ip,
            "strikeOuts": pitching.get("strikeOuts", 0),
            "walks": pitching.get("baseOnBalls", 0),
            "earnedRuns": pitching.get("earnedRuns", 0),
            "season_era": season.get("era", "N/A"),
            "season_whip": season.get("whip", "N/A"),
            "season_ip": season.get("inningsPitched", "N/A"),
            "season_so": season.get("strikeOuts", "N/A"),
            "season_bb": season.get("baseOnBalls", "N/A"),
            "season_wins": season.get("wins", "N/A"),
            "season_losses": season.get("losses", "N/A"),
            "season_saves": season.get("saves", "N/A"),
        })

    batting = player_data.get("stats", {}).get("batting", {})
    if batting.get("atBats", 0) == 0 and batting.get("plateAppearances", 0) == 0:
        return f"{player_name}（{game_date}）無打席紀錄"

    # 賽季基本數據
    s_res = requests.get(f"https://statsapi.mlb.com/api/v1/people/{info['id']}/stats?stats=season&season=2026&group=hitting")
    s_stats = s_res.json().get("stats", [])
    s_splits = s_stats[0].get("splits", []) if s_stats else []
    season = s_splits[0].get("stat", {}) if s_splits else {}

    # 賽季進階數據
    adv_res = requests.get(f"https://statsapi.mlb.com/api/v1/people/{info['id']}/stats?stats=seasonAdvanced&season=2026&group=hitting")
    adv_stats = adv_res.json().get("stats", [])
    adv_splits = adv_stats[0].get("splits", []) if adv_stats else []
    adv = adv_splits[0].get("stat", {}) if adv_splits else {}

    # 計算 wOBA（2024 weights）
    pa = int(season.get("plateAppearances", 0))
    ubb = int(season.get("baseOnBalls", 0)) - int(season.get("intentionalWalks", 0))
    hbp = int(season.get("hitByPitch", 0))
    h = int(season.get("hits", 0))
    doubles = int(season.get("doubles", 0))
    triples = int(season.get("triples", 0))
    hr = int(season.get("homeRuns", 0))
    ab = int(season.get("atBats", 0))
    ibb = int(season.get("intentionalWalks", 0))
    sf = int(season.get("sacFlies", 0))
    single = h - doubles - triples - hr
    denom = ab + ubb + hbp + sf
    if denom > 0:
        woba = (0.690*ubb + 0.722*hbp + 0.888*single + 1.271*doubles + 1.616*triples + 2.101*hr) / denom
        woba_str = f"{woba:.3f}"
    else:
        woba_str = "N/A"

    # 計算 SwStr%
    total_swings = int(adv.get("totalSwings", 0))
    swing_miss = int(adv.get("swingAndMisses", 0))
    total_pitches = int(adv.get("numberOfPitches", season.get("numberOfPitches", 0)))
    swstr = f"{swing_miss/total_pitches*100:.1f}%" if total_pitches > 0 else "N/A"

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
        "season_obp": season.get("obp", "N/A"),
        "season_slg": season.get("slg", "N/A"),
        "season_ops": season.get("ops", "N/A"),
        "season_iso": adv.get("iso", "N/A"),
        "season_babip": season.get("babip", "N/A"),
        "season_bb_pct": adv.get("walksPerPlateAppearance", "N/A"),
        "season_k_pct": adv.get("strikeoutsPerPlateAppearance", "N/A"),
        "season_p_pa": adv.get("pitchesPerPlateAppearance", "N/A"),
        "season_woba": woba_str,
        "season_swstr": swstr,
    })


TOOLS = [
    {
        "name": "fetch_game_status",
        "description": "查詢某球隊或球員在指定日期的比賽狀態。不指定日期則查今天。",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {"type": "string", "description": "球員名字或球隊名稱，例如：大谷翔平、James Wood、Mason Miller"},
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
                "player_name": {"type": "string", "description": "球員名字，例如：大谷翔平、James Wood、Mason Miller"},
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
- 大谷翔平（Shohei Ohtani）：洛杉磯道奇隊（打者）
- James Wood：華盛頓國民隊（打者）
- Mason Miller：聖地牙哥教士隊（投手）

你可以使用工具查詢任意日期的比賽狀態和球員數據。
Mason Miller 為投手，數據包含：IP（投球局數）、SO（三振）、BB（保送）、ER（自責失分）、ERA（自責失分率）、WHIP（被安打率）。
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


# ── 自動通知背景執行緒 ─────────────────────────────────────

def get_game_status(team_id):
    today = datetime.now(ET).date()
    # 先查今天，若今天沒有 Final 則查昨天（夜場比賽可能跨過午夜才結束）
    for days_back in range(2):
        check_date = str(today - timedelta(days=days_back))
        res = requests.get(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={check_date}&teamId={team_id}")
        if res.status_code != 200:
            continue
        dates = res.json().get("dates", [])
        if not dates:
            continue
        for game in dates[0].get("games", []):
            if game["status"]["abstractGameState"] == "Final":
                return game["gamePk"], "Final", check_date
    return None, None, None


def get_hr_video_url(game_pk, player_last_name):
    try:
        res = requests.get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/content", timeout=10)
        items = res.json().get("highlights", {}).get("highlights", {}).get("items", [])
        name_lower = player_last_name.lower()
        # 優先找「球員名字開頭 + home run」的 headline（直接 HR 影片）
        for item in items:
            h = item.get("headline", "").lower()
            if name_lower in h and ("home run" in h or "homer" in h) and h.startswith(name_lower):
                for pb in item.get("playbacks", []):
                    if pb.get("name") in ("mp4Avc", "hlsCloud"):
                        return pb.get("url")
        # 備用：headline 含球員名字且含 home run（不限開頭）
        for item in items:
            h = item.get("headline", "").lower()
            if name_lower in h and ("home run" in h or "homer" in h):
                for pb in item.get("playbacks", []):
                    if pb.get("name") in ("mp4Avc", "hlsCloud"):
                        return pb.get("url")
    except Exception:
        pass
    return None


def get_game_stats_message(player_id, team_name, game_pk, player_last_name="", game_date=None, is_pitcher=False):
    game_id = game_pk
    today = game_date or datetime.now(ET).strftime("%Y-%m-%d")

    box = requests.get(f"https://statsapi.mlb.com/api/v1/game/{game_id}/boxscore").json()
    pid_key = f"ID{player_id}"
    player_data = None
    for side in ["home", "away"]:
        team = box["teams"][side]
        if pid_key in team.get("players", {}):
            player_data = team["players"][pid_key]
            break

    if not player_data:
        return None

    if is_pitcher:
        pitching = player_data.get("stats", {}).get("pitching", {})
        ip = pitching.get("inningsPitched", "0.0")
        if not pitching or (str(ip) in ("0.0", "0") and pitching.get("strikeOuts", 0) == 0):
            return None

        s_res = requests.get(f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=season&season=2026&group=pitching")
        s_stats = s_res.json().get("stats", [])
        s_splits = s_stats[0].get("splits", []) if s_stats else []
        season = s_splits[0].get("stat", {}) if s_splits else {}

        return (
            f"{team_name} - {today}\n"
            f"IP: {ip} | SO: {pitching.get('strikeOuts', 0)} | "
            f"BB: {pitching.get('baseOnBalls', 0)} | ER: {pitching.get('earnedRuns', 0)}\n"
            f"Season ERA: {season.get('era', 'N/A')} | WHIP: {season.get('whip', 'N/A')}"
        )

    batting = player_data.get("stats", {}).get("batting", {})
    if batting.get("atBats", 0) == 0 and batting.get("plateAppearances", 0) == 0:
        return None

    s_res = requests.get(f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=season&season=2026&group=hitting")
    s_stats = s_res.json().get("stats", [])
    s_splits = s_stats[0].get("splits", []) if s_stats else []
    season = s_splits[0].get("stat", {}) if s_splits else {}

    hr_count = batting.get("homeRuns", 0)
    msg = (
        f"{team_name} - {today}\n"
        f"H: {batting.get('hits',0)} | HR: {hr_count} | "
        f"RBI: {batting.get('rbi',0)} | BB: {batting.get('baseOnBalls',0)} | "
        f"SO: {batting.get('strikeOuts',0)} | AB: {batting.get('atBats',0)}\n"
        f"賽季 AVG: {season.get('avg','N/A')} | SLG: {season.get('slg','N/A')}"
    )
    if hr_count > 0 and player_last_name:
        video_url = get_hr_video_url(game_pk, player_last_name)
        if video_url:
            msg += f"\n🎬 全壘打影片：{video_url}"
    return msg


def send_push_message(text):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).broadcast(
                BroadcastRequest(messages=[TextMessage(text=text)])
            )
        print("[broadcast] 發送成功")
    except Exception as e:
        print(f"[broadcast error] {e}")


def notify_loop():
    while True:
        try:
            messages = []
            today = datetime.now(ET).strftime("%Y-%m-%d")
            changed = False
            for name, info in players.items():
                # 以 "name:game_date" 為 key，避免重啟後用 today 覆蓋昨日遊戲導致跳過今天
                game_pk, status, game_date = get_game_status(info["team_id"])
                if game_pk is None or status != "Final":
                    continue
                notify_key = f"{name}:{game_date}"
                if notified_today.get(notify_key):
                    continue
                stats = get_game_stats_message(info["id"], info["team"], game_pk, info.get("en_last_name", ""), game_date, info.get("is_pitcher", False))
                # 無論有無打擊數據，都標記這場比賽已處理，避免重複
                notified_today[notify_key] = today
                changed = True
                if stats:
                    icon = "⚾" if info.get("is_pitcher") else "🏏"
                    messages.append(f"{icon} {name}\n{stats}")
                else:
                    print(f"[notify_loop] {name} 在 {game_date} 無出賽數據")

            if changed:
                save_notified(notified_today)
            if messages:
                send_push_message("\n\n".join(messages))
        except Exception as e:
            print(f"[notify_loop error] {e}")

        time.sleep(60)  # 每 1 分鐘檢查一次


def keepalive_loop():
    """每 10 分鐘 ping 自己，防止 Render Free 方案休眠"""
    app_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not app_url:
        print("[keepalive] 未設定 RENDER_EXTERNAL_URL，跳過 keepalive")
        return
    while True:
        try:
            requests.get(f"{app_url}/", timeout=10)
            print("[keepalive] ping 成功")
        except Exception as e:
            print(f"[keepalive error] {e}")
        time.sleep(600)  # 每 10 分鐘 ping 一次

# 啟動背景通知執行緒
threading.Thread(target=notify_loop, daemon=True).start()
threading.Thread(target=keepalive_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
