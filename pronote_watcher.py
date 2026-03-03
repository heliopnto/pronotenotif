"""
📅 Pronote Watcher — Surveillance via API pronotepy
Détecte : cours annulé, prof absent, cours déplacé, changement de salle
Notifie via ntfy.sh (iPhone)
Credentials ET cours notifiés persistés via l'API Render

Variables d'environnement à définir sur Render :
    PRONOTE_CREDENTIALS   → JSON credentials (contenu de credentials.json)
    NTFY_TOPIC            → ton topic ntfy.sh
    RENDER_API_KEY        → Account Settings → API Keys sur Render
    RENDER_SERVICE_ID     → ID du service (ex: srv-xxxxx)
    ALREADY_NOTIFIED      → [] (laisser vide au départ, géré automatiquement)
"""

import os
import json
import time
import logging
import threading
import requests
import pronotepy
from datetime import date, datetime
from flask import Flask

# ─────────────────────────────────────────────
# ⚙️  CONFIG
# ─────────────────────────────────────────────

NTFY_TOPIC        = os.environ.get("NTFY_TOPIC", "mon_topic_ntfy")
RENDER_API_KEY    = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")
CHECK_INTERVAL    = 60

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
    """Met à jour une variable d'env sur Render via l'API."""
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
    """Charge la liste des cours déjà notifiés depuis Render."""
    raw = os.environ.get("ALREADY_NOTIFIED", "[]")
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def save_notified(notified: set):
    """
    Sauvegarde la liste sur Render.
    On filtre uniquement les IDs du jour pour ne pas faire grossir la liste.
    """
    today_str = date.today().strftime("%Y%m%d")
    filtered = {item for item in notified if today_str in item}
    update_render_env("ALREADY_NOTIFIED", json.dumps(list(filtered)))


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
    if lesson.canceled:
        status = lesson.status or "Cours annulé"
        if "absent" in status.lower():
            return ("🔴", f"Prof. absent — {status}", "urgent")
        return ("🔴", f"Cours annulé — {status}", "urgent")
    if lesson.status:
        s = lesson.status.lower()
        if "déplacé" in s or "deplace" in s:
            return ("🟠", f"Cours déplacé — {lesson.status}", "high")
        if "salle" in s or "changement" in s:
            return ("🔵", f"Changement de salle — {lesson.status}", "default")
        return ("⚠️", lesson.status, "default")
    return None


def check_cancellations(client: pronotepy.Client):
    today     = date.today()
    today_str = today.strftime("%Y%m%d")

    # Charge la liste persistée (survit aux redémarrages Render)
    already_notified = load_notified()

    try:
        lessons = client.lessons(today)
    except Exception as e:
        log.error(f"Impossible de récupérer les cours : {e}")
        return

    if not lessons:
        log.info("Aucun cours aujourd'hui.")
        return

    new_notifications = False

    for lesson in lessons:
        if lesson.start is None:
            continue

        result = analyse_lesson(lesson)
        if result is None:
            continue

        emoji, label, priority = result
        subject_name = lesson.subject.name if lesson.subject else "Cours"
        start_str    = lesson.start.strftime("%H%M")
        # La date dans l'ID évite les doublons entre jours ET entre redémarrages
        lesson_id    = f"{today_str}_{subject_name}_{start_str}_{label}"

        if lesson_id in already_notified:
            log.info(f"Déjà notifié : {lesson_id}")
            continue

        already_notified.add(lesson_id)
        new_notifications = True

        start_fmt = lesson.start.strftime("%H:%M")
        end_fmt   = lesson.end.strftime("%H:%M") if lesson.end else "?"
        teacher   = lesson.teacher_name if hasattr(lesson, "teacher_name") and lesson.teacher_name else "Prof inconnu"
        classroom = ", ".join(lesson.classrooms) if lesson.classrooms else "N/A"

        send_notification(
            title=label,
            message=(
                f"📚 {subject_name} ({start_fmt}–{end_fmt})\n"
                f"👤 {teacher}\n"
                f"🏫 Salle : {classroom}"
            ),
            priority=priority
        )
        log.info(f"Cours signalé : {lesson_id}")

    # Sauvegarde sur Render seulement si de nouvelles notifs ont été envoyées
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
    client = None
    fails  = 0
    try:
        client = login()
    except Exception as e:
        log.critical(f"Connexion initiale impossible : {e}")
        return

    send_notification("Pronote Watcher actif", "Surveillance lancée.", priority="low")

    while True:
        try:
            log.info(f"🔍 Vérification — {datetime.now().strftime('%H:%M:%S')}")
            check_cancellations(client)
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
