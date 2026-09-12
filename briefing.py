import os
import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from html import unescape

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

RSS_URLS = [
    "https://www.france24.com/fr/rss",
    "https://www.rfi.fr/fr/rss",
    "https://www.lemonde.fr/international/rss_full.xml",
]

def get_articles():
    articles = []

    for rss_url in RSS_URLS:
        try:
            request = urllib.request.Request(
                rss_url,
                headers={"User-Agent": "Mozilla/5.0"}
            )

            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()

            root = ET.fromstring(data)

            for item in root.findall(".//item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                description = item.findtext("description", "").strip()

                if title:
                    articles.append({
                        "title": unescape(title),
                        "link": link,
                        "description": unescape(description),
                        "date": item.findtext("pubDate", "")
                    })

        except Exception as e:
            print(f"Flux ignoré: {rss_url} — {e}")

    return articles[:30]


def ask_gemini(articles):
    sources = []

    for article in articles[:15]:
        sources.append(
            f"TITRE: {article.get('title', '')}\n"
            f"DATE: {article.get('date', '')}\n"
            f"DESCRIPTION: {article.get('description', '')[:800]}\n"
            f"SOURCE: {article.get('link', '')}\n"
        )

    prompt = """Tu es un analyste géopolitique francophone.

À partir UNIQUEMENT des sources fournies, rédige un briefing quotidien.

Sections :
🌍 GRANDES TENDANCES
🟠 MOYEN-ORIENT
🔴 UKRAINE / RUSSIE
🇺🇸 ÉTATS-UNIS
🇨🇳 CHINE / INDO-PACIFIQUE
🇪🇺 EUROPE
🌍 AFRIQUE / SUD GLOBAL
📱 TENDANCES SOCIALES / DÉSINFORMATION
🇫🇷 CONSÉQUENCES POUR LA FRANCE ET L’EUROPE
🔭 SCÉNARIOS 24–72H
📅 SCÉNARIOS À 7 JOURS

Classification obligatoire :
🟢 confirmé
🟡 plausible / non confirmé
🔵 analyse
🟣 récit d’un camp ou d’une communauté

Ne présente jamais une hypothèse comme un fait.
Les réseaux sociaux ne sont jamais une preuve à eux seuls.
N'invente aucune information absente des sources.
Sois concis et analytique.
Réponds uniquement en français.
Maximum 3000 caractères.

SOURCES :

""" + "\n".join(sources)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-2.5-flash-lite:generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print("ERREUR GEMINI :", error_body)
        raise

    return result["candidates"][0]["content"]["parts"][0]["text"]

def send_telegram(text):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method="POST"
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def main():
    articles = get_articles()

    if not articles:
        raise RuntimeError("Aucun article RSS trouvé.")

    briefing = ask_gemini(articles)

    send_telegram(briefing)

    print("Briefing envoyé sur Telegram.")


if __name__ == "__main__":
    main()
