"""
Interaktive Karte für aufgezeichnete GPS-Tracks (Streamlit-App).

Aufbau der Seite:
1. Sidebar: Filter nach Sport / Tour / Track sowie Auswahl der Farb-Spalte
   für das Höhenprofil (Höhe, Geschwindigkeit, Gefälle oder einfarbig).
2. Hauptbereich: Eine Folium-Karte mit allen ausgewählten Tracks (farblich
   entlang der gewählten Spalte eingefärbt) und darunter ein gemeinsames
   Höhenprofil (Plotly) über alle Tracks hinweg.
3. Klick-Interaktion: Klickt man im Höhenprofil auf einen Punkt, wird dieser
   Punkt im Profil größer dargestellt UND als zusätzlicher Marker auf der
   Karte eingezeichnet, auf den die Karte zentriert wird.

Performance-Hinweis: GPX-Dateien werden aus DuckDB geladen und mit GeoPandas
aufwendig nachbearbeitet (Distanz, Tempo, Steigung, ...). Da Streamlit bei
JEDER Nutzerinteraktion das komplette Skript neu ausführt (auch bei einem
einfachen Klick im Profil), wird diese Verarbeitung über st.cache_data
gecacht - siehe process_track().
"""

import streamlit as st  # Web-UI-Framework
import duckdb  # Datenhaltung (lokale Analytics-DB)
import io  # GPX-Bytes als Datei-ähnliches Objekt einlesen
import geopandas as gpd  # Geodaten (Punkte, CRS-Transformationen, Distanzen)
import numpy as np  # Numerische Hilfsfunktionen (Arrays, NaN-Handling)
import folium  # Erzeugt die interaktive Leaflet-Karte
from streamlit_folium import st_folium  # Rendert eine Folium-Karte in Streamlit
import branca.colormap as cm  # Farbskala für die Karten-Einfärbung
import plotly.graph_objects as go  # Höhenprofil-Diagramm
import pandas as pd

st.set_page_config(page_title="Interactive Map", layout="wide")

DB_PATH = ".data/tracks.duckdb"


# --------------------------------------------------------------------------
# Datenzugriff (gecacht)
# --------------------------------------------------------------------------
@st.cache_resource
def get_connection():
    """
    Öffnet die DuckDB-Verbindung genau EINMAL pro App-Prozess.

    @st.cache_resource ist für "lebende" Objekte gedacht (DB-Verbindungen,
    ML-Modelle, ...), die nicht bei jedem Rerun neu erzeugt werden sollen.
    Ohne diesen Cache würde die Verbindung bei jedem Skriptdurchlauf (also
    bei jedem Klick) neu geöffnet werden.
    """
    return duckdb.connect(database=str(DB_PATH))


con = get_connection()


@st.cache_data(show_spinner=False, ttl=60)
def load_metadata() -> pd.DataFrame:
    """
    Lädt nur die "leichten" Metadaten aller Tracks (Titel, Bounding-Box,
    Min/Max-Werte für Tempo/Höhe/Gefälle, ...) - bewusst OHNE die teils
    großen GPX-Binärdaten (file_data). Diese Metadaten werden für die
    Sidebar-Filter (Sport/Tour/Track-Auswahl) gebraucht.

    ttl=60: nach 60 Sekunden wird neu aus der DB gelesen, falls z.B.
    zwischenzeitlich neue Tracks aufgezeichnet wurden.
    """
    return con.sql("""
        SELECT
            tours.tour_id, tours.tour_title,
            gpx.track_id, gpx.track_title, gpx.time_start,
            gpx.location_lat_min, gpx.location_lat_max,
            gpx.location_lon_min, gpx.location_lon_max,
            gpx.speed_min, gpx.speed_max,
            gpx.elevation_min, gpx.elevation_max,
            gpx.slope_min, gpx.slope_max,
            gpx.sport_id, sport.sport_title
        FROM tours
        LEFT JOIN gpx ON tours.tour_id = gpx.tour_id
        LEFT JOIN sport ON gpx.sport_id = sport.sport_id
        ORDER BY gpx.time_start ASC
        """).fetchdf()


