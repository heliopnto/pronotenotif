"""
📅 Pronote Watcher — Surveillance via API pronotepy
Détecte : cours annulé, prof absent, cours déplacé, changement de salle
Notifie via ntfy.sh (iPhone)

Installation :
    pip install pronotepy requests
"""

import time
import logging
import requests
import pronotepy
from datetime import date, datetime, timedelta
from pronotepy.ent import ent_hdf

# ─────────────────────────────────────────────
# ⚙️  CONFIG — À MODIFIER AVANT DE LANCER
# ─────────────────────────────────────────────

CREDENTIALS = {
    "pronote_url": "https://0620042j.index-education.net/pronote/mobile.eleve.html?fd=1&bydlg=A6ABB224-12DD-4E31-AD3E-8A39A1C2C335&login=true",
    "username": "hpintooliveira",
    "password": "4F03A732FCFB8DFDD5A20CEC011D7750E5BAA62CFE9D5439BE16A6E246C3F0DE38827DF5DAAFFF44044405A1CF935698",
    "client_identifier": "46A26DC595C677066372732118702043034EEC3E97EA4D35D138437BFFBA7FB5DDE3AA129EE889D6A0CE8C93D2F0A999F32B9F8000000000",
    "uuid": "cb1143ffaac01171"
}

# Ton topic ntfy.sh (même nom que dans l'app iPhone)
NTFY_TOPIC = "alerte_pronote"

# Vérification toutes les X secondes
CHECK_INTERVAL = 60

# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pronote_watcher.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Mémorise les cours déjà notifiés pour éviter les doublons
already_notified: set = set()


# ─────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────

def send_notification(title: str, message: str, priority: str = "high"):
    """Envoie une notification push via ntfy.sh."""
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
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

def analyse_lesson(lesson: pronotepy.Lesson) -> tuple[str, str, str] | None:
    """
    Analyse un cours et retourne (emoji, label, priorité) si alertable.
    Retourne None si le cours est normal.

    Propriétés pronotepy utilisées :
      lesson.canceled      → bool  — cours annulé / prof absent
      lesson.detention     → bool  — heure de colle
      lesson.exempted      → bool  — dispensé
      lesson.test          → bool  — contrôle
      lesson.status        → str   — texte libre ex: "Prof. absent", "Cours déplacé"
      lesson.classrooms    → list  — salles (changement détecté par comparaison)
    """

    # 1. Cours annulé ou prof absent
    if lesson.canceled:
        status = lesson.status or "Cours annulé"
        # Distinguer "Prof. absent" de "Cours annulé" via le status
        if "absent" in status.lower():
            return ("🔴", f"Prof. absent — {status}", "urgent")
        else:
            return ("🔴", f"Cours annulé — {status}", "urgent")

    # 2. Cours avec statut particulier (déplacé, changement de salle, etc.)
    if lesson.status:
        s = lesson.status.lower()
        if "déplacé" in s or "deplace" in s:
            return ("🟠", f"Cours déplacé — {lesson.status}", "high")
        if "salle" in s or "changement" in s:
            return ("🔵", f"Changement de salle — {lesson.status}", "default")
        # Autre statut inconnu → on notifie quand même
        return ("⚠️", lesson.status, "default")

    return None  # Cours normal, rien à signaler


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

    now = datetime.now()

    for lesson in lessons:
        if lesson.start is None:
            continue

        # On ne s'intéresse qu'aux cours à venir (pas encore terminés)
        if lesson.start < now and lesson.start.date() == today:
            # Cours déjà passé — on vérifie quand même pour ne pas rater
            # les annulations de dernière minute
            pass

        result = analyse_lesson(lesson)
        if result is None:
            continue

        emoji, label, priority = result

        # Identifiant unique pour éviter les doublons
        subject_name = lesson.subject.name if lesson.subject else "Cours"
        start_str    = lesson.start.strftime("%H%M")
        lesson_id    = f"{subject_name}_{start_str}_{label}"

        if lesson_id in already_notified:
            continue

        already_notified.add(lesson_id)

        # Infos du cours
        start_fmt = lesson.start.strftime("%H:%M")
        end_fmt   = lesson.end.strftime("%H:%M") if lesson.end else "?"
        teacher   = lesson.teacher_name if hasattr(lesson, "teacher_name") and lesson.teacher_name else "Prof inconnu"
        classroom = ", ".join(lesson.classrooms) if lesson.classrooms else "N/A"

        title   = f"{emoji} {label}"
        message = (
            f"📚 {subject_name} ({start_fmt}–{end_fmt})\n"
            f"👤 {teacher}\n"
            f"🏫 Salle : {classroom}"
        )

        send_notification(title, message, priority)
        log.info(f"Cours signalé : {lesson_id}")


# ─────────────────────────────────────────────
# CONNEXION
# ─────────────────────────────────────────────

def login() -> pronotepy.Client:
    log.info("Connexion à Pronote via token...")
    
    client = pronotepy.Client.token_login(**CREDENTIALS)

    if not client.logged_in:
        raise ConnectionError("Échec de connexion via token.")

    log.info(f"✅ Connecté en tant que : {client.info.name}")

    # ⚠️ IMPORTANT : exporter les nouveaux credentials
    new_creds = client.export_credentials()

    # On met à jour les credentials en mémoire
    CREDENTIALS.update(new_creds)

    return client

# ─────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ─────────────────────────────────────────────

def main():
    log.info("🎓 Pronote Watcher démarré")

    client = None
    fails  = 0

    try:
        client = login()
    except Exception as e:
        log.critical(f"Connexion initiale impossible : {e}")
        return

    send_notification(
        "✅ Pronote Watcher actif",
        "Surveillance lancée. Tu seras alerté en cas d'annulation, cours déplacé ou changement de salle.",
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
                log.warning("Trop d'erreurs, tentative de reconnexion...")
                try:
                    client = login()
                    fails = 0
                except Exception as re:
                    log.critical(f"Reconnexion échouée : {re}")
                    send_notification("⚠️ Pronote Watcher", "Reconnexion impossible. Vérification arrêtée.", priority="urgent")
                    break

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
