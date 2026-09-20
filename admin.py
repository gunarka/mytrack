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
    DEFAULT_ELEVATION_SMOOTHING_WINDOW,
    DEFAULT_MIN_ELEVATION_CHANGE_M,
    DEFAULT_MIN_SPEED_MOVING_KMH,
    delete_sport,
    delete_tour,
    delete_track,
    export_selection_gpx,
    export_tour_gpx,
    get_sports,
    get_sports_overview,
    get_track_file,
    get_tours,
    get_tours_overview,
    get_tracks,
    insert_sport,
    insert_tour,
    insert_track,
    process_and_build_track,
    recalculate_all_tracks_metadata,
    recalculate_track_metadata,
    rename_tracks,
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
    """
    Formular: eine oder MEHRERE GPX-Dateien hochladen, verarbeiten und
    speichern.

    Bei mehreren Dateien wird jede einzeln verarbeitet; scheitert eine
    davon (z.B. eine beschädigte Datei), werden die übrigen trotzdem
    gespeichert und am Ende eine Zusammenfassung angezeigt - ein
    Sammel-Import bricht also nicht am ersten Problemfall ab.
    """
    with st.expander("➕ Neue Tracks hochladen", expanded=True):
        with st.form(key="track_create_form", clear_on_submit=True):
            gpx_files = st.file_uploader(
                "GPX-Dateien",
                type=["gpx"],
                accept_multiple_files=True,
                help="Mehrere Dateien auf einmal möglich.",
            )
            track_title = st.text_input(
                "Track-Titel (optional, sonst Dateiname)",
                help=(
                    "Wird nur bei EINER Datei verwendet. Bei mehreren "
                    "Dateien dient jeweils der Dateiname als Titel."
                ),
            )
            sport = st.selectbox(
                "Sport", sports_options(), format_func=lambda s: s[1]
            )
            tour = st.selectbox(
                "Tour", tours_options(), format_func=lambda t: t[1]
            )
            submitted = st.form_submit_button("Verarbeiten und speichern")

        if not submitted:
            return
        if not gpx_files:
            st.warning("Bitte zuerst mindestens eine GPX-Datei auswählen.")
            return

        # Reverse-Geocoding + Zeitzonen-Ermittlung brauchen pro Datei einen
        # Moment (Nominatim ist auf 1 Anfrage/Sekunde begrenzt) - daher eine
        # Fortschrittsanzeige statt einer scheinbar eingefrorenen Seite.
        progress = st.progress(0.0, text="Verarbeite Dateien …")
        saved, failed = [], []
        for number, gpx_file in enumerate(gpx_files, start=1):
            progress.progress(
                (number - 1) / len(gpx_files),
                text=f"Verarbeite '{gpx_file.name}' ({number}/{len(gpx_files)}) …",
            )
            # Ein gemeinsamer Titel ergibt nur bei einer einzelnen Datei
            # Sinn - sonst hießen alle Tracks gleich.
            title = track_title if (track_title and len(gpx_files) == 1) else gpx_file.name
            try:
                record = process_and_build_track(
                    file_name=gpx_file.name,
                    file_bytes=gpx_file.getvalue(),
                    track_title=title,
                    sport_id=sport[0],
                    tour_id=tour[0],
                )
                insert_track(record)
                saved.append(title)
            except Exception as error:
                failed.append(f"{gpx_file.name}: {error}")
        progress.empty()

        if saved:
            st.success(f"{len(saved)} Track(s) gespeichert: {', '.join(saved)}")
        for message in failed:
            st.error(f"Nicht verarbeitet – {message}")
        if saved:
            st.rerun()


def _render_track_edit_form(tracks_df) -> None:
    """Formular: Titel/Sport/Tour eines bestehenden Tracks ändern oder löschen."""
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
            save = col_save.form_submit_button("Speichern", width="stretch")
            delete = col_delete.form_submit_button("Löschen", width="stretch")

        # Download der ursprünglich hochgeladenen GPX-Datei. Bewusst
        # AUSSERHALB des Formulars: st.download_button ist innerhalb von
        # st.form nicht zulässig.
        original = get_track_file(selected_id)
        if original is not None:
            file_name, file_bytes = original
            st.download_button(
                "⬇️ GPX herunterladen",
                data=file_bytes,
                file_name=file_name,
                mime="application/gpx+xml",
                key=f"track_download_{selected_id}",
                width="stretch",
            )

        if save:
            update_track(selected_id, new_title, new_sport[0], new_tour[0])
            st.success("Track aktualisiert.")
            st.rerun()
        if delete:
            delete_track(selected_id)
            st.success("Track gelöscht.")
            st.rerun()


