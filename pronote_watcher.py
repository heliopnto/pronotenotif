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
# CACHE LOCAL already_notified
# Fix : évite de relire os.environ à chaque vérification
# ─────────────────────────────────────────────

_notified_cache: set   = set()
_notified_loaded: bool = False
_notified_lock         = threading.Lock()


def load_notified() -> set:
    global _notified_cache, _notified_loaded
    with _notified_lock:
        if not _notified_loaded:
            raw = os.environ.get("ALREADY_NOTIFIED", "[]")
            try:
                _notified_cache = set(json.loads(raw))
                log.info(f"📂 {len(_notified_cache)} cours déjà notifiés chargés.")
            except Exception:
                _notified_cache = set()
            _notified_loaded = True
        return set(_notified_cache)  # copie pour éviter les mutations externes


def save_notified(notified: set):
    global _notified_cache
    with _notified_lock:
        _notified_cache = notified  # met à jour le cache local
    try:
        week_days = get_week_days()
        filtered  = {
            item for item in notified
            if any(item.startswith(d.strftime("%Y%m%d")) for d in week_days)
        }
        update_render_env("ALREADY_NOTIFIED", json.dumps(list(filtered)))
    except Exception as e:
        log.error(f"Erreur save_notified : {e}")


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
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
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
# Fix : wrapper qui notifie sur Telegram si le thread plante
# ─────────────────────────────────────────────

def watcher_loop():
    """Wrapper qui capture toute exception et notifie sur Telegram."""
    try:
        _watcher_loop_inner()
    except Exception as e:
        log.critical(f"💀 Thread de surveillance mort : {e}", exc_info=True)
        send_notification(
            "💀 <b>Pronote Watcher — Erreur critique</b>\n"
            f"Le thread a planté :\n<code>{e}</code>\n"
            "Redémarre le service sur Render."
        )


def _watcher_loop_inner():
    try:
        client = login()
    except Exception as e:
        log.critical(f"Connexion initiale impossible : {e}")
        send_notification(f"❌ <b>Pronote Watcher</b>\nConnexion initiale impossible :\n<code>{e}</code>")
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
# DÉMARRAGE DU THREAD
# Fix : guard contre le double démarrage (gunicorn multi-workers)
# ─────────────────────────────────────────────

_watcher_started = False
_watcher_lock    = threading.Lock()


def start_watcher():
    global _watcher_started
    with _watcher_lock:
        if _watcher_started:
            log.warning("Thread déjà démarré — ignoré.")
            return
        _watcher_started = True
    t = threading.Thread(target=watcher_loop, daemon=True)
    t.start()
    log.info("🚀 Thread de surveillance démarré.")


# Démarre dès le chargement du module (compatible gunicorn)
start_watcher()


# ─────────────────────────────────────────────
# SERVEUR HTTP (requis par Render)
# ─────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return """
    <html>
    <head>
        <title>Pronote Watcher</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: sans-serif; max-width: 500px; margin: 60px auto; padding: 20px; background: #f5f5f5; }
            h1 { color: #333; }
            .status { background: #d4edda; border-radius: 8px; padding: 12px 16px; margin-bottom: 24px; color: #155724; font-weight: bold; }
            .btn { display: inline-block; padding: 12px 24px; background: #0088cc; color: white; border-radius: 8px; text-decoration: none; font-size: 16px; cursor: pointer; border: none; width: 100%; box-sizing: border-box; text-align: center; margin-bottom: 10px; }
            .btn:hover { background: #006fa8; }
            .btn.green { background: #28a745; }
            .btn.green:hover { background: #1e7e34; }
            #result { margin-top: 20px; padding: 12px 16px; border-radius: 8px; display: none; }
            .ok  { background: #d4edda; color: #155724; }
            .err { background: #f8d7da; color: #721c24; }
        </style>
    </head>
    <body>
        <h1>📅 Pronote Watcher</h1>
        <div class="status">✅ Service en ligne</div>
        <a href="/test/telegram" class="btn">📱 Tester la notification Telegram</a>
        <a href="/test/pronote" class="btn green">🎓 Tester la connexion Pronote</a>
        <div id="result"></div>
        <script>
            document.querySelectorAll('.btn').forEach(btn => {
                btn.addEventListener('click', async function(e) {
                    e.preventDefault();
                    const url  = this.getAttribute('href');
                    const res  = document.getElementById('result');
                    const orig = this.textContent;
                    res.style.display = 'none';
                    this.textContent = '⏳ Test en cours...';
                    const self = this;
                    try {
                        const r    = await fetch(url);
                        const data = await r.json();
                        res.className   = data.ok ? 'ok' : 'err';
                        res.textContent = data.message;
                        res.style.display = 'block';
                    } catch(err) {
                        res.className   = 'err';
                        res.textContent = 'Erreur réseau : ' + err;
                        res.style.display = 'block';
                    }
                    self.textContent = orig;
                });
            });
        </script>
    </body>
    </html>
    """, 200


@app.route("/health")
def health():
    return {"status": "healthy"}, 200


@app.route("/test/telegram")
def test_telegram():
    try:
        today     = date.today()
        date_fr   = format_date_fr(today)
        heure_str = datetime.now().strftime("%Hh%M")
        send_notification(
            f"🔴 <b>Alerte — {date_fr}</b>\n"
            f"📚 SC.PHYS.-CHIM.APPLIQ\n"
            f"🕐 {heure_str}\n"
            f"📌 Prof. absent"
        )
        return {"ok": True, "message": "✅ Notification Telegram envoyée ! Vérifie ton téléphone."}, 200
    except Exception as e:
        return {"ok": False, "message": f"❌ Erreur : {str(e)}"}, 500


@app.route("/test/pronote")
def test_pronote():
    try:
        creds  = load_credentials()
        client = pronotepy.Client.token_login(**creds)
        if not client.logged_in:
            return {"ok": False, "message": "❌ Connexion Pronote échouée — token invalide."}, 500
        return {"ok": True, "message": f"✅ Connecté à Pronote en tant que : {client.info.name}"}, 200
    except Exception as e:
        return {"ok": False, "message": f"❌ Erreur Pronote : {str(e)}"}, 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