@st.cache_data(show_spinner=False, ttl=60)
def load_track_files(track_ids: tuple) -> pd.DataFrame:
    """
    Lädt die GPX-Binärdaten NUR für die übergebenen track_ids.

    Wird erst aufgerufen, nachdem die Sidebar-Filter feststehen, damit nicht
    bei jedem Rerun die (potenziell großen) GPX-Dateien aller Tracks aus der
    gesamten Datenbank übertragen werden müssen.
    """
    if not track_ids:
        return pd.DataFrame(columns=["track_id", "file_data"])

    # Platzhalter ("?, ?, ?, ...") statt String-Interpolation -> verhindert
    # SQL-Injection und funktioniert unabhängig von der Anzahl der IDs.
    placeholders = ",".join(["?"] * len(track_ids))
    query = f"SELECT track_id, file_data FROM gpx WHERE track_id IN ({placeholders})"
    return con.execute(query, list(track_ids)).fetchdf()


@st.cache_data(show_spinner="GPX-Daten werden verarbeitet …")
def process_track(track_id, _gpx_file: bytes) -> pd.DataFrame:
    """
    Liest eine einzelne GPX-Datei ein und berechnet alle abgeleiteten Werte
    pro Trackpunkt: zurückgelegte Distanz, Geschwindigkeit, Steigung,
    Auf-/Abstieg sowie vergangene Zeit.

    Caching-Strategie: Das Ergebnis wird pro track_id gecacht. Der
    Parametername "_gpx_file" beginnt absichtlich mit "_" - Streamlit
    schließt unterstrich-Parameter vom Hashing für den Cache-Key aus.
    Dadurch muss die (oft mehrere hundert KB große) Byte-Folge NICHT bei
    jedem Aufruf gehasht werden; die kleine, schnell zu hashende track_id
    reicht als eindeutiger Schlüssel.

    Effekt: Ein Klick im Höhenprofil (-> st.rerun()) oder ein Wechsel der
    Farb-Spalte verursacht KEINE erneute, teure GPX-/GeoPandas-Verarbeitung
    mehr, solange dieselben Tracks ausgewählt bleiben.
    """
    # GPX-Track als Punktfolge einlesen (eine Zeile pro aufgezeichnetem Punkt)
    gdf = gpd.read_file(io.BytesIO(_gpx_file), layer="track_points")
    gdf.crs = "EPSG:4326"  # GPX liefert WGS84 (Grad-Koordinaten)

    # lat/lon als eigene Spalten sichern, BEVOR die Geometrie unten in ein
    # metrisches Koordinatensystem umprojiziert wird - diese Werte werden
    # später für Karten-Marker und Klick-Auflösung gebraucht.
    gdf["lon"] = gdf.geometry.x
    gdf["lat"] = gdf.geometry.y

    # Umprojizieren ins passende UTM-System (lokal meter-genau), damit
    # geometrische Distanzen direkt in Metern berechnet werden können -
    # in Grad-Koordinaten (EPSG:4326) wären Distanzen nicht maßstabsgetreu.
    gdf.crs = "EPSG:4326"
    gdf = gdf.to_crs(gdf.estimate_utm_crs())

    # Jede Zeile mit ihrem direkten Vorgänger vergleichen, um Zeit- und
    # Distanz-Differenzen zwischen aufeinanderfolgenden Punkten zu berechnen.
    shifted_gdf = gdf.shift(1)
    gdf["time_delta"] = gdf["time"] - shifted_gdf["time"]
    gdf["dist_delta"] = gdf.distance(shifted_gdf)  # Luftlinie in Metern (UTM)
    gdf.at[0, "dist_delta"] = 0  # erster Punkt hat keinen Vorgänger
    gdf.at[0, "time_delta"] = pd.to_timedelta(0)

    # Geschwindigkeit in verschiedenen Einheiten ableiten
    gdf["m_per_s"] = gdf["dist_delta"] / gdf.time_delta.dt.seconds
    gdf.at[0, "m_per_s"] = 0
    gdf["km_per_h"] = gdf["m_per_s"] * 3.6
    gdf["min_per_km"] = 60 / (gdf["km_per_h"])

    gdf["time_passed"] = gdf["time_delta"].cumsum()

    # Höhenänderung zum Vorgänger: in Auf- (positiv) und Abstieg (negativ)
    # aufgeteilt, damit beide Anteile später z.B. aufsummiert werden können.
    gdf["ele_delta"] = gdf["ele"] - shifted_gdf["ele"]
    gdf["ascent"] = gdf["ele_delta"]
    gdf.loc[gdf.ascent < 0, ["ascent"]] = 0
    gdf["descent"] = gdf["ele_delta"]
    gdf.loc[gdf.descent > 0, ["descent"]] = 0

    # Gefälle/Steigung in % = Höhenänderung relativ zur zurückgelegten Strecke
    # (Höhenänderung allein ist zwischen Tracks nicht vergleichbar, da sie
    # von der Punktdichte abhängt - % bezogen auf die Distanz schon).
    gdf["slope"] = 100 * gdf["ele_delta"] / gdf["dist_delta"]

    # Höhe relativ zum Startpunkt (für einen alternativen Profil-Vergleich)
    gdf["ele_normalized"] = gdf["ele"] - gdf.iloc[0]["ele"]

    # Division durch 0 km/h (Stillstand) erzeugt +/-inf bei slope/min_per_km
    # -> für sauberes Plotten durch NaN ersetzen.
    gdf.replace(np.inf, np.nan, inplace=True)
    gdf.replace(-np.inf, np.nan, inplace=True)

    # Die Geometrie-Spalte wird ab hier nicht mehr gebraucht (lat/lon liegen
    # ja schon als eigene Spalten vor) -> als schlankes, reines DataFrame
    # zurückgeben. Kleinere Objekte = schnellere Cache-Treffer.
    return pd.DataFrame(gdf.drop(columns="geometry"))


