import streamlit as st
import pandas as pd
from datetime import datetime
import json
import os
import base64
from io import BytesIO
from PIL import Image

# PDF → Image
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError
import fitz  # PyMuPDF

# OneDrive / MS Graph
import msal
import requests

# ── CONFIGURATION -------------------------------------------------------------------
CLIENT_ID     = "votre_client_id"
TENANT_ID     = "votre_tenant_id"
CLIENT_SECRET = "votre_client_secret"
AUTHORITY     = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES        = ["https://graph.microsoft.com/.default"]

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
    token   = get_onedrive_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp    = requests.get("https://graph.microsoft.com/v1.0/me/drive/root/children", headers=headers)
    return resp.json().get("value", [])

def resize_image(image, max_width=800):
    """Redimensionne l'image si sa largeur dépasse max_width, tout en conservant le ratio d'aspect."""
    if image.width > max_width:
        ratio = max_width / image.width
        new_height = int(image.height * ratio)
        image = image.resize((max_width, new_height), Image.LANCZOS)
    return image

def image_to_base64(image):
    """Convertit une image PIL en format base64 pour l’intégrer dans HTML."""
    buffered = BytesIO()
    image.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()

# ── CHOIX ET CHARGEMENT DU PLAN ------------------------------------------------------
st.subheader("Choisir un plan")
source         = st.radio("Source du plan", ["Local", "OneDrive"])
uploaded_bytes = None
selected_name  = None

if source == "Local":
    up = st.file_uploader("Uploadez votre plan (PNG, JPG, PDF)", type=["png","jpg","jpeg","pdf"])
    if up:
        uploaded_bytes = up.read()
        selected_name  = up.name
