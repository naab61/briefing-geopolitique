import os
import json
import re
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser
from datetime import datetime, timezone


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

STATE_FILE = "flash_seen.json"
EVENT_STATE_FILE = "flash_events.json"

MAX_POST_AGE_MINUTES = 30
EVENT_MEMORY_HOURS = 6


TELEGRAM_CHANNELS = [
    {"name": "OSINTdefender", "channel": "osintdefender"},
    {"name": "GeoConfirmed", "channel": "csources"},
    {"name": "Liveuamap", "channel": "liveuamap"},
]


STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this",
    "are", "has", "have", "was", "were", "into", "after",
    "before", "over", "under", "its", "their", "they",
    "said", "says", "will", "been", "being", "than",
    "les", "des", "une", "dans", "pour", "avec", "sur",
    "est", "sont", "qui", "que", "aux", "par", "mais",
    "plus", "sans", "selon"
}


# ============================================================
# TYPES D'ÉVÉNEMENTS
# ============================================================

EVENT_PATTERNS = {
    "attaque": [
        "airstrike",
        "airstrikes",
        "air strike",
        "drone attack",
        "drone attacks",
        "drone strike",
        "drone strikes",
        "missile attack",
        "missile strike",
        "missile strikes",
        "bombing",
        "bombed",
        "explosion",
        "explosions",
    ],

    "victimes": [
        "killed",
        "dead",
        "deaths",
        "casualties",
        "massacre",
        "mass shooting",
    ],

    "affrontement_majeur": [
        "invasion",
        "invaded",
        "major clashes",
        "heavy fighting",
        "large-scale fighting",
    ],

    "crise_politique": [
        "coup",
        "coup attempt",
        "resignation",
        "resigns",
        "resigned",
        "government collapse",
        "government collapsed",
        "government falls",
        "state of emergency",
        "martial law",
        "mobilization",
        "mobilisation",
    ],

    "manifestation": [
        "mass protest",
        "mass protests",
        "mass demonstration",
        "mass demonstrations",
        "riot",
        "riots",
        "major unrest",
    ],

    "diplomatie": [
        "break diplomatic relations",
        "breaks diplomatic relations",
        "diplomatic relations severed",
        "ambassador expelled",
        "ambassador expelled",
        "expels ambassador",
        "expelled its ambassador",
        "embassy attack",
        "embassy attacked",
    ],

    "cessez_le_feu": [
        "ceasefire",
        "cease-fire",
        "truce",
        "peace deal",
        "peace agreement",
    ],

    "catastrophe": [
        "earthquake",
        "tsunami",
        "hurricane",
        "typhoon",
        "tornado",
        "major flood",
        "major flooding",
        "wildfire",
        "wildfires",
        "volcanic eruption",
        "plane crash",
        "aircraft crash",
        "train crash",
        "ship collision",
        "major accident",
    ],

    "nucleaire": [
        "nuclear test",
        "nuclear attack",
        "nuclear strike",
        "nuclear threat",
        "nuclear weapons used",
        "radiation leak",
        "reactor accident",
    ],

    "otage": [
        "hostage situation",
        "hostages taken",
        "hostages seized",
        "hostage crisis",
    ],

    "fermeture_majeure": [
        "airspace closed",
        "airspace closure",
        "airport closed",
        "airport closure",
        "border closed",
        "border closure",
    ],
}


# ============================================================
# MOTIFS À EXCLURE
# ============================================================

EXCLUDED_PATTERNS = [
    # Contrats / industrie / achats
    "contract",
    "contracts",
    "awarded",
    "award",
    "procurement",
    "production contract",
    "production of",
    "million contract",
    "billion contract",
    "worth $",
    "worth €",

    # Programmes / développement
    "development program",
    "development programme",
    "development of",
    "developing",
    "developed a new",
    "new program",
    "new programme",

    # Installations / infrastructures
    "facility",
    "facilities",
    "megawatts",
    "megawatt",
    "mw power",
    "power plant",
    "nuclear power plant",

    # Communication / commentaire sans événement concret
    "statement",
    "statements",
    "said on social media",
    "said in a statement",
    "says in a statement",
    "commented on",
    "commentary",
    "analysis",
    "analysts say",
    "experts say",

    # Rumeur / narration sociale sans événement établi
    "publications font état",
    "social media posts claim",
    "social media reports",
    "posts claim",
    "posts reportedly",
    "reportedly claims",
]


# ============================================================
# LIEUX UTILISÉS POUR LA DÉTECTION D'ÉVÉNEMENTS
# ============================================================

