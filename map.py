"""
map.py
======
Gemeinsame Bausteine der Kartenseite ("Karte", ehemals "Karte (Sync)",
implementiert in map_linked.py) - Sidebar-Filter, Kennzahlen-Anzeige,
Info-Punkte-Verwaltung, Planungsmodus (Tourenplanung) sowie die
Höhenermittlung für Karte/Profil.

WICHTIG - dieses Modul ist KEINE eigene Seite mehr: Es enthält keine
render_*_page()-Funktion und wird von app.py nicht direkt in die
Navigation eingebunden. Die frühere Folium/Plotly-Kartenseite ("Karte")
wurde entfernt, weil map_linked.py (Leaflet + uPlot in einer einzigen
Browser-Komponente, ohne Server-Rerun bei Hover) dieselbe Funktionalität
flüssiger bietet und seitdem die einzige, jetzt "Karte" genannte
Kartenseite der App ist (siehe app.py sowie den Modul-Docstring von
map_linked.py). Dieses Modul lebt als reine Bibliothek weiter, weil
map_linked.py folgende Bausteine unverändert wiederverwendet:

- render_track_filters()    Sidebar-Filter (Sport/Land/Jahr/Jahreszeit +
                             Baumauswahl Jahr -> Monat -> Tour -> Track)
- render_planning_toggle()  Schalter "📐 Planung" in der Seitenleiste
- _render_kpis() / _render_planning_kpis() / _render_best_efforts()
                             Kennzahlen-Spalte (je Track bzw. je Teil im
                             Planungsmodus) samt Bestzeiten
- _render_notes_panel()     Verwaltung der Info-Punkte ("Punkte zur Tour")
- _resolve_map_profile_height() / _keyed_container()
                             Höhe von Karte + Profil (siehe Abschnitt
                             weiter unten), Container-Keys fürs Layout
- _nearest_point_index()    nächstgelegener Trackpunkt zu Klick-Koordinaten
                             (Info-Punkt per Kartenklick/Rechtsklick)
- _build_planning_export_zip() u.a.  GPX-Export des Planungsmodus

Planungsmodus - aktueller Stand nach Entfernen der alten Seite "Karte":
Unterteilungspunkte lassen sich HIER NICHT mehr per Klick setzen - diese
Interaktion lebte ausschließlich in der entfernten Folium/Plotly-Seite.
_render_planning_kpis() zeigt bereits gesetzte Punkte weiterhin an,
erlaubt ihr Löschen ("✕") und den ZIP-Export; map_linked.py zeichnet sie
zusätzlich in Karte und Höhenprofil ein.

Der Datenbankzugriff (load_metadata / load_track_files) liegt - wie aller
übrige Datenzugriff auch - in functions.py; dieses Modul enthält nur
UI-Code.

Performance-Hinweis: GPX-Dateien werden aus DuckDB geladen und mit GeoPandas
aufwendig nachbearbeitet (Distanz, Tempo, Steigung, ...). Da Streamlit bei
JEDER Nutzerinteraktion das komplette Skript neu ausführt, wird diese
Verarbeitung über st.cache_data gecacht - siehe process_track() in
functions.py.
"""

import io  # ZIP-Export im Planungsmodus (in-memory statt temporärer Dateien)
import zipfile  # ZIP-Export im Planungsmodus

import gpxpy  # GPX-Export der Teile/Punkte im Planungsmodus
import gpxpy.gpx
import numpy as np  # Numerische Hilfsfunktionen (Arrays, NaN-Handling)
import pandas as pd
import streamlit as st  # Web-UI-Framework
from streamlit_javascript import st_javascript  # Liest window.parent.innerHeight im Browser aus

from functions import (
    DEFAULT_MIN_ELEVATION_CHANGE_M,
    DEFAULT_MIN_SPEED_MOVING_KMH,
    build_note_waypoints,
    compute_ascent_descent,
    compute_best_efforts,
    compute_moving_time_s,
    delete_track_note,
    insert_track_note,
    load_metadata,
    update_track_note,
)


# --------------------------------------------------------------------------
# Höhe von Karte + Höhenprofil
# --------------------------------------------------------------------------
# Die Höhe wird IMMER automatisch aus der Fensterhöhe ermittelt (per
# JavaScript, siehe _resolve_map_profile_height) - keine Auswahl/Einstellung
# mehr dafür in der Seitenleiste. Damit entfällt zwar die Möglichkeit, die
# Höhe manuell zu fixieren, dafür bleibt die Einstellungen-Sektion schlanker
# und es gibt nur noch genau ein Verhalten zu pflegen/testen.
_DEFAULT_TOTAL_HEIGHT_PX = 1100
_MAX_TOTAL_HEIGHT_PX = 2200
_MIN_MAP_HEIGHT_PX = 300
_MIN_PROFILE_HEIGHT_PX = 150
# Anteil des Höhenprofils an der Gesamthöhe, entspricht ungefähr dem
# bisherigen festen Verhältnis 300/1100.
_PROFILE_HEIGHT_RATIO = 300 / 1100
# Abzug von der per JavaScript ermittelten Fensterhöhe (window.innerHeight)
# für Bereiche außerhalb des Karte/Profil-Containers: oberer/unterer
# Innenabstand der Seite, Rahmen des Containers, ggf. Planungsmodus-Hinweis.
_WINDOW_HEIGHT_OFFSET_PX = 220


