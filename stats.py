"""
stats.py
========
Statistik-Seite der MyTrack-App.

Dieses Modul stellt die Funktion render_stats_page() bereit, die von app.py
als dritte Navigationsseite eingebunden wird (neben "Karte" und
"Verwaltung"). Es kann zum Debuggen auch direkt mit
`streamlit run stats.py` gestartet werden (siehe Dateiende).

Aufbau der Seite:
1. Kopfzeile mit den Gesamtwerten über alle Tracks (Anzahl, Kilometer,
   Aufstieg, Zeit).
2. Zwei Diagramme: Kilometer je Jahr sowie - für ein auswählbares Jahr -
   Kilometer je Monat.
3. Tabelle je Sportart (Anzahl Tracks, Kilometer, Aufstiegsmeter, Zeit).
4. Heatmap aller aufgezeichneten Punkte auf einer Folium-Karte.

Datengrundlage sind fast ausschließlich die ohnehin gespeicherten
Kennzahlen aus functions.load_metadata() - es ist also keine erneute
GPX-Verarbeitung nötig. Einzige Ausnahme ist die Heatmap, die die
Koordinaten aller Tracks braucht (functions.load_heatmap_points(), dort
ausgedünnt und gecacht).
"""

import folium
import pandas as pd
import streamlit as st
from folium.plugins import HeatMap
from streamlit_folium import st_folium

from functions import load_heatmap_points, load_metadata

# Punkte, die für die Heatmap übersprungen werden (jeder n-te Punkt wird
# verwendet). Siehe functions.load_heatmap_points().
_HEATMAP_STRIDE = 10

_MONTH_NAMES = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def _prepare(meta: pd.DataFrame) -> pd.DataFrame:
    """
    Ergänzt die Metadaten um die für die Auswertung benötigten,
    abgeleiteten Spalten (Jahr, Monat, Kilometer, Stunden) und füllt
    fehlende Sport-Zuordnungen mit einer sprechenden Bezeichnung, damit
    Tracks ohne Sportart in den Gruppierungen nicht stillschweigend
    verschwinden.
    """
    df = meta.copy()
    df["year"] = df["time_start"].dt.year
    df["month"] = df["time_start"].dt.month
    df["km"] = df["track_distance_m"] / 1000
    df["hours"] = df["track_time_s"] / 3600
    df["ascent"] = df["track_ascent_m"]
    df["sport"] = df["sport_title"].fillna("(ohne Sportart)")
    return df


def _render_totals(df: pd.DataFrame) -> None:
    """Gesamtwerte über alle Tracks als Kennzahlen-Zeile."""
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Tracks", f"{len(df):,d}".replace(",", "."))
    col2.metric("Strecke", f"{df['km'].sum():,.0f} km")
    col3.metric("Aufstieg", f"{df['ascent'].sum():,.0f} m")
    col4.metric("Zeit", f"{df['hours'].sum():,.0f} h")


def _render_per_year(df: pd.DataFrame) -> None:
    """Kilometer und Anzahl Tracks je Jahr."""
    st.subheader("Je Jahr")
    year_df = df.dropna(subset=["year"]).copy()
    if year_df.empty:
        st.info("Keine Tracks mit Zeitstempel vorhanden.")
        return
    # Jahr als Text: sonst zeichnet das Diagramm eine durchgehende
    # Zahlenachse (2019,5 usw.) statt einer Kategorie je Jahr.
    year_df["year"] = year_df["year"].astype(int).astype(str)
    per_year = year_df.groupby("year").agg(
        km=("km", "sum"), Aufstieg=("ascent", "sum"), Tracks=("track_id", "count")
    )
    st.bar_chart(per_year["km"], y_label="km", x_label="Jahr")
    st.dataframe(
        per_year.rename(columns={"km": "Kilometer", "Aufstieg": "Aufstieg (m)"}).round(0),
        width="stretch",
    )


