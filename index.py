import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory
import requests

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

DB_FILE = "matches.json"
SUBS_FILE = "subscribers.json"
SETTINGS_FILE = "portal_settings.json"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
POLL_INTERVAL_SECONDS = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

app = Flask(__name__)
db_lock = threading.Lock()
_worker_started = False


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_portal_settings():
    default = {
        "portal_locked": True,
        "price": "$5",
        "payment_info": "Pay $5 once to unlock the FULL portal (all live matches & both OBS scoreboard designs). Send payment receipt to get your Access Key.",
        "design1_name": "Design 1 — Classic Red & Gold Stadium",
        "design2_name": "Design 2 — Modern TV Broadcast HUD",
    }
    data = load_json(SETTINGS_FILE, default)
    for k, v in default.items():
        data.setdefault(k, v)
    return data


def is_key_authorized(provided_key):
    settings = get_portal_settings()
    if not settings.get("portal_locked", True):
        return True
    if not provided_key:
        return False
    subs = load_json(SUBS_FILE, {})
    user = subs.get(provided_key)
    return bool(user and user.get("active", False))


def default_match_payload(source_url, title="", match_time="", venue="", bowling_score=""):
    return {
        "title": title or "Live Cricket Match",
        "match_time": match_time or "LIVE",
        "venue": venue or "",
        "bowling_score": bowling_score or "Bowling",
        "crex_url": source_url,
        "batting_team": "TEAM A",
        "bowling_team": "TEAM B",
        "batting_flag": "https://flagcdn.com/w160/ae.png",
        "bowling_flag": "https://flagcdn.com/w160/ae.png",
        "score": "0/0",
        "overs": "0.0",
        "last_ball": "0",
        "crr": "0.00",
        "partnership": "0 (0)",
        "match_status": "MATCH IN PROGRESS",
        "over_prev_num": "PREV:",
        "over_prev_balls": ["0", "0", "0", "0", "0", "0"],
        "over_prev_total": "= 0",
        "over_curr_num": "CURR:",
        "over_curr_balls": ["•", "•", "•", "•", "•", "•"],
        "over_curr_total": "= 0",
        "striker": {
            "name": "BATTER 1",
            "runs": "0",
            "balls": "(0)",
            "fours": "0",
            "sixes": "0",
            "sr": "0.00",
            "image": "",
            "on_strike": True,
        },
        "non_striker": {
            "name": "BATTER 2",
            "runs": "0",
            "balls": "(0)",
            "fours": "0",
            "sixes": "0",
            "sr": "0.00",
            "image": "",
            "on_strike": False,
        },
        "bowler": {
            "name": "BOWLER",
            "figures": "0-0",
            "overs": "(0.0)",
            "econ": "0.00",
            "image": "",
        },
        "last_wicket": "LIVE BROADCAST",
    }


TEAM_MAP = {
    "IND": ("INDIA", "https://flagcdn.com/w160/in.png"),
    "NZ": ("NEW ZEALAND", "https://flagcdn.com/w160/nz.png"),
    "SA": ("SOUTH AFRICA", "https://flagcdn.com/w160/za.png"),
    "BAN": ("BANGLADESH", "https://flagcdn.com/w160/bd.png"),
    "PAK": ("PAKISTAN", "https://flagcdn.com/w160/pk.png"),
    "AUS": ("AUSTRALIA", "https://flagcdn.com/w160/au.png"),
    "ENG": ("ENGLAND", "https://flagcdn.com/w160/gb-eng.png"),
    "SL": ("SRI LANKA", "https://flagcdn.com/w160/lk.png"),
    "AFG": ("AFGHANISTAN", "https://flagcdn.com/w160/af.png"),
    "WI": ("WEST INDIES", "https://flagcdn.com/w160/jm.png"),
    "ZIM": ("ZIMBABWE", "https://flagcdn.com/w160/zw.png"),
    "IRE": ("IRELAND", "https://flagcdn.com/w160/ie.png"),
    "NED": ("NETHERLANDS", "https://flagcdn.com/w160/nl.png"),
    "OMA": ("OMAN", "https://flagcdn.com/w160/om.png"),
    "UAE": ("UAE", "https://flagcdn.com/w160/ae.png"),
    "DV": ("DESERT VIPERS", "https://flagcdn.com/w160/ae.png"),
    "SW": ("SHARJAH WARRIORZ", "https://flagcdn.com/w160/ae.png"),
    "AA": ("AJMAN", "https://flagcdn.com/w160/ae.png"),
    "UT": ("UP TUSKERS", "https://flagcdn.com/w160/in.png"),
}


