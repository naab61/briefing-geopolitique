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
from zoneinfo import ZoneInfo

# ============================================================
# FLASH RADAR — VERSION "TREND V12 — RADAR SOCIAL ÉMERGENT"
# ============================================================
# Principe:
#   - aucune liste de mots-clés pour décider qu'un FLASH existe
#   - détection d'événements émergents par propagation
#   - GDELT Events = signal mondial large
#   - Telegram public = signal social / OSINT
#   - X + Reddit = capteurs de tendances émergentes
#   - aucune liste de thèmes : le moteur découvre les sujets
#   - un même événement = un seul FLASH
#   - Gemini interprète et condense les signaux retenus
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
EVENT_MEMORY_HOURS = 24
MAX_FLASHES_PER_RUN = 2
MAX_TREND_FLASHES_PER_RUN = 2
TREND_MEMORY_HOURS = 24
TREND_EVENT_FILE = "flash_trends.json"
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "").strip()
X_QUERIES = [q.strip() for q in os.environ.get("X_QUERIES", "-is:retweet").split("||") if q.strip()]
REDDIT_SUBREDDITS = [
    q.strip()
    for q in os.environ.get("REDDIT_SUBREDDITS", "all").split(",")
    if q.strip()
]
PARIS_TZ = ZoneInfo("Europe/Paris")

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
# RADAR SOCIAL — TENDANCES ÉMERGENTES, SANS LISTE DE THÈMES
# ============================================================

def get_x_signals():
    """Collecte large de posts récents X.

    Aucun thème n'est imposé. La requête par défaut est uniquement
    structurelle (-is:retweet), afin de laisser le moteur découvrir les sujets.
    Une ou plusieurs requêtes peuvent être fournies via X_QUERIES, séparées
    par ||, sans que ces requêtes deviennent des critères éditoriaux.
    """
    if not X_BEARER_TOKEN:
        print("X: désactivé (X_BEARER_TOKEN absent)")
        return []

    signals = []
    endpoint = "https://api.x.com/2/tweets/search/recent"
    fields = (
        "created_at,author_id,public_metrics,lang,conversation_id,"
        "attachments,entities"
    )

    for query in X_QUERIES[:5]:
        params = urllib.parse.urlencode({
            "query": query,
            "max_results": "100",
            "tweet.fields": fields,
            "expansions": "author_id,attachments.media_keys",
            "user.fields": "username,name,verified",
            "media.fields": "type,url,preview_image_url,public_metrics",
        })
        url = endpoint + "?" + params
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + X_BEARER_TOKEN,
                "User-Agent": "FlashTrendRadar/12.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            print("X erreur:", exc)
            continue

        users = {
            u.get("id"): u
            for u in payload.get("includes", {}).get("users", [])
        }
        media = {
            m.get("media_key"): m
            for m in payload.get("includes", {}).get("media", [])
        }

        for post in payload.get("data", []):
            created = post.get("created_at", "")
            dt = parse_iso_date(created)
            age = age_minutes(dt)
            if age is None or age < 0 or age > 30:
                continue

            metrics = post.get("public_metrics") or {}
            author = users.get(post.get("author_id"), {})
            media_items = []
            for key in (post.get("attachments") or {}).get("media_keys", []):
                if key in media:
                    media_items.append(media[key])

            engagement = (
                metrics.get("like_count", 0)
                + 3 * metrics.get("reply_count", 0)
                + 4 * metrics.get("retweet_count", 0)
                + 5 * metrics.get("quote_count", 0)
            )

            signals.append({
                "kind": "x",
                "source": "X",
                "source_key": post.get("id", ""),
                "text": post.get("text", ""),
                "date": created,
                "link": "https://x.com/i/web/status/" + str(post.get("id", "")),
                "author": author.get("username", ""),
                "author_name": author.get("name", ""),
                "verified": bool(author.get("verified", False)),
                "lang": post.get("lang", ""),
                "metrics": metrics,
                "engagement": engagement,
                "media": media_items,
                "age_minutes": age,
            })

    # Déduplication entre requêtes X.
    unique = {}
    for item in signals:
        unique[item["source_key"]] = item
    signals = list(unique.values())
    print("X: %d signaux récents" % len(signals))
    return signals


