"""
📅 Pronote Watcher — Surveillance via API pronotepy
Détecte : cours annulé, prof absent, cours déplacé, changement de salle
Notifie via ntfy.sh (iPhone)
Credentials persistés via l'API Render (survit aux redémarrages)

Installation :
    pip install pronotepy requests flask

Variables d'environnement à définir sur Render :
    PRONOTE_CREDENTIALS   → le JSON credentials (copie le contenu de credentials.json)
    NTFY_TOPIC            → ton topic ntfy.sh
    RENDER_API_KEY        → ta clé API Render (dashboard → Account Settings → API Keys)
    RENDER_SERVICE_ID     → l'ID de ton service Render (ex: srv-xxxxx)
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
# ⚙️  CONFIG — Tout vient des variables d'env Render
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

already_notified: set = set()

# ─────────────────────────────────────────────
# PERSISTENCE DES CREDENTIALS VIA API RENDER
# ─────────────────────────────────────────────

def load_credentials() -> dict:
    """Charge les credentials depuis la variable d'env PRONOTE_CREDENTIALS."""
    raw = os.environ.get("PRONOTE_CREDENTIALS", "")
    if not raw:
        raise ValueError("Variable d'env PRONOTE_CREDENTIALS manquante !")
    return json.loads(raw)


def save_credentials_to_render(creds: dict):
    """
    Met à jour la variable PRONOTE_CREDENTIALS sur Render via l'API.
    Le token renouvelé persiste ainsi après redémarrage.
    """
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        log.warning("RENDER_API_KEY ou RENDER_SERVICE_ID manquant — credentials non sauvegardés sur Render.")
        return

    try:
        response = requests.put(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars/PRONOTE_CREDENTIALS",
            headers={
                "Authorization": f"Bearer {RENDER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"value": json.dumps(creds)},
            timeout=10
        )
        if response.status_code == 200:
            log.info("💾 Credentials mis à jour sur Render.")
        else:
            log.warning(f"Render API : {response.status_code} — {response.text}")
    except requests.RequestException as e:
        log.error(f"Erreur sauvegarde Render : {e}")


# ─────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────

def send_notification(title: str, message: str, priority: str = "high"):
    """Envoie une notification push via ntfy.sh."""
    try:
        safe_title = title.encode("ascii", "ignore").decode()
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": safe_title,
                "Priority": priority,
                "Tags": "school,warning",
            },
            timeout=10
        )
        log.info(f"📱 Notif envoyée : {title}")
    except requests.RequestException as e:
        log.error(f"Erreur ntfy.sh : {e}")


# ─────────────────────────────────────────────
# ANALYSE DES COURS
# ─────────────────────────────────────────────

def analyse_lesson(lesson: pronotepy.Lesson):
    """Retourne (emoji, label, priorité) si le cours a un changement, sinon None."""

    if lesson.canceled:
        status = lesson.status or "Cours annulé"
        if "absent" in status.lower():
            return ("🔴", f"Prof. absent — {status}", "urgent")
        else:
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
    """Récupère les cours du jour et notifie les changements."""
    today = date.today()

    try:
        lessons = client.lessons(today)
    except Exception as e:
        log.error(f"Impossible de récupérer les cours : {e}")
        return

    if not lessons:
        log.info("Aucun cours aujourd'hui.")
        return

    for lesson in lessons:
        if lesson.start is None:
            continue

        result = analyse_lesson(lesson)
        if result is None:
            continue

        emoji, label, priority = result
        subject_name = lesson.subject.name if lesson.subject else "Cours"
        start_str    = lesson.start.strftime("%H%M")
        lesson_id    = f"{subject_name}_{start_str}_{label}"

        if lesson_id in already_notified:
            continue

        already_notified.add(lesson_id)

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

    # Renouvelle et sauvegarde le token sur Render
    new_creds = client.export_credentials()
    save_credentials_to_render(new_creds)

    return client


# ─────────────────────────────────────────────
# BOUCLE DE SURVEILLANCE (thread séparé)
# ─────────────────────────────────────────────

def watcher_loop():
    client = None
    fails  = 0

    try:
        client = login()
    except Exception as e:
        log.critical(f"Connexion initiale impossible : {e}")
        return

    send_notification(
        "Pronote Watcher actif",
        "Surveillance lancée. Alertes : annulation, cours déplacé, changement de salle.",
        priority="low"
    )

    while True:
        try:
            log.info(f"🔍 Vérification — {datetime.now().strftime('%H:%M:%S')}")
            check_cancellations(client)
            fails = 0

        except Exception as e:
            fails += 1
            log.error(f"Erreur ({fails}/5) : {e}")

            if fails >= 5:
                log.warning("Reconnexion...")
                try:
                    client = login()
                    fails  = 0
                except Exception as re:
                    log.critical(f"Reconnexion échouée : {re}")
                    send_notification("Pronote Watcher", "Reconnexion impossible. Vérification arrêtée.", priority="urgent")
                    break

        time.sleep(CHECK_INTERVAL)


# ─────────────────────────────────────────────
# SERVEUR HTTP (requis par Render Web Service)
# ─────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index():
    return {"status": "ok", "message": "Pronote Watcher tourne ✅"}, 200

@app.route("/health")
def health():
    return {"status": "healthy"}, 200


if __name__ == "__main__":
    # Lance la surveillance dans un thread séparé
    t = threading.Thread(target=watcher_loop, daemon=True)
    t.start()

    # Lance le serveur HTTP sur le port attendu par Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
