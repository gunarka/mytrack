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
    - CRUD-Funktionen (Create/Read/Update/Delete) für die drei Tabellen
      'sport', 'tours' und 'gpx' (Abschnitt "CRUD: ...")

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
import uuid

import duckdb
import geopandas as gpd
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
    Bewegung").

    Wird bei JEDEM Verbindungsaufbau aufgerufen, ist also ein no-op, sobald
    die Spalte einmal existiert. Dadurch müssen Bestandsnutzer ihre Daten
    nicht über init_database() (= kompletter Datenverlust!) neu anlegen,
    nur weil eine neue Kennzahl hinzugekommen ist. Existiert die Tabelle
    'gpx' noch gar nicht (frischer Checkout vor dem ersten Lauf von
    init.py), passiert ebenfalls nichts - init_database() legt sie dann
    direkt inklusive aller aktuellen Spalten an.
    """
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


def init_database() -> None:
    """
    Legt die drei Tabellen 'gpx', 'tours' und 'sport' neu an.

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
    gdf["m_per_s"] = gdf["dist_delta"] / gdf["time_delta"].dt.seconds
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


def compute_ascent_descent(
    gdf: gpd.GeoDataFrame,
    min_elevation_change_m: float = DEFAULT_MIN_ELEVATION_CHANGE_M,
    use_gps_accuracy: bool = True,
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

    Gibt (ascent_m, descent_m) zurück - descent_m als NEGATIVER Wert,
    passend zur bestehenden Konvention der Tabelle 'gpx'
    (track_descent_m).
    """
    ele = gdf["ele"].to_numpy(dtype=float)
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
        gdf, min_elevation_change_m, use_gps_accuracy=use_gps_accuracy
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
    """
    geolocator = Nominatim(user_agent="gps_tracking_app")
    location = geolocator.reverse((lat, lon))
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
    return sport_id


def update_sport(sport_id: str, sport_title: str) -> None:
    """Benennt eine bestehende Sportart um."""
    con = get_connection()
    con.execute(
        "UPDATE sport SET sport_title = ? WHERE sport_id = ?", [sport_title, sport_id]
    )


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
    return tour_id


def update_tour(tour_id: str, tour_title: str) -> None:
    """Benennt eine bestehende Tour um."""
    con = get_connection()
    con.execute(
        "UPDATE tours SET tour_title = ? WHERE tour_id = ?", [tour_title, tour_id]
    )


def delete_tour(tour_id: str) -> None:
    """
    Löscht eine Tour unwiderruflich.

    Tracks, die dieser Tour zugeordnet waren, bleiben erhalten und
    verlieren lediglich die Zuordnung (tour_id wird NULL).
    """
    con = get_connection()
    con.execute("UPDATE gpx SET tour_id = NULL WHERE tour_id = ?", [tour_id])
    con.execute("DELETE FROM tours WHERE tour_id = ?", [tour_id])


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


def update_track(track_id: str, track_title: str, sport_id: str | None, tour_id: str | None) -> None:
    """Aktualisiert Titel sowie Sport-/Tour-Zuordnung eines bestehenden Tracks."""
    con = get_connection()
    con.execute(
        "UPDATE gpx SET track_title = ?, sport_id = ?, tour_id = ? WHERE track_id = ?",
        [track_title, sport_id, tour_id, track_id],
    )


def delete_track(track_id: str) -> None:
    """Löscht einen Track (inkl. der gespeicherten GPX-Datei) unwiderruflich."""
    con = get_connection()
    con.execute("DELETE FROM gpx WHERE track_id = ?", [track_id])


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
        gdf, min_elevation_change_m, use_gps_accuracy=use_gps_accuracy
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


def recalculate_all_tracks_metadata(
    min_speed_moving_kmh: float = DEFAULT_MIN_SPEED_MOVING_KMH,
    min_elevation_change_m: float = DEFAULT_MIN_ELEVATION_CHANGE_M,
    use_gps_accuracy: bool = True,
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
        )
    return len(track_ids)