def _render_track_recalculate_form(tracks_df) -> None:
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
            smoothing_window = st.number_input(
                "Glättung der Höhe (Anzahl Punkte, 0 = aus)",
                min_value=0,
                max_value=101,
                value=DEFAULT_ELEVATION_SMOOTHING_WINDOW,
                step=1,
                help=(
                    "Alternative zum Schwellwertverfahren: Die Höhe wird "
                    "vorab mit einem gleitenden Mittelwert über so viele "
                    "Trackpunkte geglättet. Das entfernt Messrauschen, "
                    "ohne - wie der Schwellwert - flache, gleichmäßige "
                    "Anstiege zu verschlucken; bei barometrisch "
                    "aufgezeichneten Höhen meist die genauere Variante. "
                    "Reine Glättung: hier z.B. 15 und die minimale "
                    "Höhenänderung oben auf 0 setzen."
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
                    smoothing_window=int(smoothing_window),
                )
            else:
                recalculate_track_metadata(
                    track_id,
                    min_speed_moving_kmh=min_speed,
                    min_elevation_change_m=min_ele_change,
                    use_gps_accuracy=use_accuracy,
                    smoothing_window=int(smoothing_window),
                )
                count = 1
        st.success(f"Metadaten für {count} Track(s) neu berechnet.")
        st.rerun()


def _clean_title(value) -> str:
    """
    Normalisiert einen Titel aus der Übersichtstabelle: fehlende Werte
    (None/NaN) werden zu einem leeren String, Leerzeichen am Rand fallen
    weg. So vergleichen sich alte und neue Titel zuverlässig, und ein
    versehentlich geleertes Feld lässt sich sauber erkennen.
    """
    if value is None or value != value:  # NaN ist ungleich sich selbst
        return ""
    return str(value).strip()


def _render_track_overview(tracks_df) -> None:
    """
    Tabelle aller vorhandenen Tracks - die Spalte "Track" ist direkt
    editierbar (st.data_editor), sodass sich Titel umbenennen lassen, ohne
    jeden Track einzeln im Bearbeiten-Formular auszuwählen.

    Alle übrigen Spalten sind gesperrt: Sie sind entweder berechnet
    (Distanz, Dauer, ...) oder bräuchten eine Auswahlliste (Sport, Tour) -
    dafür bleibt das Bearbeiten-Formular weiter zuständig. Gespeichert wird
    erst auf Knopfdruck, und zwar nur die tatsächlich geänderten Zeilen.
    """
    st.subheader("Alle Tracks")
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
    }).drop(columns=["sport_id", "tour_id"])

    # WICHTIG: track_id kommt aus DuckDB als uuid.UUID-Objekt. st.data_editor
    # wandelt solche Arrow-fremden Spalten im zurückgegebenen DataFrame still
    # in Strings um - ein Vergleich UUID == str wäre danach immer falsch und
    # die Zuordnung der bearbeiteten Zeilen würde ins Leere laufen. Deshalb
    # hier einmal bewusst auf Strings normalisieren (DuckDB akzeptiert
    # Strings beim späteren UPDATE ... WHERE track_id = ? ohne Weiteres).
    display_df["track_id"] = display_df["track_id"].astype(str)

    st.caption(
        f"{len(display_df)} Tracks. Titel in der Spalte 'Track' lassen sich "
        "direkt bearbeiten; gespeichert werden nur geänderte Zeilen."
    )
    locked_columns = [c for c in display_df.columns if c not in ("track_id", "Track")]
    # Der Editor merkt sich Eingaben anhand der Zeilenposition, nicht anhand
    # der track_id. Ändert sich der Bestand (Track angelegt, gelöscht oder
    # neu berechnet), zeigen gemerkte Positionen auf andere Zeilen - der
    # Schlüssel enthält deshalb den aktuellen Bestand und startet den Editor
    # in diesem Fall sauber neu.
    editor_key = "track_overview_editor_" + str(
        hash(tuple(display_df["track_id"]))
    )
    edited_df = st.data_editor(
        display_df,
        hide_index=True,
        width="stretch",
        key=editor_key,
        disabled=locked_columns,
        column_config={
            # track_id wird gebraucht, um die Änderungen wieder den
            # richtigen Zeilen zuzuordnen - für den Nutzer aber unnötig,
            # daher ausgeblendet statt entfernt.
            "track_id": None,
            "Track": st.column_config.TextColumn("Track", required=True),
        },
    )

    # Nur die tatsächlich geänderten Titel schreiben (statt bei jedem Klick
    # alle Zeilen zu aktualisieren). Der Abgleich läuft über ein Dictionary
    # statt über eine Suche je Zeile: das ist schneller und kann - anders als
    # die frühere .iloc[0]-Suche - bei einer unbekannten track_id nicht mehr
    # mit einem IndexError abbrechen.
    original_titles = {
        str(track_id): _clean_title(title)
        for track_id, title in zip(display_df["track_id"], display_df["Track"])
    }
    changed: dict[str, str] = {}
    empty_titles = 0
    for row in edited_df.itertuples(index=False):
        track_id = str(row.track_id)
        if track_id not in original_titles:
            continue
        new_title = _clean_title(row.Track)
        if not new_title:
            empty_titles += 1
            continue
        if new_title != original_titles[track_id]:
            changed[track_id] = new_title

    if empty_titles:
        st.warning(
            f"{empty_titles} Zeile(n) ohne Titel werden nicht gespeichert."
        )
    if st.button(
        f"Titel speichern ({len(changed)})",
        disabled=not changed,
        key="track_overview_save",
        help="Speichert ausschließlich die in der Tabelle geänderten Titel.",
    ):
        rename_tracks(changed)
        st.success(f"{len(changed)} Titel gespeichert.")
        st.rerun()


