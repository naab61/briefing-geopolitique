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

RSS_SOURCES = []

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
    {
        "name": "Telegram — OSINT Live",
        "type": "RÉSEAUX SOCIAUX / OSINT",
        "channel": "OSINTLive",
    },
    {
        "name": "Telegram — Liveuamap",
        "type": "RÉSEAUX SOCIAUX / CARTOGRAPHIE",
        "channel": "liveuamap",
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
        text = date_text.strip()

        try:
            dt = datetime.fromisoformat(
                text.replace("Z", "+00:00")
            )
        except ValueError:
            dt = parsedate_to_datetime(text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        age = (
            datetime.now(timezone.utc) - dt
        ).total_seconds() / 3600

        return 0 <= age <= max_hours

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
                    if not is_recent(date):
                        continue
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
        self.post_depth = 0
        self.in_text = False
        self.current_text = []
        self.current_post_id = None
        self.current_date = ""

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
            self.post_depth = 1
            self.in_text = False
            self.current_text = []

            self.current_post_id = attrs.get(
                "data-post"
            )
            self.current_date = ""

        elif self.in_post and tag == "div":
            self.post_depth += 1

        # Texte du message
        if (
            self.in_post
            and "tgme_widget_message_text"
            in classes
        ):
            self.in_text = True
            self.current_text = []

        if self.in_post and tag == "time":
            value = attrs.get("datetime", "")
            if value:
                self.current_date = value.strip()

    def handle_data(self, data):

        if self.in_post and self.in_text:
            data = data.strip()

            if data:
                self.current_text.append(data)

    def handle_endtag(self, tag):

        if self.in_post and tag == "div":
            self.post_depth -= 1

            if self.post_depth > 0:
                return

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
                        self.current_post_id,
                    "date": self.current_date,
                })

            self.in_post = False
            self.post_depth = 0
            self.in_text = False
            self.current_text = []
            self.current_post_id = None
            self.current_date = ""


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

            # Le flux public Telegram peut afficher plusieurs pages anciennes.
            # On ne conserve que les publications réellement récentes.
            posts = [
                post for post in parser.posts
                if is_recent(post.get("date", ""))
            ][-15:]

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
                    "date": post.get("date", ""),
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

            match_date = re.search(
                r"/((?:20)\d{2})/(\d{2})/(\d{2})/",
                link
            )

            if not match_date:
                continue

            date_text = (
                f"{match_date.group(1)}-"
                f"{match_date.group(2)}-"
                f"{match_date.group(3)}"
            )

            if not is_recent(
                date_text,
                max_hours=24 * 7
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
                "date": date_text,
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
        "MÉDIA — RADAR": 6,
    }

    articles = sorted(
        articles,
        key=lambda x: (
            priority.get(
                x.get("type", ""),
                5
            ),
            x.get("date", "")
        ),
        reverse=False
    )

    selected = []
    source_counts = {}
    theme_counts = {}

    themes = {
        "MOYEN-ORIENT": ["gaza", "israël", "iran", "syrie", "irak", "palestine"],
        "YÉMEN": ["yémen", "houthis", "mer rouge"],
        "UKRAINE-RUSSIE": ["ukraine", "russie", "poutine", "ukrainien"],
        "USA-TRUMP": ["trump", "états-unis", "washington", "usa"],
        "CHINE-INDOPACIFIQUE": ["chine", "taïwan", "pékin", "mer de chine"],
        "EUROPE": ["europe", "ue", "otan", "france", "allemagne"],
        "AFRIQUE": ["afrique", "sahel", "mali", "niger", "soudan"],
        "MAGHREB-MAROC": ["maroc", "algérie", "tunisie", "maghreb"],
        "OSINT-DÉSINFORMATION": ["désinformation", "propagande", "osint", "géolocalisation", "satellite"]
    }

    # Classe chaque article dans un thème
    for article in articles:
        text = (
            article.get("title", "") + " " +
            article.get("description", "")
        ).lower()

        article_theme = "AUTRE"

        for theme, keywords in themes.items():
            if any(keyword in text for keyword in keywords):
                article_theme = theme
                break

        article["theme"] = article_theme

    # Première passe : garantir une représentation de chaque thème
    for theme in themes:
        for article in articles:
            if article in selected:
                continue

            source_name = article.get("source", "")

            if source_counts.get(source_name, 0) >= 6:
                continue

            if article.get("theme") != theme:
                continue

            if theme_counts.get(theme, 0) >= 4:
                break

            selected.append(article)

            source_counts[source_name] = (
                source_counts.get(source_name, 0) + 1
            )

            theme_counts[theme] = (
                theme_counts.get(theme, 0) + 1
            )

            if len(selected) >= 55:
                break

        if len(selected) >= 55:
            break

    # Deuxième passe : compléter jusqu'à 55 articles
    for article in articles:
        if article in selected:
            continue

        source_name = article.get("source", "")

        if source_counts.get(source_name, 0) >= 6:
            continue

        selected.append(article)

        source_counts[source_name] = (
            source_counts.get(source_name, 0) + 1
        )

        theme = article.get("theme", "AUTRE")

        theme_counts[theme] = (
            theme_counts.get(theme, 0) + 1
        )

        if len(selected) >= 55:
            break

    # Classe les articles sélectionnés du plus récent au plus ancien
    selected = sorted(
        selected,
        key=lambda x: x.get("date", ""),
        reverse=True
    )
    sources = []

    for i, article in enumerate(
        selected,
        start=1
    ):
        article["_source_number"] = i

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
SÉLECTION ÉDITORIALE
========================

