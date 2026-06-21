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
   Tour (innerhalb ihres Jahr/Monat-Teils) auf einmal aus. Der
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
4. Planungsmodus (Tourenplanung): Über den Button "📐 Planung" in der
   Seitenleiste - nur aktivierbar, wenn genau EIN Track ausgewählt ist -
   lässt sich dieser Track in Teile unterteilen. Die
   Unterteilungspunkte werden per Mausklick gesetzt - entweder wie der
   normale Klick aus Punkt 3 im Höhenprofil, oder direkt auf der Karte
   (dort wird der nächstgelegene Trackpunkt zum Klick ermittelt, siehe
   _nearest_point_index); ein erneuter Klick auf einen bereits gesetzten
   Punkt entfernt ihn wieder (siehe _toggle_split_point /
   _render_map_and_profile). Alternativ lässt sich jeder Punkt über ein
   "✕" in der Kennzahlen-Box löschen (siehe _render_planning_kpis). Die
   Kennzahlen-Box zeigt in diesem Modus die Werte je Teil statt je
   Track (Wiederverwendung von _render_kpis mit einem pro Teil
   gebauten DataFrame, siehe _summarize_segment). Ein Export-Button
   darunter erzeugt eine ZIP-Datei mit je einer GPX-Datei pro Teil
   sowie einer weiteren GPX-Datei mit den gesetzten Punkten als Wegpunkte
   (siehe _build_planning_export_zip).

Performance-Hinweis: GPX-Dateien werden aus DuckDB geladen und mit GeoPandas
aufwendig nachbearbeitet (Distanz, Tempo, Steigung, ...). Da Streamlit bei
JEDER Nutzerinteraktion das komplette Skript neu ausführt (auch bei einem
einfachen Klick im Profil), wird diese Verarbeitung über st.cache_data
gecacht - siehe process_track() in functions.py.
"""

import io  # ZIP-Export im Planungsmodus (in-memory statt temporärer Dateien)
import zipfile  # ZIP-Export im Planungsmodus

import folium  # Erzeugt die interaktive Leaflet-Karte
import branca.colormap as cm  # Farbskala für die Karten-Einfärbung
import gpxpy  # GPX-Export der Teile/Punkte im Planungsmodus
import gpxpy.gpx
import numpy as np  # Numerische Hilfsfunktionen (Arrays, NaN-Handling)
import pandas as pd
import plotly.graph_objects as go  # Höhenprofil-Diagramm
import streamlit as st  # Web-UI-Framework
from streamlit_folium import st_folium  # Rendert eine Folium-Karte in Streamlit

from functions import (
    DEFAULT_MIN_ELEVATION_CHANGE_M,
    DEFAULT_MIN_SPEED_MOVING_KMH,
    compute_ascent_descent,
    compute_moving_time_s,
    get_connection,
    process_track,
)

# Die Verbindung wird beim ersten Import dieses Moduls einmalig über
# functions.get_connection() geholt (siehe dortige Erläuterung zu
# @st.cache_resource) - admin.py nutzt über denselben Aufruf dieselbe
# Verbindung.
con = get_connection()


# --------------------------------------------------------------------------
# Datenzugriff (gecacht)
# --------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=60)
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
            gpx.track_distance_m, gpx.track_time_s, gpx.track_time_moving_s,
            gpx.track_ascent_m, gpx.track_descent_m,
            gpx.sport_id, sport.sport_title,
            gpx.tour_id, tours.tour_title
        FROM gpx
        LEFT JOIN tours ON gpx.tour_id = tours.tour_id
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
    return f"{meters / 1000:,.1f} km"


def _format_duration(seconds: float) -> str:
    """Formatiert eine Dauer in Sekunden als 'Hh MMmin'-Text, z.B. '3h 45min'."""
    if pd.isna(seconds):
        return "–"
    total_minutes = int(round(seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}:{minutes:02d} h"


def _format_meters(value: float) -> str:
    """Formatiert einen Höhen-/Auf-/Abstiegswert in Metern, z.B. '1234 m'."""
    if pd.isna(value):
        return "–"
    return f"{value:,.0f} m"


# --------------------------------------------------------------------------
# Hover-Kennzahlen je Punkt (Karte + Höhenprofil)
# --------------------------------------------------------------------------
def _build_hover_texts(gdf: pd.DataFrame, track_title: str) -> pd.Series:
    """
    Baut für JEDEN Punkt eines verarbeiteten Track-DataFrames (siehe
    functions.process_track) einen mehrzeiligen HTML-Hovertext mit den
    Kennzahlen an genau dieser Stelle (Distanz, Höhe, Tempo, Gefälle,
    vergangene Zeit seit Trackstart).

    Wird für ZWEI Zwecke verwendet, damit Karte und Höhenprofil beim Hover
    exakt dieselben Werte an derselben Stelle anzeigen:
        - als Tooltip-Text der (dünn gesäten) Hover-Marker auf der Karte
          (siehe _render_map_and_profile)
        - als 'text' der Plotly-Trace des Höhenprofils, dort über
          'hovertemplate="%{text}..."' eingebunden

    Bewusst spaltenweise vektorisiert (statt einer Python-Schleife mit
    Einzel-Format-Aufrufen je Punkt), da ein Track durchaus mehrere tausend
    Punkte enthalten kann.

    'distance' wird unverändert aus 'gdf' übernommen - im
    Mehrtrack-Höhenprofil ist das bereits die über alle ausgewählten Tracks
    hinweg aufsummierte Strecke (siehe _render_map_and_profile), wodurch der
    Hover-Wert exakt der x-Achsen-Position im Profil entspricht.
    """
    distance_km = (gdf["distance"] / 1000).round(2)
    elevation_m = gdf["ele"].fillna(0).round(0).astype(int)
    speed_kmh = gdf["km_per_h"].fillna(0).round(1)
    slope_pct = gdf["slope"].fillna(0).round(1)
    elapsed = gdf["time_passed"].apply(
        lambda td: _format_duration(td.total_seconds()) if pd.notna(td) else "–"
    )

    title_html = f"<b>{track_title}</b><br>" if track_title else ""
    return (
        title_html
        + "📍 " + distance_km.astype(str) + " km<br>"
        + "⛰️ " + elevation_m.astype(str) + " m<br>"
        + "🚀 " + speed_kmh.astype(str) + " km/h<br>"
        + "📐 " + slope_pct.astype(str) + " %<br>"
        + "⏱️ " + elapsed
    )


def _render_kpis(df: pd.DataFrame, subheader: str = "Kennzahlen") -> None:
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

    'subheader' erlaubt es, dieselbe Funktion auch im Planungsmodus
    wiederzuverwenden: _render_planning_kpis() übergibt dort ein DataFrame
    mit denselben Spalten, aber einer Zeile je Teil (statt je Track)
    und einer entsprechend angepassten Überschrift - siehe dort.
    """
    st.subheader(subheader)
    st.slider(
        "Breite anpassen",
        min_value=10,
        max_value=35,
        key="kpi_col_width_pct",
        help="Breite dieser Spalte gegenüber der Karte rechts daneben.",
    )

    descent_abs = df["track_descent_m"].abs()

    # (Label, Summen-/Aggregatwert über alle Tracks, Formatierfunktion,
    # Werte je einzelnem Track in derselben Reihenfolge wie df)
    kpi_rows = [
        ("Länge", df["track_distance_m"].sum(), _format_distance_km, df["track_distance_m"]),
        ("Zeit", df["track_time_s"].sum(), _format_duration, df["track_time_s"]),
        ("Zeit in Bewegung", df["track_time_moving_s"].sum(), _format_duration, df["track_time_moving_s"]),
        ("Aufstieg", df["track_ascent_m"].sum(), _format_meters, df["track_ascent_m"]),
        ("Abstieg", descent_abs.sum(), _format_meters, descent_abs),
        ("Min. Höhe", df["elevation_min"].min(), _format_meters, df["elevation_min"]),
        ("Max. Höhe", df["elevation_max"].max(), _format_meters, df["elevation_max"]),
    ]

    # [1, 4]: schmale Box links (am Rand verankert), breiterer Bereich
    # daneben für die kleingedruckten Einzelwerte je Track.
    for label, total_value, formatter, per_track_values in kpi_rows:
        st.divider()
        col_box, col_tracks = st.columns([3, 2])
        with col_box:
            st.metric(label, formatter(total_value))
        with col_tracks:
            for title, value in zip(df["track_title"], per_track_values):
                st.caption(f"{title}: {formatter(value)}")    