def get_reddit_signals():
    """Collecte publique de posts récents Reddit, sans sélection thématique."""
    signals = []

    for subreddit in REDDIT_SUBREDDITS[:10]:
        endpoint = (
            "https://www.reddit.com/r/" + urllib.parse.quote(subreddit) +
            "/new.json?limit=100&raw_json=1"
        )
        try:
            raw = http_get(endpoint, timeout=30).decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except Exception as exc:
            print("Reddit erreur %s:" % subreddit, exc)
            continue

        children = (payload.get("data") or {}).get("children") or []
        for child in children:
            post = child.get("data") or {}
            created_ts = post.get("created_utc")
            try:
                dt = datetime.fromtimestamp(float(created_ts), tz=timezone.utc)
            except Exception:
                continue

            age = age_minutes(dt)
            if age is None or age < 0 or age > 60:
                continue

            score = max(0, int(post.get("score") or 0))
            comments = max(0, int(post.get("num_comments") or 0))
            engagement = score + 3 * comments
            title = post.get("title", "")
            body = post.get("selftext", "") or ""
            text = (title + "\n" + body).strip()
            if len(text) < 10:
                text = title or body

            signals.append({
                "kind": "reddit",
                "source": "Reddit",
                "source_key": post.get("id", ""),
                "text": text[:5000],
                "date": dt.isoformat(),
                "link": "https://www.reddit.com" + str(post.get("permalink", "")),
                "subreddit": post.get("subreddit", subreddit),
                "author": post.get("author", ""),
                "metrics": {
                    "score": score,
                    "comment_count": comments,
                },
                "engagement": engagement,
                "age_minutes": age,
                "media": [],
            })

    unique = {}
    for item in signals:
        unique[item["source_key"]] = item
    signals = list(unique.values())
    print("Reddit: %d signaux récents" % len(signals))
    return signals


def trend_similarity(a, b):
    # Le seuil reste volontairement modéré : les internautes peuvent
    # parler du même phénomène avec des formulations complètement différentes.
    sa = similarity(a.get("text", ""), b.get("text", ""))
    if sa >= 0.30:
        return sa

    # Les URLs identiques sont un excellent signal de propagation.
    ua = str(a.get("link", "")).split("?")[0].lower()
    ub = str(b.get("link", "")).split("?")[0].lower()
    if ua and ub and ua == ub:
        return 1.0

    return sa


def trend_cluster_score(cluster):
    signals = cluster["signals"]
    platform_count = len(set(x.get("source") for x in signals))
    total_engagement = sum(x.get("engagement", 0) for x in signals)
    ages = [x.get("age_minutes") for x in signals if x.get("age_minutes") is not None]
    youngest = min(ages) if ages else 60

    # On mesure la dynamique, pas la présence d'un sujet particulier.
    score = min(platform_count, 4) * 8
    score += min(len(signals), 12) * 2
    score += min(total_engagement / 20.0, 35)

    if youngest <= 5:
        score += 20
    elif youngest <= 15:
        score += 12
    elif youngest <= 30:
        score += 6

    # Plusieurs auteurs / communautés indépendants augmentent la robustesse.
    authors = {x.get("author") for x in signals if x.get("author")}
    communities = {x.get("subreddit") for x in signals if x.get("subreddit")}
    score += min(len(authors), 10) * 1.5
    score += min(len(communities), 8) * 2

    return round(score, 2)


def build_trend_clusters(signals):
    clusters = []

    # Priorité aux signaux ayant déjà de la traction, tout en gardant les
    # signaux récents : cela permet de découvrir une montée avant le pic.
    ordered = sorted(
        signals,
        key=lambda x: (
            x.get("engagement", 0),
            -float(x.get("age_minutes") or 60),
        ),
        reverse=True,
    )

    for signal in ordered[:300]:
        best = None
        best_sim = 0.0
        for cluster in clusters:
            representative = max(
                cluster["signals"],
                key=lambda x: x.get("engagement", 0),
            )
            sim = trend_similarity(signal, representative)
            if sim >= 0.24 and sim > best_sim:
                best = cluster
                best_sim = sim

        if best is None:
            clusters.append({"signals": [signal]})
        else:
            best["signals"].append(signal)

    for cluster in clusters:
        cluster["score"] = trend_cluster_score(cluster)
        cluster["fingerprint"] = make_trend_fingerprint(cluster)

    return sorted(clusters, key=lambda x: x["score"], reverse=True)


def make_trend_fingerprint(cluster):
    # Fingerprint stable par contenu + plateforme, sans dépendre d'un texte
    # exact ou d'un seul post.
    texts = [x.get("text", "") for x in cluster["signals"]]
    words = sorted(normalize_words(" ".join(texts)))
    platforms = sorted(set(x.get("source", "") for x in cluster["signals"]))
    return "|".join(platforms) + "::" + "|".join(words[:24])


def trend_is_duplicate(cluster, previous):
    current_text = " ".join(x.get("text", "") for x in cluster["signals"][:12])
    for item in previous:
        try:
            dt = parse_iso_date(item.get("detected_at", ""))
            if dt is None or age_minutes(dt) < 0 or age_minutes(dt) > TREND_MEMORY_HOURS * 60:
                continue
            old_text = item.get("text", "")
            if similarity(current_text, old_text) >= 0.34:
                return True
            if item.get("fingerprint") == cluster.get("fingerprint"):
                return True
        except Exception:
            continue
    return False


