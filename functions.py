"""
functions.py
============
Gemeinsam genutzte Hilfsfunktionen der MyTrack-App.

Dieses Modul bündelt alles, was von mehreren Seiten/Skripten gebraucht
wird (app.py, admin.py, map.py, init.py):

    - Datenbankverbindung & Tabellen-Setup (Abschnitt "Datenbank")
    - Einlesen und Berechnen von GPX-Tracks mit GeoPandas
      (Abschnitt "GPX-Verarbeitung")
    - Reverse-Geocoding und Zeitzonen-Ermittlung für einen Punkt
      (Abschnitt "Geocoding & Zeitzone")
    - CRUD-Funktionen (Create/Read/Update/Delete) für die Tabellen
      'sport', 'tours' und 'gpx' (Abschnitt "CRUD: ...") sowie für die
      Info-Punkte in 'track_notes' (Abschnitt "Info-Punkte")

Durch die Bündelung an einer Stelle enthalten admin.py und map.py nur noch
UI-Code; die eigentliche Logik bzw. der Datenbankzugriff steht hier EINMAL,
was Duplikate vermeidet (z.B. wurde die GPX-Aufbereitung vorher sowohl in
admin.py als auch in map.py separat implementiert) und Wartung/Tests
erleichtert.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import signal
import threading
import time
import uuid
import zipfile

import duckdb
import geopandas as gpd
import gpxpy
import gpxpy.gpx
import numpy as np
import pandas as pd
import streamlit as st
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

# Pfad zur lokalen DuckDB-Datei. Zentral hier definiert, damit alle Module
# (inkl. init.py) garantiert dieselbe Datenbank verwenden.
DB_PATH = ".data/tracks.duckdb"

# Standard-Schwellwerte für die Berechnung von "Zeit in Bewegung" sowie für
# Auf-/Abstieg (siehe compute_moving_time_s / compute_ascent_descent weiter
# unten). Diese Werte werden beim Hochladen eines neuen Tracks automatisch
# verwendet (process_and_build_track). In der Verwaltung (admin.py) lassen
# sich bereits gespeicherte Tracks mit abweichenden Werten neu berechnen,
# ohne dass diese Standardwerte selbst verändert werden.
DEFAULT_MIN_SPEED_MOVING_KMH = 1.0
DEFAULT_MIN_ELEVATION_CHANGE_M = 2.0

# Breite des gleitenden Mittelwerts (in Trackpunkten), mit dem die Höhe vor
# der Auf-/Abstiegsberechnung geglättet werden kann - Alternative bzw.
# Ergänzung zum Schwellwertverfahren (siehe compute_ascent_descent).
# 0 = keine Glättung (bisheriges Verhalten, bleibt Standard).
DEFAULT_ELEVATION_SMOOTHING_WINDOW = 0

# Standard-Distanzen für die Bestzeiten-Auswertung innerhalb eines Tracks
# (siehe compute_best_efforts): (Bezeichnung, Distanz in Metern).
BEST_EFFORT_DISTANCES = [
    ("1 km", 1_000.0),
    ("5 km", 5_000.0),
    ("10 km", 10_000.0),
    ("Halbmarathon", 21_097.5),
    ("Marathon", 42_195.0),
]


# Tabellendefinition der Info-Punkte ("Punkte zur Tour"): frei platzierbare
# Anmerkungen zu einem Track (Hütte, Aussicht, Wasserstelle, Achtung ...).
# Sie unterteilen den Track NICHT (das tun die Unterteilungspunkte des
# Planungsmodus, die nur im Sitzungszustand leben), sondern tragen
# Informationen - und müssen deshalb auch nicht exakt auf dem Track liegen.
# An EINER Stelle definiert, weil sie sowohl beim Neuanlegen der Datenbank
# (init_database) als auch bei der sanften Migration bestehender Datenbanken
# (_ensure_schema_migrations) gebraucht wird.
#
# 'point_index' ist der Index des NÄCHSTGELEGENEN Trackpunkts (bezogen auf
# das von process_track() aufbereitete DataFrame). Er wird beim Anlegen
# einmal bestimmt und gespeichert, damit der Punkt im Höhenprofil ohne
# erneute Nachbarschaftssuche an der richtigen Kilometer-Stelle erscheint.
_TRACK_NOTES_DDL = """
    CREATE TABLE track_notes (
        note_id     UUID NOT NULL,
        track_id    UUID NOT NULL,
        note_title  VARCHAR,
        note_text   VARCHAR,
        note_kind   VARCHAR,      -- 'info' (normal) oder 'planning' (im Planungsmodus angelegt)
        lat         DOUBLE,
        lon         DOUBLE,
        ele         DOUBLE,
        point_index INTEGER,
        time_stamp  TIMESTAMP
    )
