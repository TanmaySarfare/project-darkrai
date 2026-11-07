"""Project Darkrai on Streamlit Community Cloud.

Streamlit runs Python, and Project Darkrai is a ready-made web page, so this file packs the page,
its engine, the latest data.json and your images into one piece and shows it full-window.
Each nightly data update pushed to GitHub reaches the app automatically.
"""
from pathlib import Path
import base64

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parents[1]   # Community Cloud runs apps from the repository root
SITE = ROOT / "site"
IMAGES = ("logo.png", "asterisk.png", "bullets.png")

st.set_page_config(
    page_title="Project Darkrai",
    page_icon=str(SITE / "logo.png") if (SITE / "logo.png").exists() else "🌑",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Let the page fill the window: no Streamlit padding, a matching background, and one scroll bar.
st.markdown(
    """
    <style>
      header[data-testid="stHeader"] { display: none; }
      .block-container, [data-testid="stMainBlockContainer"] { padding: 0 !important; max-width: 100% !important; }
      .stApp, [data-testid="stAppViewContainer"] { background: #100D19; }
      [data-testid="stAppViewContainer"] iframe { display: block; width: 100%; height: 100vh !important; border: 0; }
    </style>
    """,
    unsafe_allow_html=True,
)


def data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def file_stamp() -> tuple:
    """Changes whenever a site file changes, so the cached page is rebuilt after a data update."""
    names = ("index.html", "engine.js", "data.json") + IMAGES
    return tuple((n, (SITE / n).stat().st_mtime) for n in names if (SITE / n).exists())


@st.cache_data(show_spinner=False)
def build_page(stamp: tuple) -> str:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    engine = (SITE / "engine.js").read_text(encoding="utf-8")
    html = html.replace('<script src="engine.js"></script>', "<script>" + engine + "</script>")
    data_file = SITE / "data.json"
    if data_file.exists():
        data = data_file.read_text(encoding="utf-8").replace("</", "<\\/")
        html = html.replace('<div id="root"></div>', '<div id="root"></div>\n<script>window.__DARKRAI_DATA__ = ' + data + ";</script>", 1)
    for name in IMAGES:
        path = SITE / name
        if path.exists():
            html = html.replace(f'"{name}"', f'"{data_uri(path)}"')
    return html


components.html(build_page(file_stamp()), height=900, scrolling=True)
