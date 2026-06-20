"""
admin.py
========
Verwaltungsoberfläche der MyTrack-App.

Dieses Modul stellt die Funktion render_admin_page() bereit, die von app.py
als eine der Navigationsseiten eingebunden wird (siehe dort). Es kann zum
Debuggen aber auch weiterhin direkt mit `streamlit run admin.py` gestartet
werden (siehe Aufruf von render_admin_page() ganz am Ende der Datei).

Die Seite besteht aus drei Tabs, die jeweils nach einem ähnlichen Muster
aufgebaut sind:
    1. "Neu anlegen"   - Formular zum Erstellen eines neuen Eintrags
    2. "Bearbeiten"     - Formular zum Ändern (und Löschen) eines
                          bestehenden Eintrags
    3. "Übersicht"      - Tabelle aller vorhandenen Einträge

Tabs:
    - Tracks:     GPX-Datei hochladen & verarbeiten, Titel/Sport/Tour
                  eines Tracks bearbeiten, Track-Metadaten ('Zeit in
                  Bewegung' sowie Auf-/Abstieg) anhand neuer Schwellwerte
                  neu berechnen, alle Tracks anzeigen.
    - Touren:     Touren anlegen, umbenennen/löschen, alle Touren samt
                  ihrer Tracks anzeigen.
    - Sportarten: Sportarten anlegen, umbenennen/löschen, alle
                  Sportarten samt ihrer Tracks anzeigen.

Die komplette fachliche Logik (GPX-Verarbeitung, Geocoding, Datenbank-
Zugriffe) steckt in functions.py - hier in admin.py befindet sich
ausschließlich UI-Code, der diese Funktionen aufruft.
"""

import streamlit as st

from functions import (
    DEFAULT_MIN_ELEVATION_CHANGE_M,
    DEFAULT_MIN_SPEED_MOVING_KMH,
    delete_sport,
    delete_tour,
    delete_track,
    get_sports,
    get_sports_overview,
    get_tours,
    get_tours_overview,
    get_tracks,
    insert_sport,
    insert_tour,
    process_and_build_track,
    insert_track,
    recalculate_all_tracks_metadata,
    recalculate_track_metadata,
    sports_options,
    tours_options,
    update_sport,
    update_tour,
    update_track,
)


# ---------------------------------------------------------------------------
# Tab "Tracks"
# ---------------------------------------------------------------------------
def _render_track_create_form() -> None:
    """Formular: neue GPX-Datei hochladen, verarbeiten und speichern."""
    with st.expander("➕ Neuen Track hochladen", expanded=True):
        with st.form(key="track_create_form", clear_on_submit=True):
            gpx_file = st.file_uploader("GPX-Datei", type=["gpx"])
            track_title = st.text_input("Track-Titel (optional, sonst Dateiname)")
            sport = st.selectbox(
                "Sport", sports_options(), format_func=lambda s: s[1]
            )
            tour = st.selectbox(
                "Tour", tours_options(), format_func=lambda t: t[1]
            )
            submitted = st.form_submit_button("Verarbeiten und speichern")

        if not submitted:
            return
        if gpx_file is None:
            st.warning("Bitte zuerst eine GPX-Datei auswählen.")
            return

        title = track_title or gpx_file.name
        # Reverse-Geocoding + Zeitzonen-Ermittlung brauchen einen Moment -
        # daher ein sichtbarer Spinner, statt dass die Seite scheinbar
        # "einfriert".
        with st.spinner(f"Verarbeite '{gpx_file.name}' …"):
            record = process_and_build_track(
                file_name=gpx_file.name,
                file_bytes=gpx_file.getvalue(),
                track_title=title,
                sport_id=sport[0],
                tour_id=tour[0],
            )
            insert_track(record)
        st.success(f"Track '{title}' gespeichert.")
        st.rerun()


