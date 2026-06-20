"""
map.py
======
Interaktive Karte für aufgezeichnete GPS-Tracks.

Dieses Modul stellt die Funktion render_map_page() bereit, die von app.py
als eine der Navigationsseiten eingebunden wird (siehe dort). Es kann zum
Debuggen aber auch weiterhin direkt mit `streamlit run map.py` gestartet
werden (siehe Aufruf von render_map_page() ganz am Ende der Datei).

Aufbau der Seite:
1. Sidebar: Pills-Filter nach Sport / Land / Jahr / Jahreszeit sowie eine
   aufklappbare Baumauswahl (Jahr -> Monat -> Tour -> Track) zur Auswahl
   einzelner Tracks; eine Tour-Checkbox wählt dabei alle Tracks dieser
   Tour (innerhalb ihres Jahr/Monat-Abschnitts) auf einmal aus. Der
   Land-Filter berücksichtigt sowohl das Start- als auch das Endland
   eines Tracks (per Reverse-Geocoding ermittelt), ein Track mit
   Grenzübertritt erscheint also unter beiden Ländern. Dazu die Auswahl
   der Farb-Spalte für das Höhenprofil (Höhe, Geschwindigkeit, Gefälle
   oder einfarbig).
2. Hauptbereich: zwei nebeneinanderliegende, umrandete Container (siehe
   st.container(border=True)). Links die Kennzahlen (Länge, Zeit,
   Auf-/Abstieg, Min/Max-Höhe) als Summe über alle aktuell ausgewählten
   Tracks, je Kennzahl eine eigene Zeile mit den Einzelwerten je Track klein
   daneben. Rechts daneben (deutlich breiter) eine Folium-Karte mit allen
   ausgewählten Tracks (farblich entlang der gewählten Spalte eingefärbt)
   und darunter ein gemeinsames Höhenprofil (Plotly) über alle Tracks
   hinweg.
3. Klick-Interaktion: Klickt man im Höhenprofil auf einen Punkt, wird dieser
   Punkt im Profil größer dargestellt UND als zusätzlicher Marker auf der
   Karte eingezeichnet, auf den die Karte zentriert wird.

Performance-Hinweis: GPX-Dateien werden aus DuckDB geladen und mit GeoPandas
aufwendig nachbearbeitet (Distanz, Tempo, Steigung, ...). Da Streamlit bei
JEDER Nutzerinteraktion das komplette Skript neu ausführt (auch bei einem
einfachen Klick im Profil), wird diese Verarbeitung über st.cache_data
gecacht - siehe process_track() in functions.py.
"""

import folium  # Erzeugt die interaktive Leaflet-Karte
import branca.colormap as cm  # Farbskala für die Karten-Einfärbung
import numpy as np  # Numerische Hilfsfunktionen (Arrays, NaN-Handling)
import pandas as pd
import plotly.graph_objects as go  # Höhenprofil-Diagramm
import streamlit as st  # Web-UI-Framework
from streamlit_folium import st_folium  # Rendert eine Folium-Karte in Streamlit

from functions import get_connection, process_track

# Die Verbindung wird beim ersten Import dieses Moduls einmalig über
# functions.get_connection() geholt (siehe dortige Erläuterung zu
# @st.cache_resource) - admin.py nutzt über denselben Aufruf dieselbe
# Verbindung.
con = get_connection()