def select_trend_clusters(clusters):
    previous = load_json_list(TREND_EVENT_FILE)
    selected = []

    for cluster in clusters:
        if cluster["score"] < 18:
            continue
        if trend_is_duplicate(cluster, previous):
            continue
        selected.append(cluster)
        if len(selected) >= MAX_TREND_FLASHES_PER_RUN:
            break

    return selected


def build_trend_source(cluster):
    parts = []
    for i, item in enumerate(cluster["signals"][:12], 1):
        metrics = item.get("metrics", {})
        parts.append(
            f"SIGNAL {i}\n"
            f"Plateforme: {item.get('source','')}\n"
            f"Auteur: {item.get('author_name') or item.get('author','')}\n"
            f"Communauté: {item.get('subreddit','')}\n"
            f"Âge du signal: {item.get('age_minutes', '')} minutes\n"
            f"Engagement: {item.get('engagement', 0)}\n"
            f"Métriques: {json.dumps(metrics, ensure_ascii=False)}\n"
            f"Texte: {item.get('text','')}\n"
            f"URL: {item.get('link','')}"
        )
    return "\n\n".join(parts)


def analyze_trend_with_gemini(source_text):
    prompt = f"""
Tu analyses une tendance émergente sur Internet à partir de publications publiques.
Il est INTERDIT de supposer à l'avance un domaine ou un thème : cela peut concerner
n'importe quoi. Ne rejette pas un sujet parce qu'il paraît banal. Ce qui compte est
la dynamique d'attention observée et la cohérence du phénomène.

Détermine :
1) ce dont les internautes parlent réellement ;
2) pourquoi ces publications semblent former un même phénomène ;
3) ce qui est observable sur sa diffusion ;
4) un titre français court et factuel.

Ne transforme pas une rumeur en fait. Si une affirmation n'est pas vérifiée,
dis-le explicitement. N'invente aucune donnée d'audience.

Réponds exactement :
OUI
TITRE: ...
RESUME: ...
POURQUOI: ...

ou uniquement :
NON

DONNEES:
{source_text}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 260},
    }
    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-3.5-flash-lite:generateContent?key="
        + GEMINI_API_KEY
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
        if re.fullmatch(r"NON", text, flags=re.I):
            return None
        title = re.search(r"(?im)^TITRE:\s*(.+)$", text)
        summary = re.search(r"(?im)^RESUME:\s*(.+)$", text)
        why = re.search(r"(?im)^POURQUOI:\s*(.+)$", text)
        if not title or not summary:
            return None
        return {
            "title": title.group(1).strip(),
            "summary": summary.group(1).strip(),
            "why": why.group(1).strip() if why else "",
        }
    except Exception as exc:
        print("ERREUR GEMINI TENDANCE:", exc)
        return None


def send_trend(cluster):
    source = build_trend_source(cluster)
    result = analyze_trend_with_gemini(source)
    if result is None:
        print("Tendance rejetée par Gemini")
        return False

    detected_at = datetime.now(timezone.utc)
    strongest = max(
        cluster["signals"],
        key=lambda x: x.get("engagement", 0),
    )
    source_date = parse_iso_date(str(strongest.get("date", "")))
    platforms = sorted(set(x.get("source", "") for x in cluster["signals"]))
    link = strongest.get("link", "")

    text = (
        f"🔥 <b>TENDANCE — {result['title'].upper()}</b>\n\n"
        f"{result['summary']}\n\n"
        f"📈 {result['why']}\n"
        f"🌐 {', '.join(platforms)}\n"
        f"🕒 Détecté : {detected_at.astimezone(PARIS_TZ).strftime('%H:%M')}\n"
    )
    if source_date:
        text += f"📰 Signal source : {source_date.astimezone(PARIS_TZ).strftime('%H:%M')}\n"
    if link.startswith(("http://", "https://")):
        safe_link = link.replace("&", "&amp;").replace('"', "&quot;")
        text += f'🔗 <a href="{safe_link}">Voir le signal</a>'

    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    sent_at = datetime.now(timezone.utc)
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()
    sent_at = datetime.now(timezone.utc)

    history = load_json_list(TREND_EVENT_FILE)
    history.append({
        "fingerprint": cluster["fingerprint"],
        "text": " ".join(x.get("text", "") for x in cluster["signals"][:12]),
        "detected_at": detected_at.isoformat(),
        "sent_at": sent_at.isoformat(),
        "score": cluster["score"],
        "sources": platforms,
    })
    save_json_list(TREND_EVENT_FILE, history, limit=500)
    print("TENDANCE envoyée | score=", cluster["score"], "|", ", ".join(platforms))
    return True


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
    "date_added": 59,
    "source_url": 60,
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


AGENCY_MARKERS = (
    "by reuters",
    "reuters /",
    "reuters/",
    "© reuters",
    "reuters news agency",
    "reuters news",
    "agence reuters",
    "selon reuters",
    "d après reuters",
    "d'apres reuters",
    "afp",
    "agence france-presse",
    "associated press",
    "ap news",
    "by the associated press",
)

AGENCY_DOMAINS = (
    "reuters.com",
    "afp.com",
    "apnews.com",
    "ap.org",
)

def detect_agency(event):
    """Return the canonical news agency behind a signal, if any.

    Direct agency articles are valid sources. A third-party article that
    explicitly credits an agency is attached to that agency and does not
    count as an additional independent source.
    """
    url = str(event.get("link") or "").lower()
    context = str(event.get("context") or "").lower()

    domain_map = {
        "reuters.com": "Reuters",
        "afp.com": "AFP",
        "apnews.com": "Associated Press",
        "ap.org": "Associated Press",
        "efe.com": "EFE",
    }

    for domain, agency in domain_map.items():
        if domain in url:
            return agency, True

    marker_map = (
        ("by reuters", "Reuters"),
        ("reuters /", "Reuters"),
        ("reuters/", "Reuters"),
        ("© reuters", "Reuters"),
        ("reuters news agency", "Reuters"),
        ("reuters news", "Reuters"),
        ("agence reuters", "Reuters"),
        ("selon reuters", "Reuters"),
        ("d après reuters", "Reuters"),
        ("d'apres reuters", "Reuters"),
        ("agence france-presse", "AFP"),
        ("by afp", "AFP"),
        ("associated press", "Associated Press"),
        ("ap news", "Associated Press"),
        ("by the associated press", "Associated Press"),
        ("agencia efe", "EFE"),
        ("by efe", "EFE"),
    )

    for marker, agency in marker_map:
        if marker in context:
            return agency, False

    return "", False


def is_agency_syndicated(event):
    agency, direct = detect_agency(event)
    event["agency"] = agency
    event["agency_direct"] = direct
    return bool(agency) and not direct


def get_page_context(url):
    if not url or not str(url).startswith(("http://", "https://")):
        return ""

    try:
        raw = http_get(url, timeout=6).decode(
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

    # Propagation multi-source explicite (Telegram / autres signaux).
    score += min(source_count, 5) * 3

    # Propagation GDELT mesurée : NumSources est le nombre de médias
    # distincts ayant mentionné l'événement dans la fenêtre de 15 min.
    if gdelt_sources >= 2:
        score += 6
    if gdelt_sources >= 3:
        score += 6
    if gdelt_sources >= 5:
        score += 4
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
    # Un même événement doit être regroupé sans fusionner
    # des informations seulement parce qu'elles partagent
    # quelques mots génériques.
    if a.get("kind") == "gdelt" and b.get("kind") == "gdelt":
        ua = str(a.get("link") or "").strip()
        ub = str(b.get("link") or "").strip()
        if ua and ub and ua == ub:
            return True

        ca = str(a.get("country") or "").strip().lower()
        cb = str(b.get("country") or "").strip().lower()
        la = str(a.get("location") or "").strip().lower()
        lb = str(b.get("location") or "").strip().lower()

        # Même pays + lieu commun + similarité suffisante.
        if ca and cb and ca == cb:
            sim = similarity(event_text(a), event_text(b))
            if la and lb and (la in lb or lb in la) and sim >= 0.22:
                return True
            if sim >= 0.52:
                return True
        return False

    ta = event_text(a)
    tb = event_text(b)
    sim = similarity(ta, tb)
    if sim >= 0.55:
        return True

    # Pour Telegram/GDELT, on exige un pays ou lieu partagé
    # avant d'accepter une similarité faible.
    location_a = str(a.get("location") or a.get("country") or "").lower()
    location_b = str(b.get("location") or b.get("country") or "").lower()
    if location_a and location_b:
        if location_a in tb.lower() or location_b in ta.lower():
            return sim >= 0.25
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
    for event in gdelt_sorted[:15]:
        event["context"] = get_page_context(
            event.get("link", "")
        )
        event["agency_syndicated"] = is_agency_syndicated(event)
        if event.get("agency"):
            kind = "direct" if event.get("agency_direct") else "reprise"
            print(
                f"GDELT agence détectée: {event['agency']} ({kind}) |",
                event.get("link", "")
            )

    # Les agences fiables sont AUTORISÉES comme sources.
    # Une reprise d'une agence reste toutefois rattachée à cette agence
    # et ne compte pas comme une source indépendante supplémentaire.
    gdelt_valid = gdelt_sorted[:60]

    # Telegram reste intégralement conservé comme signal social.
    telegram_signals = [
        x for x in signals
        if x["kind"] == "telegram"
    ]

    all_signals = gdelt_valid + telegram_signals

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
    recent_memory = []

    for item in previous:
        dt = parse_iso_date(item.get("detected_at") or item.get("date", ""))
        if not dt:
            continue

        age_h = (now - dt).total_seconds() / 3600
        if 0 <= age_h <= EVENT_MEMORY_HOURS:
            recent_keys.add(item.get("fingerprint", ""))
            recent_memory.append(item)

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

        valid_gdelt = [
            x for x in signals
            if x["kind"] == "gdelt"
            and not x.get("agency_syndicated", False)
        ]

        # Identité canonique des sources : une reprise de Reuters sur un
        # site tiers reste Reuters et ne crée pas une seconde confirmation.
        canonical_sources = set()
        independent_domains = set()
        direct_agencies = set()

        for x in valid_gdelt:
            agency = x.get("agency") or ""
            if agency:
                canonical_sources.add(f"agency:{agency}")
                if x.get("agency_direct"):
                    direct_agencies.add(agency)
            else:
                link = str(x.get("link") or "").strip()
                m = re.match(r"https?://([^/]+)", link.lower())
                if m:
                    domain = m.group(1).split(":")[0].removeprefix("www.")
                    independent_domains.add(domain)
                    canonical_sources.add(f"domain:{domain}")

        # Une reprise d'agence ne compte donc jamais comme un domaine
        # indépendant supplémentaire.
        # IMPORTANT : dans GDELT Events, NumSources (champ "sources")
        # est déjà le nombre de sources distinctes ayant mentionné cet
        # événement pendant la fenêtre de 15 minutes. On ne doit donc pas
        # compter seulement les lignes GDELT présentes dans notre cluster.
        # Une seule ligne GDELT peut représenter 2, 10 ou 20 médias distincts.
        gdelt_coverage_sources = max(
            [x.get("sources", 0) for x in signals if x["kind"] == "gdelt"],
            default=0
        )

        gdelt_articles = max(
            [x.get("articles", 0) for x in signals if x["kind"] == "gdelt"],
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

        # Sources canoniques connues explicitement par nos signaux.
        # Elles sont utiles pour l'affichage, mais ne remplacent PAS
        # NumSources pour mesurer la couverture GDELT.
        gdelt_canonical_sources = len(canonical_sources)

        # Un FLASH doit montrer une propagation réelle.
        # NumSources est ici le meilleur indicateur disponible dans le
        # fichier Events : il compte les sources distinctes, contrairement
        # à NumMentions qui mesure les mentions/documents.
        propagated = (
            gdelt_coverage_sources >= 3
            or (
                gdelt_coverage_sources >= 2
                and gdelt_articles >= 2
            )
            or len(telegram_sources) >= 2
            or len(telegram_events) >= 3
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
            gdelt_coverage_sources,
            "| GDELT articles=",
            gdelt_articles,
            "| mentions=",
            gdelt_mentions,
            "| propagated=",
            propagated
        )

        # Une seule ligne GDELT peut parfaitement représenter plusieurs
        # médias indépendants : on ne rejette donc plus sur le simple
        # nombre de liens GDELT présents dans le cluster.
        if (
            not telegram_events
            and gdelt_coverage_sources < 2
        ):
            print("  -> REJET: une seule source GDELT")
            continue

        if cluster["score"] < 16:
            print("  -> REJET: score trop faible")
            continue

        if not propagated:
            print("  -> REJET: propagation insuffisante")
            continue

        fingerprint = make_cluster_fingerprint(cluster)

        if fingerprint in recent_keys:
            print("  -> REJET: événement déjà envoyé")
            continue

        current_text = event_text(cluster["signals"][0]) + " " + " ".join(
            event_text(x) for x in cluster["signals"][1:6]
        )
        lexical_duplicate = False
        for old_event in recent_memory:
            old_text = old_event.get("semantic_text", "")
            if old_text and similarity(current_text, old_text) >= 0.34:
                lexical_duplicate = True
                break
        if lexical_duplicate:
            print("  -> REJET: événement sémantiquement déjà couvert (mémoire 24h)")
            continue

        cluster["fingerprint"] = fingerprint
        selected.append(cluster)

        if len(selected) >= MAX_FLASHES_PER_RUN:
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
Tu es le filtre éditorial final d'un radar mondial de breaking news.

SOURCE :
{source_text}

Ta mission n'est PAS de vérifier seulement s'il existe plusieurs URLs.
Tu dois décider si le cluster décrit un événement NOUVEAU, RÉELLEMENT IMPORTANT
et suffisamment clair pour mériter une alerte immédiate.

INTERPRÉTATION DE LA COUVERTURE GDELT :
- "Sources distinctes dans GDELT" = nombre de sources d'information distinctes ayant couvert l'événement.
- "Articles" = nombre de documents couvrant l'événement.
- "Mentions" = intensité des mentions de l'événement.
- Une seule ligne SOURCE peut donc représenter plusieurs médias indépendants.
- Si GDELT indique au moins 3 sources distinctes et plusieurs articles, NE considère PAS cela comme "une seule source".

AGENCES :
- Reuters, AFP, Associated Press, EFE et autres agences fiables sont des sources autorisées.
- Une dépêche Reuters reprise par plusieurs sites reste UNE source canonique Reuters.
- Une reprise d'agence ne doit pas être comptée comme une confirmation indépendante supplémentaire.
- Une agence directe peut parfaitement déclencher un FLASH si l'événement est majeur ou réellement émergent.

REFUSE (REPONSE: NON) si c'est :
- une déclaration, demande ou réaction politique ordinaire sans événement nouveau majeur ;
- une visite, réunion ou annonce institutionnelle de routine ;
- un petit contrat, partenariat, protocole ou accord universitaire/commercial ;
- une analyse, opinion, commentaire ou simple résumé ;
- une information ambiguë, mal comprise, contradictoire ou sans fait concret identifiable ;
- un événement ancien simplement remis en contexte ;
- une actualité locale ou sectorielle sans importance notable ;
- une information dont le cluster ne montre ni propagation suffisante ni impact important.

ACCEPTE si AU MOINS UNE des conditions suivantes est satisfaite :
1. le cluster montre une propagation rapide et claire (par exemple 3+ sources GDELT distinctes et plusieurs articles) ET l'événement est important ;
2. une agence fiable directe rapporte un événement majeur et manifestement nouveau ;
3. plusieurs signaux sociaux indépendants montrent qu'un événement est en train de devenir viral ou important ;
4. l'événement a un impact humain, sécuritaire, politique, diplomatique, économique, environnemental ou international suffisamment important pour justifier une alerte immédiate.

Ne rejette donc PAS automatiquement un cluster parce qu'il n'y a qu'une URL affichée dans SOURCE :
regarde les champs "Sources distinctes dans GDELT", "Articles" et "Mentions".

Le résumé doit expliquer LE FAIT NOUVEAU, pas répéter un nom d'acteur ou une catégorie GDELT.
Le titre doit être compréhensible seul et contenir le pays ou le lieu principal lorsqu'il est connu.

Si REFUS : réponds exactement :
NON

Sinon, réponds exactement :
OUI
TITRE: [3 à 8 mots, pays ou lieu si connu]
RESUME: [1 ou 2 phrases courtes décrivant le fait nouveau et pourquoi il justifie l'alerte]
PAYS: [pays ou lieu principal]

Ne rajoute aucun autre texte.
"""
    payload={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.0,"maxOutputTokens":180}}
    url=("https://generativelanguage.googleapis.com/"
         "v1beta/models/gemini-3.5-flash-lite:generateContent?key="+GEMINI_API_KEY)
    request=urllib.request.Request(url,data=json.dumps(payload).encode("utf-8"),headers={"Content-Type":"application/json"},method="POST")
    try:
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result=json.loads(response.read().decode("utf-8"))
        except Exception as first_exc:
            # Deuxième tentative avec un prompt plus court : les clusters très riches
            # peuvent dépasser le temps de lecture du premier appel.
            compact = source_text[:6500]
            retry_prompt = prompt.replace(source_text, compact)
            retry_payload={"contents":[{"parts":[{"text":retry_prompt}]}],"generationConfig":{"temperature":0.0,"maxOutputTokens":180}}
            retry_request=urllib.request.Request(url,data=json.dumps(retry_payload).encode("utf-8"),headers={"Content-Type":"application/json"},method="POST")
            with urllib.request.urlopen(retry_request, timeout=60) as response:
                result=json.loads(response.read().decode("utf-8"))
        text=result["candidates"][0]["content"]["parts"][0]["text"].strip()
        if re.search(r"(?im)^NON\s*$", text):
            return None
        title=re.search(r"(?im)^TITRE:\s*(.+)$",text)
        summary=re.search(r"(?im)^RESUME:\s*(.+)$",text)
        country=re.search(r"(?im)^PAYS:\s*(.+)$",text)
        if not title or not summary:
            raise ValueError("Format Gemini FLASH inattendu")
        return {"title":title.group(1).strip(),"summary":summary.group(1).strip(),"country":country.group(1).strip() if country else ""}
    except Exception as exc:
        print("ERREUR GEMINI FLASH :",exc)
        return None


