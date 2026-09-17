# GPS Tracking App

Eine Streamlit-App zum Hochladen, Verwalten und interaktiven Visualisieren
von GPX-Tracks (Wandern, Laufen, Radfahren, ...). Tracks lassen sich zu
**Touren** und **Sportarten** zusammenfassen, auf einer Karte mit
Höhenprofil ansehen und in einer Verwaltungsoberfläche pflegen.

Entwicklungssicht (Architekturregeln, Konventionen, Fallstricke):
[CONTEXT.md](CONTEXT.md).

## Architektur

Die App ist in sechs Python-Dateien aufgeteilt:

| Datei            | Zweck                                                                 |
|------------------|------------------------------------------------------------------------|
| `app.py`         | **Einstiegspunkt** (`streamlit run app.py`). Seitenkonfiguration, Titel/Beschreibung in der Seitenleiste, Navigation zwischen den Seiten. |
| `functions.py`   | **Gemeinsame Logik.** Datenbankverbindung & -Schema, GPX-Verarbeitung mit GeoPandas, Reverse-Geocoding, Zeitzonen-Ermittlung, Bestzeiten-Auswertung, GPX-Export, alle CRUD-Funktionen (Create/Read/Update/Delete) für Tracks, Touren und Sportarten sowie die gecachten Leseabfragen von Karte und Statistik (`load_metadata`, `load_track_files`, `load_heatmap_points`). Enthält keinerlei Oberflächen-Code. |
| `stats.py`       | **Statistik-Seite** (Seite "Statistik"). Gesamtwerte, Kilometer je Jahr/Monat, Auswertung je Sportart sowie eine Heatmap aller aufgezeichneten Punkte. Rechnet fast ausschließlich mit den gespeicherten Kennzahlen, ohne die GPX-Dateien erneut zu verarbeiten. |
| `admin.py`       | **Verwaltungsoberfläche** (Seite "Verwaltung"). Drei Tabs (Tracks, Touren, Sportarten), jeweils mit Formular zum Neuanlegen, Formular zum Bearbeiten/Löschen und einer Übersichtstabelle. |
| `map.py`         | **Kartenansicht** (Seite "Karte"). Pills-Filter nach Sport/Land/Jahr/Jahreszeit, darunter eine aufklappbare Jahr -> Monat -> Tour -> Track-Auswahl, Folium-Karte mit eingefärbten Tracks, gemeinsames Höhenprofil (Plotly) mit Klick-Interaktion sowie der Planungsmodus. |
| `init.py`        | **Eigenständiges Werkzeug** zum (Neu-)Anlegen der Datenbankstruktur. Löscht beim Klick auf den Button alle vorhandenen Daten – bewusst getrennt von `app.py`, damit das nicht versehentlich im normalen Betrieb passiert. |