# --------------------------------------------------------------------------
# Datenzugriff (gecacht)
# --------------------------------------------------------------------------
#@st.cache_data(show_spinner=False, ttl=60)
def load_metadata() -> pd.DataFrame:
    """
    Lädt nur die "leichten" Metadaten aller Tracks (Titel, Bounding-Box,
    Min/Max-Werte für Tempo/Höhe/Gefälle, Kennzahlen wie Distanz/Dauer/
    Auf-/Abstieg sowie Start-/End-Land, ...) - bewusst OHNE die teils
    großen GPX-Binärdaten (file_data). Diese Metadaten werden für die
    Sidebar-Filter (Sport/Land/Tour/Track-Auswahl) sowie die Kennzahlen-
    Anzeige gebraucht.

    ttl=60: nach 60 Sekunden wird neu aus der DB gelesen, falls z.B.
    zwischenzeitlich in der Verwaltung neue Tracks angelegt wurden.

    WICHTIG: Die Abfrage geht bewusst von 'gpx' aus (nicht von 'tours'),
    damit auch Tracks OHNE zugeordnete Tour angezeigt werden - bei einer
    Abfrage ausgehend von 'tours' würden solche Tracks durch den JOIN
    stillschweigend herausfallen.
    """
    return con.sql("""
        SELECT
            gpx.track_id, gpx.track_title, gpx.time_start,
            gpx.location_lat_min, gpx.location_lat_max,
            gpx.location_lon_min, gpx.location_lon_max,
            gpx.location_start_country, gpx.location_end_country,
            gpx.speed_min, gpx.speed_max,
            gpx.elevation_min, gpx.elevation_max,
            gpx.slope_min, gpx.slope_max,
            gpx.track_distance_m, gpx.track_time_s,
            gpx.track_ascent_m, gpx.track_descent_m,
            gpx.sport_id, sport.sport_title,
            gpx.tour_id, tours.tour_title
        FROM gpx
        LEFT JOIN tours ON gpx.tour_id = tours.tour_id
        LEFT JOIN sport ON gpx.sport_id = sport.sport_id
        ORDER BY gpx.time_start ASC
        """).fetchdf()


#@st.cache_data(show_spinner=False, ttl=60)
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


# --------------------------------------------------------------------------
# Track-Auswahl: Jahr (Expander) -> Monat -> Tour -> einzelne Tracks
# --------------------------------------------------------------------------
_MONTH_NAMES = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

# Meteorologische Jahreszeiten (Nordhalbkugel) je Monat (1-12).
_SEASON_LABELS = {
    "winter": "Winter",
    "spring": "Frühling",
    "summer": "Sommer",
    "autumn": "Herbst",
}


def _season_for_month(month: int) -> str:
    """Ordnet einen Kalendermonat (1-12) seiner meteorologischen Jahreszeit zu."""
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def _track_countries(row: pd.Series) -> list[str]:
    """
    Liefert die Liste der Länder eines Tracks (Start- UND Endland, per
    Reverse-Geocoding ermittelt), ohne Duplikate.

    Meist sind Start- und Endland identisch (Rundweg / Hin- und Rückweg) -
    in diesem Fall enthält die Liste nur einen Eintrag. Bei einem Track mit
    Grenzübertritt (Start- != Endland) enthält sie beide, sodass der Track
    im Land-Filter unter beiden auswählbar ist. Fehlt das Land (z.B. weil
    beim Hochladen kein Internet für das Reverse-Geocoding verfügbar war),
    wird der jeweilige Eintrag einfach ausgelassen.
    """
    countries = []
    for country in (row["location_start_country"], row["location_end_country"]):
        if pd.notna(country) and country not in countries:
            countries.append(country)
    return countries


def _track_checkbox_key(track_id) -> str:
    """Eindeutiger, stabiler Widget-Key für die Checkbox EINES Tracks - ein
    Track taucht im Baum (anhand seines eigenen time_start) immer nur genau
    einmal auf, daher reicht die track_id allein als Schlüssel."""
    return f"track_select_{track_id}"


def _tour_checkbox_key(year: int, month: int, tour_id) -> str:
    """
    Widget-Key der "alles auswählen"-Checkbox einer Tour INNERHALB einer
    bestimmten Jahr/Monat-Gruppe. Erstreckt sich eine Tour über mehrere
    Monate, erscheint sie entsprechend mehrfach mit jeweils eigenem Key -
    jede Checkbox wirkt dann nur auf die Tracks ihrer eigenen Gruppe.
    """
    return f"tour_select_{year}_{month}_{tour_id}"


