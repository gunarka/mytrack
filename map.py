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
5. Info-Punkte ("Punkte zur Tour"): dauerhaft gespeicherte Anmerkungen zu
   einem Track (Hütte, Aussicht, Abzweig, ...). Anders als die
   Unterteilungspunkte aus Punkt 4 trennen sie den Track NICHT und müssen
   auch nicht auf ihm liegen; sie werden in Karte (blauer Marker) und
   Höhenprofil (blaue Raute) angezeigt und beim Export als Wegpunkte an
   den zugehörigen Track angehängt. Angelegt/bearbeitet werden sie in der
   Kennzahlen-Spalte (siehe _render_notes_panel), die Position wahlweise
   per Kartenklick oder über die Koordinatenfelder.
6. Höhe von Karte + Profil: drei Modi (Fenster füllen per CSS, Fensterhöhe
   per JavaScript messen, fester Pixelwert) - siehe Abschnitt "Höhe von
   Karte + Höhenprofil" weiter unten.

Der Datenbankzugriff (load_metadata / load_track_files) liegt - wie aller
übrige Datenzugriff auch - in functions.py; dieses Modul enthält nur
UI-Code.

Performance-Hinweis: GPX-Dateien werden aus DuckDB geladen und mit GeoPandas
aufwendig nachbearbeitet (Distanz, Tempo, Steigung, ...). Da Streamlit bei
JEDER Nutzerinteraktion das komplette Skript neu ausführt (auch bei einem
einfachen Klick im Profil), wird diese Verarbeitung über st.cache_data
gecacht - siehe process_track() in functions.py.
"""

import hashlib  # Stabile Track-Signatur für den st_folium-Key (kein Python-hash(), da PYTHONHASHSEED)
import io  # ZIP-Export im Planungsmodus (in-memory statt temporärer Dateien)
import zipfile  # ZIP-Export im Planungsmodus
from html import escape  # Maskiert Nutzertexte in Karten-Popups/Hovertexten

import folium  # Erzeugt die interaktive Leaflet-Karte
from folium.plugins import Fullscreen  # Vollbild-Schaltfläche der Karte
import branca.colormap as cm  # Farbskala für die Karten-Einfärbung
import gpxpy  # GPX-Export der Teile/Punkte im Planungsmodus
import gpxpy.gpx
import numpy as np  # Numerische Hilfsfunktionen (Arrays, NaN-Handling)
import pandas as pd
import plotly.graph_objects as go  # Höhenprofil-Diagramm
import streamlit as st  # Web-UI-Framework
from streamlit_folium import st_folium  # Rendert eine Folium-Karte in Streamlit
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
    load_track_files,
    load_track_notes,
    process_track,
    update_track_note,
)


# --------------------------------------------------------------------------
# Höhe von Karte + Höhenprofil
# --------------------------------------------------------------------------
# Drei Modi, einstellbar in der Seitenleiste (siehe _render_height_settings):
#
#   "fill"   (Standard) - Karte und Profil füllen gemeinsam die Fensterhöhe.
#            Umgesetzt rein über CSS (siehe _FILL_CSS): Der umgebende
#            Container bekommt 'height: calc(100vh - ...)', das Profil
#            behält seine Pixelhöhe, die Karte nimmt per Flexbox den Rest.
#            Das ist der zuverlässige Weg, weil er OHNE Umweg über den
#            Server funktioniert: sofort beim ersten Rendern, und beim
#            Ändern der Fenstergröße zieht die Karte live mit (Leaflet und
#            uPlot reagieren von sich aus auf die Größenänderung ihres
#            iframes). Die an Python übergebenen Pixelhöhen sind in diesem
#            Modus nur noch Startwerte für den iframe.
#   "window" - alter Weg: die Fensterhöhe wird per JavaScript ausgelesen und
#            als Pixelwert zurück an Python gegeben. Braucht immer einen
#            zusätzlichen Rerun und aktualisiert sich nach einer
#            Fenster-Größenänderung erst bei der nächsten Interaktion;
#            bleibt als Ausweichweg erhalten.
#   "manual" - feste Gesamthöhe per Schieberegler.
_DEFAULT_TOTAL_HEIGHT_PX = 1100
_MAX_TOTAL_HEIGHT_PX = 2200
_MIN_MAP_HEIGHT_PX = 300
_MIN_PROFILE_HEIGHT_PX = 150
# Höhe des Höhenprofils im Modus "fill" (die Karte bekommt den Rest).
_DEFAULT_PROFILE_HEIGHT_PX = 300
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


def _height_mode() -> str:
    """Aktuell gewählter Höhen-Modus ("fill" | "window" | "manual")."""
    return st.session_state.get("map_profile_height_mode", "fill")


def _keyed_container(key: str, border: bool = False):
    """
    st.container() mit CSS-Klasse 'st-key-<key>' - darüber greifen die
    Regeln aus _FILL_CSS gezielt auf genau diesen Container zu.

    Der Rückfall ohne 'key' hält die Seite auf älteren Streamlit-Versionen
    lauffähig (dann bleibt lediglich der Füllmodus wirkungslos).
    """
    try:
        return st.container(border=border, key=key)
    except TypeError:
        return st.container(border=border)


# CSS des Modus "fill" (siehe oben). Bewusst eng auf die drei Container-Keys
# begrenzt, damit keine andere Stelle der App davon erfasst wird:
#   mp_box     - umgebender Rahmen: exakt fensterhoch, Inhalt als Flex-Spalte
#   mp_map     - Karte: nimmt den verbleibenden Platz (flex: 1) und gibt ihn
#                bis zum iframe durch (Streamlit schachtelt mehrere <div>)
#   mp_profile - Höhenprofil: behält seine Pixelhöhe
#   mp_kpis    - Kennzahlen-Spalte: scrollt bei Bedarf in sich selbst,
#                statt die Seite länger als das Fenster zu machen
_FILL_CSS = """
<style>
.st-key-mp_box { height: calc(100vh - 2rem); }
.st-key-mp_box > div { height: 100%; }
.st-key-mp_map { flex: 1 1 auto; min-height: 240px; }
.st-key-mp_map > div,
.st-key-mp_map [data-testid="stElementContainer"],
.st-key-mp_map [data-testid="stElementContainer"] > div,
.st-key-mp_map iframe { height: 100% !important; }
.st-key-mp_profile { flex: 0 0 auto; }
.st-key-mp_kpis { max-height: calc(100vh - 2rem); overflow-y: auto; }
</style>
"""


def _render_fill_css() -> None:
    """Gibt das CSS des Füllmodus aus - nur, wenn dieser aktiv ist."""
    if _height_mode() == "fill":
        st.markdown(_FILL_CSS, unsafe_allow_html=True)


def _render_height_settings() -> None:
    """
    Zeichnet die Höhen-Einstellung (Modus + passender Schieberegler) in die
    Seitenleiste. Von BEIDEN Kartenseiten genutzt, damit dort dieselben
    Optionen mit denselben Widget-Keys stehen und die Einstellung den
    Seitenwechsel übersteht.
    """
    labels = {
        "fill": "Fenster füllen",
        "window": "Fensterhöhe messen (JS)",
        "manual": "Manuell (px)",
    }
    st.radio(
        "Höhe Karte + Profil",
        options=list(labels.keys()),
        index=0,
        key="map_profile_height_mode",
        format_func=lambda x: labels[x],
        help=(
            "'Fenster füllen': Karte und Profil füllen die Fensterhöhe "
            "vollständig aus und passen sich beim Ändern der Fenstergröße "
            "sofort an (reines CSS, kein Neuladen). "
            "'Fensterhöhe messen': ermittelt die Fensterhöhe per JavaScript "
            "und rechnet daraus feste Pixelwerte - aktualisiert sich erst "
            "beim nächsten Rerun. "
            "'Manuell': feste Gesamthöhe."
        ),
    )
    if _height_mode() == "fill":
        st.slider(
            "Höhe Höhenprofil (px)",
            min_value=_MIN_PROFILE_HEIGHT_PX,
            max_value=600,
            step=10,
            value=_DEFAULT_PROFILE_HEIGHT_PX,
            key="map_profile_height_px",
            help="Die Karte darüber bekommt den restlichen Platz bis zum Fensterrand.",
        )
    elif _height_mode() == "manual":
        st.slider(
            "Höhe Karte + Profil (px)",
            min_value=_MIN_MAP_HEIGHT_PX + _MIN_PROFILE_HEIGHT_PX,
            max_value=_MAX_TOTAL_HEIGHT_PX,
            step=100,
            value=_DEFAULT_TOTAL_HEIGHT_PX,
            key="map_profile_total_height_px",
            help="Gesamthöhe von Karte und Höhenprofil zusammen, in Pixeln.",
        )


def _resolve_map_profile_height() -> tuple[int, int]:
    """
    Ermittelt die zu verwendende Höhe für Karte + Höhenprofil als
    (map_height_px, profile_height_px) - je nach Modus 'fill', 'window'
    oder 'manual' (siehe _render_height_settings).

    Im Modus "fill" sind diese Werte nur Startwerte: Die tatsächliche Höhe
    bestimmt anschließend das CSS (_FILL_CSS). Das Höhenprofil behält
    dabei genau den hier gelieferten Pixelwert.

    Im Modus "window" wird per st_javascript() der Wert von
    window.parent.innerHeight aus dem Browser geholt.

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
    Für eine regelmässige Aktualisierung kann der Schieberegler 'Höhe Karte
    + Profil' auf 'Manuell' umgeschaltet werden.
    """
    mode = _height_mode()

    if mode == "fill":
        profile_height_px = int(
            st.session_state.get("map_profile_height_px", _DEFAULT_PROFILE_HEIGHT_PX)
        )
        # Startwert für die Karte: der Rest der Standardhöhe. Die
        # endgültige Höhe setzt gleich das CSS (siehe _FILL_CSS).
        map_height_px = max(_MIN_MAP_HEIGHT_PX, _DEFAULT_TOTAL_HEIGHT_PX - profile_height_px)
        return map_height_px, profile_height_px

    if mode == "manual":
        total_height_px = st.session_state.get("map_profile_total_height_px", _DEFAULT_TOTAL_HEIGHT_PX)
        return _split_map_profile_height(total_height_px)

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

    Der Schieberegler für die Spaltenbreite ('kpi_col_width_pct') wird NICHT
    mehr hier gerendert, sondern zusammen mit den anderen
    Anzeigeeinstellungen im aufklappbaren Bereich der Seitenleiste (siehe
    render_map_page) - der hier verwendete Wert kommt also von dort.
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
_NOTE_COLOR = "#1E88E5"


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


def _note_texts(note: pd.Series, distance_m: float) -> tuple[str, str]:
    """
    Baut (Popup-HTML für die Karte, Hovertext für das Höhenprofil) eines
    Info-Punkts. Nutzertexte werden maskiert (escape), damit ein '<' im
    Titel weder das Leaflet-Popup noch den Plotly-Hovertext zerlegt.
    """
    title = escape(str(note["note_title"] or "Punkt"))
    text = escape(str(note["note_text"])) if pd.notna(note["note_text"]) else ""
    km = f"{distance_m / 1000:.1f} km"
    popup = f"<b>📍 {title}</b><br><i>{km}</i>"
    hover = f"<b>📍 {title}</b><br>{km}"
    if text:
        body = text.replace("\n", "<br>")
        popup += f"<br>{body}"
        hover += f"<br>{body}"
    return popup, hover


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
    Die Seite "Karte (Sync)" setzt sie auf False: Ihre Komponente meldet
    nichts an Streamlit zurück, ein Kartenklick käme dort also nie an.
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


def _render_map_and_profile(
    df: pd.DataFrame,
    planning_mode: bool = False,
    track_store_seed: dict | None = None,
    map_height: int = 800,
    profile_height: int = 300,
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

    'map_height'/'profile_height' (in Pixeln) bestimmen die Höhe der
    Folium-Karte bzw. des Plotly-Höhenprofils - von render_map_page() über
    _resolve_map_profile_height() ermittelt (siehe dort). Im Höhen-Modus
    "fill" sind es nur Startwerte, die endgültige Höhe macht das CSS.

    Die Info-Punkte der ausgewählten Tracks werden hier selbst nachgeladen
    (gecacht, siehe functions.load_track_notes) und als blaue Marker auf
    der Karte sowie als Rauten im Höhenprofil eingezeichnet - unabhängig
    davon, ob der Planungsmodus aktiv ist.
    """
    notes = load_track_notes(tuple(sorted(df["track_id"].tolist())))
    # ----------------------------------------------------------------------
    # Wertebereiche für Kartenausschnitt und Farbskala
    # ----------------------------------------------------------------------
    # Gemeinsame Bounding-Box über alle ausgewählten Tracks, damit die Karte
    # beim Start so zugeschnitten wird, dass alle Tracks sichtbar sind.
    map_bounds = [
        [df["location_lat_min"].min(), df["location_lon_min"].min()],
        [df["location_lat_max"].max(), df["location_lon_max"].max()],
    ]

    range_elevation = [df["elevation_min"].min(), df["elevation_max"].max()]
    # Rand ober-/unterhalb des Höhenprofils: fester Anteil der Höhendifferenz
    # (nicht Faktor auf den Wert selbst - das bräche bei Höhen um 0 m bzw.
    # unter dem Meeresspiegel).
    ele_lo, ele_hi = range_elevation
    ele_pad = max((ele_hi - ele_lo) * 0.1, 10)

    # Je nach gewählter Farb-Spalte den passenden Wertebereich für die
    # Farbskala (vmin/vmax) auswählen; "none" (einfarbig) nutzt einen
    # Dummy-Bereich, da dann gar keine Werte eingefärbt werden.
    plot_column = st.session_state.plot_column
    range_att = {
        "km_per_h": [df["speed_min"].min(), df["speed_max"].max()],
        "ele": range_elevation,
        "slope": [df["slope_min"].min(), df["slope_max"].max()],
    }.get(plot_column, [1, 1])

    # ----------------------------------------------------------------------
    # Klick-Auswahl im Höhenprofil: Zustand verwalten
    # ----------------------------------------------------------------------
    # st.session_state.selected_point hält den zuletzt im Profil angeklickten
    # Punkt als Dict {"track_id", "point_index", "lat", "lon"} fest und
    # überlebt damit auch den Rerun, der durch den Klick selbst ausgelöst wird.
    selected_point = st.session_state.setdefault("selected_point", None)

    # Unterteilungspunkte je Track (track_id -> sortierte Liste von
    # Punkt-Indizes), siehe _toggle_split_point. Defensiv auch hier gesetzt,
    # damit diese Funktion unabhängig von render_map_page() funktioniert.
    st.session_state.setdefault("split_points", {})

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

    # Sammelbehälter für die Info-Punkte ALLER Tracks: Sie werden in der
    # Schleife befüllt, aber erst danach als EINE gemeinsame Trace ins
    # Profil gehängt (siehe unten - die Reihenfolge der Traces ist für die
    # Klick-Auflösung bedeutsam).
    note_x: list[float] = []
    note_y: list[float] = []
    note_hover: list[str] = []

    for i in range(len(df)):
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

        # Info-Punkte DIESES Tracks: Position im Profil (x = fortlaufende
        # Distanz am nächstgelegenen Trackpunkt) einmal hier bestimmen -
        # die Kartenmarker entstehen gleich darunter, die Profil-Trace
        # erst NACH der Schleife (siehe dort).
        track_notes = notes[notes["track_id"] == track_id] if not notes.empty else notes
        for _, note in track_notes.iterrows():
            note_idx, note_distance, note_ele = _note_position_on_track(gdf, note)
            popup_html, hover_html = _note_texts(note, note_distance)
            note_x.append(note_distance)
            note_y.append(note_ele)
            note_hover.append(hover_html)
            folium.Marker(
                [float(note["lat"]), float(note["lon"])],
                tooltip=popup_html,
                popup=folium.Popup(popup_html, max_width=280),
                icon=folium.Icon(color="blue", icon="info-sign"),
            ).add_to(m)

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
        if plot_column != "none":
            track_att = gdf[plot_column].values.tolist()
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
                    start=ele_lo - ele_pad,
                    stop=ele_hi + ele_pad,
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

    # Info-Punkte als gemeinsame Trace ins Höhenprofil - bewusst als
    # ALLERLETZTE Trace, NACH der Track-Schleife:
    # Die Klick-Auswertung weiter unten rechnet über "curve_number // 2"
    # von der Trace-Nummer auf den Track zurück und setzt dafür voraus,
    # dass die ersten 2*n Traces paarweise zu den n Tracks gehören. Eine
    # Trace dazwischen würde diese Zuordnung verschieben; am Ende
    # angehängt bekommt sie eine Nummer >= 2*n und wird dort korrekt
    # ignoriert (siehe "track_pos < len(df)").
    if note_x:
        fig.add_trace(
            go.Scatter(
                x=note_x,
                y=note_y,
                mode="markers",
                marker=dict(
                    symbol="diamond",
                    size=14,
                    color=_NOTE_COLOR,
                    line=dict(color="white", width=2),
                ),
                text=note_hover,
                hoverlabel=dict(bgcolor=_NOTE_COLOR),
                hovertemplate="%{text}<extra></extra>",
                showlegend=False,
            )
        )

    # y-Bereich des Höhenprofils EINMAL über ALLE ausgewählten Tracks
    # setzen (zuvor wurde er in der Schleife je Track überschrieben, sodass
    # am Ende nur der letzte Track passend skaliert war und die übrigen
    # Profile abgeschnitten wurden). Der Rand wird als fester Anteil der
    # Höhendifferenz aufgeschlagen (siehe ele_pad oben).
    fig.update_yaxes(range=[ele_lo - ele_pad, ele_hi + ele_pad])

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

    # Vorgemerkte Position für einen NEUEN Info-Punkt (gesetzt durch einen
    # Kartenklick bei aktivem "Position per Kartenklick", siehe unten):
    # als grauer Marker sichtbar, damit erkennbar ist, worauf sich das
    # Formular in der Kennzahlen-Spalte gerade bezieht.
    pending_note = st.session_state.get("_note_position")
    if pending_note and st.session_state.get("note_click_mode"):
        folium.Marker(
            [pending_note["lat"], pending_note["lon"]],
            tooltip="Position für neuen Punkt",
            icon=folium.Icon(color="gray", icon="plus"),
        ).add_to(m)

    if plot_column != "none":
        m.add_child(track_col)
    folium.LayerControl().add_to(m)
    Fullscreen(
        position="topleft",
        title="Vollbild",
        title_cancel="Vollbild beenden",
        force_separate_button=True,
    ).add_to(m)


    # Key für st_folium: enkodiert sowohl die aktuelle Track-Auswahl als auch
    # die gewünschte Kartenhöhe.
    #
    # Warum ein expliziter Key nötig ist:
    # st_folium rendert eine Leaflet-Karte in einem iframe. Streamlit-
    # Komponenten aktualisieren ihre innere Darstellung in zwei Wegen:
    #   (a) Props-Update (kein Remount): die Komponente erhält neue Props und
    #       entscheidet selbst, ob/wie sie sie umsetzt.
    #   (b) Remount (key wechselt): der bisherige iframe wird entfernt und ein
    #       völlig neuer aufgebaut.
    #
    # streamlit_folium ignoriert leider die 'height'-Prop beim Props-Update -
    # der Leaflet-Container behält seine ursprüngliche Höhe. Ein Remount ist
    # also zwingend, damit die neue Höhe wirksam wird.
    #
    # Key-Strategie:
    #   track_sig  - MD5 der sortierten Track-IDs; ändert sich bei
    #                Trackwechsel → Remount → last_clicked zurückgesetzt ✓
    #   map_height - Pixelhöhe; ändert sich bei Slider/Auto-Anpassung →
    #                Remount → iframe erhält korrekte neue Höhe ✓
    #   Gleiche Tracks + gleiche Höhe → gleicher Key → kein Remount →
    #   Kartenposition/Zoom und last_clicked bleiben erhalten ✓
    #
    # (vorher kein expliziter Key: streamlit_folium nutzte dann einen
    # internen Hash des Folium-Map-Objekts - was bei Trackwechseln korrekt
    # remountete, aber bei reinen Höhenänderungen keinen neuen Hash erzeugte
    # und deshalb nie remountete.)
    _track_sig = hashlib.md5(str(current_track_ids).encode()).hexdigest()[:8]
    _fmap_key = f"fmap_{_track_sig}_{map_height}"
    # Eigener Container mit Key 'mp_map': Im Höhen-Modus "fill" greift
    # darüber das CSS zu, das den Karten-iframe auf den verbleibenden
    # Platz bis zum Fensterrand streckt (siehe _FILL_CSS).
    with _keyed_container("mp_map"):
        map_state = st_folium(m, width="stretch", height=map_height, key=_fmap_key)

    # ----------------------------------------------------------------------
    # Klick auf der KARTE auswerten. Er hat je nach Einstellung zwei
    # mögliche Bedeutungen, in dieser Rangfolge:
    #
    #   1. "Position per Kartenklick" (Checkbox in der Punkte-Verwaltung,
    #      Schlüssel 'note_click_mode') ist aktiv -> der Klick merkt die
    #      Koordinaten für einen NEUEN Info-Punkt vor. Diese Bedeutung hat
    #      Vorrang, weil ein Info-Punkt gerade NICHT auf dem Track liegen
    #      muss und der Klick deshalb nicht auf einen Trackpunkt
    #      "eingefangen" werden darf. Im Planungsmodus bleiben die
    #      Trennpunkte in dieser Zeit über das Höhenprofil setzbar.
    #   2. Sonst im Planungsmodus: der nächstgelegene Trackpunkt zum Klick
    #      wird ermittelt (siehe _nearest_point_index) und als
    #      Unterteilungspunkt umgeschaltet (siehe _toggle_split_point) -
    #      funktional dasselbe wie der Profil-Klick weiter unten.
    #
    # Da der Planungsmodus nur bei genau einem ausgewählten Track aktiv ist
    # (siehe render_map_page), reicht dort der einzige Eintrag in
    # track_store.
    # ----------------------------------------------------------------------
    note_click_mode = bool(st.session_state.get("note_click_mode"))
    if note_click_mode and not filters_changed and map_state is not None:
        last_clicked = map_state.get("last_clicked")
        if last_clicked is not None:
            # Gleicher "zuletzt verarbeiteter Klick"-Schutz wie unten:
            # st_folium liefert denselben Wert bis zum nächsten Klick
            # erneut zurück, sonst entstünde eine Rerun-Schleife.
            click_token = (round(last_clicked["lat"], 7), round(last_clicked["lng"], 7))
            if st.session_state.get("_last_note_map_click") != click_token:
                st.session_state["_last_note_map_click"] = click_token
                st.session_state["_note_position"] = {
                    "lat": float(last_clicked["lat"]),
                    "lon": float(last_clicked["lng"]),
                }
                st.rerun()

    elif planning_mode and not filters_changed and map_state is not None:
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
    # Eigener Container mit Key 'mp_profile' - Gegenstück zu 'mp_map':
    # Das Profil behält im Füllmodus seine Pixelhöhe, die Karte darüber
    # bekommt den Rest (siehe _FILL_CSS).
    with _keyed_container("mp_profile"):
        event = st.plotly_chart(
            fig, on_select="rerun", key="my_chart_key", height=profile_height
        )

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

    Bewusst als eigene, öffentliche Funktion herausgelöst: Sowohl die Seite
    "Karte" (render_map_page, hier) als auch die Seite "Karte (Sync)"
    (map_linked.render_linked_map_page) verwenden exakt dieselben Filter.
    Da beide Seiten auch dieselben Widget-Keys (sport_select, year_select,
    track_<id>, ...) benutzen, bleibt die getroffene Auswahl beim Wechsel
    zwischen den beiden Kartenseiten erhalten.
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

    Bewusst - wie render_track_filters() - als eigene, öffentliche Funktion
    herausgelöst: Beide Kartenseiten zeigen denselben Schalter mit
    demselben Widget-Key ('planning_mode'), sodass der Modus beim Wechsel
    zwischen "Karte" und "Karte (Sync)" erhalten bleibt. Gesetzt werden die
    Unterteilungspunkte allerdings nur auf der Seite "Karte" (die
    Sync-Komponente meldet nichts an Streamlit zurück); "Karte (Sync)"
    zeigt sie lediglich in Karte und Höhenprofil an.
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
                "Im Planungsmodus lässt sich der ausgewählte Track per "
                "Klick auf die Karte oder ins Höhenprofil in Teile "
                "unterteilen. Dafür muss genau ein Track ausgewählt sein. "
                "Auf der Seite \"Karte (Sync)\" werden die gesetzten Punkte "
                "nur angezeigt."
            ),
        )
    return bool(st.session_state.get("planning_mode")) and single_track_selected