# --------------------------------------------------------------------------
# Sidebar: Filter
# --------------------------------------------------------------------------
with st.sidebar:
    color_options = {
        "ele": "Höhe",
        "km_per_h": "Geschwindigkeit",
        "slope": "Gefälle",
        "none": "Nichts",
    }
    st.selectbox(
        "Einfärben mit",
        options=list(color_options.keys()),
        key="plot_column",
        format_func=lambda x: color_options[x],
    )

    meta = load_metadata()

    # Drei aufeinander aufbauende Filter: Sport -> Tour -> Track. Jede Stufe
    # filtert "meta" weiter ein, sodass z.B. die Tour-Auswahl nur noch Touren
    # zeigt, die zum gewählten Sport passen.
    sport_dict = meta.set_index("sport_id")["sport_title"].to_dict()
    st.pills(
        label="Sport",
        options=sport_dict,
        selection_mode="multi",
        key="sport_select",
        format_func=lambda x: sport_dict[x],
    )
    selected_sport = st.session_state.sport_select
    if len(selected_sport) > 0:
        meta = meta[meta["sport_id"].isin(selected_sport)]

    tour_dict = meta.set_index("tour_id")["tour_title"].to_dict()
    st.pills(
        label="Tour",
        options=tour_dict,
        selection_mode="multi",
        key="tour_select",
        format_func=lambda x: tour_dict[x],
    )
    selected_tours = st.session_state.tour_select
    if len(selected_tours) > 0:
        meta = meta[meta["tour_id"].isin(selected_tours)]

    track_dict = meta.set_index("track_id")["track_title"].to_dict()
    st.pills(
        label="Track",
        options=track_dict,
        selection_mode="multi",
        key="track_select",
        format_func=lambda x: track_dict[x],
    )
    selected_tracks = st.session_state.track_select
    if len(selected_tracks) > 0:
        meta = meta[meta["track_id"].isin(selected_tracks)]
    else:
        st.error("wähle einen Track")
        st.stop()

# Erst JETZT, nachdem feststeht welche Tracks tatsächlich gebraucht werden,
# die zugehörigen (potenziell großen) GPX-Binärdaten nachladen.
file_data = load_track_files(tuple(sorted(meta["track_id"].tolist())))
df = meta.merge(file_data, on="track_id", how="inner")