# ============================================================
# CONSTRUCTION DU TEXTE SOURCE
# ============================================================

COUNTRY_NAMES = {
    "AF":"Afghanistan","AL":"Albanie","DZ":"Algérie","AR":"Argentine",
    "AM":"Arménie","AU":"Australie","AT":"Autriche","AZ":"Azerbaïdjan",
    "BH":"Bahreïn","BD":"Bangladesh","BY":"Biélorussie","BE":"Belgique",
    "BO":"Bolivie","BA":"Bosnie-Herzégovine","BR":"Brésil","BG":"Bulgarie",
    "CA":"Canada","CL":"Chili","CN":"Chine","CO":"Colombie","HR":"Croatie",
    "CU":"Cuba","CY":"Chypre","CZ":"Tchéquie","CD":"RDC","DK":"Danemark",
    "EG":"Égypte","EE":"Estonie","ET":"Éthiopie","FI":"Finlande","FR":"France",
    "GE":"Géorgie","DE":"Allemagne","GH":"Ghana","GR":"Grèce","HU":"Hongrie",
    "IS":"Islande","IN":"Inde","ID":"Indonésie","IR":"Iran","IQ":"Irak",
    "IE":"Irlande","IL":"Israël","IT":"Italie","JP":"Japon","JO":"Jordanie",
    "KZ":"Kazakhstan","KE":"Kenya","KR":"Corée du Sud","KW":"Koweït",
    "LV":"Lettonie","LB":"Liban","LY":"Libye","LT":"Lituanie","LU":"Luxembourg",
    "MY":"Malaisie","MT":"Malte","MX":"Mexique","MD":"Moldavie","MN":"Mongolie",
    "MA":"Maroc","MZ":"Mozambique","MM":"Myanmar","NP":"Népal","NL":"Pays-Bas",
    "NZ":"Nouvelle-Zélande","NG":"Nigeria","KP":"Corée du Nord","NO":"Norvège",
    "OM":"Oman","PK":"Pakistan","PS":"Palestine","PA":"Panama","PE":"Pérou",
    "PH":"Philippines","PL":"Pologne","PT":"Portugal","QA":"Qatar","RO":"Roumanie",
    "RU":"Russie","SA":"Arabie saoudite","RS":"Serbie","SG":"Singapour","SK":"Slovaquie",
    "SI":"Slovénie","ZA":"Afrique du Sud","ES":"Espagne","LK":"Sri Lanka","SD":"Soudan",
    "SE":"Suède","CH":"Suisse","SY":"Syrie","TW":"Taïwan","TH":"Thaïlande",
    "TN":"Tunisie","TR":"Turquie","UA":"Ukraine","AE":"Émirats arabes unis",
    "GB":"Royaume-Uni","US":"États-Unis","UY":"Uruguay","UZ":"Ouzbékistan",
    "VE":"Venezuela","VN":"Vietnam","YE":"Yémen","ZM":"Zambie","ZW":"Zimbabwe"
}

