#!/usr/bin/env python3
"""
Bybit BARD/USDT Order Wall Monitor
No CHAT_ID needed — anyone can /start the bot to subscribe.

SETUP:
  pip install websocket-client requests

  1. Create a bot: Telegram → @BotFather → /newbot → copy token
  2. Fill in BOT_TOKEN below
  3. python3 bybit_monitor_bot.py
  4. Open Telegram → find your bot → send /start
"""

import json, time, threading, requests, websocket, ssl, os
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
# On Railway: set BOT_TOKEN in Variables tab
# Locally: export BOT_TOKEN=your_token  or fill it in below
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

SYMBOL          = "BARDUSDT"
TARGET_PRICE    = 1.070
TARGET_SIZE_USD = 4_300_000
INTERVAL_SEC    = 60
DEPTH           = 1000
# ─────────────────────────────────────────────────────────────────────────────

WS_URL  = "wss://stream.bybit.com/v5/public/spot"
TOPIC   = f"orderbook.{DEPTH}.{SYMBOL}"

ob        = {"b": {}, "a": {}}
ob_ready  = threading.Event()
ob_lock   = threading.Lock()

# All subscribed chat IDs
subscribers     = set()
subscribers_lock = threading.Lock()

last_update_id = 0   # for Telegram long-polling


# ── Telegram ──────────────────────────────────────────────────────────────────

def tg_send(chat_id: int, text: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[TG error] chat={chat_id} {e}")

def tg_broadcast(text: str):
    with subscribers_lock:
        subs = set(subscribers)
    for chat_id in subs:
        tg_send(chat_id, text)

def tg_poll():
    """Long-poll Telegram for /start and /stop commands."""
    global last_update_id
    print("[TG] Polling for commands …")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 30},
                timeout=40
            )
            data = r.json()
            for update in data.get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text    = msg.get("text", "").strip().lower()
                name    = msg.get("chat", {}).get("first_name", "there")

                if not chat_id:
                    continue

                if text.startswith("/start"):
                    with subscribers_lock:
                        subscribers.add(chat_id)
                    print(f"[TG] +subscriber {chat_id} ({name})")
                    tg_send(chat_id,
                        f"👋 <b>Hey {name}!</b>\n\n"
                        f"You're now subscribed to the <b>BARD/USDT Wall Monitor</b>.\n\n"
                        f"📍 Watching: <b>{TARGET_PRICE} USDT</b>\n"
                        f"🔄 Alerts every <b>60 seconds</b>\n\n"
                        f"Send /stop to unsubscribe."
                    )
                elif text.startswith("/stop"):
                    with subscribers_lock:
                        subscribers.discard(chat_id)
                    print(f"[TG] -subscriber {chat_id} ({name})")
                    tg_send(chat_id, "🛑 Unsubscribed. Send /start to subscribe again.")

        except Exception as e:
            print(f"[TG poll error] {e}")
            time.sleep(5)


# ── Order book ────────────────────────────────────────────────────────────────

def apply_snapshot(data: dict):
    with ob_lock:
        ob["b"] = {p: s for p, s in data["b"]}
        ob["a"] = {p: s for p, s in data["a"]}
    ob_ready.set()
    print(f"[OB] Snapshot  bids={len(ob['b'])}  asks={len(ob['a'])}")

def apply_delta(data: dict):
    with ob_lock:
        for side in ("b", "a"):
            for price, size in data[side]:
                if size == "0":
                    ob[side].pop(price, None)
                else:
                    ob[side][price] = size

def wall_at_target():
    total_qty = 0.0
    with ob_lock:
        for side in ("b", "a"):
            for price_str, size_str in ob[side].items():
                if abs(float(price_str) - TARGET_PRICE) < 0.0001:
                    total_qty += float(size_str)
    return total_qty, TARGET_PRICE * total_qty


# ── WebSocket ─────────────────────────────────────────────────────────────────

