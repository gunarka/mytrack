import streamlit as st

# Beispiel-Datenstruktur, wie sie aus deiner DuckDB kommen könnte
# (Gruppiert nach Jahr -> Monat -> Tour)
hierarchie_daten = {
    "2026": {
        "Juni": {
            "Alpen-Urlaub": [
                {"id": 101, "name": "Etappe 1: Aufstieg"},
                {"id": 102, "name": "Etappe 2: Gratwanderung"}
            ],
            "Feierabendrunden": [
                {"id": 103, "name": "Standard-Runde"}
            ]
        },
        "Mai": {
            "Sonntags-Touren": [
                {"id": 104, "name": "Rund um den See"}
            ]
        }
    },
    "2025": {
        "August": {
            "Pyrenäen": [
                {"id": 99, "name": "Königsetappe"}
            ]
        }
    }
}

st.title("🗂️ Hierarchisches Track-Verzeichnis")

# Variable im Session State, um den ausgewählten Track zu speichern
if "selected_track_id" not in st.session_state:
    st.session_state.selected_track_id = None

# Dynamischer Aufbau der Hierarchie mit Expandern
for jahr, monate in hierarchie_daten.items():
    with st.expander(f"📅 **{jahr}**", expanded=False):
        
        for monat, touren in monate.items():
            st.markdown(f"🔹 **{monat}**")
            
            # Ein bisschen Einrückung für die Optik
            indent = st.columns([0.05, 0.95])
            with indent[1]:
                for tour, tracks in touren.items():
                    st.caption(f"⛰️ Tour: {tour}")
                    
                    # Die eigentlichen Tracks als klickbare Buttons
                    for track in tracks:
                        if st.button(track["name"], key=f"btn_{track['id']}", use_container_width=True):
                            st.session_state.selected_track_id = track["id"]

# --- Details anzeigen ---
if st.session_state.selected_track_id:
    st.markdown("---")
    st.success(f"Geladener Track-ID: {st.session_state.selected_track_id}")
    # Hier folgt dein Code für Karte & Statistiken