def country_name(code):
    code = str(code or "").strip().upper()
    return COUNTRY_NAMES.get(code, code)

def build_cluster_source(cluster):
    gdelt = sorted(
        [x for x in cluster["signals"] if x["kind"] == "gdelt"],
        key=lambda x: (x.get("sources",0), x.get("mentions",0), x.get("articles",0)),
        reverse=True
    )
    telegram = [x for x in cluster["signals"] if x["kind"] == "telegram"]
    parts = []

    # Plusieurs sources sont envoyées à Gemini : il doit pouvoir distinguer
    # un vrai événement repris par plusieurs médias d'une simple déclaration.
    for i, item in enumerate(gdelt[:5], 1):
        parts.append(
            f"SOURCE {i}\n"
            f"Pays: {country_name(item.get('country',''))}\n"
            f"Lieu: {item.get('location','')}\n"
            f"Acteur 1: {item.get('actor1','')}\n"
            f"Acteur 2: {item.get('actor2','')}\n"
            f"Sources distinctes dans GDELT: {item.get('sources',0)}\n"
            f"Mentions: {item.get('mentions',0)}\n"
            f"Articles: {item.get('articles',0)}\n"
            f"Contexte: {item.get('context','')}\n"
            f"URL: {item.get('link','')}"
        )

    for post in telegram[:4]:
        parts.append(
            "SIGNAL SOCIAL\n"
            f"Source: {post.get('source','')}\n"
            f"Texte: {post.get('text','')}\n"
            f"URL: {post.get('link','')}"
        )
    return "\n\n".join(parts)