# --------------------------------------------------------------------------
# Wertebereiche für Kartenausschnitt und Farbskala
# --------------------------------------------------------------------------
# Gemeinsame Bounding-Box über alle ausgewählten Tracks, damit die Karte
# beim Start so zugeschnitten wird, dass alle Tracks sichtbar sind.
map_bounds = [
    [df["location_lat_min"].min(), df["location_lon_min"].min()],
    [df["location_lat_max"].max(), df["location_lon_max"].max()],
]

range_speed = [df["speed_min"].min(), df["speed_max"].max()]
range_elevation = [df["elevation_min"].min(), df["elevation_max"].max()]
range_slope = [df["slope_min"].min(), df["slope_max"].max()]
range_none = [1, 1]

# Je nach gewählter Farb-Spalte den passenden Wertebereich für die
# Farbskala (vmin/vmax) auswählen.
if st.session_state.plot_column == "km_per_h":
    range_att = range_speed
elif st.session_state.plot_column == "ele":
    range_att = range_elevation
elif st.session_state.plot_column == "slope":
    range_att = range_slope
else:
    range_att = range_none


# --------------------------------------------------------------------------
# Klick-Auswahl im Höhenprofil: Zustand verwalten
# --------------------------------------------------------------------------
# st.session_state.selected_point hält den zuletzt im Profil angeklickten
# Punkt als Dict {"track_id", "point_index", "lat", "lon"} fest und
# überlebt damit auch den Rerun, der durch den Klick selbst ausgelöst wird.
if "selected_point" not in st.session_state:
    st.session_state.selected_point = None
selected_point = st.session_state.selected_point

# Wenn sich die Sidebar-Filter geändert haben (andere/weniger/mehr Tracks),
# verwerfen wir eine evtl. vorhandene Punkt-Auswahl. Zusätzlich wird der
# interne Auswahl-Status des Plotly-Charts gelöscht: Ohne das könnte ein
# "alter" Klick (curve_number/point_index aus der vorherigen Track-
# Reihenfolge) nach einem Filterwechsel fälschlich auf einen anderen Track
# gemappt werden.
current_track_ids = tuple(sorted(df["track_id"].tolist()))
filters_changed = st.session_state.get("_last_track_ids") != current_track_ids
st.session_state["_last_track_ids"] = current_track_ids
if filters_changed:
    selected_point = None
    st.session_state.selected_point = None
    if "my_chart_key" in st.session_state:
        del st.session_state["my_chart_key"]

# Falls der Track des ausgewählten Punkts durch die Filter weggefallen ist,
# Auswahl ebenfalls verwerfen (z.B. Track-Pill wurde wieder abgewählt).
if (
    selected_point is not None
    and selected_point["track_id"] not in df["track_id"].values
):
    selected_point = None
    st.session_state.selected_point = None


# --------------------------------------------------------------------------
# Karte aufbauen
# --------------------------------------------------------------------------
# Ist ein Profilpunkt ausgewählt, wird die Karte direkt auf diesen Punkt
# zentriert (statt auf die Bounding-Box aller Tracks) - das ist die
# eigentliche "Springe zum angeklickten Punkt"-Funktionalität.
if selected_point is not None:
    m = folium.Map(
        location=[selected_point["lat"], selected_point["lon"]], zoom_start=16
    )
else:
    m = folium.Map()
    m.fit_bounds(map_bounds)

folium.TileLayer(
    tiles="https://tile.opentopomap.org/{z}/{x}/{y}.png",
    attr=(
        "Map data: &copy; OpenStreetMap contributors, SRTM | "
        'Map style: &copy; <a href="https://opentopomap.org">OpenTopoMap</a> '
        '(<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC-BY-SA</a>)'
    ),
    name="OpenTopoMap",
    max_zoom=17,
).add_to(m)

# Farbskala, mit der die Tracks entlang von Höhe/Tempo/Gefälle eingefärbt
# werden (Blau = niedrig -> Rot = hoch, analog zu einer "Regenbogen"-Skala).
track_col = cm.LinearColormap(
    colors=[
        "#0000FF",
        "#007FFF",
        "#00FFFF",
        "#7FFF00",
        "#FFFF00",
        "#FF7F00",
        "#FF0000",
    ],
    vmin=range_att[0],
    vmax=range_att[1],
    caption="",
)


