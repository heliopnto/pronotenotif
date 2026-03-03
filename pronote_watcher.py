"""
📅 Pronote Watcher — Surveillance de toute la semaine
Détecte : cours annulé, prof absent, cours déplacé, changement de salle
Notifie via Telegram (compatible CarPlay)

Variables d'environnement Render :
    PRONOTE_CREDENTIALS   → JSON credentials
    TELEGRAM_TOKEN        → token du bot BotFather
    TELEGRAM_CHAT_ID      → ton chat_id Telegram
    RENDER_API_KEY        → API Key Render
    RENDER_SERVICE_ID     → ID service Render (srv-xxxxx)
    ALREADY_NOTIFIED      → [] (géré automatiquement)
"""

import os
import json
import time
import logging
import threading
import requests
import pronotepy
from datetime import date, datetime, timedelta
from flask import Flask

# ─────────────────────────────────────────────
# ⚙️  CONFIG
# ─────────────────────────────────────────────

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
RENDER_API_KEY    = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")
CHECK_INTERVAL    = 60

JOURS_FR = {
    "Monday":    "Lundi",
    "Tuesday":   "Mardi",
    "Wednesday": "Mercredi",
    "Thursday":  "Jeudi",
    "Friday":    "Vendredi",
    "Saturday":  "Samedi",
    "Sunday":    "Dimanche",
}

MOIS_FR = {
    1: "Janvier", 2: "Février",   3: "Mars",     4: "Avril",
    5: "Mai",     6: "Juin",      7: "Juillet",  8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
}

# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# API RENDER — PERSISTENCE
# ─────────────────────────────────────────────

def update_render_env(key: str, value: str):
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        log.warning(f"Clés Render manquantes — {key} non sauvegardée.")
        return
    try:
        r = requests.put(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars/{key}",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"},
            json={"value": value},
            timeout=10
        )
        if r.status_code == 200:
            log.info(f"💾 Render env mis à jour : {key}")
        else:
            log.warning(f"Render API {key} : {r.status_code} — {r.text}")
    except requests.RequestException as e:
        log.error(f"Erreur Render ({key}) : {e}")


def load_credentials() -> dict:
    raw = os.environ.get("PRONOTE_CREDENTIALS", "")
    if not raw:
        raise ValueError("Variable d'env PRONOTE_CREDENTIALS manquante !")
    return json.loads(raw)


def save_credentials(creds: dict):
    update_render_env("PRONOTE_CREDENTIALS", json.dumps(creds))


def load_notified() -> set:
    raw = os.environ.get("ALREADY_NOTIFIED", "[]")
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def save_notified(notified: set):
    # On garde uniquement les IDs de la semaine courante
    filtered = {
        item for item in notified
        if any(item.startswith(d.strftime("%Y%m%d")) for d in get_week_days())
    }
    update_render_env("ALREADY_NOTIFIED", json.dumps(list(filtered)))


# ─────────────────────────────────────────────
# HELPERS DATE
# ─────────────────────────────────────────────

def get_week_days() -> list:
    today  = date.today()
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(5)]


def format_date_fr(d: date) -> str:
    jour = JOURS_FR.get(d.strftime("%A"), d.strftime("%A"))
    mois = MOIS_FR.get(d.month, str(d.month))
    return f"{jour} {d.day:02d} {mois}"


# ─────────────────────────────────────────────
# NOTIFICATIONS TELEGRAM
# ─────────────────────────────────────────────

