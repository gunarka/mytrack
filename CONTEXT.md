# CONTEXT

Kurzer Projekt-Kontext für die Weiterentwicklung von **MyTrack** – gedacht
als Einstieg für neue Mitarbeit und als Briefing für KI-Assistenten. Die
Benutzersicht (was die App kann, wie man sie bedient) steht im
[README](README.md); hier stehen Architektur, Konventionen und Fallstricke.

## Überblick

| | |
|---|---|
| Zweck | GPX-Tracks hochladen, verwalten, auf Karte/Höhenprofil ansehen, auswerten |
| Stack | Python 3.10+, Streamlit ≥ 1.49, DuckDB, GeoPandas/gpxpy, Folium (nur Heatmap auf der Statistik-Seite); die Kartenseite "Karte" nutzt zusätzlich Leaflet + uPlot (per CDN im Browser, siehe map_linked.py) |
| Ausführung | Lokal, Single-User, `streamlit run app.py` |
| Daten | Lokale Datei `.data/tracks.duckdb` (nicht im Repo, siehe `.gitignore`) |
| Sprache | Oberfläche, Kommentare, Docstrings und Commits auf **Deutsch** |

## Dateien und Zuständigkeiten

```
app.py         Einstiegspunkt: set_page_config, globales CSS, Sidebar, Navigation,
               "Beenden"-Knopf (Abschiedsseite + functions.shutdown_app());
               der Knopf wird NACH navigation.run() gerendert und ist auf
               jeder Seite und in jedem Zustand sichtbar
├── map_linked.py  Seite "Karte" (Standardseite) -> render_linked_map_page(settings_container=None)
│              Karte + Profil als EINE HTML/JS-Komponente (Leaflet + uPlot),
│              wählbare X-/Y-Achse + Einfärbung des Höhenprofils
├── stats.py   Seite "Statistik"  -> render_stats_page()
└── admin.py   Seite "Verwaltung" -> render_admin_page()
map.py         KEINE eigene Seite mehr - Bibliothek gemeinsamer Bausteine der
               Kartenseite (Filter, Kennzahlen, Info-Punkte, Planungsmodus,
               Höhenermittlung), von map_linked.py importiert. Frühere
               eigenständige Folium/Plotly-Kartenseite ("Karte"), siehe
               Abschnitt "Entfernte Folium/Plotly-Kartenseite" unten.
functions.py   Gesamte Fachlogik: DB, GPX, Geocoding, Kennzahlen, CRUD
init.py        Separates Werkzeug: Tabellen neu anlegen (löscht alle Daten!)
.streamlit/config.toml   Streamlit-Konfiguration (wirksam)
config.toml              Kopie im Wurzelverzeichnis - von Streamlit NICHT gelesen
requirements.txt
```

**Schichtregel:** `functions.py` enthält keinen Oberflächen-Code, die
Seitenmodule keine Fachlogik. Neue Berechnungen, Abfragen oder
Schreibzugriffe gehören nach `functions.py` und werden von den Seiten nur
aufgerufen. Jede Seite stellt genau eine `render_*_page()`-Funktion bereit;
`app.py` registriert sie über `st.Page`/`st.navigation` (mit
`position="hidden"`, die Menüpunkte werden per `st.page_link()` selbst in
den Einstellungen-Expander gerendert, der standardmäßig **eingeklappt**
ist - `expanded=False` in `app.py`/`map.py`/`map_linked.py`, alle drei
Stellen teilen sich denselben Expander-Titel "⚙️ Einstellungen").

`st.set_page_config()` muss der allererste Streamlit-Aufruf bleiben und
steht deshalb in `app.py`, nicht in den Seitenmodulen.

## Datenmodell

Vier Tabellen (Anlage in `functions.init_database()`):

- `sport` – `sport_id UUID`, `sport_title`
- `tours` – `tour_id UUID`, `tour_title`
- `track_notes` – Info-Punkte je Track (DDL als Konstante
  `_TRACK_NOTES_DDL`, damit sie in `init_database()` UND in
  `_ensure_schema_migrations()` identisch ist): `note_id`, `track_id`,
  `note_title`, `note_text`, `note_kind` (`info`/`planning`), `lat`,
  `lon`, `ele`, `point_index`, `time_stamp`.
  `point_index` ist der Index des **nächstgelegenen** Trackpunkts im
  aufbereiteten DataFrame (`process_track`) – nur daraus ergibt sich die
  Stelle im Höhenprofil; die eigentliche Position (`lat`/`lon`) darf
  daneben liegen. Abgrenzung zu den Trennpunkten des Planungsmodus:
  Trennpunkte zerschneiden den Track und leben nur in `st.session_state`,
  Info-Punkte tragen Informationen und liegen dauerhaft in der Datenbank.
