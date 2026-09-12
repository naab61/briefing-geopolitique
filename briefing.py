import os
import json
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from html import unescape

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Sources classées par fonction.
# Les médias généralistes servent surtout de radar.
# Les sources d'enquête/analyse ont davantage de poids pour l'analyse.
SOURCES = [
    {
        "name": "OCCRP — investigations",
        "type": "ENQUÊTE / OSINT",
        "url": "https://www.occrp.org/en/investigations/feed",
    },
    {
        "name": "International Crisis Group — global",
        "type": "ANALYSE / CONFLITS",
        "url": "https://www.crisisgroup.org/rss",
    },
    {
        "name": "International Crisis Group — CrisisWatch",
        "type": "ALERTE / CONFLITS",
        "url": "https://www.crisisgroup.org/rss/crisiswatch",
    },
    {
        "name": "France 24",
        "type": "MÉDIA GÉNÉRALISTE — RADAR",
        "url": "https://www.france24.com/fr/rss",
    },
    {
        "name": "RFI",
        "type": "MÉDIA GÉNÉRALISTE — RADAR",
        "url": "https://www.rfi.fr/fr/rss",
    },
    {
        "name": "Le Monde — international",
        "type": "MÉDIA GÉNÉRALISTE — RADAR",
        "url": "https://www.lemonde.fr/international/rss_full.xml",
    },
]


def get_articles():
    articles = []

    for source in SOURCES:
        try:
            request = urllib.request.Request(
                source["url"],
                headers={
                    "User-Agent": "Mozilla/5.0 "
                    "(compatible; BriefingGeopolitique/2.0)"
                }
            )

            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()

            root = ET.fromstring(data)

            # RSS classique
            items = root.findall(".//item")

            # Support minimal des flux Atom
            if not items:
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                items = root.findall(".//atom:entry", ns)

            for item in items:
                title = ""
                link = ""
                description = ""
                date = ""

                # RSS
                title_node = item.find("title")
                if title_node is not None and title_node.text:
                    title = title_node.text.strip()

                link_node = item.find("link")
                if link_node is not None and link_node.text:
                    link = link_node.text.strip()

                description_node = item.find("description")
                if description_node is not None and description_node.text:
                    description = description_node.text.strip()

                date_node = item.find("pubDate")
                if date_node is not None and date_node.text:
                    date = date_node.text.strip()

                # Atom
                if not title:
                    ns = {"atom": "http://www.w3.org/2005/Atom"}

                    title_node = item.find("atom:title", ns)
                    if title_node is not None and title_node.text:
                        title = title_node.text.strip()

                    link_node = item.find("atom:link", ns)
                    if link_node is not None:
                        link = link_node.attrib.get("href", "").strip()

                    summary_node = item.find("atom:summary", ns)
                    if summary_node is not None and summary_node.text:
                        description = summary_node.text.strip()

                    date_node = item.find("atom:updated", ns)
                    if date_node is not None and date_node.text:
                        date = date_node.text.strip()

                if title:
                    articles.append({
                        "title": unescape(title),
                        "link": link,
                        "description": unescape(description),
                        "date": date,
                        "source": source["name"],
                        "type": source["type"],
                    })

            print(
                f"Source OK: {source['name']} "
                f"({len(items)} éléments)"
            )

        except Exception as e:
            print(
                f"Source ignorée: {source['name']} "
                f"— {e}"
            )

    return articles


def clean_text(text, max_length=900):
    text = text.replace("<![CDATA[", "")
    text = text.replace("]]>", "")
    text = " ".join(text.split())
    return text[:max_length]