`map.py`, `stats.py` und `admin.py` stellen jeweils eine Funktion
`render_map_page()`, `render_stats_page()` bzw. `render_admin_page()`
bereit. `app.py` registriert diese über
[`st.navigation`](https://docs.streamlit.io/develop/api-reference/navigation/st.navigation)
als Seiten und kümmert sich um die gemeinsame Seitenleiste. Diese Dateien
lassen sich zum Debuggen weiterhin auch einzeln starten
(`streamlit run admin.py` / `streamlit run map.py` /
`streamlit run stats.py`).

Die Datenbankverbindung (`functions.get_connection()`) ist über
`st.cache_resource` als Singleton implementiert: Alle Module im selben
Streamlit-Prozess teilen sich dieselbe DuckDB-Verbindung.

Die Leseabfragen der Kartenseite sind über `st.cache_data` gecacht. Jede
schreibende Funktion (anlegen / ändern / löschen / neu berechnen) leert
diesen Cache gezielt (`_invalidate_track_caches()`), sodass Änderungen aus
der Verwaltung sofort auf der Karte sichtbar sind.

## Datenmodell

Lokale [DuckDB](https://duckdb.org/)-Datei unter `.data/tracks.duckdb` mit
drei Tabellen:

- **`sport`** – Sportarten (`sport_id`, `sport_title`)
- **`tours`** – Touren (`tour_id`, `tour_title`)
- **`gpx`** – Tracks: Titel, Zuordnung zu Sport/Tour, Start-/Endadresse
  (per Reverse-Geocoding ermittelt), Zeitzone, Kennzahlen (Distanz, Dauer,
  Zeit in Bewegung, Auf-/Abstieg, Min/Max von Höhe/Tempo/Steigung) sowie
  die rohe GPX-Datei als Blob.

  "Zeit in Bewegung" sowie Auf-/Abstieg werden über Schwellwerte
  berechnet: Punkte unterhalb einer minimalen Geschwindigkeit zählen
  nicht als Bewegung, Höhenschwankungen unterhalb einer minimalen
  Höhenänderung nicht als Auf-/Abstieg (filtert GPS-/Barometer-Rauschen).
  Diese Schwellwerte werden beim Hochladen automatisch mit
  Standardwerten angewendet und lassen sich in der Verwaltung
  nachträglich für einzelne oder alle Tracks neu berechnen (siehe
  unten) - optional skaliert mit der je Punkt gespeicherten
  GPS-Genauigkeit (vdop/hdop), falls die GPX-Datei diese enthält.

Die Zuordnung eines Tracks zu Sport bzw. Tour ist **optional**: Wird eine
Sportart oder Tour gelöscht, bleiben die zugehörigen Tracks erhalten und
verlieren lediglich die Zuordnung (`sport_id`/`tour_id` wird `NULL`).

## Installation & Start

Benötigt Python 3.10+ und Streamlit 1.49 oder neuer (für `width="stretch"`
sowie `height=` bei `st.plotly_chart`).

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

Ist der Dienst nicht erreichbar, schlägt der Upload **nicht** fehl: Der
Track wird ohne Ortsangaben gespeichert (der Land-Filter auf der Karte
zeigt ihn dann nicht an). Um die Adressen nachzutragen, den Track erneut
hochladen.

## Nutzung

**Verwaltung** (Seite "Verwaltung"):

1. Zuerst optional Sportarten und/oder Touren anlegen (Tabs "Sportarten" /
   "Touren").
2. Im Tab "Tracks" eine oder **mehrere** GPX-Dateien auf einmal
   hochladen, optional Titel/Sport/Tour vergeben und speichern – Distanz, Dauer, Zeit in Bewegung,
   Höhenprofil-Kennzahlen sowie Start-/Endort werden automatisch
   berechnet.
   Ein gemeinsamer Titel wird nur bei einer einzelnen Datei verwendet;
   bei mehreren dient jeweils der Dateiname als Titel. Scheitert eine
   Datei, werden die übrigen trotzdem gespeichert.
3. Bestehende Tracks, Touren und Sportarten lassen sich im jeweiligen
   "Bearbeiten"-Bereich umbenennen bzw. löschen. Dort steht auch
   **"⬇️ GPX herunterladen"** bereit, das die ursprünglich hochgeladene
   Datei wieder ausgibt. Alle vorhandenen Einträge werden zusätzlich in
   einer Übersichtstabelle angezeigt; dort lässt sich die Spalte "Track"
   **direkt bearbeiten**, um mehrere Titel auf einmal umzubenennen
   (Speichern per Knopf darunter).
4. Im Bereich "Track-Metadaten neu berechnen" (Tab "Tracks") lassen sich
   "Zeit in Bewegung" sowie Auf-/Abstieg eines einzelnen oder aller
   Tracks anhand neu eingegebener Schwellwerte (minimale Geschwindigkeit
   bzw. minimale Höhenänderung) aus den gespeicherten GPX-Rohdaten neu
   berechnen - z.B. wenn die beim Hochladen verwendeten Standardwerte für
   einen bestimmten Tracktyp nicht passen. Optional wird der
   Höhenänderungs-Schwellwert dabei mit der je Punkt gespeicherten
   GPS-Genauigkeit skaliert, sofern diese in der GPX-Datei vorhanden ist.
   Alternativ lässt sich dort eine **Glättung der Höhe** (gleitender
   Mittelwert über n Trackpunkte) einstellen – siehe unten.
5. Im Tab "Touren" gibt es je Tour **"⬇️ Tour als GPX exportieren"**:
   alle Tracks der Tour in einer Datei, zeitlich sortiert und je Etappe
   als eigenes Segment (`<trkseg>`), damit Pausen zwischen den Etappen
   nicht als Luftlinie interpretiert werden.

**Karte** (Seite "Karte"):

In der Seitenleiste zunächst optional über die Pills nach Sport, Land,
Jahr und/oder Jahreszeit filtern (kaskadierend: jede Stufe zeigt nur noch
die zur vorherigen Auswahl passenden Optionen). Der Land-Filter basiert
auf dem per Reverse-Geocoding ermittelten Start- und Endland eines Tracks;
Tracks mit Grenzübertritt (Start- und Endland unterschiedlich) erscheinen
unter beiden Ländern. Darunter die eigentliche Track-Auswahl als
aufklappbare Liste nach Jahr, darin gruppiert nach Monat und Tour. Ein
Klick auf die Checkbox einer Tour wählt alle ihre Tracks innerhalb dieser
Jahr/Monat-Gruppe auf einmal aus; einzelne Tracks lassen sich daneben auch
gezielt einzeln (ab-)wählen. Es muss mindestens ein Track ausgewählt sein.
Zusätzlich kann oben eine Farb-Spalte für das Höhenprofil gewählt werden
(Höhe, Geschwindigkeit, Gefälle oder einfarbig).

Links neben der Karte zeigt ein Kennzahlen-Bereich Länge, Zeit, Zeit in
Bewegung, Pause, Ø-Tempo, Ø-Tempo in Bewegung, Auf-/Abstieg sowie
Min-/Max-Höhe als Summe über alle aktuell ausgewählten Tracks an; klein
daneben jeweils die Werte je einzelnem Track. Die Durchschnittstempi
werden aus Gesamtstrecke / Gesamtzeit gebildet (nicht als Mittelwert der
Einzeldurchschnitte), damit lange Tracks nicht gleich stark gewichtet
werden wie kurze.

Ist genau **ein** Track ausgewählt, erscheint darunter der Bereich
**"🏅 Bestzeiten"**: je Standarddistanz (1 km, 5 km, 10 km, Halbmarathon,
Marathon) der schnellste zusammenhängende Abschnitt irgendwo im Track –
mit Tempo und der Kilometerstelle, an der er beginnt. Grundlage ist die
ohnehin berechnete kumulierte Distanz, es ist also keine zusätzliche
Geo-Berechnung nötig.

Ein Klick auf einen Punkt im Höhenprofil zentriert die Karte auf den
entsprechenden Ort.

**Planung** (Schalter "📐 Planung" in der Seitenleiste):

Ist genau **ein** Track ausgewählt, lässt sich der Planungsmodus
einschalten. Darin wird der Track per Mausklick – auf die Karte oder ins
Höhenprofil – in Teile unterteilt:

- Ein Klick setzt einen Unterteilungspunkt (auf der Karte wird der
  nächstgelegene Trackpunkt verwendet), ein erneuter Klick auf denselben
  Punkt entfernt ihn wieder; alternativ über das "✕" in der Punkteliste.
- Die Kennzahlen-Box zeigt statt der Werte je Track die Werte je Teil.
- "📦 Export" lädt eine ZIP-Datei mit je einer GPX-Datei pro Teil sowie
  einer weiteren GPX-Datei mit den Unterteilungspunkten als Wegpunkte.

Wird ein zweiter Track dazu ausgewählt, schaltet sich der Modus
automatisch wieder ab.

### Anzeigeeinstellungen

Im Bereich "⚙️ Einstellungen" der Seitenleiste (zusammen mit der
Navigation): Farb-Spalte für Karte und Höhenprofil, Breite der
Kennzahlen-Spalte sowie die Höhe von Karte + Profil – wahlweise
automatisch an die Browser-Fensterhöhe angepasst oder manuell per
Schieberegler.

**Statistik** (Seite "Statistik"):

Auswertung über alle Tracks hinweg, ohne Filter:

- Kopfzeile mit Anzahl Tracks, Gesamtstrecke, Gesamtaufstieg und
  Gesamtzeit.
- Tab "Zeitverlauf": Kilometer je Jahr (mit Aufstieg und Anzahl Tracks)
  sowie Kilometer je Monat für ein auswählbares Jahr.
- Tab "Sportarten": Aufstiegsmeter je Sportart als Diagramm, dazu eine
  Tabelle mit Anzahl Tracks, Kilometern, Aufstieg und Stunden.
- Tab "Heatmap": alle aufgezeichneten Punkte als Wärmekarte – zeigt auf
  einen Blick, welche Gegenden und Strecken wie oft zurückgelegt wurden.
  Die Punkte werden ausgedünnt geladen (jeder 10.) und gecacht.

## Auf- und Abstieg: Schwellwert oder Glättung

Für Auf-/Abstieg stehen zwei Verfahren zur Verfügung, die sich in der
Verwaltung ("Track-Metadaten neu berechnen") auch kombinieren lassen:

- **Schwellwert mit Hysterese** (Standard): Höhenänderungen unterhalb der
  eingestellten minimalen Höhenänderung gelten als Rauschen. Robust gegen
  zappelnde GPS-Höhen, unterschätzt aber lange, gleichmäßig flache
  Anstiege, deren Einzelschritte jeweils unter dem Schwellwert bleiben.
- **Glättung** (gleitender Mittelwert über n Trackpunkte): mittelt das
  Rauschen weg und erhält den langsamen Trend. Bei barometrisch
  aufgezeichneten Höhen (Sportuhr, Radcomputer) meist die genauere
  Variante. Für das reine Glättungsverfahren die minimale Höhenänderung
  auf `0` und die Glättung auf z.B. `15` setzen.

Beispiel (synthetischer Track, gleichmäßiger Anstieg von 600 m mit ±2 m
Rauschen): Schwellwert 2 m ergibt 1183 m, ohne Schwellwert 1463 m,
Glättung über 15 Punkte 596 m.

## Hinweise / Einschränkungen

- DuckDB erlaubt pro Datenbankdatei nur **eine** schreibende Verbindung
  gleichzeitig. `app.py` und `init.py` sollten daher nicht parallel als
  separate Prozesse laufen.
- Die GPX-Verarbeitung (Distanz/Tempo/Steigung) wird pro Track gecacht
  (`@st.cache_data`), damit Klicks im Höhenprofil keine erneute,
  aufwändige GeoPandas-Berechnung auslösen.
- Die Heatmap der Statistik-Seite muss beim ersten Aufruf alle
  GPX-Dateien einlesen; das Ergebnis wird gecacht und erst bei der
  nächsten Änderung an den Tracks neu aufgebaut.
- "Bestzeiten" wertet nur EINEN Track aus. Über mehrere Tracks hinweg
  wäre der Begriff nicht sinnvoll definiert, da Abschnitte sonst über
  getrennte Aufzeichnungen hinweg laufen würden.
- Für den Datei-Upload werden ausschließlich `.gpx`-Dateien mit einem
  `<trk>`-Track akzeptiert (Format wie von gängigen GPS-Geräten/Apps
  exportiert). Dateien ohne Trackpunkte (reine Wegpunkt- oder
  Routen-Dateien) werden mit einer Meldung abgewiesen, statt die Seite
  mit einem Fehler abbrechen zu lassen.
- Kennzahlen aus Zeit und Tempo setzen Zeitstempel in der GPX-Datei
  voraus. Fehlen sie, bleiben Dauer, Tempo und "Zeit in Bewegung" leer;
  Distanz und Höhenwerte werden trotzdem berechnet.