def _split_map_profile_height(total_height_px: int) -> tuple[int, int]:
    """
    Teilt eine Gesamthöhe in Karten- und Profilhöhe auf (siehe
    _PROFILE_HEIGHT_RATIO), unter Einhaltung von Mindesthöhen je Element.
    """
    total_height_px = max(total_height_px, _MIN_MAP_HEIGHT_PX + _MIN_PROFILE_HEIGHT_PX)
    profile_height_px = max(_MIN_PROFILE_HEIGHT_PX, round(total_height_px * _PROFILE_HEIGHT_RATIO))
    map_height_px = max(_MIN_MAP_HEIGHT_PX, total_height_px - profile_height_px)
    return map_height_px, profile_height_px


def _keyed_container(key: str, border: bool = False):
    """
    st.container() mit CSS-Klasse 'st-key-<key>' - erlaubt es, einzelne
    Container (Karte, Profil, Kennzahlen-Spalte) gezielt per CSS
    anzusprechen. Aktuell ohne eigene Regeln genutzt; der Rückfall ohne
    'key' hält die Seite auf älteren Streamlit-Versionen lauffähig.
    """
    try:
        return st.container(border=border, key=key)
    except TypeError:
        return st.container(border=border)


def _resolve_map_profile_height() -> tuple[int, int]:
    """
    Ermittelt die zu verwendende Höhe für Karte + Höhenprofil als
    (map_height_px, profile_height_px) - per JavaScript aus der
    Browser-Fensterhöhe.

    Dazu wird per st_javascript() der Wert von window.parent.innerHeight
    aus dem Browser geholt.

    Wichtig: 'window.innerHeight' würde die Höhe des eigenen
    Komponenten-Iframes zurückliefern (die immer 0 ist), nicht die
    des Browser-Fensters. 'window.parent.innerHeight' greift dagegen auf das
    Elternfenster zu - das funktioniert, weil Streamlits Custom-Components-
    Iframes das Sandbox-Attribut 'allow-same-origin' tragen und damit
    same-origin-Zugriff auf window.parent haben.

    Die Komponente wertet das JS beim ersten Einbetten aus und löst dann
    einen Rerun aus - beim allerersten Aufruf (bevor das JS ausgewertet
    wurde) liefert sie 0 zurück und die Karte erscheint zunächst in der
    Standard-Höhe (_DEFAULT_TOTAL_HEIGHT_PX). Ab dem zweiten Rerun steht
    der echte Wert zur Verfügung.

    Nach einer Browser-Fenster-Grössenänderung wird der Wert beim nächsten
    Rerun (z.B. durch eine Nutzerinteraktion) aktualisiert, da st_javascript
    die Auswertung bei jeder neuen Komponentenmontierung wiederholt.
    """
    # window.parent.innerHeight = Höhe des Browser-Viewports (nicht des Iframes)
    window_height = st_javascript("window.parent.innerHeight", key="window_height_js")
    min_plausible = _MIN_MAP_HEIGHT_PX + _MIN_PROFILE_HEIGHT_PX + _WINDOW_HEIGHT_OFFSET_PX
    if isinstance(window_height, (int, float)) and window_height >= min_plausible:
        total_height_px = int(window_height) - _WINDOW_HEIGHT_OFFSET_PX
    else:
        total_height_px = _DEFAULT_TOTAL_HEIGHT_PX
    total_height_px = min(total_height_px, _MAX_TOTAL_HEIGHT_PX)
    return _split_map_profile_height(total_height_px)


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


def _tour_checkbox_key(year: int, tour_id) -> str:
    """
    Widget-Key der "alles auswählen"-Checkbox einer Tour innerhalb eines
    Jahres.

    Eine Tour erscheint im Baum genau EINMAL je Jahr (unter dem Monat
    ihres frühesten Tracks, siehe _render_track_tree) - auch dann, wenn
    ihre Etappen über einen Monatswechsel laufen. Der Monat ist deshalb
    bewusst nicht Teil des Schlüssels: Sonst hinge der Key davon ab, in
    welchem Monat die Tour gerade einsortiert wird, und die Auswahl ginge
    beim Filtern verloren.
    """
    return f"tour_select_{year}_{tour_id}"


