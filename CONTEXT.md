# CONTEXT

Kurzer Projekt-Kontext für die Weiterentwicklung von **MyTrack** – gedacht
als Einstieg für neue Mitarbeit und als Briefing für KI-Assistenten. Die
Benutzersicht (was die App kann, wie man sie bedient) steht im
[README](README.md); hier stehen Architektur, Konventionen und Fallstricke.

## Überblick

| | |
|---|---|
| Zweck | GPX-Tracks hochladen, verwalten, auf Karte/Höhenprofil ansehen, auswerten |
| Stack | Python 3.10+, Streamlit ≥ 1.49, DuckDB, GeoPandas/gpxpy, Folium, Plotly |
| Ausführung | Lokal, Single-User, `streamlit run app.py` |
| Daten | Lokale Datei `.data/tracks.duckdb` (nicht im Repo, siehe `.gitignore`) |
| Sprache | Oberfläche, Kommentare, Docstrings und Commits auf **Deutsch** |

## Dateien und Zuständigkeiten

```
app.py         Einstiegspunkt: set_page_config, globales CSS, Sidebar, Navigation
├── map.py     Seite "Karte"      -> render_map_page(settings_container=None)
├── stats.py   Seite "Statistik"  -> render_stats_page()
└── admin.py   Seite "Verwaltung" -> render_admin_page()
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
den Einstellungen-Expander gerendert).

`st.set_page_config()` muss der allererste Streamlit-Aufruf bleiben und
steht deshalb in `app.py`, nicht in den Seitenmodulen.

## Datenmodell

Drei Tabellen (Anlage in `functions.init_database()`):

- `sport` – `sport_id UUID`, `sport_title`
- `tours` – `tour_id UUID`, `tour_title`
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

**Schema-Änderungen:** Neue Spalten zusätzlich in
`_ensure_schema_migrations()` ergänzen (läuft bei jedem
Verbindungsaufbau). Bestandsdatenbanken dürfen nicht auf
`init_database()` angewiesen sein – das löscht alle Daten.

## Caching

| Funktion | Cache | Inhalt |
|---|---|---|
| `get_connection()` | `cache_resource` | DuckDB-Verbindung als Singleton |
| `process_track()` | `cache_data` | Aufbereitete Trackpunkte je `track_id` |
| `load_metadata()` | `cache_data` | Kennzahlen aller Tracks für Karte/Statistik |
| `load_track_files()` | `cache_data` | GPX-Blobs der ausgewählten Tracks |
| `load_heatmap_points()` | `cache_data` | Ausgedünnte Punkte für die Heatmap |
| `reverse_geocode()`, `get_timezone()` | `cache_data` | Ergebnisse externer Abfragen |

Jede schreibende Funktion ruft am Ende `_invalidate_track_caches()` auf.
Wer eine neue Schreiboperation ergänzt, muss das ebenfalls tun – sonst
zeigen Karte und Statistik veraltete Daten.

DuckDB erlaubt nur **eine** schreibende Verbindung je Datei: `app.py` und
`init.py` nie gleichzeitig laufen lassen.

## Zustand (`st.session_state`)

Konvention: sprechende Schlüssel als Widget-`key`; interne Hilfswerte mit
führendem Unterstrich.

- Filter/Auswahl Karte: `sport_select`, `country_select`, `year_select`,
  `season_select`, Checkboxen `track_<id>` / `tour_<jahr>_<monat>_<id>`
- Anzeige: `plot_column`, `kpi_col_width_pct`, `map_profile_height_mode`,
  `map_profile_total_height_px`, `window_height_js`
- Karte/Profil: `selected_point`, `my_chart_key`
- Planung: `planning_mode`, `split_points`, `_last_planning_click`,
  `_last_planning_map_click`, `_last_track_ids`

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
- Streamlit ≥ 1.49: `width="stretch"` statt `use_container_width`,
  `height=` bei `st.plotly_chart`.
- Layout-CSS zentral im `st.markdown`-Block in `app.py`, nicht verteilt in
  den Seiten und nicht in `config.toml` (TOML ist kein CSS).
- Keine Geheimnisse, keine Datenbank, keine `__pycache__`-Ordner
  einchecken.

## Wo ändere ich was?

| Vorhaben | Ort |
|---|---|
| Neue Kennzahl je Track | `summarize_track()` + Spalte in `init_database()`/`_ensure_schema_migrations()` + Anzeige in `_render_kpis()` (map.py) |
| Neuer Filter auf der Karte | `render_map_page()` (Pills-Block) + ggf. `load_metadata()` |
| Neue Auswertung | `stats.py` (`_render_*`), Daten über `load_metadata()` |
| Neue Verwaltungsfunktion | Formular in `admin.py`, Schreiblogik in `functions.py` |
| Neue Seite | Modul mit `render_*_page()` + Eintrag in `pages` in `app.py` |
| Änderung am GPX-Parsing | `process_gpx_dataframe()` (pro Punkt) |

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

## Arbeitsweise

Änderungen werden als Patch geliefert und im Projektverzeichnis eingespielt:

```bash
git apply --check ~/Downloads/<name>.patch && git apply ~/Downloads/<name>.patch
git add -A && git commit -m "<Beschreibung>"
git push
```
