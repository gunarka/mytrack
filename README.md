# GPS Tracking App

Eine Streamlit-App zum Hochladen, Verwalten und interaktiven Visualisieren
von GPX-Tracks (Wandern, Laufen, Radfahren, ...). Tracks lassen sich zu
**Touren** und **Sportarten** zusammenfassen, auf einer Karte mit
Höhenprofil ansehen und in einer Verwaltungsoberfläche pflegen.

## Architektur

Die App ist in vier Python-Dateien aufgeteilt:

| Datei            | Zweck                                                                 |
|------------------|------------------------------------------------------------------------|
| `app.py`         | **Einstiegspunkt** (`streamlit run app.py`). Seitenkonfiguration, Titel/Beschreibung in der Seitenleiste, Navigation zwischen den Seiten. |
| `functions.py`   | **Gemeinsame Logik.** Datenbankverbindung & -Schema, GPX-Verarbeitung mit GeoPandas, Reverse-Geocoding, Zeitzonen-Ermittlung sowie alle CRUD-Funktionen (Create/Read/Update/Delete) für Tracks, Touren und Sportarten. Enthält keinerlei Oberflächen-Code. |
| `admin.py`       | **Verwaltungsoberfläche** (Seite "Verwaltung"). Drei Tabs (Tracks, Touren, Sportarten), jeweils mit Formular zum Neuanlegen, Formular zum Bearbeiten/Löschen und einer Übersichtstabelle. |
| `map.py`         | **Kartenansicht** (Seite "Karte"). Pills-Filter nach Sport/Jahr/Jahreszeit, darunter eine aufklappbare Jahr -> Monat -> Tour -> Track-Auswahl, Folium-Karte mit eingefärbten Tracks, gemeinsames Höhenprofil (Plotly) mit Klick-Interaktion. |
| `init.py`        | **Eigenständiges Werkzeug** zum (Neu-)Anlegen der Datenbankstruktur. Löscht beim Klick auf den Button alle vorhandenen Daten – bewusst getrennt von `app.py`, damit das nicht versehentlich im normalen Betrieb passiert. |