def _on_tour_toggle(tour_key: str, track_ids: list) -> None:
    """
    Callback der Tour-Checkbox ("alles auswählen"): überträgt deren neuen
    Wert auf alle Track-Checkboxen dieser Jahr/Monat/Tour-Gruppe.

    Läuft als on_change-Handler VOR dem eigentlichen Skript-Rerun, daher
    ist das direkte Setzen von st.session_state[...] hier zulässig - die
    betroffenen Checkboxen werden erst danach (im Rerun) neu gezeichnet und
    übernehmen dann automatisch diesen neuen Wert.
    """
    checked = st.session_state[tour_key]
    for track_id in track_ids:
        st.session_state[_track_checkbox_key(track_id)] = checked


def _on_track_toggle(tour_key: str | None, sibling_track_ids: list) -> None:
    """
    Callback einer einzelnen Track-Checkbox: hält - falls der Track einer
    Tour angehört - die übergeordnete Tour-Checkbox synchron. Sie wird nur
    dann als "ausgewählt" angezeigt, wenn wirklich ALLE Tracks der Gruppe
    ausgewählt sind (kein echtes Tri-State, aber nah genug an der Erwartung
    "Tour-Haken = alle Tracks dabei").
    """
    if tour_key is None:
        return
    all_checked = all(
        st.session_state.get(_track_checkbox_key(tid), False)
        for tid in sibling_track_ids
    )
    st.session_state[tour_key] = all_checked


def _persistent_checkbox(label: str, key: str, default: bool, on_change, args: tuple):
    """
    Wrapper um st.checkbox(), der den von Streamlit selbst ausgegebenen
    Hinweis "created with a default value but also had its value set via
    the Session State API" vermeidet: 'value' wird nur beim allerersten
    Rendern dieses Keys übergeben. Existiert der Key bereits in
    st.session_state (sei es durch einen früheren Klick oder weil ein
    on_change-Handler - siehe _on_tour_toggle/_on_track_toggle - ihn vorab
    gesetzt hat), übernimmt Streamlit ohnehin automatisch diesen Wert.
    """
    kwargs = {"key": key, "on_change": on_change, "args": args}
    if key not in st.session_state:
        kwargs["value"] = default
    return st.checkbox(label, **kwargs)


def _render_track_tree(meta: pd.DataFrame) -> list:
    """
    Baut die Track-Auswahl als verschachtelte Struktur auf:
    Jahr (Expander) -> Monat -> Tour ("alles auswählen") -> einzelne Tracks.
    Tracks ohne Tour erscheinen direkt unter ihrem Monat.

    'meta' sollte bereits durch die Pills-Filter (Sport/Jahr/Jahreszeit)
    eingeschränkt sein - der Baum zeigt ausschließlich die übergebenen
    Zeilen an. Gibt die Liste der aktuell per Checkbox ausgewählten
    track_ids zurück.
    """
    years = sorted(meta["year"].dropna().unique().tolist(), reverse=True)
    most_recent_year = years[0] if years else None

    for year in years:
        year_df = meta[meta["year"] == year]
        with st.expander(f"{int(year)} ({len(year_df)})", expanded=(year == most_recent_year)):
            months = sorted(year_df["month"].dropna().unique().tolist(), reverse=True)
            for month in months:
                month_df = year_df[year_df["month"] == month]
                st.markdown(f"**{_MONTH_NAMES[int(month) - 1]}**")

                with_tour = month_df[month_df["tour_id"].notna()]
                without_tour = month_df[month_df["tour_id"].isna()]

                # Tracks mit Tour: gruppiert mit "alles auswählen"-Checkbox.
                for tour_id, group in with_tour.groupby("tour_id"):
                    tour_title = group["tour_title"].iloc[0]
                    track_ids = group["track_id"].tolist()
                    tour_key = _tour_checkbox_key(int(year), int(month), tour_id)
                    initial = all(
                        st.session_state.get(_track_checkbox_key(tid), False)
                        for tid in track_ids
                    )
                    _persistent_checkbox(
                        f"🧭 {tour_title} ({len(track_ids)})",
                        key=tour_key,
                        default=initial,
                        on_change=_on_tour_toggle,
                        args=(tour_key, track_ids),
                    )
                    for _, row in group.iterrows():
                        track_key = _track_checkbox_key(row["track_id"])
                        _persistent_checkbox(
                            f"　↳ {row['track_title']}",
                            key=track_key,
                            default=False,
                            on_change=_on_track_toggle,
                            args=(tour_key, track_ids),
                        )

                # Tracks ohne Tour: einzeln, direkt unter dem Monat.
                for _, row in without_tour.iterrows():
                    track_key = _track_checkbox_key(row["track_id"])
                    _persistent_checkbox(
                        row["track_title"],
                        key=track_key,
                        default=False,
                        on_change=_on_track_toggle,
                        args=(None, []),
                    )

    return [
        track_id
        for track_id in meta["track_id"].tolist()
        if st.session_state.get(_track_checkbox_key(track_id), False)
    ]