else:
    files = list_onedrive_files()
    names = [f["name"] for f in files if f["name"].lower().endswith((".png",".jpg",".jpeg",".pdf"))]
    selected_name = st.selectbox("Sélectionnez un fichier", names)
    if selected_name:
        token   = get_onedrive_token()
        file_id = next(f["id"] for f in files if f["name"] == selected_name)
        headers = {"Authorization": f"Bearer {token}"}
        resp    = requests.get(f"https://graph.microsoft.com/v1.0/me/drive/items/{file_id}/content", headers=headers)
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
                doc  = fitz.open(stream=uploaded_bytes, filetype="pdf")
                pix  = doc.load_page(0).get_pixmap()
                image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        else:
            image = Image.open(BytesIO(uploaded_bytes))
    except Exception as e:
        st.error(f"Erreur lors du chargement du fichier : {e}")
        st.stop()

    if image:
        # Redimensionner l'image pour l'adapter à l'affichage initial
        image = resize_image(image, max_width=800)
        original_w, original_h = image.size

        # Convertir l'image en base64 pour l'intégrer dans HTML
        image_base64 = image_to_base64(image)

        # Définir une taille fixe pour la zone visible (conteneur)
        container_width = 800
        container_height = 600

        # ── COMPOSANT HTML AVEC PANZOOM ET FABRIC.JS ─────────────────────────────────
        st.subheader("Annotation du plan")
        html_code = f"""
        <div id="panzoom-container" style="width: {container_width}px; height: {container_height}px; overflow: hidden;">
            <canvas id="canvas" style="border: 1px solid #ccc;"></canvas>
        </div>
        <input type="hidden" id="annotation-data" value="">
        <script src="https://unpkg.com/@panzoom/panzoom@4.5.1/dist/panzoom.min.js"></script>
        <script src="https://unpkg.com/fabric@5.3.0/dist/fabric.min.js"></script>
        <script>
            document.addEventListener('DOMContentLoaded', function() {{
                // Initialiser le canvas Fabric.js
                const canvas = new fabric.Canvas('canvas', {{
                    width: {container_width},
                    height: {container_height},
                    selection: false // Désactiver la sélection par défaut
                }});

                // Charger l'image de fond
                fabric.Image.fromURL('data:image/png;base64,{image_base64}', function(img) {{
                    img.scaleToWidth({container_width});
                    img.scaleToHeight({container_height});
                    canvas.setBackgroundImage(img, canvas.renderAll.bind(canvas));
                }});

                // Initialiser Panzoom sur le conteneur
                const panzoomContainer = document.getElementById('panzoom-container');
                const panzoom = Panzoom(panzoomContainer, {{
                    maxScale: 5,
                    minScale: 0.5,
                    step: 0.1,
                    contain: 'outside',
                    canvas: true
                }});
                panzoomContainer.addEventListener('wheel', panzoom.zoomWithWheel);

                // Gestion des clics pour ajouter des annotations
                canvas.on('mouse:down', function(o) {{
                    const pointer = canvas.getPointer(o.e);
                    const shape = new fabric.Circle({{
                        left: pointer.x,
                        top: pointer.y,
                        radius: 5,
                        fill: 'rgba(255,0,0,0.3)',
                        stroke: '#FF0000',
                        strokeWidth: 2,
                        selectable: false
                    }});
                    canvas.add(shape);
                    const zoom = panzoom.getScale();
                    const pan = panzoom.getPan();
                    const x = (shape.left + pan.x) / zoom / {original_w};
                    const y = (shape.top + pan.y) / zoom / {original_h};
                    const annotation = {{
                        type: 'point',
                        x: x,
                        y: y,
                        width: 5 / {original_w},
                        height: 5 / {original_h}
                    }};
                    document.getElementById('annotation-data').value = JSON.stringify(annotation);
                    canvas.remove(shape); // Retirer le point après l'enregistrement
                    canvas.renderAll();
                }});

                // Gestion des événements tactiles pour mobile
                canvas.on('touch:start', function(o) {{
                    const pointer = canvas.getPointer(o.e);
                    const shape = new fabric.Circle({{
                        left: pointer.x,
                        top: pointer.y,
                        radius: 5,
                        fill: 'rgba(255,0,0,0.3)',
                        stroke: '#FF0000',
                        strokeWidth: 2,
                        selectable: false
                    }});
                    canvas.add(shape);
                    const zoom = panzoom.getScale();
                    const pan = panzoom.getPan();
                    const x = (shape.left + pan.x) / zoom / {original_w};
                    const y = (shape.top + pan.y) / zoom / {original_h};
                    const annotation = {{
                        type: 'point',
                        x: x,
                        y: y,
                        width: 5 / {original_w},
                        height: 5 / {original_h}
                    }};
                    document.getElementById('annotation-data').value = JSON.stringify(annotation);
                    canvas.remove(shape); // Retirer le point après l'enregistrement
                    canvas.renderAll();
                }});
            }});
        </script>
        """

        # Affiche le composant dans Streamlit
        st.components.v1.html(html_code, height=container_height, width=container_width)

        st.write("Utilisez la molette ou le pincement pour zoomer, glissez pour déplacer l’image, et cliquez (ou tap sur mobile) pour ajouter un point.")

        # Récupérer les données d’annotation depuis JavaScript
        annotation_data = st.components.v1.html(
            """<span id="annotation-data-receiver"></span>""",
            height=0,
            width=0
        )

        # Vérifier si une annotation a été dessinée
        if annotation_data and "value" in dir(annotation_data):
            try:
                ann_data = json.loads(annotation_data.value)
                if ann_data:
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
                            "type": ann_data["type"],
                            "x": round(ann_data["x"], 4),
                            "y": round(ann_data["y"], 4),
                            "width": round(ann_data["width"], 4),
                            "height": round(ann_data["height"], 4),
                        }
                        st.session_state["annotations"].append(ann)
                        with open(ANNOTATIONS_FILE, "w") as f:
                            json.dump(st.session_state["annotations"], f, indent=2)
                        st.rerun()
            except json.JSONDecodeError:
                pass

# ── AFFICHAGE, FILTRAGE & EXPORT ────────────────────────────────────────────────────
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
        format_func=lambda i: f"{filtered.loc[i,'timestamp']} – {filtered.loc[i,'comment'][:30]}"
    )
    new_stt = st.selectbox("Nouveau statut", ["À faire", "En cours", "Résolu"], key="new_status")
    if st.button("Mettre à jour"):
        st.session_state["annotations"][idx]["status"] = new_stt
        with open(ANNOTATIONS_FILE, "w") as f:
            json.dump(st.session_state["annotations"], f, indent=2)
        st.rerun()

    if st.button("Exporter en CSV"):
        filtered.to_csv("annotations_export.csv", index=False)
        st.success("Exporté sous 'annotations_export.csv'")