"""
map_linked.py
=============
Seite "Karte (Sync)" - Karte und Höhenprofil in EINER Browser-Komponente,
mit beidseitiger Hover-Synchronisation.

Warum eine zweite Kartenseite?
------------------------------
Die bestehende Seite "Karte" (map.py) besteht aus zwei getrennten
Streamlit-Elementen: einer Folium-Karte (eigener iframe) und einem
Plotly-Höhenprofil. Beide können nur über einen Server-Rerun miteinander
reden - `st_folium` meldet ausschließlich Klicks/Viewport, `st.plotly_chart`
ausschließlich `on_select`. Ein Rerun dauert je nach Trackgröße 100-500 ms;
für eine Hover-Kopplung ("Maus über dem Profil -> Punkt auf der Karte")
bräuchte es aber < 16 ms.

Diese Seite löst das, indem Karte UND Profil in derselben JavaScript-
Laufzeit liegen:

    Leaflet  (Karte, Canvas-Renderer, Raster-Kacheln von OpenTopoMap/OSM)
    uPlot    (Höhenprofil, Canvas, sehr schnell bei vielen Punkten)

Warum Leaflet und nicht MapLibre GL? MapLibre braucht WebGL UND einen Web
Worker (jede GeoJSON-Quelle wird dort geparst). Im Streamlit-iframe kam der
Basiskarten-Layer durch, die Track-Linien aber nicht - ein Fehlerbild, das
genau auf diese Zusatzanforderungen zeigt. Leaflet rendert alles im
Hauptthread auf ein Canvas, braucht weder WebGL noch Worker und ist in
dieser App über Folium bereits erprobt.

Beides wird über `st.components.v1.html()` als ein einziger iframe
eingebettet. Streamlit liefert dabei nur einmal die Daten (JSON im HTML),
alle Interaktionen laufen danach vollständig im Browser ab - ohne Rerun,
ohne Server.

Funktionsumfang
---------------
1. Hover über dem Profil   -> Marker wandert auf der Karte mit.
2. Hover über der Karte    -> Cursor/Fadenkreuz wandert im Profil mit
                              (nächstgelegener Trackpunkt, Gitter-Index).
3. Gemeinsame Werte-Anzeige (Track, km, Höhe, Tempo, Steigung, Zeit) in
   einer Leiste über der Karte.
4. Einfärbung von Linie UND Profilkurve nach Höhe/Tempo/Gefälle über
   dieselbe Farbskala wie auf der Folium-Seite (blau -> rot), inkl.
   Farblegende. Bei "Nichts" bekommt jeder Track eine eigene Farbe.
5. Zoom im Profil (Ziehen mit der Maus) zoomt die Karte auf denselben
   Abschnitt; Doppelklick bzw. die Schaltfläche "Alles zeigen" setzt
   beides zurück.
6. Klick ins Profil zentriert die Karte auf den Punkt.
7. Start (S, grün), Ende (Z, rot) und - im Planungsmodus - die gesetzten
   Unterteilungspunkte (orange, nummeriert) erscheinen sowohl auf der Karte
   als auch im Höhenprofil, jeweils an derselben Kilometerstelle.
8. Rechtsklick auf die Karte öffnet ein schwebendes Overlay "📍 Punkt
   speichern?" (Titel + Beschreibung); ist genau ein Track ausgewählt,
   legt "Speichern" dort einen neuen Info-Punkt an der angeklickten
   Stelle an (siehe "Rückkanal" unten).

Bewusste Einschränkungen
------------------------
- Die Komponente selbst ist eine Einbahnstraße: `st.components.v1.html()`
  liefert nur einmal Daten hinein, ein Kartenklick käme normalerweise nie
  in Streamlit an. Für den einen Fall, in dem trotzdem ein Rückkanal nötig
  ist (neuen Info-Punkt per Rechtsklick anlegen, siehe Punkt 8 oben), wird
  daher ein kleiner Trick verwendet: Die Komponente schickt die Eingaben
  per `window.parent.postMessage(...)`; eine unsichtbare
  `streamlit-javascript`-Komponente (echte bidirektionale Custom
  Component) lauscht im selben übergeordneten Fenster auf diese Nachricht
  und liefert sie an Python zurück (siehe `_handle_note_bridge` weiter
  unten). Unterteilungspunkte gehen diesen Weg bewusst NICHT mit - sie
  lassen sich hier weiterhin nur ANZEIGEN; gesetzt und gelöscht werden sie
  auf der Seite "Karte" (map.py) bzw. über die Punkteliste in der
  Kennzahlen-Spalte. Der Schalter "📐 Planung" und die Kennzahlen je Teil
  sind dagegen auf beiden Seiten vorhanden (siehe
  map.render_planning_toggle).
- Die JS-Bibliotheken werden von einem CDN geladen (siehe _CDN_*), es wird
  also eine Internetverbindung benötigt. Je Bibliothek sind zwei CDNs
  hinterlegt; schlägt das erste fehl, wird das zweite versucht. Klappt
  beides nicht, erscheint eine Meldung IN der Komponente statt einer
  leeren Fläche.
- Jeder Fehler im Browser (JS-Ausnahme, Kachel-/Ladefehler) wird als roter
  Balken oben in der Komponente angezeigt - ein stilles Scheitern in der
  Browser-Konsole, das man in Streamlit nie zu sehen bekommt, gibt es
  damit nicht mehr.
- Sehr große Auswahlen werden für die Übertragung ausgedünnt (siehe
  _MAX_TOTAL_POINTS), damit das eingebettete JSON klein bleibt.

Aufbau der Datei
----------------
- _build_payload()      Daten aller ausgewählten Tracks -> JSON-Struktur
- _component_html()     JSON + HTML/JS-Vorlage -> fertiges Dokument
- render_linked_map_page()  Seitenaufbau (Einstellungen, Filter, Kennzahlen)

Fachlogik (DB, GPX) liegt weiterhin ausschließlich in functions.py; die
Sidebar-Filter und die Kennzahlen-Anzeige werden aus map.py
wiederverwendet, damit beide Kartenseiten identisch filtern.
"""

import json

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_javascript import st_javascript

from functions import insert_track_note, load_track_files, load_track_notes, process_track
from map import (
    _keyed_container,
    _nearest_point_index,
    _render_fill_css,
    _render_height_settings,
    _render_kpis,
    _render_notes_panel,
    _render_planning_kpis,
    _resolve_map_profile_height,
    _height_mode,
    render_no_selection_hint,
    render_planning_toggle,
    render_track_filters,
)

# --------------------------------------------------------------------------
# Externe Bibliotheken (CDN, feste Versionen, mit Ausweich-CDN)
# --------------------------------------------------------------------------
# Bewusst auf exakte Versionen gepinnt: Ein stiller Major-Wechsel beim CDN
# würde die Seite sonst ohne eigenes Zutun zerlegen. Je Bibliothek sind zwei
# Adressen hinterlegt - die zweite wird nur geladen, wenn die erste nicht
# erreichbar ist (siehe Lade-Logik in der HTML-Vorlage).
_CDN_LEAFLET_JS = [
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js",
]
_CDN_LEAFLET_CSS = [
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css",
]
_CDN_UPLOT_JS = [
    "https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js",
    "https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.iife.min.js",
]

# --------------------------------------------------------------------------
# Hintergrundkarten (reine Raster-Kacheln, kein API-Schlüssel nötig)
# --------------------------------------------------------------------------
# Hinweis zur Nutzung: Die Kachel-Server sind Gemeinschaftsressourcen. Bei
# intensiver Nutzung bitte die jeweiligen Nutzungsbedingungen beachten
# (OSM: operations.osmfoundation.org/policies/tiles/).
_BASEMAPS = {
    "opentopo": {
        "label": "OpenTopoMap",
        "tiles": "https://tile.opentopomap.org/{z}/{x}/{y}.png",
        "maxzoom": 17,
        "attribution": (
            "Kartendaten: © OpenStreetMap-Mitwirkende, SRTM | "
            "Darstellung: © OpenTopoMap (CC-BY-SA)"
        ),
    },
    "osm": {
        "label": "OpenStreetMap",
        "tiles": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "maxzoom": 19,
        "attribution": "© OpenStreetMap-Mitwirkende",
    },
    "carto": {
        "label": "Carto Positron (hell)",
        "tiles": "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "maxzoom": 19,
        "attribution": "© OpenStreetMap-Mitwirkende, © CARTO",
    },
}