def _render_track_edit_form() -> None:
    """Formular: Titel/Sport/Tour eines bestehenden Tracks ändern oder löschen."""
    tracks_df = get_tracks()
    with st.expander("✏️ Track bearbeiten", expanded=False):
        if tracks_df.empty:
            st.info("Noch keine Tracks vorhanden.")
            return

        options = list(zip(tracks_df["track_id"], tracks_df["track_title"]))
        selected_id, _ = st.selectbox(
            "Track auswählen",
            options=options,
            format_func=lambda o: o[1],
            key="track_edit_select",
        )
        row = tracks_df[tracks_df["track_id"] == selected_id].iloc[0]

        sport_opts = sports_options()
        tour_opts = tours_options()
        sport_index = next(
            (i for i, s in enumerate(sport_opts) if s[0] == row["sport_id"]), 0
        )
        tour_index = next(
            (i for i, t in enumerate(tour_opts) if t[0] == row["tour_id"]), 0
        )

        with st.form(key="track_edit_form"):
            new_title = st.text_input("Track-Titel", value=row["track_title"])
            new_sport = st.selectbox(
                "Sport", sport_opts, index=sport_index, format_func=lambda s: s[1]
            )
            new_tour = st.selectbox(
                "Tour", tour_opts, index=tour_index, format_func=lambda t: t[1]
            )
            col_save, col_delete = st.columns(2)
            save = col_save.form_submit_button("Speichern", use_container_width=True)
            delete = col_delete.form_submit_button(
                "Löschen", use_container_width=True
            )

        if save:
            update_track(selected_id, new_title, new_sport[0], new_tour[0])
            st.success("Track aktualisiert.")
            st.rerun()
        if delete:
            delete_track(selected_id)
            st.success("Track gelöscht.")
            st.rerun()


def _render_track_recalculate_form() -> None:
    """
    Formular: 'Zeit in Bewegung' sowie Auf-/Abstieg eines einzelnen oder
    aller Tracks anhand neuer Schwellwerte aus den gespeicherten GPX-
    Rohdaten neu berechnen (siehe recalculate_track_metadata /
    recalculate_all_tracks_metadata in functions.py).

    Sinnvoll, wenn sich die beim Hochladen verwendeten Standardwerte im
    Nachhinein als ungeeignet herausstellen (z.B. weil ein sehr langsamer
    Wander-Track viele kurze Stopps fälschlich als 'Bewegung' zählt, oder
    ein Track mit schlechter GPS-Genauigkeit unrealistisch hohe Auf-/
    Abstiegswerte zeigt). Distanz, Gesamtdauer und alle übrigen Kennzahlen
    bleiben dabei unverändert, da sie nicht von diesen Schwellwerten
    abhängen.
    """
    tracks_df = get_tracks()
    with st.expander("🔄 Track-Metadaten neu berechnen", expanded=False):
        if tracks_df.empty:
            st.info("Noch keine Tracks vorhanden.")
            return

        st.caption(
            "Berechnet 'Zeit in Bewegung' sowie Auf-/Abstieg aus den "
            "gespeicherten GPX-Rohdaten neu - z.B. wenn die bisherigen "
            "Schwellwerte zu unplausiblen Werten geführt haben. Distanz, "
            "Gesamtdauer sowie alle übrigen Kennzahlen bleiben unverändert."
        )

        track_options = [(None, "– alle Tracks –")] + list(
            zip(tracks_df["track_id"], tracks_df["track_title"])
        )

        with st.form(key="track_recalculate_form"):
            target = st.selectbox(
                "Track", track_options, format_func=lambda t: t[1]
            )
            min_speed = st.number_input(
                "Minimale Geschwindigkeit für 'Zeit in Bewegung' (km/h)",
                min_value=0.0,
                value=DEFAULT_MIN_SPEED_MOVING_KMH,
                step=0.1,
                help=(
                    "Punkte mit niedrigerer Geschwindigkeit gelten als "
                    "Stillstand/Pause und zählen nicht zur Bewegungszeit."
                ),
            )
            min_ele_change = st.number_input(
                "Minimale Höhenänderung für Auf-/Abstieg (m)",
                min_value=0.0,
                value=DEFAULT_MIN_ELEVATION_CHANGE_M,
                step=0.1,
                help=(
                    "Höhenschwankungen unterhalb dieses Werts gelten als "
                    "Messrauschen und werden nicht als Auf- oder Abstieg "
                    "gezählt (Schwellwert-Verfahren mit Hysterese)."
                ),
            )
            use_accuracy = st.checkbox(
                "Schwellwert mit gespeicherter GPS-Genauigkeit skalieren "
                "(falls in der GPX-Datei vorhanden)",
                value=True,
                help=(
                    "Enthält ein Track eine je Punkt gespeicherte GPS-"
                    "Genauigkeit (vdop, ersatzweise hdop), wird der obige "
                    "Schwellwert an ungenaueren Stellen automatisch "
                    "vergrößert. Tracks ohne gespeicherte Genauigkeit "
                    "nutzen weiterhin unverändert den festen Wert oben."
                ),
            )
            submitted = st.form_submit_button("Neu berechnen")

        if not submitted:
            return

        track_id = target[0]
        with st.spinner("Berechne Track-Metadaten neu …"):
            if track_id is None:
                count = recalculate_all_tracks_metadata(
                    min_speed_moving_kmh=min_speed,
                    min_elevation_change_m=min_ele_change,
                    use_gps_accuracy=use_accuracy,
                )
            else:
                recalculate_track_metadata(
                    track_id,
                    min_speed_moving_kmh=min_speed,
                    min_elevation_change_m=min_ele_change,
                    use_gps_accuracy=use_accuracy,
                )
                count = 1
        st.success(f"Metadaten für {count} Track(s) neu berechnet.")
        st.rerun()