def _tour_open_key(year: int, tour_id) -> str:
    """Sitzungs-Key, der merkt, ob eine Tour im Baum aufgeklappt ist."""
    return f"_tour_open_{year}_{tour_id}"


def _on_tour_expand(open_key: str) -> None:
    """Callback des ▸/▾-Knopfes: klappt eine Tour auf bzw. wieder zu."""
    st.session_state[open_key] = not st.session_state.get(open_key, False)


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


def _render_tour_group(year: int, tour_id, group: pd.DataFrame) -> None:
    """
    Zeichnet EINE Tour im Auswahlbaum: eine Zeile mit Aufklapp-Knopf und
    "alles auswählen"-Checkbox, darunter - nur wenn aufgeklappt - die
    einzelnen Tracks der Tour.

    Mehrtagestouren machen den Baum sonst sehr lang; eingeklappt steht je
    Tour nur eine Zeile, und der Normalfall "die ganze Tour ansehen" ist
    ein einziger Klick. Der Aufklapp-Knopf erscheint nur bei mehr als
    einem Track - bei einer Tour mit genau einem Track gäbe es darunter
    nichts zu zeigen, was die Zeile nicht schon sagt.

    Ein verschachtelter st.expander wäre hier nicht möglich: Der Baum
    steckt bereits in einem Jahres-Expander, und Streamlit erlaubt keine
    Expander im Expander. Deshalb der eigene Knopf mit Merker im
    Sitzungszustand (siehe _tour_open_key).
    """
    tour_title = group["tour_title"].iloc[0]
    track_ids = group["track_id"].tolist()
    tour_key = _tour_checkbox_key(year, tour_id)
    open_key = _tour_open_key(year, tour_id)
    is_open = st.session_state.get(open_key, False)

    # Zeitraum der Tour als kurze Zusatzinfo (z.B. "12.–14.07.").
    days = group["time_start"].dropna()
    if len(days):
        first, last = days.min(), days.max()
        span = (
            f"{first:%d.%m.}"
            if first.date() == last.date()
            else f"{first:%d.%m.}–{last:%d.%m.}"
        )
    else:
        span = ""

    label = f"🧭 {tour_title} ({len(track_ids)}){f' · {span}' if span else ''}"
    initial = all(
        st.session_state.get(_track_checkbox_key(tid), False) for tid in track_ids
    )

    if len(track_ids) > 1:
        col_btn, col_box = st.columns([1, 8], gap="small", vertical_alignment="center")
        with col_btn:
            st.button(
                "▾" if is_open else "▸",
                key=f"btn{open_key}",
                on_click=_on_tour_expand,
                args=(open_key,),
                help="Tracks der Tour ein-/ausblenden",
            )
        box_container = col_box
    else:
        is_open = False
        box_container = st.container()

    with box_container:
        _persistent_checkbox(
            label,
            key=tour_key,
            default=initial,
            on_change=_on_tour_toggle,
            args=(tour_key, track_ids),
        )

    if not is_open:
        return

    for _, row in group.iterrows():
        day = f"{row['time_start']:%d.%m.} " if pd.notna(row["time_start"]) else ""
        _persistent_checkbox(
            f"　↳ {day}{row['track_title']}",
            key=_track_checkbox_key(row["track_id"]),
            default=False,
            on_change=_on_track_toggle,
            args=(tour_key, track_ids),
        )