# Obergrenze für die Gesamtzahl übertragener Trackpunkte. Darüber wird
# gleichmäßig ausgedünnt (jeder n-te Punkt, Start/Ende bleiben erhalten).
# 12.000 Punkte entsprechen grob 700 kB JSON im HTML-Dokument - genug für
# ein glattes Profil, aber klein genug, dass der Aufbau der Seite nicht
# spürbar träge wird.
_MAX_TOTAL_POINTS = 12000

# Farbskala für die Einfärbung nach Höhe/Tempo/Gefälle - identisch zur
# LinearColormap der Folium-Seite (blau = niedrig ... rot = hoch).
_RAMP = [
    "#0000FF", "#007FFF", "#00FFFF", "#7FFF00", "#FFFF00", "#FF7F00", "#FF0000",
]

# Kategoriale Farben für den Modus "Nichts": jeder Track eine eigene Farbe
# (auf der Folium-Seite sind dort alle Tracks gleich eingefärbt).
_TRACK_COLORS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#00808a", "#f032e6", "#808000", "#9a6324", "#000075",
]

_COLOR_OPTIONS = {
    "ele": ("Höhe", "m"),
    "km_per_h": ("Geschwindigkeit", "km/h"),
    "slope": ("Gefälle", "%"),
    "none": ("Nichts", ""),
}


def _clean(values, decimals: int) -> list:
    """
    Wandelt eine Zahlenspalte in eine JSON-taugliche Liste um: gerundet auf
    'decimals' Nachkommastellen, NaN/Inf werden zu None (in JSON: null).

    Notwendig, weil json.dumps() für NaN sonst das JavaScript-fremde
    Literal 'NaN' schreibt und uPlot/Leaflet Lücken nur über 'null'
    korrekt behandeln.
    """
    arr = np.asarray(values, dtype="float64")
    return [None if not np.isfinite(v) else round(float(v), decimals) for v in arr]