LOCATIONS = [
    # Ukraine / Russie
    "kyiv",
    "kiev",
    "odesa",
    "odessa",
    "kharkiv",
    "kherson",
    "zaporizhzhia",
    "zaporizhzhia",
    "donetsk",
    "luhansk",
    "mariupol",
    "crimea",
    "crimea",
    "moscow",
    "moscou",
    "st petersburg",
    "saint petersburg",
    "russia",
    "russie",
    "ukraine",

    # Moyen-Orient
    "gaza",
    "rafah",
    "beirut",
    "beyrouth",
    "lebanon",
    "liban",
    "israel",
    "israël",
    "iran",
    "syria",
    "syrie",
    "damascus",
    "iraq",
    "irak",
    "baghdad",
    "yemen",
    "yémen",
    "sanaa",
    "sana'a",

    # États-Unis / Europe
    "washington",
    "new york",
    "london",
    "paris",
    "berlin",
    "brussels",
    "bruxelles",

    # Afrique
    "sudan",
    "soudan",
    "khartoum",
    "somalia",
    "somalie",
    "ethiopia",
    "ethiopie",
    "nigeria",
    "south africa",
    "durban",

    # Asie
    "china",
    "chine",
    "beijing",
    "taiwan",
    "japan",
    "japon",
    "north korea",
    "south korea",
    "india",
    "pakistan",

    # Autres lieux déjà rencontrés
    "oufa",
    "ufa",
    "kuibyshev",
    "kouïbychev",
    "aberdeen",
]


# ============================================================
# PARSEUR TELEGRAM
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
        classes = attrs.get("class", "")

        # Début d'un post
        if "tgme_widget_message_wrap" in classes:

            self.in_post = True
            self.post_depth = 1

            self.in_text = False
            self.current_text = []

            self.current_post_id = attrs.get("data-post")
            self.current_date = ""

            return

        # Divs internes du post
        if self.in_post and tag == "div":

            self.post_depth += 1

            if (
                "tgme_widget_message" in classes
                and attrs.get("data-post")
            ):
                self.current_post_id = attrs.get("data-post")

        # Texte du message
        if (
            self.in_post
            and "tgme_widget_message_text" in classes
        ):
            self.in_text = True
            self.current_text = []

        # Date du message
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

        if not self.in_post:
            return

        if tag == "div":

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
                    "post_id": self.current_post_id,
                    "date": self.current_date,
                })

            self.in_post = False
            self.post_depth = 0

            self.in_text = False
            self.current_text = []

            self.current_post_id = None
            self.current_date = ""


# ============================================================
# ÉTAT DES POSTS DÉJÀ VUS
# ============================================================

def load_seen():

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return set(
                json.load(f)
            )

    except Exception:

        return set()


def save_seen(seen):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            list(seen)[-500:],
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# ÉTAT DES ÉVÉNEMENTS FLASH
# ============================================================