# --------------------------------------------------------------------------
# Kennzahlen (KPIs): Länge, Zeit, Auf-/Abstieg, Min/Max-Höhe
# --------------------------------------------------------------------------
def _format_distance_km(meters: float) -> str:
    """Formatiert eine Distanz in Metern als Kilometer-Text, z.B. '12.3 km'."""
    if pd.isna(meters):
        return "–"
    return f"{meters / 1000:.1f} km"


def _format_duration(seconds: float) -> str:
    """Formatiert eine Dauer in Sekunden als 'Hh MMmin'-Text, z.B. '3h 45min'."""
    if pd.isna(seconds):
        return "–"
    total_minutes = int(round(seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes:02d}min"


def _format_meters(value: float) -> str:
    """Formatiert einen Höhen-/Auf-/Abstiegswert in Metern, z.B. '1234 m'."""
    if pd.isna(value):
        return "–"
    return f"{value:.0f} m"


def _render_kpis(df: pd.DataFrame) -> None:
    """
    Zeigt Kennzahlen zu den aktuell ausgewählten Tracks an: pro Kennzahl
    (Länge, Zeit, Auf-/Abstieg, Min/Max-Höhe) eine eigene, am linken Rand
    verankerte Zeile - links eine kompakte Kennzahlen-Box mit der Summe
    (bzw. bei Min/Max-Höhe dem Minimum/Maximum) über alle ausgewählten
    Tracks, daneben klein die Werte je einzelnem Track. Die Zeilen sind
    jeweils durch einen Trenner voneinander abgesetzt.

    Länge/Zeit/Auf-/Abstieg werden für die Box-Summe aufaddiert. Bei
    Min/Max-Höhe wäre ein simples Aufsummieren fachlich sinnlos - hier wird
    stattdessen das Minimum aller Track-Minima bzw. das Maximum aller
    Track-Maxima gebildet, also die tiefste bzw. höchste Stelle über alle
    ausgewählten Tracks hinweg.

    Hinweis: 'track_descent_m' ist in der Datenbank als NEGATIVER Wert
    abgelegt (siehe summarize_track() in functions.py). Für die Anzeige
    hier wird der Betrag gebildet, damit "Abstieg" wie "Aufstieg" als
    positive Meterzahl erscheint.
    """
    st.subheader("Kennzahlen")

    descent_abs = df["track_descent_m"].abs()

    # (Label, Summen-/Aggregatwert über alle Tracks, Formatierfunktion,
    # Werte je einzelnem Track in derselben Reihenfolge wie df)
    kpi_rows = [
        ("Länge", df["track_distance_m"].sum(), _format_distance_km, df["track_distance_m"]),
        ("Zeit", df["track_time_s"].sum(), _format_duration, df["track_time_s"]),
        ("Aufstieg", df["track_ascent_m"].sum(), _format_meters, df["track_ascent_m"]),
        ("Abstieg", descent_abs.sum(), _format_meters, descent_abs),
        ("Min. Höhe", df["elevation_min"].min(), _format_meters, df["elevation_min"]),
        ("Max. Höhe", df["elevation_max"].max(), _format_meters, df["elevation_max"]),
    ]

    # [1, 4]: schmale Box links (am Rand verankert), breiterer Bereich
    # daneben für die kleingedruckten Einzelwerte je Track.
    for label, total_value, formatter, per_track_values in kpi_rows:
        st.divider()
        col_box, col_tracks = st.columns([1, 1])
        with col_box:
            st.metric(label, formatter(total_value))
        with col_tracks:
            for title, value in zip(df["track_title"], per_track_values):
                st.caption(f"{title}: {formatter(value)}")    
            #track_text = "  ·  ".join(
            #    f"{title}: {formatter(value)}"
            #    for title, value in zip(df["track_title"], per_track_values)
            #)
            #st.caption(track_text)
        


def _render_map_and_profile(df: pd.DataFrame) -> None:
    """
    Baut die Folium-Karte und das Plotly-Höhenprofil für die aktuell
    ausgewählten Tracks auf und rendert beide übereinander.

    Wird aus render_map_page() heraus innerhalb der rechten Spalte
    aufgerufen (Kennzahlen links, Karte + Höhenprofil rechts daneben -
    siehe dortige Spaltenaufteilung).
    """
    # ----------------------------------------------------------------------
    # Wertebereiche für Kartenausschnitt und Farbskala
    # ----------------------------------------------------------------------
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

    # ----------------------------------------------------------------------
    # Klick-Auswahl im Höhenprofil: Zustand verwalten
    # ----------------------------------------------------------------------
    # st.session_state.selected_point hält den zuletzt im Profil angeklickten
    # Punkt als Dict {"track_id", "point_index", "lat", "lon"} fest und
    # überlebt damit auch den Rerun, der durch den Klick selbst ausgelöst wird.
    if "selected_point" not in st.session_state:
        st.session_state.selected_point = None
    selected_point = st.session_state.selected_point

    # Wenn sich die Sidebar-Filter geändert haben (andere/weniger/mehr
    # Tracks), verwerfen wir eine evtl. vorhandene Punkt-Auswahl. Zusätzlich
    # wird der interne Auswahl-Status des Plotly-Charts gelöscht: Ohne das
    # könnte ein "alter" Klick (curve_number/point_index aus der vorherigen
    # Track-Reihenfolge) nach einem Filterwechsel fälschlich auf einen
    # anderen Track gemappt werden.
    current_track_ids = tuple(sorted(df["track_id"].tolist()))
    filters_changed = st.session_state.get("_last_track_ids") != current_track_ids
    st.session_state["_last_track_ids"] = current_track_ids
    if filters_changed:
        selected_point = None
        st.session_state.selected_point = None
        if "my_chart_key" in st.session_state:
            del st.session_state["my_chart_key"]

    # Falls der Track des ausgewählten Punkts durch die Filter weggefallen
    # ist, Auswahl ebenfalls verwerfen (z.B. Track-Pill wurde wieder
    # abgewählt).
    if (
        selected_point is not None
        and selected_point["track_id"] not in df["track_id"].values
    ):
        selected_point = None
        st.session_state.selected_point = None

    # ----------------------------------------------------------------------
    # Karte aufbauen
    # ----------------------------------------------------------------------
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

    # ----------------------------------------------------------------------
    # Höhenprofil aufbauen + pro Track auf der Karte einzeichnen
    # ----------------------------------------------------------------------
    fig = go.Figure()

    # distance läuft über alle Tracks hinweg weiter (gemeinsame x-Achse im Profil)
    distance = 0
    # track_id -> verarbeitetes DataFrame, für die Klick-Auflösung weiter unten
    track_store = {}

    for i in range(0, len(df)):

        gpx_file = df["file_data"].iloc[i]
        track_id = df["track_id"].iloc[i]

        # Gecachte, teure Verarbeitung (siehe functions.process_track)
        gdf = process_track(track_id, gpx_file)

        # Streckenlänge fortlaufend über alle Tracks hinweg aufsummieren,
        # damit im gemeinsamen Profil mehrere Tracks hintereinander auf der
        # x-Achse erscheinen, statt sich zu überlappen.
        gdf["distance"] = gdf["dist_delta"].cumsum() + distance
        distance = gdf["distance"].max()

        track_store[track_id] = gdf

        # --- Karte: Track einzeichnen ---------------------------------
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

        # Werte der gewählten Spalte (Höhe/Tempo/Gefälle) für die Einfärbung
        # der Linie; bei "Nichts" wird stattdessen ein konstanter Wert
        # verwendet, damit die Linie trotzdem (einfarbig) gezeichnet wird.
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

        # --- Profil: Track als Trace hinzufügen --------------------------
        # Marker-Größe/-Umrandung normal, AUSSER am gerade ausgewählten
        # Punkt: dort wird der Marker vergrößert und schwarz umrandet, um
        # ihn im Profil optisch hervorzuheben.
        marker_sizes = np.full(len(gdf), 5)
        marker_line_widths = np.zeros(len(gdf))
        if selected_point is not None and selected_point["track_id"] == track_id:
            marker_sizes[selected_point["point_index"]] = 20
            marker_line_widths[selected_point["point_index"]] = 3

        # Haupt-Trace: ein Punkt pro Trackpunkt, Farbe nach der gewählten
        # Spalte, mit Flächenfüllung bis zur x-Achse (Silhouette des
        # Höhenprofils).
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

    # Ausgewählten Punkt zuletzt auf der Karte einzeichnen, damit er
    # garantiert über allen Track-Linien/-Markern liegt (Folium zeichnet
    # später hinzugefügte Elemente oberhalb früherer).
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
        margin=dict(l=0, r=0, t=0, b=0),
    )
    # on_select="rerun": ein Klick im Profil löst einen kompletten
    # Skript-Rerun aus; "event" enthält danach die Klick-Information
    # (welche Trace, welcher Punkt) für DIESEN Durchlauf.
    event = st.plotly_chart(fig, on_select="rerun", key="my_chart_key", height=300)

    # ----------------------------------------------------------------------
    # Klick im Profil auswerten und Auswahl in den Session State legen
    # ----------------------------------------------------------------------
    clicked_points = event.selection.get("points", []) if event is not None else []

    # Direkt nach einem Filterwechsel überspringen wir die Auswertung (siehe
    # Kommentar weiter oben bei "filters_changed") - sonst könnte eine ALTE,
    # noch im Chart-Status gespeicherte Klick-Position fälschlich auf einen
    # anderen Track der neu sortierten Liste zeigen.
    if not filters_changed and clicked_points:
        pt = clicked_points[0]
        curve_number = pt["curve_number"]
        point_index = pt["point_index"]

        # Pro Track wurden oben GENAU zwei Traces angelegt (Profil +
        # Start/Ende), in dieser Reihenfolge -> daraus lässt sich der Track
        # wieder eindeutig zurückrechnen.
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

            # Nur wenn sich die Auswahl tatsächlich geändert hat einen
            # weiteren Rerun auslösen - verhindert eine Endlosschleife,
            # falls derselbe Klick (z.B. aus dem Chart-Status) erneut
            # ausgewertet wird.
            if new_selection != st.session_state.selected_point:
                st.session_state.selected_point = new_selection
                st.rerun()