def _render_track_tree(meta: pd.DataFrame) -> list:
    """
    Baut die Track-Auswahl als verschachtelte Struktur auf:
    Jahr (Expander) -> Monat -> Tour (eingeklappt) bzw. einzelner Track.

    Eine Tour erscheint je Jahr genau einmal, einsortiert unter dem Monat
    ihres frühesten Tracks - auch wenn ihre Etappen über einen
    Monatswechsel laufen. Sie ist zunächst eingeklappt und zeigt nur eine
    Zeile mit Titel, Etappenzahl und Zeitraum (siehe _render_tour_group);
    aufgeklappt darunter die einzelnen Tracks. Tracks ohne Tour stehen
    direkt unter ihrem Monat.

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
            # Monat, in dem jede Tour dieses Jahres einsortiert wird: der
            # ihres frühesten Tracks. Ohne diese Zuordnung würde eine über
            # den Monatswechsel laufende Tour in beiden Monaten auftauchen.
            with_tour = year_df[year_df["tour_id"].notna()]
            tour_month = (
                with_tour.groupby("tour_id")["month"].min().to_dict() if len(with_tour) else {}
            )

            months = sorted(year_df["month"].dropna().unique().tolist(), reverse=True)
            for month in months:
                month_df = year_df[year_df["month"] == month]
                without_tour = month_df[month_df["tour_id"].isna()]
                tours_here = [tid for tid, m in tour_month.items() if m == month]
                if not tours_here and without_tour.empty:
                    continue

                st.markdown(f"**{_MONTH_NAMES[int(month) - 1]}**")

                # Touren dieses Monats - mit ALLEN ihren Tracks des Jahres,
                # nicht nur denen des Monats (s.o.).
                for tour_id in tours_here:
                    group = with_tour[with_tour["tour_id"] == tour_id].sort_values("time_start")
                    _render_tour_group(int(year), tour_id, group)

                # Tracks ohne Tour: einzeln, direkt unter dem Monat.
                for _, row in without_tour.iterrows():
                    _persistent_checkbox(
                        row["track_title"],
                        key=_track_checkbox_key(row["track_id"]),
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
    """Formatiert eine Dauer in Sekunden als 'H:MM h'-Text, z.B. '3:45 h'."""
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


def _format_speed(km_per_h: float) -> str:
    """Formatiert eine Geschwindigkeit, z.B. '12.3 km/h'."""
    if pd.isna(km_per_h) or np.isinf(km_per_h):
        return "–"
    return f"{km_per_h:,.1f} km/h"


def _safe_speed_kmh(distance_m, seconds):
    """
    Durchschnittstempo in km/h aus Strecke und Zeit - arbeitet sowohl mit
    einzelnen Werten (Summe über alle Tracks) als auch mit pandas-Serien
    (Einzelwerte je Track). Eine Dauer von 0 oder fehlende Werte ergeben
    NaN statt einer Division durch 0 bzw. "inf".
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        speed = (
            np.asarray(distance_m, dtype=float)
            / np.asarray(seconds, dtype=float)
            * 3.6
        )
    speed = np.where(np.isfinite(speed), speed, np.nan)
    if np.ndim(speed) == 0:
        return float(speed)
    return pd.Series(speed, index=getattr(distance_m, "index", None))


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

    Der Schieberegler für die Spaltenbreite ('kpi_col_width_pct') wird NICHT
    mehr hier gerendert, sondern zusammen mit den anderen
    Anzeigeeinstellungen im aufklappbaren Bereich der Seitenleiste (siehe
    map_linked.render_linked_map_page) - der hier verwendete Wert kommt
    also von dort.
    """
    st.subheader(subheader)

    descent_abs = df["track_descent_m"].abs()

    # Abgeleitete Kennzahlen. Die Gesamtwerte werden aus den SUMMEN
    # gebildet (Gesamtstrecke / Gesamtzeit), nicht als Mittelwert der
    # Einzel-Durchschnitte - sonst zählte ein 2-km-Track genauso viel wie
    # ein 60-km-Track.
    total_distance = df["track_distance_m"].sum()
    total_time = df["track_time_s"].sum()
    total_moving = df["track_time_moving_s"].sum()
    pause_per_track = df["track_time_s"] - df["track_time_moving_s"]

    # (Label, Summen-/Aggregatwert über alle Tracks, Formatierfunktion,
    # Werte je einzelnem Track in derselben Reihenfolge wie df)
    kpi_rows = [
        ("Länge", total_distance, _format_distance_km, df["track_distance_m"]),
        ("Zeit", total_time, _format_duration, df["track_time_s"]),
        ("Zeit in Bewegung", total_moving, _format_duration, df["track_time_moving_s"]),
        ("Pause", total_time - total_moving, _format_duration, pause_per_track),
        (
            "Ø Tempo",
            _safe_speed_kmh(total_distance, total_time),
            _format_speed,
            _safe_speed_kmh(df["track_distance_m"], df["track_time_s"]),
        ),
        (
            "Ø Tempo in Bewegung",
            _safe_speed_kmh(total_distance, total_moving),
            _format_speed,
            _safe_speed_kmh(df["track_distance_m"], df["track_time_moving_s"]),
        ),
        ("Aufstieg", df["track_ascent_m"].sum(), _format_meters, df["track_ascent_m"]),
        ("Abstieg", descent_abs.sum(), _format_meters, descent_abs),
        ("Min. Höhe", df["elevation_min"].min(), _format_meters, df["elevation_min"]),
        ("Max. Höhe", df["elevation_max"].max(), _format_meters, df["elevation_max"]),
    ]

    # [3, 2]: Kennzahlen-Box links, daneben die kleingedruckten
    # Einzelwerte je Track.
    for label, total_value, formatter, per_track_values in kpi_rows:
        st.divider()
        col_box, col_tracks = st.columns([3, 2])
        with col_box:
            st.metric(label, formatter(total_value))
        with col_tracks:
            for title, value in zip(df["track_title"], per_track_values):
                st.caption(f"{title}: {formatter(value)}")


def _render_best_efforts(gdf: pd.DataFrame) -> None:
    """
    Zeigt die Bestzeiten innerhalb EINES Tracks (siehe
    functions.compute_best_efforts): je Standarddistanz (1 km, 5 km, ...)
    der schnellste Abschnitt irgendwo im Track, mit Tempo und der Stelle,
    an der dieser Abschnitt beginnt.

    Wird nur angezeigt, wenn genau ein Track ausgewählt ist - über mehrere
    Tracks hinweg wäre eine "Bestzeit" nicht sinnvoll definiert, da die
    Abschnitte dann quer über getrennte Aufzeichnungen laufen würden.
    """
    efforts = compute_best_efforts(gdf)
    with st.expander("🏅 Bestzeiten", expanded=False):
        if efforts.empty:
            st.caption(
                "Keine Auswertung möglich - der Track ist kürzer als 1 km "
                "oder enthält keine Zeitstempel."
            )
            return
        for row in efforts.itertuples(index=False):
            st.metric(
                row.label,
                _format_duration(row.time_s),
                help=f"Schnellste {row.label} dieses Tracks",
            )
            st.caption(
                f"{_format_speed(row.speed_kmh)} · ab km {row.start_km:.1f}"
            )


# --------------------------------------------------------------------------
# Info-Punkte ("Punkte zur Tour")
# --------------------------------------------------------------------------
# Dauerhaft gespeicherte Anmerkungen zu einem Track (Hütte, Aussicht,
# Wasserstelle, Gefahrenstelle ...), siehe functions._TRACK_NOTES_DDL.
#
# Bewusste Abgrenzung zu den Unterteilungspunkten des Planungsmodus:
#   - Unterteilungspunkte TRENNEN den Track in Teile, liegen immer exakt
#     auf einem Trackpunkt und leben nur im Sitzungszustand.
#   - Info-Punkte TRENNEN NICHTS. Sie dürfen neben dem Track liegen (die
#     Hütte steht selten genau auf der Spur) und bleiben gespeichert.
# Angelegt werden sie in beiden Betriebsarten - normal wie im
# Planungsmodus; angezeigt werden sie immer, auf Karte UND Höhenprofil.


def _note_position_on_track(gdf: pd.DataFrame, note: pd.Series) -> tuple[int, float, float]:
    """
    Liefert zu einem Info-Punkt (index, distance_m, elevation_m) bezogen
    auf das verarbeitete Track-DataFrame.

    Der gespeicherte 'point_index' zeigt auf den nächstgelegenen
    Trackpunkt; daraus ergibt sich die Stelle auf der x-Achse des
    Höhenprofils. Er wird defensiv in den gültigen Bereich geklemmt -
    wurde ein Track nach dem Anlegen des Punkts neu hochgeladen und ist
    dabei kürzer geworden, soll der Punkt trotzdem noch dargestellt
    werden. Fehlt eine eigene Höhe, wird die des Trackpunkts verwendet,
    damit der Punkt im Profil auf der Kurve liegt.
    """
    idx = 0 if pd.isna(note["point_index"]) else int(note["point_index"])
    idx = max(0, min(idx, len(gdf) - 1))
    elevation = float(note["ele"]) if pd.notna(note["ele"]) else float(gdf["ele"].iloc[idx])
    return idx, float(gdf["distance"].iloc[idx]), elevation


def _note_default_position(gdf: pd.DataFrame, track_id) -> tuple[float, float]:
    """
    Vorbelegung der Koordinaten im Formular "Punkt hinzufügen", in dieser
    Reihenfolge: zuletzt auf der Karte angeklickte Stelle (siehe
    '_note_position'), sonst der zuletzt im Höhenprofil gewählte Punkt,
    sonst der Startpunkt des Tracks.
    """
    pending = st.session_state.get("_note_position")
    if pending:
        return float(pending["lat"]), float(pending["lon"])
    selected = st.session_state.get("selected_point")
    if selected and selected["track_id"] == track_id:
        return float(selected["lat"]), float(selected["lon"])
    return float(gdf["lat"].iloc[0]), float(gdf["lon"].iloc[0])


def _render_notes_panel(
    notes: pd.DataFrame,
    gdf: pd.DataFrame | None = None,
    track_id=None,
    track_title: str | None = None,
    planning_mode: bool = False,
    allow_map_click: bool = True,
) -> None:
    """
    Zeichnet die Verwaltung der Info-Punkte in die Kennzahlen-Spalte:
    Liste der vorhandenen Punkte (je Punkt ein Aufklapp-Feld zum
    Bearbeiten/Löschen) und darunter das Formular für einen neuen Punkt.

    Bearbeiten und Anlegen setzen genau EINEN ausgewählten Track voraus
    ('gdf' ist dann dessen verarbeitetes DataFrame): Ein Punkt gehört zu
    einem Track, und für die Stelle im Höhenprofil braucht es dessen
    Punktfolge. Sind mehrere Tracks ausgewählt, werden die Punkte nur
    aufgelistet - auf Karte und Profil erscheinen sie trotzdem alle.

    'allow_map_click' blendet die Option "Position per Kartenklick" aus.
    Der einzige verbleibende Aufrufer (map_linked.py) setzt sie auf False:
    Seine Leaflet/uPlot-Komponente meldet Klicks nicht an Streamlit zurück
    (siehe Modul-Docstring dort) - ein neuer Info-Punkt lässt sich dort
    stattdessen per Rechtsklick-Overlay anlegen (eigener Rückkanal, siehe
    map_linked._handle_note_bridge), oder hier über die Koordinatenfelder.
    """
    st.divider()
    st.caption("📍 Punkte zur Tour")

    if notes.empty:
        st.caption("– keine –")

    for _, note in notes.iterrows():
        editable = gdf is not None and note["track_id"] == track_id
        if not editable:
            st.caption(f"📍 {note['note_title'] or 'Punkt'}")
            continue

        idx, distance_m, _ = _note_position_on_track(gdf, note)
        note_id = str(note["note_id"])
        with st.expander(f"📍 {note['note_title'] or 'Punkt'} · {distance_m / 1000:.1f} km"):
            new_title = st.text_input("Titel", value=note["note_title"] or "", key=f"nt_{note_id}")
            new_text = st.text_area(
                "Beschreibung",
                value="" if pd.isna(note["note_text"]) else note["note_text"],
                key=f"nx_{note_id}",
                height=80,
            )
            col_lat, col_lon = st.columns(2)
            new_lat = col_lat.number_input(
                "Breite", value=float(note["lat"]), format="%.6f", step=0.0001, key=f"na_{note_id}"
            )
            new_lon = col_lon.number_input(
                "Länge", value=float(note["lon"]), format="%.6f", step=0.0001, key=f"no_{note_id}"
            )
            col_save, col_del = st.columns(2)
            if col_save.button("Speichern", key=f"ns_{note_id}", width="stretch"):
                # Position geändert -> nächstgelegenen Trackpunkt (und damit
                # die Stelle im Höhenprofil) neu bestimmen.
                new_index = _nearest_point_index(gdf, new_lat, new_lon)
                update_track_note(
                    note_id,
                    new_title.strip() or "Punkt",
                    new_text.strip(),
                    new_lat,
                    new_lon,
                    float(gdf["ele"].iloc[new_index]),
                    new_index,
                )
                st.rerun()
            if col_del.button("Löschen", key=f"nd_{note_id}", width="stretch"):
                delete_track_note(note_id)
                st.rerun()

    if gdf is None:
        st.caption(
            "Zum Anlegen oder Ändern genau einen Track auswählen."
        )
        return

    # ------------------------------------------------------------------
    # Neuer Punkt
    # ------------------------------------------------------------------
    if allow_map_click:
        st.checkbox(
            "Position per Kartenklick",
            key="note_click_mode",
            help=(
                "Ist dies aktiv, übernimmt ein Klick auf die Karte die "
                "Koordinaten in das Formular unten - der Punkt muss dabei NICHT "
                "auf dem Track liegen. Im Planungsmodus hat diese Einstellung "
                "Vorrang: Der Kartenklick setzt dann keinen Trennpunkt "
                "mehr (das geht weiterhin über das Höhenprofil)."
            ),
        )
    lat_default, lon_default = _note_default_position(gdf, track_id)
    add_label = f"➕ Punkt hinzufügen{f' – {track_title}' if track_title else ''}"
    with st.expander(add_label, expanded=bool(st.session_state.get("_note_position"))):
        # Bewusst ohne 'key' an den Eingabefeldern: So übernimmt das
        # Formular die per Kartenklick geänderte Vorbelegung (value=) beim
        # nächsten Rerun automatisch.
        with st.form("note_add_form", clear_on_submit=True):
            title = st.text_input("Titel", placeholder="z.B. Hütte, Aussicht, Abzweig")
            text = st.text_area("Beschreibung", height=80)
            col_lat, col_lon = st.columns(2)
            lat = col_lat.number_input("Breite", value=lat_default, format="%.6f", step=0.0001)
            lon = col_lon.number_input("Länge", value=lon_default, format="%.6f", step=0.0001)
            if st.form_submit_button("Hinzufügen", width="stretch"):
                point_index = _nearest_point_index(gdf, lat, lon)
                insert_track_note(
                    track_id,
                    title.strip() or "Punkt",
                    text.strip(),
                    lat,
                    lon,
                    float(gdf["ele"].iloc[point_index]),
                    point_index,
                    note_kind="planning" if planning_mode else "info",
                )
                st.session_state["_note_position"] = None
                st.rerun()


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
    einem Klick auf der Karte am nächsten liegt - genutzt für Info-Punkte
    (Kartenklick bzw. Rechtsklick-Rückkanal, siehe _render_notes_panel und
    map_linked._handle_note_bridge).

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


def _gdf_slice_to_gpx_xml(
    gdf: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    name: str,
    notes: pd.DataFrame | None = None,
) -> str:
    """Baut aus den Punkten start_idx bis end_idx (inklusive) eines
    verarbeiteten Track-DataFrames eine eigenständige GPX-Datei (ein
    <trk> mit genau einem <trkseg>) und gibt deren XML-Text zurück.

    'notes' sind die Info-Punkte des Tracks; übernommen werden nur die,
    deren nächstgelegener Trackpunkt in diesem Abschnitt liegt - so
    wandert jeder Punkt in genau die Teildatei, zu der er gehört."""
    gpx = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack(name=name)
    gpx.tracks.append(track)
    if notes is not None and not notes.empty:
        in_segment = notes["point_index"].between(start_idx, end_idx)
        gpx.waypoints.extend(build_note_waypoints(notes[in_segment]))
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
    notes: pd.DataFrame | None = None,
) -> bytes:
    """
    Baut die ZIP-Datei für den Export-Button des Planungsmodus: je
    Teil eine eigenständige GPX-Datei (_gdf_slice_to_gpx_xml) sowie -
    sofern mindestens ein Unterteilungspunkt gesetzt ist - eine weitere
    GPX-Datei mit allen Punkten als Wegpunkte (_split_points_to_gpx_xml).
    Gibt die fertige ZIP-Datei als Bytes zurück (für st.download_button).

    'notes' sind die Info-Punkte des Tracks. Sie landen als Wegpunkte in
    derjenigen Teildatei, in deren Abschnitt sie liegen (siehe
    _gdf_slice_to_gpx_xml), und zusätzlich vollständig in einer eigenen
    Datei - damit gehen sie beim Export nie verloren, egal ob das
    Zielprogramm die Teile einzeln oder gesammelt einliest.
    """
    base_name = _slugify_filename(track_title)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, (start_idx, end_idx) in enumerate(bounds, start=1):
            xml = _gdf_slice_to_gpx_xml(
                gdf, start_idx, end_idx, name=f"{track_title} – Teil {n}", notes=notes
            )
            zf.writestr(f"{base_name}_Teil_{n:02d}.gpx", xml)
        if split_indices:
            xml_points = _split_points_to_gpx_xml(gdf, split_indices)
            zf.writestr(f"{base_name}_trennpunkte.gpx", xml_points)
        if notes is not None and not notes.empty:
            gpx_notes = gpxpy.gpx.GPX()
            gpx_notes.waypoints.extend(build_note_waypoints(notes))
            zf.writestr(f"{base_name}_punkte.gpx", gpx_notes.to_xml())
    return buffer.getvalue()


def _render_planning_kpis(
    gdf: pd.DataFrame,
    track_id: str,
    track_title: str,
    notes: pd.DataFrame | None = None,
) -> None:
    """
    Planungsmodus-Variante von _render_kpis(): zeigt statt der Kennzahlen
    je Track die Kennzahlen je Teil eines einzelnen Tracks (der
    Planungsmodus ist nur bei genau einem ausgewählten Track aktivierbar -
    siehe render_planning_toggle). Dafür wird ein DataFrame mit einer Zeile
    je Teil gebaut (Spalten wie eine normale Track-Zeile, siehe
    _summarize_segment) und direkt an _render_kpis() übergeben - dadurch
    bleiben Formatierung und KPI-Zeilen (inkl. künftiger Änderungen daran)
    automatisch zwischen Track- und Teils-Ansicht konsistent.

    Darunter folgen die Liste der gesetzten Unterteilungspunkte (je mit
    Lösch-Button) sowie der Export-Button (siehe
    _build_planning_export_zip) - "Export unter KPIs" im Planungsmodus.
    Neue Punkte lassen sich hier NICHT setzen (siehe Modul-Docstring) -
    nur anzeigen, löschen und exportieren.

    'gdf' ist das bereits verarbeitete Track-DataFrame (siehe
    functions.process_track) - wird von map_linked.render_linked_map_page()
    vorab EINMAL berechnet und für Kennzahlen, Karte UND Höhenprofil
    wiederverwendet, damit der (recht teure) GPX-Aufbereitungsschritt nicht
    mehrfach pro Seitenaufruf läuft.
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
    st.caption("Trennpunkte")
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
    zip_bytes = _build_planning_export_zip(gdf, track_title, bounds, split_indices, notes)
    st.download_button(
        "📦 Export",
        data=zip_bytes,
        file_name=f"{_slugify_filename(track_title)}_planung.zip",
        mime="application/zip",
        key="planning_export_button",
        width="stretch",
        help=(
            "Lädt eine ZIP-Datei herunter: je eine GPX-Datei pro Teil "
            "(inkl. der Info-Punkte des jeweiligen Abschnitts), eine "
            "GPX-Datei mit den Trennpunkten sowie eine mit allen "
            "Info-Punkten als Wegpunkte."
        ),
    )


