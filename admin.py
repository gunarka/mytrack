import streamlit as st                  # frontend framework
import duckdb as duckdb                 # db storage 
import pandas as pd                     # dataframe manipulation
import numpy as np                      # numerical operations
import gpxpy as gpxpy                   # gpx parsing
import gpxpy.gpx as gpxpy_gpx           # gpxpy submodule for gpx parsing
from geopy.geocoders import Nominatim   # geocoding library for reverse geocoding
import geopandas as gpd                 # geospatial data manipulation
from timezonefinder import TimezoneFinder # timezone lookup based on lat/lon
import datetime                   # for handling timestamps
import json                 # for handling JSON data
import uuid            # for generating UUIDs

DB_PATH      = ".data/tracks.duckdb"
con = duckdb.connect(database=str(DB_PATH))
tours = con.execute("SELECT tour_id, tour_title FROM tours").fetchall()
sports = con.execute("SELECT sport_id, sport_title FROM sport").fetchall()

tab_track, tab_tour, tab_sport = st.tabs(   ["Tracks", "Touren", "Sportarten"])

with tab_track:
    
    

    with st.form(key="track_form", clear_on_submit=True):
        gpx_file = st.file_uploader("Upload a GPX file", type=["gpx"])
        track_title = st.text_input("Track Titel")
        tour = st.selectbox("Tour", tours, format_func=lambda t: t[1])
        sport = st.selectbox("Sport", sports, format_func=lambda s: s[1])
        submitted = st.form_submit_button("Verarbeiten und Speichern")

        if gpx_file is not None and submitted == True:    
        
            if track_title == "":
                track_title = gpx_file.name

            st.success(f"Uploaded file: {gpx_file.name} ({track_title})")

            gdf = gpd.read_file(gpx_file.getvalue(), layer='track_points')
            ddf = pd.DataFrame()

            lat_start = gdf.iloc[0]['geometry'].y
            lon_start = gdf.iloc[0]['geometry'].x
            
            lat_end = gdf.iloc[-1]['geometry'].y
            lon_end = gdf.iloc[-1]['geometry'].x
        



            geolocator = Nominatim(user_agent="my_app")
            location_start = geolocator.reverse((lat_start, lon_start))
            location_end = geolocator.reverse((lat_end, lon_end))

            # Initialize the finder
            tf = TimezoneFinder()
            timezone_str = tf.timezone_at(lng=lon_start, lat=lat_start) 
            time_start = gdf['time'].dt.tz_convert(timezone_str).iloc[0]
            time_end = gdf['time'].dt.tz_convert(timezone_str).iloc[-1]



            gdf.crs = "EPSG:4326"
            gdf = gdf.to_crs(gdf.estimate_utm_crs())

            shifted_gdf = gdf.shift(1)
            gdf['time_delta'] = gdf['time'] - shifted_gdf['time']  
            gdf['dist_delta'] = gdf.distance(shifted_gdf)

            # speed in various formats
            gdf['m_per_s'] = gdf['dist_delta'] / gdf.time_delta.dt.seconds 
            gdf['km_per_h'] = gdf['m_per_s'] * 3.6
            gdf['min_per_km'] = 60 / (gdf['km_per_h'])

            gdf['distance'] = gdf['dist_delta'].cumsum()

            gdf['time_passed'] = gdf['time_delta'].cumsum()


            # ascent is elevation delta, but only positive values
            gdf['ele_delta'] = gdf['ele'] - shifted_gdf['ele'] 
            gdf['ascent'] = gdf['ele_delta']
            gdf.loc[gdf.ascent < 0, ['ascent']] = 0



            gdf['descent'] = gdf['ele_delta']
            gdf.loc[gdf.descent > 0, ['descent']] = 0


            # Slope in %
            # (since ele_delta is not really comparable)
            gdf['slope'] = 100 * gdf['ele_delta'] / gdf['dist_delta']

            # Ele normalized: Startpoint as 0
            gdf['ele_normalized'] = gdf['ele'] - gdf.loc[0]['ele']

            # slope and min_per_km can be infinite if 0 km/h
            # Replace inf with nan for better plotting
            gdf.replace(np.inf, np.nan, inplace=True)
            gdf.replace(-np.inf, np.nan, inplace=True)

       

            

           
            ddf['track_id'] = [str(uuid.uuid4())]  
            ddf['track_title'] = [track_title]
            ddf['sport_id'] = [sport[0]]  # sport_id from selected sport in dropdown
            ddf['tour_id'] = [tour[0]]  # tour_id from selected tour in dropdown
            ddf['location_start_country'] = [location_start.raw.get("address").get("country")]
            ddf['location_start_state'] = [location_start.raw.get("address").get("state")]
            ddf['location_start_county'] = [location_start.raw.get("address").get("county")]
            ddf['location_start_town'] = [location_start.raw.get("address").get("town")]
            ddf['location_start_suburb'] = [location_start.raw.get("address").get("suburb")]
            ddf['location_start_road'] = [location_start.raw.get("address").get("road")]
            ddf['location_end_country'] = [location_end.raw.get("address").get("country")]
            ddf['location_end_state'] = [location_end.raw.get("address").get("state")]
            ddf['location_end_county'] = [location_end.raw.get("address").get("county")]
            ddf['location_end_town'] = [location_end.raw.get("address").get("town")]
            ddf['location_end_suburb'] = [location_end.raw.get("address").get("suburb")]
            ddf['location_end_road'] = [location_end.raw.get("address").get("road")]
            ddf['location_start_lat_lon'] = [{"lat": lat_start, "lon": lon_start}]
            ddf['location_lat_min']
            ddf['location_lat_max']
            ddf['location_lon_min']
            ddf['location_lon_max']
            ddf['location_end_lat_lon'] = [{"lat": lat_end, "lon": lon_end}]
            ddf['location_start_address'] = [json.dumps(location_start.raw)]
            ddf['location_end_address']   = [json.dumps(location_end.raw)]
            ddf['time_zone'] = [timezone_str]
            ddf['time_start'] = [time_start]
            ddf['time_end'] = [time_end]
            ddf['track_time_s'] = [gdf.iloc[-1]['time_passed'].total_seconds()]
            ddf['track_distance_m'] = [gdf.iloc[-1]['distance']]
            ddf['track_ascent_m'] = [gdf['ascent'].sum()]
            ddf['track_descent_m'] = [gdf['descent'].sum()]
            ddf["elevation_min"] 
            ddf["elevation_max"] 
            ddf["speed_min"]
            ddf["speed_max"]
            ddf['file_name'] =   [gpx_file.name]
            ddf['file_data'] = [gpx_file.getvalue()]
            ddf['time_stamp'] = [datetime.datetime.now().isoformat()]


        
            con.sql("""
            INSERT INTO gpx
            SELECT
                CAST(track_id AS UUID)   AS track_id,
                TRY_CAST(track_title AS VARCHAR)   AS track_title,
                CAST(sport_id AS UUID)   AS sport_id,
                TRY_CAST(tour_id AS UUID)   AS tour_id,

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

                CAST(time_zone          AS VARCHAR)   AS time_zone,
                TRY_CAST(time_start     AS TIMESTAMP) AS time_start,
                TRY_CAST(time_end       AS TIMESTAMP) AS time_end,
                CAST(track_time_s       AS DOUBLE)    AS track_time_s,
                CAST(track_distance_m   AS DOUBLE)    AS track_distance_m,
                CAST(track_ascent_m     AS DOUBLE)    AS track_ascent_m,
                CAST(track_descent_m    AS DOUBLE)    AS track_descent_m,

                CAST(file_name          AS VARCHAR)   AS file_name,
                CAST(file_data          AS BLOB)      AS file_data,
                TRY_CAST(time_stamp     AS TIMESTAMP) AS time_stamp
            FROM ddf
            """)
            

            
            st.success("GPX file processed and stored in the database!")

        else:
            st.warning("Please upload a GPX file and submit the form to process and store the track data.")
