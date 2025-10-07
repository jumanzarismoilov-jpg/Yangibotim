#!/usr/bin/env python3
# coding: utf-8
"""
telegram_reward_bot.py
Comprehensive Telegram reward bot with:
- Reply keyboard (user main menu)
- Inline keyboards (ads/tasks claim, admin approve/reject)
- Referral system (each successful referral -> +4.00 rubl)
- /transfer to send money by Telegram ID
- /order -> saved and posted to OWNER_CHANNEL
- /bonus daily (0.5 - 5.0 rubl)
- Admin asset registry & create_ad flow
- Claim workflow: user clicks "Claim" -> bot asks for proof via /proof <claim_id> with photo -> admin approves -> reward
- SQLite storage
Run: python3 telegram_reward_bot.py
"""
import time, requests, sqlite3, json, random, html
from datetime import datetime, timedelta
from threading import Thread

# ========== CONFIG ==========
TOKEN = "REPLACE_WITH_YOUR_BOT_TOKEN"
API = f"https://api.telegram.org/bot{TOKEN}"
ADMIN_IDS = [123456789]        # <- set admin numeric IDs
OWNER_CHANNEL = "@YourChannel" # <- where orders/ads will be posted
REQUIRED_CHANNELS = ["@ExampleChannel"]  # optional
MEMBERSHIP_CHECK_INTERVAL = 10 * 60
DB_FILE = "bot_data.db"
# ============================

