"""
📅 Pronote Watcher v2 — Scraping HTML direct
Surveille les étiquettes Pronote et notifie via ntfy.sh

Installation :
    pip install selenium requests webdriver-manager

Le script gère automatiquement le téléchargement de ChromeDriver.
"""

import time
import logging
import requests
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────
# ⚙️  CONFIG — À MODIFIER AVANT DE LANCER
# ─────────────────────────────────────────────

PRONOTE_URL  = "https://0620042j.index-education.net/pronote/eleve.html?identifiant=JhSNxYxc4tPaXy6c"
PRONOTE_USER = "h.pintooliveir"
PRONOTE_PASS = "Aqzsedwrfx741852963."

# Ton topic ntfy.sh (le même que dans l'app iPhone)
NTFY_TOPIC   = "alerte_pronote"

# Intervalle de vérification en secondes
CHECK_INTERVAL = 60

# Afficher le navigateur ? True = visible, False = invisible (recommandé)
HEADLESS = True

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

# Mémorise les cours déjà notifiés (clé = "matière_heure_étiquette")
already_notified: set = set()

# ─────────────────────────────────────────────
# MAPPING DES CLASSES CSS → TYPE D'ALERTE
# Basé sur les classes observées dans ton HTML
# ─────────────────────────────────────────────
TAG_STYLES = {
    "gd-red-foncee":  ("🔴", "urgent"),   # Prof. absent / Cours annulé
    "gd-red":         ("🔴", "urgent"),
    "gd-orange":      ("🟠", "high"),     # Cours déplacé
    "gd-blue-moyen":  ("🔵", "default"),  # Changement de salle
    "gd-blue":        ("🔵", "default"),
    "gd-green":       ("🟢", "low"),
}

def get_tag_style(tag_classes: str):
    """Retourne (emoji, priorité) selon les classes CSS du tag."""
    for css_class, style in TAG_STYLES.items():
        if css_class in tag_classes:
            return style
    return ("⚠️", "default")


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


def create_driver() -> webdriver.Chrome:
    """Crée et configure le driver Chrome."""
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver


def login(driver: webdriver.Chrome):
    """Se connecte à Pronote."""
    log.info("Connexion à Pronote...")
    driver.get(PRONOTE_URL)
    
    wait = WebDriverWait(driver, 15)
    
    # Attendre le formulaire de connexion
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[type='password']")))
    
    user_field = driver.find_element(By.CSS_SELECTOR, "input[type='text']")
    pass_field  = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
    
    user_field.clear()
    user_field.send_keys(PRONOTE_USER)
    pass_field.clear()
    pass_field.send_keys(PRONOTE_PASS)
    
    # Cliquer sur "Connexion"
    btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
    btn.click()
    
    # Attendre que le dashboard charge
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".liste-cours, .page-cours, [class*='cours']")))
    log.info("✅ Connecté à Pronote")


def parse_cours(driver: webdriver.Chrome) -> list:
    """
    Analyse les cours visibles et retourne ceux qui ont une étiquette.

    Étiquettes détectées :
      - 🔴 Prof. absent     (gd-red-foncee)
      - 🔴 Cours annulé     (gd-red-foncee)
      - 🟠 Cours déplacé    (gd-orange)
      - 🔵 Changement salle (gd-blue-moyen)
    """
    results = []
    
    cours_elements = driver.find_elements(By.CSS_SELECTOR, "li.flex-contain")
    
    for cours in cours_elements:
        # Y a-t-il une étiquette dans ce cours ?
        etiquettes = cours.find_elements(By.CSS_SELECTOR, ".container-etiquette .tag-style")
        if not etiquettes:
            continue
        
        # Heures
        heures = cours.find_elements(By.CSS_SELECTOR, ".container-heures div")
        heure_debut = heures[0].text.strip() if len(heures) > 0 else "?"
        heure_fin   = heures[1].text.strip() if len(heures) > 1 else ""
        
        # Infos cours (matière, prof, groupe, salle)
        items = cours.find_elements(By.CSS_SELECTOR, ".container-cours > li")
        matiere = items[0].text.strip() if len(items) > 0 else "Cours"
        prof    = items[1].text.strip() if len(items) > 1 else "Prof inconnu"
        
        # Salle = dernier li sans classe container-etiquette
        salle = ""
        for li in items[2:]:
            cls = li.get_attribute("class") or ""
            if "container-etiquette" not in cls:
                salle = li.text.strip()
        
        for etiquette in etiquettes:
            tag_text    = etiquette.text.strip()
            tag_classes = etiquette.get_attribute("class") or ""
            cours_id    = f"{matiere}_{heure_debut}_{tag_text}"
            
            results.append({
                "id":          cours_id,
                "heure_debut": heure_debut,
                "heure_fin":   heure_fin,
                "matiere":     matiere,
                "prof":        prof,
                "salle":       salle,
                "etiquette":   tag_text,
                "tag_classes": tag_classes,
            })
    
    return results


def check_and_notify(driver: webdriver.Chrome):
    """Rafraîchit la page et notifie les nouvelles étiquettes."""
    driver.refresh()
    time.sleep(3)  # Laisse la page charger
    
    cours_avec_etiquette = parse_cours(driver)
    
    if not cours_avec_etiquette:
        log.info("Aucune étiquette — tout va bien 👍")
        return
    
    for cours in cours_avec_etiquette:
        cid = cours["id"]
        
        if cid in already_notified:
            log.info(f"Déjà notifié : {cid}")
            continue
        
        already_notified.add(cid)
        
        emoji, priority = get_tag_style(cours["tag_classes"])
        heures = f"{cours['heure_debut']}–{cours['heure_fin']}" if cours["heure_fin"] else cours["heure_debut"]
        
        title   = f"{emoji} {cours['etiquette']} — {cours['matiere']}"
        message = (
            f"📚 {cours['matiere']} ({heures})\n"
            f"👤 {cours['prof']}\n"
            f"🏫 Salle : {cours['salle'] or 'N/A'}\n"
            f"➜ {cours['etiquette']}"
        )
        
        send_notification(title, message, priority)
        log.info(f"Nouveau cours signalé : {cid}")


def main():
    log.info("🎓 Pronote Watcher v2 démarré")
    
    driver = None
    
    try:
        driver = create_driver()
        login(driver)
        
        send_notification(
            "✅ Pronote Watcher actif",
            "Surveillance lancée. Tu seras alerté en cas d'annulation, cours déplacé ou changement de salle.",
            priority="low"
        )
        
        while True:
            log.info(f"🔍 Vérification — {datetime.now().strftime('%H:%M:%S')}")
            
            try:
                check_and_notify(driver)
                
            except TimeoutException:
                log.warning("Timeout page — reconnexion...")
                login(driver)
            
            except WebDriverException as e:
                log.error(f"Erreur navigateur : {e}")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(10)
                driver = create_driver()
                login(driver)
            
            time.sleep(CHECK_INTERVAL)
    
    except KeyboardInterrupt:
        log.info("Arrêt manuel (Ctrl+C)")
    
    finally:
        if driver:
            driver.quit()
            log.info("Navigateur fermé.")


if __name__ == "__main__":
    main()