def send_notification(message: str):
    """Envoie un message Telegram (HTML, emojis, CarPlay natif)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10
        )
        if r.status_code == 200:
            log.info(f"📱 Telegram envoyé : {message[:60]}...")
        else:
            log.warning(f"Telegram erreur {r.status_code} : {r.text}")
    except requests.RequestException as e:
        log.error(f"Erreur Telegram : {e}")


# ─────────────────────────────────────────────
# ANALYSE DES COURS
# ─────────────────────────────────────────────

def analyse_lesson(lesson: pronotepy.Lesson):
    """Retourne (emoji, status_label) ou None si cours normal."""
    if lesson.canceled:
        status = lesson.status or "Cours annulé"
        if "absent" in status.lower():
            return ("🔴", "Prof. absent")
        return ("🔴", "Cours annulé")
    if lesson.status:
        s = lesson.status.lower()
        if "déplacé" in s or "deplace" in s:
            return ("🟠", "Cours déplacé")
        if "remplacement" in s:
            return ("🟡", "Remplacement")
        if "salle" in s or "changement" in s:
            return ("🔵", "Changement de salle")
        return ("⚠️", lesson.status)
    return None


def check_week(client: pronotepy.Client):
    """Vérifie tous les cours lundi→vendredi et notifie les changements."""
    week_days         = get_week_days()
    already_notified  = load_notified()
    new_notifications = False

    for day in week_days:
        try:
            lessons = client.lessons(day)
        except Exception as e:
            log.error(f"Erreur récupération cours {day} : {e}")
            continue

        if not lessons:
            continue

        for lesson in lessons:
            if lesson.start is None:
                continue

            result = analyse_lesson(lesson)
            if result is None:
                continue

            emoji, status_label = result
            subject_name = lesson.subject.name if lesson.subject else "Cours inconnu"
            day_str      = day.strftime("%Y%m%d")
            start_str    = lesson.start.strftime("%H%M")
            lesson_id    = f"{day_str}_{subject_name}_{start_str}_{status_label}"

            if lesson_id in already_notified:
                log.info(f"Déjà notifié : {lesson_id}")
                continue

            already_notified.add(lesson_id)
            new_notifications = True

            date_fr   = format_date_fr(day)
            heure_str = lesson.start.strftime("%Hh%M")

            message = (
                f"{emoji} <b>Alerte — {date_fr}</b>\n"
                f"📚 {subject_name}\n"
                f"🕐 {heure_str}\n"
                f"📌 {status_label}"
            )

            send_notification(message)
            log.info(f"Notifié : {lesson_id}")

    if new_notifications:
        save_notified(already_notified)


# ─────────────────────────────────────────────
# CONNEXION
# ─────────────────────────────────────────────

def login() -> pronotepy.Client:
    log.info("Connexion à Pronote via token...")
    creds  = load_credentials()
    client = pronotepy.Client.token_login(**creds)
    if not client.logged_in:
        raise ConnectionError("Échec de connexion via token.")
    log.info(f"✅ Connecté : {client.info.name}")
    save_credentials(client.export_credentials())
    return client


# ─────────────────────────────────────────────
# BOUCLE DE SURVEILLANCE
# ─────────────────────────────────────────────

def watcher_loop():
    try:
        client = login()
    except Exception as e:
        log.critical(f"Connexion initiale impossible : {e}")
        return

    send_notification("✅ <b>Pronote Watcher actif</b>\nSurveillance de la semaine lancée.")

    fails = 0
    while True:
        try:
            log.info(f"🔍 Vérification semaine — {datetime.now().strftime('%H:%M:%S')}")
            check_week(client)
            fails = 0
        except Exception as e:
            fails += 1
            log.error(f"Erreur ({fails}/5) : {e}")
            if fails >= 5:
                try:
                    client = login()
                    fails  = 0
                except Exception as re:
                    log.critical(f"Reconnexion échouée : {re}")
                    send_notification("⚠️ <b>Pronote Watcher</b>\nReconnexion impossible. Vérification arrêtée.")
                    break
        time.sleep(CHECK_INTERVAL)


# ─────────────────────────────────────────────
# SERVEUR HTTP (requis par Render)
# ─────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index():
    return {"status": "ok", "message": "Pronote Watcher tourne"}, 200

@app.route("/health")
def health():
    return {"status": "healthy"}, 200


if __name__ == "__main__":
    threading.Thread(target=watcher_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