def load_event_state():

    if not os.path.exists(
        EVENT_STATE_FILE
    ):
        return []

    try:

        with open(
            EVENT_STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

            if isinstance(data, list):
                return data

            return []

    except Exception:

        return []


def save_event_state(events):

    with open(
        EVENT_STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            events[-100:],
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# DATES
# ============================================================

def parse_post_datetime(date_text):

    if not date_text:
        return None

    try:

        dt = datetime.fromisoformat(
            date_text.replace(
                "Z",
                "+00:00"
            )
        )

        # On refuse une date sans fuseau :
        # impossible de garantir la fraîcheur.
        if dt.tzinfo is None:
            return None

        return dt.astimezone(
            timezone.utc
        )

    except Exception:

        return None


def post_age_minutes(post):

    dt = parse_post_datetime(
        post.get("date", "")
    )

    if dt is None:
        return None

    return (
        datetime.now(timezone.utc) - dt
    ).total_seconds() / 60


def is_flash_recent(
    post,
    max_minutes=MAX_POST_AGE_MINUTES
):

    date_text = post.get(
        "date",
        ""
    )

    age_minutes = post_age_minutes(
        post
    )

    if age_minutes is None:

        print(
            "FRAÎCHEUR REJETÉE | "
            f"{post.get('channel')} | "
            f"{post.get('post_id')} | "
            f"date={date_text} | "
            "date invalide ou sans fuseau"
        )

        return False

    print(
        "FRAÎCHEUR | "
        f"{post.get('channel')} | "
        f"{post.get('post_id')} | "
        f"date={date_text} | "
        f"age={age_minutes:.1f} min"
    )

    return (
        0 <= age_minutes <= max_minutes
    )


# ============================================================
# NORMALISATION
# ============================================================

def normalize_words(text):

    text = text.lower()

    text = re.sub(
        r"https?://\S+",
        " ",
        text
    )

    text = re.sub(
        r"[^a-z0-9àâçéèêëîïôûùüÿœæ\s]",
        " ",
        text
    )

    return {
        word
        for word in text.split()
        if (
            len(word) >= 4
            and word not in STOPWORDS
        )
    }


# ============================================================
# DÉTECTION DU TYPE D'ÉVÉNEMENT
# ============================================================

def detect_event_type(text):

    text = text.lower()

    for event_type, patterns in EVENT_PATTERNS.items():

        for pattern in patterns:

            if pattern in text:
                return event_type

    return None


def detect_location(text):

    text = text.lower()

    for location in LOCATIONS:

        if location in text:
            return location

    return ""


# ============================================================
# SIGNATURE D'ÉVÉNEMENT
# ============================================================

def event_signature(post):

    text = post.get(
        "text",
        ""
    )

    event_type = detect_event_type(
        text
    )

    location = detect_location(
        text
    )

    if not event_type:
        return ""

    # Le lieu permet d'éviter de considérer
    # deux événements différents comme identiques.
    if location:

        return (
            f"{event_type}|{location}"
        )

    # Si aucun lieu n'est détecté,
    # on utilise quelques mots significatifs.
    words = sorted(
        normalize_words(text)
    )

    if words:

        return (
            f"{event_type}|"
            + "|".join(words[:5])
        )

    return event_type


# ============================================================
# ÉVÉNEMENT DÉJÀ ENVOYÉ
# ============================================================

def event_already_sent(
    post,
    events,
    max_hours=EVENT_MEMORY_HOURS
):

    current_signature = event_signature(
        post
    )

    if not current_signature:
        return False

    now = datetime.now(
        timezone.utc
    )

    for old_event in events:

        old_signature = old_event.get(
            "signature",
            ""
        )

        if (
            old_signature
            != current_signature
        ):
            continue

        old_date = parse_post_datetime(
            old_event.get(
                "date",
                ""
            )
        )

        if old_date is None:
            continue

        age_hours = (
            now - old_date
        ).total_seconds() / 3600

        if (
            0 <= age_hours <= max_hours
        ):

            print(
                "DOUBLON ÉVÉNEMENT | "
                f"{current_signature} | "
                f"ancien={age_hours:.1f}h"
            )

            return True

    return False


# ============================================================
# RÉCUPÉRATION DES NOUVEAUX POSTS
# ============================================================

def get_new_posts():

    seen = load_seen()

    new_posts = []

    first_run = (
        len(seen) == 0
    )

    for channel in TELEGRAM_CHANNELS:

        username = channel[
            "channel"
        ]

        url = (
            "https://t.me/s/"
            + username
        )

        try:

            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent":
                        "Mozilla/5.0 "
                        "(compatible; "
                        "FlashGeopolitique/1.0)"
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

            print(
                f"{channel['name']} : "
                f"{len(parser.posts)} posts bruts détectés"
            )

            for post in parser.posts:

                post_id = post.get(
                    "post_id"
                )

                if not post_id:

                    print(
                        "POST SANS ID : "
                        + post.get(
                            "text",
                            ""
                        )[:100]
                    )

                    continue

                if post_id in seen:
                    continue

                seen.add(
                    post_id
                )

                new_posts.append({
                    "channel":
                        channel["name"],

                    "post_id":
                        post_id,

                    "text":
                        post["text"],

                    "date":
                        post["date"],

                    "link":
                        "https://t.me/"
                        + post_id,
                })

        except Exception as e:

            print(
                f"Erreur "
                f"{channel['name']}: {e}"
            )

    # IMPORTANT :
    # sauvegarder même lors du premier lancement.
    save_seen(
        seen
    )

    print(
        "Total avant filtrage : "
        f"{len(new_posts)} nouveaux posts"
    )

    if first_run:

        print(
            "Premier lancement : "
            "flux initial mémorisé, "
            "aucun FLASH historique envoyé."
        )

        return []

    return new_posts


# ============================================================
# FILTRE FLASH
# ============================================================

def is_flash_candidate(post):

    text = post.get(
        "text",
        ""
    ).lower()

    # --------------------------------------------------------
    # 1. EXCLUSIONS ABSOLUES
    # --------------------------------------------------------

    for pattern in EXCLUDED_PATTERNS:

        if pattern in text:

            print(
                "REJET FILTRE | "
                f"{post.get('channel')} | "
                f"{post.get('post_id')} | "
                f"raison={pattern}"
            )

            return False

    # --------------------------------------------------------
    # 2. DÉTECTION DE L'ÉVÉNEMENT
    # --------------------------------------------------------

    event_type = detect_event_type(
        text
    )

    if not event_type:

        print(
            "REJET FILTRE | "
            f"{post.get('channel')} | "
            f"{post.get('post_id')} | "
            "aucun événement majeur détecté"
        )

        return False

    # --------------------------------------------------------
    # 3. CAS PARTICULIER : VICTIMES / MASSACRE
    # --------------------------------------------------------

    if event_type == "victimes":

        concrete_victim_signal = any(
            pattern in text
            for pattern in [
                "killed",
                "dead",
                "deaths",
                "casualties",
                "mass shooting",
            ]
        )

        if not concrete_victim_signal:

            print(
                "REJET FILTRE | "
                f"{post.get('channel')} | "
                f"{post.get('post_id')} | "
                "victimes non suffisamment concrètes"
            )

            return False

    # --------------------------------------------------------
    # 4. CAS ATTAQUE :
    #    on veut un événement concret,
    #    pas simplement le mot "strike"
    # --------------------------------------------------------

    if event_type == "attaque":

        concrete_attack = any(
            pattern in text
            for pattern in [
                "airstrike",
                "airstrikes",
                "air strike",
                "drone attack",
                "drone attacks",
                "drone strike",
                "drone strikes",
                "missile attack",
                "missile strike",
                "missile strikes",
                "bombing",
                "bombed",
                "explosion",
                "explosions",
            ]
        )

        if not concrete_attack:

            print(
                "REJET FILTRE | "
                f"{post.get('channel')} | "
                f"{post.get('post_id')} | "
                "attaque opérationnelle/routine"
            )

            return False

    # --------------------------------------------------------
    # 5. ÉVITER LES RÉCITS SOCIAUX SANS FAIT CONCRET
    # --------------------------------------------------------

    narrative_only_patterns = [
        "posts claim",
        "social media claims",
        "social media reports",
        "publications font état",
        "reports suggest",
        "reports claim",
        "according to posts",
    ]

    if any(
        pattern in text
        for pattern in narrative_only_patterns
    ):

        # On accepte quand même si le texte contient
        # un signal concret fort.
        concrete_signal = any(
            pattern in text
            for pattern in [
                "explosion",
                "explosions",
                "killed",
                "dead",
                "deaths",
                "casualties",
                "earthquake",
                "plane crash",
                "aircraft crash",
                "ceasefire",
                "coup",
                "ambassador expelled",
                "airspace closed",
            ]
        )

        if not concrete_signal:

            print(
                "REJET FILTRE | "
                f"{post.get('channel')} | "
                f"{post.get('post_id')} | "
                "récit non suffisamment concret"
            )

            return False

    # --------------------------------------------------------
    # 6. LOCALISATION OU SIGNAL STRATÉGIQUE
    # --------------------------------------------------------

    location = detect_location(
        text
    )

    if not location:

        strong_event = event_type in {
            "nucleaire",
            "cessez_le_feu",
            "diplomatie",
            "crise_politique",
            "catastrophe",
        }

        if not strong_event:

            print(
                "REJET FILTRE | "
                f"{post.get('channel')} | "
                f"{post.get('post_id')} | "
                "aucun lieu identifiable"
            )

            return False

    print(
        "CANDIDAT FLASH | "
        f"{post.get('channel')} | "
        f"{post.get('post_id')} | "
        f"type={event_type} | "
        f"lieu={location or 'non identifié'}"
    )

    return True


# ============================================================
# DÉDOUBLONNAGE DES POSTS DU MÊME RUN
# ============================================================

def same_event(
    post1,
    post2
):

    signature1 = event_signature(
        post1
    )

    signature2 = event_signature(
        post2
    )

    if (
        signature1
        and signature2
        and signature1 == signature2
    ):

        return True

    return False


# ============================================================
# SÉLECTION FINALE
# ============================================================

def select_flash_events(posts):

    candidates = []

    for post in posts:

        if not is_flash_recent(
            post
        ):
            continue

        if not is_flash_candidate(
            post
        ):
            continue

        candidates.append(
            post
        )

    print(
        f"{len(candidates)} candidats "
        "après filtres."
    )

    previous_events = load_event_state()

    selected = []

    for post in candidates:

        if event_already_sent(
            post,
            previous_events
        ):
            continue

        duplicate = False

        for existing in selected:

            if same_event(
                post,
                existing
            ):

                duplicate = True
                break

        if not duplicate:

            selected.append(
                post
            )

    return selected


# ============================================================
# GEMINI : TRADUCTION + CONDENSATION UNIQUEMENT
# ============================================================

def shorten_flash_with_gemini(
    source_text
):

    prompt = f"""
Tu es uniquement un traducteur-résumeur pour un flux FLASH géopolitique.

SOURCE :
{source_text}

RÈGLES ABSOLUES :

- Traduis en français.
- Ne rajoute aucune information.
- Ne complète aucune information.
- Ne déduis rien.
- Ne fais aucune analyse.
- Ne vérifie pas l'information.
- Ne donne aucun contexte extérieur.
- Ne transforme jamais une information incertaine en fait certain.
- Conserve les formulations telles que "selon", "aurait", "des informations font état de", etc.
- Garde uniquement l'événement principal.
- Titre de 1 à 4 mots.
- Résumé de 1 ou 2 phrases très courtes.
- Le titre doit reprendre uniquement un lieu ou sujet explicitement présent dans SOURCE.
- Si aucun lieu ou sujet clair n'est présent, utilise exactement : ÉVÉNEMENT

Réponds EXACTEMENT :

TITRE: [titre]
RESUME: [résumé]

Aucun autre texte.
"""

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 120
        }
    }

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        "gemini-3.5-flash-lite:generateContent?key="
        + GEMINI_API_KEY
    )

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload
        ).encode("utf-8"),
        headers={
            "Content-Type":
                "application/json"
        },
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            result = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

        text = (
            result["candidates"][0]
            ["content"]["parts"][0]["text"]
            .strip()
        )

        title_match = re.search(
            r"(?im)^TITRE:\s*(.+)$",
            text
        )

        summary_match = re.search(
            r"(?im)^RESUME:\s*(.+)$",
            text
        )

        if (
            not title_match
            or not summary_match
        ):

            raise ValueError(
                "Format Gemini FLASH inattendu"
            )

        return {
            "title":
                title_match.group(1).strip(),

            "summary":
                summary_match.group(1).strip(),
        }

    except Exception as e:

        print(
            "ERREUR GEMINI FLASH :",
            e
        )

        return {
            "title":
                "ÉVÉNEMENT",

            "summary":
                source_text,
        }