Ne sélectionne jamais les informations simplement parce qu'elles
sont très nombreuses dans les sources.

Cherche en priorité ce qui a changé récemment :
- fait nouveau
- changement de posture politique ou diplomatique
- évolution militaire ou stratégique
- nouvelle donnée économique ou énergétique
- document ou donnée nouvelle
- signal OSINT ou géolocalisation
- enquête originale
- narrative émergente ou campagne de désinformation
- conséquence possible pour la France ou l'Europe

Répartis l'attention entre les zones surveillées :
Moyen-Orient, Yémen, Liban, Ukraine/Russie, États-Unis/Trump,
Chine/Indo-Pacifique, Europe, Afrique/Sahel/Sud global,
Maroc/Maghreb.

Si une zone ne présente aucun signal suffisamment solide,
ne remplis pas artificiellement sa rubrique.

Plusieurs articles provenant de la même agence, de la même dépêche
ou décrivant exactement le même événement ne constituent PAS
plusieurs confirmations indépendantes.

Une forte couverture médiatique ne signifie pas qu'un événement
est plus important.

Ne laisse aucune source généraliste, notamment RFI ou Le Monde,
monopoliser le briefing.

Privilégie la valeur informative, la nouveauté, la qualité des preuves
et la diversité des sources plutôt que le volume d'articles.

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
appuie-toi sur les sources fournies.

IMPORTANT : NE GÉNÈRE AUCUNE SECTION
"SOURCES CLÉS" et NE GÉNÈRE AUCUN
marqueur du type [Source N], [Source],
Source 1, Sourcel ou équivalent.

Le programme construit automatiquement
la section "🔎 SOURCES CLÉS" à partir
des liens exacts des sources fournies.

Ne fabrique donc aucun lien et ne tente
pas de reproduire les URLs dans ta réponse.

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
STRUCTURE OBLIGATOIRE
========================

🔴 FAITS DU JOUR

Classe les faits du plus récent au plus ancien
selon leur date et heure de publication lorsqu'elles
sont disponibles.

Chaque fait doit être clairement séparé.

Ne répète pas plusieurs fois le même événement.

========================

🟡 SIGNAUX À CONFIRMER

Présente uniquement les informations
non suffisamment corroborées.

Indique ce qui est affirmé, qui le diffuse
et les éléments disponibles pour ou contre.

========================

🔵 ANALYSE

Explique ce que les faits permettent
de comprendre.

Ne répète pas les faits déjà présentés.

========================

🟣 NARRATIVES EN CIRCULATION

Présente uniquement les narratives
réellement significatives.

Indique clairement le camp, la communauté
ou l'acteur auquel elles sont associées.

Une narrative n'est jamais présentée
comme un fait établi.

========================

🔭 CE QUI PEUT CHANGER ENSUITE

24–72 h

Présente uniquement les évolutions concrètes
et plausibles à surveiller dans les prochaines
24 à 72 heures.

7 jours

Présente uniquement les évolutions susceptibles
de devenir importantes dans les sept prochains jours.

Ne répète pas les informations déjà présentées.

========================

👁️ À SURVEILLER

Liste uniquement les indicateurs ou événements
dont l'évolution mérite une surveillance particulière.

========================

RÈGLE DE NON-RÉPÉTITION

Une même information ne doit apparaître qu'une seule fois.

Ne reformule pas le même événement dans plusieurs sections.

========================

RÈGLE SUR LA VEILLE

Ne reprends jamais un sujet ou une information
déjà présents dans le briefing précédent.

Un sujet ancien ne peut réapparaître que si un
développement réellement nouveau est apparu depuis.

Dans ce cas, présente uniquement le nouveau
développement et ce qu'il change.

========================

SOURCES ET LIENS

Ne génère aucune section « SOURCES CLÉS ».

Ne génère aucun bloc récapitulatif de sources
à la fin du briefing.

Le lien direct doit être placé avec l'information
à laquelle il correspond.

Utilise uniquement les URLs fournies.

N'invente jamais d'URL.

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