# --------------------------------------------------------------------------
# Höhenprofil aufbauen + pro Track auf der Karte einzeichnen
# --------------------------------------------------------------------------
fig = go.Figure()

# distance läuft über alle Tracks hinweg weiter (gemeinsame x-Achse im Profil)
distance = 0
# track_id -> verarbeitetes DataFrame, für die Klick-Auflösung weiter unten
track_store = {}

for i in range(0, len(df)):

    gpx_file = df["file_data"].iloc[i]
    track_id = df["track_id"].iloc[i]

    # Gecachte, teure Verarbeitung (siehe process_track oben)
    gdf = process_track(track_id, gpx_file)

    # Streckenlänge fortlaufend über alle Tracks hinweg aufsummieren, damit
    # im gemeinsamen Profil mehrere Tracks hintereinander auf der x-Achse
    # erscheinen, statt sich zu überlappen.
    gdf["distance"] = gdf["dist_delta"].cumsum() + distance
    distance = gdf["distance"].max()

    track_store[track_id] = gdf

    # --- Karte: Track einzeichnen -----------------------------------------
    track_loc = gdf[["lat", "lon"]].values.tolist()

    folium.CircleMarker(
        [gdf["lat"].iloc[0], gdf["lon"].iloc[0]],
        tooltip="Start",
        fill=True,
        fill_color="green",
        radius=10,
        fill_opacity=0.8,
        stroke=True,
        color="white",
        opacity=0.8,
    ).add_to(m)
    folium.CircleMarker(
        [gdf["lat"].iloc[-1], gdf["lon"].iloc[-1]],
        tooltip="Ende",
        fill=True,
        fill_color="red",
        radius=10,
        fill_opacity=0.8,
        stroke=True,
        color="white",
        opacity=0.8,
    ).add_to(m)

    # Werte der gewählten Spalte (Höhe/Tempo/Gefälle) für die Einfärbung der
    # Linie; bei "Nichts" wird stattdessen ein konstanter Wert verwendet,
    # damit die Linie trotzdem (einfarbig) gezeichnet werden kann.
    if st.session_state.plot_column != "none":
        track_att = gdf[st.session_state.plot_column].values.tolist()
    else:
        track_att = np.repeat([1], len(track_loc))

    folium.ColorLine(
        positions=track_loc,
        colors=track_att,
        colormap=track_col,
        weight=5,
    ).add_to(m)

    # --- Profil: Track als Trace hinzufügen --------------------------------
    # Marker-Größe/-Umrandung normal, AUSSER am gerade ausgewählten Punkt:
    # dort wird der Marker vergrößert und schwarz umrandet, um ihn im
    # Profil optisch hervorzuheben.
    marker_sizes = np.full(len(gdf), 5)
    marker_line_widths = np.zeros(len(gdf))
    if selected_point is not None and selected_point["track_id"] == track_id:
        marker_sizes[selected_point["point_index"]] = 20
        marker_line_widths[selected_point["point_index"]] = 3

    # Haupt-Trace: ein Punkt pro Trackpunkt, Farbe nach der gewählten Spalte,
    # mit Flächenfüllung bis zur x-Achse (Silhouette des Höhenprofils).
    # WICHTIG: Die curve_number dieser Trace (gerade Zahl: 0, 2, 4, ...)
    # wird weiter unten genutzt, um einen Klick im Profil wieder einem
    # konkreten Trackpunkt zuzuordnen.
    fig.add_trace(
        go.Scatter(
            x=gdf["distance"],
            y=gdf["ele"],
            fill="tozeroy",
            mode="markers",
            marker=dict(
                color=track_att,
                colorscale="jet",
                cmin=range_att[0],
                cmax=range_att[1],
                size=marker_sizes,
                line=dict(color="black", width=marker_line_widths),
            ),
            fillgradient=dict(
                type="vertical",
                colorscale=[
                    (0.0, "rgba(120, 190, 170, 0.0)"),
                    (1.0, "rgba(120, 190, 170, 0.8)"),
                ],
                start=range_elevation[0] * 0.9,
                stop=range_elevation[1] * 1.1,
            ),
            showlegend=False,
        )
    )

    # Zweite Trace: nur Start- und Endpunkt des Tracks, groß und farbig
    # hervorgehoben (analog zu den Start/Ende-Markern auf der Karte).
    # WICHTIG: Die curve_number dieser Trace ist immer ungerade (1, 3, 5, ...).
    fig.add_trace(
        go.Scatter(
            mode="markers",
            x=[gdf.at[0, "distance"], gdf.iloc[-1]["distance"]],
            y=[gdf.at[0, "ele"], gdf.iloc[-1]["ele"]],
            marker=dict(
                color=["green", "red"],
                size=15,
                line=dict(color="white", width=2),
            ),
            showlegend=False,
        )
    )
    fig.update_yaxes(range=[gdf["ele"].min() * 0.9, gdf["ele"].max() * 1.1])