def render_track_filters() -> pd.DataFrame:
    """
    Rendert die komplette Track-Auswahl in der Seitenleiste und liefert die
    Metadaten der ausgewählten Tracks zurück.

    Der Ablauf ist:

        1. Metadaten aller Tracks laden (load_metadata) und Jahr, Monat,
           Jahreszeit sowie die Länderliste je Track ableiten.
        2. Vier kaskadierende Pills-Filter: Sport -> Land -> Jahr ->
           Jahreszeit. Jede Stufe zeigt nur noch Optionen, die zur bisherigen
           Auswahl passen.
        3. Aufklappbare Baum-Auswahl Jahr -> Monat -> Tour -> Track
           (_render_track_tree).

    Sind keine Tracks vorhanden oder ist keiner ausgewählt, wird eine
    Meldung in der Seitenleiste ausgegeben und ein LEERES DataFrame
    zurückgegeben. Bewusst kein st.stop(): Das würde den kompletten
    Skriptdurchlauf abbrechen, sodass auch die erst nach der Seite
    gerenderten Elemente von app.py - insbesondere der "🚪 Beenden"-Knopf -
    nicht mehr erscheinen. Die aufrufende Seite prüft stattdessen
    `meta.empty` und bricht nur ihren eigenen Aufbau ab (siehe
    render_no_selection_hint()).

    Bewusst als eigene, öffentliche Funktion herausgelöst und von
    map_linked.render_linked_map_page() (Seite "Karte") wiederverwendet,
    statt den Filter-Code dort zu duplizieren.
    """
    with st.sidebar:
        meta = load_metadata()

        if meta["track_id"].dropna().empty:
            st.info("Noch keine Tracks vorhanden. Lege zuerst welche in der Verwaltung an.")
            return meta.iloc[0:0]

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
            st.warning("Bitte mindestens einen Track auswählen.")
            return meta.iloc[0:0]
        meta = meta[meta["track_id"].isin(selected_tracks)]
        return meta


