"""
📅 Pronote Watcher — Surveillance de toute la semaine
Détecte : cours annulé, prof absent, cours déplacé, changement de salle
Notifie via ntfy.sh avec format : Jour Date / Cours / Status / Heure

Variables d'environnement Render :
    PRONOTE_CREDENTIALS   → JSON credentials
    NTFY_TOPIC            → topic ntfy.sh
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

NTFY_TOPIC        = os.environ.get("NTFY_TOPIC", "mon_topic_ntfy")
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
    # On garde uniquement les IDs de la semaine en cours
    monday = get_week_days()[0].strftime("%Y%m%d")
    friday = get_week_days()[-1].strftime("%Y%m%d")
    filtered = {
        item for item in notified
        if any(item.startswith(d.strftime("%Y%m%d")) for d in get_week_days())
    }
    update_render_env("ALREADY_NOTIFIED", json.dumps(list(filtered)))


# ─────────────────────────────────────────────
# HELPERS DATE
# ─────────────────────────────────────────────

def get_week_days() -> list:
    """Retourne la liste des jours lundi→vendredi de la semaine courante."""
    today  = date.today()
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(5)]


def format_date_fr(d: date) -> str:
    """Ex: Lundi 03 Mars"""
    jour = JOURS_FR.get(d.strftime("%A"), d.strftime("%A"))
    mois = MOIS_FR.get(d.month, str(d.month))
    return f"{jour} {d.day:02d} {mois}"


# ─────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────

PRIORITY_MAP = {
    "urgent":  "5",
    "high":    "4",
    "default": "3",
    "low":     "2",
    "min":     "1",
}

def send_notification(title: str, message: str, priority: str = "high"):
    try:
        safe_title    = title.encode("ascii", "ignore").decode().strip()
        ntfy_priority = PRIORITY_MAP.get(priority, "3")
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": safe_title, "Priority": ntfy_priority, "Tags": "warning,school"},
            timeout=10
        )
        if r.status_code == 200:
            log.info(f"📱 Notif envoyée : {title}")
        else:
            log.warning(f"ntfy.sh erreur {r.status_code} : {r.text}")
    except requests.RequestException as e:
        log.error(f"Erreur ntfy.sh : {e}")


# ─────────────────────────────────────────────
# ANALYSE DES COURS
# ─────────────────────────────────────────────

def analyse_lesson(lesson: pronotepy.Lesson):
    """Retourne (emoji, status_label, priorité) ou None si cours normal."""
    if lesson.canceled:
        status = lesson.status or "Cours annulé"
        if "absent" in status.lower():
            return ("🔴", "Prof. absent", "urgent")
        return ("🔴", "Cours annulé", "urgent")
    if lesson.status:
        s = lesson.status.lower()
        if "déplacé" in s or "deplace" in s:
            return ("🟠", f"Cours déplacé", "high")
        if "remplacement" in s:
            return ("🟡", f"Remplacement", "high")
        if "salle" in s or "changement" in s:
            return ("🔵", f"Changement de salle", "default")
        # Autre statut Pronote inconnu
        return ("⚠️", lesson.status, "default")
    return None


def check_week(client: pronotepy.Client):
    """Vérifie tous les cours de la semaine et notifie les changements."""
    week_days        = get_week_days()
    already_notified = load_notified()
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

            emoji, status_label, priority = result
            subject_name = lesson.subject.name if lesson.subject else "Cours inconnu"
            day_str      = day.strftime("%Y%m%d")
            start_str    = lesson.start.strftime("%H%M")
            lesson_id    = f"{day_str}_{subject_name}_{start_str}_{status_label}"

            if lesson_id in already_notified:
                log.info(f"Déjà notifié : {lesson_id}")
                continue

            already_notified.add(lesson_id)
            new_notifications = True

            # Format de la notif
            date_fr   = format_date_fr(day)
            heure_str = lesson.start.strftime("%Hh%M")

            title = f"{emoji} Alerte {date_fr}"
            message = (
                f"{emoji} {status_label}\n"
                f"📚 {subject_name}\n"
                f"🕐 {heure_str}"
            )

            send_notification(title, message, priority)
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

    send_notification("Pronote Watcher actif", "Surveillance de la semaine lancée.", priority="low")

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
                    send_notification("Pronote Watcher", "Reconnexion impossible.", priority="urgent")
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
