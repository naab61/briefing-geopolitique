import os
import json
import re
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

STATE_FILE = "flash_seen.json"

FLASH_KEYWORDS = [
    # Conflits / sécurité
    "airstrike", "airstrikes",
    "missile", "missiles",
    "drone attack", "drone strike", "drone strikes",
    "explosion", "explosions",
    "attack", "attacks", "attacked",
    "strike", "strikes", "struck",
    "killed", "dead", "deaths", "casualties",
    "clash", "clashes",
    "fighting",
    "shelling",
    "bombing",
    "invasion", "invaded",
    "intercepted", "interception",
    "hostage", "hostages",
    "ceasefire", "truce",

    # Crises politiques / institutionnelles
    "coup",
    "coup attempt",
    "resignation",
    "resigns",
    "resigned",
    "government collapse",
    "government falls",
    "state of emergency",
    "martial law",
    "arrested",
    "arrest",
    "detained",
    "detention",

    # Manifestations / troubles majeurs
    "protest",
    "protests",
    "protesters",
    "demonstration",
    "demonstrations",
    "riot",
    "riots",
    "unrest",
    "mass protest",

    # Diplomatie / relations internationales
    "diplomatic crisis",
    "diplomatic relations",
    "ambassador",
    "expelled",
    "expels",
    "expulsion",
    "embassy",
    "break diplomatic relations",
    "diplomatic ties",

    # Mesures internationales majeures
    "sanctions",
    "sanctioned",
    "tariffs",
    "blockade",
    "blocked",
    "border closed",
    "border closure",
    "airspace closed",
    "airspace closure",

    # Catastrophes / événements majeurs
    "earthquake",
    "tsunami",
    "flood",
    "floods",
    "wildfire",
    "wildfires",
    "volcanic eruption",
    "eruption",
    "disaster",

    # Stratégique / nucléaire
    "nuclear",
    "nuclear threat",
    "nuclear test"
]

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are",
    "has", "have", "was", "were", "into", "after", "before",
    "over", "under", "its", "their", "they", "said", "says",
    "les", "des", "une", "dans", "pour", "avec", "sur", "est",
    "sont", "qui", "que", "aux", "par"
}

TELEGRAM_CHANNELS = [
    {
        "name": "OSINTdefender",
        "channel": "osintdefender",
    },
    {
        "name": "GeoConfirmed",
        "channel": "csources",
    },
    {
        "name": "OSINT Live",
        "channel": "OSINTLive",
    },
    {
        "name": "Liveuamap",
        "channel": "liveuamap",
    },
]


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

        elif self.in_post and tag == "div":
            self.post_depth += 1
            if "tgme_widget_message" in classes and attrs.get("data-post"):
                self.current_post_id = attrs.get("data-post")

        if self.in_post and "tgme_widget_message_text" in classes:
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

            text = " ".join(self.current_text).strip()

            if text and len(text) > 30:
                self.posts.append({
                    "text": text,
                    "post_id": self.current_post_id,
                    "date": self.current_date
                })

            self.in_post = False
            self.post_depth = 0
            self.in_text = False
            self.current_text = []
            self.current_post_id = None
            self.current_date = ""
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


def get_new_posts():

    seen = load_seen()

    new_posts = []

    first_run = len(seen) == 0

    for channel in TELEGRAM_CHANNELS:

        username = channel["channel"]

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
            print(f"{channel['name']} : {len(parser.posts)} posts bruts détectés")

            for post in parser.posts:

                post_id = post.get(
                    "post_id"
                )

                if not post_id:
                    print(f"POST SANS ID : {post.get('text', '')[:80]}")
                    continue

                if post_id in seen:
                    continue

                seen.add(post_id)

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
                f"Erreur {channel['name']}: {e}"
            )

    print(f"Total avant dédoublonnage : {len(new_posts)} nouveaux posts")

    if first_run:
        print("Premier lancement : flux initial mémorisé, aucun FLASH historique envoyé.")
        return []
    
    save_seen(seen)

    return new_posts

def shorten_flash_with_gemini(source_text):

    prompt = f"""
Tu es un traducteur-résumeur pour un flux FLASH géopolitique.

SOURCE :
{source_text}

Consignes STRICTES :

- Traduis le texte en français.
- Réduis-le à 1 ou 2 phrases très courtes.
- Conserve uniquement les informations explicitement présentes dans SOURCE.
- N'ajoute absolument aucune information provenant de tes connaissances.
- N'ajoute aucun contexte.
- N'ajoute aucune analyse.
- N'ajoute aucune interprétation.
- Ne déduis rien.
- Ne transforme jamais une information incertaine en fait certain.
- Conserve les nuances telles que "selon", "aurait", "des informations font état de", etc.
- Si le texte contient plusieurs informations, conserve uniquement l'information principale de l'événement.
- Réponds UNIQUEMENT avec le texte final en français.
- Aucun titre.
- Aucun emoji.
- Aucun commentaire.

SOURCE :
{source_text}
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

    data = json.dumps(payload).encode("utf-8")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:generateContent?key="
        + GEMINI_API_KEY
    )

    request = urllib.request.Request(
        url,
        data=data,
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

        return (
            result["candidates"][0]["content"]["parts"][0]["text"]
            .strip()
        )

    except Exception as e:

        print(
            "ERREUR GEMINI FLASH :",
            e
        )

        # En cas d'échec, on conserve le texte original.
        return source_text

def send_flash(post):

    short_text = shorten_flash_with_gemini(
        post["text"]
    )

    text = (
        "⚡ <b>FLASH</b>\n\n"
        f"{short_text}\n\n"
        f"🕒 {post['date']}\n"
        f"📡 {post['channel']}\n"
        f"🔗 <a href=\"{post['link']}\">"
        f"Source originale"
        f"</a>"
    )

    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

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

    except urllib.error.HTTPError as e:

        error_body = e.read().decode(
            "utf-8",
            errors="replace"
        )

        print(
            "ERREUR TELEGRAM FLASH :",
            error_body
        )

        raise

def normalize_words(text):
    text = text.lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9àâçéèêëîïôûùüÿœæ\s]", " ", text)

    words = {
        word
        for word in text.split()
        if len(word) >= 4 and word not in STOPWORDS
    }

    return words


def is_flash_candidate(post):
    text = post.get("text", "").lower()
    return any(keyword in text for keyword in FLASH_KEYWORDS)


def same_event(post1, post2):

    words1 = normalize_words(post1["text"])
    words2 = normalize_words(post2["text"])

    if not words1 or not words2:
        return False

    common = words1 & words2

    # Il faut au moins plusieurs mots significatifs en commun
    if len(common) >= 4:
        return True

    # Pour les messages très courts, on reste plus prudent
    if len(common) >= 3:
        smaller = min(len(words1), len(words2))

        if smaller <= 8:
            return True

    return False


def select_flash_events(posts):

    candidates = [
        post for post in posts
        if is_flash_candidate(post)
    ]

    selected = []

    for post in candidates:

        duplicate = False

        for existing in selected:

            if same_event(post, existing):
                duplicate = True
                break

        if not duplicate:
            selected.append(post)

    return selected

def main():
    posts = get_new_posts()

    print(f"{len(posts)} nouveaux posts détectés.")

    flash_events = select_flash_events(posts)

    print(f"{len(flash_events)} événements FLASH retenus.")

    for post in flash_events:
        send_flash(post)
        print("FLASH envoyé :", post["channel"], post["post_id"])

if __name__ == "__main__":

    main()