- `gpx` – ein Datensatz je Track:
  - Identität/Zuordnung: `track_id`, `track_title`, `sport_id`, `tour_id`
    (beide optional, beim Löschen von Sport/Tour auf `NULL` gesetzt)
  - Orte: `location_start_*` / `location_end_*` (Land, Bundesland, Kreis,
    Ort, Stadtteil, Straße), `*_lat_lon` als `STRUCT(lat, lon)`,
    `*_address` als JSON, Bounding-Box `location_lat/lon_min/max`
  - Zeit: `time_zone`, `time_start`, `time_end`, `track_time_s`,
    `track_time_moving_s`
  - Kennzahlen: `track_distance_m`, `track_ascent_m`, `track_descent_m`,
    `elevation_min/max`, `speed_min/max`, `slope_min/max`
  - Rohdaten: `file_name`, `file_data` (BLOB mit der Original-GPX),
    `time_stamp`

Die GPX-Datei bleibt vollständig gespeichert. Dadurch sind Höhenprofil,
Bestzeiten, Planung, Heatmap und das Neuberechnen von Kennzahlen jederzeit
ohne erneuten Upload möglich.

Beim Löschen eines Tracks werden seine Info-Punkte mitgelöscht
(`delete_track()`), beim Export als Wegpunkte angehängt
(`build_note_waypoints()` / `_with_note_waypoints()`, genutzt von
`get_track_file()`, `export_tour_gpx()` und dem Planungs-ZIP in `map.py`).

**GPX-Export in der Verwaltung:** `export_selection_gpx(track_ids,
tour_ids)` bündelt eine beliebige Auswahl aus einzelnen Tracks (via
`get_track_file()`) und/oder ganzen Touren (via `export_tour_gpx()`) zu
EINEM Download - eine Datei direkt bei genau einer Auswahl, sonst ein
ZIP (Namenskollisionen werden mit Zähler-Suffix aufgelöst). Genutzt vom
eigenständigen Expander `_render_gpx_export_expander()` in `admin.py`,
der oberhalb der drei Tabs steht (siehe `render_admin_page()`).

**Schema-Änderungen:** Neue Spalten zusätzlich in
`_ensure_schema_migrations()` ergänzen (läuft bei jedem
Verbindungsaufbau). Bestandsdatenbanken dürfen nicht auf
`init_database()` angewiesen sein – das löscht alle Daten.

## Caching

| Funktion | Cache | Inhalt |
|---|---|---|
| `get_connection()` | `cache_resource` | DuckDB-Verbindung als Singleton (geschlossen nur via `close_connection()`) |
| `process_track()` | `cache_data` | Aufbereitete Trackpunkte je `track_id` |
| `load_metadata()` | `cache_data` | Kennzahlen aller Tracks für Karte/Statistik |
| `load_track_files()` | `cache_data` | GPX-Blobs der ausgewählten Tracks |
| `load_track_notes()` | `cache_data` | Info-Punkte der ausgewählten Tracks |
| `load_heatmap_points()` | `cache_data` | Ausgedünnte Punkte für die Heatmap |
| `reverse_geocode()`, `get_timezone()` | `cache_data` | Ergebnisse externer Abfragen |

Jede schreibende Funktion ruft am Ende `_invalidate_track_caches()` auf.
Wer eine neue Schreiboperation ergänzt, muss das ebenfalls tun – sonst
zeigen Karte und Statistik veraltete Daten.

DuckDB erlaubt nur **eine** schreibende Verbindung je Datei: `app.py` und
`init.py` nie gleichzeitig laufen lassen. Der "Beenden"-Knopf ruft
`close_connection()` auf und gibt die Datei damit wieder frei.

## Zustand (`st.session_state`)

Konvention: sprechende Schlüssel als Widget-`key`; interne Hilfswerte mit
führendem Unterstrich.

- Filter/Auswahl Karte: `sport_select`, `country_select`, `year_select`,
  `season_select`, Checkboxen `track_select_<id>` /
  `tour_select_<jahr>_<id>` sowie `_tour_open_<jahr>_<id>` (auf-/zugeklappt)