def _render_tracks_tab() -> None:
    # get_tracks() wird EINMAL je Rerun abgefragt und an die drei Bereiche
    # durchgereicht (statt dreimal dieselbe Abfrage auszuführen).
    tracks_df = get_tracks()
    _render_track_create_form()
    _render_track_edit_form(tracks_df)
    _render_track_recalculate_form(tracks_df)
    _render_track_overview(tracks_df)


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
            save = col_save.form_submit_button("Speichern", width="stretch")
            delete = col_delete.form_submit_button("Löschen", width="stretch")

        # Alle Tracks der Tour als EINE zusammenhängende GPX-Datei (ein
        # <trk> mit einem <trkseg> je Etappe). Bewusst ausserhalb des
        # Formulars: st.download_button ist innerhalb von st.form nicht
        # zulässig. st.download_button braucht die Daten bereits beim
        # Rendern, der Export wird daher hier direkt erzeugt - bei den
        # üblichen Tourgrößen ist das unkritisch.
        tour_gpx = export_tour_gpx(selected_id)
        if tour_gpx is None:
            st.caption("Diese Tour enthält noch keine Tracks – kein Export möglich.")
        else:
            st.download_button(
                "⬇️ Tour als GPX exportieren",
                data=tour_gpx,
                file_name=f"{selected_title}.gpx".replace("/", "_"),
                mime="application/gpx+xml",
                key=f"tour_download_{selected_id}",
                width="stretch",
                help=(
                    "Alle Tracks dieser Tour in einer Datei, zeitlich "
                    "sortiert und je Etappe als eigenes Segment."
                ),
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
            save = col_save.form_submit_button("Speichern", width="stretch")
            delete = col_delete.form_submit_button("Löschen", width="stretch")

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
# GPX-Export
# ---------------------------------------------------------------------------
def _render_gpx_export_expander(tracks_df, tours_df) -> None:
    """
    Eigenständiger Export-Bereich oberhalb der drei Tabs: einzelne Tracks
    und/oder ganze Touren (jeweils mit allen ihren Tracks) lassen sich
    frei auswählen und gemeinsam herunterladen.

    Bei genau EINER Auswahl (ein Track ODER eine Tour) liefert
    export_selection_gpx() direkt eine einzelne GPX-Datei; bei mehreren
    Auswahlen ein ZIP-Archiv mit je einer Datei pro Track bzw. Tour.
    Info-Punkte werden in jedem Fall mit exportiert (siehe
    export_selection_gpx() in functions.py).
    """
    with st.expander("⬇️ GPX-Export", expanded=False):
        if tracks_df.empty and tours_df.empty:
            st.info("Noch keine Tracks oder Touren vorhanden.")
            return

        st.caption(
            "Einzelne Tracks und/oder ganze Touren auswählen und "
            "herunterladen. Bei mehr als einer Auswahl wird ein "
            "ZIP-Archiv mit je einer GPX-Datei erzeugt; Info-Punkte "
            "werden immer mit exportiert."
        )

        track_options = list(zip(tracks_df["track_id"], tracks_df["track_title"]))
        tour_options = list(zip(tours_df["tour_id"], tours_df["tour_title"]))

        selected_tracks = st.multiselect(
            "Einzelne Tracks",
            options=track_options,
            format_func=lambda t: t[1],
            key="gpx_export_tracks",
        )
        selected_tours = st.multiselect(
            "Touren (jeweils alle enthaltenen Tracks als eine Datei)",
            options=tour_options,
            format_func=lambda t: t[1],
            key="gpx_export_tours",
        )

        track_ids = [t[0] for t in selected_tracks]
        tour_ids = [t[0] for t in selected_tours]
        total = len(track_ids) + len(tour_ids)
        if total == 0:
            st.caption("Noch nichts ausgewählt.")
            return

        export = export_selection_gpx(track_ids, tour_ids)
        if export is None:
            st.warning("Für die Auswahl konnte keine GPX-Datei erzeugt werden.")
            return

        file_name, file_bytes, mime = export
        st.download_button(
            f"⬇️ Auswahl herunterladen ({total})",
            data=file_bytes,
            file_name=file_name,
            mime=mime,
            key="gpx_export_download",
            width="stretch",
        )


# ---------------------------------------------------------------------------
# Öffentliche Einstiegsfunktion
# ---------------------------------------------------------------------------
def render_admin_page() -> None:
    """Baut die komplette Verwaltungsseite mit Export-Bereich und den drei Tabs auf."""
    _render_gpx_export_expander(get_tracks(), get_tours())

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