def render_no_selection_hint() -> None:
    """
    Hinweis im Hauptbereich, wenn render_track_filters() nichts geliefert
    hat (keine Tracks vorhanden oder keiner ausgewählt).

    Wird von beiden Kartenseiten unmittelbar vor dem vorzeitigen Verlassen
    der jeweiligen render_*_page()-Funktion aufgerufen, damit der
    Hauptbereich nicht einfach leer bleibt. Der restliche Skriptdurchlauf
    (und damit der "🚪 Beenden"-Knopf in app.py) läuft normal weiter.
    """
    st.info(
        "Keine Auswahl: In der Seitenleiste links mindestens einen Track "
        "auswählen, um Karte, Höhenprofil und Kennzahlen zu sehen."
    )


def render_planning_toggle(selected_tracks: list) -> bool:
    """
    Zeichnet den Schalter "📐 Planung" in die Seitenleiste und meldet
    zurück, ob der Planungsmodus aktiv ist.

    Der Modus ist nur bei GENAU einem ausgewählten Track aktivierbar, da
    sich nur ein einzelner Track sinnvoll in Teile unterteilen lässt (siehe
    Modul-Docstring, Punkt 4). Fällt die Bedingung weg (z.B. weitere Tracks
    dazu ausgewählt, während der Modus bereits aktiv war), wird er
    automatisch wieder deaktiviert, statt nur das Steuerelement zu sperren.

    HINWEIS: Unterteilungspunkte lassen sich in der aktuellen Oberfläche
    NICHT mehr per Klick SETZEN - diese Interaktion lebte ausschließlich in
    der inzwischen entfernten Folium/Plotly-Seite "Karte". Bereits
    gesetzte Punkte (aus einer laufenden Sitzung) lassen sich weiterhin in
    der Kennzahlen-Spalte einsehen, über "✕" löschen und exportieren (siehe
    _render_planning_kpis) - Karte und Höhenprofil auf map_linked.py zeigen
    sie zusätzlich an.
    """
    with st.sidebar:
        st.divider()
        single_track_selected = len(selected_tracks) == 1
        if not single_track_selected:
            st.session_state.planning_mode = False
        st.toggle(
            "📐 Planung",
            key="planning_mode",
            disabled=not single_track_selected,
            help=(
                "Zeigt den ausgewählten Track in Teilen, mit Kennzahlen je "
                "Teil und Export als ZIP. Dafür muss genau ein Track "
                "ausgewählt sein."
            ),
        )
    return bool(st.session_state.get("planning_mode")) and single_track_selected

