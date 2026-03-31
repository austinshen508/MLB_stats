import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, "env.env"))

LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
USER_ID = os.getenv("LINE_USER_ID")

NOTIFIED_FILE = os.path.join(BASE_DIR, "notified_games.txt")

players = {
    "大谷翔平（道奇）": {"id": 660271, "team": "Los Angeles Dodgers", "team_id": 119},
    "James Wood（國民）": {"id": 695578, "team": "Washington Nationals", "team_id": 120},
}


def get_todays_game_status(team_id):
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today}&teamId={team_id}"
    res = requests.get(url)
    if res.status_code != 200:
        return None, None
    dates = res.json().get("dates", [])
    if not dates:
        return None, None
    for game in dates[0].get("games", []):
        game_pk = game["gamePk"]
        status = game["status"]["abstractGameState"]  # "Preview", "Live", "Final"
        return game_pk, status
    return None, None


def load_notified_games():
    if not os.path.exists(NOTIFIED_FILE):
        return set()
    with open(NOTIFIED_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def mark_game_notified(game_pk):
    with open(NOTIFIED_FILE, "a") as f:
        f.write(f"{game_pk}\n")


def get_season_stats(player_id):
    url = f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=season&season=2026&group=hitting"
    res = requests.get(url)
    if res.status_code != 200:
        return None
    stats = res.json().get("stats", [])
    splits = stats[0].get("splits", []) if stats else []
    if not splits:
        return None
    s = splits[0].get("stat", {})
    return {
        "avg": s.get("avg", ".000"),
        "slg": s.get("slg", ".000"),
    }


def get_latest_game_stats(player_id, override_team_name=None):
    url = f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=gameLog&season=2026"
    res = requests.get(url)
    if res.status_code != 200:
        print(f"❌ Failed to fetch logs for player {player_id}")
        return None

    data = res.json()
    logs = data.get("stats", [])[0].get("splits", [])
    if not logs:
        return "No game logs"

    logs.sort(key=lambda x: x["date"], reverse=True)
    latest = logs[0]
    game_date = latest["date"]
    game_id = latest["game"]["gamePk"]

    days_diff = (datetime.now(ZoneInfo("America/New_York")).date() - datetime.strptime(game_date, "%Y-%m-%d").date()).days
    diff_str = f"（{days_diff} 天前）" if days_diff > 0 else "（今天）"

    box_url = f"https://statsapi.mlb.com/api/v1/game/{game_id}/boxscore"
    box_res = requests.get(box_url)
    if box_res.status_code != 200:
        print(f"❌ Failed to fetch boxscore for game {game_id}")
        return None

    box = box_res.json()
    pid_key = f"ID{player_id}"
    player_data = None
    team_name = override_team_name or "Unknown Team"

    for side in ["home", "away"]:
        team = box["teams"][side]
        team_players = team.get("players", {})
        if pid_key in team_players:
            player_data = team_players[pid_key]
            team_name = team.get("team", {}).get("name", team_name)
            break

    if not player_data:
        return f"{player_id}: 無出賽紀錄"

    batting = player_data.get("stats", {}).get("batting", {})
    if batting.get("atBats", 0) == 0 and batting.get("plateAppearances", 0) == 0:
        return f"{player_id}: 無出賽紀錄"

    season = get_season_stats(player_id)
    season_line = f"賽季 AVG: {season['avg']} | SLG: {season['slg']}" if season else ""

    result = (
        f"{team_name} - {game_date} {diff_str}\n"
        f"H: {batting.get('hits', 0)} | HR: {batting.get('homeRuns', 0)} | "
        f"RBI: {batting.get('rbi', 0)} | BB: {batting.get('baseOnBalls', 0)} | "
        f"SO: {batting.get('strikeOuts', 0)} | AB: {batting.get('atBats', 0)}\n"
        f"{season_line}"
    )
    return result


def send_line_message(user_id, access_token, message):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    data = {
        "to": user_id,
        "messages": [{"type": "text", "text": message}],
    }
    res = requests.post(url, headers=headers, json=data)
    if res.status_code != 200:
        print(f"❌ LINE API error: {res.status_code}, {res.text}")
    else:
        print("✅ LINE message sent.")
    return res.status_code


def main():
    us_time = datetime.now(ZoneInfo("America/New_York"))
    print(f"\n{'='*40}")
    print(f"🕐 {us_time.strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"{'='*40}")

    notified = load_notified_games()
    messages = []
    game_pks_to_mark = set()

    for name, info in players.items():
        pid = info["id"]
        team_id = info["team_id"]

        game_pk, status = get_todays_game_status(team_id)
        print(f"{name}: game_pk={game_pk}, status={status}")

        if game_pk is None:
            print(f"  → 今天沒有比賽")
            continue

        status_label = {"Preview": "尚未開始", "Live": "進行中"}.get(status, status)
        if status != "Final":
            print(f"  → 比賽{status_label}（{status}），跳過")
            continue

        if str(game_pk) in notified:
            print(f"  → 已通知過 game {game_pk}，跳過")
            continue

        stats = get_latest_game_stats(pid, override_team_name=info["team"])
        if stats:
            messages.append(f"📊 {name}\n{stats}")
            game_pks_to_mark.add(str(game_pk))

    if messages:
        final_message = "\n\n".join(messages)
        send_line_message(USER_ID, LINE_TOKEN, final_message)
        for gp in game_pks_to_mark:
            mark_game_notified(gp)
    else:
        print("無需發送通知")


if __name__ == "__main__":
    main()
