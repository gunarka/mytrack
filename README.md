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
| `functions.py`   | **Gemeinsame Logik.** Datenbankverbindung & -Schema, GPX-Verarbeitung mit GeoPandas, Reverse-Geocoding, Zeitzonen-Ermittlung, Bestzeiten-Auswertung, GPX-Export, alle CRUD-Funktionen (Create/Read/Update/Delete) für Tracks, Touren und Sportarten sowie die gecachten Leseabfragen von Karte und Statistik (`load_metadata`, `load_track_files`, `load_track_notes`, `load_heatmap_points`). Enthält keinerlei Oberflächen-Code. |
| `stats.py`       | **Statistik-Seite** (Seite "Statistik"). Gesamtwerte, Kilometer je Jahr/Monat, Auswertung je Sportart sowie eine Heatmap aller aufgezeichneten Punkte. Rechnet fast ausschließlich mit den gespeicherten Kennzahlen, ohne die GPX-Dateien erneut zu verarbeiten. |
| `admin.py`       | **Verwaltungsoberfläche** (Seite "Verwaltung"). Drei Tabs (Tracks, Touren, Sportarten), jeweils mit Formular zum Neuanlegen, Formular zum Bearbeiten/Löschen und einer Übersichtstabelle. |
| `map.py`         | **Kartenansicht** (Seite "Karte"). Pills-Filter nach Sport/Land/Jahr/Jahreszeit, darunter eine aufklappbare Jahr -> Monat -> Tour -> Track-Auswahl (Touren eingeklappt), Folium-Karte mit eingefärbten Tracks, gemeinsames Höhenprofil (Plotly) mit Klick-Interaktion, die Info-Punkte sowie der Planungsmodus. |
| `map_linked.py`  | **Karte mit Hover-Synchronisation** (Seite "Karte (Sync)"). Dieselben Filter, Kennzahlen und den Planungs-Schalter wie `map.py`, aber Karte und Höhenprofil in EINER Browser-Komponente (Leaflet + uPlot). Dadurch reagieren beide ohne Server-Rerun aufeinander: Hover im Profil zeigt den Punkt auf der Karte und umgekehrt. |
| `init.py`        | **Eigenständiges Werkzeug** zum (Neu-)Anlegen der Datenbankstruktur. Löscht beim Klick auf den Button alle vorhandenen Daten – bewusst getrennt von `app.py`, damit das nicht versehentlich im normalen Betrieb passiert. |

