"""
app.py
======
Haupteinstiegspunkt der GPS-Tracking-App.

Dies ist die einzige Datei, mit der die App regulär gestartet wird:

    streamlit run app.py

Sie übernimmt drei Aufgaben:
    1. Grundkonfiguration der Seite (Titel, Icon, breites Layout) - muss
       als allererster Streamlit-Befehl im gesamten Programm ausgeführt
       werden, daher steht sie hier statt in map.py/admin.py.
    2. Titel und Beschreibung in der Seitenleiste, sichtbar auf jeder Seite.
    3. Seitenleisten-Navigation, über die zwischen "Karte" (map.py) und
       "Verwaltung" (admin.py) gewechselt werden kann.

admin.py und map.py enthalten dazu jeweils eine render_*_page()-Funktion
mit dem kompletten Seiteninhalt; app.py registriert diese Funktionen nur
noch als Streamlit-"Pages" und ruft die ausgewählte Seite auf. Die
eigentliche fachliche Logik (Datenbank, GPX-Verarbeitung, Geocoding) liegt
gebündelt in functions.py.
"""

import streamlit as st

from admin import render_admin_page
from map import render_map_page

# Muss als allererster Streamlit-Befehl der gesamten App stehen.
st.set_page_config(
    page_title="GPS Tracking",
    page_icon="🗺️",
    layout="wide",
)

# Titel + kurze Erklärung oben in der Seitenleiste - wird unabhängig von der
# gewählten Seite (Karte/Verwaltung) immer angezeigt.
with st.sidebar:
    st.title("🗺️ GPS Tracking")
    st.caption("Aufgezeichnete Touren ansehen und verwalten")
    st.divider()

# st.navigation erzeugt automatisch ein Auswahlmenü in der Seitenleiste und
# führt beim Wechseln der Seite die jeweils hinterlegte render_*_page()-
# Funktion aus. default=True legt fest, welche Seite beim ersten Aufruf der
# App gezeigt wird.
pages = [
    st.Page(
        render_map_page,
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

navigation = st.navigation(pages, position="sidebar")
navigation.run()
