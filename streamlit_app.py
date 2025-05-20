# streamlit_app.py

# ── Patch pour rétablir image_to_url quel que soit le nombre d'arguments -------------
import base64
import io
import numpy as np
from PIL import Image
import streamlit.elements.image as _st_image


def _custom_image_to_url(*args, **kwargs):
    """
    Ce patch intercepte tous les appels à image_to_url(...)
    et accepte n'importe quel nombre d'arguments.
    On prend le premier positional argument comme image.
    """
    img = args[0]
    if isinstance(img, np.ndarray):
        img = Image.fromarray(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{data}"


# Injection du patch
_st_image.image_to_url = _custom_image_to_url

# ── Imports standards ---------------------------------------------------------------
import streamlit as st
from streamlit_drawable_canvas import st_canvas
import pandas as pd
from datetime import datetime
import json
import os

# PDF → Image
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError
import fitz  # PyMuPDF

# OneDrive / MS Graph
import msal
import requests

# ── CONFIGURATION -------------------------------------------------------------------
CLIENT_ID = "votre_client_id"
TENANT_ID = "votre_tenant_id"
CLIENT_SECRET = "votre_client_secret"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]

ANNOTATIONS_FILE = "annotations.json"
if not os.path.exists(ANNOTATIONS_FILE):
    with open(ANNOTATIONS_FILE, "w") as f:
        json.dump([], f)

if "annotations" not in st.session_state:
    with open(ANNOTATIONS_FILE, "r") as f:
        st.session_state["annotations"] = json.load(f)

st.title("BuildozAir Simplifié – Annotation de Plans")


# ── FONCTIONS -----------------------------------------------------------------------
def get_onedrive_token():
    app = msal.ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=SCOPES)
    return result.get("access_token")


def list_onedrive_files():
    token = get_onedrive_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get("https://graph.microsoft.com/v1.0/me/drive/root/children", headers=headers)
    return resp.json().get("value", [])


def resize_image(image, max_width=800):
    """Redimensionne l'image si sa largeur dépasse max_width, tout en conservant le ratio d'aspect."""
    if image.width > max_width:
        ratio = max_width / image.width
        new_height = int(image.height * ratio)
        image = image.resize((max_width, new_height), Image.LANCZOS)
    return image


# ── CHOIX ET CHARGEMENT DU PLAN ------------------------------------------------------
st.subheader("Choisir un plan")
source = st.radio("Source du plan", ["Local", "OneDrive"])
uploaded_bytes = None
selected_name = None

if source == "Local":
    up = st.file_uploader("Uploadez votre plan (PNG, JPG, PDF)", type=["png", "jpg", "jpeg", "pdf"])
    if up:
        uploaded_bytes = up.read()
        selected_name = up.name
else:
    files = list_onedrive_files()
    names = [f["name"] for f in files if f["name"].lower().endswith((".png", ".jpg", ".jpeg", ".pdf"))]
    selected_name = st.selectbox("Sélectionnez un fichier", names)
    if selected_name:
        token = get_onedrive_token()
        file_id = next(f["id"] for f in files if f["name"] == selected_name)
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(f"https://graph.microsoft.com/v1.0/me/drive/items/{file_id}/content", headers=headers)
        uploaded_bytes = resp.content

