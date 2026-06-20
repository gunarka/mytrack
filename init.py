"""
init.py
=======
Eigenständiges Werkzeug zum (Neu-)Anlegen der Datenbankstruktur.

ACHTUNG: Der Button auf dieser Seite löscht alle bestehenden Tabellen
('gpx', 'tours', 'sport') samt Inhalt und legt sie leer neu an
(siehe functions.init_database()). Das ist bewusst NICHT Teil der
normalen App-Navigation (app.py), damit ein Datenverlust im laufenden
Betrieb nicht versehentlich per Klick ausgelöst werden kann.

Aufruf separat über: streamlit run init.py
"""

import streamlit as st

from functions import init_database

st.title("Datenbank initialisieren")
st.warning(
    "Dieser Vorgang löscht ALLE vorhandenen Tracks, Touren und Sportarten "
    "unwiderruflich und legt die Tabellen leer neu an."
)

if st.button("Datenbank initialisieren"):
    init_database()
    st.success("Tabellen 'gpx', 'tours' und 'sport' wurden neu angelegt.")