def render_map_page() -> None:
    """Baut die komplette Kartenseite auf (Sidebar-Filter, Karte, Höhenprofil)."""

    # ----------------------------------------------------------------------
    # Sidebar: Filter
    # ----------------------------------------------------------------------
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

        if meta["track_id"].dropna().empty:
            st.info("Noch keine Tracks vorhanden. Lege zuerst welche in der Verwaltung an.")
            st.stop()

        # Jahr/Monat/Jahreszeit aus dem Startzeitpunkt ableiten - Basis
        # sowohl für die Pills-Filter als auch für die Baum-Gruppierung
        # weiter unten. Tracks ganz ohne Zeitstempel (selten, z.B. GPX ohne
        # <time>-Angaben) haben dadurch kein Jahr und tauchen im Baum nicht
        # auf, bleiben aber über die anderen Seiten weiterhin sichtbar.
        meta = meta.copy()
        meta["year"] = meta["time_start"].dt.year
        meta["month"] = meta["time_start"].dt.month
        meta["season"] = meta["month"].apply(
            lambda m: _season_for_month(int(m)) if pd.notna(m) else None
        )
        # Liste der Länder (Start + Ende) je Track - Basis für den
        # Land-Filter direkt unten.
        meta["countries"] = meta.apply(_track_countries, axis=1)

        # Vier kaskadierende Pills-Filter: Sport -> Land -> Jahr ->
        # Jahreszeit. Jede Stufe filtert "meta" weiter ein, sodass z.B. die
        # Jahr-Auswahl nur noch Jahre zeigt, die zum gewählten Sport/Land
        # passen.
        sport_dict = meta.set_index("sport_id")["sport_title"].dropna().to_dict()
        st.pills(
            label="Sport",
            options=sport_dict,
            selection_mode="multi",
            key="sport_select",
            format_func=lambda x: sport_dict[x],
        )
        if st.session_state.sport_select:
            meta = meta[meta["sport_id"].isin(st.session_state.sport_select)]

        country_options = sorted({c for countries in meta["countries"] for c in countries})
        st.pills(
            label="Land",
            options=country_options,
            selection_mode="multi",
            key="country_select",
        )
        if st.session_state.country_select:
            selected_countries = set(st.session_state.country_select)
            meta = meta[
                meta["countries"].apply(lambda cs: bool(selected_countries.intersection(cs)))
            ]

        year_options = sorted(meta["year"].dropna().astype(int).unique().tolist(), reverse=True)
        st.pills(
            label="Jahr",
            options=year_options,
            selection_mode="multi",
            key="year_select",
        )
        if st.session_state.year_select:
            meta = meta[meta["year"].isin(st.session_state.year_select)]

        season_options = [s for s in _SEASON_LABELS if s in set(meta["season"].dropna())]
        st.pills(
            label="Jahreszeit",
            options=season_options,
            selection_mode="multi",
            key="season_select",
            format_func=lambda x: _SEASON_LABELS[x],
        )
        if st.session_state.season_select:
            meta = meta[meta["season"].isin(st.session_state.season_select)]

        st.divider()
        st.caption("Tracks auswählen")
        if meta.empty:
            st.info("Keine Tracks für diese Filter.")
            selected_tracks = []
        else:
            selected_tracks = _render_track_tree(meta)

        if not selected_tracks:
            st.error("wähle einen Track")
            st.stop()
        meta = meta[meta["track_id"].isin(selected_tracks)]

    # Erst JETZT, nachdem feststeht welche Tracks tatsächlich gebraucht
    # werden, die zugehörigen (potenziell großen) GPX-Binärdaten nachladen.
    file_data = load_track_files(tuple(sorted(meta["track_id"].tolist())))
    df = meta.merge(file_data, on="track_id", how="inner")

    # ----------------------------------------------------------------------
    # Hauptbereich: Kennzahlen links, Karte + Höhenprofil rechts daneben
    # ----------------------------------------------------------------------
    col_kpis, col_map = st.columns([1, 6], gap="small")

    with col_kpis:
        with st.container(border=True):
            _render_kpis(df)

    with col_map:
        with st.container(border=True):
            _render_map_and_profile(df)


# Direkter Start zu Debug-Zwecken: `streamlit run map.py`. Im Normalbetrieb
# wird render_map_page() stattdessen von app.py über die Navigation
# aufgerufen.
if __name__ == "__main__":
    st.set_page_config(page_title="Karte", layout="wide")
    render_map_page()