# Ausgewählten Punkt zuletzt auf der Karte einzeichnen, damit er garantiert
# über allen Track-Linien/-Markern liegt (Folium zeichnet später
# hinzugefügte Elemente oberhalb früherer).
if selected_point is not None:
    folium.CircleMarker(
        [selected_point["lat"], selected_point["lon"]],
        tooltip="Ausgewählter Punkt",
        fill=True,
        fill_color="yellow",
        radius=12,
        fill_opacity=1.0,
        stroke=True,
        color="black",
        weight=3,
    ).add_to(m)

folium.LayerControl().add_to(m)
m.add_child(track_col)
st_folium(m, width="stretch", height=800)

fig.update_layout(
    xaxis_fixedrange=True, 
    yaxis_fixedrange=True,
    margin=dict(l=0, r=0, t=0, b=0) 
    )
# on_select="rerun": ein Klick im Profil löst einen kompletten Skript-Rerun
# aus; "event" enthält danach die Klick-Information (welche Trace, welcher
# Punkt) für DIESEN Durchlauf.
event = st.plotly_chart(fig, on_select="rerun", key="my_chart_key", height=300)


# --------------------------------------------------------------------------
# Klick im Profil auswerten und Auswahl in den Session State legen
# --------------------------------------------------------------------------
clicked_points = event.selection.get("points", []) if event is not None else []

# Direkt nach einem Filterwechsel überspringen wir die Auswertung (siehe
# Kommentar weiter oben bei "filters_changed") - sonst könnte eine ALTE,
# noch im Chart-Status gespeicherte Klick-Position fälschlich auf einen
# anderen Track der neu sortierten Liste zeigen.
if not filters_changed and clicked_points:
    pt = clicked_points[0]
    curve_number = pt["curve_number"]
    point_index = pt["point_index"]

    # Pro Track wurden oben GENAU zwei Traces angelegt (Profil + Start/Ende),
    # in dieser Reihenfolge -> daraus lässt sich der Track wieder eindeutig
    # zurückrechnen.
    track_pos = curve_number // 2  # Index des Tracks innerhalb von df
    is_main_trace = (
        curve_number % 2 == 0
    )  # gerade = Profil-Trace, ungerade = Start/Ende-Trace

    if track_pos < len(df):
        clicked_track_id = df["track_id"].iloc[track_pos]
        gdf_clicked = track_store[clicked_track_id]

        if is_main_trace:
            resolved_index = point_index
        else:
            # Die Start/Ende-Trace hat nur 2 Punkte: 0 = Start, 1 = Ende.
            resolved_index = 0 if point_index == 0 else len(gdf_clicked) - 1

        row = gdf_clicked.iloc[resolved_index]
        new_selection = {
            "track_id": clicked_track_id,
            "point_index": int(resolved_index),
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
        }

        # Nur wenn sich die Auswahl tatsächlich geändert hat einen weiteren
        # Rerun auslösen - verhindert eine Endlosschleife, falls derselbe
        # Klick (z.B. aus dem Chart-Status) erneut ausgewertet wird.
        if new_selection != st.session_state.selected_point:
            st.session_state.selected_point = new_selection
            st.rerun()