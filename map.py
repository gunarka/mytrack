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
    color_options = {"ele":"Höhe","km_per_h":"Geschwindigkeit", "slope":"Gefälle", "none":"Nichts" }
    st.selectbox("Wähle eine Spalte zum Plotten", options=list(color_options.keys()), key="plot_column", format_func=lambda x: color_options[x])
    df = con.sql("SELECT * FROM tours left join gpx ON tours.tour_id = gpx.tour_id left join sport ON gpx.sport_id = sport.sport_id ORDER BY gpx.time_start ASC ").fetchdf()
    tour_dict  = df.set_index("tour_id")["tour_title"].to_dict()
    st.pills(label="Tour", options=tour_dict, selection_mode="multi", key="tour_select", format_func=lambda x: tour_dict[x])
    track_dict  = df.set_index("track_id")["track_title"].to_dict()
    st.pills(label="Track", options=track_dict, selection_mode="multi", key="track_select", format_func=lambda x: track_dict[x])
    #st.write(st.session_state.track_select)
    selected_tracks = st.session_state.track_select
    df = df[df["track_id"].isin(selected_tracks)]


map_bounds = [[df["location_lat_min"].min(), df["location_lon_min"].min()],[df["location_lat_max"].max() , [df["location_lon_max"].max()]]]

range_speed = [df["speed_min"].min(), df["speed_max"].max()]
range_elevation =[df["elevation_min"].min(), df["elevation_max"].max()]
range_slope =[df["slope_min"].min(), df["slope_max"].max()]
range_none = [1,1]

if st.session_state.plot_column == "km_per_h":
    range_att =  range_speed
elif st.session_state.plot_column == "ele":
    range_att = range_elevation
elif st.session_state.plot_column == "slope":
    range_att =  range_slope
else:
    range_att = range_none

m = folium.Map()
m.fit_bounds(map_bounds)
folium.TileLayer(tiles="https://tile.opentopomap.org/{z}/{x}/{y}.png",attr=('Map data: &copy; OpenStreetMap contributors, SRTM | ''Map style: &copy; <a href="https://opentopomap.org">OpenTopoMap</a> ''(<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC-BY-SA</a>)'),name="OpenTopoMap",max_zoom=17).add_to(m)
track_col = cm.LinearColormap( colors=['#0000FF', '#007FFF', '#00FFFF', '#7FFF00', '#FFFF00', '#FF7F00', '#FF0000'], vmin=range_att[0], vmax=range_att[1], caption="")


fig = go.Figure()

distance = 0



#st.dataframe(df.head())
for i in range(0,len(df)):

    gpx_file = df["file_data"].iloc[i]

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
    
    
    #track_col = linear.plot_colorspace.scale(min(track_att), max(track_att))
    
    folium.CircleMarker([gdf['lat'].iloc[0], gdf['lon'].iloc[0]], tooltip="Start", fill=True, fill_color="green", radius=10, fill_opacity=0.8,stroke=True, color="white",opacity=0.8).add_to(m)
    folium.CircleMarker([gdf['lat'].iloc[-1], gdf['lon'].iloc[-1]], tooltip="Ende", fill=True, fill_color="red", radius=10, fill_opacity=0.8,stroke=True, color="white",opacity=0.8).add_to(m)
    
    if st.session_state.plot_column != "none":
        track_att = gdf[st.session_state.plot_column].values.tolist()
    else:
        track_att = np.repeat([1],len(track_loc))
    folium.ColorLine(
    positions=track_loc,
    colors=track_att,
    colormap=track_col,
    weight=5
    ).add_to(m)
    
    # --- Render in Streamlit --------------------------------------------




    fig.add_trace(go.Scatter(x=gdf["distance"], y=gdf["ele"],
        fill='tozeroy',
        mode='markers',
        #line_color="white",
        marker = dict(color =track_att,colorscale="jet",cmin=range_att[0],cmax=range_att[1], size=3),
        fillgradient=dict(type='vertical', colorscale=[(0.0, 'rgba(120, 190, 170, 0.0)'),(1.0, 'rgba(120, 190, 170, 0.8)')],start =  range_elevation[0]*0.9,stop =  range_elevation[1]*1.1),showlegend=False))

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
m.add_child(track_col)
st_folium(m, width="strech", height=800)
fig.data[0].on_click(st.write("Hello"))
st.plotly_chart(fig)
con.close()