def ask_gemini(articles):
    # On limite le volume envoyé à Gemini pour rester léger.
    selected = articles[:40]

    sources = []

    for i, article in enumerate(selected, start=1):
        sources.append(
            f"""
SOURCE {i}
Type : {article.get('type', '')}
Nom : {article.get('source', '')}
Titre : {article.get('title', '')}
Date : {article.get('date', '')}
Description : {clean_text(article.get('description', ''))}
Lien : {article.get('link', '')}
""".strip()
        )

    prompt = """
Tu es un analyste géopolitique francophone spécialisé en OSINT,
renseignement en sources ouvertes, conflits, influence informationnelle
et analyse stratégique.

OBJECTIF

Produire un briefing quotidien réellement utile à quelqu'un qui suit
la géopolitique sérieusement.

Tu dois faire le travail de tri et de confrontation des informations.
Ne te contente surtout pas de résumer les titres.

HIÉRARCHIE DES SOURCES

1. ENQUÊTES / OSINT / DOCUMENTS
2. SOURCES PRIMAIRES ou données directement observables
3. SOURCES LOCALES
4. ORGANISMES spécialisés dans les conflits
5. ANALYSES spécialisées
6. MÉDIAS GÉNÉRALISTES

Les médias généralistes présents ici sont principalement des radars.
Ils peuvent signaler un événement important, mais ne doivent pas
automatiquement devenir la preuve de cet événement.

RÈGLES DE VÉRIFICATION

- Une seule source = ne pas présenter comme confirmé si l'information
  est contestable.
- Deux articles qui reprennent la même dépêche ou la même déclaration
  ne constituent PAS deux confirmations indépendantes.
- Distingue toujours un fait observé, une déclaration, une affirmation
  d'un camp, une hypothèse et ton analyse.
- Si les sources se contredisent, conserve la contradiction.
- Si les éléments sont insuffisants, écris explicitement :
  "preuves insuffisantes".
- Ne complète jamais les trous avec ton imagination.
- N'invente aucun chiffre, lieu, acteur, date ou événement.
- Les réseaux sociaux ne sont jamais une preuve suffisante à eux seuls.
- Une déclaration officielle n'est pas automatiquement un fait établi.
  Indique qui affirme quoi.
- Ne transforme pas une analyse de Crisis Group ou d'un autre expert
  en fait.
- Cherche les signaux faibles : changement de vocabulaire officiel,
  mouvement diplomatique, sanctions, nominations, déploiements,
  ruptures commerciales, évolution narrative, activité informationnelle,
  tensions régionales, changements de posture.
- Porte une attention permanente au Maroc et au Maghreb.
- Porte une attention particulière aux évolutions inhabituelles
  concernant Donald Trump et à la manipulation médiatique/informationnelle.
- Cherche les conséquences possibles pour la France et l'Europe.

CLASSIFICATION OBLIGATOIRE

🟢 CONFIRMÉ
Fait solidement étayé par les sources disponibles.

🟡 PLAUSIBLE / NON CONFIRMÉ
Élément crédible mais insuffisamment établi.

🔵 ANALYSE
Interprétation ou déduction analytique clairement présentée comme telle.

🟣 RÉCIT / NARRATIVE
Ce qu'affirme ou diffuse un gouvernement, un mouvement,
un camp politique, une communauté ou un réseau informationnel.
Ne pas présenter ce récit comme un fait.

FORMAT

🌍 GRANDES TENDANCES

🟠 MOYEN-ORIENT

🔴 UKRAINE / RUSSIE

🇺🇸 ÉTATS-UNIS

🇨🇳 CHINE / INDO-PACIFIQUE

🇪🇺 EUROPE

🌍 AFRIQUE / SUD GLOBAL

🇲🇦 MAROC / MAGHREB

📱 TENDANCES SOCIALES / DÉSINFORMATION

🇫🇷 CONSÉQUENCES POUR LA FRANCE ET L’EUROPE

🔭 SCÉNARIOS 24–72H

📅 SCÉNARIOS À 7 JOURS

À la fin, ajoute :

🔎 SOURCES CLÉS
- maximum 5 liens réellement importants
- privilégie les enquêtes, documents, sources spécialisées ou
  éléments permettant de vérifier l'information

CONTRAINTES

- Français uniquement.
- Style direct, dense et analytique.
- Pas de remplissage.
- Pas de répétition.
- Maximum 3800 caractères.
- Les liens doivent rester cliquables.
- Ne cite que les liens réellement fournis dans les sources.
- Si aucune information sérieuse n'existe sur une section, écris :
  "Pas de signal solide dans les sources disponibles."
- Ne force jamais une actualité dans une catégorie simplement
  pour remplir la section.

SOURCES DISPONIBLES :

""" + "\n\n".join(sources)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-3.5-flash-lite:generateContent"
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
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as e:
        error_body = e.read().decode(
            "utf-8",
            errors="replace"
        )
        print("ERREUR GEMINI :", error_body)
        raise

    try:
        return (
            result["candidates"][0]
            ["content"]["parts"][0]["text"]
        )
    except (KeyError, IndexError, TypeError):
        print("Réponse Gemini inattendue :", result)
        raise RuntimeError(
            "Réponse Gemini inutilisable."
        )


def send_telegram(text):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "false",
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
        return response.read().decode("utf-8")


def main():
    articles = get_articles()

    if not articles:
        raise RuntimeError(
            "Aucune source exploitable trouvée."
        )

    print(
        f"{len(articles)} éléments récupérés "
        f"depuis les sources."
    )

    briefing = ask_gemini(articles)

    send_telegram(briefing)

    print("Briefing V2 envoyé sur Telegram.")


if __name__ == "__main__":
    main()