- Anzeige: `kpi_col_width_pct`, `window_height_js` (Rückgabewert der
  JS-Fensterhöhen-Messung, siehe `map._resolve_map_profile_height()` -
  keine eigene Auswahl/Einstellung mehr für den Höhen-Modus)
- Info-Punkte: `_note_position` (vorgemerkte Koordinaten). `note_click_mode`
  wird von `_render_notes_panel()` nur gesetzt, wenn `allow_map_click=True`
  ist - der einzige verbleibende Aufrufer (`map_linked.py`) übergibt IMMER
  `False`, der Schlüssel entsteht in der aktuellen Oberfläche also nicht
  mehr (die Funktion selbst unterstützt ihn weiterhin, für einen
  potenziellen künftigen Aufrufer mit Kartenklick-Rückkanal).
- Karte/Profil: `selected_point`, `my_chart_key`
- Kartenseite ("Karte", `map_linked.py`): `lm_plot_column` (Einfärbung),
  `lm_axis_x`/`lm_axis_y` (Achsenwahl des Höhenprofils, siehe
  `map_linked._AXIS_X_OPTIONS`/`_AXIS_Y_OPTIONS` - bleiben als normale
  Widget-`key`s automatisch über Reruns/Trackwechsel hinweg erhalten),
  `lm_basemap`, `_lm_note_bridge_gen` (Zähler für den `key` der
  `streamlit-javascript`-Rückkanal-Komponente, siehe
  `map_linked._handle_note_bridge`)
- Planung: `planning_mode`, `split_points` (Setzen per Klick ist in der
  aktuellen Oberfläche nicht mehr möglich - nur Anzeigen, Löschen über
  `_render_planning_kpis()` und Export)
- Beenden: `_shutdown_requested` (Abschiedsseite statt `navigation.run()`)

Die Track-Checkboxen werden bewusst persistent gehalten, damit die Auswahl
Filterwechsel und Rerenders übersteht (`_persistent_checkbox()` in
`map.py`).

## Konventionen

- Ausführliche deutsche Docstrings am Dateikopf und an jeder öffentlichen
  Funktion, dazu Kommentare, die das **Warum** erklären (nicht das Was).
  Dieser Stil ist durchgängig und soll erhalten bleiben.
- Öffentliche Funktionen ohne Unterstrich, modulinterne Helfer mit
  führendem `_`.
- Type Hints an Funktionssignaturen.
- Streamlit ≥ 1.49: `width="stretch"` statt `use_container_width`.
- Layout-CSS zentral im `st.markdown`-Block in `app.py`, nicht verteilt in
  den Seiten und nicht in `config.toml` (TOML ist kein CSS).
- Keine Geheimnisse, keine Datenbank, keine `__pycache__`-Ordner
  einchecken.

## Wo ändere ich was?

| Vorhaben | Ort |
|---|---|
| Neue Kennzahl je Track | `summarize_track()` + Spalte in `init_database()`/`_ensure_schema_migrations()` + Anzeige in `_render_kpis()` (map.py) |
| Neuer Filter auf der Karte | `render_track_filters()` (Pills-Block) in `map.py` + ggf. `load_metadata()` |
| Neue Auswertung | `stats.py` (`_render_*`), Daten über `load_metadata()` |
| Neue Verwaltungsfunktion | Formular in `admin.py`, Schreiblogik in `functions.py` |
| Neue Seite | Modul mit `render_*_page()` + Eintrag in `pages` in `app.py` |
| Verhalten beim Beenden | `shutdown_app()`/`close_connection()` in `functions.py`, UI-Teil am Ende von `app.py` |
| Verhalten ohne Track-Auswahl | `render_track_filters()` (leeres DataFrame) + `render_no_selection_hint()` in `map.py` |
| Filter der Kartenseite | `render_track_filters()` in `map.py`, genutzt von `map_linked.py` |
| Darstellung der Track-Auswahl | `_render_track_tree()` / `_render_tour_group()` in `map.py` |
| Info-Punkte (Logik/Export) | `functions.py`, Abschnitt "Info-Punkte" |
| Info-Punkte (Bedienung) | `_render_notes_panel()` in `map.py`, genutzt von `map_linked.py` |
| Bestzeiten | `_render_best_efforts()` in `map.py`, genutzt von `map_linked.py` |
| Höhe von Karte + Profil | `_resolve_map_profile_height()` in `map.py` (automatisch per JS, keine Auswahl mehr) |
| Achsenwahl (X/Y) des Höhenprofils | `_AXIS_X_OPTIONS`/`_AXIS_Y_OPTIONS`/`_AXIS_X_FIELDS`/`_AXIS_Y_FIELDS` in `map_linked.py`, Übertragung in `_build_payload()`, Auswertung im JS-Teil (`XKEY`/`YKEY`); kumulierte Zeit/Bewegungszeit/Punktzähler je Punkt ebenfalls dort berechnet (Zeit in Bewegung nutzt die Spalte `time_moving_passed_s` aus `functions.process_gpx_dataframe()`) |
| Einfärbung Karte/Profil | `_COLOR_OPTIONS` in `map_linked.py`, Auswertung im JS-Teil (`COL`/`attrOf()`) - unabhängig von der Achsenwahl |
| Planungs-Schalter | `render_planning_toggle()` in `map.py`; Trennpunkte setzen per Klick gibt es in der aktuellen Oberfläche nicht mehr (siehe "Entfernte Folium/Plotly-Kartenseite" unten) |
| Interaktion Karte/Profil ohne Rerun | JS-Vorlage `_HTML_TEMPLATE` in `map_linked.py` |
| Änderung am GPX-Parsing | `process_gpx_dataframe()` (pro Punkt) |