# --------------------------------------------------------------------------
# Planungsmodus: Track in Teile unterteilen, Kennzahlen je Teil,
# GPX-Export
# --------------------------------------------------------------------------
def _segment_bounds(n_points: int, split_indices: list[int]) -> list[tuple[int, int]]:
    """
    Wandelt eine Liste von Unterteilungspunkt-Indizes in die (jeweils
    INKLUSIVEN) Start-/End-Indizes der daraus entstehenden Teile um.

    Ohne Unterteilungspunkte ergibt sich genau ein Teil (der gesamte
    Track, von Index 0 bis zum letzten Index). Aufeinanderfolgende
    Teile teilen sich jeweils ihren Grenzpunkt (Ende von Teil N =
    Anfang von Teil N+1) - dadurch ergibt die Summe der
    Teils-Kennzahlen (siehe _summarize_segment) wieder exakt die
    Kennzahlen des Gesamttracks, ohne den gemeinsamen Punkt doppelt zu
    zählen (seine eigene Distanz/Zeit zu sich selbst ist 0).
    """
    if n_points <= 1:
        return [(0, max(n_points - 1, 0))]
    boundaries = sorted(
        {0, n_points - 1} | {i for i in split_indices if 0 < i < n_points - 1}
    )
    return list(zip(boundaries[:-1], boundaries[1:]))