def on_open(ws):
    print(f"[WS] Connected → {TOPIC}")
    ws.send(json.dumps({"op": "subscribe", "args": [TOPIC]}))

def on_message(ws, raw):
    msg = json.loads(raw)
    if "op" in msg:
        print(f"[WS] {msg}")
        return
    if not msg.get("topic", "").startswith("orderbook"):
        return
    mtype = msg["type"]
    data  = msg["data"]
    if mtype == "snapshot":
        apply_snapshot(data)
    elif mtype == "delta":
        if data.get("u") == 1:
            apply_snapshot(data)
        else:
            apply_delta(data)

def on_error(ws, err):  print(f"[WS] Error: {err}")
def on_close(ws, c, m): print(f"[WS] Closed ({c})"); ob_ready.clear()

def ws_loop():
    while True:
        try:
            app = websocket.WebSocketApp(WS_URL,
                on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close)
            app.run_forever(ping_interval=20, ping_timeout=10,
                            sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            print(f"[WS] Crash: {e}")
        print("[WS] Reconnecting in 5 s …"); time.sleep(5)


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt(v: float) -> str:
    if v >= 1e6:  return f"${v/1e6:.3f}M"
    if v >= 1e3:  return f"${v/1e3:.1f}K"
    return f"${v:.2f}"

def bar(cur: float, orig: float, w: int = 10) -> str:
    r = min(cur / orig, 1.0) if orig else 0
    return "🟩" * int(r * w) + "⬜" * (w - int(r * w)) + f"  {r*100:.1f}%"


# ── Alert loop ────────────────────────────────────────────────────────────────

def alert_loop():
    n         = 0
    prev_usd  = None
    sent_half = False

    print("[Bot] Waiting for snapshot …")
    ob_ready.wait()

    while True:
        qty, usd = wall_at_target()
        ts = datetime.now().strftime("%H:%M:%S")
        n += 1

        with subscribers_lock:
            sub_count = len(subscribers)

        if sub_count == 0:
            print(f"[{ts}] No subscribers yet — waiting for /start")
            time.sleep(INTERVAL_SEC)
            continue

        if usd == 0.0:
            msg = (
                f"🚨 WALL GONE\n"                          # ← top line = notification preview
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 <b>BARD/USDT @ {TARGET_PRICE} USDT</b>\n"
                f"⚡ No order found at this level!\n"
                f"Could be filled or cancelled.\n"
                f"🕐 {ts}"
            )
            print(f"[{ts}] ❌  No wall at {TARGET_PRICE}")
        else:
            delta_str = ""
            if prev_usd is not None:
                d    = usd - prev_usd
                sign = "+" if d >= 0 else ""
                delta_str = f"\n📊 Change: <b>{sign}{fmt(d)}</b>"

            msg = (
                f"💰 {fmt(usd)}  ({qty:,.0f} BARD)\n"     # ← top line = notification preview
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 <b>BARD/USDT Wall #{n} @ {TARGET_PRICE} USDT</b>"
                f"{delta_str}\n\n"
                f"{bar(usd, TARGET_SIZE_USD)}\n\n"
                f"🕐 {ts}"
            )

            if usd < TARGET_SIZE_USD * 0.5 and not sent_half:
                msg += "\n\n⚠️ <b>Wall dropped below 50%!</b>"
                sent_half = True

            prev_usd = usd
            print(f"[{ts}] Wall @ {TARGET_PRICE}: {fmt(usd)}  ({qty:,.0f} BARD)")

        tg_broadcast(msg)
        time.sleep(INTERVAL_SEC)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "YOUR_" in BOT_TOKEN:
        print("❌  Fill in BOT_TOKEN at the top of the file.")
        sys.exit(1)

    threading.Thread(target=ws_loop,  daemon=True).start()
    threading.Thread(target=tg_poll,  daemon=True).start()

    try:
        alert_loop()
    except KeyboardInterrupt:
        print("\n👋  Stopped.")
        tg_broadcast("🛑 <b>BARD/USDT Monitor stopped.</b>")