"""


# ---------------------------------------------------------------------------
# Datenbank
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_connection() -> duckdb.DuckDBPyConnection:
    """
    Liefert die (einzige) DuckDB-Verbindung dieses Streamlit-Prozesses.

    @st.cache_resource sorgt dafür, dass die Verbindung nur EINMAL geöffnet
    wird, unabhängig davon, wie oft das Skript durch Nutzerinteraktionen neu
    ausgeführt wird ("Rerun") und unabhängig davon, von welcher Seite
    (Karte/Admin) aus sie angefordert wird - alle Module importieren
    dieselbe Funktion und teilen sich damit dieselbe Verbindung.

    WICHTIG: Die Verbindung darf deshalb NIRGENDS manuell mit con.close()
    geschlossen werden - sie wird vom Cache verwaltet und lebt so lange wie
    der Streamlit-Prozess selbst.
    """
    # DuckDB legt zwar die Datenbankdatei selbst an, NICHT aber fehlende
    # übergeordnete Verzeichnisse - bei einem frischen Checkout (".data/"
    # existiert noch nicht) würde der Verbindungsaufbau sonst fehlschlagen.
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    con = duckdb.connect(database=str(DB_PATH))
    _ensure_schema_migrations(con)
    return con


def _ensure_schema_migrations(con: duckdb.DuckDBPyConnection) -> None:
    """
    Sanfte Schema-Migration für bereits bestehende, schon befüllte
    Datenbanken: ergänzt nachträglich eingeführte Spalten der Tabelle
    'gpx', falls sie noch fehlen (z.B. 'track_time_moving_s' für "Zeit in
    Bewegung"), und legt nachträglich eingeführte Tabellen an (Info-Punkte,
    siehe _TRACK_NOTES_DDL).

    Wird bei JEDEM Verbindungsaufbau aufgerufen, ist also ein no-op, sobald
    die Spalte einmal existiert. Dadurch müssen Bestandsnutzer ihre Daten
    nicht über init_database() (= kompletter Datenverlust!) neu anlegen,
    nur weil eine neue Kennzahl hinzugekommen ist. Existiert die Tabelle
    'gpx' noch gar nicht (frischer Checkout vor dem ersten Lauf von
    init.py), passiert ebenfalls nichts - init_database() legt sie dann
    direkt inklusive aller aktuellen Spalten an.
    """
    # Info-Punkte: als eigene Tabelle nachgereicht, deshalb hier vor der
    # gpx-Prüfung - sie muss auch dann existieren, wenn 'gpx' (noch) leer
    # ist bzw. fehlt. "IF NOT EXISTS" macht den Aufruf zum no-op.
    con.sql(_TRACK_NOTES_DDL.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))

    table_exists = con.sql(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'gpx'"
    ).fetchone()
    if not table_exists:
        return

    existing_columns = {
        row[1] for row in con.execute("PRAGMA table_info('gpx')").fetchall()
    }
    if "track_time_moving_s" not in existing_columns:
        con.execute("ALTER TABLE gpx ADD COLUMN track_time_moving_s DOUBLE")


def close_connection() -> None:
    """
    Schliesst die DuckDB-Verbindung und leert den Ressourcen-Cache.

    Gegenstück zu get_connection() und die EINZIGE Stelle, an der die
    Verbindung geschlossen werden darf (siehe Hinweis dort): Sie wird nur
    beim geordneten Beenden der App über den "Beenden"-Knopf in der
    Seitenleiste (app.py) aufgerufen. Erst dadurch gibt DuckDB die
    Schreibsperre auf die Datei '.data/tracks.duckdb' wieder frei - sonst
    könnte ein direkt anschliessender Start von init.py oder ein zweiter
    App-Start die Datenbank nicht öffnen.

    get_connection.clear() entfernt zusätzlich den zwischengespeicherten
    (jetzt geschlossenen) Verbindungs-Handle aus dem Streamlit-Cache, damit
    nachfolgender Code nicht versehentlich auf eine tote Verbindung trifft.
    Fehler werden bewusst verschluckt: Ein Beenden darf nie an einer
    bereits geschlossenen oder nie geöffneten Verbindung scheitern.
    """
    try:
        con = get_connection()
        con.close()
    except Exception:
        pass
    try:
        get_connection.clear()
    except Exception:
        pass


def shutdown_app(delay_s: float = 1.5) -> None:
    """
    Beendet die Anwendung vollständig: Datenbank trennen und den
    Streamlit-Serverprozess (das Terminal, in dem 'streamlit run app.py'
    läuft) stoppen.

    Der eigentliche Abschuss läuft in einem Hintergrund-Thread mit kurzer
    Verzögerung ('delay_s'), damit Streamlit die zuletzt gerenderte Seite
    ("Anwendung beendet") noch an den Browser ausliefern kann - würde der
    Prozess sofort sterben, sähe der Nutzer nur einen Verbindungsfehler.

    SIGTERM gibt Streamlit die Gelegenheit, sich selbst geordnet
    herunterzufahren; greift das nicht (z.B. unter Windows, wo SIGTERM nur
    eingeschränkt unterstützt wird), erzwingt os._exit() das Ende. os._exit
    umgeht bewusst jegliches Aufräumen der Laufzeitumgebung, weil an dieser
    Stelle alles Wichtige - die Datenbankverbindung - bereits geschlossen
    ist.
    """
    close_connection()

    def _terminate() -> None:
        time.sleep(delay_s)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            pass
        time.sleep(2.0)
        os._exit(0)

    threading.Thread(target=_terminate, daemon=True).start()


def init_database() -> None:
    """
    Legt die vier Tabellen 'gpx', 'tours', 'sport' und 'track_notes' neu an.

    ACHTUNG: Bereits vorhandene Tabellen (und alle enthaltenen Daten!)
    werden vorher gelöscht. Diese Funktion wird ausschließlich über das
    separate Werkzeug init.py per Button ausgelöst und ist bewusst NICHT
    Teil der normalen App-Navigation (app.py), um versehentlichen
    Datenverlust im laufenden Betrieb zu vermeiden.
    """
    con = get_connection()

    con.sql("DROP TABLE IF EXISTS gpx")
    con.sql("DROP TABLE IF EXISTS tours")
    con.sql("DROP TABLE IF EXISTS sport")
    con.sql("DROP TABLE IF EXISTS track_notes")

    con.sql("""
        CREATE TABLE gpx (
            track_id                 UUID        NOT NULL,
            track_title               VARCHAR,
            sport_id                  UUID,
            tour_id                   UUID,

            location_start_country    VARCHAR,
            location_start_state      VARCHAR,
            location_start_county     VARCHAR,
            location_start_town       VARCHAR,
            location_start_suburb     VARCHAR,
            location_start_road       VARCHAR,

            location_end_country      VARCHAR,
            location_end_state        VARCHAR,
            location_end_county       VARCHAR,
            location_end_town         VARCHAR,
            location_end_suburb       VARCHAR,
            location_end_road         VARCHAR,

            location_start_lat_lon    STRUCT(lat DOUBLE, lon DOUBLE),
            location_end_lat_lon      STRUCT(lat DOUBLE, lon DOUBLE),
            location_start_address    JSON,
            location_end_address      JSON,

            location_lat_min          DOUBLE,
            location_lat_max          DOUBLE,
            location_lon_min          DOUBLE,
            location_lon_max          DOUBLE,

            time_zone                 VARCHAR,
            time_start                TIMESTAMP,
            time_end                  TIMESTAMP,
            track_time_s              DOUBLE,
            track_time_moving_s       DOUBLE,
            track_distance_m          DOUBLE,
            track_ascent_m            DOUBLE,
            track_descent_m           DOUBLE,

            elevation_min             DOUBLE,
            elevation_max             DOUBLE,
            speed_min                 DOUBLE,
            speed_max                 DOUBLE,
            slope_min                 DOUBLE,
            slope_max                 DOUBLE,

            file_name                 VARCHAR,
            file_data                 BLOB,
            time_stamp                TIMESTAMP
        )
    """)

    con.sql("""
        CREATE TABLE tours (
            tour_id    UUID NOT NULL,
            tour_title VARCHAR
        )
    """)

    con.sql("""
        CREATE TABLE sport (
            sport_id    UUID NOT NULL,
            sport_title VARCHAR
        )
    """)

    con.sql(_TRACK_NOTES_DDL)

    _invalidate_track_caches()


# ---------------------------------------------------------------------------
# GPX-Verarbeitung
# ---------------------------------------------------------------------------
def process_gpx_dataframe(gpx_bytes: bytes) -> gpd.GeoDataFrame:
    """
    Liest die Rohbytes einer GPX-Datei ein und berechnet für JEDEN
    Trackpunkt die abgeleiteten Werte, die sowohl für die Speicherung
    (Kennzahlen, siehe summarize_track) als auch für die Kartenansicht
    (Höhenprofil, Einfärbung) gebraucht werden:

        - lat / lon (vor der Umprojektion gesichert)
        - dist_delta / time_delta -> Distanz & Zeit zum Vorgängerpunkt
        - m_per_s / km_per_h / min_per_km -> Geschwindigkeit
        - distance / time_passed -> kumulierte Strecke / vergangene Zeit
        - ascent / descent -> positiver bzw. negativer Höhenunterschied
        - slope -> Steigung/Gefälle in %
        - ele_normalized -> Höhe relativ zum Startpunkt

    Gibt ein GeoDataFrame zurück (inkl. Geometrie-Spalte, projiziert in das
    passende lokale UTM-System). Für reine Tabellen-Weiterverarbeitung ohne
    Geometrie siehe process_track().
    """
    gdf = gpd.read_file(io.BytesIO(gpx_bytes), layer="track_points")
    if gdf.empty:
        raise ValueError(
            "Die GPX-Datei enthält keine Trackpunkte (<trkpt>). Reine "
            "Wegpunkt- oder Routen-Dateien werden nicht unterstützt."
        )
    gdf = gdf.reset_index(drop=True)
    gdf.crs = "EPSG:4326"  # GPX liefert WGS84 (Grad-Koordinaten)

    # lat/lon als eigene Spalten sichern, BEVOR die Geometrie unten in ein
    # metrisches Koordinatensystem umprojiziert wird.
    gdf["lat"] = gdf.geometry.y
    gdf["lon"] = gdf.geometry.x

    # Umprojizieren ins passende UTM-System (lokal meter-genau), damit
    # geometrische Distanzen direkt in Metern berechnet werden können - in
    # Grad-Koordinaten (EPSG:4326) wären Distanzen nicht maßstabsgetreu.
    gdf = gdf.to_crs(gdf.estimate_utm_crs())

    # Jede Zeile mit ihrem direkten Vorgänger vergleichen, um Zeit- und
    # Distanz-Differenzen zwischen aufeinanderfolgenden Punkten zu berechnen.
    shifted = gdf.shift(1)
    gdf["time_delta"] = gdf["time"] - shifted["time"]
    gdf["dist_delta"] = gdf.distance(shifted)  # Luftlinie in Metern (UTM)
    gdf.at[0, "dist_delta"] = 0  # erster Punkt hat keinen Vorgänger
    gdf.at[0, "time_delta"] = pd.to_timedelta(0)

    # Geschwindigkeit in verschiedenen Einheiten ableiten.
    #
    # WICHTIG: .dt.total_seconds() (nicht .dt.seconds). '.seconds' liefert
    # nur den SEKUNDEN-ANTEIL einer Zeitdifferenz: Bruchteile werden
    # abgeschnitten (0.5 s -> 0 -> Division durch 0 -> Tempo NaN, der Punkt
    # zählt dann nie als "in Bewegung") und nach 24 h springt der Wert
    # zurück auf 0.
    gdf["m_per_s"] = gdf["dist_delta"] / gdf["time_delta"].dt.total_seconds()
    gdf.at[0, "m_per_s"] = 0
    gdf["km_per_h"] = gdf["m_per_s"] * 3.6
    gdf["min_per_km"] = 60 / gdf["km_per_h"]

    gdf["distance"] = gdf["dist_delta"].cumsum()
    gdf["time_passed"] = gdf["time_delta"].cumsum()

    # Höhenänderung zum Vorgänger: in Auf- (positiv) und Abstieg (negativ)
    # aufgeteilt, damit beide Anteile später aufsummiert werden können.
    gdf["ele_delta"] = gdf["ele"] - shifted["ele"]
    gdf["ascent"] = gdf["ele_delta"].clip(lower=0)
    gdf["descent"] = gdf["ele_delta"].clip(upper=0)

    # Steigung/Gefälle in % = Höhenänderung relativ zur zurückgelegten
    # Strecke (die reine Höhenänderung allein ist zwischen Tracks nicht
    # vergleichbar, da sie von der Punktdichte abhängt - % bezogen auf die
    # Distanz schon).
    gdf["slope"] = 100 * gdf["ele_delta"] / gdf["dist_delta"]

    # Höhe relativ zum Startpunkt (für einen alternativen Profil-Vergleich).
    gdf["ele_normalized"] = gdf["ele"] - gdf.iloc[0]["ele"]

    # Division durch 0 km/h (Stillstand) erzeugt +/-inf bei slope/min_per_km
    # -> für sauberes Plotten/Speichern durch NaN ersetzen.
    gdf.replace([np.inf, -np.inf], np.nan, inplace=True)

    return gdf


@st.cache_data(show_spinner="GPX-Daten werden verarbeitet …")
def process_track(track_id: str, _gpx_bytes: bytes) -> pd.DataFrame:
    """
    Gecachte Hülle um process_gpx_dataframe() für die Kartenansicht:
    liefert ein reines DataFrame OHNE Geometrie-Spalte (kleiner & schneller
    zu cachen) für Plotly-Höhenprofil und Folium-Kartenlinien.

    Caching-Strategie: Das Ergebnis wird pro track_id gecacht. Der
    Parametername "_gpx_bytes" beginnt absichtlich mit "_" - Streamlit
    schließt unterstrich-Parameter vom Hashing für den Cache-Key aus.
    Dadurch muss die (oft mehrere hundert KB große) Byte-Folge NICHT bei
    jedem Aufruf gehasht werden; die kleine, schnell zu hashende track_id
    reicht als eindeutiger Schlüssel.
    """
    gdf = process_gpx_dataframe(_gpx_bytes)
    return pd.DataFrame(gdf.drop(columns="geometry"))


def _resolve_gps_accuracy_series(gdf: gpd.GeoDataFrame) -> pd.Series | None:
    """
    Liefert die in der GPX-Datei je Trackpunkt gespeicherte GPS-Genauigkeit
    als Skalierungsfaktor für die Auf-/Abstieg-Schwelle (siehe
    compute_ascent_descent), oder None, falls keine Genauigkeit gespeichert
    wurde.

    Bevorzugt 'vdop' (vertikale Streuung - fachlich passender für
    Höhenwerte als die horizontale), fällt auf 'hdop' zurück, falls die
    GPX-Datei kein vdop enthält. Beide Felder sind Teil des GPX-Standards
    (gpd.read_file(..., layer="track_points") liefert die Spalten immer
    mit), werden aber nicht von jedem Gerät/jeder App tatsächlich befüllt -
    sind ALLE Werte einer Spalte NaN, gilt sie als "nicht gespeichert" und
    die nächste Spalte wird versucht; sind beide leer, wird None
    zurückgegeben (Aufrufer verwendet dann einen festen Schwellwert).

    Werte < 1.0 (überdurchschnittlich präzise Einzel-Fixes) werden auf 1.0
    begrenzt, damit der vom Nutzer eingegebene Schwellwert als UNTERGRENZE
    erhalten bleibt und sich nur bei schlechterer Genauigkeit (dop > 1)
    vergrößert. Einzelne fehlende Werte (NaN) innerhalb einer ansonsten
    befüllten Spalte werden ebenso als 1.0 behandelt, vergrößern den
    Schwellwert an dieser Stelle also nicht.
    """
    for col in ("vdop", "hdop"):
        if col in gdf.columns and gdf[col].notna().any():
            return gdf[col].astype(float).fillna(1.0).clip(lower=1.0)
    return None


def smooth_elevation(gdf: gpd.GeoDataFrame, window: int) -> pd.Series:
    """
    Glättet die Höhenspur mit einem zentrierten gleitenden Mittelwert über
    'window' Trackpunkte.

    Alternative zum Schwellwertverfahren (siehe compute_ascent_descent):
    Das Schwellwertverfahren verwirft Höhenänderungen unterhalb eines
    festen Betrags und unterschätzt dadurch gleichmäßige, flache Anstiege.
    Die Glättung mittelt das Rauschen stattdessen weg und erhält den
    langsamen Trend - bei barometrisch aufgezeichneten Höhen (Sportuhren,
    Radcomputer) liefert das meist die realistischeren Werte. Beide
    Verfahren lassen sich auch kombinieren (erst glätten, dann mit einem
    kleinen Restschwellwert summieren).

    window <= 1 gibt die Höhe unverändert zurück. Am Anfang und Ende des
    Tracks wird über weniger Punkte gemittelt (min_periods=1), statt dort
    NaN zu erzeugen.
    """
    ele = gdf["ele"].astype(float)
    if window is None or window <= 1:
        return ele
    return ele.rolling(window=int(window), center=True, min_periods=1).mean()


def compute_ascent_descent(
    gdf: gpd.GeoDataFrame,
    min_elevation_change_m: float = DEFAULT_MIN_ELEVATION_CHANGE_M,
    use_gps_accuracy: bool = True,
    smoothing_window: int = DEFAULT_ELEVATION_SMOOTHING_WINDOW,
) -> tuple[float, float]:
    """
    Berechnet Auf- und Abstieg über ein Schwellwert-Verfahren mit
    Hysterese, statt einfach alle Punkt-zu-Punkt-Höhendifferenzen
    aufzusummieren: Kleine Höhenschwankungen unterhalb von
    'min_elevation_change_m' (GPS-/Barometer-Rauschen) werden NICHT
    mitgezählt. Erst wenn sich die Höhe gegenüber dem letzten Bezugspunkt
    um mindestens den Schwellwert verändert hat, wird die Differenz dem
    Auf- bzw. Abstieg zugerechnet UND der Bezugspunkt auf die aktuelle Höhe
    zurückgesetzt. Das ist deutlich robuster als die naive Summe aller
    Einzeldifferenzen, die bei dicht aufgezeichneten GPS-Tracks durch
    Messrauschen zu stark überhöhten Werten führt.

    Ist 'use_gps_accuracy' gesetzt UND enthält der Track eine je Punkt
    gespeicherte GPS-Genauigkeit (vdop, ersatzweise hdop - siehe
    _resolve_gps_accuracy_series), wird der Schwellwert je Punkt mit
    diesem Wert skaliert: bei schlechterer Genauigkeit (höherer dop-Wert)
    wird automatisch ein größerer Schwellwert verwendet, bei einem
    optimalen Fix (dop <= 1) bleibt der eingegebene Wert unverändert. Ist
    keine Genauigkeit gespeichert (häufig der Fall, z.B. bei vielen Handy-
    Apps), wird durchgehend der feste Schwellwert verwendet.

    Ist 'smoothing_window' > 1, wird die Höhe vorab mit einem gleitenden
    Mittelwert über so viele Punkte geglättet (siehe smooth_elevation).
    Mit 'min_elevation_change_m = 0' entsteht daraus das reine
    Glättungsverfahren, mit 'smoothing_window = 0' das reine
    Schwellwertverfahren.

    Gibt (ascent_m, descent_m) zurück - descent_m als NEGATIVER Wert,
    passend zur bestehenden Konvention der Tabelle 'gpx'
    (track_descent_m).
    """
    ele = smooth_elevation(gdf, smoothing_window).to_numpy(dtype=float)
    n = len(ele)
    if n == 0 or np.isnan(ele[0]):
        return 0.0, 0.0

    accuracy = _resolve_gps_accuracy_series(gdf) if use_gps_accuracy else None
    if accuracy is not None:
        thresholds = (min_elevation_change_m * accuracy).to_numpy()
    else:
        thresholds = np.full(n, min_elevation_change_m)

    # Sequentielles Verfahren (jeder Schritt hängt vom zuletzt gesetzten
    # Bezugspunkt ab) - bei den hier üblichen Trackgrößen (einige tausend
    # Punkte) ist eine einfache Python-Schleife performant genug und bleibt
    # deutlich lesbarer als eine vektorisierte Variante.
    ascent = 0.0
    descent = 0.0
    ref_ele = ele[0]
    for i in range(1, n):
        if np.isnan(ele[i]):
            continue
        diff = ele[i] - ref_ele
        threshold = thresholds[i]
        if diff >= threshold:
            ascent += diff
            ref_ele = ele[i]
        elif diff <= -threshold:
            descent += diff
            ref_ele = ele[i]
        # |diff| < threshold: als Rauschen ignoriert, ref_ele bleibt stehen
        # und sammelt sich erst bei einer der nächsten Differenzen weiter an.

    return ascent, descent


def compute_moving_time_s(
    gdf: gpd.GeoDataFrame,
    min_speed_moving_kmh: float = DEFAULT_MIN_SPEED_MOVING_KMH,
) -> float:
    """
    Summiert die Zeit-Differenzen ('time_delta', siehe
    process_gpx_dataframe) aller Punkte, deren Geschwindigkeit
    ('km_per_h', bezogen auf den jeweiligen Vorgängerpunkt) mindestens
    'min_speed_moving_kmh' beträgt - ergibt "Zeit in Bewegung" als
    Gegenstück zur reinen Gesamtdauer 'track_time_s' (die auch
    Pausen/Stillstand mit einschließt).

    Punkte mit NaN-Geschwindigkeit (z.B. zwei Punkte mit identischem
    Zeitstempel) zählen NICHT als Bewegung.
    """
    moving = gdf["km_per_h"] >= min_speed_moving_kmh
    seconds = gdf.loc[moving, "time_delta"].dt.total_seconds()
    return float(seconds.sum())


def summarize_track(
    gdf: gpd.GeoDataFrame,
    min_speed_moving_kmh: float = DEFAULT_MIN_SPEED_MOVING_KMH,
    min_elevation_change_m: float = DEFAULT_MIN_ELEVATION_CHANGE_M,
    use_gps_accuracy: bool = True,
    smoothing_window: int = DEFAULT_ELEVATION_SMOOTHING_WINDOW,
) -> dict:
    """
    Fasst ein verarbeitetes Track-DataFrame (siehe process_gpx_dataframe)
    zu den Kennzahlen zusammen, die in der Tabelle 'gpx' pro Track
    gespeichert werden: Gesamtzeit/-strecke, Zeit in Bewegung, Auf-/
    Abstieg, Min/Max von Höhe/Tempo/Steigung sowie die Bounding-Box (für
    den Kartenausschnitt).

    'min_speed_moving_kmh', 'min_elevation_change_m' und
    'use_gps_accuracy' steuern dabei NUR "Zeit in Bewegung" sowie Auf-/
    Abstieg (siehe compute_moving_time_s / compute_ascent_descent) - alle
    übrigen Kennzahlen hängen nicht von diesen Schwellwerten ab.
    """
    ascent_m, descent_m = compute_ascent_descent(
        gdf,
        min_elevation_change_m,
        use_gps_accuracy=use_gps_accuracy,
        smoothing_window=smoothing_window,
    )
    return {
        "track_time_s": gdf.iloc[-1]["time_passed"].total_seconds(),
        "track_time_moving_s": compute_moving_time_s(gdf, min_speed_moving_kmh),
        "track_distance_m": float(gdf.iloc[-1]["distance"]),
        "track_ascent_m": ascent_m,
        "track_descent_m": descent_m,
        "elevation_min": float(gdf["ele"].min()),
        "elevation_max": float(gdf["ele"].max()),
        "speed_min": float(gdf["km_per_h"].min(skipna=True)),
        "speed_max": float(gdf["km_per_h"].max(skipna=True)),
        "slope_min": float(gdf["slope"].min(skipna=True)),
        "slope_max": float(gdf["slope"].max(skipna=True)),
        "location_lat_min": float(gdf["lat"].min()),
        "location_lat_max": float(gdf["lat"].max()),
        "location_lon_min": float(gdf["lon"].min()),
        "location_lon_max": float(gdf["lon"].max()),
    }


# ---------------------------------------------------------------------------
# Bestzeiten innerhalb eines Tracks
# ---------------------------------------------------------------------------
def compute_best_efforts(
    gdf: pd.DataFrame,
    distances: list[tuple[str, float]] | None = None,
) -> pd.DataFrame:
    """
    Sucht je Zieldistanz den SCHNELLSTEN zusammenhängenden Abschnitt
    innerhalb eines Tracks ("Bestleistung" nach Strava-Prinzip): Für 1 km,
    5 km, ... wird über alle möglichen Startpunkte hinweg das Zeitfenster
    gesucht, in dem diese Distanz am schnellsten zurückgelegt wurde - nicht
    nur ab Kilometer 0, sondern an jeder Stelle des Tracks.

    Grundlage sind die bereits in process_gpx_dataframe() berechneten
    kumulierten Spalten 'distance' (Meter) und 'time_passed' (Zeit seit
    Trackstart); es ist also keine erneute Geo-Berechnung nötig.

    Verfahren: ein Zwei-Zeiger-Durchlauf je Zieldistanz. Der hintere Zeiger
    'j' läuft über alle Punkte, der vordere Zeiger 'i' wird so weit
    nachgezogen, wie der Abschnitt dabei noch mindestens die Zieldistanz
    behält - das ist linear in der Punktzahl statt quadratisch.

    Distanzen, die länger sind als der Track selbst, werden übersprungen.
    Gibt ein DataFrame mit den Spalten 'label', 'distance_m', 'time_s',
    'speed_kmh' und 'start_km' (Position im Track) zurück - leer, wenn
    keine Auswertung möglich ist (z.B. GPX ohne Zeitstempel).
    """
    if distances is None:
        distances = BEST_EFFORT_DISTANCES

    columns = ["label", "distance_m", "time_s", "speed_kmh", "start_km"]
    if "time_passed" not in gdf.columns or gdf.empty:
        return pd.DataFrame(columns=columns)

    dist = gdf["distance"].to_numpy(dtype=float)
    seconds = gdf["time_passed"].dt.total_seconds().to_numpy(dtype=float)
    valid = ~(np.isnan(dist) | np.isnan(seconds))
    dist, seconds = dist[valid], seconds[valid]
    if len(dist) < 2:
        return pd.DataFrame(columns=columns)

    # Auf den Trackanfang normieren, damit 'start_km' unabhängig davon ist,
    # ob 'distance' bei 0 beginnt (im Mehrtrack-Profil der Kartenseite läuft
    # die Distanz über mehrere Tracks hinweg weiter).
    offset = dist[0]
    dist = dist - offset
    total_distance = dist[-1]

    rows = []
    for label, target_m in distances:
        if total_distance < target_m:
            continue
        best_time = None
        best_start = 0.0
        i = 0
        for j in range(1, len(dist)):
            while i + 1 <= j and dist[j] - dist[i + 1] >= target_m:
                i += 1
            if dist[j] - dist[i] >= target_m:
                elapsed = seconds[j] - seconds[i]
                if elapsed > 0 and (best_time is None or elapsed < best_time):
                    best_time = elapsed
                    best_start = dist[i]
        if best_time is not None:
            rows.append({
                "label": label,
                "distance_m": target_m,
                "time_s": best_time,
                "speed_kmh": target_m / best_time * 3.6,
                "start_km": best_start / 1000,
            })
    return pd.DataFrame(rows, columns=columns)


# ---------------------------------------------------------------------------
# GPX-Export
# ---------------------------------------------------------------------------
def get_track_file(track_id: str) -> tuple[str, bytes] | None:
    """
    Liefert die ursprünglich hochgeladene GPX-Datei eines Tracks als
    (Dateiname, Bytes) - für den Download-Knopf in der Verwaltung. Gibt
    None zurück, falls der Track nicht existiert.

    Sind zu dem Track Info-Punkte erfasst (siehe Abschnitt "Info-Punkte"),
    werden sie als Wegpunkte (<wpt>) angehängt, damit sie zusammen mit dem
    Track im Zielprogramm ankommen. Ohne Info-Punkte bleiben die Rohbytes
    unverändert.
    """
    con = get_connection()
    row = con.execute(
        "SELECT file_name, file_data FROM gpx WHERE track_id = ?", [track_id]
    ).fetchone()
    if row is None:
        return None
    file_name = row[0] or f"{track_id}.gpx"
    if not file_name.lower().endswith(".gpx"):
        file_name = f"{file_name}.gpx"
    return file_name, _with_note_waypoints(bytes(row[1]), (str(track_id),))


def export_tour_gpx(tour_id: str) -> bytes | None:
    """
    Fasst alle Tracks EINER Tour zu einer einzigen GPX-Datei zusammen -
    z.B. um eine über mehrere Tage aufgezeichnete Mehrtagestour am Stück an
    ein Navigationsgerät oder ein anderes Programm zu übergeben.

    Aufbau der Datei: EIN <trk> mit je einem <trkseg> pro Ausgangstrack, in
    zeitlicher Reihenfolge. Die Aufteilung in Segmente ist wichtig - würden
    alle Punkte in ein einziges Segment geschrieben, würden Auswerter die
    Lücke zwischen zwei Etappen (z.B. die Nacht dazwischen) als
    durchgehende Bewegung interpretieren und Luftlinien quer über die Karte
    zeichnen.

    Die Rohpunkte werden dabei unverändert aus den gespeicherten
    GPX-Dateien übernommen (inkl. Höhe und Zeitstempel). Gibt None zurück,
    wenn die Tour keine Tracks enthält.
    """
    con = get_connection()
    rows = con.execute(
        """
        SELECT track_id, track_title, file_data
        FROM gpx
        WHERE tour_id = ?
        ORDER BY time_start NULLS LAST
        """,
        [tour_id],
    ).fetchall()
    if not rows:
        return None

    tour_title = con.execute(
        "SELECT tour_title FROM tours WHERE tour_id = ?", [tour_id]
    ).fetchone()
    name = (tour_title[0] if tour_title else None) or "Tour"

    merged = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack(name=name)
    merged.tracks.append(track)
    for track_id, track_title, file_data in rows:
        source = gpxpy.parse(bytes(file_data).decode("utf-8", errors="replace"))
        for source_track in source.tracks:
            for segment in source_track.segments:
                if segment.points:
                    track.segments.append(segment)

    # Info-Punkte ALLER Tracks dieser Tour als Wegpunkte mitgeben (siehe
    # Abschnitt "Info-Punkte"): Die Tour-Datei enthält damit dieselben
    # Anmerkungen wie die Einzeltracks, aus denen sie zusammengesetzt ist.
    merged.waypoints.extend(
        build_note_waypoints(load_track_notes(tuple(str(row[0]) for row in rows)))
    )
    return merged.to_xml().encode("utf-8")


def export_selection_gpx(
    track_ids: list[str] | None = None,
    tour_ids: list[str] | None = None,
) -> tuple[str, bytes, str] | None:
    """
    Fasst eine beliebige Auswahl aus einzelnen Tracks und/oder ganzen
    Touren zu EINEM Download zusammen - für den Export-Bereich in der
    Verwaltung (siehe admin.py).

    Jede Tour wird dabei wie bei export_tour_gpx() zu einer einzigen
    GPX-Datei zusammengefasst (ein <trk> mit einem <trkseg> je
    enthaltenem Track); jeder einzeln ausgewählte Track bleibt eine
    eigene Datei. Info-Punkte werden in jedem Fall mit exportiert (siehe
    get_track_file() / export_tour_gpx()).

    Besteht die gesamte Auswahl aus genau EINER Datei (ein Track ODER
    eine Tour), wird diese Datei direkt zurückgegeben. Bei mehreren
    Dateien entsteht stattdessen ein ZIP-Archiv, damit sich auch eine
    gemischte Auswahl aus mehreren Tracks und/oder Touren mit einem
    Klick herunterladen lässt.

    Gibt (Dateiname, Bytes, MIME-Typ) zurück, oder None, wenn die
    Auswahl leer ist oder keine der angegebenen IDs (mehr) existiert
    bzw. keine Tracks enthält.
    """
    files: list[tuple[str, bytes]] = []

    for track_id in track_ids or []:
        result = get_track_file(track_id)
        if result is not None:
            files.append(result)

    if tour_ids:
        con = get_connection()
        for tour_id in tour_ids:
            gpx_bytes = export_tour_gpx(tour_id)
            if gpx_bytes is None:
                continue
            title_row = con.execute(
                "SELECT tour_title FROM tours WHERE tour_id = ?", [tour_id]
            ).fetchone()
            title = (title_row[0] if title_row else None) or "Tour"
            files.append((f"{title}.gpx".replace("/", "_"), gpx_bytes))

    if not files:
        return None

    if len(files) == 1:
        file_name, file_bytes = files[0]
        return file_name, file_bytes, "application/gpx+xml"

    # Mehrere Dateien: als ZIP bündeln. Namenskollisionen (z.B. zwei
    # Tracks mit identischem Titel) werden mit einem Zähler-Suffix
    # aufgelöst, damit keine Datei im Archiv eine andere überschreibt.
    buffer = io.BytesIO()
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_name, file_bytes in files:
            count = used_names.get(file_name, 0)
            used_names[file_name] = count + 1
            if count:
                stem, _, ext = file_name.rpartition(".")
                file_name = f"{stem} ({count}).{ext}" if stem else f"{file_name} ({count})"
            archive.writestr(file_name, file_bytes)

    return "gpx_export.zip", buffer.getvalue(), "application/zip"


# ---------------------------------------------------------------------------
# Info-Punkte ("Punkte zur Tour")
# ---------------------------------------------------------------------------
# Frei platzierbare Anmerkungen zu einem Track (siehe _TRACK_NOTES_DDL).
# Abgrenzung zu den Unterteilungspunkten des Planungsmodus: Jene zerschneiden
# einen Track in Teile und leben nur im Sitzungszustand, diese hier tragen
# Informationen, liegen dauerhaft in der Datenbank und müssen nicht exakt auf
# dem Track liegen. Beim Export werden sie als GPX-Wegpunkte (<wpt>) an den
# zugehörigen Track angehängt.
NOTE_KIND_LABELS = {"info": "Info", "planning": "Planung"}


@st.cache_data(show_spinner=False)
def load_track_notes(track_ids: tuple) -> pd.DataFrame:
    """
    Lädt die Info-Punkte der übergebenen Tracks, sortiert nach Track und
    Position auf dem Track (point_index).

    Gecacht wie die übrigen Leseabfragen der Kartenseite; jede schreibende
    Funktion leert den Cache über _invalidate_track_caches(). Ohne
    track_ids wird ein leeres DataFrame mit den richtigen Spalten
    zurückgegeben, damit aufrufender Anzeige-Code nicht auf fehlende
    Spalten prüfen muss.
    """
    columns = [
        "note_id", "track_id", "note_title", "note_text", "note_kind",
        "lat", "lon", "ele", "point_index", "time_stamp",
    ]
    if not track_ids:
        return pd.DataFrame(columns=columns)

    con = get_connection()
    placeholders = ",".join(["?"] * len(track_ids))
    query = f"""
        SELECT {", ".join(columns)}
        FROM track_notes
        WHERE track_id IN ({placeholders})
        ORDER BY track_id, point_index
    """
    return con.execute(query, list(track_ids)).fetchdf()


def insert_track_note(
    track_id: str,
    note_title: str,
    note_text: str,
    lat: float,
    lon: float,
    ele: float | None,
    point_index: int,
    note_kind: str = "info",
) -> str:
    """Legt einen neuen Info-Punkt an und gibt dessen neue ID zurück."""
    con = get_connection()
    note_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO track_notes
            (note_id, track_id, note_title, note_text, note_kind,
             lat, lon, ele, point_index, time_stamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            note_id, str(track_id), note_title, note_text, note_kind,
            float(lat), float(lon),
            None if ele is None or not np.isfinite(ele) else float(ele),
            int(point_index), datetime.datetime.now(),
        ],
    )
    _invalidate_track_caches()
    return note_id


def update_track_note(
    note_id: str,
    note_title: str,
    note_text: str,
    lat: float,
    lon: float,
    ele: float | None,
    point_index: int,
) -> None:
    """Ändert Text und/oder Position eines bestehenden Info-Punkts."""
    con = get_connection()
    con.execute(
        """
        UPDATE track_notes
        SET note_title = ?, note_text = ?, lat = ?, lon = ?, ele = ?, point_index = ?
        WHERE note_id = ?
        """,
        [
            note_title, note_text, float(lat), float(lon),
            None if ele is None or not np.isfinite(ele) else float(ele),
            int(point_index), str(note_id),
        ],
    )
    _invalidate_track_caches()


def delete_track_note(note_id: str) -> None:
    """Löscht einen Info-Punkt unwiderruflich."""
    con = get_connection()
    con.execute("DELETE FROM track_notes WHERE note_id = ?", [str(note_id)])
    _invalidate_track_caches()


def build_note_waypoints(notes: pd.DataFrame) -> list:
    """
    Wandelt Info-Punkte in GPX-Wegpunkte (<wpt>) um - der gemeinsame Weg,
    auf dem sie in JEDEN Export gelangen (Einzeltrack, Tour, Planungs-ZIP).

    Titel landet in <name>, der Freitext in <desc>, die Art (Info/Planung)
    in <type>. Fehlt die Höhe, bleibt <ele> weg - gängige Programme kommen
    damit besser zurecht als mit einer erfundenen 0.
    """
    waypoints = []
    if notes is None or notes.empty:
        return waypoints
    for _, note in notes.iterrows():
        if pd.isna(note["lat"]) or pd.isna(note["lon"]):
            continue
        waypoints.append(
            gpxpy.gpx.GPXWaypoint(
                latitude=float(note["lat"]),
                longitude=float(note["lon"]),
                elevation=None if pd.isna(note["ele"]) else float(note["ele"]),
                name=note["note_title"] or "Punkt",
                description=None if pd.isna(note["note_text"]) else note["note_text"],
                type=NOTE_KIND_LABELS.get(note["note_kind"], None),
            )
        )
    return waypoints


def _with_note_waypoints(gpx_bytes: bytes, track_ids: tuple) -> bytes:
    """
    Hängt die Info-Punkte der genannten Tracks als Wegpunkte an eine
    fertige GPX-Datei an und gibt sie neu serialisiert zurück.

    Gibt es keine Punkte, werden die Rohbytes UNVERÄNDERT zurückgegeben:
    Ein überflüssiges Parsen/Neuschreiben würde die Originaldatei sonst
    ohne Not umformatieren (Reihenfolge der Attribute, Erweiterungen
    fremder Geräte).
    """
    notes = load_track_notes(track_ids)
    if notes.empty:
        return gpx_bytes
    gpx = gpxpy.parse(bytes(gpx_bytes).decode("utf-8", errors="replace"))
    gpx.waypoints.extend(build_note_waypoints(notes))
    return gpx.to_xml().encode("utf-8")


# ---------------------------------------------------------------------------
# Geocoding & Zeitzone
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def reverse_geocode(lat: float, lon: float) -> dict:
    """
    Reverse-Geocoding eines Punktes über OpenStreetMap/Nominatim.

    Liefert ein Dict mit den für die App relevanten Adressfeldern sowie
    "raw" (komplette Nominatim-Antwort als JSON-String, für die Anzeige im
    Detail bzw. zur Archivierung in der Datenbank). Ergebnis wird gecacht,
    damit derselbe Punkt nicht mehrfach gegen die (rate-limitierte)
    Nominatim-API angefragt wird.

    Ist der Dienst nicht erreichbar (kein Internet, Zeitüberschreitung,
    Rate-Limit), werden alle Felder als None zurückgegeben statt eine
    Ausnahme auszulösen: Der Track lässt sich dann trotzdem hochladen und
    verliert lediglich die Ortsangaben (die Start-/End-Adresse kann später
    ergänzt werden, indem der Track erneut hochgeladen wird).
    """
    geolocator = Nominatim(user_agent="mytrack_app", timeout=10)
    try:
        location = geolocator.reverse((lat, lon))
    except Exception:
        location = None
    address = location.raw.get("address", {}) if location else {}
    return {
        "country": address.get("country"),
        "state": address.get("state"),
        "county": address.get("county"),
        "town": address.get("town") or address.get("city") or address.get("village"),
        "suburb": address.get("suburb"),
        "road": address.get("road"),
        "raw": json.dumps(location.raw) if location else json.dumps({}),
    }


@st.cache_data(show_spinner=False)
def get_timezone(lat: float, lon: float) -> str | None:
    """Ermittelt die IANA-Zeitzone (z.B. 'Europe/Berlin') für einen Punkt."""
    tf = TimezoneFinder()
    return tf.timezone_at(lng=lon, lat=lat)


def _to_local_time(time_series: pd.Series, timezone_str: str | None) -> pd.Series:
    """
    Wandelt eine Zeit-Spalte in die angegebene Zielzeitzone um - robust
    gegenüber GPX-Dateien, deren <time>-Werte KEINE Zeitzone enthalten.

    Laut GPX-Spezifikation sollten Zeitstempel als UTC mit 'Z'-Suffix
    vorliegen (-> "tz-aware" beim Einlesen). Manche Geräte/Exporteure lassen
    das 'Z' jedoch weg, wodurch die Zeiten "tz-naiv" eingelesen werden und
    ein direkter .dt.tz_convert() mit einem TypeError fehlschlagen würde.
    In diesem Fall wird zunächst UTC angenommen (gängigste Annahme für GPX)
    und erst danach in die Zielzeitzone konvertiert.
    """
    if time_series.dt.tz is None:
        time_series = time_series.dt.tz_localize("UTC")
    return time_series.dt.tz_convert(timezone_str)


def process_and_build_track(
    file_name: str,
    file_bytes: bytes,
    track_title: str,
    sport_id: str | None,
    tour_id: str | None,
    min_speed_moving_kmh: float = DEFAULT_MIN_SPEED_MOVING_KMH,
    min_elevation_change_m: float = DEFAULT_MIN_ELEVATION_CHANGE_M,
    use_gps_accuracy: bool = True,
    smoothing_window: int = DEFAULT_ELEVATION_SMOOTHING_WINDOW,
) -> dict:
    """
    Komplette Verarbeitung einer neu hochgeladenen GPX-Datei für den
    Import: GPX einlesen -> Kennzahlen berechnen (inkl. "Zeit in Bewegung"
    und Auf-/Abstieg per Schwellwert, siehe summarize_track) -> Start-/
    Endort per Reverse-Geocoding ermitteln -> Zeitzone bestimmen -> fertigen
    Datensatz für insert_track() zusammenbauen.

    'min_speed_moving_kmh', 'min_elevation_change_m' und
    'use_gps_accuracy' werden direkt an summarize_track() durchgereicht;
    ihre Standardwerte (siehe DEFAULT_MIN_SPEED_MOVING_KMH /
    DEFAULT_MIN_ELEVATION_CHANGE_M oben) sorgen dafür, dass die Berechnung
    bereits beim normalen Hochladen eines Tracks automatisch erfolgt - eine
    spätere Neuberechnung mit abweichenden Werten ist über
    recalculate_track_metadata() möglich (siehe dort, von admin.py genutzt).

    Wird ausschließlich vom Upload-Formular in admin.py aufgerufen; hält
    den UI-Code dort schlank, da die gesamte fachliche Verarbeitung hier
    an einer Stelle gebündelt ist.
    """
    gdf = process_gpx_dataframe(file_bytes)

    lat_start, lon_start = float(gdf.iloc[0]["lat"]), float(gdf.iloc[0]["lon"])
    lat_end, lon_end = float(gdf.iloc[-1]["lat"]), float(gdf.iloc[-1]["lon"])

    timezone_str = get_timezone(lat_start, lon_start)
    local_time = _to_local_time(gdf["time"], timezone_str)
    time_start = local_time.iloc[0]
    time_end = local_time.iloc[-1]

    start_address = reverse_geocode(lat_start, lon_start)
    end_address = reverse_geocode(lat_end, lon_end)

    summary = summarize_track(
        gdf,
        min_speed_moving_kmh=min_speed_moving_kmh,
        min_elevation_change_m=min_elevation_change_m,
        use_gps_accuracy=use_gps_accuracy,
        smoothing_window=smoothing_window,
    )

    return {
        "track_id": str(uuid.uuid4()),
        "track_title": track_title,
        "sport_id": sport_id,
        "tour_id": tour_id,

        "location_start_country": start_address["country"],
        "location_start_state": start_address["state"],
        "location_start_county": start_address["county"],
        "location_start_town": start_address["town"],
        "location_start_suburb": start_address["suburb"],
        "location_start_road": start_address["road"],

        "location_end_country": end_address["country"],
        "location_end_state": end_address["state"],
        "location_end_county": end_address["county"],
        "location_end_town": end_address["town"],
        "location_end_suburb": end_address["suburb"],
        "location_end_road": end_address["road"],

        "location_start_lat_lon": {"lat": lat_start, "lon": lon_start},
        "location_end_lat_lon": {"lat": lat_end, "lon": lon_end},
        "location_start_address": start_address["raw"],
        "location_end_address": end_address["raw"],

        "location_lat_min": summary["location_lat_min"],
        "location_lat_max": summary["location_lat_max"],
        "location_lon_min": summary["location_lon_min"],
        "location_lon_max": summary["location_lon_max"],

        "time_zone": timezone_str,
        "time_start": time_start,
        "time_end": time_end,
        "track_time_s": summary["track_time_s"],
        "track_time_moving_s": summary["track_time_moving_s"],
        "track_distance_m": summary["track_distance_m"],
        "track_ascent_m": summary["track_ascent_m"],
        "track_descent_m": summary["track_descent_m"],

        "elevation_min": summary["elevation_min"],
        "elevation_max": summary["elevation_max"],
        "speed_min": summary["speed_min"],
        "speed_max": summary["speed_max"],
        "slope_min": summary["slope_min"],
        "slope_max": summary["slope_max"],

        "file_name": file_name,
        "file_data": file_bytes,
        "time_stamp": datetime.datetime.now().isoformat(),
    }



# ---------------------------------------------------------------------------
# CRUD: Sportarten
# ---------------------------------------------------------------------------
def get_sports() -> pd.DataFrame:
    """Alle Sportarten (id + Titel), alphabetisch sortiert."""
    con = get_connection()
    return con.sql(
        "SELECT sport_id, sport_title FROM sport ORDER BY sport_title"
    ).fetchdf()


def sports_options(include_none: bool = True) -> list[tuple]:
    """
    Sportarten als Liste von (id, titel)-Tupeln, passend für st.selectbox
    (options=..., format_func=lambda x: x[1]).

    include_none=True stellt zusätzlich eine "keine Zuordnung"-Option an
    erster Stelle bereit, damit Tracks auch ohne (oder vor Anlage einer)
    Sportart hochgeladen werden können.
    """
    options = list(get_sports().itertuples(index=False, name=None))
    if include_none:
        options = [(None, "– keine Sportart –")] + options
    return options


def get_sports_overview() -> pd.DataFrame:
    """Sportarten zusammen mit den zugehörigen Touren/Tracks, für die Übersichtstabelle."""
    con = get_connection()
    return con.sql("""
        SELECT
            sport.sport_title  AS "Sport",
            tours.tour_title   AS "Tour",
            gpx.track_title    AS "Track",
            gpx.time_start     AS "Start",
            gpx.track_distance_m AS "Distanz (m)"
        FROM sport
        LEFT JOIN gpx   ON gpx.sport_id = sport.sport_id
        LEFT JOIN tours ON gpx.tour_id  = tours.tour_id
        ORDER BY sport.sport_title, gpx.time_start
    """).fetchdf()


def insert_sport(sport_title: str) -> str:
    """Legt eine neue Sportart an und gibt deren neue ID zurück."""
    con = get_connection()
    sport_id = str(uuid.uuid4())
    con.execute("INSERT INTO sport VALUES (?, ?)", [sport_id, sport_title])
    _invalidate_track_caches()
    return sport_id


def update_sport(sport_id: str, sport_title: str) -> None:
    """Benennt eine bestehende Sportart um."""
    con = get_connection()
    con.execute(
        "UPDATE sport SET sport_title = ? WHERE sport_id = ?", [sport_title, sport_id]
    )
    _invalidate_track_caches()


def delete_sport(sport_id: str) -> None:
    """
    Löscht eine Sportart unwiderruflich.

    Tracks, die dieser Sportart zugeordnet waren, bleiben erhalten und
    verlieren lediglich die Zuordnung (sport_id wird NULL), damit keine
    Tracks durch das Löschen einer Sportart verloren gehen.
    """
    con = get_connection()
    con.execute("UPDATE gpx SET sport_id = NULL WHERE sport_id = ?", [sport_id])
    con.execute("DELETE FROM sport WHERE sport_id = ?", [sport_id])
    _invalidate_track_caches()


# ---------------------------------------------------------------------------
# CRUD: Touren
# ---------------------------------------------------------------------------
def get_tours() -> pd.DataFrame:
    """Alle Touren (id + Titel), alphabetisch sortiert."""
    con = get_connection()
    return con.sql(
        "SELECT tour_id, tour_title FROM tours ORDER BY tour_title"
    ).fetchdf()


def tours_options(include_none: bool = True) -> list[tuple]:
    """Touren als Liste von (id, titel)-Tupeln, passend für st.selectbox."""
    options = list(get_tours().itertuples(index=False, name=None))
    if include_none:
        options = [(None, "– keine Tour –")] + options
    return options


def get_tours_overview() -> pd.DataFrame:
    """Touren zusammen mit den zugehörigen Tracks, für die Übersichtstabelle."""
    con = get_connection()
    return con.sql("""
        SELECT
            tours.tour_title      AS "Tour",
            gpx.track_title       AS "Track",
            sport.sport_title     AS "Sport",
            gpx.time_start        AS "Start",
            gpx.time_end          AS "Ende",
            gpx.track_distance_m  AS "Distanz (m)"
        FROM tours
        LEFT JOIN gpx   ON tours.tour_id = gpx.tour_id
        LEFT JOIN sport ON gpx.sport_id  = sport.sport_id
        ORDER BY tours.tour_title, gpx.time_start
    """).fetchdf()


def insert_tour(tour_title: str) -> str:
    """Legt eine neue Tour an und gibt deren neue ID zurück."""
    con = get_connection()
    tour_id = str(uuid.uuid4())
    con.execute("INSERT INTO tours VALUES (?, ?)", [tour_id, tour_title])
    _invalidate_track_caches()
    return tour_id


def update_tour(tour_id: str, tour_title: str) -> None:
    """Benennt eine bestehende Tour um."""
    con = get_connection()
    con.execute(
        "UPDATE tours SET tour_title = ? WHERE tour_id = ?", [tour_title, tour_id]
    )
    _invalidate_track_caches()


def delete_tour(tour_id: str) -> None:
    """
    Löscht eine Tour unwiderruflich.

    Tracks, die dieser Tour zugeordnet waren, bleiben erhalten und
    verlieren lediglich die Zuordnung (tour_id wird NULL).
    """
    con = get_connection()
    con.execute("UPDATE gpx SET tour_id = NULL WHERE tour_id = ?", [tour_id])
    con.execute("DELETE FROM tours WHERE tour_id = ?", [tour_id])
    _invalidate_track_caches()


# ---------------------------------------------------------------------------
# CRUD: Tracks
# ---------------------------------------------------------------------------
def get_tracks() -> pd.DataFrame:
    """
    Liefert alle Tracks inkl. Sport-/Tour-Titel (aber bewusst OHNE die
    teils großen GPX-Binärdaten) - für Übersichts- und
    Bearbeitungs-Ansichten in admin.py.
    """
    con = get_connection()
    return con.sql("""
        SELECT
            gpx.track_id, gpx.track_title,
            gpx.sport_id, sport.sport_title,
            gpx.tour_id, tours.tour_title,
            gpx.time_start, gpx.time_end,
            gpx.track_distance_m, gpx.track_time_s, gpx.track_time_moving_s,
            gpx.track_ascent_m, gpx.track_descent_m,
            gpx.location_start_county, gpx.location_end_county,
            gpx.file_name
        FROM gpx
        LEFT JOIN sport ON gpx.sport_id = sport.sport_id
        LEFT JOIN tours ON gpx.tour_id  = tours.tour_id
        ORDER BY gpx.time_start DESC
    """).fetchdf()


# ---------------------------------------------------------------------------
# Gecachte Leseabfragen für die Kartenseite
# ---------------------------------------------------------------------------
# Beide Abfragen werden bei jeder Nutzerinteraktion auf der Kartenseite
# gebraucht und deshalb gecacht. Statt eines Zeitablaufs (ttl) leert
# _invalidate_track_caches() den Cache gezielt bei jeder Änderung an Tracks,
# Touren oder Sportarten - dadurch ist die Karte sofort aktuell, ohne dass
# die Abfragen bei jedem Klick erneut laufen.
@st.cache_data(show_spinner=False)
def load_metadata() -> pd.DataFrame:
    """
    Lädt nur die "leichten" Metadaten aller Tracks (Titel, Bounding-Box,
    Min/Max-Werte für Tempo/Höhe/Gefälle, Kennzahlen wie Distanz/Dauer/
    Auf-/Abstieg sowie Start-/End-Land, ...) - bewusst OHNE die teils
    großen GPX-Binärdaten (file_data). Diese Metadaten werden für die
    Sidebar-Filter (Sport/Land/Tour/Track-Auswahl) sowie die Kennzahlen-
    Anzeige der Kartenseite gebraucht.

    WICHTIG: Die Abfrage geht bewusst von 'gpx' aus (nicht von 'tours'),
    damit auch Tracks OHNE zugeordnete Tour angezeigt werden - bei einer
    Abfrage ausgehend von 'tours' würden solche Tracks durch den JOIN
    stillschweigend herausfallen.
    """
    con = get_connection()
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


@st.cache_data(show_spinner=False)
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
    con = get_connection()
    placeholders = ",".join(["?"] * len(track_ids))
    query = f"SELECT track_id, file_data FROM gpx WHERE track_id IN ({placeholders})"
    return con.execute(query, list(track_ids)).fetchdf()


@st.cache_data(show_spinner="Heatmap wird aufgebaut …")
def load_heatmap_points(stride: int = 10) -> list[list[float]]:
    """
    Liefert die Koordinaten ALLER gespeicherten Tracks als flache Liste
    [[lat, lon], ...] für die Heatmap auf der Statistik-Seite.

    'stride' dünnt die Punkte aus (jeder n-te Punkt): Eine Heatmap über
    Hunderte Tracks mit je mehreren tausend Punkten wäre sonst sowohl in
    der Übertragung als auch im Browser unnötig schwer - für die Aussage
    "wo war ich unterwegs" ändert ein ausgedünnter Track das Bild nicht.

    Gelesen wird direkt mit gpxpy statt über process_gpx_dataframe(), da
    hier nur lat/lon gebraucht werden und die vollständige GeoPandas-
    Aufbereitung (Umprojektion, Tempo, Steigung) dafür zu teuer wäre.
    """
    con = get_connection()
    rows = con.sql("SELECT file_data FROM gpx").fetchall()

    points: list[list[float]] = []
    step = max(1, int(stride))
    for (file_data,) in rows:
        try:
            gpx = gpxpy.parse(bytes(file_data).decode("utf-8", errors="replace"))
        except Exception:
            continue  # beschädigte Datei überspringen statt die Seite abzubrechen
        for track in gpx.tracks:
            for segment in track.segments:
                for point in segment.points[::step]:
                    points.append([point.latitude, point.longitude])
    return points


def rename_tracks(titles: dict) -> int:
    """
    Benennt mehrere Tracks auf einmal um (track_id -> neuer Titel) - für
    das direkte Bearbeiten in der Übersichtstabelle der Verwaltung
    (st.data_editor). Gibt die Anzahl der geänderten Tracks zurück.
    """
    con = get_connection()
    for track_id, track_title in titles.items():
        con.execute(
            "UPDATE gpx SET track_title = ? WHERE track_id = ?",
            [track_title, track_id],
        )
    if titles:
        _invalidate_track_caches()
    return len(titles)


def _invalidate_track_caches() -> None:
    """
    Leert die Lese-Caches der Kartenseite. Wird von allen schreibenden
    Funktionen (insert/update/delete/recalculate) aufgerufen, damit
    Änderungen aus der Verwaltung sofort auf der Karte sichtbar sind.

    Bewusst NICHT st.cache_data.clear(): das würde auch den (teuren)
    Cache der GPX-Verarbeitung (process_track) verwerfen.
    """
    load_metadata.clear()
    load_track_files.clear()
    load_heatmap_points.clear()
    load_track_notes.clear()


def insert_track(data: dict) -> None:
    """
    Fügt einen neuen, bereits vollständig berechneten Track in die Tabelle
    'gpx' ein. 'data' muss alle Felder enthalten, die
    process_and_build_track() erzeugt.

    Die Werte werden zunächst in ein einzeiliges DataFrame ("row") gepackt
    und per INSERT...SELECT...FROM mit expliziten CASTs eingefügt - so
    übernimmt DuckDB die Umwandlung in die richtigen Spaltentypen (u.a.
    UUID, STRUCT und JSON), ohne dass jeder Wert einzeln manuell konvertiert
    werden muss. ("row" wird hier nicht direkt benutzt, sondern von DuckDB
    per Namens-Erkennung ("replacement scan") in der SQL-Abfrage gefunden.)
    """
    con = get_connection()
    row = pd.DataFrame([data])  # noqa: F841 (von DuckDB per Namen referenziert)

    # WICHTIG: Die Ziel-Spaltenliste wird hier bewusst EXPLIZIT angegeben
    # (statt "INSERT INTO gpx SELECT ... FROM row"). Ohne sie ordnet DuckDB
    # die SELECT-Spalten dem Ziel rein POSITIONAL zu - das bricht, sobald
    # die physische Spaltenreihenfolge der Tabelle von der hier (und in
    # init_database()) verwendeten Reihenfolge abweicht. Genau das passiert
    # bei per ALTER TABLE ... ADD COLUMN nachträglich ergänzten Spalten
    # (siehe _ensure_schema_migrations): DuckDB hängt neue Spalten IMMER
    # ans Ende der Tabelle an, unabhängig davon, wo sie in init_database()
    # "logisch" stehen. Bei Bestandsdatenbanken landet 'track_time_moving_s'
    # also tatsächlich als letzte Spalte, nicht zwischen 'track_time_s' und
    # 'track_distance_m'. Eine rein positionale Zuordnung verschiebt dann
    # alle nachfolgenden Werte um eins - mit dem Ergebnis, dass am Ende
    # 'file_data' (BLOB) in die 'time_stamp'-Spalte (TIMESTAMP) einsortiert
    # wird, was den (sonst kryptischen) Fehler "Unimplemented type for cast
    # (BLOB -> TIMESTAMP)" auslöst. Mit expliziter Spaltenliste matcht
    # DuckDB stattdessen über die NAMEN und ist damit unabhängig von der
    # physischen Speicherreihenfolge.
    con.sql("""
        INSERT INTO gpx (
            track_id, track_title, sport_id, tour_id,

            location_start_country, location_start_state, location_start_county,
            location_start_town, location_start_suburb, location_start_road,

            location_end_country, location_end_state, location_end_county,
            location_end_town, location_end_suburb, location_end_road,

            location_start_lat_lon, location_end_lat_lon,
            location_start_address, location_end_address,

            location_lat_min, location_lat_max, location_lon_min, location_lon_max,

            time_zone, time_start, time_end, track_time_s, track_time_moving_s,
            track_distance_m, track_ascent_m, track_descent_m,

            elevation_min, elevation_max, speed_min, speed_max, slope_min, slope_max,

            file_name, file_data, time_stamp
        )
        SELECT
            CAST(track_id AS UUID)            AS track_id,
            TRY_CAST(track_title AS VARCHAR)  AS track_title,
            TRY_CAST(sport_id AS UUID)        AS sport_id,
            TRY_CAST(tour_id AS UUID)         AS tour_id,

            TRY_CAST(location_start_country AS VARCHAR) AS location_start_country,
            TRY_CAST(location_start_state   AS VARCHAR) AS location_start_state,
            TRY_CAST(location_start_county  AS VARCHAR) AS location_start_county,
            TRY_CAST(location_start_town    AS VARCHAR) AS location_start_town,
            TRY_CAST(location_start_suburb  AS VARCHAR) AS location_start_suburb,
            TRY_CAST(location_start_road    AS VARCHAR) AS location_start_road,

            TRY_CAST(location_end_country   AS VARCHAR) AS location_end_country,
            TRY_CAST(location_end_state     AS VARCHAR) AS location_end_state,
            TRY_CAST(location_end_county    AS VARCHAR) AS location_end_county,
            TRY_CAST(location_end_town      AS VARCHAR) AS location_end_town,
            TRY_CAST(location_end_suburb    AS VARCHAR) AS location_end_suburb,
            TRY_CAST(location_end_road      AS VARCHAR) AS location_end_road,

            CAST(location_start_lat_lon AS STRUCT(lat DOUBLE, lon DOUBLE)) AS location_start_lat_lon,
            CAST(location_end_lat_lon   AS STRUCT(lat DOUBLE, lon DOUBLE)) AS location_end_lat_lon,
            CAST(location_start_address AS JSON) AS location_start_address,
            CAST(location_end_address   AS JSON) AS location_end_address,

            CAST(location_lat_min AS DOUBLE) AS location_lat_min,
            CAST(location_lat_max AS DOUBLE) AS location_lat_max,
            CAST(location_lon_min AS DOUBLE) AS location_lon_min,
            CAST(location_lon_max AS DOUBLE) AS location_lon_max,

            CAST(time_zone AS VARCHAR)        AS time_zone,
            TRY_CAST(time_start AS TIMESTAMP) AS time_start,
            TRY_CAST(time_end   AS TIMESTAMP) AS time_end,
            CAST(track_time_s     AS DOUBLE)  AS track_time_s,
            CAST(track_time_moving_s AS DOUBLE) AS track_time_moving_s,
            CAST(track_distance_m AS DOUBLE)  AS track_distance_m,
            CAST(track_ascent_m   AS DOUBLE)  AS track_ascent_m,
            CAST(track_descent_m  AS DOUBLE)  AS track_descent_m,

            CAST(elevation_min AS DOUBLE) AS elevation_min,
            CAST(elevation_max AS DOUBLE) AS elevation_max,
            CAST(speed_min     AS DOUBLE) AS speed_min,
            CAST(speed_max     AS DOUBLE) AS speed_max,
            CAST(slope_min     AS DOUBLE) AS slope_min,
            CAST(slope_max     AS DOUBLE) AS slope_max,

            CAST(file_name AS VARCHAR) AS file_name,
            CAST(file_data AS BLOB)    AS file_data,
            TRY_CAST(time_stamp AS TIMESTAMP) AS time_stamp
        FROM row
    """)
    _invalidate_track_caches()


def update_track(track_id: str, track_title: str, sport_id: str | None, tour_id: str | None) -> None:
    """Aktualisiert Titel sowie Sport-/Tour-Zuordnung eines bestehenden Tracks."""
    con = get_connection()
    con.execute(
        "UPDATE gpx SET track_title = ?, sport_id = ?, tour_id = ? WHERE track_id = ?",
        [track_title, sport_id, tour_id, track_id],
    )
    _invalidate_track_caches()


def delete_track(track_id: str) -> None:
    """
    Löscht einen Track (inkl. der gespeicherten GPX-Datei) unwiderruflich.

    Die Info-Punkte des Tracks werden mitgelöscht: Sie beziehen sich über
    'point_index' auf genau diesen Track und wären ohne ihn sinnlos
    (anders als bei Sport/Tour, wo die Zuordnung nur entfällt).
    """
    con = get_connection()
    con.execute("DELETE FROM track_notes WHERE track_id = ?", [track_id])
    con.execute("DELETE FROM gpx WHERE track_id = ?", [track_id])
    _invalidate_track_caches()


# ---------------------------------------------------------------------------
# Neuberechnung von Track-Metadaten
# ---------------------------------------------------------------------------
# Wird vom entsprechenden Bereich in admin.py genutzt, um "Zeit in
# Bewegung" sowie Auf-/Abstieg bereits gespeicherter Tracks anhand neu
# eingegebener Schwellwerte neu zu berechnen - z.B. wenn sich die beim
# Hochladen verwendeten Standardwerte (DEFAULT_MIN_SPEED_MOVING_KMH /
# DEFAULT_MIN_ELEVATION_CHANGE_M) im Nachhinein als ungeeignet für einen
# bestimmten Tracktyp (z.B. sehr langsames Wandern vs. schnelles Radfahren)
# herausstellen. Alle übrigen Kennzahlen (Distanz, Gesamtdauer, Min/Max-
# Werte, Start-/Endort, ...) bleiben unverändert, da sie nicht von diesen
# Schwellwerten abhängen und daher nicht neu berechnet werden müssen.
def recalculate_track_metadata(
    track_id: str,
    min_speed_moving_kmh: float = DEFAULT_MIN_SPEED_MOVING_KMH,
    min_elevation_change_m: float = DEFAULT_MIN_ELEVATION_CHANGE_M,
    use_gps_accuracy: bool = True,
    smoothing_window: int = DEFAULT_ELEVATION_SMOOTHING_WINDOW,
) -> None:
    """
    Berechnet 'Zeit in Bewegung' sowie Auf-/Abstieg EINES bestehenden
    Tracks aus der gespeicherten GPX-Rohdatei (Spalte 'file_data') neu und
    schreibt die aktualisierten Werte zurück in die Tabelle 'gpx'.

    Löst KeyError/ValueError aus, falls 'track_id' nicht existiert.
    """
    con = get_connection()
    row = con.execute(
        "SELECT file_data FROM gpx WHERE track_id = ?", [track_id]
    ).fetchone()
    if row is None:
        raise ValueError(f"Track {track_id!r} wurde nicht gefunden.")

    gdf = process_gpx_dataframe(row[0])
    ascent_m, descent_m = compute_ascent_descent(
        gdf,
        min_elevation_change_m,
        use_gps_accuracy=use_gps_accuracy,
        smoothing_window=smoothing_window,
    )
    moving_s = compute_moving_time_s(gdf, min_speed_moving_kmh)

    con.execute(
        """
        UPDATE gpx
        SET track_ascent_m = ?, track_descent_m = ?, track_time_moving_s = ?
        WHERE track_id = ?
        """,
        [ascent_m, descent_m, moving_s, track_id],
    )
    _invalidate_track_caches()


def recalculate_all_tracks_metadata(
    min_speed_moving_kmh: float = DEFAULT_MIN_SPEED_MOVING_KMH,
    min_elevation_change_m: float = DEFAULT_MIN_ELEVATION_CHANGE_M,
    use_gps_accuracy: bool = True,
    smoothing_window: int = DEFAULT_ELEVATION_SMOOTHING_WINDOW,
) -> int:
    """
    Wie recalculate_track_metadata(), aber für ALLE vorhandenen Tracks auf
    einmal (z.B. nach Anpassung der Standard-Schwellwerte für die gesamte
    Sammlung). Gibt die Anzahl der neu berechneten Tracks zurück.
    """
    con = get_connection()
    track_ids = con.sql("SELECT track_id FROM gpx").fetchdf()["track_id"]
    for track_id in track_ids:
        recalculate_track_metadata(
            str(track_id),
            min_speed_moving_kmh=min_speed_moving_kmh,
            min_elevation_change_m=min_elevation_change_m,
            use_gps_accuracy=use_gps_accuracy,
            smoothing_window=smoothing_window,
        )
    return len(track_ids)

