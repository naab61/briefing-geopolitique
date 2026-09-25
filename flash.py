import os
import csv
import io
import json
import re
import urllib.request
import urllib.parse
import urllib.error
import zipfile
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta

# ============================================================
# FLASH RADAR — VERSION "TREND"
# ============================================================
# Principe:
#   - aucune liste de mots-clés pour décider qu'un FLASH existe
#   - détection d'événements émergents par propagation
#   - GDELT Events = signal mondial large
#   - Telegram public = signal social / OSINT
#   - un même événement = un seul FLASH
#   - Gemini ne fait que traduire + condenser
#
# Limite assumée:
# GitHub Actions + scraping public ne garantit pas 1-5 min.
# Ce moteur réduit fortement les retards liés aux seules sources
# Telegram, mais la planification GitHub Actions peut rester retardée.
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

SEEN_FILE = "flash_seen.json"
EVENT_FILE = "flash_events.json"
GDELT_FILE = "flash_gdelt_seen.json"

MAX_TELEGRAM_AGE_MINUTES = 30
MAX_EVENT_AGE_MINUTES = 180
EVENT_MEMORY_HOURS = 12

TELEGRAM_CHANNELS = [
    {"name": "OSINTdefender", "channel": "osintdefender"},
    {"name": "GeoConfirmed", "channel": "csources"},
    {"name": "Liveuamap", "channel": "liveuamap"},
]

GDELT_LASTUPDATE = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"


# ============================================================
# OUTILS
# ============================================================

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are",
    "has", "have", "was", "were", "into", "after", "before",
    "over", "under", "its", "their", "they", "said", "says",
    "will", "been", "being", "than",
    "les", "des", "une", "dans", "pour", "avec", "sur", "est",
    "sont", "qui", "que", "aux", "par", "mais", "plus", "sans",
    "selon", "avec", "entre", "être", "été", "ont", "pas"
}