def _render_track_overview() -> None:
    """Tabelle aller vorhandenen Tracks."""
    st.subheader("Alle Tracks")
    tracks_df = get_tracks()
    if tracks_df.empty:
        st.info("Noch keine Tracks vorhanden.")
        return
    display_df = tracks_df.rename(columns={
        "track_title": "Track",
        "sport_title": "Sport",
        "tour_title": "Tour",
        "time_start": "Start",
        "time_end": "Ende",
        "track_distance_m": "Distanz (m)",
        "track_time_s": "Dauer (s)",
        "track_time_moving_s": "Zeit in Bewegung (s)",
        "track_ascent_m": "Aufstieg (m)",
        "track_descent_m": "Abstieg (m)",
        "location_start_county": "Start-Gebiet",
        "location_end_county": "End-Gebiet",
        "file_name": "Datei",
    })
    st.dataframe(
        display_df.drop(columns=["track_id", "sport_id", "tour_id"]),
        hide_index=True,
        width="stretch",
    )


def _render_tracks_tab() -> None:
    _render_track_create_form()
    _render_track_edit_form()
    _render_track_recalculate_form()
    _render_track_overview()


# ---------------------------------------------------------------------------
# Tab "Touren"
# ---------------------------------------------------------------------------
def _render_tour_create_form() -> None:
    """Formular: neue Tour anlegen."""
    with st.expander("➕ Neue Tour anlegen", expanded=True):
        with st.form(key="tour_create_form", clear_on_submit=True):
            tour_title = st.text_input("Tour-Titel")
            submitted = st.form_submit_button("Speichern")

        if submitted:
            if not tour_title:
                st.warning("Bitte einen Tour-Titel eingeben.")
                return
            insert_tour(tour_title)
            st.success(f"Tour '{tour_title}' angelegt.")
            st.rerun()


def _render_tour_edit_form() -> None:
    """Formular: bestehende Tour umbenennen oder löschen."""
    tours_df = get_tours()
    with st.expander("✏️ Tour bearbeiten", expanded=False):
        if tours_df.empty:
            st.info("Noch keine Touren vorhanden.")
            return

        options = list(zip(tours_df["tour_id"], tours_df["tour_title"]))
        selected_id, selected_title = st.selectbox(
            "Tour auswählen", options=options, format_func=lambda o: o[1],
            key="tour_edit_select",
        )

        with st.form(key="tour_edit_form"):
            new_title = st.text_input("Tour-Titel", value=selected_title)
            col_save, col_delete = st.columns(2)
            save = col_save.form_submit_button("Speichern", use_container_width=True)
            delete = col_delete.form_submit_button(
                "Löschen", use_container_width=True
            )

        if save:
            update_tour(selected_id, new_title)
            st.success("Tour aktualisiert.")
            st.rerun()
        if delete:
            delete_tour(selected_id)
            st.success("Tour gelöscht. Zugeordnete Tracks bleiben erhalten.")
            st.rerun()