Pour chaque information, indique l'heure de publication au format HH:MM lorsqu'elle est disponible.
Ne jamais inventer une heure.

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

        text = (
            result["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

        return text.strip()

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
# SOURCES CLÉS — GÉNÉRATION AUTOMATIQUE
# ============================================================

def add_key_sources(text, articles, limit=12):
    from html import escape
    # Supprime les blocs de sources que Gemini peut générer lui-même.
    cut_positions = []

    match = re.search(
        r"(?is)SOURCES?\s+CL[ÉE]S",
        text
    )
    if match:
        cut_positions.append(match.start())

    match = re.search(
        r"(?im)^\s*[-•]?\s*Source\s+\d+(?:\s|:)",
        text
    )
    if match:
        cut_positions.append(match.start())

    if cut_positions:
        text = text[:min(cut_positions)].rstrip()

    # Gemini peut générer sa propre rubrique de sources.
    # On supprime absolument tout ce qui suit la première
    # occurrence de "SOURCES CLÉS", même si Gemini ajoute
    # des espaces, caractères invisibles ou du texte sur la même ligne.
    parts = re.split(
        r"(?i)SOURCES?\s+CL[ÉE]S",
        text,
        maxsplit=1
    )

    text = parts[0].rstrip()

    # Supprime les éventuels liens Markdown ou URLs que Gemini
    # aurait placés dans le corps du briefing.
    text = re.sub(
        r"\[[^\]]*\]\(https?://[^)]+\)",
        "",
        text
    )

    text = re.sub(
        r"https?://[^\s<>\])]+",
        "",
        text
    )

    # Supprime les fragments isolés de liens X/Twitter.
    text = re.sub(
        r"(?im)^\s*[-•]?\s*/(?:status|photo)/\d+\s*$",
        "",
        text
    )

    # Supprime les slugs de liens isolés.
    text = re.sub(
        r"(?im)^\s*[-•]?\s*(?:[a-z0-9]+-){3,}[a-z0-9]+/?\s*$",
        "",
        text
    )

    # Nettoyage des espaces/lignes vides.
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    ).strip()

    # Construit la vraie rubrique SOURCES CLÉS uniquement
    # à partir des articles réellement récupérés par le script.
    key_articles = [
        article
        for article in articles
        if article.get("_source_number")
        and article.get("link")
    ][:limit]

    if not key_articles:
        return text

    lines = [
        "🔎 SOURCES CLÉS",
        "",
    ]

    for article in key_articles:
        number = article["_source_number"]

        title = clean_text(
            article.get("title", ""),
            180
        )

        link = escape(
            article.get("link", ""),
            quote=True
        )

        date_text = article.get("date", "")
        heure = ""

        if "T" in date_text or re.search(
            r"\d{2}:\d{2}",
            date_text
        ):
            try:
                try:
                    dt = datetime.fromisoformat(
                        date_text.replace(
                            "Z",
                            "+00:00"
                        )
                    )
                except ValueError:
                    dt = parsedate_to_datetime(
                        date_text
                    )

                heure = dt.strftime(
                    "%H:%M"
                )

            except Exception:
                pass

        prefix = f"- Source {number}"

        if heure:
            prefix += f" {heure}"

        if title:
            lines.append(
                f'{prefix} : <a href="{link}">'
                f'{escape(title)}</a>'
            )
        else:
            lines.append(
                f'{prefix} : <a href="{link}">'
                f'Source</a>'
            )

    return (
        text.rstrip()
        + "\n\n"
        + "\n".join(lines)
    )
# ============================================================
# TELEGRAM — ENVOI
# ============================================================

def add_publication_hours(text, articles):
    import re
    from html import escape

    def source_link(source_number):
        article = next(
            (
                item for item in articles
                if item.get("_source_number") == source_number
            ),
            None
        )

        if article is None:
            return None

        link = article.get("link", "")
        date_text = article.get("date", "")

        if not link:
            return None

        heure = ""

        if "T" in date_text or re.search(
            r"\d{2}:\d{2}",
            date_text
        ):
            try:
                try:
                    dt = datetime.fromisoformat(
                        date_text.replace(
                            "Z",
                            "+00:00"
                        )
                    )
                except ValueError:
                    dt = parsedate_to_datetime(
                        date_text
                    )

                heure = dt.strftime("%H:%M")

            except Exception:
                heure = ""

        safe_link = escape(
            link,
            quote=True
        )

        if heure:
            return f'{heure} <a href="{safe_link}">Source</a>'

        return f'<a href="{safe_link}">Source</a>'

    def replace_source_list(match):
        numbers = re.findall(r"Source\s+(\d+)", match.group(0))
        links = []

        for number in numbers:
            link = source_link(int(number))
            if link:
                links.append(link)

        return ", ".join(links)

    text = re.sub(
        r"\[Source\s+\d+(?:\s*,\s*Source\s+\d+)*\]",
        replace_source_list,
        text,
        flags=re.IGNORECASE
    )

    def replace_bare_source(match):
        link = source_link(int(match.group(1)))
        return link if link else match.group(0)

    text = re.sub(
        r"(?<![\w>])Source\s+(\d+)(?!\d)",
        replace_bare_source,
        text,
        flags=re.IGNORECASE
    )

    return text
    
def send_telegram(text):

    # Telegram HTML n'accepte qu'un nombre limité de balises.
    # Gemini peut produire des pseudo-balises comme <bos>.
    # On les neutralise AVANT de découper le message.
    text = re.sub(
        r"</?(?!a\b|/a\b)[a-zA-Z][^>]*>",
        "",
        text
    )

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
            "parse_mode": "HTML",
            
            "disable_web_page_preview":
                "true",
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
