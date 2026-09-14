import os
import json
import re
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MAX_AGE_HOURS = 36

# ============================================================
# SOURCES
# ============================================================

RSS_SOURCES = [
    {
        "name": "AFP — dépêches",
        "type": "AGENCE — BREAKING",
        "url": "https://www.afp.com/fr/actus/afp_actualite/792%2C31%2C9%2C7%2C33/feed"
    },
    {
        "name": "International Crisis Group",
        "type": "ANALYSE / CONFLITS",
        "url": "https://www.crisisgroup.org/rss"
    },
    {
        "name": "RFI",
        "type": "MÉDIA — RADAR",
        "url": "https://www.rfi.fr/fr/rss"
    },
    {
        "name": "Le Monde — international",
        "type": "MÉDIA — RADAR",
        "url": "https://www.lemonde.fr/international/rss_full.xml"
    }
]

TELEGRAM_CHANNELS = [
    {
        "name": "Telegram — OSINTdefender",
        "type": "RÉSEAUX SOCIAUX / OSINT",
        "channel": "osintdefender",
    },
    {
        "name": "Telegram — Twitter/TikTok / GeoConfirmed",
        "type": "RÉSEAUX SOCIAUX / GÉOLOCALISATION",
        "channel": "csources",
    },
]


BELLINGCAT_URL = "https://www.bellingcat.com/news/"


# ============================================================
# OUTILS
# ============================================================