# ============================================================
# ENVOI TELEGRAM
# ============================================================

def format_time(date_string):
    dt = parse_iso_date(date_string)
    if dt is None:
        return date_string

    return dt.astimezone(PARIS_TZ).strftime("%H:%M")


def semantic_duplicate_with_gemini(cluster, result):
    """Vérifie sémantiquement si le FLASH est le même événement qu'un FLASH récent."""
    history = load_json_list(EVENT_FILE)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=EVENT_MEMORY_HOURS)
    candidates = []
    for item in history:
        dt = parse_iso_date(item.get("detected_at") or item.get("date", ""))
        if dt and dt >= cutoff:
            candidates.append(item)
    if not candidates:
        return False

    candidates = candidates[-12:]
    previous = "\n".join(
        f"ID {i}: {x.get('title','')} — {x.get('summary','')} — {x.get('semantic_text','')}"
        for i, x in enumerate(candidates)
    )
    prompt = f"""
Compare le nouveau candidat avec les FLASH déjà envoyés ci-dessous.
Réponds uniquement DUPLICATE ou NOUVEAU.
DUPLICATE signifie : même événement réel, même phénomène concret ou même
information principale, même si les formulations, sources ou langues diffèrent.
NOUVEAU signifie : événement distinct ou développement réellement nouveau.
Ne considère pas comme différent le simple changement de formulation ou de média.

NOUVEAU CANDIDAT:
Titre: {result.get('title','')}
Résumé: {result.get('summary','')}
Signaux: {event_text(cluster['signals'][0])}

FLASH RÉCENTS:
{previous}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 20},
    }
    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-3.5-flash-lite:generateContent?key="
        + GEMINI_API_KEY
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
        answer = data["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
        return "DUPLICATE" in answer and "NOUVEAU" not in answer
    except Exception as exc:
        print("Vérification doublon Gemini impossible:", exc)
        return False


def send_flash(cluster):
    source = build_cluster_source(cluster)

    result = shorten_flash_with_gemini(source)

    if result is None:
        print("FLASH rejeté par le filtre éditorial Gemini | raison non détaillée")
        return False

    if semantic_duplicate_with_gemini(cluster, result):
        print("FLASH rejeté : doublon sémantique détecté dans les dernières 24h")
        return False

    title = result["title"]
    summary = result["summary"]

    primary = next(
        (
            x for x in cluster["signals"]
            if x["kind"] == "gdelt"
            and not x.get("agency_syndicated", False)
            and x.get("link")
            and (x.get("agency_direct") or not x.get("agency"))
        ),
        None
    )

    if primary is None:
        primary = next(
            (
                x for x in cluster["signals"]
                if x.get("link") and not x.get("agency_syndicated", False)
            ),
            None
        )

    link = (
        primary.get("link")
        if primary
        else ""
    )

    detected_at = datetime.now(timezone.utc)
    source_signal_date = min(
        [
            x.get("date", "")
            for x in cluster["signals"]
            if x.get("date")
        ]
        or [""]
    )

    source_labels = set()
    for x in cluster["signals"]:
        if x.get("agency"):
            # Une agence est une source canonique. Une reprise ne crée pas
            # une nouvelle source et n'est donc pas affichée séparément.
            if x.get("agency_direct"):
                source_labels.add(x.get("agency"))
        elif x.get("kind") == "telegram":
            if x.get("source"):
                source_labels.add(x.get("source"))
        elif x.get("kind") == "gdelt" and not x.get("agency_syndicated", False):
            link = str(x.get("link") or "")
            m = re.match(r"https?://([^/]+)", link.lower())
            if m:
                source_labels.add(m.group(1).split(":")[0].removeprefix("www."))

    sources = sorted(source_labels)

    text = (
        f"🔴 <b>FLASH — {title.upper()}</b>\n\n"
        f"{summary}\n\n"
        f"🕒 Détecté : {detected_at.astimezone(PARIS_TZ).strftime('%H:%M')}\n"
        f"📰 Signal source : {format_time(source_signal_date) if source_signal_date else 'inconnu'}\n"
        f"📡 {', '.join(sources)}\n"
    )

    if link and str(link).startswith(("http://", "https://")):
        safe_link = link.replace("&", "&amp;").replace('"', "&quot;")
        text += (
            f'🔗 <a href="{safe_link}">Ouvrir la source originale</a>'
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
    sent_at = datetime.now(timezone.utc)

    events = load_json_list(EVENT_FILE)

    events.append({
        "fingerprint": cluster["fingerprint"],
        "date": source_signal_date,
        "detected_at": detected_at.isoformat(),
        "sent_at": sent_at.isoformat(),
        "source_signal_at": source_signal_date,
        "semantic_text": event_text(cluster["signals"][0]) + " " + " ".join(
            event_text(x) for x in cluster["signals"][1:8]
        ),
        "title": title,
        "summary": summary,
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
    print("=== FLASH TREND RADAR V12 ===")
    print("X: capteur", "ACTIF" if X_BEARER_TOKEN else "INACTIF")
    print("Reddit: capteur ACTIF | subreddits:", ", ".join(REDDIT_SUBREDDITS[:10]))

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

    # Nouveau radar : découverte de tendances sociales sans liste de thèmes.
    social_signals = get_x_signals() + get_reddit_signals()
    if social_signals:
        trend_clusters = build_trend_clusters(social_signals)
        print("Tendances sociales candidates:", len(trend_clusters))
        trend_selected = select_trend_clusters(trend_clusters)
        print("TENDANCES retenues:", len(trend_selected))
        for trend in trend_selected:
            send_trend(trend)
    else:
        print("Aucun capteur social direct configuré ou aucun signal récent.")

    signals = gdelt_events + telegram_posts

    print(
        "Signaux événementiels récents:",
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