def _summarize_segment(gdf: pd.DataFrame, start_idx: int, end_idx: int) -> dict:
    """
    Berechnet die Kennzahlen EINES Teils (Punkte start_idx bis
    end_idx, beide inklusive) eines bereits verarbeiteten Track-DataFrames
    (siehe functions.process_track) - analog zu summarize_track() in
    functions.py, aber für einen Teilbereich statt den gesamten Track.
    Die zurückgegebenen Schlüssel entsprechen bewusst den Spaltennamen, die
    _render_kpis() von einer Track-Zeile erwartet (track_distance_m, ...),
    damit _render_planning_kpis() für die Anzeige direkt _render_kpis()
    wiederverwenden kann.

    Länge und Gesamtzeit werden als Differenz der bereits über den
    GESAMTEN Track kumulierten Spalten 'distance' bzw. 'time_passed'
    gebildet (Ende minus Anfang) statt den Teil isoliert neu zu
    berechnen - das ist gleichwertig, aber günstiger.

    Auf-/Abstieg sowie "Zeit in Bewegung" dagegen NICHT als einfache
    Differenz, sondern über dieselben Schwellwert-Funktionen wie der
    Gesamttrack (compute_ascent_descent / compute_moving_time_s),
    angewendet NUR auf die Punkte dieses Teils: Das Schwellwert-
    Verfahren für Auf-/Abstieg hängt vom jeweils zuletzt erreichten
    Bezugspunkt ab, der an jeder Teilsgrenze neu beginnt - eine
    Differenz der kumulierten Gesamttrack-Werte wäre hier NICHT
    gleichwertig. Verwendet werden dabei die Standard-Schwellwerte
    (DEFAULT_MIN_ELEVATION_CHANGE_M / DEFAULT_MIN_SPEED_MOVING_KMH); wurde
    ein Track in der Verwaltung mit abweichenden Schwellwerten neu
    berechnet, können die Teils-Summen daher in seltenen Fällen
    minimal von den (in der Datenbank gespeicherten) Gesamttrack-Werten
    abweichen, da diese individuellen Schwellwerte hier nicht
    gespeichert/bekannt sind.
    """
    segment = gdf.iloc[start_idx : end_idx + 1].reset_index(drop=True)
    ascent_m, descent_m = compute_ascent_descent(segment, DEFAULT_MIN_ELEVATION_CHANGE_M)
    # Für "Zeit in Bewegung" wird die ERSTE Zeile des Teils
    # ausgenommen: ihr 'time_delta' (siehe process_gpx_dataframe)
    # beschreibt das Intervall VOM VORHERIGEN Punkt zu diesem
    # Teils-Startpunkt und gehört damit fachlich zum VORHERIGEN
    # Teil, dessen letzter Punkt genau dieser (gemeinsame)
    # Grenzpunkt ist - würde sie hier mitgezählt, würde dieses Intervall
    # doppelt in die Summe einfließen (einmal als letztes Intervall des
    # vorherigen, einmal als "erstes" dieses Teils). Beim
    # allerersten Teil (start_idx == 0) ist 'time_delta' an Position
    # 0 ohnehin bereits 0 (kein Vorgänger vorhanden), das Ausschließen
    # ändert dort also nichts am Ergebnis.
    moving_s = compute_moving_time_s(segment.iloc[1:], DEFAULT_MIN_SPEED_MOVING_KMH)
    return {
        "track_distance_m": float(gdf["distance"].iloc[end_idx] - gdf["distance"].iloc[start_idx]),
        "track_time_s": float(
            (gdf["time_passed"].iloc[end_idx] - gdf["time_passed"].iloc[start_idx]).total_seconds()
        ),
        "track_time_moving_s": moving_s,
        "track_ascent_m": ascent_m,
        "track_descent_m": descent_m,
        "elevation_min": float(segment["ele"].min()),
        "elevation_max": float(segment["ele"].max()),
    }


def _nearest_point_index(gdf: pd.DataFrame, lat: float, lon: float) -> int:
    """
    Findet den Index des Punktes in 'gdf' (Spalten 'lat'/'lon' in Grad), der
    einem Klick auf der Karte am nächsten liegt - für die Zuordnung eines
    Kartenklicks (siehe st_folium-Rückgabewert 'last_clicked' in
    _render_map_and_profile) zu einem konkreten Trackpunkt im
    Planungsmodus.

    Die Entfernung wird dabei nur NÄHERUNGSWEISE in einer lokal-ebenen
    Projektion bestimmt (Breitengrad direkt in Meter umgerechnet,
    Längengrad zusätzlich mit cos(Breite) skaliert, da Längengrade in
    Richtung der Pole "schmaler" werden) statt geodätisch exakt - für die
    Suche nach dem NÄCHSTEN Punkt auf einem GPS-Track (Punktabstand
    typischerweise wenige bis einige zig Meter) reicht diese Näherung
    locker aus.
    """
    lat_rad = np.radians(float(gdf["lat"].mean()))
    dx_m = (gdf["lon"] - lon) * 111_320 * np.cos(lat_rad)
    dy_m = (gdf["lat"] - lat) * 110_540
    return int((dx_m**2 + dy_m**2).idxmin())