# ── CONVERSION EN IMAGE ------------------------------------------------------------
image = None
if uploaded_bytes:
    try:
        if selected_name.lower().endswith(".pdf"):
            try:
                pages = convert_from_bytes(uploaded_bytes, dpi=150)
                image = pages[0]
            except PDFInfoNotInstalledError:
                doc = fitz.open(stream=uploaded_bytes, filetype="pdf")
                pix = doc.load_page(0).get_pixmap()
                image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        else:
            image = Image.open(io.BytesIO(uploaded_bytes))
    except Exception as e:
        st.error(f"Erreur lors du chargement du fichier : {e}")
        st.stop()

    if image:
        # Redimensionner l'image pour l'adapter à l'affichage initial
        image = resize_image(image, max_width=800)
        original_w, original_h = image.size

        # Ajouter un curseur pour le zoom
        if "zoom_level" not in st.session_state:
            st.session_state.zoom_level = 1.0  # Zoom par défaut (1x)

        zoom_level = st.slider("Niveau de zoom", 0.5, 3.0, st.session_state.zoom_level, 0.1, key="zoom_slider")
        st.session_state.zoom_level = zoom_level

        # Redimensionner l'image selon le niveau de zoom
        zoomed_w = int(original_w * zoom_level)
        zoomed_h = int(original_h * zoom_level)
        zoomed_image = image.resize((zoomed_w, zoomed_h), Image.LANCZOS)

        # Mettre à jour les dimensions pour le canevas
        img_w, img_h = zoomed_image.size

        st.subheader("Annotation du plan")
        canvas_result = st_canvas(
            fill_color="rgba(255,0,0,0.3)",
            stroke_width=2,
            stroke_color="#FF0000",
            background_image=zoomed_image,
            update_streamlit=True,
            height=img_h,
            width=img_w,
            drawing_mode="rect",
            key="canvas",
        )

        if canvas_result.json_data and canvas_result.json_data.get("objects"):
            obj = canvas_result.json_data["objects"][-1]
            # Ajuster les coordonnées en fonction du zoom
            x, y = obj["left"] / zoom_level / original_w, obj["top"] / zoom_level / original_h
            w, h = obj["width"] / zoom_level / original_w, obj["height"] / zoom_level / original_h

            st.sidebar.subheader("Détails de l'annotation")
            cat = st.sidebar.selectbox("Catégorie", ["QHSE", "Qualité", "Planning", "Autre"])
            ivt = st.sidebar.selectbox("Intervenant", ["Architecte", "Électricien", "Client", "Assistante"])
            cmt = st.sidebar.text_area("Commentaire")
            pf = st.sidebar.file_uploader("Ajouter une photo", type=["png", "jpg", "jpeg"])
            stt = st.sidebar.selectbox("Statut", ["À faire", "En cours", "Résolu"])

            photo_path = None
            if pf:
                os.makedirs("photos", exist_ok=True)
                photo_path = os.path.join("photos", pf.name)
                with open(photo_path, "wb") as f:
                    f.write(pf.read())

            if st.sidebar.button("Ajouter annotation"):
                ann = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "category": cat,
                    "intervenant": ivt,
                    "comment": cmt,
                    "photo": photo_path,
                    "status": stt,
                    "x": round(x, 4),
                    "y": round(y, 4),
                    "width": round(w, 4),
                    "height": round(h, 4),
                }
                st.session_state["annotations"].append(ann)
                with open(ANNOTATIONS_FILE, "w") as f:
                    json.dump(st.session_state["annotations"], f, indent=2)
                st.experimental_rerun()

# ── AFFICHAGE, FILTRAGE & EXPORT ----------------------------------------------------
if st.session_state["annotations"]:
    st.subheader("Annotations enregistrées")
    df = pd.DataFrame(st.session_state["annotations"])
    st.dataframe(df)

    st.sidebar.subheader("Filtrer et traiter")
    cats = df["category"].unique().tolist()
    ivts = df["intervenant"].unique().tolist()
    stats = df["status"].unique().tolist()
    f_cats = st.sidebar.multiselect("Par catégorie", options=cats, default=cats)
    f_ivts = st.sidebar.multiselect("Par intervenant", options=ivts, default=ivts)
    f_stats = st.sidebar.multiselect("Par statut", options=stats, default=stats)

    filtered = df[
        df["category"].isin(f_cats) &
        df["intervenant"].isin(f_ivts) &
        df["status"].isin(f_stats)
        ]

    st.subheader("Annotations filtrées")
    st.dataframe(filtered)

    st.subheader("Modifier le statut")
    idx = st.selectbox(
        "Sélectionner une annotation",
        filtered.index,
        format_func=lambda i: f"{filtered.loc[i, 'timestamp']} – {filtered.loc[i, 'comment'][:30]}"
    )
    new_stt = st.selectbox("Nouveau statut", ["À faire", "En cours", "Résolu"], key="new_status")
    if st.button("Mettre à jour"):
        st.session_state["annotations"][idx]["status"] = new_stt
        with open(ANNOTATIONS_FILE, "w") as f:
            json.dump(st.session_state["annotations"], f, indent=2)
        st.experimental_rerun()

    if st.button("Exporter en CSV"):
        filtered.to_csv("annotations_export.csv", index=False)
        st.success("Exporté sous 'annotations_export.csv'")