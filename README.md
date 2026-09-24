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
| `admin.py`       | **Verwaltungsoberfläche** (Seite "Verwaltung"). Eigenständiger Export-Bereich ("⬇️ GPX-Export") oberhalb von drei Tabs (Tracks, Touren, Sportarten), die jeweils ein Formular zum Neuanlegen, ein Formular zum Bearbeiten/Löschen und eine Übersichtstabelle enthalten. |
| `map_linked.py`  | **Kartenansicht** (Seite "Karte", Standardseite beim Start). Pills-Filter nach Sport/Land/Jahr/Jahreszeit, darunter eine aufklappbare Jahr -> Monat -> Tour -> Track-Auswahl (Touren eingeklappt), Karte und Höhenprofil in EINER Browser-Komponente (Leaflet + uPlot) mit beidseitiger Hover-Synchronisation, wählbaren Profil-Achsen und Einfärbung, die Info-Punkte sowie der Planungsmodus. |
| `map.py`         | **Gemeinsame Bausteine der Kartenseite** (keine eigene Seite): Sidebar-Filter, Kennzahlen-Anzeige, Bestzeiten, Info-Punkte-Verwaltung, Planungsmodus-Kennzahlen/-Export sowie die Höhenermittlung für Karte/Profil - von `map_linked.py` importiert. |
| `init.py`        | **Eigenständiges Werkzeug** zum (Neu-)Anlegen der Datenbankstruktur. Löscht beim Klick auf den Button alle vorhandenen Daten – bewusst getrennt von `app.py`, damit das nicht versehentlich im normalen Betrieb passiert. |