def _toggle_split_point(track_id: str, point_index: int, n_points: int) -> bool:
    """
    Fügt 'point_index' als Unterteilungspunkt des Tracks 'track_id' hinzu,
    falls er dort noch nicht gesetzt ist, oder entfernt ihn wieder, falls
    er es bereits ist - "erstellen" und "löschen" laufen also über
    denselben Klick (siehe _render_map_and_profile, dort wird diese
    Funktion bei jedem Klick im Planungsmodus aufgerufen).

    Start- und Endpunkt des Tracks (Index 0 bzw. n_points - 1) können
    nicht als Unterteilungspunkt gesetzt werden, da sie ohnehin bereits
    die äußeren Teilsgrenzen bilden - ein Klick dorthin wird
    ignoriert.

    Gibt zurück, ob sich dadurch tatsächlich etwas verändert hat (False
    für einen ignorierten Klick auf Start/Ende).
    """
    if point_index <= 0 or point_index >= n_points - 1:
        return False
    splits = st.session_state.split_points.setdefault(track_id, [])
    if point_index in splits:
        splits.remove(point_index)
    else:
        splits.append(point_index)
        splits.sort()
    return True


def _slugify_filename(text: str) -> str:
    """
    Erzeugt aus einem beliebigen Titel einen einfachen, dateisystem- und
    ZIP-sicheren Dateinamen für den Export: alles außer Buchstaben, Ziffern,
    '_' und '-' wird durch ein Leerzeichen ersetzt, anschließend werden
    die so entstandenen Wörter mit '_' wieder zusammengesetzt (entfernt
    dabei automatisch mehrfache/führende/abschließende Leerzeichen).
    """
    cleaned = "".join(c if (c.isalnum() or c in "_-") else " " for c in text)
    return "_".join(cleaned.split()) or "track"


def _gdf_slice_to_gpx_xml(gdf: pd.DataFrame, start_idx: int, end_idx: int, name: str) -> str:
    """Baut aus den Punkten start_idx bis end_idx (inklusive) eines
    verarbeiteten Track-DataFrames eine eigenständige GPX-Datei (ein
    <trk> mit genau einem <trkseg>) und gibt deren XML-Text zurück."""
    gpx = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack(name=name)
    gpx.tracks.append(track)
    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)
    for _, row in gdf.iloc[start_idx : end_idx + 1].iterrows():
        segment.points.append(
            gpxpy.gpx.GPXTrackPoint(
                latitude=float(row["lat"]),
                longitude=float(row["lon"]),
                elevation=float(row["ele"]) if pd.notna(row["ele"]) else None,
                time=row["time"].to_pydatetime() if pd.notna(row["time"]) else None,
            )
        )
    return gpx.to_xml()


def _split_points_to_gpx_xml(gdf: pd.DataFrame, split_indices: list[int]) -> str:
    """Baut aus den gesetzten Unterteilungspunkten eine eigenständige GPX-
    Datei mit einem Wegpunkt (<wpt>) je Punkt, fortlaufend nummeriert in
    Track-Reihenfolge, und gibt deren XML-Text zurück."""
    gpx = gpxpy.gpx.GPX()
    for n, idx in enumerate(sorted(split_indices), start=1):
        row = gdf.iloc[idx]
        gpx.waypoints.append(
            gpxpy.gpx.GPXWaypoint(
                latitude=float(row["lat"]),
                longitude=float(row["lon"]),
                elevation=float(row["ele"]) if pd.notna(row["ele"]) else None,
                time=row["time"].to_pydatetime() if pd.notna(row["time"]) else None,
                name=f"Punkt {n}",
            )
        )
    return gpx.to_xml()


def _build_planning_export_zip(
    gdf: pd.DataFrame,
    track_title: str,
    bounds: list[tuple[int, int]],
    split_indices: list[int],
) -> bytes:
    """
    Baut die ZIP-Datei für den Export-Button des Planungsmodus: je
    Teil eine eigenständige GPX-Datei (_gdf_slice_to_gpx_xml) sowie -
    sofern mindestens ein Unterteilungspunkt gesetzt ist - eine weitere
    GPX-Datei mit allen Punkten als Wegpunkte (_split_points_to_gpx_xml).
    Gibt die fertige ZIP-Datei als Bytes zurück (für st.download_button).
    """
    base_name = _slugify_filename(track_title)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, (start_idx, end_idx) in enumerate(bounds, start=1):
            xml = _gdf_slice_to_gpx_xml(
                gdf, start_idx, end_idx, name=f"{track_title} – Teil {n}"
            )
            zf.writestr(f"{base_name}_Teil_{n:02d}.gpx", xml)
        if split_indices:
            xml_points = _split_points_to_gpx_xml(gdf, split_indices)
            zf.writestr(f"{base_name}_punkte.gpx", xml_points)
    return buffer.getvalue()