def is_recent(date_text, max_hours=MAX_AGE_HOURS):
    if not date_text:
        return False

    try:
        dt = parsedate_to_datetime(date_text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        age = (
            datetime.now(timezone.utc) - dt
        ).total_seconds() / 3600

        return age <= max_hours

    except Exception:
        return False

def clean_html(text):
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = " ".join(text.split())
    return text.strip()


def clean_text(text, max_length=1000):
    text = clean_html(text)
    return text[:max_length]


# ============================================================
# RSS
# ============================================================

def get_rss_articles():
    articles = []

    for source in RSS_SOURCES:
        try:
            request = urllib.request.Request(
                source["url"],
                headers={
                    "User-Agent":
                        "Mozilla/5.0 "
                        "(compatible; BriefingGeopolitique/3.0)"
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=30
            ) as response:
                data = response.read()

            root = ET.fromstring(data)

            items = root.findall(".//item")

            if not items:
                ns = {
                    "atom": "http://www.w3.org/2005/Atom"
                }
                items = root.findall(
                    ".//atom:entry",
                    ns
                )

            count = 0

            for item in items[:20]:

                title = ""
                link = ""
                description = ""
                date = ""

                # RSS
                node = item.find("title")
                if node is not None and node.text:
                    title = node.text.strip()

                node = item.find("link")
                if node is not None and node.text:
                    link = node.text.strip()

                node = item.find("description")
                if node is not None and node.text:
                    description = node.text.strip()

                node = item.find("pubDate")
                if node is not None and node.text:
                    date = node.text.strip()

                # Atom
                if not title:
                    ns = {
                        "atom":
                        "http://www.w3.org/2005/Atom"
                    }

                    node = item.find(
                        "atom:title",
                        ns
                    )
                    if node is not None and node.text:
                        title = node.text.strip()

                    node = item.find(
                        "atom:link",
                        ns
                    )
                    if node is not None:
                        link = node.attrib.get(
                            "href",
                            ""
                        )

                    node = item.find(
                        "atom:summary",
                        ns
                    )
                    if node is not None and node.text:
                        description = node.text.strip()

                    node = item.find(
                        "atom:updated",
                        ns
                    )
                    if node is not None and node.text:
                        date = node.text.strip()

                if title:
                    articles.append({
                        "title": unescape(title),
                        "link": link,
                        "description":
                            unescape(description),
                        "date": date,
                        "source": source["name"],
                        "type": source["type"],
                    })

                    count += 1

            print(
                f"Source OK: {source['name']} "
                f"({count} éléments)"
            )

        except Exception as e:
            print(
                f"Source ignorée: "
                f"{source['name']} — {e}"
            )

    return articles


# ============================================================
# TELEGRAM PUBLIC
# ============================================================

class TelegramPostParser(HTMLParser):

    def __init__(self):
        super().__init__()

        self.in_post = False
        self.current_text = []
        self.current_post_id = None

        self.posts = []

    def handle_starttag(self, tag, attrs):

        attrs = dict(attrs)

        classes = attrs.get(
            "class",
            ""
        )

        # Conteneur d'un message Telegram
        if (
            "tgme_widget_message_wrap"
            in classes
        ):
            self.in_post = True
            self.current_text = []

            self.current_post_id = attrs.get(
                "data-post"
            )

        # Texte du message
        if (
            self.in_post
            and "tgme_widget_message_text"
            in classes
        ):
            self.current_text = []

    def handle_data(self, data):

        if self.in_post:
            data = data.strip()

            if data:
                self.current_text.append(data)

    def handle_endtag(self, tag):

        if tag == "div" and self.in_post:

            text = " ".join(
                self.current_text
            ).strip()

            if (
                text
                and len(text) > 30
            ):
                self.posts.append({
                    "text": text,
                    "post_id":
                        self.current_post_id
                })

            self.in_post = False
            self.current_text = []


def get_telegram_articles():
    articles = []

    for channel in TELEGRAM_CHANNELS:

        username = channel["channel"]

        url = (
            f"https://t.me/s/"
            f"{username}"
        )

        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent":
                        "Mozilla/5.0 "
                        "(compatible; "
                        "BriefingGeopolitique/3.0)"
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=30
            ) as response:
                html = response.read().decode(
                    "utf-8",
                    errors="replace"
                )

            parser = TelegramPostParser()
            parser.feed(html)

            posts = parser.posts[-15:]

            for post in posts:

                post_id = post.get(
                    "post_id"
                )

                link = ""

                if post_id:
                    link = (
                        "https://t.me/"
                        + post_id
                    )

                articles.append({
                    "title":
                        "Publication Telegram",
                    "link": link,
                    "description":
                        post["text"],
                    "date": "",
                    "source":
                        channel["name"],
                    "type":
                        channel["type"],
                })

            print(
                f"Social OK: "
                f"{channel['name']} "
                f"({len(posts)} publications)"
            )

        except Exception as e:

            print(
                f"Social ignoré: "
                f"{channel['name']} — {e}"
            )

    return articles


# ============================================================
# BELLINGCAT
# ============================================================

def get_bellingcat_articles():

    articles = []

    try:

        request = urllib.request.Request(
            BELLINGCAT_URL,
            headers={
                "User-Agent":
                    "Mozilla/5.0 "
                    "(compatible; "
                    "BriefingGeopolitique/3.0)"
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:
            html = response.read().decode(
                "utf-8",
                errors="replace"
            )

        # On récupère les liens vers les articles.
        matches = re.findall(
            r'href=["\']([^"\']+)["\'][^>]*>'
            r'\s*([^<]{20,200})',
            html,
            flags=re.I
        )

        seen = set()

        for link, title in matches:

            title = clean_text(title, 250)

            if not title:
                continue

            if link.startswith("/"):
                link = (
                    "https://www.bellingcat.com"
                    + link
                )

            if (
                "bellingcat.com"
                not in link
            ):
                continue

            if link in seen:
                continue

            seen.add(link)

            articles.append({
                "title": title,
                "link": link,
                "description":
                    "Investigation Bellingcat "
                    "issue de sources ouvertes.",
                "date": "",
                "source":
                    "Bellingcat",
                "type":
                    "OSINT / GÉOLOCALISATION / SATELLITE",
            })

            if len(articles) >= 15:
                break

        print(
            f"Source OK: Bellingcat "
            f"({len(articles)} éléments)"
        )

    except Exception as e:

        print(
            f"Source ignorée: "
            f"Bellingcat — {e}"
        )

    return articles


# ============================================================
# GEMINI
# ============================================================

def ask_gemini(articles):

    # On donne une vraie priorité aux sources OSINT
    # et sociales, plutôt qu'aux médias généralistes.

    priority = {
        "ENQUÊTE / OSINT": 1,
        "OSINT / GÉOLOCALISATION / SATELLITE": 1,
        "RÉSEAUX SOCIAUX / GÉOLOCALISATION": 2,
        "RÉSEAUX SOCIAUX / OSINT": 2,
        "ANALYSE / CONFLITS": 3,
        "MÉDIA — RADAR": 4,
    }

    articles = sorted(
        articles,
        key=lambda x:
            priority.get(
                x.get("type", ""),
                5
            )
    )

    selected = articles[:55]

    sources = []

    for i, article in enumerate(
        selected,
        start=1
    ):

        sources.append(
            f"""
SOURCE {i}

TYPE :
{article.get('type', '')}

SOURCE :
{article.get('source', '')}

TITRE :
{article.get('title', '')}

DATE :
{article.get('date', '')}

CONTENU :
{clean_text(
    article.get(
        'description',
        ''
    ),
    1100
)}

LIEN :
{article.get('link', '')}
""".strip()
        )

        prompt = """

Tu es un analyste géopolitique francophone
spécialisé en OSINT, renseignement en sources
ouvertes, conflits, influence informationnelle
et analyse stratégique.

Ta mission n'est PAS de résumer mécaniquement
les articles.

Tu dois identifier les signaux importants,
les faits vérifiables, les contradictions,
les évolutions rapides et les conséquences
stratégiques.

========================
HIÉRARCHIE DES SOURCES
========================

PRIORITÉ FORTE :

- sources primaires
- documents officiels
- enquêtes
- OSINT
- géolocalisation
- imagerie satellite
- données
- documents
- sources locales spécialisées
- publications originales de témoins ou acteurs

PRIORITÉ MOYENNE :

- analyses spécialisées
- organismes spécialisés dans les conflits
- centres de recherche

PRIORITÉ FAIBLE :

- médias généralistes

Les médias généralistes servent surtout
de radar et de piste de recherche.

========================
RÈGLE ABSOLUE SUR LES SOURCES
========================

Les informations fournies après
"SOURCES FOURNIES" contiennent des LIENS.

Quand tu cites une information factuelle,
utilise DIRECTEMENT le lien correspondant
à la source qui contient cette information.

NE JAMAIS écrire :

[Source 1]
[Source 40]
[Source 7]
[source##]
SOURCE 12

NE JAMAIS utiliser une numérotation interne
des sources.

Le lecteur doit pouvoir cliquer directement
sur le lien et revenir à l'information brute.

Pour chaque information importante,
donne si possible :

🔎 Source brute : URL DIRECTE

Si plusieurs sources indépendantes corroborent
le même fait :

🔎 Source brute : URL DIRECTE
🔗 Corroboration : URL DIRECTE

Utilise uniquement les URLs présentes
dans les sources fournies.

N'invente JAMAIS une URL.

Ne remplace JAMAIS une URL d'article par
la page d'accueil du média.

Ne remplace JAMAIS le lien d'un post social
par le lien général du canal.

Pour un post Telegram ou X :
utilise le lien DIRECT DU POST lorsqu'il est fourni.

========================
CORROBORATION
========================

Deux comptes sociaux qui recopient la même
information = UNE SEULE source indépendante.

Deux médias qui reprennent la même dépêche
= UNE SEULE source indépendante.

Une déclaration officielle ≠ preuve indépendante.

Une publication sociale seule n'est pas une preuve.

Si plusieurs sources indépendantes concordent,
indique-le.

Si elles se contredisent,
conserve la contradiction.

Si les éléments ne permettent pas de conclure :

"preuves insuffisantes"

========================
RÉSEAUX SOCIAUX / OSINT
========================

Les publications Telegram, X/Twitter,
vidéos, images et autres contenus sociaux
sont des SIGNAUX.

Pour chaque signal important :

1. indique ce qui est affirmé ;
2. indique qui le diffuse ;
3. cherche une corroboration dans les autres
   sources fournies ;
4. indique si c'est corroboré ;
5. indique si c'est contradictoire ;
6. indique s'il peut s'agir d'une narrative,
   d'une opération d'influence ou d'un recyclage.

Si aucune corroboration indépendante n'existe :

🟡 PLAUSIBLE / NON CONFIRMÉ

ou :

🟣 RÉCIT / NARRATIVE

========================
OSINT
========================

Porte une attention particulière à :

- géolocalisation
- vidéos
- images
- mouvements militaires
- frappes
- infrastructures
- navires
- aéronefs
- satellites
- changements de terrain
- destructions
- sanctions
- mouvements diplomatiques
- changements de posture militaire
- réseaux d'influence
- propagande
- désinformation

Ne prétends jamais avoir vérifié
une image, une vidéo, une géolocalisation
ou une donnée si les sources fournies
ne permettent pas réellement de le vérifier.

========================
NARRATIVES
========================

Repère les narratives :

🇷🇺 russe
🇺🇦 ukrainienne
🇺🇸 américaine
🇨🇳 chinoise
🇮🇷 iranienne
🇮🇱 israélienne
🇵🇸 palestinienne
🇪🇺 européenne
🇫🇷 française
🌍 locales

Une narrative doit toujours être présentée
comme une narrative, jamais comme un fait.

========================
SURVEILLANCE PERMANENTE
========================

Surveille particulièrement :

🇲🇦 Maroc / Maghreb
🇺🇸 Donald Trump / États-Unis
🇷🇺 Russie
🇺🇦 Ukraine
🇮🇱 Israël / Palestine
🇮🇷 Iran
🇨🇳 Chine / Indo-Pacifique
🇪🇺 Europe
🌍 Afrique / Sahel
🌍 Sud global

========================
FORMAT
========================

🌍 GRANDES TENDANCES

🟠 MOYEN-ORIENT

🔴 UKRAINE / RUSSIE

🇺🇸 ÉTATS-UNIS

🇨🇳 CHINE / INDO-PACIFIQUE

🇪🇺 EUROPE

🌍 AFRIQUE / SUD GLOBAL

🇲🇦 MAROC / MAGHREB

📱 RÉSEAUX SOCIAUX / OSINT

🧠 NARRATIVES ET DÉSINFORMATION

🇫🇷 CONSÉQUENCES POUR LA FRANCE ET L'EUROPE

🔭 SCÉNARIOS 24–72H

📅 SCÉNARIOS À 7 JOURS

🔎 SOURCES CLÉS

========================
CLASSIFICATION
========================

🟢 CONFIRMÉ

🟡 PLAUSIBLE / NON CONFIRMÉ

🔵 ANALYSE

🟣 RÉCIT / NARRATIVE

========================
STYLE
========================

Français uniquement.

Direct.

Dense.

Analytique.

Pas de remplissage.

Pas de répétition.

Ne force aucune information.

Chaque affirmation importante doit être
accompagnée de son lien direct lorsqu'un
lien pertinent est disponible.

Maximum 3800 caractères.

Si aucune information sérieuse
n'est disponible :

"Pas de signal solide dans les sources disponibles."

========================
SOURCES FOURNIES
========================

""" + "\n\n".join(sources)
    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        "gemini-3.5-flash-lite:"
        "generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ]
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload
        ).encode("utf-8"),
        headers={
            "Content-Type":
                "application/json",
            "x-goog-api-key":
                GEMINI_API_KEY
        },
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=90
        ) as response:

            result = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

    except urllib.error.HTTPError as e:

        error_body = e.read().decode(
            "utf-8",
            errors="replace"
        )

        print(
            "ERREUR GEMINI :",
            error_body
        )

        raise

    try:

        return (
            result["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

    except (
        KeyError,
        IndexError,
        TypeError
    ):

        print(
            "Réponse Gemini inattendue :",
            result
        )

        raise RuntimeError(
            "Réponse Gemini inutilisable."
        )


# ============================================================
# TELEGRAM — ENVOI
# ============================================================

def send_telegram(text):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    max_length = 3900

    chunks = []

    while len(text) > max_length:

        cut = text.rfind(
            "\n",
            0,
            max_length
        )

        if cut < 1000:
            cut = max_length

        chunks.append(
            text[:cut]
        )

        text = text[
            cut:
        ].lstrip()

    if text:
        chunks.append(text)

    for i, chunk in enumerate(
        chunks
    ):

        data = urllib.parse.urlencode({
            "chat_id":
                TELEGRAM_CHAT_ID,
            "text":
                chunk,
            "disable_web_page_preview":
                "false",
        }).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=data,
            method="POST"
        )

        try:

            with urllib.request.urlopen(
                request,
                timeout=30
            ) as response:

                response.read()

            print(
                f"Telegram : partie "
                f"{i + 1}/{len(chunks)} envoyée."
            )

        except urllib.error.HTTPError as e:

            error_body = e.read().decode(
                "utf-8",
                errors="replace"
            )

            print(
                "ERREUR TELEGRAM :",
                error_body
            )

            raise


# ============================================================
# MAIN
# ============================================================

def main():

    articles = []

    # RSS
    articles.extend(
        get_rss_articles()
    )

    # Telegram public
    articles.extend(
        get_telegram_articles()
    )

    # Bellingcat
    articles.extend(
        get_bellingcat_articles()
    )

    if not articles:

        raise RuntimeError(
            "Aucune source exploitable trouvée."
        )

    print(
        f"{len(articles)} éléments "
        f"récupérés au total."
    )

    briefing = ask_gemini(
        articles
    )

    send_telegram(
        briefing
    )

    print(
        "Briefing V3 envoyé sur Telegram."
    )


if __name__ == "__main__":
    main()
