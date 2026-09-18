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

    MapLibre GL JS  (Karte, WebGL, Raster-Kacheln von OpenTopoMap/OSM)
    uPlot           (Höhenprofil, Canvas, sehr schnell bei vielen Punkten)

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

Bewusste Einschränkungen
------------------------
- Die Komponente ist eine Einbahnstraße: Sie meldet nichts an Streamlit
  zurück. Der Planungsmodus (Unterteilungspunkte setzen) bleibt deshalb auf
  der Seite "Karte" (map.py), da er Serverzustand braucht. Für einen
  Rückkanal wäre eine echte bidirektionale Custom Component nötig
  (Frontend-Build) oder `streamlit-javascript`.
- Die JS-Bibliotheken werden von einem CDN geladen (siehe _CDN_*), es wird
  also eine Internetverbindung benötigt. Für den Offline-Betrieb lassen
  sich die vier Dateien lokal ablegen und die Konstanten anpassen.
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

from functions import load_track_files, process_track
from map import (
    _render_kpis,
    _resolve_map_profile_height,
    render_track_filters,
)

# --------------------------------------------------------------------------
# Externe Bibliotheken (CDN, feste Versionen)
# --------------------------------------------------------------------------
# Bewusst auf exakte Versionen gepinnt: Ein stiller Major-Wechsel beim CDN
# würde die Seite sonst ohne eigenes Zutun zerlegen.
_CDN_MAPLIBRE_JS = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"
_CDN_MAPLIBRE_CSS = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"
_CDN_UPLOT_JS = "https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js"
_CDN_UPLOT_CSS = "https://unpkg.com/uplot@1.6.31/dist/uPlot.min.css"

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
    Literal 'NaN' schreibt und uPlot/MapLibre Lücken nur über 'null'
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
        sub = gdf.iloc[idx]

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
    # würde MapLibre sonst kommentarlos eine leere Karte zeigen lassen.
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
        "layout": {"mapHeight": int(map_height), "profileHeight": int(profile_height)},
        "stride": stride,
    }