def normalize_words(text):
    text = (text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(
        r"[^a-z0-9àâçéèêëîïôûùüÿœæ\s]",
        " ",
        text
    )
    return {
        word for word in text.split()
        if len(word) >= 4 and word not in STOPWORDS
    }


def similarity(text_a, text_b):
    a = normalize_words(text_a)
    b = normalize_words(text_b)
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def parse_iso_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except Exception:
        return None


def parse_gdelt_date(value):
    try:
        return datetime.strptime(
            str(value),
            "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def age_minutes(dt):
    if dt is None:
        return None
    return (
        datetime.now(timezone.utc) - dt
    ).total_seconds() / 60


def http_get(url, timeout=30):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                "Mozilla/5.0 "
                "(compatible; FlashTrendRadar/1.0)"
        }
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


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

        if "tgme_widget_message_wrap" in classes:
            self.in_post = True
            self.post_depth = 1
            self.in_text = False
            self.current_text = []
            self.current_post_id = attrs.get("data-post")
            self.current_date = ""
            return

        if self.in_post and tag == "div":
            self.post_depth += 1

            if (
                "tgme_widget_message" in classes
                and attrs.get("data-post")
            ):
                self.current_post_id = attrs.get("data-post")

        if (
            self.in_post
            and "tgme_widget_message_text" in classes
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
        if not self.in_post:
            return

        if tag == "div":
            self.post_depth -= 1

            if self.post_depth > 0:
                return

            text = " ".join(self.current_text).strip()

            if text and len(text) > 30:
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


def get_telegram_posts():
    posts = []

    for channel in TELEGRAM_CHANNELS:
        url = "https://t.me/s/" + channel["channel"]

        try:
            html = http_get(url).decode(
                "utf-8",
                errors="replace"
            )

            parser = TelegramPostParser()
            parser.feed(html)

            for post in parser.posts:
                post_id = post.get("post_id")
                if not post_id:
                    continue

                posts.append({
                    "kind": "telegram",
                    "source": channel["name"],
                    "source_key": post_id,
                    "text": post["text"],
                    "date": post["date"],
                    "link": "https://t.me/" + post_id,
                })

            print(
                f"Telegram {channel['name']}: "
                f"{len(parser.posts)} posts"
            )

        except Exception as exc:
            print(
                f"Telegram erreur {channel['name']}: {exc}"
            )

    return posts


# ============================================================
# GDELT EVENTS — SIGNAL MONDIAL SANS MOTS-CLÉS
# ============================================================

# Colonnes utiles du fichier Events GDELT V2.
# Les indices sont ceux du codebook officiel.
GDELT = {
    "id": 0,
    "actor1": 6,
    "actor2": 16,
    "event_code": 26,
    "quadclass": 29,
    "goldstein": 30,
    "mentions": 31,
    "sources": 32,
    "articles": 33,
    "avg_tone": 34,
    "action_geo": 50,
    "action_country": 51,
    "date_added": 56,
    "source_url": 57,
}


def get_latest_gdelt_export_url():
    raw = http_get(GDELT_LASTUPDATE).decode(
        "utf-8",
        errors="replace"
    )

    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 3 and ".export.CSV.zip" in parts[2]:
            return parts[2]

    raise RuntimeError(
        "Impossible de trouver le dernier fichier GDELT Events."
    )


def read_gdelt_events():
    url = get_latest_gdelt_export_url()
    print("GDELT dernier fichier:", url)

    data = http_get(url, timeout=60)

    events = []
    newest_added = None
    newest_age = None

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if not names:
            return []

        with archive.open(names[0]) as raw:
            reader = csv.reader(
                io.TextIOWrapper(
                    raw,
                    encoding="utf-8",
                    errors="replace"
                ),
                delimiter="\t"
            )

            for row in reader:
                if len(row) <= GDELT["source_url"]:
                    continue

                added = parse_gdelt_date(
                    row[GDELT["date_added"]]
                )

                # DATEADDED peut être vide. Dans ce cas, on utilise
                # l'horodatage du nom du fichier GDELT.
                if added is None:
                    export_match = re.search(
                        r"(\d{14})\.export\.CSV\.zip$",
                        url
                    )
                    if export_match:
                        added = parse_gdelt_date(
                            export_match.group(1)
                        )

                age = age_minutes(added)
                if age is None:
                    continue

                # Le runner GitHub peut avoir quelques minutes d'écart
                # avec l'horodatage de publication GDELT. Un événement
                # légèrement "dans le futur" doit donc rester exploitable.
                if age < 0:
                    if age < -15:
                        continue
                    age = 0

                if newest_age is None or age < newest_age:
                    newest_age = age
                    newest_added = added

                if age > MAX_EVENT_AGE_MINUTES:
                    continue

                try:
                    mentions = int(
                        row[GDELT["mentions"]] or 0
                    )
                except Exception:
                    mentions = 0

                try:
                    sources = int(
                        row[GDELT["sources"]] or 0
                    )
                except Exception:
                    sources = 0

                try:
                    articles = int(
                        row[GDELT["articles"]] or 0
                    )
                except Exception:
                    articles = 0

                event = {
                    "kind": "gdelt",
                    "source": "GDELT Events",
                    "source_key": row[GDELT["id"]],
                    "date": added.isoformat(),
                    "actor1": row[GDELT["actor1"]].strip(),
                    "actor2": row[GDELT["actor2"]].strip(),
                    "event_code": row[GDELT["event_code"]].strip(),
                    "quadclass": row[GDELT["quadclass"]].strip(),
                    "goldstein": row[GDELT["goldstein"]].strip(),
                    "mentions": mentions,
                    "sources": sources,
                    "articles": articles,
                    "location": row[GDELT["action_geo"]].strip(),
                    "country": row[GDELT["action_country"]].strip(),
                    "link": row[GDELT["source_url"]].strip(),
                    "age_minutes": age,
                }

                events.append(event)

    print(
        "GDELT événements récents:",
        len(events)
    )

    if newest_added is not None:
        print(
            "GDELT événement le plus récent:",
            newest_added.isoformat(),
            "| âge_min=",
            round(newest_age, 1)
        )
    else:
        print(
            "GDELT diagnostic: aucun événement temporel exploitable dans le fichier."
        )

    return events


# ============================================================
# TITRES / DESCRIPTIONS DES SOURCES GDELT
# ============================================================

class PageMetaParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.title = []
        self.description = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag.lower() == "title":
            self.in_title = True

        if tag.lower() == "meta":
            name = (
                attrs.get("name")
                or attrs.get("property")
                or ""
            ).lower()

            if name in {
                "description",
                "og:description",
                "twitter:description",
            }:
                self.description = (
                    attrs.get("content") or ""
                ).strip()

    def handle_data(self, data):
        if self.in_title:
            value = data.strip()
            if value:
                self.title.append(value)

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False


def get_page_context(url):
    if not url:
        return ""

    try:
        raw = http_get(url, timeout=12).decode(
            "utf-8",
            errors="replace"
        )

        parser = PageMetaParser()
        parser.feed(raw)

        title = " ".join(parser.title).strip()
        description = parser.description.strip()

        parts = [x for x in [title, description] if x]
        return " — ".join(parts)[:1200]

    except Exception as exc:
        print(
            "Contexte source inaccessible:",
            url,
            exc
        )
        return ""


# ============================================================
# PERSISTANCE
# ============================================================

def load_json_list(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_json_list(path, data, limit=500):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            data[-limit:],
            f,
            ensure_ascii=False,
            indent=2
        )


def load_seen():
    return set(load_json_list(SEEN_FILE))


def save_seen(seen):
    save_json_list(
        SEEN_FILE,
        list(seen),
        limit=2000
    )


# ============================================================
# CLUSTERING / DÉTECTION DE PROPAGATION
# ============================================================

def event_text(event):
    if event["kind"] == "gdelt":
        return " ".join(
            x for x in [
                event.get("actor1", ""),
                event.get("actor2", ""),
                event.get("location", ""),
                event.get("country", ""),
                event.get("context", ""),
            ] if x
        )

    return event.get("text", "")


def event_key(event):
    if event["kind"] == "gdelt":
        return "gdelt:" + str(
            event.get("source_key", "")
        )

    return "telegram:" + str(
        event.get("source_key", "")
    )


def cluster_score(cluster):
    sources = set(
        x.get("source", "")
        for x in cluster["signals"]
    )

    gdelt_events = [
        x for x in cluster["signals"]
        if x["kind"] == "gdelt"
    ]

    telegram_events = [
        x for x in cluster["signals"]
        if x["kind"] == "telegram"
    ]

    source_count = len(sources)

    mentions = sum(
        x.get("mentions", 0)
        for x in gdelt_events
    )

    gdelt_sources = max(
        [x.get("sources", 0) for x in gdelt_events],
        default=0
    )

    score = 0

    # Propagation multi-source.
    score += min(source_count, 5) * 3

    # Propagation GDELT mesurée.
    if gdelt_sources >= 2:
        score += 6
    if gdelt_sources >= 3:
        score += 6
    if mentions >= 5:
        score += 3
    if mentions >= 10:
        score += 5

    # Plusieurs signaux sociaux indépendants.
    if len(telegram_events) >= 2:
        score += 8
    elif len(telegram_events) == 1:
        score += 2

    # Fraîcheur.
    ages = []
    for signal in cluster["signals"]:
        if signal.get("age_minutes") is not None:
            ages.append(signal["age_minutes"])
        else:
            dt = parse_iso_date(signal.get("date", ""))
            age = age_minutes(dt)
            if age is not None:
                ages.append(age)

    if ages:
        youngest = min(ages)
        if youngest <= 5:
            score += 8
        elif youngest <= 15:
            score += 5
        elif youngest <= 30:
            score += 2

    return score


def same_cluster(a, b):
    # Le clustering ne cherche pas un mot-clé.
    # Il cherche si deux signaux décrivent suffisamment le même objet.
    ta = event_text(a)
    tb = event_text(b)

    sim = similarity(ta, tb)

    if sim >= 0.34:
        return True

    # Un signal GDELT peut être rapproché d'un post Telegram
    # si le lieu / acteurs principaux sont communs.
    location_a = (
        a.get("location")
        or a.get("country")
        or ""
    ).lower()

    location_b = (
        b.get("location")
        or b.get("country")
        or ""
    ).lower()

    if location_a and location_b:
        if location_a in tb.lower() or location_b in ta.lower():
            return sim >= 0.15

    return False


def build_clusters(signals):
    clusters = []

    # On enrichit les événements GDELT avec le contexte source
    # uniquement pour les meilleurs événements, afin de limiter
    # les requêtes HTTP.
    gdelt_sorted = sorted(
        [
            x for x in signals
            if x["kind"] == "gdelt"
        ],
        key=lambda x: (
            x.get("sources", 0),
            x.get("mentions", 0),
            -x.get("age_minutes", 9999)
        ),
        reverse=True
    )

    # Les 60 événements GDELT les plus propagés sont les seuls
    # à recevoir une récupération de titre/contexte.
    for event in gdelt_sorted[:60]:
        event["context"] = get_page_context(
            event.get("link", "")
        )

    # Telegram reste intégralement conservé comme signal social.
    telegram_signals = [
        x for x in signals
        if x["kind"] == "telegram"
    ]

    all_signals = gdelt_sorted[:60] + telegram_signals

    for signal in all_signals:
        placed = False

        for cluster in clusters:
            if any(
                same_cluster(signal, existing)
                for existing in cluster["signals"]
            ):
                cluster["signals"].append(signal)
                placed = True
                break

        if not placed:
            clusters.append({
                "signals": [signal]
            })

    for cluster in clusters:
        cluster["score"] = cluster_score(cluster)

    clusters.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return clusters


# ============================================================
# SÉLECTION DES VRAIS FLASH
# ============================================================

def select_flash_clusters(clusters):
    selected = []

    previous = load_json_list(EVENT_FILE)
    now = datetime.now(timezone.utc)

    recent_keys = set()

    for item in previous:
        dt = parse_iso_date(item.get("date", ""))
        if not dt:
            continue

        if 0 <= (now - dt).total_seconds() / 3600 <= EVENT_MEMORY_HOURS:
            recent_keys.add(
                item.get("fingerprint", "")
            )

    for cluster in clusters:
        signals = cluster["signals"]

        telegram_sources = {
            x.get("source")
            for x in signals
            if x["kind"] == "telegram"
        }

        telegram_events = [
            x for x in signals
            if x["kind"] == "telegram"
        ]

        gdelt_sources = max(
            [
                x.get("sources", 0)
                for x in signals
                if x["kind"] == "gdelt"
            ],
            default=0
        )

        gdelt_mentions = max(
            [
                x.get("mentions", 0)
                for x in signals
                if x["kind"] == "gdelt"
            ],
            default=0
        )

        # Un FLASH doit montrer une propagation réelle.
        # Trois formes sont acceptées :
        #   1) plusieurs canaux sociaux indépendants ;
        #   2) propagation GDELT mesurable ;
        #   3) plusieurs posts sociaux concordants, même sur un seul canal.
        propagated = (
            gdelt_sources >= 2
            or len(telegram_sources) >= 2
            or gdelt_mentions >= 10
            or len(telegram_events) >= 2
            or (
                len(telegram_events) >= 2
                and len(telegram_sources) == 1
                and all(
                    age_minutes(
                        parse_iso_date(x.get("date", ""))
                    ) is not None
                    and age_minutes(
                        parse_iso_date(x.get("date", ""))
                    ) <= 15
                    for x in telegram_events
                )
            )
        )

        print(
            "Cluster | score=",
            cluster["score"],
            "| telegram=",
            len(telegram_events),
            "| canaux=",
            len(telegram_sources),
            "| GDELT sources=",
            gdelt_sources,
            "| mentions=",
            gdelt_mentions,
            "| propagated=",
            propagated
        )

        if cluster["score"] < 10:
            print("  -> REJET: score trop faible")
            continue

        if not propagated:
            print("  -> REJET: propagation insuffisante")
            continue

        fingerprint = make_cluster_fingerprint(cluster)

        if fingerprint in recent_keys:
            print("  -> REJET: événement déjà envoyé")
            continue

        cluster["fingerprint"] = fingerprint
        selected.append(cluster)

        if len(selected) >= 5:
            break

    return selected


def make_cluster_fingerprint(cluster):
    pieces = []

    for signal in cluster["signals"]:
        if signal["kind"] == "gdelt":
            pieces.extend([
                signal.get("actor1", ""),
                signal.get("actor2", ""),
                signal.get("location", ""),
                signal.get("country", ""),
            ])
        else:
            pieces.append(signal.get("text", ""))

    words = sorted(
        normalize_words(" ".join(pieces))
    )

    return "|".join(words[:18])


# ============================================================
# GEMINI — TRADUCTION + CONDENSATION UNIQUEMENT
# ============================================================

def shorten_flash_with_gemini(source_text):
    prompt = f"""
Tu es uniquement un traducteur-résumeur pour un flux FLASH.

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
- Conserve "selon", "aurait", "des informations font état de", etc.
- Garde uniquement l'événement principal.
- Titre de 1 à 5 mots.
- Résumé de 1 ou 2 phrases très courtes.
- Le titre doit utiliser uniquement des éléments présents dans SOURCE.

Réponds exactement :

TITRE: [titre]
RESUME: [résumé]

Aucun autre texte.
"""

    payload = {
        "contents": [{
            "parts": [{
                "text": prompt
            }]
        }],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 140
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
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
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

        if not title_match or not summary_match:
            raise ValueError(
                "Format Gemini FLASH inattendu"
            )

        return {
            "title": title_match.group(1).strip(),
            "summary": summary_match.group(1).strip(),
        }

    except Exception as exc:
        print("ERREUR GEMINI FLASH :", exc)
        return {
            "title": "ÉVÉNEMENT",
            "summary": source_text[:500],
        }


# ============================================================
# CONSTRUCTION DU TEXTE SOURCE
# ============================================================

def build_cluster_source(cluster):
    # Priorité à un signal GDELT avec plusieurs sources.
    gdelt = sorted(
        [
            x for x in cluster["signals"]
            if x["kind"] == "gdelt"
        ],
        key=lambda x: (
            x.get("sources", 0),
            x.get("mentions", 0)
        ),
        reverse=True
    )

    telegram = [
        x for x in cluster["signals"]
        if x["kind"] == "telegram"
    ]

    parts = []

    if gdelt:
        best = gdelt[0]

        parts.append(
            "SOURCE GDELT\n"
            f"Acteur 1: {best.get('actor1', '')}\n"
            f"Acteur 2: {best.get('actor2', '')}\n"
            f"Lieu: {best.get('location', '')}\n"
            f"Pays: {best.get('country', '')}\n"
            f"Mentions: {best.get('mentions', 0)}\n"
            f"Sources distinctes: {best.get('sources', 0)}\n"
            f"Articles: {best.get('articles', 0)}\n"
            f"Contexte: {best.get('context', '')}\n"
            f"URL: {best.get('link', '')}"
        )

    if telegram:
        for post in telegram[:3]:
            parts.append(
                "SIGNAL SOCIAL TELEGRAM\n"
                f"Source: {post.get('source', '')}\n"
                f"Texte: {post.get('text', '')}\n"
                f"URL: {post.get('link', '')}"
            )

    return "\n\n".join(parts)


# ============================================================
# ENVOI TELEGRAM
# ============================================================

def format_time(date_string):
    dt = parse_iso_date(date_string)
    if dt is None:
        return date_string

    return dt.astimezone().strftime("%H:%M")


def send_flash(cluster):
    source = build_cluster_source(cluster)

    result = shorten_flash_with_gemini(source)

    title = result["title"]
    summary = result["summary"]

    primary = next(
        (
            x for x in cluster["signals"]
            if x["kind"] == "gdelt"
            and x.get("link")
        ),
        None
    )

    if primary is None:
        primary = next(
            (
                x for x in cluster["signals"]
                if x.get("link")
            ),
            None
        )

    link = (
        primary.get("link")
        if primary
        else ""
    )

    latest_date = max(
        [
            x.get("date", "")
            for x in cluster["signals"]
        ]
        or [""]
    )

    sources = sorted({
        x.get("source", "")
        for x in cluster["signals"]
        if x.get("source")
    })

    text = (
        f"🔴 <b>FLASH — {title.upper()}</b>\n\n"
        f"{summary}\n\n"
        f"🕒 {format_time(latest_date)}\n"
        f"📡 {', '.join(sources)}\n"
    )

    if link:
        text += (
            f'🔗 <a href="{link}">Source originale</a>'
        )

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method="POST"
    )

    with urllib.request.urlopen(
        request,
        timeout=30
    ) as response:
        response.read()

    events = load_json_list(EVENT_FILE)

    events.append({
        "fingerprint": cluster["fingerprint"],
        "date": latest_date,
        "score": cluster["score"],
        "sources": sources,
    })

    save_json_list(
        EVENT_FILE,
        events,
        limit=200
    )

    print(
        "FLASH envoyé | score=",
        cluster["score"],
        "| sources=",
        ", ".join(sources)
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("=== FLASH TREND RADAR ===")

    telegram_posts = get_telegram_posts()

    # Fraîcheur Telegram.
    telegram_posts = [
        post for post in telegram_posts
        if (
            age_minutes(
                parse_iso_date(
                    post.get("date", "")
                )
            ) is not None
            and 0 <= age_minutes(
                parse_iso_date(
                    post.get("date", "")
                )
            ) <= MAX_TELEGRAM_AGE_MINUTES
        )
    ]

    gdelt_events = read_gdelt_events()

    signals = gdelt_events + telegram_posts

    print(
        "Signaux récents:",
        len(signals)
    )

    if not signals:
        print("Aucun signal récent.")
        return

    clusters = build_clusters(signals)

    print(
        "Clusters:",
        len(clusters)
    )

    selected = select_flash_clusters(clusters)

    print(
        "FLASH retenus:",
        len(selected)
    )

    for cluster in selected:
        send_flash(cluster)


if __name__ == "__main__":
    main()
