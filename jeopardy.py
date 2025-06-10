# jeopardy.py
#
# ⚡ Local Jeopardy game — now with:
#   • Final Jeopardy wagering screen for players (clamped server‐side)
#   • Host can close wagers and reveal final clue with a 2-minute timer
#   • Players’ Final Jeopardy answers captured at timer expiry
#   • Host‐side review of each Final Jeopardy answer with correct/incorrect
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
from time import time, sleep
import socket, os, csv
from collections import OrderedDict

# ──────────────────────────────  CONFIG  ─────────────────────────────────────
READING_TIME    = 3
FINAL_TIMER     = 12
PORT            = 5050
CSV_FILE        = "jeopardy.csv"
DEFAULT_VALUES  = [200, 400, 600, 800, 1000]
HOSTS_ROOM      = "hosts"

app = Flask(__name__, static_url_path='/static', template_folder='templates')
app.config["SECRET_KEY"] = os.urandom(16).hex()
socketio = SocketIO(app, cors_allowed_origins="*")

# ─────────────────────────────  LOAD ROUNDS  ─────────────────────────────────
def load_rounds(path):
    if not os.path.exists(path):
        return None, None
    rounds, bank = OrderedDict(), {}
    with open(path, newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            rnd = (row["round"] or "J").strip().upper()
            cat = row["category"].strip()
            val = int(row["value"] or 0)
            clue = row["clue"].strip()
            ans  = row["answer"].strip()
            img  = row.get("image","").strip()
            rounds.setdefault(rnd, OrderedDict())
            rounds[rnd].setdefault(cat, {})[val] = {
                "text": clue, "answer": ans, "image": img, "value": val
            }
    ui, bank_by_round = OrderedDict(), {}
    for rnd, cats in rounds.items():
        ui[rnd] = []
        bank_by_round[rnd] = {}
        for ci, (cat, clues) in enumerate(cats.items()):
            vals = sorted(clues)
            ui[rnd].append({"name": cat, "clues": vals})
            for ri, v in enumerate(vals):
                bank_by_round[rnd][(ci, ri)] = clues[v]
    return ui, bank_by_round

CATS_BY_ROUND, CLUE_BANK_BY_ROUND = load_rounds(CSV_FILE)
if not CATS_BY_ROUND:
    CATS_BY_ROUND = OrderedDict({
        "J": [{"name": f"Category {i+1}", "clues": DEFAULT_VALUES[:]} for i in range(3)]
    })
    CLUE_BANK_BY_ROUND = {
        "J": {
            (ci, ri): {"value": v, "text": "Placeholder", "answer": "", "image": ""}
            for ci in range(3)
            for ri, v in enumerate(DEFAULT_VALUES)
        }
    }

ROUND_ORDER       = list(CATS_BY_ROUND.keys())
current_round_idx = 0
FINAL_ROUND_IDX   = len(ROUND_ORDER) - 1

# ──────────────────────────────  GAME STATE  ────────────────────────────────
players           = {}    # sid → {"name": str, "score": int}
attempted_players = set()
buzz_open         = False
buzz_start_time   = 0.0
current_val       = 0
first_buzzer_sid  = None
host_locked       = False

used_cells_by_round = { rnd: set() for rnd in ROUND_ORDER }

# ** Final‐jeopardy wagers & answers **
wagers        = {}   # sid → wager
final_answers = {}   # sid → answer string

# ** Review queue state (host only) **
_review_list = []
_review_idx  = 0

# ─────────────────────────────  ROUTES  ─────────────────────────────────────
@app.route("/")
def player_page():
    return render_template("player.html")

@app.route("/host")
def host_page():
    r = ROUND_ORDER[current_round_idx]
    return render_template(
        "host.html",
        data=CATS_BY_ROUND[r],
        round_name=r
    )

# ──────────────────────────── SOCKET EVENTS ────────────────────────────────
@socketio.on("register_host")
def register_host():
    join_room(HOSTS_ROOM)

@socketio.on("get_used")
def send_initial_used():
    r = ROUND_ORDER[current_round_idx]
    emit("init_used", list(used_cells_by_round[r]))

@socketio.on("player_join")
def handle_join(name):
    players[request.sid] = {"name": name[:20], "score": 0}
    emit("joined", {"name": name[:20], "score": 0})
    broadcast_scores()

@socketio.on("disconnect")
def handle_disconnect():
    players.pop(request.sid, None)
    broadcast_scores()

@socketio.on("change_round")
def handle_change_round(delta):
    global current_round_idx
    if current_round_idx == FINAL_ROUND_IDX and delta < 0:
        idx = current_round_idx
    else:
        idx = current_round_idx + (1 if delta > 0 else -1)
        idx = max(0, min(FINAL_ROUND_IDX, idx))
    current_round_idx = idx
    r = ROUND_ORDER[current_round_idx]

    socketio.emit("new_board", {
        "categories": CATS_BY_ROUND[r],
        "round": r,
        "used": list(used_cells_by_round[r])
    }, room=HOSTS_ROOM)

    # If Final Jeopardy, clear prior wagers & answers, and prompt players with category
    if current_round_idx == FINAL_ROUND_IDX:
        wagers.clear()
        final_answers.clear()
        # grab the category string (only one category in final)
        final_category = next(iter(CATS_BY_ROUND[r]))["name"]
        socketio.emit("start_final_wager", {
          "duration": 60,
          "category": final_category
        })

@socketio.on("start_clue")
def handle_start_clue(data):
    global current_val, attempted_players, buzz_open, first_buzzer_sid, host_locked
    if host_locked: return
    host_locked = True
    socketio.emit("lock_board", room=HOSTS_ROOM)

    cat, row = data["cat"], data["row"]
    current_val = int(data["val"])
    attempted_players.clear()
    buzz_open = False
    first_buzzer_sid = None

    rkey = ROUND_ORDER[current_round_idx]
    used_cells_by_round[rkey].add((cat, row))
    socketio.emit("mark_used", {"cat": cat, "row": row}, room=HOSTS_ROOM)

    clue = CLUE_BANK_BY_ROUND[rkey][(cat, row)]
    socketio.emit("new_clue")
    socketio.emit("show_clue", {"text": clue["text"], "image": clue["image"]})
    socketio.emit("reading_timer", {"duration": READING_TIME})
    socketio.start_background_task(after_reading_open_buzzers)

@socketio.on("mark_used")
def passthrough_mark_used(data):
    emit("mark_used", data, room=HOSTS_ROOM)

def after_reading_open_buzzers():
    sleep(READING_TIME)
    open_buzzers()

def open_buzzers():
    global buzz_open, buzz_start_time
    buzz_open = True
    buzz_start_time = time()
    socketio.emit("open_buzz")

@socketio.on("buzz")
def handle_buzz():
    global buzz_open, first_buzzer_sid
    sid = request.sid
    if not buzz_open or sid in attempted_players: return
    first_buzzer_sid = sid
    buzz_open = False
    delta = round(time() - buzz_start_time, 3)
    socketio.emit("close_buzz")
    socketio.emit("player_buzzed", {
        "name": players[sid]["name"],
        "time": f"{delta:.3f}"
    })

@socketio.on("judge")
def handle_judge(data):
    global first_buzzer_sid, buzz_open, host_locked
    if not first_buzzer_sid: return
    correct = bool(data.get("correct"))
    pname = players[first_buzzer_sid]["name"]
    if correct:
        players[first_buzzer_sid]["score"] += current_val
        socketio.emit("info", f"{pname} gained {current_val} points.")
        end_clue()
    else:
        players[first_buzzer_sid]["score"] -= current_val
        attempted_players.add(first_buzzer_sid)
        socketio.emit("info", f"{pname} lost {current_val} points.")
        if len(attempted_players) < len(players):
            open_buzzers()
        else:
            end_clue()

@socketio.on("skip_clue")
def skip_clue():
    end_clue()

@socketio.on("adjust_score")
def handle_adjust(data):
    sid   = data.get("sid")
    delta = data.get("delta", 0)
    if sid in players:
        players[sid]["score"] += delta
        socketio.emit("info", f"{players[sid]['name']} {('+' if delta>=0 else '')}{delta}")
        broadcast_scores()

@socketio.on("submit_wager")
def handle_submit_wager(data):
    sid = request.sid
    try:
        raw = int(data.get("wager", 0))
    except (ValueError, TypeError):
        raw = 0
    score    = players.get(sid, {}).get("score", 0)
    max_wager= max(score, 100)
    wager    = max(0, min(raw, max_wager))
    wagers[sid] = wager
    emit("wager_received", {"wager": wager})

@socketio.on("close_wagers")
def handle_close_wagers():
    for sid in players:
        wagers.setdefault(sid, 0)
        final_answers.setdefault(sid, "")
    r = ROUND_ORDER[current_round_idx]
    final_clue = next(iter(CLUE_BANK_BY_ROUND[r].values()))
    socketio.emit("final_clue", {
        "text":  final_clue["text"],
        "image": final_clue["image"]
    })
    socketio.emit("final_timer", {"duration": FINAL_TIMER})
    socketio.start_background_task(_notify_review_ready)

def _notify_review_ready():
    sleep(FINAL_TIMER)
    socketio.emit("enable_review", room=HOSTS_ROOM)

@socketio.on("submit_answer")
def handle_submit_answer(data):
    final_answers[request.sid] = data.get("answer", "").strip()
    emit("answer_received")

@socketio.on("start_answer_review")
def handle_start_answer_review():
    global _review_list, _review_idx
    _review_list = [(sid, final_answers[sid], wagers.get(sid,0)) for sid in final_answers]
    _review_idx = 0
    _send_review()

@socketio.on("judge_final_answer")
def handle_judge_final_answer(data):
    global _review_idx
    sid     = data["sid"]
    correct = bool(data["correct"])
    w       = wagers.get(sid, 0)
    if correct:
        players[sid]["score"] += w
    else:
        players[sid]["score"] -= w
    socketio.emit("review_outcome", {"sid": sid}, room=HOSTS_ROOM)
    broadcast_scores()
    _review_idx += 1
    _send_review()

def _send_review():
    if _review_idx >= len(_review_list):
        socketio.emit("end_review", room=HOSTS_ROOM)
        return
    sid, ans, w = _review_list[_review_idx]
    name = players.get(sid,{}).get("name","Unknown")
    socketio.emit("review_answer", {
        "sid": sid, "name": name, "answer": ans, "wager": w
    }, room=HOSTS_ROOM)

def end_clue():
    global first_buzzer_sid, attempted_players, buzz_open, host_locked
    first_buzzer_sid = None
    attempted_players.clear()
    buzz_open = False
    host_locked = False
    socketio.emit("unlock_board", room=HOSTS_ROOM)
    broadcast_scores()

def broadcast_scores():
    lst = [{"sid":sid,"name":p["name"],"score":p["score"]} for sid,p in players.items()]
    lst.sort(key=lambda x: -x["score"])
    socketio.emit("scores", lst)

if __name__ == "__main__":
    host_ip = socket.gethostbyname(socket.gethostname())
    print(f"★ Jeopardy server running at http://{host_ip}:{PORT}")
    socketio.run(app, host="0.0.0.0", port=PORT)