# ============================================================
# HEURE AFFICHÉE
# ============================================================

def format_flash_time(
    date_string
):

    try:

        dt = parse_post_datetime(
            date_string
        )

        if dt is None:
            return date_string

        return dt.astimezone().strftime(
            "%H:%M"
        )

    except Exception:

        return date_string


# ============================================================
# ENVOI TELEGRAM
# ============================================================

def send_flash(post):

    result = shorten_flash_with_gemini(
        post["text"]
    )

    title = result["title"]
    summary = result["summary"]

    text = (
        f"🔴 <b>FLASH — "
        f"{title.upper()}</b>\n\n"
        f"{summary}\n\n"
        f"🕒 {format_flash_time(post['date'])}\n"
        f"📡 {post['channel']}\n"
        f"🔗 <a href=\"{post['link']}\">"
        f"Source originale</a>"
    )

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id":
            TELEGRAM_CHAT_ID,

        "text":
            text,

        "parse_mode":
            "HTML",

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

        # ----------------------------------------------------
        # IMPORTANT :
        # l'événement n'est mémorisé qu'après
        # un envoi Telegram réussi.
        # ----------------------------------------------------

        events = load_event_state()

        events.append({
            "signature":
                event_signature(post),

            "date":
                post.get(
                    "date",
                    ""
                ),

            "channel":
                post.get(
                    "channel",
                    ""
                ),

            "post_id":
                post.get(
                    "post_id",
                    ""
                ),
        })

        save_event_state(
            events
        )

        print(
            "FLASH envoyé :",
            post["channel"],
            post["post_id"]
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

    posts = get_new_posts()

    print(
        f"{len(posts)} nouveaux posts détectés."
    )

    flash_events = select_flash_events(
        posts
    )

    print(
        f"{len(flash_events)} événements "
        "FLASH retenus."
    )

    for post in flash_events:

        send_flash(
            post
        )


if __name__ == "__main__":

    main()