# ========== DB ==========
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance REAL DEFAULT 0.0,
    last_bonus TEXT,
    streak INTEGER DEFAULT 0,
    referrer_id INTEGER
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user INTEGER,
    to_user INTEGER,
    amount REAL,
    created_at TEXT,
    type TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    text TEXT,
    created_at TEXT,
    status TEXT DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS partners (
    chat_id TEXT PRIMARY KEY,
    title TEXT,
    owner_id INTEGER,
    collected REAL DEFAULT 0.0,
    is_partner INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    partner_chat_id TEXT,
    rewarded_at TEXT,
    amount REAL,
    active INTEGER DEFAULT 1,
    UNIQUE(user_id, partner_chat_id)
);
CREATE TABLE IF NOT EXISTS media_assets (
    id TEXT PRIMARY KEY,
    type TEXT,
    title TEXT,
    owner_id INTEGER,
    is_ad_enabled INTEGER DEFAULT 0,
    is_required_subscribe INTEGER DEFAULT 0,
    reward_amount REAL DEFAULT 0.0,
    penalty_amount REAL DEFAULT 0.0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS ads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT,
    creator_id INTEGER,
    price_total REAL,
    count_workers INTEGER,
    text TEXT,
    created_at TEXT,
    status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id INTEGER,
    user_id INTEGER,
    status TEXT DEFAULT 'pending',
    proof_file_id TEXT,
    created_at TEXT
);
""")
conn.commit()

# ========== Helpers ==========
def api_request(method, params=None, files=None):
    url = f"{API}/{method}"
    try:
        if params is None:
            r = requests.get(url, timeout=15)
        else:
            r = requests.post(url, data=params, files=files, timeout=20)
        return r.json()
    except Exception as e:
        print("API error", e)
        return None

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return api_request("sendMessage", data)

def answer_callback(callback_id, text=None, show_alert=False):
    api_request("answerCallbackQuery", {"callback_query_id": callback_id, "text": text or "", "show_alert": show_alert})

def get_user_balance(uid):
    cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone()
    return r[0] if r else 0.0

def ensure_user(uid, username=None):
    cur.execute("INSERT OR IGNORE INTO users(user_id, username) VALUES(?,?)", (uid, username))
    if username:
        cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
    conn.commit()

def change_balance(uid, delta):
    ensure_user(uid)
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, uid))
    conn.commit()

def create_transaction(from_user, to_user, amount, ttype="transfer", note=None):
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT INTO transactions(from_user,to_user,amount,created_at,type,note) VALUES(?,?,?,?,?,?)",
                (from_user, to_user, amount, now, ttype, note))
    conn.commit()
    return cur.lastrowid

# ========== Keyboards ==========
def user_reply_keyboard():
    kb = [
        ["🎁 Kunlik bonus", "👥 Referal"],
        ["💰 Balans", "🧾 Buyurtmalar"],
        ["🎮 O‘yinlar", "📢 Reklama joylash"],
        ["⚙️ Sozlamalar"]
    ]
    return {"keyboard": kb, "resize_keyboard": True}

def admin_reply_keyboard():
    kb = [
        ["📊 Statistika", "➕ Reklama qo‘shish"],
        ["🎦 YouTube / Shorts qo‘shish", "📸 Instagram qo‘shish"],
        ["📢 Kanallarni boshqarish", "💵 Balanslar"],
        ["🧾 Tranzaksiyalar", "🔙 Orqaga"]
    ]
    return {"keyboard": kb, "resize_keyboard": True}

def make_inline(buttons_rows):
    # buttons_rows: list of lists of (text, callback_data)
    ik = {"inline_keyboard": [[{"text": t, "callback_data": d} for (t,d) in row] for row in buttons_rows]}
    return ik

# ========== Core Flows ==========
def handle_start(uid, username, args):
    ensure_user(uid, username)
    # referral handling: /start ref123 or /start 123
    if args:
        code = args[0]
        try:
            if code.startswith("ref"):
                ref_id = int(code.replace("ref",""))
            else:
                ref_id = int(code)
            if ref_id != uid:
                cur.execute("SELECT referrer_id FROM users WHERE user_id=?", (uid,))
                r = cur.fetchone()
                if not r or not r[0]:
                    cur.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (ref_id, uid))
                    conn.commit()
                    change_balance(ref_id, 4.0)
                    create_transaction(ref_id, uid, 4.0, ttype="referral", note="ref bonus")
                    send_message(ref_id, f"🎉 Siz yangi foydalanuvchi taklif qildingiz — +4.00 rubl!")
        except:
            pass
    # required channels check
    not_sub = []
    for ch in REQUIRED_CHANNELS:
        m = api_request("getChatMember", {"chat_id": ch, "user_id": uid})
        if not m or not m.get("ok") or m["result"].get("status") not in ("member","creator","administrator"):
            not_sub.append(ch)
    if not_sub:
        links = "\n".join([f"https://t.me/{c.lstrip('@')}" for c in not_sub])
        send_message(uid, f"Botni ishlatish uchun quyidagi kanallarga obuna bo'ling va /start yuboring:\n{links}")
        return
    # welcome + show main keyboard
    send_message(uid, f"Assalomu alaykum, <b>{html.escape(username or str(uid))}</b>! 👋\nQuyidagi menyudan tanlang:", reply_markup=user_reply_keyboard())

def handle_bonus(uid):
    cur.execute("SELECT last_bonus FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone()
    now = datetime.utcnow()
    if r and r[0]:
        last = datetime.fromisoformat(r[0])
        if now - last < timedelta(hours=24):
            send_message(uid, "Siz bugun allaqachon bonus olgansiz. Keyingi bonus uchun 24 soat kuting.")
            return
    amount = round(random.uniform(0.5, 5.0), 2)
    cur.execute("UPDATE users SET last_bonus=?, streak=COALESCE(streak,0)+1 WHERE user_id=?", (now.isoformat(), uid))
    change_balance(uid, amount)
    conn.commit()
    send_message(uid, f"🎁 Kunlik bonus: +{amount:.2f} rubl.\nSizning yangi balansingiz: {get_user_balance(uid):.2f} rubl")

def handle_ref(uid):
    ensure_user(uid)
    # create a simple ref code "ref<uid>"
    code = f"ref{uid}"
    link = f"https://t.me/{get_bot_username()}?start={code}"
    send_message(uid, f"🔗 Sizning referal linkingiz:\n{link}\nHar bir yangi taklif uchun: +4.00 rubl")

# helper to fetch bot username once
_bot_username_cache = None
def get_bot_username():
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    r = api_request("getMe")
    if r and r.get("ok"):
        _bot_username_cache = r["result"]["username"]
        return _bot_username_cache
    return "YourBot"

def handle_balance(uid):
    bal = get_user_balance(uid)
    send_message(uid, f"💰 Sizning balans: {bal:.2f} rubl")

def handle_order(uid, username, text):
    if not text:
        send_message(uid, "Foydalanish: /order <buyurtma matni>")
        return
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT INTO orders(user_id,username,text,created_at) VALUES(?,?,?,?)", (uid, username, text, now))
    conn.commit()
    oid = cur.lastrowid
    # post to owner channel
    msg = f"📦 <b>Yangi buyurtma</b>\nID: {oid}\nFoydalanuvchi: @{username if username else uid}\nVaqt (UTC): {now}\nMatn: {html.escape(text)}"
    resp = send_message(OWNER_CHANNEL, msg)
    if resp and resp.get("ok"):
        send_message(uid, f"Sizning buyurtmangiz qabul qilindi. ID: {oid}")
    else:
        send_message(uid, f"Buyurtma saqlandi (ID:{oid}), lekin kanalga yuborib bo'lmadi. Iltimos, botni OWNER_CHANNEL ga admin qiling.")

def handle_transfer(sender_id, parts):
    # /transfer <to_id> <amount>
    if len(parts) < 3:
        send_message(sender_id, "Foydalanish: /transfer <to_user_id> <amount>")
        return
    try:
        to_id = int(parts[1])
        amount = round(float(parts[2]), 2)
    except:
        send_message(sender_id, "Notog'ri format. ID va summa tekshiring.")
        return
    if amount <= 0:
        send_message(sender_id, "Summa 0 dan katta bo'lishi kerak.")
        return
    if to_id == sender_id:
        send_message(sender_id, "O'zingizga yuborolmaysiz.")
        return
    bal = get_user_balance(sender_id)
    if bal < amount:
        send_message(sender_id, f"Yetarli balans yo'q. Sizda: {bal:.2f} rubl")
        return
    ensure_user(to_id)
    change_balance(sender_id, -amount)
    change_balance(to_id, amount)
    create_transaction(sender_id, to_id, amount, ttype="transfer", note="user transfer")
    send_message(sender_id, f"✅ Siz {to_id} ga {amount:.2f} rubl yubordingiz.\nYangi balans: {get_user_balance(sender_id):.2f} rubl")
    # notify recipient if bot can
    try:
        send_message(to_id, f"💸 Sizga {amount:.2f} rubl {sender_id} tomonidan yuborildi.\nYangi balans: {get_user_balance(to_id):.2f} rubl")
    except:
        pass

# ========== Admin & Media / Ads ==========
def admin_keyboard_for(uid):
    if uid in ADMIN_IDS:
        return admin_reply_keyboard()
    return None

def add_asset(admin_id, parts):
    # /add_asset TYPE id title reward penalty required(yes/no)
    if len(parts) < 6:
        send_message(admin_id, "Foydalanish: /add_asset <TYPE> <ID> <title> <reward> <penalty> <required(yes/no)>")
        return
    typ, aid, title, reward_s, penalty_s, req = parts[1:7]
    try:
        reward = float(reward_s)
        penalty = float(penalty_s)
    except:
        send_message(admin_id, "Reward va penalty raqam bo'lishi kerak.")
        return
    req_flag = 1 if req.lower() in ("yes","y","ha","true","1") else 0
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT OR REPLACE INTO media_assets(id,type,title,owner_id,is_ad_enabled,is_required_subscribe,reward_amount,penalty_amount,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (aid, typ, title, admin_id, 1, req_flag, reward, penalty, now))
    conn.commit()
    send_message(admin_id, f"Asset qo'shildi: {aid} ({typ})")

def list_assets(admin_id):
    cur.execute("SELECT id,type,title,is_ad_enabled,is_required_subscribe,reward_amount,penalty_amount FROM media_assets")
    rows = cur.fetchall()
    if not rows:
        send_message(admin_id, "Hech qanday asset yo'q.")
        return
    lines = []
    for r in rows:
        lines.append(f"{r[0]} [{r[1]}] {r[2]} - reward:{r[5]:.2f} penalty:{r[6]:.2f} required:{'yes' if r[4] else 'no'}")
    send_message(admin_id, "\n".join(lines))

def create_ad(admin_id, parts):
    # /create_ad <asset_id> <price_total> <count_workers> <text...>
    if len(parts) < 5:
        send_message(admin_id, "Foydalanish: /create_ad <asset_id> <price_total> <count_workers> <text>")
        return
    aid = parts[1]
    try:
        price = float(parts[2])
        cnt = int(parts[3])
    except:
        send_message(admin_id, "price yoki count noto'g'ri.")
        return
    text = " ".join(parts[4:])
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT INTO ads(asset_id,creator_id,price_total,count_workers,text,created_at) VALUES(?,?,?,?,?,?)",
                (aid, admin_id, price, cnt, text, now))
    conn.commit()
    aid_db = cur.lastrowid
    # post to owner channel with inline "Claim" buttons
    # create inline rows: each worker will press Claim which creates a claim record
    ik = make_inline([ [("✅ Bajarildi", f"claim:{aid_db}"), ("❌ Bekor", f"cancelad:{aid_db}")] ])
    post = f"📢 <b>Yangi reklama/topshiriq</b>\nID: {aid_db}\nAsset: {aid}\nTo'lov: {price:.2f}\nWorker limit: {cnt}\n\n{text}"
    send_message(OWNER_CHANNEL, post, reply_markup=ik)
    send_message(admin_id, f"Ad yaratildi va OWNER_CHANNEL ga joylandi. ID: {aid_db}")

# ========== Claims & Proof ==========
def create_claim(ad_id, user_id):
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT INTO claims(ad_id,user_id,created_at) VALUES(?,?,?)", (ad_id, user_id, now))
    conn.commit()
    cid = cur.lastrowid
    # ask user to send proof via /proof <cid> and attach photo
    send_message(user_id, f"Siz claim yaratdingiz. Iltimos, tasdiqlash uchun screenshot yuboring (YouTube/Instagram proof). Keyin quyidagicha yuboring:\n/proof {cid}\n\nRasm yuborilgach, adminlar tekshiradi.")
    # notify admins that claim created
    for aid in ADMIN_IDS:
        send_message(aid, f"🔔 New claim #{cid} for ad {ad_id} by user {user_id}. Wait for proof. You can approve with inline buttons when proof arrives.")
    return cid

def attach_proof_and_notify(user_id, cid, file_id):
    # attach file_id to claim
    cur.execute("UPDATE claims SET proof_file_id=? WHERE id=?", (file_id, cid))
    conn.commit()
    # notify admins with approve/reject buttons and show file_id
    # We cannot embed file in admin message easily without download/upload; we send file_id (admins can open in Telegram client)
    ik = make_inline([[("✔️ Tasdiqlash", f"approve:{cid}"), ("🚫 Rad etish", f"reject:{cid}")]])
    for aid in ADMIN_IDS:
        send_message(aid, f"🖼️ Claim #{cid} uchun proof ilova qo'shildi.\nUser: {user_id}\nFile ID: {file_id}\n\nInline tugmalar bilan tasdiqlang yoki rad eting.", reply_markup=ik)
    send_message(user_id, "📨 Proof qabul qilindi. Adminlar tekshiradi. Sizga xabar keladi.")

def approve_claim(cid, approver_id):
    cur.execute("SELECT ad_id,user_id,proof_file_id,status FROM claims WHERE id=?", (cid,))
    r = cur.fetchone()
    if not r:
        return False, "Claim topilmadi."
    ad_id, user_id, proof, status = r
    if status != "pending":
        return False, "Claim allaqachon qayta ishlangan."
    # fetch ad and asset to know reward
    cur.execute("SELECT asset_id FROM ads WHERE id=?", (ad_id,))
    ad = cur.fetchone()
    if not ad:
        return False, "Ad topilmadi."
    asset_id = ad[0]
    cur.execute("SELECT reward_amount FROM media_assets WHERE id=?", (asset_id,))
    asset = cur.fetchone()
    reward = asset[0] if asset else 0.0
    # mark claim approved, pay user from ad budget? For simplicity we credit reward from system (admin must ensure ad paid)
    cur.execute("UPDATE claims SET status='approved' WHERE id=?", (cid,))
    change_balance(user_id, float(reward))
    create_transaction(0, user_id, float(reward), ttype="claim_reward", note=f"claim:{cid}")
    conn.commit()
    send_message(user_id, f"✅ Sizning claim #{cid} tasdiqlandi. +{reward:.2f} rubl hisobingizga qo'shildi.")
    send_message(approver_id, f"Claim #{cid} tasdiqlandi va userga +{reward:.2f} rubl berildi.")
    return True, "ok"

def reject_claim(cid, approver_id):
    cur.execute("SELECT user_id,status FROM claims WHERE id=?", (cid,))
    r = cur.fetchone()
    if not r:
        return False, "Claim topilmadi."
    user_id, status = r
    if status != "pending":
        return False, "Claim allaqachon qayta ishlangan."
    cur.execute("UPDATE claims SET status='rejected' WHERE id=?", (cid,))
    conn.commit()
    send_message(user_id, f"❌ Sizning claim #{cid} rad etildi. Iltimos, qayta urinib ko'ring yoki admin bilan bog'laning.")
    send_message(approver_id, f"Claim #{cid} rad etildi.")
    return True, "ok"

# ========== Polling ==========

offset_file = "offset.txt"
def load_offset():
    try:
        with open(offset_file,"r") as f:
            return int(f.read().strip())
    except:
        return None

def save_offset(o):
    with open(offset_file,"w") as f:
        f.write(str(o))

def handle_update(u):
    if "message" in u:
        m = u["message"]
        uid = m["from"]["id"]
        username = m["from"].get("username") or m["from"].get("first_name")
        chat_id = m["chat"]["id"]
        text = m.get("text","")
        # photo handling for proof: if user sends photo and previous step included /proof <cid> in their text, we process
        if "photo" in m and text.startswith("/proof"):
            parts = text.split()
            if len(parts) >= 2:
                try:
                    cid = int(parts[1])
                    # get largest file_id
                    file_id = m["photo"][-1]["file_id"]
                    attach_proof_and_notify(uid, cid, file_id)
                except:
                    send_message(uid, "Notog'ri /proof format. /proof <claim_id> (matn bilan birga rasm yuboring)")
            else:
                send_message(uid, "Foydalanish: /proof <claim_id> (rasmni shu message bilan birga yuboring)")
            return
        # commands and menu
        if text.startswith("/start"):
            parts = text.split()
            args = parts[1:] if len(parts)>1 else None
            handle_start(uid, username, args)
            return
        if text == "🎁 Kunlik bonus" or text.startswith("/bonus"):
            handle_bonus(uid)
            return
        if text == "👥 Referal" or text.startswith("/ref"):
            handle_ref(uid)
            return
        if text == "💰 Balans" or text.startswith("/balance"):
            handle_balance(uid)
            return
        if text.startswith("/order") or text == "🧾 Buyurtmalar":
            if text.startswith("/order"):
                parts = text.split(maxsplit=1)
                body = parts[1] if len(parts)>1 else ""
                handle_order(uid, username, body)
            else:
                # list user's orders
                cur.execute("SELECT id,text,created_at,status FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (uid,))
                rows = cur.fetchall()
                if not rows:
                    send_message(uid, "Sizda buyurtma yo'q.")
                else:
                    lines = [f"ID:{r[0]} {r[3]} {r[2]}\n{r[1]}" for r in rows]
                    send_message(uid, "\n\n".join(lines))
            return
        if text.startswith("/transfer") or text.startswith("/pay"):
            parts = text.split()
            handle_transfer(uid, parts)
            return
        if text.startswith("/order "):
            parts = text.split(maxsplit=1)
            handle_order(uid, username, parts[1] if len(parts)>1 else "")
            return
        # admin commands
        if uid in ADMIN_IDS:
            if text.startswith("/add_asset"):
                add_asset(uid, text.split())
                return
            if text.startswith("/list_assets"):
                list_assets(uid)
                return
            if text.startswith("/create_ad"):
                create_ad(uid, text.split())
                return
            if text.startswith("/admin") or text == "🔙 Orqaga":
                send_message(uid, "Admin panel:", reply_markup=admin_reply_keyboard())
                return
            if text == "📊 Statistika":
                cur.execute("SELECT COUNT(*) FROM users")
                users_count = cur.fetchone()[0]
                cur.execute("SELECT SUM(balance) FROM users")
                total_bal = cur.fetchone()[0] or 0.0
                send_message(uid, f"📈 Users: {users_count}\n💰 Total balance: {total_bal:.2f}")
                return
            if text == "🧾 Tranzaksiyalar" or text.startswith("/transactions"):
                cur.execute("SELECT id,from_user,to_user,amount,created_at,type FROM transactions ORDER BY created_at DESC LIMIT 30")
                rows = cur.fetchall()
                lines = [f"{r[0]} {r[5]} {r[3]:.2f} from:{r[1]} to:{r[2]} at:{r[4]}" for r in rows]
                send_message(uid, "\n".join(lines) if lines else "Hech narsa")
                return
        # interactive reply help
        if text == "🎮 O‘yinlar":
            send_message(uid, "O‘yinlar hali kichik to'plam: /slot va /guess (tekin demo).", reply_markup=user_reply_keyboard())
            return
        if text == "📢 Reklama joylash":
            send_message(uid, "Reklama yaratish: /create_ad <asset_id> <price_total> <count_workers> <text>\n(Adminlar e'lon qilishi mumkin).")
            return
        # fallback
        send_message(chat_id, "Buyruqlar: /start, /balance, /transfer, /order, /bonus, /ref\nSiz menyudan tanlang:", reply_markup=user_reply_keyboard())
    elif "callback_query" in u:
        cq = u["callback_query"]
        data = cq.get("data","")
        uid = cq["from"]["id"]
        cid = cq["id"]
        # claim:<ad_id>
        if data.startswith("claim:"):
            ad_id = int(data.split(":",1)[1])
            # create claim and ask for proof
            create_claim(ad_id, uid)
            answer_callback(cid, "Claim yaratilmoqda. Sizga shaxsiy xabar yuborildi.")
            return
        if data.startswith("cancelad:"):
            ad_id = int(data.split(":",1)[1])
            answer_callback(cid, "Ad cancel requested (admins only).")
            # only admins can cancel: notify admins
            for aid in ADMIN_IDS:
                send_message(aid, f"User {uid} requested cancel for ad {ad_id}.")
            return
        if data.startswith("approve:"):
            claim_id = int(data.split(":",1)[1])
            ok,msg = approve_claim(claim_id, uid)
            answer_callback(cid, msg)
            return
        if data.startswith("reject:"):
            claim_id = int(data.split(":",1)[1])
            ok,msg = reject_claim(claim_id, uid)
            answer_callback(cid, msg)
            return
        answer_callback(cid, "Unknown action.")

def polling_loop():
    offset = load_offset()
    print("Polling started. Offset:", offset)
    while True:
        try:
            params = {"timeout": 30, "offset": offset} if offset else {"timeout":30}
            resp = requests.get(f"{API}/getUpdates", params=params, timeout=40)
            data = resp.json()
            if not data.get("ok"):
                time.sleep(2); continue
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                save_offset(offset)
                try:
                    handle_update(u)
                except Exception as e:
                    print("handle_update error:", e)
        except Exception as e:
            print("polling error", e)
            time.sleep(5)

# ========== Background membership/housekeeping ==========
def membership_check_loop():
    while True:
        try:
            # check rewards: if user left partner -> penalty (simplified)
            cur.execute("SELECT id,user_id,partner_chat_id FROM rewards WHERE active=1")
            rows = cur.fetchall()
            for rid, uid, pchat in rows:
                try:
                    r = api_request("getChatMember", {"chat_id": pchat, "user_id": uid})
                    if not r or not r.get("ok") or r["result"].get("status") in ("left","kicked"):
                        cur.execute("UPDATE rewards SET active=0 WHERE id=?", (rid,))
                        change_balance(uid, -2.0)
                        conn.commit()
                        send_message(uid, f"Siz {pchat} guruhidan chiqqansiz — 2.0 rubl jarima olindi.")
                except:
                    pass
        except Exception as e:
            print("membership err", e)
        time.sleep(MEMBERSHIP_CHECK_INTERVAL)

# start threads
Thread(target=membership_check_loop, daemon=True).start()
if __name__ == "__main__":
    polling_loop()
