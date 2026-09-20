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

STATE_FILE = "flash_seen.json"

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


def send_flash(post):

    text = (
        "⚡ <b>FLASH</b>\n\n"
        f"{post['text']}\n\n"
        f"🕒 {post['date']}\n"
        f"📡 {post['channel']}\n"
        f"🔗 <a href=\"{post['link']}\">"
        f"Source originale"
        f"</a>"
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

    with urllib.request.urlopen(
        request,
        timeout=30
    ) as response:

        response.read()


FLASH_KEYWORDS = [
    "airstrike", "airstrikes", "missile", "missiles",
    "drone attack", "drone strike", "drone strikes",
    "explosion", "explosions",
    "attack", "attacks", "attacked",
    "strike", "strikes", "struck",
    "killed", "dead", "deaths", "casualties",
    "ceasefire", "truce",
    "invasion", "invaded",
    "intercepted", "interception",
    "hostage", "hostages",
    "earthquake", "tsunami",
    "nuclear"
]

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are",
    "has", "have", "was", "were", "into", "after", "before",
    "over", "under", "its", "their", "they", "said", "says",
    "les", "des", "une", "dans", "pour", "avec", "sur", "est",
    "sont", "qui", "que", "aux", "par"
}


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
    union = words1 | words2

    similarity = len(common) / len(union)

    return similarity >= 0.45


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

def test_flash():
    post = {
        "channel": "TEST",
        "post_id": "TEST",
        "text": "🧪 TEST FLASH — message fictif pour vérifier le routage Telegram.",
        "link": "https://t.me/Geopolitique_flash",
        "date": datetime.now(timezone.utc).isoformat(),
    }
    send_flash(post)


if __name__ == "__main__":
    test_flash()
    # main()

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