def fetch_page_html(url):
    html = ""
    try:
        res = requests.get(url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            html = res.text
    except Exception as exc:
        print(f"Requests error: {exc}")

    is_cricinfo = "espncricinfo.com" in url.lower()
    has_data = "__NEXT_DATA__" in html if is_cricinfo else bool(
        re.search(r"\d+\s*\(\d+\)\s+\d+\s+\d+\s+\d+\.\d+", html)
    )
    if (not html or not has_data) and HAS_PLAYWRIGHT:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.goto(url, wait_until="domcontentloaded", timeout=12000)
                page.wait_for_timeout(1500)
                html = page.content()
                browser.close()
        except Exception as exc:
            print(f"Playwright error: {exc}")

    return html


def parse_cricinfo_page(html_text, data):
    soup = BeautifulSoup(html_text, "html.parser")
    raw_text = " ".join(soup.stripped_strings)
    page_text = re.sub(r"\(\s*(\d+(?:\.\d+)?)\s*\)", r"(\1)", raw_text)

    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script and next_script.string:
        try:
            nd = json.loads(next_script.string)
            props = nd.get("props", {}).get("appPageProps", {}).get("data", {})
            match_obj = props.get("match", {})
            content = props.get("content", {})

            teams = match_obj.get("teams", [])
            if len(teams) >= 2:
                for t in teams:
                    t_info = t.get("team", {})
                    abbr = t_info.get("abbreviation", "").upper()
                    name = t_info.get("name", abbr).upper()
                    img_url = t_info.get("imageUrl") or ""
                    if img_url and not img_url.startswith("http"):
                        img_url = f"https://img1.hscicdn.com/image/upload/f_auto,t_ds_square_w_160{img_url}"
                    fallback_flag = TEAM_MAP.get(abbr, (name, data["batting_flag"]))[1]

                    if t.get("isBatting"):
                        data["batting_team"] = name
                        data["batting_flag"] = img_url or fallback_flag
                        if t.get("score"):
                            data["score"] = t["score"].replace("-", "/")
                        if t.get("scoreInfo"):
                            ov_m = re.search(r"(\d+(?:\.\d+)?)\s*ov", t["scoreInfo"])
                            if ov_m:
                                data["overs"] = ov_m.group(1)
                    else:
                        data["bowling_team"] = name
                        data["bowling_flag"] = img_url or fallback_flag

            if match_obj.get("statusText"):
                data["match_status"] = match_obj["statusText"].upper()

            live_perf = content.get("livePerformance", {})
            batsmen = live_perf.get("batsmen", [])
            bowlers = live_perf.get("bowlers", [])

            if len(batsmen) >= 1:
                b1 = batsmen[0]
                p1 = b1.get("player", {})
                p1_img = p1.get("headshotImageUrl") or ""
                if p1_img and not p1_img.startswith("http"):
                    p1_img = f"https://img1.hscicdn.com/image/upload/f_auto,t_ds_square_w_160{p1_img}"
                data["striker"] = {
                    "name": p1.get("fieldingName", p1.get("name", "BATTER 1")).upper(),
                    "runs": str(b1.get("runs", 0)),
                    "balls": f"({b1.get('balls', 0)})",
                    "fours": str(b1.get("fours", 0)),
                    "sixes": str(b1.get("sixes", 0)),
                    "sr": str(b1.get("strikerate", "0.00")),
                    "image": p1_img,
                    "on_strike": bool(b1.get("isOnStrike", True)),
                }
            if len(batsmen) >= 2:
                b2 = batsmen[1]
                p2 = b2.get("player", {})
                p2_img = p2.get("headshotImageUrl") or ""
                if p2_img and not p2_img.startswith("http"):
                    p2_img = f"https://img1.hscicdn.com/image/upload/f_auto,t_ds_square_w_160{p2_img}"
                data["non_striker"] = {
                    "name": p2.get("fieldingName", p2.get("name", "BATTER 2")).upper(),
                    "runs": str(b2.get("runs", 0)),
                    "balls": f"({b2.get('balls', 0)})",
                    "fours": str(b2.get("fours", 0)),
                    "sixes": str(b2.get("sixes", 0)),
                    "sr": str(b2.get("strikerate", "0.00")),
                    "image": p2_img,
                    "on_strike": bool(b2.get("isOnStrike", False)),
                }

            if len(bowlers) >= 1:
                bw = bowlers[0]
                pw = bw.get("player", {})
                pw_img = pw.get("headshotImageUrl") or ""
                if pw_img and not pw_img.startswith("http"):
                    pw_img = f"https://img1.hscicdn.com/image/upload/f_auto,t_ds_square_w_160{pw_img}"
                data["bowler"] = {
                    "name": pw.get("fieldingName", pw.get("name", "BOWLER")).upper(),
                    "figures": f"{bw.get('wickets', 0)}-{bw.get('conceded', 0)}",
                    "overs": f"({bw.get('overs', '0.0')})",
                    "econ": str(bw.get("economy", "0.00")),
                    "image": pw_img,
                }
        except Exception as exc:
            print(f"Cricinfo JSON parse note: {exc}")

    crr_m = re.search(r"Current RR:\s*([\d.]+)", page_text, re.IGNORECASE)
    if crr_m:
        data["crr"] = crr_m.group(1)

    pship_m = re.search(r"Partnership:\s*(\d+\s*\(\d+\))", page_text, re.IGNORECASE)
    if pship_m:
        data["partnership"] = pship_m.group(1)

    lw_m = re.search(r"Last wicket:\s*([A-Za-z.\s'-]+\d+\s*\(\d+\))", page_text, re.IGNORECASE)
    if lw_m:
        data["last_wicket"] = f"LAST WKT: {lw_m.group(1).strip().upper()}"

    return data


def extract_crex_team_flags(soup, base_url, bat_code, bowl_code):
    bat_flag = ""
    bowl_flag = ""
    team_imgs = []

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        alt = (img.get("alt") or "").strip()
        if not src:
            continue
        low_src = src.lower()
        low_alt = alt.lower()
        if any(s in low_src for s in ["player", "crex", "google", "appstore", "playstore", "data:image"]):
            continue
        full_src = urljoin(base_url, src)

        if any(k in low_src for k in ["team", "flag", "logo", "cricketvectors"]):
            if full_src not in team_imgs:
                team_imgs.append(full_src)

        if not bat_flag and bat_code and (
            low_alt == bat_code.lower()
            or f"/{bat_code.lower()}." in low_src
            or f"_{bat_code.lower()}." in low_src
            or f"-{bat_code.lower()}." in low_src
        ):
            bat_flag = full_src
        if not bowl_flag and bowl_code and (
            low_alt == bowl_code.lower()
            or f"/{bowl_code.lower()}." in low_src
            or f"_{bowl_code.lower()}." in low_src
            or f"-{bowl_code.lower()}." in low_src
        ):
            bowl_flag = full_src

    if (not bat_flag or not bowl_flag) and len(team_imgs) >= 2:
        if not bat_flag:
            bat_flag = team_imgs[0]
        if not bowl_flag:
            bowl_flag = team_imgs[1]

    if not bat_flag and bat_code in TEAM_MAP:
        bat_flag = TEAM_MAP[bat_code][1]
    if not bowl_flag and bowl_code in TEAM_MAP:
        bowl_flag = TEAM_MAP[bowl_code][1]

    return bat_flag, bowl_flag


def find_player_images(soup, base_url, player_names):
    found = {}
    all_imgs = soup.find_all("img")
    for name in player_names:
        parts = [p.lower() for p in re.sub(r"[^A-Za-z\s]", "", name).split() if len(p) > 2]
        last_name = parts[-1] if parts else name.lower()
        for img in all_imgs:
            src = img.get("src") or img.get("data-src") or ""
            alt = (img.get("alt") or "").lower()
            if not src or any(x in src.lower() for x in ["flag", "team", "logo"]):
                continue
            if last_name in alt or last_name in src.lower():
                found[name] = urljoin(base_url, src)
                break
        if name not in found:
            node = soup.find(string=re.compile(re.escape(last_name), re.I))
            if node:
                parent = node.parent
                for _ in range(4):
                    if not parent:
                        break
                    img = parent.find("img")
                    if img:
                        src = img.get("src") or img.get("data-src") or ""
                        if src and not any(x in src.lower() for x in ["flag", "team", "logo"]):
                            found[name] = urljoin(base_url, src)
                            break
                    parent = parent.parent
    return found


def parse_crex_page(html_text, data):
    soup = BeautifulSoup(html_text, "html.parser")
    tokens = [re.sub(r"\s+", " ", t).strip() for t in soup.stripped_strings if t.strip()]
    raw_text = " ".join(tokens)
    page_text = re.sub(r"\(\s*(\d+(?:\.\d+)?)\s*\)", r"(\1)", raw_text)
    base_url = data.get("crex_url", "https://crex.com")

    teams_in_title = re.findall(r"\b([A-Z]{2,4})\s+vs\s+([A-Z]{2,4})\b", page_text)
    if teams_in_title:
        all_codes = list(teams_in_title[0])
    else:
        slug_match = re.search(r"/([a-z]{2,4})-vs-([a-z]{2,4})-", base_url.lower())
        all_codes = [slug_match.group(1).upper(), slug_match.group(2).upper()] if slug_match else []

    score_match = re.search(
        r"\b([A-Z]{2,4})\s+(\d+[-/]\d+)\s*\(?(\d+\.\d+|\d+)\)?", page_text
    )
    if score_match:
        bat_code, raw_score, overs = score_match.groups()
        data["score"] = raw_score.replace("-", "/")
        data["overs"] = overs
        data["batting_team"] = TEAM_MAP.get(bat_code, (bat_code, ""))[0]

        bowl_code = next((c for c in all_codes if c != bat_code), "")
        if bowl_code:
            data["bowling_team"] = TEAM_MAP.get(bowl_code, (bowl_code, ""))[0]

        bat_flag, bowl_flag = extract_crex_team_flags(soup, base_url, bat_code, bowl_code)
        if bat_flag:
            data["batting_flag"] = bat_flag
        if bowl_flag:
            data["bowling_flag"] = bowl_flag

    crr_match = re.search(r"CRR\s*:?\s*([\d.]+)", page_text, re.IGNORECASE)
    if crr_match and float(crr_match.group(1)) > 0:
        data["crr"] = crr_match.group(1)
    else:
        try:
            runs_scored = int(data["score"].split("/")[0])
            ov_parts = str(data["overs"]).split(".")
            completed_ov = int(ov_parts[0]) + (int(ov_parts[1]) / 6.0 if len(ov_parts) > 1 else 0.0)
            if completed_ov > 0:
                data["crr"] = f"{runs_scored / completed_ov:.2f}"
        except (ValueError, IndexError):
            pass

    pship_match = re.search(r"P'ship\s*:?\s*(\d+)\s*\((\d+)\)", page_text, re.IGNORECASE)
    if pship_match:
        data["partnership"] = f"{pship_match.group(1)} ({pship_match.group(2)})"

    status_match = re.search(
        r"\b([A-Z]{2,4}\s+(?:trail by \d+ runs?|lead by \d+ runs?|need \d+ runs?(?: to win)?|opt to (?:bat|bowl)|won by [^.|]+))",
        page_text,
        re.IGNORECASE,
    )
    if status_match:
        data["match_status"] = status_match.group(1).strip().upper()

    over_blocks = re.findall(
        r"Over\s+(\d+)\s*:?\s*((?:(?:[0-6]|W|w|\d*wd|\d*nb|\d*lb|\d*b|\+|•)\s+){1,10})=\s*(\d+)",
        page_text,
    )
    if len(over_blocks) >= 2:
        prev_ov, prev_balls, prev_tot = over_blocks[-2]
        curr_ov, curr_balls, curr_tot = over_blocks[-1]
        p_list = prev_balls.strip().split()
        c_list = curr_balls.strip().split()
        while len(c_list) < 6:
            c_list.append("•")

        data["over_prev_num"] = f"OVER {prev_ov}:"
        data["over_prev_balls"] = p_list[:6]
        data["over_prev_total"] = f"= {prev_tot}"

        data["over_curr_num"] = f"OVER {curr_ov}:"
        data["over_curr_balls"] = c_list[:6]
        data["over_curr_total"] = f"= {curr_tot}"

        bowled = [b for b in c_list if b != "•"]
        if bowled:
            data["last_ball"] = bowled[-1]
        elif p_list:
            data["last_ball"] = p_list[-1]

    batter_patterns = [
        re.compile(
            r"\b([A-Z]\.?\s+[A-Za-z'-]+)\s*(\*?)\s+(\d+)\s*\((\d+)\)\s+(?:4s\s*:?\s*)?(\d+)\s+(?:6s\s*:?\s*)?(\d+)\s+(?:SR\s*:?\s*)?([\d.]+)"
        ),
        re.compile(
            r"\b([A-Za-z][A-Za-z.'-]+\s+[A-Za-z'-]+)\s*(\*?)\s+(\d+)\s*\((\d+)\)\s+(?:4s\s*:?\s*)?(\d+)\s+(?:6s\s*:?\s*)?(\d+)\s+(?:SR\s*:?\s*)?([\d.]+)"
        ),
    ]
    ignored = {"over", "last", "wkt", "match", "live", "stats", "series", "crr", "rrr", "day", "session"}
    batter_matches = []
    for pat in batter_patterns:
        matches = [
            m for m in pat.findall(page_text)
            if not any(w.lower() in ignored for w in m[0].split())
        ]
        if matches:
            batter_matches = matches
            break

    bowler_patterns = [
        re.compile(
            r"\b([A-Z]\.?\s+[A-Za-z'-]+)\s+(\d+-\d+)\s+\(?(\d+(?:\.\d+)?)\)?\s+(?:Econ\s*:?\s*)?([\d.]+)"
        ),
        re.compile(
            r"\b([A-Za-z][A-Za-z.'-]+\s+[A-Za-z'-]+)\s+(\d+-\d+)\s+\(?(\d+(?:\.\d+)?)\)?\s+(?:Econ\s*:?\s*)?([\d.]+)"
        ),
    ]
    bowler_matches = []
    for pat in bowler_patterns:
        matches = [
            m for m in pat.findall(page_text)
            if not any(w.lower() in ignored for w in m[0].split())
        ]
        if matches:
            bowler_matches = matches
            break

    names = []
    if len(batter_matches) >= 1:
        names.append(batter_matches[0][0])
    if len(batter_matches) >= 2:
        names.append(batter_matches[1][0])
    if bowler_matches:
        names.append(bowler_matches[0][0])
    player_imgs = find_player_images(soup, base_url, names)

    if len(batter_matches) >= 1:
        b1 = batter_matches[0]
        b2_star = batter_matches[1][1] if len(batter_matches) >= 2 else ""
        b1_on_strike = bool(b1[1]) or not bool(b2_star)
        data["striker"] = {
            "name": b1[0].upper(),
            "runs": b1[2],
            "balls": f"({b1[3]})",
            "fours": b1[4],
            "sixes": b1[5],
            "sr": b1[6],
            "image": player_imgs.get(b1[0], data.get("striker", {}).get("image", "")),
            "on_strike": b1_on_strike,
        }
    if len(batter_matches) >= 2:
        b2 = batter_matches[1]
        data["non_striker"] = {
            "name": b2[0].upper(),
            "runs": b2[2],
            "balls": f"({b2[3]})",
            "fours": b2[4],
            "sixes": b2[5],
            "sr": b2[6],
            "image": player_imgs.get(b2[0], data.get("non_striker", {}).get("image", "")),
            "on_strike": bool(b2[1]),
        }

    if bowler_matches:
        bw = bowler_matches[0]
        data["bowler"] = {
            "name": bw[0].upper(),
            "figures": bw[1],
            "overs": f"({bw[2]})",
            "econ": bw[3],
            "image": player_imgs.get(bw[0], data.get("bowler", {}).get("image", "")),
        }

    lw_match = re.search(
        r"Last\s*Wkt\s*:?\s*([A-Za-z.\s'-]+\d+\s*\(\d+\))",
        page_text,
        re.IGNORECASE,
    )
    if lw_match:
        data["last_wicket"] = f"LAST WKT: {lw_match.group(1).strip().upper()}"

    return data


def update_single_match(match_id):
    with db_lock:
        db = load_json(DB_FILE, {})
        if match_id not in db:
            return None
        source_url = db[match_id].get("crex_url", "")
        snapshot = dict(db[match_id])

    if not source_url:
        return snapshot

    try:
        html_text = fetch_page_html(source_url)
        if html_text:
            if "espncricinfo.com" in source_url.lower():
                updated = parse_cricinfo_page(html_text, snapshot)
            else:
                updated = parse_crex_page(html_text, snapshot)

            with db_lock:
                db = load_json(DB_FILE, {})
                if match_id in db:
                    db[match_id] = updated
                    save_json(DB_FILE, db)
            return updated
    except Exception as exc:
        print(f"[{match_id}] Background update error: {exc}")

    return snapshot


def background_poller():
    while True:
        with db_lock:
            match_ids = list(load_json(DB_FILE, {}).keys())
        for mid in match_ids:
            update_single_match(mid)
        time.sleep(POLL_INTERVAL_SECONDS)


def start_background_worker():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        t = threading.Thread(target=background_poller, daemon=True)
        t.start()


start_background_worker()


# --- PUBLIC & OBS ROUTES (DESIGN 1 & DESIGN 2) ---

@app.route("/")
@app.route("/live-matches")
def serve_home():
    return send_from_directory(".", "home.html")


@app.route("/admin")
def serve_admin():
    return send_from_directory(".", "admin.html")


@app.route("/obs/<match_id>")
def serve_overlay_design1(match_id):
    return send_from_directory(".", "overlay.html")


@app.route("/obs2/<match_id>")
def serve_overlay_design2(match_id):
    return send_from_directory(".", "overlay2.html")


@app.route("/api/match/<match_id>")
def get_match_api(match_id):
    provided_key = request.args.get("key", "").strip()
    with db_lock:
        db = load_json(DB_FILE, {})
        data = db.get(match_id)
        settings = get_portal_settings()
        unlocked = is_key_authorized(provided_key)

    if not data:
        return jsonify({"error": "Match not found"}), 404

    if unlocked:
        result = dict(data)
        result["locked"] = False
        return jsonify(result)

    return jsonify({
        "locked": True,
        "title": data.get("title", "Live Cricket Match"),
        "batting_team": data.get("batting_team", "TEAM A"),
        "bowling_team": data.get("bowling_team", "TEAM B"),
        "price": settings.get("price", "$5"),
        "payment_info": settings.get("payment_info", ""),
    })


@app.route("/api/matches", methods=["GET"])
def list_matches():
    provided_key = request.args.get("key", "").strip()
    with db_lock:
        db = load_json(DB_FILE, {})
        settings = get_portal_settings()
        unlocked = is_key_authorized(provided_key)

    summary = [
        {
            "id": mid,
            "title": info.get("title", mid),
            "match_time": info.get("match_time", "LIVE"),
            "venue": info.get("venue", ""),
            "batting_team": info.get("batting_team", "TEAM A"),
            "bowling_team": info.get("bowling_team", "TEAM B"),
            "batting_flag": info.get("batting_flag", ""),
            "bowling_flag": info.get("bowling_flag", ""),
            "bowling_score": info.get("bowling_score", "Bowling"),
            "score": info.get("score", "0/0"),
            "overs": info.get("overs", "0.0"),
            "status": info.get("match_status", ""),
        }
        for mid, info in db.items()
    ]
    return jsonify({
        "unlocked": unlocked,
        "price": settings.get("price", "$5"),
        "payment_info": settings.get("payment_info", ""),
        "design1_name": settings.get("design1_name", "Design 1 — Classic Red & Gold"),
        "design2_name": settings.get("design2_name", "Design 2 — Modern TV Broadcast"),
        "matches": summary,
    })


# --- ADMIN ROUTES ---

@app.route("/api/admin/overview", methods=["POST"])
def admin_overview():
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    with db_lock:
        db = load_json(DB_FILE, {})
        subs = load_json(SUBS_FILE, {})
        settings = get_portal_settings()

    matches = [
        {
            "id": mid,
            "title": info.get("title", mid),
            "match_time": info.get("match_time", "LIVE"),
            "venue": info.get("venue", ""),
            "bowling_score": info.get("bowling_score", ""),
            "crex_url": info.get("crex_url", ""),
            "score": info.get("score", "0/0"),
            "overs": info.get("overs", "0.0"),
        }
        for mid, info in db.items()
    ]
    users = [
        {
            "key": k,
            "name": v.get("name", "Subscriber"),
            "active": v.get("active", True),
            "note": v.get("note", "$5 Paid"),
        }
        for k, v in subs.items()
    ]
    return jsonify({"settings": settings, "matches": matches, "users": users})


@app.route("/api/admin/settings", methods=["POST"])
def update_portal_settings():
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    with db_lock:
        settings = get_portal_settings()
        if "portal_locked" in body:
            settings["portal_locked"] = bool(body["portal_locked"])
        if body.get("price"):
            settings["price"] = body["price"].strip()
        if body.get("payment_info"):
            settings["payment_info"] = body["payment_info"].strip()
        if body.get("design1_name"):
            settings["design1_name"] = body["design1_name"].strip()
        if body.get("design2_name"):
            settings["design2_name"] = body["design2_name"].strip()
        save_json(SETTINGS_FILE, settings)
    return jsonify({"ok": True, "settings": settings})


@app.route("/api/admin/upload-design", methods=["POST"])
def upload_design_html():
    password = request.form.get("password", "")
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    slot = request.form.get("slot", "design2")
    filename = "overlay.html" if slot == "design1" else "overlay2.html"
    file_obj = request.files.get("file")
    if not file_obj:
        return jsonify({"error": "No HTML file selected"}), 400

    content = file_obj.read().decode("utf-8", errors="ignore")
    if "<html" not in content.lower():
        return jsonify({"error": "Invalid HTML file"}), 400

    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    return jsonify({"ok": True, "saved_as": filename})


@app.route("/api/admin/users", methods=["POST"])
def create_subscriber():
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    name = body.get("name", "").strip() or "Paid Subscriber"
    note = body.get("note", "").strip() or "$5 Full Portal"
    custom_key = re.sub(r"[^a-zA-Z0-9_-]+", "", body.get("key", "").strip())
    user_key = custom_key or f"sub-{secrets.token_hex(3)}"

    with db_lock:
        subs = load_json(SUBS_FILE, {})
        subs[user_key] = {"name": name, "note": note, "active": True}
        save_json(SUBS_FILE, subs)

    return jsonify({"ok": True, "key": user_key})


@app.route("/api/admin/users/<user_key>/toggle", methods=["POST"])
def toggle_subscriber(user_key):
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    with db_lock:
        subs = load_json(SUBS_FILE, {})
        if user_key not in subs:
            return jsonify({"error": "User not found"}), 404
        subs[user_key]["active"] = not subs[user_key].get("active", True)
        save_json(SUBS_FILE, subs)
        return jsonify({"ok": True, "active": subs[user_key]["active"]})


@app.route("/api/admin/users/<user_key>", methods=["DELETE"])
def delete_subscriber(user_key):
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    with db_lock:
        subs = load_json(SUBS_FILE, {})
        if user_key in subs:
            del subs[user_key]
            save_json(SUBS_FILE, subs)
    return jsonify({"ok": True})


@app.route("/api/matches", methods=["POST"])
def create_or_update_match():
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    raw_slug = body.get("slug", "").strip().lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", raw_slug).strip("-")
    source_url = body.get("crex_url", "").strip()
    title = body.get("title", "").strip()
    match_time = body.get("match_time", "").strip()
    venue = body.get("venue", "").strip()
    bowling_score = body.get("bowling_score", "").strip()

    if not slug or not source_url:
        return jsonify({"error": "Match ID and Match URL are required"}), 400

    with db_lock:
        db = load_json(DB_FILE, {})
        existing = db.get(
            slug,
            default_match_payload(source_url, title, match_time, venue, bowling_score),
        )
        existing["crex_url"] = source_url
        if title:
            existing["title"] = title
        if match_time:
            existing["match_time"] = match_time
        if venue:
            existing["venue"] = venue
        if bowling_score:
            existing["bowling_score"] = bowling_score
        db[slug] = existing
        save_json(DB_FILE, db)

    threading.Thread(target=update_single_match, args=(slug,), daemon=True).start()
    return jsonify({"ok": True, "id": slug})


@app.route("/api/matches/<match_id>", methods=["DELETE"])
def delete_match(match_id):
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid admin password"}), 401

    with db_lock:
        db = load_json(DB_FILE, {})
        if match_id in db:
            del db[match_id]
            save_json(DB_FILE, db)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
