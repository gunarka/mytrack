"""
app.py
======
Haupteinstiegspunkt der MyTrack-App.

Dies ist die einzige Datei, mit der die App regulär gestartet wird:

    streamlit run app.py

Sie übernimmt drei Aufgaben:
    1. Grundkonfiguration der Seite (Titel, Icon, breites Layout) - muss
       als allererster Streamlit-Befehl im gesamten Programm ausgeführt
       werden, daher steht sie hier statt in map.py/admin.py.
    2. Titel und Beschreibung in der Seitenleiste, sichtbar auf jeder Seite.
    3. Seitenleisten-Navigation, über die zwischen "Karte" (map.py) und
       "Verwaltung" (admin.py) gewechselt werden kann - zusammen mit den
       Anzeigeeinstellungen der Kartenseite (Farbauswahl, Spaltenbreite,
       Höhe von Karte/Profil) in einem gemeinsamen, einklappbaren Bereich
       der Seitenleiste (siehe 'settings_expander' weiter unten).

admin.py und map.py enthalten dazu jeweils eine render_*_page()-Funktion
mit dem kompletten Seiteninhalt; app.py registriert diese Funktionen nur
noch als Streamlit-"Pages" und ruft die ausgewählte Seite auf. Die
eigentliche fachliche Logik (Datenbank, GPX-Verarbeitung, Geocoding) liegt
gebündelt in functions.py.
"""

import functools

import streamlit as st

from admin import render_admin_page
from map import render_map_page

# Muss als allererster Streamlit-Befehl der gesamten App stehen.
st.set_page_config(
    page_title="MyTrack",
    page_icon="🗺️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Globales CSS-Styling - gilt auf beiden Seiten (Karte/Verwaltung), da dieser
# Block vor der Navigation ausgeführt wird:
#
#   .stAppHeader          — obere App-Leiste komplett entfernen (kein leerer
#                           Streifen am oberen Rand des Hauptinhalts).
#   .stMainBlockContainer — Innenabstand des Hauptinhaltsbereichs reduzieren.
#   .stVerticalBlock      — Lücke zwischen übereinanderliegenden Elementen
#                           auf 0 setzen.
#
#   Seitenleiste — kein Einklapp-Button, kein Leerraum oben:
#   [stSidebarHeader]       enthält ausschliesslich den Einklapp-Button und
#                           einen leeren Logo-Platzhalter. Wird komplett
#                           ausgeblendet → der Nutzerinhalt beginnt direkt
#                           am oberen Rand der Seitenleiste (y=0).
#   [stSidebarCollapseButton] explizit versteckt (Streamlit ≥ 1.38 benennt
#                           das Element so; ältere Versionen nutzten
#                           [collapsedControl] - beide Regeln koexistieren
#                           harmlos).
#   [collapsedControl]      Hamburger-Icon im Hauptinhalt (erscheint wenn
#                           die Seitenleiste eingeklappt wäre) ebenfalls
#                           versteckt - da die Seitenleiste permanent offen
#                           bleibt, wird dieser Button nie gebraucht.
#
#   [stSidebar]             Breite auf 360 px erhöht (Standard: 300 px),
#                           damit Filter und Einstellungen mehr Platz haben.
st.markdown(
    """
    <style>
    .stAppHeader {
        display: none;
    }
    .stMainBlockContainer {
        padding-top: 1rem;
        padding-bottom: 1rem;
        padding-left: 1rem;
        padding-right: 1rem;
    }
    .stVerticalBlock {
        gap: 0px !important;
    }

    [data-testid="stSidebarHeader"] {
        display: none;
    }
    [data-testid="stSidebarCollapseButton"] {
        display: none;
    }
    [data-testid="collapsedControl"] {
        display: none;
    }

    [data-testid="stSidebar"] {
        min-width: 360px;
        max-width: 360px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# Titel + kurze Erklärung oben in der Seitenleiste - wird unabhängig von der
# gewählten Seite (Karte/Verwaltung) immer angezeigt.
with st.sidebar:
    st.title("🗺️ MyTrack")
    #st.caption("Aufgezeichnete Touren ansehen und verwalten")
    st.divider()

    # Gemeinsamer, einklappbarer Seitenleisten-Bereich für:
    #   - die Seiten-Navigation (Karte/Verwaltung, siehe weiter unten -
    #     st.navigation() selbst wird dafür mit position="hidden" *nicht*
    #     dargestellt, stattdessen bauen wir die Menüpunkte manuell per
    #     st.page_link() hier hinein),
    #   - sowie (nur auf der Kartenseite) die Anzeigeeinstellungen Farbe,
    #     Spaltenbreite und Höhe von Karte/Profil (werden von
    #     render_map_page() über das Argument 'settings_container'
    #     hineingerendert, siehe map.py).
    # Als EIN Container-Objekt angelegt (statt zweimal mit demselben Label
    # aufgerufen), damit alle genannten Elemente in genau demselben
    # auf-/zuklappbaren Bereich landen, auch wenn sie aus unterschiedlichen
    # Modulen/Funktionen heraus befüllt werden.
    settings_expander = st.expander("⚙️ Einstellungen", expanded=True)

# st.navigation deklariert die verfügbaren Seiten und übernimmt das Routing
# (welche render_*_page()-Funktion beim Seitenwechsel läuft); mit
# position="hidden" wird dabei aber NICHT automatisch ein Menü in der
# Seitenleiste gezeichnet - das übernehmen wir stattdessen selbst weiter
# unten per st.page_link(), damit es im selben einklappbaren Bereich wie
# die Anzeigeeinstellungen landet (siehe 'settings_expander' oben).
# functools.partial reicht 'settings_expander' an render_map_page() durch,
# ohne dass st.navigation()/st.Page() etwas davon wissen müssen - beide
# erwarten weiterhin nur eine ohne Argumente aufrufbare Funktion.
# default=True legt fest, welche Seite beim ersten Aufruf der App gezeigt
# wird.
pages = [
    st.Page(
        functools.partial(render_map_page, settings_container=settings_expander),
        title="Karte",
        icon="🗺️",
        url_path="karte",
        default=True,
    ),
    st.Page(
        render_admin_page,
        title="Verwaltung",
        icon="⚙️",
        url_path="verwaltung",
    ),
]

navigation = st.navigation(pages, position="hidden")

with settings_expander:
    st.caption("Navigation")
    for page in pages:
        st.page_link(page)
    st.divider()

navigation.run()