# --------------------------------------------------------------------------
# HTML/JS-Vorlage der Komponente
# --------------------------------------------------------------------------
# Der Platzhalter __PAYLOAD__ wird in _component_html() durch das JSON
# ersetzt. Bewusst KEIN f-String: die Vorlage enthält jede Menge geschweifte
# Klammern (JS-Blöcke, CSS), die sonst alle verdoppelt werden müssten.
_HTML_TEMPLATE = """
<link rel="stylesheet" href="__CSS_MAPLIBRE__" />
<link rel="stylesheet" href="__CSS_UPLOT__" />
<script src="__JS_MAPLIBRE__"></script>
<script src="__JS_UPLOT__"></script>
<style>
  html, body { margin: 0; padding: 0; font-family: "Source Sans Pro", system-ui, sans-serif; }
  #wrap { position: relative; }
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
  #map { width: 100%; }
  #profile { width: 100%; position: relative; }
  #legend {
    position: absolute; right: 10px; bottom: 10px; z-index: 5;
    background: rgba(255,255,255,0.88); border: 1px solid #ccc; border-radius: 4px;
    padding: 4px 6px; font-size: 11px; color: #333;
  }
  #legend .bar { height: 8px; width: 150px; border: 1px solid #bbb; margin: 2px 0; }
  #legend .scale { display: flex; justify-content: space-between;
                   font-variant-numeric: tabular-nums; }
  button.reset {
    position: absolute; right: 10px; top: 10px; z-index: 5;
    background: #fff; border: 1px solid #ccc; border-radius: 4px;
    padding: 3px 8px; font-size: 12px; cursor: pointer;
  }
  button.reset:hover { background: #f0f0f0; }
  .pin { width: 14px; height: 14px; border-radius: 50%; background: #ff2d55;
         border: 2px solid #fff; box-shadow: 0 0 4px rgba(0,0,0,0.5); }
  .endpoint { width: 12px; height: 12px; border-radius: 50%; border: 2px solid #fff;
              box-shadow: 0 0 3px rgba(0,0,0,0.5); }
  .u-legend { display: none; }  /* eigene Werte-Anzeige oben, siehe #readout */
</style>

<div id="wrap">
  <div id="readout"><span class="hint">Maus über Karte oder Profil bewegen …</span></div>
  <div id="map"></div>
  <div id="profile"><button class="reset" id="reset">Alles zeigen</button></div>
  <div id="legend"></div>
</div>

<script>
const D = __PAYLOAD__;
const T = D.tracks;
const MAPH = D.layout.mapHeight, PROFH = D.layout.profileHeight;
document.getElementById("map").style.height = MAPH + "px";
document.getElementById("profile").style.height = PROFH + "px";

/* ---------------------------------------------------------------------
   1. Flache Indizes: Alle Tracks werden zu EINER Punktfolge verkettet.
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

/* Werte der Farb-Spalte je Punkt (oder null im Modus "Nichts"). */
const COL = D.color.column;
function attrOf(t, i) {
  if (COL === "ele") return t.ele[i];
  if (COL === "km_per_h") return t.spd[i];
  if (COL === "slope") return t.slope[i];
  return null;
}

/* ---------------------------------------------------------------------
   2. Farbskala (identisch zur Folium-Seite): linear zwischen sieben
      Stützfarben von blau nach rot.
   --------------------------------------------------------------------- */
const RGB = D.color.ramp.map(h => [
  parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16),
]);
function rampColor(v, alpha) {
  if (v === null || v === undefined || Number.isNaN(v)) return "rgba(130,130,130," + (alpha ?? 1) + ")";
  let f = (v - D.color.vmin) / (D.color.vmax - D.color.vmin);
  f = Math.max(0, Math.min(1, f));
  const x = f * (RGB.length - 1), i = Math.min(RGB.length - 2, Math.floor(x)), r = x - i;
  const c = [0, 1, 2].map(k => Math.round(RGB[i][k] + (RGB[i + 1][k] - RGB[i][k]) * r));
  return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + (alpha ?? 1) + ")";
}

/* Farblegende unten rechts (nur wenn nach einem Wert eingefärbt wird). */
{
  const el = document.getElementById("legend");
  if (COL === "none") {
    el.innerHTML = T.map(t =>
      '<div><span style="display:inline-block;width:10px;height:10px;background:'
      + t.color + ';margin-right:5px"></span>' + esc(t.title) + "</div>").join("");
  } else {
    const steps = [];
    for (let i = 0; i <= 20; i++) steps.push(rampColor(D.color.vmin + (D.color.vmax - D.color.vmin) * i / 20));
    el.innerHTML = "<div>" + D.color.label + " (" + D.color.unit + ")</div>"
      + '<div class="bar" style="background:linear-gradient(to right,' + steps.join(",") + ')"></div>'
      + '<div class="scale"><span>' + D.color.vmin + "</span><span>" + D.color.vmax + "</span></div>";
  }
}
function esc(s) { return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

/* ---------------------------------------------------------------------
   3. Karte (MapLibre GL). Raster-Kacheln als Style, eine Linien-Ebene je
      Track. Die Einfärbung entlang der Linie erfolgt über 'line-gradient'
      (benötigt lineMetrics: true an der Quelle) - dieselbe Idee wie
      folium.ColorLine, aber vom Browser interpoliert.
   --------------------------------------------------------------------- */
const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: { base: { type: "raster", tiles: [D.basemap.tiles], tileSize: 256,
                       maxzoom: D.basemap.maxzoom, attribution: D.basemap.attribution } },
    layers: [{ id: "base", type: "raster", source: "base" }],
  },
  bounds: D.bounds, fitBoundsOptions: { padding: 30 }, attributionControl: true,
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }));
map.dragRotate.disable();
map.touchZoomRotate.disableRotation();

/* Hover-Marker (folgt Profil bzw. Maus) */
const pinEl = document.createElement("div");
pinEl.className = "pin";
const pin = new maplibregl.Marker({ element: pinEl })
  .setLngLat([LON[0], LAT[0]]).addTo(map);
pinEl.style.display = "none";

map.on("load", () => {
  T.forEach((t, ti) => {
    const coords = [];
    for (let i = 0; i < t.lon.length; i++) coords.push([t.lon[i], t.lat[i]]);
    map.addSource("trk" + ti, {
      type: "geojson", lineMetrics: true,
      data: { type: "Feature", geometry: { type: "LineString", coordinates: coords } },
    });

    const paint = { "line-width": 4, "line-opacity": 0.95 };
    if (COL === "none") {
      paint["line-color"] = t.color;
    } else {
      /* line-gradient erwartet streng steigende Stützstellen in [0,1].
         Auf höchstens 120 Stufen reduziert - mehr ist optisch nicht
         unterscheidbar, macht den Ausdruck aber unnötig groß. */
      const n = t.lon.length, steps = Math.min(120, n);
      const stops = [];
      for (let s = 0; s < steps; s++) {
        const i = Math.round(s * (n - 1) / (steps - 1 || 1));
        const p = Math.min(0.999999, Math.max(0, s / (steps - 1 || 1)));
        if (s > 0 && p <= stops[stops.length - 2]) continue;
        stops.push(p, rampColor(attrOf(t, i)));
      }
      /* line-color wird hier bewusst NICHT zusätzlich gesetzt: MapLibre
         wertet bei gesetztem line-gradient ohnehin nur den Verlauf aus. */
      paint["line-gradient"] = ["interpolate", ["linear"], ["line-progress"]].concat(stops);
    }
    map.addLayer({ id: "trk" + ti, type: "line", source: "trk" + ti,
                   layout: { "line-cap": "round", "line-join": "round" }, paint });

    /* Start-/Endpunkt wie auf der Folium-Seite (grün/rot). */
    [["#00a000", 0], ["#d00000", t.lon.length - 1]].forEach(([c, i]) => {
      const el = document.createElement("div");
      el.className = "endpoint"; el.style.background = c;
      new maplibregl.Marker({ element: el }).setLngLat([t.lon[i], t.lat[i]]).addTo(map);
    });
  });
});

/* ---------------------------------------------------------------------
   4. Gitter-Index für "nächstgelegener Trackpunkt zur Maus".
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
  for (let r = 1; r <= 4 && best < 0; r++) {        /* Radius erweitern, bis etwas gefunden wird */
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        if (r > 1 && Math.max(Math.abs(dy), Math.abs(dx)) < r) continue;  /* innere Ringe schon geprüft */
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
   5. Höhenprofil (uPlot). Eine gemeinsame x-Achse (km über alle Tracks),
      je Track eine Serie - außerhalb des eigenen Abschnitts mit null
      gefüllt, damit die Kurven nicht ineinander laufen.
   --------------------------------------------------------------------- */
const xs = Array.from(XS);
const series = T.map((t, ti) => {
  const y = new Array(N).fill(null);
  for (let i = 0; i < t.ele.length; i++) y[offset[ti] + i] = t.ele[i];
  return y;
});

/* Die Profilkurve wird mit demselben Farbverlauf gezeichnet wie die
   Kartenlinie: ein horizontaler Canvas-Verlauf entlang der x-Achse - da x
   die Distanz ist, entsprechen sich Kartenposition und Profilfarbe exakt. */
function gradientFor(ti, alpha) {
  return (u) => {
    const t = T[ti];
    if (COL === "none") return alpha ? "rgba(0,0,0,0)" : t.color;
    const ctx = u.ctx;
    const x0 = u.bbox.left, x1 = u.bbox.left + u.bbox.width;
    const grad = ctx.createLinearGradient(x0, 0, x1, 0);
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
    return grad;
  };
}

const fmt = (v, d, u) => (v === null || v === undefined) ? "–" : v.toFixed(d) + " " + u;
function fmtDur(sec) {
  if (sec === null || sec === undefined) return "–";
  const s = Math.round(sec);
  return String(Math.floor(s / 3600)).padStart(2, "0") + ":"
       + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + ":"
       + String(s % 60).padStart(2, "0");
}

const readout = document.getElementById("readout");
function showPoint(g) {
  if (g < 0) return;
  const t = T[owner[g]], i = local[g];
  pinEl.style.display = "block";
  pin.setLngLat([t.lon[i], t.lat[i]]);
  readout.innerHTML =
    '<span class="title">' + esc(t.title) + "</span>"
    + '<span><span class="k">km</span> <span class="v">' + fmt(t.km[i], 2, "") + "</span></span>"
    + '<span><span class="k">Höhe</span> <span class="v">' + fmt(t.ele[i], 0, "m") + "</span></span>"
    + '<span><span class="k">Tempo</span> <span class="v">' + fmt(t.spd[i], 1, "km/h") + "</span></span>"
    + '<span><span class="k">Steigung</span> <span class="v">' + fmt(t.slope[i], 1, "%") + "</span></span>"
    + '<span><span class="k">Zeit</span> <span class="v">' + fmtDur(t.sec[i]) + "</span></span>";
}

let syncing = false;   /* verhindert Rückkopplung Karte <-> Profil */

const opts = {
  width: document.getElementById("profile").clientWidth,
  height: PROFH - 4,
  cursor: {
    y: false,
    drag: { x: true, y: false },
    points: { show: true, size: 9 },
  },
  legend: { show: false },
  scales: { x: { time: false } },
  axes: [
    { label: "Distanz (km)", size: 40 },
    { label: "Höhe (m)", size: 55 },
  ],
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
    /* Hover im Profil -> Marker auf der Karte */
    setCursor: [u => {
      const idx = u.cursor.idx;
      if (idx === null || idx === undefined) return;
      if (!syncing) showPoint(idx);
    }],
    /* Zoom im Profil (Ziehen) -> Karte auf denselben Abschnitt */
    setScale: [(u, k) => {
      if (k !== "x") return;
      const lo = u.scales.x.min, hi = u.scales.x.max;
      let w = 181, s = 91, e = -181, n = -91, found = false;
      for (let g = 0; g < N; g++) {
        if (XS[g] < lo || XS[g] > hi) continue;
        found = true;
        w = Math.min(w, LON[g]); e = Math.max(e, LON[g]);
        s = Math.min(s, LAT[g]); n = Math.max(n, LAT[g]);
      }
      if (found) map.fitBounds([[w, s], [e, n]], { padding: 40, duration: 500 });
    }],
  },
};

const u = new uPlot(opts, [xs, ...series], document.getElementById("profile"));

/* Klick ins Profil -> Karte auf den Punkt zentrieren */
u.over.addEventListener("click", () => {
  const idx = u.cursor.idx;
  if (idx === null || idx === undefined) return;
  map.easeTo({ center: [LON[idx], LAT[idx]], zoom: Math.max(map.getZoom(), 14), duration: 400 });
});

/* Hover auf der Karte -> Cursor im Profil */
map.on("mousemove", e => {
  const g = nearest(e.lngLat.lat, e.lngLat.lng);
  if (g < 0) return;
  showPoint(g);
  syncing = true;
  u.setCursor({ left: u.valToPos(XS[g], "x"), top: u.valToPos(T[owner[g]].ele[local[g]] ?? 0, "y") });
  syncing = false;
});
map.on("mouseout", () => { pinEl.style.display = "none"; });

/* "Alles zeigen": Profil-Zoom und Kartenausschnitt zurücksetzen */
document.getElementById("reset").addEventListener("click", () => {
  u.setScale("x", { min: xs[0], max: xs[N - 1] });
  map.fitBounds(D.bounds, { padding: 30, duration: 500 });
});

/* Breitenänderung des Browserfensters: uPlot neu vermessen */
new ResizeObserver(() => {
  u.setSize({ width: document.getElementById("profile").clientWidth, height: PROFH - 4 });
  map.resize();
}).observe(document.getElementById("wrap"));
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
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (
        _HTML_TEMPLATE
        .replace("__CSS_MAPLIBRE__", _CDN_MAPLIBRE_CSS)
        .replace("__CSS_UPLOT__", _CDN_UPLOT_CSS)
        .replace("__JS_MAPLIBRE__", _CDN_MAPLIBRE_JS)
        .replace("__JS_UPLOT__", _CDN_UPLOT_JS)
        .replace("__PAYLOAD__", data)
    )


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
        st.radio(
            "Höhe Karte + Profil",
            options=["window", "manual"],
            index=0,
            key="map_profile_height_mode",
            format_func=lambda x: "An Fensterhöhe anpassen" if x == "window" else "Manuell",
            help=(
                "'An Fensterhöhe anpassen' liest die Browser-Fensterhöhe per "
                "JavaScript aus und passt Karte + Profil entsprechend an."
            ),
        )
        if st.session_state.map_profile_height_mode == "manual":
            st.slider(
                "Höhe Karte + Profil (px)",
                min_value=450,
                max_value=2200,
                step=100,
                value=1100,
                key="map_profile_total_height_px",
                help="Gesamthöhe von Karte und Höhenprofil zusammen, in Pixeln.",
            )

    # Dieselben Filter wie auf der Seite "Karte" (gemeinsame Widget-Keys,
    # die Auswahl bleibt beim Seitenwechsel also erhalten).
    meta = render_track_filters()

    # Erst jetzt die (großen) GPX-Binärdaten der ausgewählten Tracks laden.
    file_data = load_track_files(tuple(sorted(meta["track_id"].tolist())))
    df = meta.merge(file_data, on="track_id", how="inner")

    map_height, profile_height = _resolve_map_profile_height()

    kpi_width_pct = st.session_state.kpi_col_width_pct
    col_kpis, col_map = st.columns([kpi_width_pct, 100 - kpi_width_pct], gap="small")

    with col_kpis:
        with st.container(border=True):
            _render_kpis(df)

    with col_map:
        with st.container(border=True):
            payload = _build_payload(
                df,
                st.session_state.lm_plot_column,
                st.session_state.lm_basemap,
                map_height,
                profile_height,
            )
            components.html(
                _component_html(payload),
                # +40 px für die Werte-Leiste über der Karte; ohne Aufschlag
                # schneidet der iframe das Profil unten ab.
                height=map_height + profile_height + 40,
                scrolling=False,
            )
            if payload["stride"] > 1:
                st.caption(
                    f"Hinweis: Für die Darstellung wurde jeder {payload['stride']}. "
                    "Trackpunkt übertragen (Gesamtauswahl zu groß). Kennzahlen "
                    "links basieren unverändert auf allen Punkten."
                )


# Direkter Start zu Debug-Zwecken: `streamlit run map_linked.py`.
if __name__ == "__main__":
    st.set_page_config(page_title="Karte (Sync)", layout="wide")
    render_linked_map_page()