with tab_tour:

    with st.expander("Tour erstellen", expanded=True):
        with st.form("tour_form", clear_on_submit=True):
            tour_title = st.text_input("Tour Titel")
            tour_submitted = st.form_submit_button("Speichern")

        if tour_submitted and tour_title:
            tour_id = str(uuid.uuid4())
            con.execute("INSERT INTO tours VALUES (?, ?)", [tour_id, tour_title])
            st.success(f"Erstellt: {tour_title}")
            st.rerun()

    with st.expander("Touren bearbeiten", expanded=True):

        tours_df = con.sql("SELECT tours.tour_id, tours.tour_title, gpx.track_title , gpx.time_start , gpx.time_end , gpx.track_distance_m  FROM tours left join gpx ON tours.tour_id = gpx.tour_id").fetchdf()

        st.data_editor(tours_df,
            column_config={
            "tour_id": None,
            "tour_title": st.column_config.TextColumn("Tour Titel"),
            "track_title": st.column_config.TextColumn("Track Titel", disabled=True),
            "time_start": st.column_config.DatetimeColumn("Startdatum", format="YYYY-MM-DD", disabled=True),
            "time_end": st.column_config.DatetimeColumn("Enddatum", format="YYYY-MM-DD", disabled=True),
            "track_distance_m": st.column_config.NumberColumn("Distanz (m)", disabled=True)
            },
            hide_index=True
        )

with tab_sport:
    with st.expander("Sport erstellen", expanded=True):
        with st.form("sport_form", clear_on_submit=True):
            sport_title = st.text_input("Sport Titel")
            sport_submitted = st.form_submit_button("Speichern")

        if sport_submitted and sport_title:
            sport_id = str(uuid.uuid4())
            con.execute("INSERT INTO sport VALUES (?, ?)", [sport_id, sport_title])
            st.success(f"Erstellt: {sport_title}")
            st.rerun()

    with st.expander("Sports bearbeiten", expanded=True):

        sports_df = con.sql("SELECT tours.tour_id, tours.tour_title, gpx.track_title , sport.sport_title, gpx.time_start , gpx.time_end , gpx.track_distance_m  FROM tours left join gpx ON tours.tour_id = gpx.tour_id left join sport ON gpx.sport_id = sport.sport_id").fetchdf()
    
        st.data_editor(sports_df,
            column_config={
            "tour_id": None,
            "tour_title": st.column_config.TextColumn("Tour Titel"),
            "track_title": st.column_config.TextColumn("Track Titel", disabled=True),
            "time_start": st.column_config.DatetimeColumn("Startdatum", format="YYYY-MM-DD", disabled=True),
            "time_end": st.column_config.DatetimeColumn("Enddatum", format="YYYY-MM-DD", disabled=True),
            "track_distance_m": st.column_config.NumberColumn("Distanz (m)", disabled=True)
            },
            hide_index=True
        )
        

db_results = con.sql("SELECT track_id, track_title, time_start, time_end, track_distance_m FROM gpx").fetchdf()     
st.data_editor(db_results)
con.close()