def render_map_page(settings_container=None) -> None:
    """
    Baut die komplette Kartenseite auf (Sidebar-Filter, Karte, Höhenprofil).

    'settings_container' ist ein Streamlit-Container (typischerweise ein
    st.expander), in den die Anzeigeeinstellungen - Farbauswahl für
    Karte/Profil, Spaltenbreite der Kennzahlen-Box sowie Höhe von
    Karte/Profil - gerendert werden. Im Normalbetrieb übergibt app.py
    hierfür denselben aufklappbaren Seitenleisten-Bereich, der dort auch
    die Seiten-Navigation (Karte/Verwaltung) enthält - dadurch landen
    Navigation UND Anzeigeeinstellungen gemeinsam in einem einzigen
    einklappbaren Container. Beim direkten Debug-Start
    (`streamlit run map.py`, siehe Dateiende) gibt es dieses app.py nicht,
    daher wird in diesem Fall ein eigener Expander angelegt.
    """
    if settings_container is None:
        settings_container = st.sidebar.expander("⚙️ Einstellungen", expanded=True)

    # Unterteilungspunkte des Planungsmodus (track_id -> Liste von
    # Punkt-Indizes) zentral initialisieren: _render_planning_kpis() wird
    # weiter unten VOR _render_map_and_profile() aufgerufen und greift
    # bereits darauf zu.
    st.session_state.setdefault("split_points", {})

    # ----------------------------------------------------------------------
    # Aufklappbarer Seitenleisten-Bereich: Anzeigeeinstellungen
    # ----------------------------------------------------------------------
    # Enthält (zusammen mit der Navigation aus app.py): Farbauswahl für
    # Karte/Höhenprofil, Spaltenbreite der Kennzahlen-Box sowie die Höhe von
    # Karte + Höhenprofil (automatisch an die Fensterhöhe angepasst oder
    # manuell per Schieberegler) - bewusst von den darunter folgenden
    # Filtern (Sport/Land/Jahr/Jahreszeit, Track-Auswahl) getrennt, damit
    # diese immer sofort sichtbar bleiben.
    with settings_container:
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

        st.slider(
            "Spaltenbreite Kennzahlen",
            min_value=10,
            max_value=35,
            value=15,
            key="kpi_col_width_pct",
            help="Breite der Kennzahlen-Spalte gegenüber der Karte rechts daneben.",
        )

        _render_height_settings()

    # CSS des Füllmodus - muss vor dem Aufbau des Hauptbereichs im
    # Dokument stehen, damit Karte und Profil gleich beim ersten Rendern
    # in der richtigen Höhe erscheinen.
    _render_fill_css()

    # ----------------------------------------------------------------------
    # Sidebar: Filter (Sport/Land/Jahr/Jahreszeit + Track-Baum)
    # ----------------------------------------------------------------------
    # Gemeinsam mit der Seite "Karte (Sync)" genutzt, siehe
    # render_track_filters() weiter oben.
    meta = render_track_filters()
    if meta.empty:
        # Keine Tracks vorhanden bzw. keiner ausgewählt: nur den Aufbau
        # DIESER Seite beenden (kein st.stop()), damit app.py danach noch
        # den "🚪 Beenden"-Knopf in die Seitenleiste rendern kann.
        render_no_selection_hint()
        return
    selected_tracks = meta["track_id"].tolist()

    # Planungsmodus-Schalter in der Seitenleiste (gemeinsam mit der Seite
    # "Karte (Sync)", siehe render_planning_toggle() weiter oben).
    render_planning_toggle(selected_tracks)

    # Erst JETZT, nachdem feststeht welche Tracks tatsächlich gebraucht
    # werden, die zugehörigen (potenziell großen) GPX-Binärdaten nachladen.
    file_data = load_track_files(tuple(sorted(meta["track_id"].tolist())))
    df = meta.merge(file_data, on="track_id", how="inner")

    # planning_mode kann (s.o.) zwar nur bei genau einem ausgewählten Track
    # aktiviert werden, "len(df) == 1" wird hier trotzdem defensiv erneut
    # geprüft, falls sich die Auswahl zwischen Sidebar und diesem Punkt
    # noch ändern sollte.
    planning_active = bool(st.session_state.get("planning_mode")) and len(df) == 1

    # Ist genau EIN Track ausgewählt, wird er HIER schon einmal verarbeitet
    # und sowohl an die Kennzahlen-Spalte (Bestzeiten bzw. Kennzahlen je
    # Teil im Planungsmodus) als auch an _render_map_and_profile()
    # weitergereicht, damit process_track() nicht zweimal pro Seitenaufruf
    # für denselben Track läuft (siehe _render_map_and_profile, Parameter
    # 'track_store_seed').
    precomputed_track_store = None
    if len(df) == 1:
        track_id = df["track_id"].iloc[0]
        precomputed_track_store = {track_id: process_track(track_id, df["file_data"].iloc[0])}

    # ----------------------------------------------------------------------
    # Hauptbereich: Kennzahlen links, Karte + Höhenprofil rechts daneben
    # ----------------------------------------------------------------------
    # Die Breite der linken Spalte (in Prozent) kommt aus dem Schieberegler
    # "Spaltenbreite Kennzahlen" im Anzeigeeinstellungen-Bereich der
    # Seitenleiste (siehe weiter oben in dieser Funktion) - der liegt im
    # Skriptablauf bereits VOR dieser Stelle, der Wert in session_state ist
    # also bereits aktuell.
    kpi_width_pct = st.session_state.kpi_col_width_pct
    col_kpis, col_map = st.columns([kpi_width_pct, 100 - kpi_width_pct], gap="small")

    # Höhe von Karte + Höhenprofil: automatisch aus der Fensterhöhe oder
    # manuell per Schieberegler (siehe _resolve_map_profile_height sowie die
    # zugehörige Auswahl im Anzeigeeinstellungen-Bereich der Seitenleiste).
    map_height, profile_height = _resolve_map_profile_height()

    # Info-Punkte der ausgewählten Tracks: einmal hier geladen (gecacht)
    # für die Verwaltung in der Kennzahlen-Spalte und den Planungs-Export;
    # _render_map_and_profile() holt sie sich für die Darstellung selbst.
    notes = load_track_notes(tuple(sorted(df["track_id"].tolist())))
    single_track_id = df["track_id"].iloc[0] if len(df) == 1 else None
    single_gdf = precomputed_track_store[single_track_id] if single_track_id is not None else None

    with col_kpis:
        with _keyed_container("mp_kpis", border=True):
            if planning_active:
                _render_planning_kpis(
                    single_gdf,
                    single_track_id,
                    df["track_title"].iloc[0],
                    notes=notes,
                )
            else:
                _render_kpis(df)
            # Bestzeiten nur bei genau einem Track (siehe
            # _render_best_efforts).
            if single_gdf is not None:
                st.divider()
                _render_best_efforts(single_gdf)

            # Info-Punkte: Liste, Bearbeiten und Neuanlage - in beiden
            # Betriebsarten verfügbar (siehe _render_notes_panel).
            _render_notes_panel(
                notes,
                gdf=single_gdf,
                track_id=single_track_id,
                track_title=df["track_title"].iloc[0] if single_track_id is not None else None,
                planning_mode=planning_active,
            )

    with col_map:
        with _keyed_container("mp_box", border=True):
            if planning_active:
                st.caption(
                    "📐 Planungsmodus: Klicke auf die Karte oder ins Höhenprofil, um "
                    "Trennpunkte zu setzen - ein erneuter Klick auf einen "
                    "bestehenden Punkt entfernt ihn wieder."
                )
            if st.session_state.get("note_click_mode"):
                st.caption(
                    "📍 Kartenklick setzt die Position für einen neuen Punkt "
                    "(Formular links). Trennpunkte lassen sich solange über das "
                    "Höhenprofil setzen."
                )
            _render_map_and_profile(
                df,
                planning_mode=planning_active,
                track_store_seed=precomputed_track_store,
                map_height=map_height,
                profile_height=profile_height,
            )


# Direkter Start zu Debug-Zwecken: `streamlit run map.py`. Im Normalbetrieb
# wird render_map_page() stattdessen von app.py über die Navigation
# aufgerufen.
if __name__ == "__main__":
    st.set_page_config(page_title="Karte", layout="wide")
    render_map_page()