`admin.py` und `map.py` stellen jeweils eine Funktion `render_admin_page()`
bzw. `render_map_page()` bereit. `app.py` registriert diese über
[`st.navigation`](https://docs.streamlit.io/develop/api-reference/navigation/st.navigation)
als Seiten und kümmert sich um die gemeinsame Seitenleiste. Beide Dateien
lassen sich zum Debuggen weiterhin auch einzeln starten
(`streamlit run admin.py` / `streamlit run map.py`).

Die Datenbankverbindung (`functions.get_connection()`) ist über
`st.cache_resource` als Singleton implementiert: Alle Module im selben
Streamlit-Prozess teilen sich dieselbe DuckDB-Verbindung.

## Datenmodell

Lokale [DuckDB](https://duckdb.org/)-Datei unter `.data/tracks.duckdb` mit
drei Tabellen:

- **`sport`** – Sportarten (`sport_id`, `sport_title`)
- **`tours`** – Touren (`tour_id`, `tour_title`)
- **`gpx`** – Tracks: Titel, Zuordnung zu Sport/Tour, Start-/Endadresse
  (per Reverse-Geocoding ermittelt), Zeitzone, Kennzahlen (Distanz, Dauer,
  Auf-/Abstieg, Min/Max von Höhe/Tempo/Steigung) sowie die rohe GPX-Datei
  als Blob.

Die Zuordnung eines Tracks zu Sport bzw. Tour ist **optional**: Wird eine
Sportart oder Tour gelöscht, bleiben die zugehörigen Tracks erhalten und
verlieren lediglich die Zuordnung (`sport_id`/`tour_id` wird `NULL`).

## Installation & Start

```bash
pip install -r requirements.txt

# Einmalig: Datenbankstruktur anlegen
streamlit run init.py
# -> im Browser auf "Datenbank initialisieren" klicken

# App starten
streamlit run app.py
```

Für das Reverse-Geocoding (Ermittlung von Ort/Land aus den GPS-Koordinaten)
wird beim Hochladen eines neuen Tracks die öffentliche
[Nominatim](https://nominatim.org/)-API von OpenStreetMap angefragt – dafür
ist eine Internetverbindung nötig. Bitte die
[Nutzungsbedingungen](https://operations.osmfoundation.org/policies/nominatim/)
von Nominatim beachten (u. a. Rate-Limit von 1 Anfrage/Sekunde); bei sehr
vielen Uploads hintereinander kann es deshalb etwas dauern.

## Nutzung

**Verwaltung** (Seite "Verwaltung"):

1. Zuerst optional Sportarten und/oder Touren anlegen (Tabs "Sportarten" /
   "Touren").
2. Im Tab "Tracks" eine GPX-Datei hochladen, optional Titel/Sport/Tour
   vergeben und speichern – Distanz, Dauer, Höhenprofil-Kennzahlen sowie
   Start-/Endort werden automatisch berechnet.
3. Bestehende Tracks, Touren und Sportarten lassen sich im jeweiligen
   "Bearbeiten"-Bereich umbenennen bzw. löschen. Alle vorhandenen Einträge
   werden zusätzlich in einer Übersichtstabelle angezeigt.

**Karte** (Seite "Karte"):

In der Seitenleiste zunächst optional über die Pills nach Sport, Jahr
und/oder Jahreszeit filtern (kaskadierend: jede Stufe zeigt nur noch die
zur vorherigen Auswahl passenden Optionen). Darunter die eigentliche
Track-Auswahl als aufklappbare Liste nach Jahr, darin gruppiert nach Monat
und Tour. Ein Klick auf die Checkbox einer Tour wählt alle ihre Tracks
innerhalb dieser Jahr/Monat-Gruppe auf einmal aus; einzelne Tracks lassen
sich daneben auch gezielt einzeln (ab-)wählen. Es muss mindestens ein
Track ausgewählt sein. Zusätzlich kann oben eine Farb-Spalte für das
Höhenprofil gewählt werden (Höhe, Geschwindigkeit, Gefälle oder einfarbig).
Ein Klick auf einen Punkt im Höhenprofil zentriert die Karte auf den
entsprechenden Ort.

## Hinweise / Einschränkungen

- DuckDB erlaubt pro Datenbankdatei nur **eine** schreibende Verbindung
  gleichzeitig. `app.py` und `init.py` sollten daher nicht parallel als
  separate Prozesse laufen.
- Die GPX-Verarbeitung (Distanz/Tempo/Steigung) wird pro Track gecacht
  (`@st.cache_data`), damit Klicks im Höhenprofil keine erneute,
  aufwändige GeoPandas-Berechnung auslösen.
- Für den Datei-Upload werden ausschließlich `.gpx`-Dateien mit einem
  `<trk>`-Track akzeptiert (Format wie von gängigen GPS-Geräten/Apps
  exportiert).

## Änderungen in diesem Refactoring

Gegenüber der ursprünglichen Version wurden folgende Punkte überarbeitet:

- Gemeinsame Logik (Datenbank, GPX-Verarbeitung, Geocoding, CRUD) wurde aus
  `admin.py` und `map.py` herausgelöst und in `functions.py` gebündelt –
  dadurch gibt es z. B. die GPX-Aufbereitung nur noch einmal statt
  zweimal mit leicht unterschiedlichem Code.
- `admin.py` unterstützt jetzt zusätzlich zum Anlegen auch das
  **Bearbeiten und Löschen** bestehender Tracks, Touren und Sportarten
  (vorher nur Anlegen möglich; die alte editierbare Tabellenansicht hat
  Änderungen nicht gespeichert).
- Neue `app.py` als zentraler Einstiegspunkt mit Titel, Seitenleiste und
  Navigation zwischen Karte und Verwaltung.
- Die Datenbankverbindung wird app-weit als Singleton (`st.cache_resource`)
  verwaltet statt in `admin.py` bei jedem Durchlauf neu geöffnet und am
  Ende geschlossen zu werden.
- Sport-/Tour-Zuordnung eines Tracks ist jetzt optional.
- Durchgängige Kommentierung des Codes auf Deutsch.
- Kartenseite: Die bisherige flache Sport/Tour/Track-Filterung wurde durch
  Pills für Sport/Jahr/Jahreszeit plus eine aufklappbare Jahr -> Monat ->
  Tour -> Track-Auswahl ersetzt; eine Tour-Checkbox wählt dabei alle
  Tracks dieser Tour auf einmal aus.
