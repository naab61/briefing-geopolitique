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

    for article in articles:
        sources.append(
            f"TITRE: {article['title']}\n"
            f"DATE: {article['date']}\n"
            f"DESCRIPTION: {article['description']}\n"
            f"SOURCE: {article['link']}\n"
        )

    prompt = """Tu es un analyste géopolitique francophone.

À partir UNIQUEMENT des informations fournies ci-dessous, rédige un briefing géopolitique quotidien clair et utile.

Structure obligatoire :

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

Pour chaque information importante, utilise obligatoirement une classification :
🟢 confirmé
🟡 plausible / non confirmé
🔵 analyse
🟣 récit d’un camp ou d’une communauté

Règles importantes :
- Ne transforme jamais une hypothèse en fait.
- Les publications sur les réseaux sociaux ne constituent jamais à elles seules une preuve.
- Sépare clairement faits, hypothèses, récits et analyse.
- Si les sources fournies ne permettent pas de parler d'un sujet, dis-le brièvement plutôt que d'inventer.
- Sois concis mais analytique.
- Français naturel.
- Maximum environ 3500 caractères.

Voici les sources disponibles :

""" + "\n".join(sources)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-2.5-flash-lite:generateContent"
    )

    payload = {
        "contents": [
            {
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

    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))

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