## Karte + Profil in einer Komponente (map_linked.py)

Eine Karte als Folium-iframe und ein separates Plotly-Chart können nur
über einen Server-Rerun miteinander reden. `st_folium` meldet
ausschließlich Klicks/Viewport, `st.plotly_chart` ausschließlich
`on_select` – ein Rerun dauert 100-500 ms, eine Hover-Kopplung bräuchte
< 16 ms. Genau das bot eine frühere, inzwischen entfernte eigenständige
Folium/Plotly-Kartenseite nicht (siehe Abschnitt "Entfernte Folium/
Plotly-Kartenseite" unten) - deshalb liegen auf der (jetzt einzigen)
Seite "Karte" Karte und Profil in **derselben JS-Laufzeit**:

- Aufbau: `_build_payload()` (DataFrames -> JSON) ->
  `_component_html()` (JSON in die HTML/JS-Vorlage) ->
  `st.components.v1.html()`.
- Kopplung im Browser: uPlot-Hook `setCursor` -> Leaflet-Marker;
  Leaflet-`mousemove` -> `u.setCursor()`. Der nächstgelegene Trackpunkt
  wird über einen Gitter-Index (~200-m-Zellen) gesucht, nicht linear.
- Achsenwahl (`XKEY`/`YKEY`, aus `D.axis.x`/`D.axis.y`): `_build_payload()`
  überträgt je Punkt IMMER alle vier X-Kandidaten (`km`/`tmin`/`tmovmin`/
  `pt`) und alle drei Y-Kandidaten (`ele`/`spd`/`slope`) - das JS wählt nur
  noch per `t[XKEY]`/`t[YKEY]` das passende Feld aus. Dadurch bleibt der
  gesamte übrige Code (Gradient, Marker, Zoom-Sync, Karten-Hover ->
  Profil-Cursor) unabhängig von der aktuellen Achsenwahl. Neuer
  X-/Y-Kandidat: zusätzliches Feld je Punkt in `_build_payload()` (Python)
  ergänzen UND in `_AXIS_X_OPTIONS`/`_AXIS_Y_OPTIONS`/`_AXIS_X_FIELDS`/
  `_AXIS_Y_FIELDS` eintragen - am JS-Teil selbst ändert sich dafür nichts.
- Einfärbung: Die Linie wird in 240 Abschnitte mit je einer Farbe zerlegt
  (Leaflet-Canvas), das Profil bekommt einen Canvas-Verlauf entlang der
  x-Achse (uPlot). Karte und Profil verwenden dafür denselben Punktindex
  (nicht den X-Achsen-Wert), Karten- und Profilfarbe stimmen deshalb
  UNABHÄNGIG von der aktuellen Achsenwahl punktgenau überein.
- **Warum nicht MapLibre GL?** Erster Anlauf, verworfen: MapLibre braucht
  WebGL *und* einen Web Worker (dort wird jede GeoJSON-Quelle geparst). Im
  Streamlit-iframe kamen Basiskarte und DOM-Marker durch, die
  GeoJSON-Linien aber nicht. Leaflet rendert im Hauptthread auf ein
  Canvas und braucht weder WebGL noch Worker.
- **Fehler sichtbar machen:** In einem iframe sieht niemand die
  Browser-Konsole. `window.onerror`, `unhandledrejection` und
  Leaflets `tileerror` schreiben deshalb in einen roten Balken oben in der
  Komponente. Beim Erweitern der JS-Vorlage diesen Weg beibehalten - sonst
  äußert sich jeder Tippfehler wieder als leere Fläche.
- Die Bibliotheken werden per `loadScript()` nachgeladen (nicht als
  `<script src>` im Dokument), damit ein nicht erreichbares CDN eine
  Meldung ergibt statt "X is not defined". Zweites CDN als Ausweichweg.
- Marker im Profil (Start `S`, Ende `Z`, Trennpunkte orange/nummeriert)
  werden im `draw`-Hook direkt auf das uPlot-Canvas gezeichnet, nicht als
  eigene Serie - eine Serie bräuchte je Marker-Sorte ein weiteres Array der
  Länge N. Die Trennpunkt-Indizes beziehen sich auf das volle DataFrame und
  werden in `_build_payload()` per `np.searchsorted` auf den nächsten
  tatsächlich übertragenen (ausgedünnten) Punkt abgebildet.
- Die Komponente selbst ist eine **Einbahnstraße**: `st.components.v1.html()`
  liefert nur einmal Daten hinein, kein eingebauter Rückkanal nach
  Streamlit. Trennpunkte werden hier deshalb nur **angezeigt und
  gelöscht** (über die Punkteliste in der Kennzahlen-Spalte,
  `map._render_planning_kpis()`) - neu SETZEN lässt sich in der aktuellen
  Oberfläche nicht mehr (siehe "Entfernte Folium/Plotly-Kartenseite"
  unten). Schalter (`map.render_planning_toggle()`), Kennzahlen je Teil
  und GPX-Export bleiben unverändert.
- **Rückkanal für "📍 Punkt speichern?" (neue Info-Punkte):** Rechtsklick
  auf die Karte öffnet ein schwebendes JS-Overlay (Titel + Beschreibung,
  siehe `.note-overlay`/`openNoteOverlay()` in der HTML-Vorlage). "Speichern"
  schickt die Eingabe per `window.parent.postMessage({mytrackNote: {...}})`.
  Eine unsichtbar gerenderte `streamlit-javascript`-Komponente
  (`_NOTE_BRIDGE_JS`, siehe `_handle_note_bridge()`) lauscht im selben
  übergeordneten Fenster auf genau diese Nachricht (`window.parent` ist von
  beiden Komponenten-iframes aus dasselbe Objekt, da beide direkte
  Kind-iframes derselben Streamlit-Seite sind und wegen
  `allow-same-origin` same-origin darauf zugreifen dürfen - dasselbe
  Prinzip wie bei `st_javascript("window.parent.innerHeight")` in
  `map._resolve_map_profile_height()`) und liefert sie als Rückgabewert an
  Python. Nach dem Verarbeiten wird die Bridge-Komponente über einen in
  `session_state["_lm_note_bridge_gen"]` gezählten Suffix im `key` neu
  gemountet, damit sie nicht bei jedem weiteren Rerun denselben Punkt
  erneut meldet. Nur möglich bei genau einem ausgewählten Track (JS prüft
  `SINGLE_TRACK_ID`, Python prüft `trackId` gegen `single_track_id` erneut).
  Diese Bridge trägt bewusst NUR den einen Fall "neuer Info-Punkt" - für
  Trennpunkte (s.o.) wurde sie nicht verwendet, um die Komponente nicht
  unnötig zu verkomplizieren.
- Keine neuen Python-Abhängigkeiten (`streamlit-javascript` stand bereits
  vor dieser Änderung in requirements.txt, siehe `map.py`); die beiden
  JS-Bibliotheken Leaflet/uPlot kommen versionsgepinnt vom CDN (`_CDN_*`).

## Höhe von Karte und Profil

Keine Auswahl/Einstellung mehr dafür in der Seitenleiste - die Höhe wird
IMMER automatisch ermittelt, über `map._resolve_map_profile_height()`:
misst per `st_javascript` (`window.parent.innerHeight`) die Browser-
Fensterhöhe und teilt sie über `_split_map_profile_height()` in Karten-
und Profilhöhe auf (Mindesthöhen `_MIN_MAP_HEIGHT_PX`/
`_MIN_PROFILE_HEIGHT_PX`, Obergrenze `_MAX_TOTAL_HEIGHT_PX`). Braucht je
Messung einen Rerun und hinkt nach Größenänderungen bis zur nächsten
Nutzerinteraktion hinterher - das ist bekannt und akzeptiert, dafür gibt es
nur noch dieses eine Verhalten zu pflegen/testen (kein CSS-Füllmodus, kein
manueller Schieberegler mehr).

`_keyed_container()` in `map.py` vergibt weiterhin feste Keys (`mp_box`,
`mp_map`, `mp_profile`, `mp_kpis`) für Karte/Profil/Kennzahlen-Container -
aktuell ohne eigene CSS-Regeln, aber als Ansatzpunkt erhalten, falls
künftig wieder gezielt CSS auf einzelne Bereiche wirken soll. Fällt auf ein
schlüsselloses `st.container()` zurück, falls die Streamlit-Version `key`
noch nicht kennt.

## Achsenwahl und Einfärbung des Höhenprofils (map_linked.py)

Drei unabhängige Auswahlen in den Anzeigeeinstellungen, alle als normale
`st.selectbox`-Widgets mit `key` (bleiben dadurch automatisch über
Reruns/Trackwechsel hinweg erhalten):

| Auswahl | session_state-Key | Optionen (Reihenfolge = Vorbelegung) | Felder je Punkt |
|---|---|---|---|
| X-Achse | `lm_axis_x` | Entfernung, Zeit, Zeit in Bewegung, Track Punkt # | `km`/`tmin`/`tmovmin`/`pt` |
| Y-Achse | `lm_axis_y` | Höhe, Geschwindigkeit, Gefälle | `ele`/`spd`/`slope` |
| Einfärbung | `lm_plot_column` | Höhe, Geschwindigkeit, Gefälle, Nichts | (nutzt dieselben Felder wie die Y-Achse, siehe `attrOf()`) |

`map_linked._AXIS_X_OPTIONS`/`_AXIS_Y_OPTIONS`/`_COLOR_OPTIONS` sind die
jeweilige Beschriftung+Einheit; `_AXIS_X_FIELDS`/`_AXIS_Y_FIELDS` bilden
den Auswahlwert auf den Feldnamen in der an die Komponente übertragenen
Track-Struktur ab.

`_build_payload()` überträgt je Punkt IMMER alle vier X- und alle drei
Y-Kandidaten (keine Neuberechnung bei einer Änderung der Achsenwahl nötig
außer dem üblichen Rerun) - das JS wählt nur per `D.axis.x`/`D.axis.y`
(`XKEY`/`YKEY`) das passende Feld aus (siehe vorheriger Abschnitt). Die
Einfärbung ist bewusst unabhängig von der Y-Achse: Sie liest ihren
Vergleichswert selbst aus `t.ele`/`t.spd`/`t.slope` (Funktion `attrOf()`),
nicht aus dem gerade als Y-Achse gewählten Feld.

Alle vier X-Kandidaten laufen - wie zuvor nur `km` - über ALLE
ausgewählten Tracks hinweg stetig weiter (`distance_offset`/
`time_offset`/`time_moving_offset`/`point_offset` in `_build_payload()`),
damit mehrere Tracks im gemeinsamen Profil hintereinander erscheinen
statt sich zu überlagern. Die kumulierte "Zeit in Bewegung" je Punkt
(Spalte `time_moving_passed_s`, Sekunden) wird in
`functions.process_gpx_dataframe()` berechnet: Zeitdifferenzen zu Punkten
unterhalb `DEFAULT_MIN_SPEED_MOVING_KMH` fließen mit 0 statt ihrer
tatsächlichen Dauer ein, bevor kumuliert wird - Pendant zur Track-weiten
Kennzahl aus `compute_moving_time_s()`, hier aber als fortlaufende Reihe
je Punkt statt als einzelner Summenwert.

Die Werte-Leiste über der Karte (`showPoint()` im JS-Teil) zeigt
UNABHÄNGIG von der Achsenwahl immer alle Kennzahlen (Track, km, Höhe,
Tempo, Steigung, Zeit) - nur Profilkurve, Flächenfüllung und
Achsenbeschriftung wechseln mit `XKEY`/`YKEY`.

## Entfernte Folium/Plotly-Kartenseite

Bis zu dieser Änderung gab es zwei Kartenseiten: "Karte" (Folium +
Plotly, in `map.py`, mit `render_map_page()`) und "Karte (Sync)" (Leaflet
+ uPlot in einer Komponente, in `map_linked.py`). Die erste wurde
entfernt; die zweite wurde zu "Karte" umbenannt und ist jetzt die
Standardseite. Für künftige Arbeit an dieser Stelle wichtig:

- `map.py` ist seitdem KEINE eigene Seite mehr (kein `render_map_page()`,
  kein `st.Page(...)`-Eintrag in `app.py`), sondern reine Bibliothek für
  `map_linked.py` (siehe Modul-Docstring von `map.py`).
- Mit der alten Seite sind auch entfernt: `_render_map_and_profile()`,
  `_build_hover_texts()`, `_note_texts()`, `_NOTE_COLOR`,
  `_toggle_split_point()` sowie die Folium/Plotly-spezifischen Importe
  (`folium`, `folium.plugins.Fullscreen`, `branca.colormap`,
  `plotly.graph_objects`, `streamlit_folium.st_folium` - `folium`/
  `streamlit_folium` bleiben aber reale Abhängigkeiten, da `stats.py`
  seine Heatmap weiterhin darüber baut). `plotly`/`branca` sind seitdem
  aus `requirements.txt` entfernt, da sie sonst nirgends mehr importiert
  werden.
- **Funktionslücke:** Unterteilungspunkte im Planungsmodus ließen sich
  bislang NUR auf der alten Seite per Klick (Karte oder Profil) SETZEN
  (`_toggle_split_point()`, über `st_folium`- bzw. Plotly-`on_select`-
  Rückgabewerte). Die neue alleinige Seite "Karte" hat dafür keinen
  Rückkanal (`st.components.v1.html()` ist eine Einbahnstraße, siehe
  oben) - Trennpunkte lassen sich seitdem nur noch ANZEIGEN, LÖSCHEN
  (`map._render_planning_kpis()`, Knopf "✕") und EXPORTIEREN, nicht mehr
  per Klick neu setzen. Eine Wiederherstellung dieser Funktion bräuchte
  einen eigenen Rückkanal in `map_linked.py`, ähnlich dem für neue
  Info-Punkte (`_handle_note_bridge()`) - z.B. indem der Planungsmodus
  beim Rechtsklick-Overlay zwischen "Info-Punkt anlegen" und "Trennpunkt
  setzen" umschaltet.
- Die Funktion `_render_best_efforts()` ("🏅 Bestzeiten") blieb in
  `map.py` erhalten und wird jetzt explizit von `render_linked_map_page()`
  aufgerufen (vorher geschah das nur in `render_map_page()`).
- `_note_position_on_track()` (in `map.py`) hatte zwischenzeitlich (für
  eine seitdem wieder entfernte Y-Achsen-Auswahl der alten Seite) einen
  `y_column`-Parameter; nach dem Entfernen der alten Seite liefert sie
  wieder einfach `(index, distance_m, elevation_m)`.

## Fallstricke

- `timedelta.dt.total_seconds()` verwenden, nie `.dt.seconds` (schneidet
  Bruchteile ab und springt nach 24 h zurück).
- Distanzen erst nach `to_crs(estimate_utm_crs())` berechnen; in EPSG:4326
  sind sie nicht maßstabsgetreu.
- `lat`/`lon` werden vor der Umprojektion als eigene Spalten gesichert.
- Nominatim hat ein Rate-Limit von 1 Anfrage/Sekunde. Fällt der Dienst aus,
  wird der Track ohne Ortsangaben gespeichert statt den Upload abzubrechen.
- GPX ohne Zeitstempel: Dauer, Tempo und "Zeit in Bewegung" bleiben leer,
  Distanz und Höhe werden trotzdem berechnet.
- GPX ohne `<trkpt>` (reine Wegpunkt-/Routendateien) werden mit Meldung
  abgewiesen.
- Auf-/Abstieg hängt vom Verfahren ab (Schwellwert mit Hysterese vs.
  Glättung) – Vergleichswerte nur mit identischen Parametern.
- `config.toml` im Wurzelverzeichnis ist wirkungslos; wirksam ist
  `.streamlit/config.toml`.
- `st.data_editor` gibt Arrow-fremde Spalten **als Strings** zurück. Die
  IDs kommen aus DuckDB aber als `uuid.UUID`-Objekte – ein Vergleich
  `uuid == str` ist immer falsch. Vor dem Editor deshalb
  `df["track_id"] = df["track_id"].astype(str)` setzen (DuckDB nimmt
  Strings in `WHERE track_id = ?` an) und den Abgleich alt/neu über ein
  Dictionary statt über eine Suche je Zeile führen.
- `st.data_editor` merkt sich Eingaben je **Zeilenposition**, bei festem
  `key` auch dann, wenn sich die Daten geändert haben. Tabellen mit
  veränderlichem Bestand bekommen deshalb einen Schlüssel, der den
  aktuellen Bestand enthält (siehe `_render_track_overview()`).
- JSON in `<script>`: `</` muss maskiert werden (siehe
  `_component_html()`), sonst beendet ein Track-Titel mit `</script>` den
  Skriptblock.
- `st.components.v1.html` ist seit Streamlit 1.56 abgekündigt (Ersatz:
  `st.iframe`, erkennt HTML-Text selbst). `_render_component()` wählt
  automatisch; der HTML-Text muss dafür mit `<` beginnen.
- uPlot braucht sein Stylesheet, sonst liegen Achsen und Cursor nicht über
  der Zeichenfläche und das Diagramm wirkt leer. Es ist deshalb als
  `_UPLOT_CSS` fest eingebaut statt per CDN geladen.
- uPlot vor dem Layout des iframes zu erzeugen ergibt Breite 0 (leeres
  Diagramm): Der Aufbau wartet deshalb zwei Frames ab und rechnet mit
  einer Mindestbreite.
- In `map_linked.py` kein f-String für die HTML-Vorlage verwenden – die
  Vorlage ist voller geschweifter Klammern (JS/CSS). Platzhalter werden
  per `.replace()` ersetzt.
- `process_track()` liefert ein **gecachtes** DataFrame. Kein Modul sollte
  Spalten darin verändern (in-place mutieren) - wer eine über mehrere
  Tracks hinweg fortlaufende Größe braucht (wie `map_linked.py` es für
  Entfernung/Zeit/Zeit in Bewegung/Punktzähler tut, siehe Abschnitt
  "Achsenwahl und Einfärbung des Höhenprofils"), berechnet sie als neues
  Array aus den vorhandenen Spalten (z.B. `dist_delta`), statt eine
  vorhandene Spalte zu überschreiben.
- `render_*_page()` darf den Seitenaufbau **nur per `return`** abbrechen,
  nie per `st.stop()`: `st.stop()` beendet den kompletten Skriptdurchlauf,
  damit auch den erst danach gerenderten "🚪 Beenden"-Knopf aus `app.py`
  (und ein späteres Abfangen ist nicht möglich – nach einer Stop-Anforderung
  bricht jedes weitere `st.`-Kommando erneut ab). `render_track_filters()`
  liefert deshalb bei fehlender Auswahl ein **leeres DataFrame**;
  `render_linked_map_page()` prüft `meta.empty`, zeigt
  `render_no_selection_hint()` und kehrt zurück.
- Beenden: Die Abschiedsseite muss **vor** `navigation.run()` geprüft und
  mit `st.stop()` abgeschlossen werden – sonst läuft beim Herunterfahren
  noch eine Seite gegen die gerade geschlossene Verbindung. Der Prozess
  wird zeitversetzt in einem Hintergrund-Thread beendet, damit die Antwort
  den Browser noch erreicht; `get_connection()` darf sonst nirgends
  geschlossen werden.

- Streamlit erlaubt **keinen `st.expander` im `st.expander`**. Die Touren
  im Track-Baum stecken bereits im Jahres-Expander und werden deshalb über
  einen eigenen `▸`/`▾`-Knopf mit Merker in `st.session_state`
  auf-/zugeklappt (`_render_tour_group()`).
- `st_folium` liefert `last_clicked` bei jedem Rerun erneut zurück, bis
  ein neuer Klick erfolgt - relevant für die Heatmap-Karte in `stats.py`
  bzw. für jeden künftigen `st_folium`-Einsatz: Jede Auswertung eines
  Kartenklicks braucht ihren eigenen "zuletzt verarbeiteter Klick"-Merker
  in `st.session_state`, sonst entsteht eine Rerun-Schleife.
- Eingabefelder, deren Vorbelegung sich zwischen Reruns ändert (die
  Koordinaten im Formular "Punkt hinzufügen"), bekommen **keinen** `key`:
  Mit `key` würde Streamlit den alten Wert aus dem Sitzungszustand
  wiederherstellen und die neue Vorbelegung ignorieren.

## Arbeitsweise

Änderungen werden als Patch geliefert und im Projektverzeichnis eingespielt:

```bash
git apply --check ~/Downloads/<name>.patch && git apply ~/Downloads/<name>.patch
git add -A && git commit -m "<Beschreibung>"
git push
```