`map_linked.py`, `stats.py` und `admin.py` stellen jeweils eine Funktion
`render_linked_map_page()`, `render_stats_page()` bzw. `render_admin_page()`
bereit. `app.py` registriert diese über
[`st.navigation`](https://docs.streamlit.io/develop/api-reference/navigation/st.navigation)
als Seiten und kümmert sich um die gemeinsame Seitenleiste. Diese Dateien
lassen sich zum Debuggen weiterhin auch einzeln starten
(`streamlit run admin.py` / `streamlit run map_linked.py` /
`streamlit run stats.py`); `map.py` ist keine eigene Seite und hat daher
keinen eigenen Startmodus.

Die Sidebar-Filter (Sport/Land/Jahr/Jahreszeit und die Track-Auswahl)
liegen als `map.render_track_filters()` an einer Stelle und werden von
`map_linked.py` genutzt, statt sie dort zu duplizieren.

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

Benötigt Python 3.10+ und Streamlit 1.49 oder neuer (für `width="stretch"`).

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
6. Oberhalb der drei Tabs steht der eigenständige Bereich
   **"⬇️ GPX-Export"**: beliebig viele einzelne Tracks und/oder ganze
   Touren (jeweils mit allen ihren Tracks) auswählen und gemeinsam
   herunterladen. Bei genau einer Auswahl gibt es direkt eine einzelne
   GPX-Datei, bei mehreren ein ZIP-Archiv mit je einer Datei pro Track
   bzw. Tour. Punkte (Info-Punkte) werden dabei immer mit exportiert.

**Karte** (Seite "Karte", Standardseite beim Start):

Karte und Höhenprofil liegen in EINER Browser-Komponente (Leaflet + uPlot)
und synchronisieren sich gegenseitig ohne Serverkontakt: Hover über dem
Profil bewegt einen Marker auf der Karte mit, Hover über der Karte bewegt
den Cursor im Profil mit; eine gemeinsame Werte-Leiste (Track, km, Höhe,
Tempo, Steigung, Zeit) zeigt dabei immer alle Kennzahlen des Punkts unter
dem Mauszeiger, unabhängig von der gewählten Achsendarstellung (siehe
unten). Zoom im Profil (Ziehen mit der Maus) zoomt die Karte auf denselben
Abschnitt; ein Klick im Profil zentriert die Karte auf den Punkt; die
Schaltfläche "Alles zeigen" setzt beides zurück.

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

In den Anzeigeeinstellungen (siehe unten) lassen sich X-/Y-Achse des
Höhenprofils, die Einfärbung von Linie und Profilkurve sowie die
Hintergrundkarte wählen.

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

**Planung** (Schalter "📐 Planung" in der Seitenleiste):

Ist genau **ein** Track ausgewählt, lässt sich der Planungsmodus
einschalten. Er zeigt den Track in Teile unterteilt:

- Die Kennzahlen-Box zeigt statt der Werte je Track die Werte je Teil.
- "📦 Export" lädt eine ZIP-Datei: je eine GPX-Datei pro Teil (mitsamt den
  Info-Punkten des jeweiligen Abschnitts), eine GPX-Datei mit den
  Trennpunkten und eine mit allen Info-Punkten als Wegpunkte.
- Bereits gesetzte Punkte erscheinen orange und nummeriert auf Karte und
  Höhenprofil und lassen sich über das "✕" in der Punkteliste wieder
  löschen. **Neue Punkte lassen sich in der aktuellen Oberfläche nicht per
  Klick setzen** – dafür bräuchte es einen Rückkanal von der Karte/dem
  Profil nach Streamlit, den es (anders als beim Anlegen eines Info-Punkts
  per Rechtsklick, siehe unten) hierfür noch nicht gibt.

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
  normalen Betrieb als auch im Planungsmodus – entweder über
  **Rechtsklick auf die Karte** (öffnet ein Overlay "📍 Punkt speichern?"
  mit Titel/Beschreibung; die Koordinaten des Klicks werden dabei
  übernommen) oder über "➕ Punkt hinzufügen" in der Kennzahlen-Spalte
  (Titel, Beschreibung, Koordinaten manuell eingeben).
- Angezeigt werden sie immer: als blauer Marker auf der Karte (Titel und
  Text im Popup) und als blaue Raute im Höhenprofil, an der Stelle des
  nächstgelegenen Trackpunkts auf der jeweils gewählten X-/Y-Achse (siehe
  Anzeigeeinstellungen unten).
- Jeder Punkt lässt sich über sein Aufklapp-Feld in der Kennzahlen-Spalte
  ändern oder löschen.
- **Export:** Sie hängen als Wegpunkte (`<wpt>`) an jedem Export des
  zugehörigen Tracks – "⬇️ GPX herunterladen" in der Verwaltung, "Tour als
  GPX exportieren" sowie dem ZIP des Planungsmodus.

### Anzeigeeinstellungen

Im (standardmäßig eingeklappten) Bereich "⚙️ Einstellungen" der
Seitenleiste (zusammen mit der Navigation): X-/Y-Achse des Höhenprofils,
Einfärbung von Linie und Profilkurve, Hintergrundkarte sowie die Breite
der Kennzahlen-Spalte.

**X-Achse** – Größe auf der waagerechten Achse des Höhenprofils, läuft bei
mehreren ausgewählten Tracks über alle hinweg stetig weiter (die Tracks
erscheinen im gemeinsamen Profil hintereinander statt sich zu
überlagern):

| Option | Zeigt |
|---|---|
| **Entfernung** (Standard) | Kumulierte Distanz seit Beginn der Auswahl, in km. |
| **Zeit** | Kumulierte Zeit seit Beginn der Auswahl, in Minuten – inklusive Pausen/Stillstand. |
| **Zeit in Bewegung** | Wie "Zeit", aber ohne Pausen/Stillstand (Zeitdifferenzen unterhalb der Bewegungs-Schwelle zählen nicht mit). |
| **Track Punkt #** | Fortlaufende Nummer des Trackpunkts. |

**Y-Achse** – Größe auf der senkrechten Achse des Höhenprofils:

| Option | Zeigt |
|---|---|
| **Höhe** (Standard) | Meter über dem Meeresspiegel. |
| **Geschwindigkeit** | Tempo je Punkt in km/h. |
| **Gefälle** | Steigung/Gefälle je Punkt in %. |

**Einfärbung** – Farbe von Track-Linie UND Profilkurve, unabhängig von
X-/Y-Achse wählbar: Höhe, Geschwindigkeit, Gefälle oder Nichts (dann
bekommt jeder Track stattdessen seine eigene Farbe; Legende unten rechts
im Profil).

Start ("S", grün), Ende ("Z", rot), Info-Punkte ("i", blau) und – im
Planungsmodus – die Trennpunkte (orange, nummeriert) erscheinen jeweils an
derselben X-/Y-Stelle auf Karte UND Höhenprofil, mit senkrechter
Hilfslinie im Profil. Die Werte-Leiste über der Karte zeigt beim Hover
unabhängig von der Achsenwahl immer ALLE Kennzahlen (Track, km, Höhe,
Tempo, Steigung, Zeit).

Zusätzlich lässt sich die **Hintergrundkarte** wählen (OpenTopoMap,
OpenStreetMap, Carto Positron). Tritt im Browser ein Fehler auf (Bibliothek
nicht ladbar, Kachelserver nicht erreichbar), erscheint dazu ein roter
Hinweis oben in der Komponente – statt einer wortlos leeren Karte.

Die Höhe von Karte + Höhenprofil wird ohne eigene Einstellung automatisch
per JavaScript aus der Fensterhöhe ermittelt und passt sich bei einer
Nutzerinteraktion an eine geänderte Fenstergröße an.

**Neue Info-Punkte** ("📍 Punkte zur Tour") lassen sich direkt auf der
Karte anlegen:

- **Rechtsklick** auf eine Stelle in der Karte öffnet ein schwebendes
  Overlay **"📍 Punkt speichern?"** mit Feldern für Titel und Beschreibung.
- "Speichern" legt den Punkt an genau dieser Stelle an – die Karte muss
  dafür **nicht** auf einen Trackpunkt treffen.
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
- Die Kartenseite lädt Leaflet und uPlot von einem CDN und
  benötigt dafür eine Internetverbindung. Je Bibliothek sind zwei CDNs
  hinterlegt (unpkg, jsDelivr); ist keines erreichbar, erscheint eine
  Meldung in der Komponente statt einer leeren Fläche. Für den
  Offline-Betrieb lassen sich die Dateien lokal ablegen und die Konstanten
  `_CDN_*` in `map_linked.py` anpassen.
- Sehr große Auswahlen werden für die
  Darstellung ausgedünnt (höchstens 12.000 Punkte insgesamt, siehe
  `_MAX_TOTAL_POINTS`); ein Hinweis unter der Karte weist darauf hin. Die
  Kennzahlen werden davon nicht berührt.
- Jeder Streamlit-Rerun (z.B. ein geänderter Filter oder eine geänderte
  Achsen-/Farbauswahl) baut die Kartenkomponente neu auf – Kartenausschnitt
  und Profil-Zoom beginnen dann wieder bei der Gesamtansicht.
- Kennzahlen aus Zeit und Tempo setzen Zeitstempel in der GPX-Datei
  voraus. Fehlen sie, bleiben Dauer, Tempo und "Zeit in Bewegung" leer;
  Distanz und Höhenwerte werden trotzdem berechnet.
- Im Planungsmodus lassen sich Unterteilungspunkte aktuell **nicht** per
  Klick setzen (siehe Abschnitt "Karte" oben) – nur anzeigen, löschen und
  exportieren. Das war eine Klick-Interaktion einer früheren,
  eigenständigen Karten-/Profilseite auf Basis von Folium/Plotly, die zu
  Gunsten der synchronisierten Leaflet/uPlot-Komponente entfernt wurde.