def _render_per_month(df: pd.DataFrame) -> None:
    """Kilometer je Monat für ein auswählbares Jahr."""
    st.subheader("Je Monat")
    years = sorted(df["year"].dropna().astype(int).unique().tolist(), reverse=True)
    if not years:
        st.info("Keine Tracks mit Zeitstempel vorhanden.")
        return

    selected_year = st.selectbox("Jahr", years, key="stats_year_select")
    year_df = df[df["year"] == selected_year]

    # Alle zwölf Monate anzeigen (auch die ohne Tracks), damit Lücken im
    # Jahresverlauf sichtbar werden statt einfach zu fehlen.
    per_month = (
        year_df.groupby("month")["km"].sum()
        .reindex(range(1, 13), fill_value=0.0)
    )
    per_month.index = _MONTH_NAMES
    st.bar_chart(per_month, y_label="km", x_label="Monat")


def _render_per_sport(df: pd.DataFrame) -> None:
    """Anzahl Tracks, Kilometer, Aufstiegsmeter und Zeit je Sportart."""
    st.subheader("Je Sportart")
    per_sport = (
        df.groupby("sport")
        .agg(
            Tracks=("track_id", "count"),
            Kilometer=("km", "sum"),
            Aufstieg=("ascent", "sum"),
            Stunden=("hours", "sum"),
        )
        .sort_values("Kilometer", ascending=False)
    )
    if per_sport.empty:
        st.info("Noch keine Tracks vorhanden.")
        return
    st.bar_chart(per_sport["Aufstieg"], y_label="Aufstieg (m)", x_label="Sportart")
    st.dataframe(
        per_sport.rename(columns={"Aufstieg": "Aufstieg (m)"}).round(1),
        width="stretch",
    )


def _render_heatmap() -> None:
    """
    Heatmap über die Punkte ALLER gespeicherten Tracks - zeigt auf einen
    Blick, welche Gegenden und Strecken wie häufig zurückgelegt wurden.

    Die Punkte werden ausgedünnt geladen und gecacht (siehe
    functions.load_heatmap_points); der Kartenausschnitt wird auf ihre
    Ausdehnung gesetzt.
    """
    st.subheader("Heatmap")
    st.caption(
        "Alle aufgezeichneten Punkte über sämtliche Tracks hinweg - "
        "unabhängig von den Filtern der Kartenseite."
    )

    points = load_heatmap_points(_HEATMAP_STRIDE)
    if not points:
        st.info("Noch keine Tracks vorhanden.")
        return

    lats = [lat for lat, _ in points]
    lons = [lon for _, lon in points]
    heat_map = folium.Map()
    heat_map.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])
    HeatMap(points, radius=8, blur=6, min_opacity=0.3).add_to(heat_map)

    # key: verhindert, dass die Karte bei jeder Interaktion auf der Seite
    # neu aufgebaut wird (siehe gleiche Begründung in map.py).
    st_folium(heat_map, width="stretch", height=600, key="stats_heatmap")
    st.caption(f"{len(points):,d} Punkte (jeder {_HEATMAP_STRIDE}.)".replace(",", "."))


def render_stats_page() -> None:
    """Baut die komplette Statistik-Seite auf."""
    meta = load_metadata()
    if meta.empty:
        st.info("Noch keine Tracks vorhanden. Lege zuerst welche in der Verwaltung an.")
        return

    df = _prepare(meta)

    _render_totals(df)
    st.divider()

    tab_time, tab_sport, tab_heat = st.tabs(["Zeitverlauf", "Sportarten", "Heatmap"])
    with tab_time:
        _render_per_year(df)
        st.divider()
        _render_per_month(df)
    with tab_sport:
        _render_per_sport(df)
    with tab_heat:
        _render_heatmap()


# Direkter Start zu Debug-Zwecken: `streamlit run stats.py`. Im
# Normalbetrieb wird render_stats_page() stattdessen von app.py über die
# Navigation aufgerufen.
if __name__ == "__main__":
    st.set_page_config(page_title="Statistik", layout="wide")
    render_stats_page()