def _render_tour_overview() -> None:
    """Tabelle aller Touren samt ihrer Tracks."""
    st.subheader("Alle Touren")
    overview_df = get_tours_overview()
    if overview_df.empty:
        st.info("Noch keine Touren vorhanden.")
        return
    st.dataframe(overview_df, hide_index=True, width="stretch")


def _render_tours_tab() -> None:
    _render_tour_create_form()
    _render_tour_edit_form()
    _render_tour_overview()


# ---------------------------------------------------------------------------
# Tab "Sportarten"
# ---------------------------------------------------------------------------
def _render_sport_create_form() -> None:
    """Formular: neue Sportart anlegen."""
    with st.expander("➕ Neue Sportart anlegen", expanded=True):
        with st.form(key="sport_create_form", clear_on_submit=True):
            sport_title = st.text_input("Sport-Titel")
            submitted = st.form_submit_button("Speichern")

        if submitted:
            if not sport_title:
                st.warning("Bitte einen Sport-Titel eingeben.")
                return
            insert_sport(sport_title)
            st.success(f"Sportart '{sport_title}' angelegt.")
            st.rerun()


def _render_sport_edit_form() -> None:
    """Formular: bestehende Sportart umbenennen oder löschen."""
    sports_df = get_sports()
    with st.expander("✏️ Sportart bearbeiten", expanded=False):
        if sports_df.empty:
            st.info("Noch keine Sportarten vorhanden.")
            return

        options = list(zip(sports_df["sport_id"], sports_df["sport_title"]))
        selected_id, selected_title = st.selectbox(
            "Sportart auswählen", options=options, format_func=lambda o: o[1],
            key="sport_edit_select",
        )

        with st.form(key="sport_edit_form"):
            new_title = st.text_input("Sport-Titel", value=selected_title)
            col_save, col_delete = st.columns(2)
            save = col_save.form_submit_button("Speichern", use_container_width=True)
            delete = col_delete.form_submit_button(
                "Löschen", use_container_width=True
            )

        if save:
            update_sport(selected_id, new_title)
            st.success("Sportart aktualisiert.")
            st.rerun()
        if delete:
            delete_sport(selected_id)
            st.success("Sportart gelöscht. Zugeordnete Tracks bleiben erhalten.")
            st.rerun()


def _render_sport_overview() -> None:
    """Tabelle aller Sportarten samt ihrer Tracks."""
    st.subheader("Alle Sportarten")
    overview_df = get_sports_overview()
    if overview_df.empty:
        st.info("Noch keine Sportarten vorhanden.")
        return
    st.dataframe(overview_df, hide_index=True, width="stretch")


def _render_sports_tab() -> None:
    _render_sport_create_form()
    _render_sport_edit_form()
    _render_sport_overview()


# ---------------------------------------------------------------------------
# Öffentliche Einstiegsfunktion
# ---------------------------------------------------------------------------
def render_admin_page() -> None:
    """Baut die komplette Verwaltungsseite mit ihren drei Tabs auf."""
    tab_track, tab_tour, tab_sport = st.tabs(["Tracks", "Touren", "Sportarten"])

    with tab_track:
        _render_tracks_tab()
    with tab_tour:
        _render_tours_tab()
    with tab_sport:
        _render_sports_tab()


# Direkter Start zu Debug-Zwecken: `streamlit run admin.py`. Im
# Normalbetrieb wird render_admin_page() stattdessen von app.py über die
# Navigation aufgerufen.
if __name__ == "__main__":
    st.set_page_config(page_title="Verwaltung", layout="wide")
    render_admin_page()