`map.py`, `map_linked.py`, `stats.py` und `admin.py` stellen jeweils eine
Funktion `render_map_page()`, `render_linked_map_page()`,
`render_stats_page()` bzw. `render_admin_page()` bereit. `app.py` registriert diese über
[`st.navigation`](https://docs.streamlit.io/develop/api-reference/navigation/st.navigation)
als Seiten und kümmert sich um die gemeinsame Seitenleiste. Diese Dateien
lassen sich zum Debuggen weiterhin auch einzeln starten
(`streamlit run admin.py` / `streamlit run map.py` /
`streamlit run map_linked.py` / `streamlit run stats.py`).

Die Sidebar-Filter (Sport/Land/Jahr/Jahreszeit und die Track-Auswahl)
liegen als `map.render_track_filters()` an einer Stelle und werden von
beiden Kartenseiten genutzt – beide verwenden dieselben Widget-Keys, die
Auswahl bleibt beim Wechsel zwischen ihnen also erhalten.

Die Datenbankverbindung (`functions.get_connection()`) ist über
`st.cache_resource` als Singleton implementiert: Alle Module im selben
Streamlit-Prozess teilen sich dieselbe DuckDB-Verbindung. Geschlossen wird
sie ausschließlich beim geordneten Beenden über den Knopf "🚪 Beenden"
(`functions.close_connection()` / `functions.shutdown_app()`).

Die Leseabfragen der Kartenseite sind über `st.cache_data` gecacht. Jede
schreibende Funktion (anlegen / ändern / löschen / neu berechnen) leert
diesen Cache gezielt (`_invalidate_track_caches()`), sodass Änderungen aus
der Verwaltung sofort auf der Karte sichtbar sind.

## Datenmodell

Lokale [DuckDB](https://duckdb.org/)-Datei unter `.data/tracks.duckdb` mit
vier Tabellen:

- **`sport`** – Sportarten (`sport_id`, `sport_title`)
- **`tours`** – Touren (`tour_id`, `tour_title`)
- **`track_notes`** – Info-Punkte zu einem Track (`note_id`, `track_id`,
  Titel, Beschreibung, Art, `lat`/`lon`, Höhe, `point_index`). Sie
  unterteilen den Track nicht, müssen nicht auf ihm liegen und werden
  beim Export als Wegpunkte mitgegeben (siehe "Punkte zur Tour" unten).
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
Die Info-Punkte eines Tracks werden dagegen mit ihm gelöscht – ohne
seinen Streckenverlauf hätten sie keinen Bezug mehr.

Neue Tabellen und Spalten werden bei jedem Verbindungsaufbau sanft
nachgezogen (`_ensure_schema_migrations()`); `init.py` ist dafür **nicht**
nötig (es würde alle Daten löschen).

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

Beendet wird die App über den Knopf **"🚪 Beenden"** am unteren Rand der
Seitenleiste (siehe unten) – damit endet auch der `streamlit`-Prozess im
Terminal. Alternativ `Strg+C` im Terminal.

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
   **direkt bearbeiten**, um mehrere Titel auf einmal umzubenennen.
   Der Knopf darunter zeigt die Zahl der geänderten Zeilen und speichert
   auch nur diese; leer gelassene Titel werden übersprungen und gemeldet.
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
aufklappbare Liste nach Jahr, darin gruppiert nach Monat.

Eine **Tour steht dort als EINE Zeile** – eingeklappt, mit Titel, Anzahl
der Etappen und Zeitraum (z. B. `🧭 Alpenüberquerung (6) · 12.07.–17.07.`).
Ein Klick auf ihre Checkbox wählt alle Etappen auf einmal aus; der
`▸`-Knopf davor klappt die einzelnen Tracks auf, die sich dann auch
einzeln (ab-)wählen lassen. Eine Tour erscheint je Jahr genau **einmal**,
einsortiert unter dem Monat ihrer ersten Etappe – auch wenn sie über
einen Monatswechsel läuft. Es muss mindestens ein Track ausgewählt sein –
solange keiner gewählt ist, weisen Seitenleiste und Hauptbereich darauf hin;
die Seitenleiste (inklusive **"🚪 Beenden"**) bleibt dabei voll bedienbar.
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

**Planung** (Schalter "📐 Planung" in der Seitenleiste, auf **beiden**
Kartenseiten vorhanden):

Ist genau **ein** Track ausgewählt, lässt sich der Planungsmodus
einschalten. Darin wird der Track per Mausklick – auf die Karte oder ins
Höhenprofil – in Teile unterteilt:

- Ein Klick setzt einen Unterteilungspunkt (auf der Karte wird der
  nächstgelegene Trackpunkt verwendet), ein erneuter Klick auf denselben
  Punkt entfernt ihn wieder; alternativ über das "✕" in der Punkteliste.
- Die Kennzahlen-Box zeigt statt der Werte je Track die Werte je Teil.
- "📦 Export" lädt eine ZIP-Datei: je eine GPX-Datei pro Teil (mitsamt den
  Info-Punkten des jeweiligen Abschnitts), eine GPX-Datei mit den
  Trennpunkten und eine mit allen Info-Punkten als Wegpunkte.
- Die gesetzten Punkte erscheinen orange und nummeriert auf Karte und
  Höhenprofil – auch auf der Seite "Karte (Sync)" (dort nur zur Anzeige,
  siehe unten).

Wird ein zweiter Track dazu ausgewählt, schaltet sich der Modus
automatisch wieder ab.

### Punkte zur Tour (Info-Punkte)

Unterhalb der Kennzahlen liegt der Bereich **"📍 Punkte zur Tour"**:
dauerhaft gespeicherte Anmerkungen zu einem Track – Hütte, Aussicht,
Wasserstelle, Abzweig, Gefahrenstelle.

- Sie **unterteilen den Track nicht** (das tun die Trennpunkte des
  Planungsmodus) und müssen **nicht auf dem Track liegen** – die Hütte
  steht selten genau auf der Spur.
- Angelegt werden sie bei genau **einem** ausgewählten Track, sowohl im
  normalen Betrieb als auch im Planungsmodus, über "➕ Punkt hinzufügen"
  (Titel, Beschreibung, Koordinaten).
- Die Position lässt sich direkt eingeben oder – mit der Checkbox
  **"Position per Kartenklick"** – per Klick auf die Karte übernehmen;
  die vorgemerkte Stelle erscheint solange als grauer Marker. Im
  Planungsmodus hat diese Einstellung Vorrang: Der Kartenklick setzt dann
  keinen Trennpunkt mehr, das geht währenddessen über das Höhenprofil.
- Angezeigt werden sie immer und auf beiden Kartenseiten: als blauer
  Marker auf der Karte (Titel und Text im Popup) und als blaue Raute im
  Höhenprofil, an der Kilometer-Stelle des nächstgelegenen Trackpunkts.
- Jeder Punkt lässt sich über sein Aufklapp-Feld ändern oder löschen.
- **Export:** Sie hängen als Wegpunkte (`<wpt>`) an jedem Export des
  zugehörigen Tracks – "⬇️ GPX herunterladen" in der Verwaltung, "Tour als
  GPX exportieren" sowie dem ZIP des Planungsmodus.

### Anzeigeeinstellungen

Im Bereich "⚙️ Einstellungen" der Seitenleiste (zusammen mit der
Navigation): Farb-Spalte für Karte und Höhenprofil, Breite der
Kennzahlen-Spalte sowie die Höhe von Karte + Profil. Für die Höhe gibt es
drei Modi:

| Modus | Verhalten |
|---|---|
| **Fenster füllen** (Standard) | Karte und Profil füllen zusammen die Fensterhöhe. Das Höhenprofil behält die eingestellte Pixelhöhe, die Karte bekommt den Rest bis zum unteren Fensterrand. Wirkt sofort beim ersten Laden und zieht beim Ändern der Fenstergröße live mit; die Kennzahlen-Spalte scrollt bei Bedarf in sich selbst, statt die Seite zu verlängern. |
| **Fensterhöhe messen (JS)** | Liest die Fensterhöhe per JavaScript aus und rechnet daraus feste Pixelwerte. Braucht immer einen zusätzlichen Durchlauf und aktualisiert sich nach einer Größenänderung erst bei der nächsten Interaktion – nur noch als Ausweichweg gedacht. |
| **Manuell (px)** | Feste Gesamthöhe per Schieberegler. |

**Karte (Sync)** (Seite "Karte (Sync)"):

Dieselbe Track-Auswahl wie auf der Seite "Karte", aber Karte und
Höhenprofil arbeiten hier direkt zusammen – ohne Nachladen:

- **Maus über dem Höhenprofil** → ein roter Marker wandert auf der Karte
  an die passende Stelle.
- **Maus über der Karte** → das Fadenkreuz im Profil springt auf den
  nächstgelegenen Trackpunkt.
- Eine Leiste über der Karte zeigt zum jeweiligen Punkt Track, Kilometer,
  Höhe, Tempo, Steigung und die vergangene Zeit.
- **Im Profil ziehen** zoomt auf einen Abschnitt; die Karte zoomt
  automatisch auf genau diesen Abschnitt mit. "Alles zeigen" (oben rechts
  im Profil) setzt beides zurück.
- **Klick ins Profil** zentriert die Karte auf den Punkt.
- **Marker im Höhenprofil:** Start ("S", grün), Ende ("Z", rot), die
  Info-Punkte ("i", blau) und – im Planungsmodus – die Trennpunkte
  (orange, nummeriert), jeweils mit senkrechter Hilfslinie. Dieselben
  Punkte liegen an derselben Stelle auf der Karte.
- Tritt im Browser ein Fehler auf (Bibliothek nicht ladbar, Kachelserver
  nicht erreichbar), erscheint dazu ein roter Hinweis oben in der
  Komponente – statt einer wortlos leeren Karte.
- Linie und Profilkurve sind nach derselben Farbskala eingefärbt wie auf
  der Seite "Karte" (Höhe, Geschwindigkeit, Gefälle); bei "Nichts"
  bekommt jeder Track eine eigene Farbe. Unten rechts liegt die Legende.
- In den Einstellungen lässt sich zusätzlich die Hintergrundkarte wählen
  (OpenTopoMap, OpenStreetMap, Carto Positron).

Der **Planungsmodus** lässt sich auch hier über den Schalter "📐 Planung"
einschalten: Die Kennzahlen je Teil, die Punkteliste (mit "✕" zum Löschen)
und der ZIP-Export stehen wie auf der Seite "Karte" links, die Trennpunkte
erscheinen in Karte und Höhenprofil. **Neue Trennpunkte per Klick setzen**
geht dagegen nur auf der Seite "Karte": Das braucht Serverzustand, den die
Komponente hier normalerweise nicht an Streamlit zurückmelden kann.

**Neue Info-Punkte** ("📍 Punkte zur Tour") lassen sich hier dagegen direkt
auf der Karte anlegen:

- **Rechtsklick** auf eine Stelle in der Karte öffnet ein schwebendes
  Overlay **"📍 Punkt speichern?"** mit Feldern für Titel und Beschreibung.
- "Speichern" legt den Punkt an genau dieser Stelle an – die Karte muss
  dafür **nicht** auf einen Trackpunkt treffen, wie beim Formular auf der
  Seite "Karte".
- Das geht nur bei genau **einem** ausgewählten Track; bei mehreren zeigt
  das Overlay stattdessen einen Hinweis, links in der Punkteliste zuerst
  auf einen Track einzugrenzen.
- Technisch verlässt sich das auf einen kleinen Rückkanal-Trick über die
  Zusatzbibliothek `streamlit-javascript` (siehe CONTEXT.md) – dafür ist
  am Verhalten der Seite selbst sonst nichts weiter zu beachten.
- Das Formular mit den Koordinatenfeldern (Titel, Beschreibung, Breite,
  Länge) bleibt links in der Punkteliste ebenfalls bestehen, z. B. um
  bereits angelegte Punkte zu bearbeiten oder Koordinaten von Hand
  einzutragen.

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

### Anwendung beenden

Ganz unten in der Seitenleiste liegt der Knopf **"🚪 Beenden"**. Er wird auf
jeder Seite und in jedem Zustand angezeigt – auch dann, wenn noch kein Track
ausgewählt ist oder die Datenbank noch leer ist. Ein Klick genügt – es
passiert dreierlei:

1. Die DuckDB-Verbindung wird geschlossen – die Datei
   `.data/tracks.duckdb` ist danach wieder frei (z. B. für `init.py`).
2. Es erscheint die Meldung "Anwendung beendet"; das Browser-Fenster kann
   geschlossen werden.
3. Der Streamlit-Prozess im Terminal wird beendet – kein `Strg+C` nötig.

Es gibt keine Rückfrage: Laufende Uploads oder nicht gespeicherte
Formulareingaben gehen beim Klick verloren.

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
- Die Seite "Karte (Sync)" lädt Leaflet und uPlot von einem CDN und
  benötigt dafür eine Internetverbindung. Je Bibliothek sind zwei CDNs
  hinterlegt (unpkg, jsDelivr); ist keines erreichbar, erscheint eine
  Meldung in der Komponente statt einer leeren Fläche. Für den
  Offline-Betrieb lassen sich die Dateien lokal ablegen und die Konstanten
  `_CDN_*` in `map_linked.py` anpassen.
- Sehr große Auswahlen werden auf der Seite "Karte (Sync)" für die
  Darstellung ausgedünnt (höchstens 12.000 Punkte insgesamt, siehe
  `_MAX_TOTAL_POINTS`); ein Hinweis unter der Karte weist darauf hin. Die
  Kennzahlen werden davon nicht berührt.
- Jeder Streamlit-Rerun (z.B. ein geänderter Filter) baut die Komponente
  der Seite "Karte (Sync)" neu auf – Kartenausschnitt und Profil-Zoom
  beginnen dann wieder bei der Gesamtansicht.
- Kennzahlen aus Zeit und Tempo setzen Zeitstempel in der GPX-Datei
  voraus. Fehlen sie, bleiben Dauer, Tempo und "Zeit in Bewegung" leer;
  Distanz und Höhenwerte werden trotzdem berechnet.