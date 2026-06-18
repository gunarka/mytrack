
import streamlit as st                  # frontend framework
import duckdb as duckdb                 # db storage 

if st.button("Initialize Database"):
    DB_PATH      = ".data/tracks.duckdb"
    con = duckdb.connect(database=str(DB_PATH))

    con.sql("DROP TABLE IF EXISTS gpx")
    st.success("Table gpx dropped successfully!")
    con.sql("DROP TABLE IF EXISTS tours")
    st.success("Table tours dropped successfully!")
    con.sql("DROP TABLE IF EXISTS sport")
    st.success("Table sport dropped successfully!")

    con.sql("""
    CREATE TABLE IF NOT EXISTS gpx (
        track_id              UUID        NOT NULL,
        track_title           VARCHAR,
        sport_id              UUID,
        tour_id               UUID,
            
        location_start_country  VARCHAR,
        location_start_state    VARCHAR,
        location_start_county   VARCHAR,
        location_start_town     VARCHAR,
        location_start_suburb   VARCHAR,
        location_start_road     VARCHAR,
            
        location_end_country    VARCHAR,
        location_end_state      VARCHAR,
        location_end_county     VARCHAR,
        location_end_town       VARCHAR,
        location_end_suburb     VARCHAR,
        location_end_road       VARCHAR,
            
        location_start_lat_lon  STRUCT(lat DOUBLE, lon DOUBLE),
        location_end_lat_lon    STRUCT(lat DOUBLE, lon DOUBLE),
        location_start_address  JSON,
        location_end_address    JSON,
        
        location_lat_min        DOUBLE,
        location_lat_max        DOUBLE,
        location_lon_min        DOUBLE,
        location_lon_max        DOUBLE,
        
        time_zone             VARCHAR,
        time_start            TIMESTAMP,
        time_end              TIMESTAMP,
        track_time_s          DOUBLE,
        track_distance_m      DOUBLE,
        track_ascent_m        DOUBLE,
        track_descent_m       DOUBLE,
            
        elevation_min DOUBLE,
        elevation_max DOUBLE,
        speed_min DOUBLE,
        speed_max DOUBLE,
        slope_min DOUBLE,
        slope_max DOUBLE,
            
        file_name             VARCHAR,
        file_data             BLOB,
        time_stamp            TIMESTAMP
    )
    """)
    st.success("Table gpx initialized successfully!")
    
    con.sql("""
        CREATE TABLE IF NOT EXISTS tours (
            tour_id               UUID        NOT NULL,
            tour_title            VARCHAR
        )
    """)
    st.success("Table tours initialized successfully!")
    con.sql("""
    CREATE TABLE IF NOT EXISTS sport (
        sport_id         UUID        NOT NULL,
        sport_title      VARCHAR
    )
    """)
    st.success("Table sport initialized successfully!")
    st.success("Done!")
    con.close()