def _render_planning_kpis(gdf: pd.DataFrame, track_id: str, track_title: str) -> None:
    """
    Planungsmodus-Variante von _render_kpis(): zeigt statt der Kennzahlen
    je Track die Kennzahlen je Teil eines einzelnen Tracks (der
    Planungsmodus ist nur bei genau einem ausgewählten Track aktivierbar -
    siehe render_map_page). Dafür wird ein DataFrame mit einer Zeile je
    Teil gebaut (Spalten wie eine normale Track-Zeile, siehe
    _summarize_segment) und direkt an _render_kpis() übergeben - dadurch
    bleiben Formatierung und KPI-Zeilen (inkl. künftiger Änderungen daran)
    automatisch zwischen Track- und Teils-Ansicht konsistent.

    Darunter folgen die Liste der gesetzten Unterteilungspunkte (je mit
    Lösch-Button) sowie der Export-Button (siehe
    _build_planning_export_zip) - "Export unter KPIs" im Planungsmodus.

    'gdf' ist das bereits verarbeitete Track-DataFrame (siehe
    functions.process_track) - wird von render_map_page() vorab EINMAL
    berechnet und sowohl hierher als auch an _render_map_and_profile()
    durchgereicht, damit der (recht teure) GPX-Aufbereitungsschritt nicht
    zweimal pro Seitenaufruf läuft.
    """
    split_indices = sorted(st.session_state.split_points.get(track_id, []))
    bounds = _segment_bounds(len(gdf), split_indices)

    seg_df = pd.DataFrame([_summarize_segment(gdf, a, b) for a, b in bounds])
    seg_df["track_title"] = [f"Teil {n}" for n in range(1, len(bounds) + 1)]

    if len(bounds) <= 1:
        st.info(
            "Noch keine Unterteilungspunkte gesetzt - klicke ins "
            "Höhenprofil, um den Track in Teile zu unterteilen."
        )

    _render_kpis(seg_df, subheader="Kennzahlen – Teile")

    # ------------------------------------------------------------------
    # Unterteilungspunkte: Liste mit Lösch-Button je Punkt
    # ------------------------------------------------------------------
    st.divider()
    st.caption("Unterteilungspunkte")
    if not split_indices:
        st.caption("– keine –")
    else:
        for n, idx in enumerate(split_indices, start=1):
            row = gdf.iloc[idx]
            col_label, col_del = st.columns([4, 1])
            with col_label:
                st.caption(f"Punkt {n}: {row['distance'] / 1000:.1f} km, {_format_meters(row['ele'])}")
            with col_del:
                if st.button("✕", key=f"del_split_{track_id}_{idx}", help="Punkt löschen"):
                    st.session_state.split_points[track_id].remove(idx)
                    st.rerun()

    # ------------------------------------------------------------------
    # Export: je Teil eine GPX-Datei + eine GPX-Datei mit den Punkten
    # ------------------------------------------------------------------
    st.divider()
    zip_bytes = _build_planning_export_zip(gdf, track_title, bounds, split_indices)
    st.download_button(
        "📦 Export",
        data=zip_bytes,
        file_name=f"{_slugify_filename(track_title)}_planung.zip",
        mime="application/zip",
        key="planning_export_button",
        width="stretch",
        help=(
            "Lädt eine ZIP-Datei herunter: je eine GPX-Datei pro Teil "
            "sowie eine weitere GPX-Datei mit den Unterteilungspunkten als "
            "Wegpunkte."
        ),
    )


