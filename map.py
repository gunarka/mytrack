import streamlit as st                  # frontend framework
import duckdb as duckdb                 # db storage 
import io                  # for handling file data 
from io import BytesIO         # for handling file data
import geopandas as gpd                 # geospatial data manipulation
from geopy.geocoders import Nominatim   # geocoding library for reverse geocoding
import numpy as np                     # for numerical operations
from timezonefinder import TimezoneFinder # timezone lookup based on lat/lon
import datetime                   # for handling timestamps
import folium                 # for interactive maps
from streamlit_folium import st_folium          # for displaying folium maps in Streamlit
import branca.colormap as cm               
import plotly.graph_objects as go
import pandas as pd

st.set_page_config(page_title="Interactive Map", layout="wide")

DB_PATH      = ".data/tracks.duckdb"
con = duckdb.connect(database=str(DB_PATH))

with st.sidebar:
    color_options = ["km_per_h", "slope", "ele" ]
    st.selectbox("Wähle eine Spalte zum Plotten", options=color_options, key="plot_column")
    df = con.sql("SELECT tours.tour_id, tours.tour_title, gpx.track_id, gpx.track_title , sport.sport_id, sport.sport_title, gpx.time_start , gpx.time_end , gpx.track_distance_m  FROM tours left join gpx ON tours.tour_id = gpx.tour_id left join sport ON gpx.sport_id = sport.sport_id").fetchdf()

m = folium.Map(
#location=[52, 12],
zoom_start=11,
tiles=None,  # we'll add the OpenTopoMap tile layer manually
)
folium.TileLayer(
tiles="https://tile.opentopomap.org/{z}/{x}/{y}.png",
attr=(
'Map data: &copy; OpenStreetMap contributors, SRTM | '
'Map style: &copy; <a href="https://opentopomap.org">OpenTopoMap</a> '
'(<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC-BY-SA</a>)'
),
name="OpenTopoMap",
max_zoom=17,  # OpenTopoMap tiles top out around z17
).add_to(m)

fig = go.Figure()

distance = 0


db_results = con.sql("SELECT * FROM gpx left join tours on gpx.tour_id = tours.tour_id").fetchdf()
st.dataframe(db_results.head())
for i in range(0,len(db_results)):

    gpx_file = db_results["file_data"][i]

    gdf = gpd.read_file(io.BytesIO(gpx_file), layer="track_points")
    gdf.crs = "EPSG:4326"
    gdf['lon'] = gdf.geometry.x
    gdf['lat'] = gdf.geometry.y








    gdf.crs = "EPSG:4326"
    gdf = gdf.to_crs(gdf.estimate_utm_crs())
    shifted_gdf = gdf.shift(1)
    gdf['time_delta'] = gdf['time'] - shifted_gdf['time']  
    gdf['dist_delta'] = gdf.distance(shifted_gdf)
    gdf.at[0,"dist_delta"]=0
    gdf.at[0,"time_delta"] = pd.to_timedelta(0)

    # speed in various formats
    gdf['m_per_s'] = gdf['dist_delta'] / gdf.time_delta.dt.seconds
    gdf.at[0,"m_per_s"]=0
    gdf['km_per_h'] = gdf['m_per_s'] * 3.6
    gdf['min_per_km'] = 60 / (gdf['km_per_h'])

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
    gdf['ele_normalized'] = gdf['ele'] - gdf.iloc[0]['ele']
    # slope and min_per_km can be infinite if 0 km/h
    # Replace inf with nan for better plotting
    gdf.replace(np.inf, np.nan, inplace=True)
    gdf.replace(-np.inf, np.nan, inplace=True)
    gdf['distance'] = gdf['dist_delta'].cumsum()+distance
    distance=gdf['distance'].max()

    #gdf.at[0,'distance'] = 0

    # --- Build the map ------------------------------------------------------
    
    track_loc = gdf[['lat', 'lon']].values.tolist()
    track_att = gdf[st.session_state.plot_column].values.tolist()
    track_col = cm.LinearColormap(
    colors=['blue', 'green', 'yellow', 'red'],
    vmin=min(track_att),
    vmax=max(track_att),
    caption=st.session_state.plot_column
    )
    #track_col = linear.plot_colorspace.scale(min(track_att), max(track_att))
    
    folium.Marker([gdf['lat'].iloc[0], gdf['lon'].iloc[0]], tooltip="Start", icon=folium.Icon(color="green"),).add_to(m)
    folium.Marker([gdf['lat'].iloc[-1], gdf['lon'].iloc[-1]], tooltip="Ende", icon=folium.Icon(color="red"),).add_to(m)
    folium.ColorLine(
    positions=track_loc,
    colors=track_att,
    colormap=track_col,
    weight=5
    ).add_to(m)
    #m.add_child(track_col)
    # --- Render in Streamlit --------------------------------------------




    fig.add_trace(go.Scatter(x=gdf["distance"], y=gdf["ele"],
        fill='tozeroy',
        mode='lines',
        line_color='white',
        fillgradient=dict(
            type='vertical', # Or "horizontal"
            colorscale=[
                (0.0, 'rgba(120, 190, 170, 0.0)'),  # Transparent at the bottom
                (1.0, 'rgba(120, 190, 170, 0.8)')   # 80% opaque at the top
            ],
            start =  gdf["ele"].min()*0.9,
            stop =  gdf["ele"].max()*1.1
        ),
        showlegend=False
    ))

    fig.add_trace(
        go.Scatter(
            mode='markers',
            x=[gdf.at[0,"distance"], gdf.iloc[-1]["distance"]],
            y=[gdf.at[0,"ele"],gdf.iloc[-1]["ele"]],
            marker=dict(
                color=["green", "red"],
                size=15,
                line=dict(
                    color='white',
                    width=2
                )
            ),
            showlegend=False
        )
    )
    fig.update_yaxes(range=[gdf["ele"].min()*0.9, gdf["ele"].max()*1.1]) 

folium.LayerControl().add_to(m)
st_folium(m, width="strech", height=800)

st.plotly_chart(fig)
con.close()