def _build_payload(
    df: pd.DataFrame,
    plot_column: str,
    basemap_key: str,
    map_height: int,
    profile_height: int,
    split_points: dict | None = None,
    notes: pd.DataFrame | None = None,
    fill: bool = False,
) -> dict:
    """
    Baut die komplette Datenstruktur für die Browser-Komponente auf.

    Je Track werden die Punkt-Arrays (lat, lon, Höhe, Tempo, Gefälle,
    kumulierte Distanz, vergangene Zeit) übertragen. Die Distanz läuft -
    wie im Plotly-Profil der Folium-Seite - über alle Tracks hinweg weiter,
    damit mehrere Tracks im gemeinsamen Profil hintereinander erscheinen
    statt sich zu überlagern.

    Zusätzlich enthält die Struktur den Wertebereich der Farbskala (aus den
    gespeicherten Kennzahlen, nicht aus den ausgedünnten Punkten - so bleibt
    die Einfärbung unabhängig von der Ausdünnung), die Bounding-Box aller
    Tracks sowie die Anzeige-Einstellungen.

    'notes' sind die Info-Punkte der Tracks (siehe
    functions.load_track_notes). Sie werden je Track mit ihrer eigenen
    Position (sie müssen nicht auf dem Track liegen) und der Kilometer-
    Stelle ihres nächstgelegenen Trackpunkts übertragen; die Komponente
    zeichnet sie als blaue Marker auf Karte und Höhenprofil.

    'fill' meldet den Höhen-Modus "Fenster füllen" an die Komponente: Sie
    lässt die Karte dann über die Flexbox mitwachsen, statt ihr eine feste
    Pixelhöhe zu geben.

    'split_points' sind die im Planungsmodus gesetzten Unterteilungspunkte
    (track_id -> Liste von Punkt-Indizes, siehe map.st.session_state
    'split_points'). Sie werden auf die ausgedünnte Punktfolge umgerechnet
    (der nächstgelegene übertragene Punkt gewinnt) und je Track als
    'splits' mitgeschickt; die Komponente zeichnet sie als orange Marker
    auf Karte UND Höhenprofil. Gesetzt werden können sie hier nicht - dafür
    bräuchte es einen Rückkanal nach Streamlit (siehe Modul-Docstring).
    """
    # Zuerst alle Tracks verarbeiten (gecacht, siehe functions.process_track),
    # um die Gesamtpunktzahl und damit den nötigen Ausdünnungsfaktor zu kennen.
    frames = []
    for i in range(len(df)):
        track_id = df["track_id"].iloc[i]
        frames.append((track_id, df["track_title"].iloc[i], process_track(track_id, df["file_data"].iloc[i])))

    total_points = sum(len(gdf) for _, _, gdf in frames)
    stride = max(1, -(-total_points // _MAX_TOTAL_POINTS))  # aufgerundete Division

    tracks = []
    distance_offset = 0.0
    for pos, (track_id, title, gdf) in enumerate(frames):
        # Fortlaufende Gesamtdistanz über alle Tracks (in km für die x-Achse).
        cum_m = gdf["dist_delta"].cumsum().to_numpy() + distance_offset
        distance_offset = float(cum_m[-1]) if len(cum_m) else distance_offset

        # Gleichmäßig ausdünnen, Start- und Endpunkt immer behalten.
        idx = list(range(0, len(gdf), stride))
        if idx and idx[-1] != len(gdf) - 1:
            idx.append(len(gdf) - 1)
        # Punkte ohne gültige Koordinate aussortieren: eine einzige NaN-Position
        # würde die Kartenlinie und den Gitter-Index im Browser unbrauchbar
        # machen (Leaflet zeichnet eine Linie mit NaN-Punkt gar nicht).
        lat_all, lon_all = gdf["lat"].to_numpy(), gdf["lon"].to_numpy()
        idx = [i for i in idx if np.isfinite(lat_all[i]) and np.isfinite(lon_all[i])]
        if len(idx) < 2:
            continue
        sub = gdf.iloc[idx]

        # Unterteilungspunkte (Planungsmodus) auf die ausgedünnte Punktfolge
        # abbilden: Der Punkt-Index bezieht sich auf das VOLLE Track-
        # DataFrame, übertragen wird aber nur jeder n-te Punkt (stride).
        # Gesucht ist deshalb der nächstgelegene tatsächlich übertragene
        # Punkt - bei stride = 1 ist das exakt derselbe Punkt.
        # Hinweis: eigene Zählvariable 'sub' statt 'pos' - 'pos' ist die
        # Nummer des Tracks (Schleifenkopf) und bestimmt weiter unten
        # dessen Farbe; ein Überschreiben hier hätte die Trackfarben von
        # den gesetzten Trennpunkten abhängig gemacht.
        splits = []
        for n, raw_idx in enumerate(sorted((split_points or {}).get(track_id, [])), start=1):
            if not 0 <= raw_idx < len(gdf):
                continue
            sub_pos = int(np.searchsorted(idx, raw_idx))
            if sub_pos >= len(idx):
                sub_pos = len(idx) - 1
            elif sub_pos > 0 and abs(idx[sub_pos - 1] - raw_idx) <= abs(idx[sub_pos] - raw_idx):
                sub_pos -= 1
            splits.append({"i": sub_pos, "n": n})

        # Info-Punkte dieses Tracks: eigene Koordinaten (sie dürfen neben
        # dem Track liegen), Höhe und Kilometer-Stelle vom nächstgelegenen
        # Trackpunkt - Letztere bestimmt die Position im Höhenprofil.
        track_notes = []
        if notes is not None and not notes.empty:
            own = notes[notes["track_id"] == track_id]
            for _, note in own.iterrows():
                raw_idx = 0 if pd.isna(note["point_index"]) else int(note["point_index"])
                raw_idx = max(0, min(raw_idx, len(gdf) - 1))
                track_notes.append({
                    "lat": round(float(note["lat"]), 6),
                    "lon": round(float(note["lon"]), 6),
                    "km": round(float(cum_m[raw_idx]) / 1000.0, 4),
                    "ele": (
                        round(float(note["ele"]), 1)
                        if pd.notna(note["ele"])
                        else round(float(gdf["ele"].iloc[raw_idx]), 1)
                    ),
                    "title": str(note["note_title"] or "Punkt"),
                    "text": "" if pd.isna(note["note_text"]) else str(note["note_text"]),
                })

        time_passed = sub["time_passed"]
        seconds = (
            time_passed.dt.total_seconds()
            if pd.api.types.is_timedelta64_dtype(time_passed)
            else pd.Series(np.nan, index=sub.index)
        )

        tracks.append({
            "id": str(track_id),
            "title": str(title),
            "color": _TRACK_COLORS[pos % len(_TRACK_COLORS)],
            "lat": _clean(sub["lat"], 6),
            "lon": _clean(sub["lon"], 6),
            "ele": _clean(sub["ele"], 1),
            "spd": _clean(sub["km_per_h"], 2),
            "slope": _clean(sub["slope"], 1),
            "km": _clean(cum_m[idx] / 1000.0, 4),
            "sec": _clean(seconds, 0),
            "splits": splits,
            "notes": track_notes,
        })

    # Wertebereich der Farbskala aus den gespeicherten Kennzahlen (identisch
    # zur Folium-Seite), damit dieselben Farben dieselben Werte bedeuten.
    color_range = {
        "ele": [df["elevation_min"].min(), df["elevation_max"].max()],
        "km_per_h": [df["speed_min"].min(), df["speed_max"].max()],
        "slope": [df["slope_min"].min(), df["slope_max"].max()],
    }.get(plot_column, [0, 1])
    vmin, vmax = float(color_range[0]), float(color_range[1])
    if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
        vmin, vmax = 0.0, 1.0

    label, unit = _COLOR_OPTIONS.get(plot_column, ("", ""))
    basemap = _BASEMAPS[basemap_key]

    # Bounding-Box aller Tracks für den Startausschnitt der Karte. Fehlen
    # die gespeicherten Werte (ältere Datensätze), wird sie aus den
    # übertragenen Punkten selbst gebildet - eine Bounding-Box mit NaN
    # würde Leaflet sonst kommentarlos eine leere Karte zeigen lassen.
    lats = [v for t in tracks for v in t["lat"] if v is not None]
    lons = [v for t in tracks for v in t["lon"] if v is not None]
    bounds = [
        [float(df["location_lon_min"].min()), float(df["location_lat_min"].min())],
        [float(df["location_lon_max"].max()), float(df["location_lat_max"].max())],
    ]
    if not all(np.isfinite(v) for pair in bounds for v in pair):
        bounds = [[min(lons), min(lats)], [max(lons), max(lats)]]

    return {
        "tracks": tracks,
        "bounds": bounds,
        "color": {
            "column": plot_column,
            "label": label,
            "unit": unit,
            "vmin": round(vmin, 2),
            "vmax": round(vmax, 2),
            "ramp": _RAMP,
        },
        "basemap": basemap,
        "layout": {
            "mapHeight": int(map_height),
            "profileHeight": int(profile_height),
            "fill": bool(fill),
        },
        "stride": stride,
    }


# --------------------------------------------------------------------------
# HTML/JS-Vorlage der Komponente
# --------------------------------------------------------------------------
# Das Stylesheet von uPlot ist hier fest eingebaut (statt per CDN geladen):
# Ohne diese Regeln liegen Achsen und Cursor nicht absolut über der
# Zeichenfläche - das Diagramm sähe dann kaputt oder leer aus. Ein
# fehlgeschlagener CDN-Abruf soll nicht genau dieses Bild erzeugen.
_UPLOT_CSS = (
    ".uplot, .uplot *, .uplot *::before, .uplot *::after {box-sizing: border-box;}"
    ".uplot {font-family: inherit; line-height: 1.5; width: min-content;}"
    ".u-title {text-align: center; font-size: 18px; font-weight: bold;}"
    ".u-wrap {position: relative; user-select: none;}"
    ".u-over, .u-under {position: absolute;}"
    ".u-under {overflow: hidden;}"
    ".uplot canvas {display: block; position: relative; width: 100%; height: 100%;}"
    ".u-axis {position: absolute;}"
    ".u-legend {display: none;}"
    ".u-select {background: rgba(0,0,0,0.07); position: absolute; pointer-events: none;}"
    ".u-cursor-x, .u-cursor-y {position: absolute; left: 0; top: 0; pointer-events: none;"
    " will-change: transform;}"
    ".u-hz .u-cursor-x, .u-vt .u-cursor-y {height: 100%; border-right: 1px dashed #607D8B;}"
    ".u-hz .u-cursor-y, .u-vt .u-cursor-x {width: 100%; border-bottom: 1px dashed #607D8B;}"
    ".u-cursor-pt {position: absolute; top: 0; left: 0; border-radius: 50%; border: 0 solid;"
    " pointer-events: none; will-change: transform; background-clip: padding-box !important;}"
    ".u-axis.u-off, .u-select.u-off, .u-cursor-x.u-off, .u-cursor-y.u-off,"
    " .u-cursor-pt.u-off {display: none;}"
)

# Der Platzhalter __PAYLOAD__ wird in _component_html() durch das JSON
# ersetzt. Bewusst KEIN f-String: die Vorlage enthält jede Menge geschweifte
# Klammern (JS-Blöcke, CSS), die sonst alle verdoppelt werden müssten.
_HTML_TEMPLATE = """<style>
  __UPLOT_CSS__
  html, body { margin: 0; padding: 0; font-family: "Source Sans Pro", system-ui, sans-serif; }
  /* Flex-Spalte über die volle iframe-Höhe: Im Höhen-Modus "fill" streckt
     das CSS der Seite (map._FILL_CSS) den iframe auf den verbleibenden
     Platz bis zum Fensterrand - die Karte wächst dann über "flex: 1"
     einfach mit, ohne dass Python eine Pixelhöhe kennen müsste. In den
     anderen Modi setzt boot() feste Pixelhöhen, die Flexbox ändert daran
     nichts. */
  html, body, #wrap { height: 100%; }
  #wrap { position: relative; display: flex; flex-direction: column; }
  #map { flex: 1 1 auto; min-height: 0; }
  #profile { flex: 0 0 auto; }
  #err {
    background: #fdecea; color: #7f231c; border-bottom: 1px solid #f5c6c2;
    padding: 6px 10px; font-size: 12px; white-space: pre-wrap;
  }
  #readout {
    display: flex; flex-wrap: wrap; gap: 14px; align-items: center;
    padding: 6px 10px; font-size: 13px; line-height: 1.3;
    background: #f5f5f5; border-bottom: 1px solid #ddd; min-height: 22px;
  }
  #readout .v { font-variant-numeric: tabular-nums; font-weight: 600; }
  #readout .k { color: #666; }
  #readout .title { font-weight: 600; max-width: 260px; overflow: hidden;
                    text-overflow: ellipsis; white-space: nowrap; }
  #readout .hint { color: #888; }
  #map { width: 100%; background: #e8e8e8; }
  #profile { width: 100%; position: relative; }
  #legend {
    position: absolute; right: 10px; bottom: 10px; z-index: 500;
    background: rgba(255,255,255,0.88); border: 1px solid #ccc; border-radius: 4px;
    padding: 4px 6px; font-size: 11px; color: #333; pointer-events: none;
  }
  #legend .bar { height: 8px; width: 150px; border: 1px solid #bbb; margin: 2px 0; }
  #legend .scale { display: flex; justify-content: space-between;
                   font-variant-numeric: tabular-nums; }
  #reset {
    position: absolute; right: 10px; top: 6px; z-index: 600;
    background: #fff; border: 1px solid #ccc; border-radius: 4px;
    padding: 3px 8px; font-size: 12px; cursor: pointer;
  }
  #reset:hover { background: #f0f0f0; }
  #map { position: relative; }
  .note-overlay {
    position: absolute; z-index: 700; width: 220px;
    background: #fff; border: 1px solid #bbb; border-radius: 6px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.28); padding: 8px; font-size: 12px;
  }
  .note-overlay-head { font-weight: 600; margin-bottom: 6px; }
  .note-overlay-hint { color: #666; margin-bottom: 8px; }
  .note-overlay input, .note-overlay textarea {
    width: 100%; box-sizing: border-box; margin-bottom: 6px; padding: 4px 6px;
    font-size: 12px; font-family: inherit; border: 1px solid #ccc; border-radius: 4px;
    resize: vertical;
  }
  .note-overlay-actions { display: flex; gap: 6px; }
  .note-overlay-actions button {
    flex: 1; padding: 4px 6px; font-size: 12px; border-radius: 4px;
    border: 1px solid #ccc; background: #f7f7f7; cursor: pointer;
  }
  .note-overlay-actions .note-save { background: #1E88E5; color: #fff; border-color: #1E88E5; }
  .note-overlay-actions .note-save:disabled { opacity: 0.6; cursor: default; }
  .note-overlay-actions button:hover:not(:disabled) { filter: brightness(0.95); }
</style>

<div id="wrap">
  <div id="err" hidden></div>
  <div id="readout"><span class="hint">Karte wird geladen …</span></div>
  <div id="map"></div>
  <div id="profile"><button id="reset">Alles zeigen</button></div>
  <div id="legend"></div>
</div>

<script>
const D = __PAYLOAD__;

/* ---------------------------------------------------------------------
   0. Fehler sichtbar machen.
      In einem Streamlit-iframe bekommt man die Browser-Konsole praktisch
      nie zu Gesicht - ein JS-Fehler äußert sich sonst nur als leere
      Fläche. Deshalb landet JEDER Fehler als roter Balken in der
      Komponente selbst.
   --------------------------------------------------------------------- */
function showError(msg) {
  const el = document.getElementById("err");
  el.hidden = false;
  el.textContent = "⚠ " + msg;
}
window.addEventListener("error", e => showError(
  (e.message || "Fehler") + (e.filename ? "  (" + e.filename + ":" + e.lineno + ")" : "")));
window.addEventListener("unhandledrejection", e => showError(String(e.reason)));

/* ---------------------------------------------------------------------
   1. Bibliotheken nachladen - mit Ausweich-CDN und klarer Meldung.
      Die Skripte werden NICHT als <script src> im Dokument eingebunden,
      damit ein nicht erreichbares CDN nicht stillschweigend dazu führt,
      dass der restliche Code mit "X is not defined" abbricht.
   --------------------------------------------------------------------- */
function loadScript(url) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = url;
    s.onload = resolve;
    s.onerror = () => reject(new Error(url));
    document.head.appendChild(s);
  });
}
async function need(globalName, urls) {
  if (window[globalName]) return;
  for (const url of urls) {
    try {
      await loadScript(url);
      if (window[globalName]) return;
    } catch (e) { /* nächstes CDN versuchen */ }
  }
  throw new Error("Bibliothek '" + globalName + "' konnte nicht geladen werden. "
    + "Besteht eine Internetverbindung? Versucht: " + urls.join(" , "));
}
function loadCss(urls) {
  urls.forEach(url => {
    const l = document.createElement("link");
    l.rel = "stylesheet"; l.href = url;
    document.head.appendChild(l);
  });
}

(async function () {
  try {
    loadCss(D.cdn.leafletCss);
    await need("L", D.cdn.leafletJs);
    await need("uPlot", D.cdn.uplotJs);
    /* Zwei Frames warten, damit der iframe seine endgültige Breite hat -
       sonst bekäme uPlot beim ersten Aufbau die Breite 0. */
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    boot();
  } catch (e) {
    showError(e && e.message ? e.message : String(e));
  }
})();

function boot() {
const T = D.tracks;
const MAPH = D.layout.mapHeight, PROFH = D.layout.profileHeight;
/* Im Füllmodus bekommt die Karte ihre Höhe über die Flexbox (siehe CSS
   oben) - eine Pixelhöhe würde sie wieder festnageln. Das Profil behält
   in beiden Fällen seine eingestellte Höhe. */
if (!D.layout.fill) document.getElementById("map").style.height = MAPH + "px";
document.getElementById("profile").style.height = PROFH + "px";

if (!T.length) { showError("Keine Trackpunkte übertragen."); return; }

/* ---------------------------------------------------------------------
   2. Flache Indizes: Alle Tracks werden zu EINER Punktfolge verkettet.
      owner[g] = Track-Nummer, local[g] = Punkt-Nummer innerhalb des Tracks.
      Damit lässt sich jeder Profil-Index in O(1) auf einen Kartenpunkt
      abbilden und umgekehrt.
   --------------------------------------------------------------------- */
let N = 0;
T.forEach(t => N += t.km.length);
const owner = new Int32Array(N), local = new Int32Array(N);
const XS = new Float64Array(N), LAT = new Float64Array(N), LON = new Float64Array(N);
const offset = [];
{
  let g = 0;
  T.forEach((t, ti) => {
    offset.push(g);
    for (let i = 0; i < t.km.length; i++, g++) {
      owner[g] = ti; local[g] = i;
      XS[g] = t.km[i]; LAT[g] = t.lat[i]; LON[g] = t.lon[i];
    }
  });
}

const COL = D.color.column;
function attrOf(t, i) {
  if (COL === "ele") return t.ele[i];
  if (COL === "km_per_h") return t.spd[i];
  if (COL === "slope") return t.slope[i];
  return null;
}

/* ---------------------------------------------------------------------
   3. Farbskala (identisch zur Folium-Seite): linear zwischen sieben
      Stützfarben von blau nach rot.
   --------------------------------------------------------------------- */
const RGB = D.color.ramp.map(h => [
  parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16),
]);
function rampColor(v, alpha) {
  const a = (alpha === null || alpha === undefined) ? 1 : alpha;
  if (v === null || v === undefined || Number.isNaN(v)) return "rgba(130,130,130," + a + ")";
  let f = (v - D.color.vmin) / (D.color.vmax - D.color.vmin);
  f = Math.max(0, Math.min(1, f));
  const x = f * (RGB.length - 1), i = Math.min(RGB.length - 2, Math.floor(x)), r = x - i;
  const c = [0, 1, 2].map(k => Math.round(RGB[i][k] + (RGB[i + 1][k] - RGB[i][k]) * r));
  return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")";
}
function esc(s) {
  return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

/* Farblegende unten rechts */
{
  const el = document.getElementById("legend");
  if (COL === "none") {
    el.innerHTML = T.map(t =>
      '<div><span style="display:inline-block;width:10px;height:10px;background:'
      + t.color + ';margin-right:5px"></span>' + esc(t.title) + "</div>").join("");
  } else {
    const steps = [];
    for (let i = 0; i <= 20; i++) {
      steps.push(rampColor(D.color.vmin + (D.color.vmax - D.color.vmin) * i / 20));
    }
    el.innerHTML = "<div>" + esc(D.color.label) + " (" + esc(D.color.unit) + ")</div>"
      + '<div class="bar" style="background:linear-gradient(to right,' + steps.join(",") + ')"></div>'
      + '<div class="scale"><span>' + D.color.vmin + "</span><span>" + D.color.vmax + "</span></div>";
  }
}

/* ---------------------------------------------------------------------
   4. Karte (Leaflet, Canvas-Renderer).
      Die Track-Linie wird in Abschnitte zerlegt, die jeweils EINE Farbe
      bekommen - so entsteht der Farbverlauf entlang der Strecke, ohne auf
      GPU-Verläufe angewiesen zu sein. 240 Abschnitte je Track sind optisch
      nicht mehr von einem echten Verlauf zu unterscheiden.
   --------------------------------------------------------------------- */
const map = L.map("map", { preferCanvas: true, zoomControl: true });
const renderer = L.canvas({ padding: 0.3 });
L.tileLayer(D.basemap.tiles, {
  maxZoom: D.basemap.maxzoom, maxNativeZoom: D.basemap.maxzoom,
  attribution: D.basemap.attribution,
}).addTo(map);
L.control.scale({ imperial: false }).addTo(map);
map.on("tileerror", () => showError(
  "Kartenkacheln konnten nicht geladen werden (" + D.basemap.tiles + ")."));

const allBounds = L.latLngBounds([D.bounds[0][1], D.bounds[0][0]],
                                 [D.bounds[1][1], D.bounds[1][0]]);
map.fitBounds(allBounds, { padding: [20, 20] });

T.forEach(t => {
  const n = t.lat.length;
  const chunks = Math.max(1, Math.min(240, n - 1));
  for (let s = 0; s < chunks; s++) {
    const a = Math.round(s * (n - 1) / chunks);
    const b = Math.round((s + 1) * (n - 1) / chunks);
    const pts = [];
    for (let i = a; i <= b; i++) pts.push([t.lat[i], t.lon[i]]);
    if (pts.length < 2) continue;
    const mid = Math.round((a + b) / 2);
    L.polyline(pts, {
      color: COL === "none" ? t.color : rampColor(attrOf(t, mid)),
      weight: 4, opacity: 0.95, renderer: renderer, interactive: false,
    }).addTo(map);
  }
  /* Start-/Endpunkt wie auf der Folium-Seite (grün/rot). */
  L.circleMarker([t.lat[0], t.lon[0]], { radius: 7, weight: 2, color: "#fff",
    fillColor: "#00a000", fillOpacity: 1, renderer: renderer })
    .bindTooltip("Start: " + esc(t.title)).addTo(map);
  L.circleMarker([t.lat[n - 1], t.lon[n - 1]], { radius: 7, weight: 2, color: "#fff",
    fillColor: "#d00000", fillOpacity: 1, renderer: renderer })
    .bindTooltip("Ende: " + esc(t.title)).addTo(map);
  /* Unterteilungspunkte des Planungsmodus (orange, wie auf der Seite
     "Karte"). Gesetzt werden sie dort - hier werden sie nur angezeigt. */
  (t.splits || []).forEach(s => {
    L.circleMarker([t.lat[s.i], t.lon[s.i]], { radius: 8, weight: 2, color: "#fff",
      fillColor: "#ff8c00", fillOpacity: 1, renderer: renderer })
      .bindTooltip("Trennpunkt " + s.n + ": " + (t.km[s.i] || 0).toFixed(2) + " km").addTo(map);
  });
  /* Info-Punkte (blau): eigene Koordinaten, können neben dem Track liegen.
     Angelegt und bearbeitet werden sie auf der Seite "Karte"; hier werden
     sie - wie die Trennpunkte - nur angezeigt. */
  (t.notes || []).forEach(nt => {
    L.circleMarker([nt.lat, nt.lon], { radius: 8, weight: 2, color: "#fff",
      fillColor: "#1E88E5", fillOpacity: 1, renderer: renderer })
      .bindTooltip("📍 " + esc(nt.title) + (nt.text ? "<br>" + esc(nt.text) : ""))
      .addTo(map);
  });
});

/* Hover-Marker: folgt dem Profil bzw. der Maus. */
const pin = L.circleMarker([LAT[0], LON[0]], {
  radius: 8, weight: 3, color: "#fff", fillColor: "#ff2d55",
  opacity: 0, fillOpacity: 0, renderer: renderer, interactive: false,
}).addTo(map);
function pinVisible(on) { pin.setStyle({ opacity: on ? 1 : 0, fillOpacity: on ? 1 : 0 }); }

/* ---------------------------------------------------------------------
   5. Gitter-Index für "nächstgelegener Trackpunkt zur Maus".
      Ein linearer Durchlauf über alle Punkte wäre bei jedem mousemove zu
      teuer; stattdessen werden die Punkte in Zellen von ~0,002° (~200 m)
      einsortiert und nur die Nachbarzellen durchsucht.
   --------------------------------------------------------------------- */
const CELL = 0.002, grid = new Map();
const key = (a, b) => a + ":" + b;
for (let g = 0; g < N; g++) {
  const k = key(Math.floor(LAT[g] / CELL), Math.floor(LON[g] / CELL));
  let bucket = grid.get(k);
  if (!bucket) grid.set(k, bucket = []);
  bucket.push(g);
}
function nearest(lat, lon) {
  const cy = Math.floor(lat / CELL), cx = Math.floor(lon / CELL);
  const kx = Math.cos(lat * Math.PI / 180);
  let best = -1, bestD = Infinity;
  for (let r = 1; r <= 4 && best < 0; r++) {
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        if (r > 1 && Math.max(Math.abs(dy), Math.abs(dx)) < r) continue;
        const bucket = grid.get(key(cy + dy, cx + dx));
        if (!bucket) continue;
        for (const g of bucket) {
          const a = (LAT[g] - lat), b = (LON[g] - lon) * kx, d = a * a + b * b;
          if (d < bestD) { bestD = d; best = g; }
        }
      }
    }
  }
  return best;
}

/* ---------------------------------------------------------------------
   6. Höhenprofil (uPlot). Eine gemeinsame x-Achse (km über alle Tracks),
      je Track eine Serie - außerhalb des eigenen Abschnitts mit null
      gefüllt, damit die Kurven nicht ineinander laufen.
   --------------------------------------------------------------------- */
const xs = Array.from(XS);
const series = T.map((t, ti) => {
  const y = new Array(N).fill(null);
  for (let i = 0; i < t.ele.length; i++) y[offset[ti] + i] = t.ele[i];
  return y;
});

/* Die Profilkurve bekommt denselben Farbverlauf wie die Kartenlinie: ein
   horizontaler Canvas-Verlauf entlang der x-Achse. Da x die Distanz ist,
   entsprechen sich Kartenposition und Profilfarbe exakt. */
function gradientFor(ti, alpha) {
  return (u) => {
    const t = T[ti];
    if (COL === "none") return alpha === null ? t.color : "rgba(0,0,0,0)";
    const x0 = u.bbox.left, x1 = u.bbox.left + u.bbox.width;
    if (!(x1 > x0)) return rampColor(attrOf(t, 0), alpha);
    const grad = u.ctx.createLinearGradient(x0, 0, x1, 0);
    const n = t.km.length, steps = Math.min(64, n);
    let last = -1;
    for (let s = 0; s < steps; s++) {
      const i = Math.round(s * (n - 1) / (steps - 1 || 1));
      let p = (u.valToPos(t.km[i], "x", true) - x0) / (x1 - x0);
      p = Math.max(0, Math.min(1, p));
      if (p <= last) continue;
      last = p;
      grad.addColorStop(p, rampColor(attrOf(t, i), alpha));
    }
    if (last < 0) return rampColor(attrOf(t, 0), alpha);
    return grad;
  };
}

const fmt = (v, d, unit) =>
  (v === null || v === undefined) ? "–" : v.toFixed(d) + (unit ? " " + unit : "");
function fmtDur(sec) {
  if (sec === null || sec === undefined) return "–";
  const s = Math.round(sec);
  return String(Math.floor(s / 3600)).padStart(2, "0") + ":"
       + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + ":"
       + String(s % 60).padStart(2, "0");
}

const readout = document.getElementById("readout");
readout.innerHTML = '<span class="hint">' + T.length + " Track(s), " + N
  + " Punkte – Maus über Karte oder Profil bewegen …</span>";
function showPoint(g) {
  if (g < 0) return;
  const t = T[owner[g]], i = local[g];
  pinVisible(true);
  pin.setLatLng([t.lat[i], t.lon[i]]);
  readout.innerHTML =
    '<span class="title">' + esc(t.title) + "</span>"
    + '<span><span class="k">km</span> <span class="v">' + fmt(t.km[i], 2, "") + "</span></span>"
    + '<span><span class="k">Höhe</span> <span class="v">' + fmt(t.ele[i], 0, "m") + "</span></span>"
    + '<span><span class="k">Tempo</span> <span class="v">' + fmt(t.spd[i], 1, "km/h") + "</span></span>"
    + '<span><span class="k">Steigung</span> <span class="v">' + fmt(t.slope[i], 1, "%") + "</span></span>"
    + '<span><span class="k">Zeit</span> <span class="v">' + fmtDur(t.sec[i]) + "</span></span>";
}

/* ---------------------------------------------------------------------
   6b. Marker IM HÖHENPROFIL: Start (grün), Ende (rot) und - sofern im
       Planungsmodus gesetzt - die Unterteilungspunkte (orange, nummeriert).
       Damit zeigen Karte und Profil dieselben Punkte; auf der Karte sind
       es Leaflet-Marker (s.o.), im Profil wird direkt auf das
       uPlot-Canvas gezeichnet (draw-Hook). Eine eigene uPlot-Serie wäre
       teurer: sie bräuchte ein weiteres Array der Länge N je Marker-Sorte.
   --------------------------------------------------------------------- */
const PMARKS = [];
T.forEach(t => {
  const n = t.km.length;
  PMARKS.push({ x: t.km[0], y: t.ele[0], c: "#00a000", lab: "S" });
  PMARKS.push({ x: t.km[n - 1], y: t.ele[n - 1], c: "#d00000", lab: "Z" });
  (t.splits || []).forEach(s =>
    PMARKS.push({ x: t.km[s.i], y: t.ele[s.i], c: "#ff8c00", lab: String(s.n) }));
  /* Info-Punkte an der Kilometer-Stelle ihres nächstgelegenen Trackpunkts
     (blau, Beschriftung "i") - dieselben Punkte wie auf der Karte. */
  (t.notes || []).forEach(nt =>
    PMARKS.push({ x: nt.km, y: nt.ele, c: "#1E88E5", lab: "i" }));
});
function drawMarks(uu) {
  if (!PMARKS.length) return;
  const ctx = uu.ctx, bb = uu.bbox;
  /* Canvas-Pixel je CSS-Pixel: Strichstärken/Radien müssen mitskalieren,
     sonst sind die Marker auf hochauflösenden Bildschirmen zu klein. */
  const pr = (uu.ctx.canvas.height / uu.height) || 1;
  ctx.save();
  ctx.beginPath();
  ctx.rect(bb.left, bb.top, bb.width, bb.height);
  ctx.clip();
  PMARKS.forEach(m => {
    if (m.x === null || m.x === undefined) return;
    const x = uu.valToPos(m.x, "x", true);
    if (x < bb.left - 1 || x > bb.left + bb.width + 1) return;   /* außerhalb des Zooms */
    const yBase = bb.top + bb.height;
    const y = (m.y === null || m.y === undefined) ? yBase : uu.valToPos(m.y, "y", true);
    ctx.setLineDash([4 * pr, 3 * pr]);
    ctx.strokeStyle = m.c;
    ctx.lineWidth = 1.5 * pr;
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, yBase); ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x, y, 5 * pr, 0, 2 * Math.PI);
    ctx.fillStyle = m.c; ctx.fill();
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 2 * pr; ctx.stroke();
    ctx.font = "bold " + (11 * pr) + "px system-ui, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "bottom";
    ctx.lineWidth = 3 * pr; ctx.strokeStyle = "rgba(255,255,255,0.9)";
    ctx.strokeText(m.lab, x, y - 8 * pr);
    ctx.fillStyle = m.c;
    ctx.fillText(m.lab, x, y - 8 * pr);
  });
  ctx.restore();
}

const profileEl = document.getElementById("profile");
const plotWidth = () => Math.max(320, profileEl.clientWidth || 0);
let syncing = false;   /* verhindert Rückkopplung Karte <-> Profil */

const u = new uPlot({
  width: plotWidth(),
  height: Math.max(120, PROFH - 4),
  cursor: { y: false, drag: { x: true, y: false } },
  legend: { show: false },
  scales: { x: { time: false } },
  axes: [{ label: "Distanz (km)", size: 40 }, { label: "Höhe (m)", size: 55 }],
  series: [
    { label: "km" },
    ...T.map((t, ti) => ({
      label: t.title,
      stroke: gradientFor(ti, null),
      fill: gradientFor(ti, 0.15),
      width: 2,
      spanGaps: false,
      points: { show: false },
    })),
  ],
  hooks: {
    /* Nach dem Zeichnen der Kurven: Start-/End-/Trennpunkt-Marker
       obendrauf (siehe drawMarks). */
    draw: [uu => drawMarks(uu)],
    /* Hover im Profil -> Marker auf der Karte */
    setCursor: [uu => {
      const idx = uu.cursor.idx;
      if (idx === null || idx === undefined) return;
      if (!syncing) showPoint(idx);
    }],
    /* Zoom im Profil (Ziehen) -> Karte auf denselben Abschnitt */
    setScale: [(uu, k) => {
      if (k !== "x") return;
      const lo = uu.scales.x.min, hi = uu.scales.x.max;
      const pts = [];
      for (let g = 0; g < N; g++) if (XS[g] >= lo && XS[g] <= hi) pts.push([LAT[g], LON[g]]);
      if (pts.length > 1) map.fitBounds(L.latLngBounds(pts), { padding: [30, 30] });
      else map.fitBounds(allBounds, { padding: [20, 20] });
    }],
  },
}, [xs, ...series], profileEl);

/* Klick ins Profil -> Karte auf den Punkt zentrieren */
u.over.addEventListener("click", () => {
  const idx = u.cursor.idx;
  if (idx === null || idx === undefined) return;
  map.setView([LAT[idx], LON[idx]], Math.max(map.getZoom(), 14));
});

/* Hover auf der Karte -> Cursor im Profil */
map.on("mousemove", e => {
  const g = nearest(e.latlng.lat, e.latlng.lng);
  if (g < 0) return;
  showPoint(g);
  syncing = true;
  const ele = T[owner[g]].ele[local[g]];
  u.setCursor({ left: u.valToPos(XS[g], "x"),
                top: u.valToPos(ele === null ? u.scales.y.min : ele, "y") });
  syncing = false;
});
map.on("mouseout", () => pinVisible(false));

/* ---------------------------------------------------------------------
   6c. Rechtsklick auf die Karte -> Overlay "📍 Punkt speichern?".
       Die Komponente kann selbst nichts an Streamlit zurückmelden (siehe
       Moduldocstring); stattdessen wird die Eingabe per postMessage an
       das übergeordnete Fenster geschickt, wo eine unsichtbare
       streamlit-javascript-Komponente darauf lauscht und den neuen Punkt
       serverseitig anlegt (siehe map_linked._handle_note_bridge). Neue
       Punkte lassen sich nur anlegen, wenn genau EIN Track ausgewählt
       ist - wie in der Punkteliste links.
   --------------------------------------------------------------------- */
const SINGLE_TRACK_ID = T.length === 1 ? T[0].id : null;
let noteOverlayEl = null;
function closeNoteOverlay() {
  if (noteOverlayEl) { noteOverlayEl.remove(); noteOverlayEl = null; }
}
function openNoteOverlay(latlng, point) {
  closeNoteOverlay();
  const mapEl = document.getElementById("map");
  const rect = mapEl.getBoundingClientRect();
  const box = document.createElement("div");
  box.className = "note-overlay";
  box.style.left = Math.max(4, Math.min(point.x, rect.width - 228)) + "px";
  box.style.top = Math.max(4, Math.min(point.y, rect.height - 40)) + "px";

  if (!SINGLE_TRACK_ID) {
    box.innerHTML =
      '<div class="note-overlay-head">📍 Punkt speichern?</div>'
      + '<div class="note-overlay-hint">Dazu links in der Punkteliste '
      + 'genau einen Track auswählen.</div>'
      + '<div class="note-overlay-actions">'
      + '<button class="note-cancel" type="button">Schließen</button></div>';
    mapEl.appendChild(box);
    noteOverlayEl = box;
    box.querySelector(".note-cancel").addEventListener("click", closeNoteOverlay);
    return;
  }

  box.innerHTML =
    '<div class="note-overlay-head">📍 Punkt speichern?</div>'
    + '<input type="text" class="note-title" maxlength="120" '
    + 'placeholder="Titel (z.B. Hütte, Aussicht)">'
    + '<textarea class="note-text" maxlength="1000" rows="2" '
    + 'placeholder="Beschreibung (optional)"></textarea>'
    + '<div class="note-overlay-actions">'
    + '<button class="note-save" type="button">Speichern</button>'
    + '<button class="note-cancel" type="button">Abbrechen</button></div>';
  mapEl.appendChild(box);
  noteOverlayEl = box;
  const titleInput = box.querySelector(".note-title");
  const textInput = box.querySelector(".note-text");
  const saveBtn = box.querySelector(".note-save");
  titleInput.focus();
  box.querySelector(".note-cancel").addEventListener("click", closeNoteOverlay);
  saveBtn.addEventListener("click", () => {
    saveBtn.disabled = true;
    saveBtn.textContent = "Wird gespeichert …";
    window.parent.postMessage({
      mytrackNote: {
        trackId: SINGLE_TRACK_ID,
        lat: latlng.lat,
        lon: latlng.lng,
        title: titleInput.value,
        text: textInput.value,
      },
    }, "*");
    setTimeout(closeNoteOverlay, 400);
  });
}
map.on("contextmenu", e => openNoteOverlay(e.latlng, e.containerPoint));
map.on("movestart zoomstart dragstart", closeNoteOverlay);

/* "Alles zeigen": Profil-Zoom und Kartenausschnitt zurücksetzen */
document.getElementById("reset").addEventListener("click", () => {
  u.setScale("x", { min: xs[0], max: xs[N - 1] });
  map.fitBounds(allBounds, { padding: [20, 20] });
});

/* Breitenänderung des Browserfensters: uPlot und Karte neu vermessen */
let lastW = plotWidth();
new ResizeObserver(() => {
  const w = plotWidth();
  if (w !== lastW) {
    lastW = w;
    u.setSize({ width: w, height: Math.max(120, PROFH - 4) });
  }
  map.invalidateSize();
}).observe(document.getElementById("wrap"));

/* Leaflet vermisst sich beim Aufbau im iframe gelegentlich zu früh. */
setTimeout(() => map.invalidateSize(), 200);
}
</script>
"""


def _component_html(payload: dict) -> str:
    """
    Setzt die HTML/JS-Vorlage mit den konkreten Daten und CDN-Adressen
    zusammen.

    '</' im JSON wird maskiert: Enthielte ein Track-Titel die Zeichenfolge
    "</script>", würde der Browser den Skriptblock sonst mittendrin
    beenden (klassische XSS-/Kaputt-Rendering-Falle bei JSON in <script>).
    """
    payload = dict(payload)
    payload["cdn"] = {
        "leafletJs": _CDN_LEAFLET_JS,
        "leafletCss": _CDN_LEAFLET_CSS,
        "uplotJs": _CDN_UPLOT_JS,
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (
        _HTML_TEMPLATE
        .replace("__UPLOT_CSS__", _UPLOT_CSS)
        .replace("__PAYLOAD__", data)
    )


def _render_component(html: str, height: int) -> None:
    """
    Bettet das HTML als iframe ein.

    Streamlit ≥ 1.56 hat st.components.v1.html durch st.iframe ersetzt
    (die alte Funktion warnt bei jedem Rerun und fällt später weg).
    st.iframe erkennt selbst, ob ein HTML-Text, eine URL oder ein Pfad
    übergeben wurde - deshalb wird der Text ohne führende Leerzeichen
    übergeben, damit die Erkennung eindeutig auf "<" trifft. Ältere
    Streamlit-Versionen nutzen weiterhin den alten Aufruf.
    """
    html = html.lstrip()
    render_iframe = getattr(st, "iframe", None)
    if render_iframe is not None:
        render_iframe(html, height=height)
    else:
        components.html(html, height=height, scrolling=False)


def render_linked_map_page(settings_container=None) -> None:
    """
    Baut die Seite "Karte (Sync)" auf: Filter in der Seitenleiste,
    Kennzahlen links, synchronisierte Karte + Höhenprofil rechts.

    'settings_container' ist - wie bei render_map_page() - der aufklappbare
    Seitenleisten-Bereich aus app.py, in den die Anzeigeeinstellungen
    gerendert werden.

    Wichtig für das Bedienempfinden: Jeder Streamlit-Rerun (z.B. ein
    geänderter Filter) baut die Komponente neu auf und setzt damit
    Kartenausschnitt und Profil-Zoom zurück. Bleibt das erzeugte HTML
    unverändert, rendert Streamlit den iframe dagegen nicht neu - deshalb
    werden hier nur die tatsächlich benötigten Daten in das Dokument
    geschrieben.
    """
    if settings_container is None:
        settings_container = st.sidebar.expander("⚙️ Einstellungen", expanded=True)

    with settings_container:
        st.selectbox(
            "Einfärben mit",
            options=list(_COLOR_OPTIONS.keys()),
            key="lm_plot_column",
            format_func=lambda x: _COLOR_OPTIONS[x][0],
        )
        st.selectbox(
            "Hintergrundkarte",
            options=list(_BASEMAPS.keys()),
            key="lm_basemap",
            format_func=lambda x: _BASEMAPS[x]["label"],
        )
        st.slider(
            "Spaltenbreite Kennzahlen",
            min_value=10,
            max_value=35,
            value=15,
            key="kpi_col_width_pct",
            help="Breite der Kennzahlen-Spalte gegenüber der Karte rechts daneben.",
        )
        # Dieselbe Höhen-Einstellung wie auf der Seite "Karte" (gemeinsame
        # Widget-Keys, siehe map._render_height_settings).
        _render_height_settings()

    # CSS des Füllmodus - vor dem Hauptbereich, damit die Komponente gleich
    # beim ersten Rendern die richtige Höhe bekommt.
    _render_fill_css()

    # Unterteilungspunkte des Planungsmodus (track_id -> Liste von
    # Punkt-Indizes) - dieselbe Ablage wie auf der Seite "Karte"; hier nur
    # defensiv angelegt, falls diese Seite zuerst aufgerufen wird.
    st.session_state.setdefault("split_points", {})

    # Dieselben Filter wie auf der Seite "Karte" (gemeinsame Widget-Keys,
    # die Auswahl bleibt beim Seitenwechsel also erhalten).
    meta = render_track_filters()
    if meta.empty:
        # Wie auf der Seite "Karte": nur diesen Seitenaufbau beenden (kein
        # st.stop()), damit der "🚪 Beenden"-Knopf aus app.py weiterhin
        # gerendert wird.
        render_no_selection_hint()
        return

    # Ebenso der Planungsmodus-Schalter: gemeinsamer Widget-Key
    # 'planning_mode', der Modus bleibt beim Seitenwechsel also erhalten.
    # Gesetzt werden die Punkte auf der Seite "Karte" (die Komponente hier
    # meldet nichts an Streamlit zurück), angezeigt werden sie auf beiden.
    planning_active = render_planning_toggle(meta["track_id"].tolist())

    # Erst jetzt die (großen) GPX-Binärdaten der ausgewählten Tracks laden.
    file_data = load_track_files(tuple(sorted(meta["track_id"].tolist())))
    df = meta.merge(file_data, on="track_id", how="inner")
    planning_active = planning_active and len(df) == 1

    map_height, profile_height = _resolve_map_profile_height()

    kpi_width_pct = st.session_state.kpi_col_width_pct
    col_kpis, col_map = st.columns([kpi_width_pct, 100 - kpi_width_pct], gap="small")

    # Info-Punkte der ausgewählten Tracks (gecacht): für die Verwaltung
    # links und die Anzeige in der Komponente rechts.
    notes = load_track_notes(tuple(sorted(df["track_id"].tolist())))
    single_track_id = df["track_id"].iloc[0] if len(df) == 1 else None
    single_gdf = (
        process_track(single_track_id, df["file_data"].iloc[0])
        if single_track_id is not None
        else None
    )

    # Rückkanal für "📍 Punkt speichern?" (Rechtsklick auf die Karte, siehe
    # _handle_note_bridge). Löst diese einen neuen Punkt aus, rerunnt sie
    # die Seite selbst - der restliche Seitenaufbau unten läuft dann mit
    # dem neu angelegten Punkt bereits in 'notes' (siehe load_track_notes
    # oben, das cached DataFrame wird beim Rerun neu geladen).
    _handle_note_bridge(single_gdf, single_track_id, planning_active)

    with col_kpis:
        with _keyed_container("mp_kpis", border=True):
            if planning_active:
                # Kennzahlen je Teil, Punkteliste und ZIP-Export - identisch
                # zur Seite "Karte" (process_track ist gecacht, der Aufruf
                # innerhalb von _build_payload kostet also nicht doppelt).
                _render_planning_kpis(
                    single_gdf,
                    single_track_id,
                    df["track_title"].iloc[0],
                    notes=notes,
                )
            else:
                _render_kpis(df)

            # Info-Punkte: dieselbe Verwaltung wie auf der Seite "Karte".
            # 'allow_map_click' (der Checkbox-Umweg über st_folium) bleibt
            # hier aus - stattdessen per Rechtsklick direkt auf der Karte
            # rechts speichern (siehe _handle_note_bridge). Das Formular
            # unten mit den Koordinatenfeldern bleibt als Alternative
            # bestehen, z.B. um bereits angelegte Punkte zu bearbeiten.
            _render_notes_panel(
                notes,
                gdf=single_gdf,
                track_id=single_track_id,
                track_title=df["track_title"].iloc[0] if single_track_id is not None else None,
                planning_mode=planning_active,
                allow_map_click=False,
            )

    with col_map:
        with _keyed_container("mp_box", border=True):
            if single_track_id is not None:
                st.caption(
                    "💡 Rechtsklick auf die Karte speichert einen neuen "
                    "Punkt direkt an dieser Stelle."
                )
            if planning_active:
                st.caption(
                    "📐 Planungsmodus: Die Trennpunkte werden in Karte und "
                    "Höhenprofil angezeigt (orange, nummeriert; Start = S, "
                    "Ende = Z). Gesetzt und gelöscht werden sie auf der Seite "
                    "\"Karte\" oder über das \"✕\" in der Punkteliste links."
                )
            payload = _build_payload(
                df,
                st.session_state.lm_plot_column,
                st.session_state.lm_basemap,
                map_height,
                profile_height,
                split_points=st.session_state.split_points if planning_active else None,
                notes=notes,
                fill=_height_mode() == "fill",
            )
            # +48 px für die Werte-Leiste über der Karte (und ggf. den
            # Fehlerbalken); ohne Aufschlag schneidet der iframe das Profil
            # unten ab. Im Füllmodus ist das nur der Startwert - der
            # iframe wird anschließend per CSS auf die Fensterhöhe
            # gestreckt (Container-Key 'mp_map', siehe map._FILL_CSS).
            with _keyed_container("mp_map"):
                _render_component(
                    _component_html(payload),
                    height=map_height + profile_height + 48,
                )
            if payload["stride"] > 1:
                st.caption(
                    f"Hinweis: Für die Darstellung wurde jeder {payload['stride']}. "
                    "Trackpunkt übertragen (Gesamtauswahl zu groß). Kennzahlen "
                    "links basieren unverändert auf allen Punkten."
                )


# --------------------------------------------------------------------------
# Rückkanal für "📍 Punkt speichern?" (Rechtsklick auf die Karte)
# --------------------------------------------------------------------------
# `st.components.v1.html()` kann selbst keine Werte an Streamlit zurück-
# melden (siehe Moduldocstring). Der Trick: Die Kartenkomponente schickt
# die Eingaben per `window.parent.postMessage(...)`; die folgende, unsicht-
# bare `streamlit-javascript`-Komponente lauscht im selben übergeordneten
# Fenster (`window.parent` ist von beiden Komponenten aus dasselbe Objekt,
# da beide direkte Kind-iframes derselben Streamlit-Seite sind) auf genau
# diese Nachricht und liefert sie als Rückgabewert an Python.
# ACHTUNG (ASI-Falle): streamlit_javascript führt den String über
# `eval("(async () => {return " + js_code + "})()")` aus (siehe dessen
# Quelltext). Begänne diese Zeichenkette mit einem Zeilenumbruch, würde
# JavaScripts automatische Semikolon-Einfügung daraus "return;"
# machen - die Funktion läge sofort mit 'undefined' zurück, OHNE die
# Promise unten je zu erzeugen. Der Listener auf window.parent würde dann
# NIE registriert: postMessage aus der Kartenkomponente hätte keinen
# Empfänger, neue Punkte würden also still und leise verworfen (kein
# Fehler sichtbar, "Speichern" scheint zu funktionieren, der Punkt
# erscheint aber nie auf der Karte). Deshalb hier bewusst EIN
# zusammenhängender String ohne führenden Zeilenumbruch, direkt hinter
# "await".
_NOTE_BRIDGE_JS = (
    "await new Promise((resolve) => {"
    "  function handler(e) {"
    "    if (e && e.data && e.data.mytrackNote) {"
    "      window.parent.removeEventListener('message', handler);"
    "      resolve(JSON.stringify(e.data.mytrackNote));"
    "    }"
    "  }"
    "  window.parent.addEventListener('message', handler);"
    "})"
)


def _handle_note_bridge(
    single_gdf: pd.DataFrame | None, single_track_id, planning_active: bool
) -> None:
    """
    Verarbeitet einen per Rechtsklick-Overlay gespeicherten Punkt (siehe
    _NOTE_BRIDGE_JS oben) und legt ihn als Info-Punkt an.

    `st_javascript` wartet asynchron auf die postMessage-Nachricht und
    liefert bei jedem Rerun bis dahin 0 zurück; erst wenn die Nachricht
    eintrifft, wird der eigentliche Wert geliefert. Damit derselbe Punkt
    danach nicht bei jedem weiteren Rerun erneut gespeichert wird, bekommt
    die Komponente über einen in session_state gezählten Suffix im 'key'
    einen frischen Mount - das startet in der Komponente eine neue,
    NOCH NICHT erfüllte Promise.

    Ein Punkt lässt sich nur anlegen, wenn genau EIN Track ausgewählt ist
    (dieselbe Regel wie im Formular der Punkteliste); die Komponente prüft
    das bereits selbst (siehe SINGLE_TRACK_ID in der HTML-Vorlage), hier
    wird sie zur Sicherheit serverseitig wiederholt.
    """
    gen = st.session_state.get("_lm_note_bridge_gen", 0)
    raw = st_javascript(_NOTE_BRIDGE_JS, key=f"lm_note_bridge_{gen}")
    if not raw:
        return
    try:
        incoming = json.loads(raw)
    except (TypeError, ValueError):
        incoming = None
    # Komponente für den nächsten Punkt neu mounten, sobald diese Nachricht
    # verarbeitet wurde - unabhängig davon, ob sie gültig war.
    st.session_state["_lm_note_bridge_gen"] = gen + 1
    if not incoming or single_gdf is None or single_track_id is None:
        return
    if str(incoming.get("trackId")) != str(single_track_id):
        return
    try:
        lat, lon = float(incoming["lat"]), float(incoming["lon"])
    except (KeyError, TypeError, ValueError):
        return
    point_index = _nearest_point_index(single_gdf, lat, lon)
    insert_track_note(
        single_track_id,
        (incoming.get("title") or "").strip() or "Punkt",
        (incoming.get("text") or "").strip(),
        lat,
        lon,
        float(single_gdf["ele"].iloc[point_index]),
        point_index,
        note_kind="planning" if planning_active else "info",
    )
    st.rerun()


# Direkter Start zu Debug-Zwecken: `streamlit run map_linked.py`.
if __name__ == "__main__":
    st.set_page_config(page_title="Karte (Sync)", layout="wide")
    render_linked_map_page()