def _render_map_and_profile(
    df: pd.DataFrame,
    planning_mode: bool = False,
    track_store_seed: dict | None = None,
) -> None:
    """
    Baut die Folium-Karte und das Plotly-Höhenprofil für die aktuell
    ausgewählten Tracks auf und rendert beide übereinander.

    Wird aus render_map_page() heraus innerhalb der rechten Spalte
    aufgerufen (Kennzahlen links, Karte + Höhenprofil rechts daneben -
    siehe dortige Spaltenaufteilung).

    'planning_mode' aktiviert die Unterteilung in Teile per Mausklick
    - im Höhenprofil ODER direkt auf der Karte (siehe _toggle_split_point,
    _nearest_point_index) - sowie deren farbliche Hervorhebung auf Karte
    und Höhenprofil; gilt nur sinnvoll, wenn 'df' genau einen Track enthält
    (siehe render_map_page).

    'track_store_seed' erlaubt es, ein im Planungsmodus bereits von
    render_map_page() berechnetes Track-DataFrame hier wiederzuverwenden,
    statt es (für denselben einzigen Track) ein zweites Mal über
    process_track() zu berechnen.
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

    # Unterteilungspunkte je Track (track_id -> sortierte Liste von
    # Punkt-Indizes), siehe _toggle_split_point. Wird defensiv auch hier
    # initialisiert, obwohl render_map_page() das bereits zentral erledigt
    # (siehe dort), damit diese Funktion auch unabhängig davon funktioniert.
    if "split_points" not in st.session_state:
        st.session_state.split_points = {}

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
        st.session_state["_last_planning_click"] = None
        st.session_state["_last_planning_map_click"] = None
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
    # track_id -> verarbeitetes DataFrame, für die Klick-Auflösung weiter unten.
    # Im Planungsmodus bereits von render_map_page() vorberechnete Einträge
    # (siehe track_store_seed) werden übernommen, statt process_track() ein
    # zweites Mal für denselben Track aufzurufen.
    track_store = dict(track_store_seed) if track_store_seed else {}

    for i in range(0, len(df)):

        gpx_file = df["file_data"].iloc[i]
        track_id = df["track_id"].iloc[i]

        # Gecachte, teure Verarbeitung (siehe functions.process_track) -
        # bzw. Wiederverwendung des vorberechneten Ergebnisses (s.o.).
        gdf = track_store.get(track_id)
        if gdf is None:
            gdf = process_track(track_id, gpx_file)

        # Streckenlänge fortlaufend über alle Tracks hinweg aufsummieren,
        # damit im gemeinsamen Profil mehrere Tracks hintereinander auf der
        # x-Achse erscheinen, statt sich zu überlappen.
        gdf["distance"] = gdf["dist_delta"].cumsum() + distance
        distance = gdf["distance"].max()

        track_store[track_id] = gdf

        # Hover-Kennzahlen je Punkt - einmal pro Track berechnet, weiter
        # unten sowohl für die Hover-Marker auf der Karte als auch für das
        # Höhenprofil verwendet (siehe _build_hover_texts).
        hover_texts = _build_hover_texts(gdf, df["track_title"].iloc[i])

        # Unterteilungspunkte DIESES Tracks (Planungsmodus) - einmal hier
        # ermittelt, weiter unten sowohl für die Marker auf der Karte als
        # auch für die Hervorhebung im Höhenprofil verwendet.
        track_splits = (
            set(st.session_state.split_points.get(track_id, [])) if planning_mode else set()
        )

        # --- Karte: Track einzeichnen ---------------------------------
        track_loc = gdf[["lat", "lon"]].values.tolist()

        folium.CircleMarker(
            [gdf["lat"].iloc[0], gdf["lon"].iloc[0]],
            tooltip="<b>Start</b><br>" + hover_texts.iloc[0],
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
            tooltip="<b>Ende</b><br>" + hover_texts.iloc[-1],
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

        # --- Karte: Hover-Marker mit Kennzahlen je Punkt ----------------
        # Eine durchgehende Linie (ColorLine oben) kann in Folium/Leaflet
        # selbst keinen punktgenauen Hover anbieten - dafür braucht es
        # eigene Marker je Punkt. Da ein Track durchaus mehrere tausend
        # Punkte enthalten kann, würde EIN Marker pro Punkt die Karte mit
        # ebenso vielen DOM-Elementen überladen und spürbar verlangsamen -
        # daher wird hier eine über den Track verteilte Auswahl an Punkten
        # verwendet ('hover_stride'), die zudem mit der Anzahl gleichzeitig
        # angezeigter Tracks sinkt, damit auch bei vielen ausgewählten
        # Tracks insgesamt nicht zu viele Marker entstehen. Start- und
        # Endpunkt sind durch die eigenen Marker oben bereits abgedeckt,
        # werden hier aber der Einfachheit halber (harmlos) mit erfasst.
        #
        # Die Marker selbst bleiben praktisch unsichtbar (fill_opacity nur
        # knapp über 0, statt exakt 0): Ein Kreis mit fill_opacity=0 würde
        # vom Browser nicht mehr als "gefüllt" gewertet und entsprechend
        # auch keine Hover-Ereignisse mehr auslösen.
        target_hover_points = max(15, 700 // max(1, len(df)))
        hover_stride = max(1, len(gdf) // target_hover_points)
        for idx in range(0, len(gdf), hover_stride):
            row = gdf.iloc[idx]
            folium.CircleMarker(
                [row["lat"], row["lon"]],
                tooltip=hover_texts.iloc[idx],
                radius=10,
                fill=True,
                fill_opacity=0.01,
                opacity=0,
                weight=0,
            ).add_to(m)

        # Unterteilungspunkte (Planungsmodus) als eigene, orange Marker -
        # dauerhaft sichtbar, im Gegensatz zum (gelben) zuletzt
        # ausgewählten Punkt weiter unten, der nur den letzten Klick zeigt.
        for n, split_idx in enumerate(sorted(track_splits), start=1):
            if 0 <= split_idx < len(gdf):
                srow = gdf.iloc[split_idx]
                folium.CircleMarker(
                    [srow["lat"], srow["lon"]],
                    tooltip=f"Trennpunkt {n}",
                    fill=True,
                    fill_color="orange",
                    radius=9,
                    fill_opacity=0.9,
                    stroke=True,
                    color="white",
                    weight=2,
                ).add_to(m)

        # --- Profil: Track als Trace hinzufügen --------------------------
        # Marker-Größe/-Umrandung normal, AUSSER:
        # - an gesetzten Unterteilungspunkten (Planungsmodus): orange
        #   umrandet und etwas vergrößert, dauerhaft sichtbar (siehe
        #   _toggle_split_point).
        # - am gerade ausgewählten Punkt: zusätzlich vergrößert, um ihn
        #   im Profil optisch hervorzuheben (Klick-Feedback, siehe Punkt 3
        #   im Modul-Docstring oben).
        marker_sizes = np.full(len(gdf), 5)
        marker_line_widths = np.zeros(len(gdf))
        marker_line_colors = np.full(len(gdf), "black", dtype=object)

        for split_idx in track_splits:
            if 0 <= split_idx < len(gdf):
                marker_sizes[split_idx] = 14
                marker_line_widths[split_idx] = 3
                marker_line_colors[split_idx] = "orange"

        if selected_point is not None and selected_point["track_id"] == track_id:
            sel_idx = selected_point["point_index"]
            marker_sizes[sel_idx] = 20
            marker_line_widths[sel_idx] = 3
            if sel_idx not in track_splits:
                marker_line_colors[sel_idx] = "black"

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
                    line=dict(color=marker_line_colors.tolist(), width=marker_line_widths),
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
                # Hover zeigt dieselben Kennzahlen wie die Hover-Marker auf
                # der Karte (siehe _build_hover_texts) - "<extra></extra>"
                # unterdrückt die sonst zusätzlich angezeigte Trace-Box mit
                # Tracename/Farbsample.
                text=hover_texts,
                hoverlabel=dict(bgcolor="black"),
                hovertemplate="%{text}<extra></extra>",
                showlegend=False,
            )
        )

        # Zweite Trace: nur Start- und Endpunkt des Tracks, groß und farbig
        # hervorgehoben (analog zu den Start/Ende-Markern auf der Karte).
        # WICHTIG: Die curve_number dieser Trace ist immer ungerade (1, 3, 5, ...).
        # hoverinfo="skip": diese Trace liegt direkt über den Start-/
        # End-Punkten der Haupt-Trace (s.o.), die dort bereits die
        # Kennzahlen-Hovertexte liefert - ohne "skip" würde stattdessen
        # diese (hoverlose) Overlay-Trace den Hover an genau diesen beiden
        # Punkten "stehlen".
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
                hoverinfo="skip",
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

    m.add_child(track_col)
    folium.LayerControl().add_to(m)
    folium.plugins.Fullscreen(
    position="topleft",
    title="Expand me",
    title_cancel="Exit me",
    force_separate_button=True,
    ).add_to(m)
    
    # BEWUSST ohne 'key=': Ändert sich der Karteninhalt spürbar (z.B. beim
    # Wechsel auf einen anderen Track), erzeugt streamlit-folium dadurch
    # automatisch einen neuen internen Schlüssel, die Komponente wird neu
    # gemountet und 'last_clicked' (s.u.) damit auf None zurückgesetzt. Mit
    # einem FESTEN 'key' würde dagegen ein alter Kartenklick über einen
    # Trackwechsel hinweg "hängen bleiben" und könnte fälschlich auf den
    # NEUEN Track angewendet werden (siehe Klick-Auswertung weiter unten).
    map_state = st_folium(m, width="stretch", height=800)

    # ----------------------------------------------------------------------
    # Klick auf der KARTE auswerten (Planungsmodus): der nächstgelegene
    # Trackpunkt zum Klick wird ermittelt (siehe _nearest_point_index) und
    # als Unterteilungspunkt umgeschaltet (siehe _toggle_split_point) -
    # funktional dasselbe wie der Profil-Klick weiter unten, nur mit Klick
    # auf der Karte statt im Höhenprofil als Auslöser. Da der Planungsmodus
    # nur bei genau einem ausgewählten Track aktiv ist (siehe
    # render_map_page), reicht hier der einzige Eintrag in track_store.
    # ----------------------------------------------------------------------
    if planning_mode and not filters_changed and map_state is not None:
        last_clicked = map_state.get("last_clicked")
        if last_clicked is not None:
            click_track_id = df["track_id"].iloc[0]
            gdf_for_click = track_store[click_track_id]
            resolved_index = _nearest_point_index(
                gdf_for_click, last_clicked["lat"], last_clicked["lng"]
            )

            # Eigener "zuletzt verarbeiteter Klick"-Schutz, analog zu
            # '_last_planning_click' beim Profil-Klick weiter unten, aber
            # unabhängig davon geführt: st_folium liefert denselben
            # 'last_clicked'-Wert über mehrere Reruns hinweg zurück, bis
            # ein NEUER Kartenklick erfolgt - ohne diesen Schutz würde der
            # Punkt bei jedem Rerun erneut umgeschaltet.
            click_token = (
                click_track_id,
                round(last_clicked["lat"], 7),
                round(last_clicked["lng"], 7),
            )
            if st.session_state.get("_last_planning_map_click") != click_token:
                st.session_state["_last_planning_map_click"] = click_token
                if _toggle_split_point(click_track_id, resolved_index, len(gdf_for_click)):
                    row = gdf_for_click.iloc[resolved_index]
                    st.session_state.selected_point = {
                        "track_id": click_track_id,
                        "point_index": int(resolved_index),
                        "lat": float(row["lat"]),
                        "lon": float(row["lon"]),
                    }
                    st.rerun()

    fig.update_layout(
        xaxis_fixedrange=True,
        yaxis_fixedrange=True,
        margin=dict(l=0, r=0, t=0, b=0),
        hoverlabel=dict(bgcolor="white", font_size=13, align="left"),
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

            if planning_mode:
                # Im Planungsmodus zählt JEDER neue Klick als Umschalt-
                # Aktion für einen Unterteilungspunkt (siehe
                # _toggle_split_point) - AUCH ein wiederholter Klick auf
                # denselben Punkt, um ihn wieder zu entfernen. Der normale
                # Endlosschleifen-Schutz unten (Vergleich mit
                # selected_point) würde das verhindern, da ein erneuter
                # Klick auf denselben Punkt ja dieselbe new_selection
                # ergäbe - daher hier ein eigener, von selected_point
                # unabhängiger Schutz anhand des zuletzt verarbeiteten
                # Klicks.
                click_token = (clicked_track_id, resolved_index)
                if st.session_state.get("_last_planning_click") != click_token:
                    st.session_state["_last_planning_click"] = click_token
                    if _toggle_split_point(clicked_track_id, resolved_index, len(gdf_clicked)):
                        st.session_state.selected_point = new_selection
                        st.rerun()
            # Nur wenn sich die Auswahl tatsächlich geändert hat einen
            # weiteren Rerun auslösen - verhindert eine Endlosschleife,
            # falls derselbe Klick (z.B. aus dem Chart-Status) erneut
            # ausgewertet wird.
            elif new_selection != st.session_state.selected_point:
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

        # ------------------------------------------------------------------
        # Planungsmodus (Tourenplanung): nur aktivierbar bei GENAU einem
        # ausgewählten Track, da sich nur ein einzelner Track sinnvoll in
        # Teile unterteilen lässt (siehe Modul-Docstring, Punkt 4).
        # Fällt die Bedingung weg (z.B. weitere Tracks dazu ausgewählt,
        # während der Modus bereits aktiv war), wird er automatisch wieder
        # deaktiviert, statt nur das Steuerelement zu sperren.
        # ------------------------------------------------------------------
        st.divider()
        single_track_selected = len(selected_tracks) == 1
        if not single_track_selected:
            st.session_state.planning_mode = False
        st.toggle(
            "📐 Planung",
            key="planning_mode",
            disabled=not single_track_selected,
            help=(
                "Im Planungsmodus lässt sich der ausgewählte Track per "
                "Klick auf die Karte oder ins Höhenprofil in Teile "
                "unterteilen. Dafür muss genau ein Track ausgewählt sein."
            ),
        )

    # Erst JETZT, nachdem feststeht welche Tracks tatsächlich gebraucht
    # werden, die zugehörigen (potenziell großen) GPX-Binärdaten nachladen.
    file_data = load_track_files(tuple(sorted(meta["track_id"].tolist())))
    df = meta.merge(file_data, on="track_id", how="inner")

    # planning_mode kann (s.o.) zwar nur bei genau einem ausgewählten Track
    # aktiviert werden, "len(df) == 1" wird hier trotzdem defensiv erneut
    # geprüft, falls sich die Auswahl zwischen Sidebar und diesem Punkt
    # noch ändern sollte.
    planning_active = bool(st.session_state.get("planning_mode")) and len(df) == 1

    # Im Planungsmodus wird der (einzige) Track HIER schon einmal
    # verarbeitet und sowohl an die Kennzahlen-Spalte als auch an
    # _render_map_and_profile() weitergereicht, damit process_track()
    # nicht zweimal pro Seitenaufruf für denselben Track läuft (siehe
    # _render_map_and_profile, Parameter 'track_store_seed').
    precomputed_track_store = None
    if planning_active:
        track_id = df["track_id"].iloc[0]
        precomputed_track_store = {track_id: process_track(track_id, df["file_data"].iloc[0])}

    # ----------------------------------------------------------------------
    # Hauptbereich: Kennzahlen links, Karte + Höhenprofil rechts daneben
    # ----------------------------------------------------------------------
    # Die Breite der linken Spalte (in Prozent) lässt sich über einen
    # Schieberegler INNERHALB dieser Spalte einstellen (siehe _render_kpis,
    # direkt unter der Überschrift "Kennzahlen") - st.columns() braucht die
    # Breiten aber bereits HIER, bevor der Spalteninhalt (und damit der
    # Schieberegler selbst) gerendert wird. Daher zunächst der zuletzt
    # gespeicherte Wert aus session_state (mit Default beim allerersten
    # Aufruf); der Schieberegler aktualisiert denselben Schlüssel weiter
    # unten und wirkt damit ab dem NÄCHSTEN Rerun (das Verschieben des
    # Reglers selbst löst bereits einen Rerun aus).
    if "kpi_col_width_pct" not in st.session_state:
        st.session_state.kpi_col_width_pct = 15
    kpi_width_pct = st.session_state.kpi_col_width_pct
    col_kpis, col_map = st.columns([kpi_width_pct, 100 - kpi_width_pct], gap="small")

    with col_kpis:
        with st.container(border=True):
            if planning_active:
                track_id = df["track_id"].iloc[0]
                _render_planning_kpis(
                    precomputed_track_store[track_id], track_id, df["track_title"].iloc[0]
                )
            else:
                _render_kpis(df)

    with col_map:
        with st.container(border=True):
            if planning_active:
                st.caption(
                    "📐 Planungsmodus: Klicke auf die Karte oder ins Höhenprofil, um "
                    "Unterteilungspunkte zu setzen - ein erneuter Klick auf einen "
                    "bestehenden Punkt entfernt ihn wieder."
                )
            _render_map_and_profile(
                df, planning_mode=planning_active, track_store_seed=precomputed_track_store
            )


# Direkter Start zu Debug-Zwecken: `streamlit run map.py`. Im Normalbetrieb
# wird render_map_page() stattdessen von app.py über die Navigation
# aufgerufen.
if __name__ == "__main__":
    st.set_page_config(page_title="Karte", layout="wide")
    render_map_page()
