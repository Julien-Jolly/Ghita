import streamlit as st
import pandas as pd
import numpy as np
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium
from datetime import datetime
from PIL import Image
import io, os, json

# PDF → Image
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError
import fitz  # PyMuPDF fallback

# Amazon S3
import boto3

# OneDrive / Microsoft Graph (conservé pour compatibilité)
import msal, requests

# ── 0) CONFIGURATION GLOBALE & STOCKAGE ────────────────────────────────────────
st.set_page_config(page_title="BuildozAir Simplifié", layout="wide")

ANNOTATIONS_FILE = "annotations.json"
if not os.path.exists(ANNOTATIONS_FILE):
    with open(ANNOTATIONS_FILE, "w") as f:
        json.dump([], f)
if "projects" not in st.session_state:
    with open(ANNOTATIONS_FILE, "r") as f:
        st.session_state["projects"] = json.load(f)

# Configuration AWS S3
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
S3_BUCKET_NAME = "jujul"

try:
    s3_client = boto3.client(
        "s3",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
    s3_client.head_bucket(Bucket=S3_BUCKET_NAME)
except Exception as e:
    st.error(f"Erreur de configuration S3 : {e}. Vérifiez vos credentials et le bucket.")
    st.stop()

def upload_to_s3(file_name, file_content):
    try:
        if not file_content or len(file_content) == 0:
            st.error(f"Contenu vide pour {file_name}.")
            return None
        s3_client.upload_fileobj(
            io.BytesIO(file_content),
            S3_BUCKET_NAME,
            file_name
        )
        return file_name
    except Exception as e:
        st.error(f"Erreur lors du téléversement de {file_name} sur S3 : {e}")
        return None

def download_from_s3(file_key):
    try:
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=file_key)
        return response['Body'].read()
    except Exception as e:
        st.error(f"Erreur lors du téléchargement de {file_key} depuis S3 : {e}")
        return None

# Migration des anciennes données
for project in st.session_state["projects"]:
    for image in project.get("images", []):
        if "image_path" in image and "image_key" not in image:
            image_path = image["image_path"]
            if os.path.exists(image_path):
                try:
                    with open(image_path, "rb") as f:
                        image_content = f.read()
                    if image_content:
                        image_key = upload_to_s3(image["image_name"], image_content)
                        if image_key:
                            image["image_key"] = image_key
                            del image["image_path"]
                        else:
                            st.warning(f"Échec de la migration de l'image {image['image_name']} vers S3.")
                    else:
                        st.warning(f"Contenu vide pour l'image {image['image_name']} au chemin {image_path}.")
                except Exception as e:
                    st.error(f"Erreur lors de la lecture de {image_path} : {e}")
            else:
                st.warning(f"Chemin {image_path} introuvable pour migration.")
with open(ANNOTATIONS_FILE, "w") as f:
    json.dump(st.session_state["projects"], f, indent=2)

if not st.session_state["projects"] or not any("project_name" in proj for proj in st.session_state["projects"]):
    st.session_state["projects"] = [{"project_name": "Projet par défaut", "images": []}]
    with open(ANNOTATIONS_FILE, "w") as f:
        json.dump(st.session_state["projects"], f, indent=2)
if "drawn_feats_count" not in st.session_state:
    st.session_state["drawn_feats_count"] = 0
if "current_annotation" not in st.session_state:
    st.session_state["current_annotation"] = None

# ── 1) NAVIGATION DANS LA SIDEBAR ──────────────────────────────────────────────
st.sidebar.title("Navigation")
page = st.sidebar.radio("Aller à", ["Annoter", "Gérer", "Planning"])

# ── 2) CONFIG ONE DRIVE ──────────────────────────
CLIENT_ID = "votre_client_id"
TENANT_ID = "votre_tenant_id"
CLIENT_SECRET = "votre_client_secret"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]

def get_onedrive_token():
    app = msal.ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET
    )
    res = app.acquire_token_for_client(scopes=SCOPES)
    return res.get("access_token")

def list_onedrive_files():
    token = get_onedrive_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get("https://graph.microsoft.com/v1.0/me/drive/root/children", headers=headers)
    return resp.json().get("value", [])

# ── 3) HELPER : CHARGER IMAGE ─────────────────────────────────
def load_image_from_bytes(uploaded_bytes, name):
    try:
        if name.lower().endswith(".pdf"):
            try:
                pages = convert_from_bytes(uploaded_bytes, dpi=150)
                return pages[0]
            except PDFInfoNotInstalledError:
                doc = fitz.open(stream=uploaded_bytes, filetype="pdf")
                pix = doc.load_page(0).get_pixmap()
                return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        else:
            return Image.open(io.BytesIO(uploaded_bytes))
    except Exception as e:
        st.error(f"Erreur chargement image : {e}")
        return None

# ── 4) PAGE “Annoter” ──────────────────────────────────────────────────────────
if page == "Annoter":
    st.header("Annoter le plan")

    # 4.1) Sélection ou création d’un projet
    project_names = [proj["project_name"] for proj in st.session_state["projects"] if "project_name" in proj]
    project_names.append("Nouveau projet")
    selected_project = st.selectbox("Sélectionnez un projet", project_names)

    # Réinitialiser les variables globales
    uploaded_bytes = None
    name = None
    image_key = None

    if selected_project == "Nouveau projet":
        new_project_name = st.text_input("Nom du nouveau projet")
        if st.button("Créer le projet") and new_project_name:
            st.session_state["projects"].append({
                "project_name": new_project_name,
                "images": []
            })
            with open(ANNOTATIONS_FILE, "w") as f:
                json.dump(st.session_state["projects"], f, indent=2)
            st.rerun()
        else:
            st.write("Veuillez uploader une nouvelle image pour ce projet.")
            source = st.radio("Source du plan", ["Local", "OneDrive"], disabled=not new_project_name)
            if source == "Local" and new_project_name:
                up = st.file_uploader("Uploadez PNG/JPG/PDF", type=["png","jpg","jpeg","pdf"])
                if up:
                    uploaded_bytes = up.read()
                    name = up.name
            elif source == "OneDrive" and new_project_name:
                files = list_onedrive_files()
                names = [f["name"] for f in files if f["name"].lower().endswith((".png",".jpg",".jpeg",".pdf"))]
                name = st.selectbox("Sélectionnez un fichier OneDrive", names)
                if name:
                    token = get_onedrive_token()
                    fid = next(f["id"] for f in files if f["name"] == name)
                    headers = {"Authorization": f"Bearer {token}"}
                    resp = requests.get(
                        f"https://graph.microsoft.com/v1.0/me/drive/items/{fid}/content",
                        headers=headers
                    )
                    uploaded_bytes = resp.content

            if uploaded_bytes and name and new_project_name:
                image_key = upload_to_s3(name, uploaded_bytes)
                if image_key:
                    new_project_idx = next(i for i, proj in enumerate(st.session_state["projects"]) if proj["project_name"] == new_project_name)
                    st.session_state["projects"][new_project_idx]["images"].append({
                        "image_name": name,
                        "image_key": image_key,
                        "annotations": []
                    })
                    with open(ANNOTATIONS_FILE, "w") as f:
                        json.dump(st.session_state["projects"], f, indent=2)
                    st.rerun()

    else:
        # Logique pour un projet existant
        project_idx = next(i for i, proj in enumerate(st.session_state["projects"]) if proj["project_name"] == selected_project)
        project = st.session_state["projects"][project_idx]

        image_names = [img["image_name"] for img in project["images"]]
        image_names.append("Ajouter une nouvelle image")
        selected_image = st.selectbox("Sélectionnez une image", image_names)

        if selected_image == "Ajouter une nouvelle image":
            source = st.radio("Source du plan", ["Local", "OneDrive"])
            if source == "Local":
                up = st.file_uploader("Uploadez PNG/JPG/PDF", type=["png","jpg","jpeg","pdf"])
                if up:
                    uploaded_bytes = up.read()
                    name = up.name
            else:
                files = list_onedrive_files()
                names = [f["name"] for f in files if f["name"].lower().endswith((".png",".jpg",".jpeg",".pdf"))]
                name = st.selectbox("Sélectionnez un fichier OneDrive", names)
                if name:
                    token = get_onedrive_token()
                    fid = next(f["id"] for f in files if f["name"] == name)
                    headers = {"Authorization": f"Bearer {token}"}
                    resp = requests.get(
                        f"https://graph.microsoft.com/v1.0/me/drive/items/{fid}/content",
                        headers=headers
                    )
                    uploaded_bytes = resp.content

            if uploaded_bytes and name:
                image_key = upload_to_s3(name, uploaded_bytes)
                if image_key:
                    image_exists = any(img["image_name"] == name for img in project["images"])
                    if not image_exists:
                        project["images"].append({
                            "image_name": name,
                            "image_key": image_key,
                            "annotations": []
                        })
                        with open(ANNOTATIONS_FILE, "w") as f:
                            json.dump(st.session_state["projects"], f, indent=2)
                        st.rerun()
        else:
            image_idx = next(i for i, proj in enumerate(project["images"]) if proj["image_name"] == selected_image)
            image_data = project["images"][image_idx]
            name = image_data["image_name"]
            if "image_key" in image_data:
                image_key = image_data["image_key"]
            elif "image_path" in image_data and os.path.exists(image_data["image_path"]):
                try:
                    with open(image_data["image_path"], "rb") as f:
                        image_content = f.read()
                    if image_content:
                        image_key = upload_to_s3(name, image_content)
                        if image_key:
                            image_data["image_key"] = image_key
                            del image_data["image_path"]
                            with open(ANNOTATIONS_FILE, "w") as f:
                                json.dump(st.session_state["projects"], f, indent=2)
                        else:
                            st.warning(f"Échec de la migration de {name} vers S3.")
                    else:
                        st.warning(f"Contenu vide pour {name} au chemin {image_data['image_path']}.")
                except Exception as e:
                    st.error(f"Erreur lors de la lecture de {image_data['image_path']} : {e}")
            else:
                st.error("Image non disponible : ni image_key ni image_path valide.")
                image_key = None
            uploaded_bytes = download_from_s3(image_key) if image_key else None

    if uploaded_bytes and name and selected_project != "Nouveau projet":
        image = load_image_from_bytes(uploaded_bytes, name)
        if image:
            arr = np.array(image)
            h, w = arr.shape[:2]
            m = folium.Map(location=[h/2, w/2], zoom_start=0, crs="Simple", min_zoom=-1, max_zoom=4, width="100%", height=600)
            folium.raster_layers.ImageOverlay(image=arr, bounds=[[0,0],[h,w]], interactive=True, cross_origin=False, opacity=1).add_to(m)
            image_idx = next(i for i, img in enumerate(project["images"]) if img["image_name"] == name)
            annotations = project["images"][image_idx]["annotations"]
            for ann in annotations:
                x = ann["x"] * w
                y = ann["y"] * h
                if ann["type"] == "point":
                    folium.Marker(location=[y, x], popup=f"Point: {ann['comment']}", icon=folium.Icon(color="red", icon="circle")).add_to(m)
                elif ann["type"] == "rectangle":
                    width = ann["width"] * w
                    height = ann["height"] * h
                    bounds = [[y, x], [y + height, x + width]]
                    folium.Rectangle(bounds=bounds, color="blue", fill=True, fill_opacity=0.2, popup=f"Rectangle: {ann['comment']}").add_to(m)
            Draw(export=False, draw_options={"polyline": False, "polygon": False, "circle": False, "circlemarker": False, "marker": True, "rectangle": True}, edit_options={"edit": True}).add_to(m)
            st.subheader("Zoomer, déplacer et dessiner")
            out = st_folium(m, width=800, height=600, returned_objects=["all_drawings"])
            feats = []
            if out is not None:
                if isinstance(out, list):
                    feats = out
                elif isinstance(out, dict):
                    drawings = out.get("all_drawings", {})
                    if isinstance(drawings, dict):
                        feats = drawings.get("features", [])
                    else:
                        feats = drawings if isinstance(drawings, list) else []
                else:
                    feats = []
            prev_count = st.session_state["drawn_feats_count"]
            if len(feats) > prev_count:
                feat = feats[-1]
                geom = feat["geometry"]
                if geom["type"] == "Point":
                    y, x = geom["coordinates"]
                    x_norm, y_norm = x/w, y/h
                    width_norm, height_norm = 0.0, 0.0
                    ann_type = "point"
                else:
                    coords = geom["coordinates"][0][:-1]
                    xs = [pt[1] for pt in coords]
                    ys = [pt[0] for pt in coords]
                    x_min, x_max = min(xs), max(xs)
                    y_min, y_max = min(ys), max(ys)
                    x_norm = x_min/w
                    y_norm = y_min/h
                    width_norm = (x_max - x_min)/w
                    height_norm = (y_max - y_min)/h
                    ann_type = "rectangle"
                st.session_state["current_annotation"] = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "type": ann_type,
                    "x": round(x_norm, 4),
                    "y": round(y_norm, 4),
                    "width": round(width_norm, 4),
                    "height": round(height_norm, 4),
                    "category": "Autre",
                    "intervenant": "",
                    "comment": "",
                    "photo": None,
                    "status": "À faire",
                    "due_date": ""
                }
                st.session_state["drawn_feats_count"] = len(feats)
                st.rerun()
            if st.session_state["current_annotation"]:
                st.sidebar.header("Détails de la nouvelle annotation")
                category = st.sidebar.selectbox("Catégorie", ["QHSE","Qualité","Planning","Autre"], index=["QHSE","Qualité","Planning","Autre"].index(st.session_state["current_annotation"]["category"]))
                intervenant = st.sidebar.selectbox("Intervenant", ["Architecte","Électricien","Client","Assistante"], index=0 if not st.session_state["current_annotation"]["intervenant"] else ["Architecte","Électricien","Client","Assistante"].index(st.session_state["current_annotation"]["intervenant"]))
                comment = st.sidebar.text_area("Commentaire", value=st.session_state["current_annotation"]["comment"])
                photo_file = st.sidebar.file_uploader("Ajouter une photo", type=["png","jpg","jpeg"])
                status = st.sidebar.selectbox("Statut", ["À faire","En cours","Résolu"], index=["À faire","En cours","Résolu"].index(st.session_state["current_annotation"]["status"]))
                due_date = st.sidebar.date_input("Échéance", value=datetime.strptime(st.session_state["current_annotation"]["due_date"], "%Y-%m-%d") if st.session_state["current_annotation"]["due_date"] else datetime.today())
                photo_path = None
                if photo_file:
                    os.makedirs("photos", exist_ok=True)
                    photo_path = os.path.join("photos", photo_file.name)
                    with open(photo_path, "wb") as f:
                        f.write(photo_file.read())
                if st.sidebar.button("Enregistrer l'annotation"):
                    ann = st.session_state["current_annotation"].copy()
                    ann.update({"category": category, "intervenant": intervenant, "comment": comment, "photo": photo_path, "status": status, "due_date": due_date.strftime("%Y-%m-%d")})
                    image_idx = next(i for i, img in enumerate(project["images"]) if img["image_name"] == name)
                    project["images"][image_idx]["annotations"].append(ann)
                    with open(ANNOTATIONS_FILE, "w") as f:
                        json.dump(st.session_state["projects"], f, indent=2)
                    st.session_state["current_annotation"] = None
                    st.rerun()

# ── 5) PAGE “Gérer” ───────────────────────────────────────────────────────────────
elif page == "Gérer":
    st.header("Gérer les annotations")
    if not st.session_state["projects"]:
        st.warning("Aucun projet existant.")
    else:
        project_names = [proj["project_name"] for proj in st.session_state["projects"] if "project_name" in proj]
        selected_project = st.selectbox("Sélectionnez un projet", project_names)
        project_idx = next(i for i, proj in enumerate(st.session_state["projects"]) if proj["project_name"] == selected_project)
        project = st.session_state["projects"][project_idx]
        if not project["images"]:
            st.warning("Aucune image dans ce projet.")
        else:
            image_names = [img["image_name"] for img in project["images"]]
            selected_image = st.selectbox("Sélectionnez une image", image_names)
            image_idx = next(i for i, img in enumerate(project["images"]) if img["image_name"] == selected_image)
            image_data = project["images"][image_idx]
            if not image_data["annotations"]:
                st.warning("Aucune annotation pour cette image.")
            else:
                df = pd.DataFrame(image_data["annotations"])
                st.write("### Toutes les annotations")
                st.dataframe(df)
                st.sidebar.header("Filtres")
                cats = df["category"].unique().tolist()
                ivts = df["intervenant"].unique().tolist()
                stats = df["status"].unique().tolist()
                f_cats = st.sidebar.multiselect("Catégorie", options=cats, default=cats)
                f_ivts = st.sidebar.multiselect("Intervenant", options=ivts, default=ivts)
                f_stats = st.sidebar.multiselect("Statut", options=stats, default=stats)
                filt = df[df["category"].isin(f_cats) & df["intervenant"].isin(f_ivts) & df["status"].isin(f_stats)]
                st.write("### Résultats filtrés")
                st.dataframe(filt)
                st.write("### Mettre à jour statut")
                idx = st.selectbox("Sélectionner une annotation", filt.index, format_func=lambda i: f"{filt.loc[i,'timestamp']} – {filt.loc[i,'comment'][:20]}")
                new_stat = st.selectbox("Nouveau statut", ["À faire","En cours","Résolu"], key="upd_status")
                if st.button("Mettre à jour"):
                    project["images"][image_idx]["annotations"][idx]["status"] = new_stat
                    with open(ANNOTATIONS_FILE, "w") as f:
                        json.dump(st.session_state["projects"], f, indent=2)
                    st.rerun()

# ── 6) PAGE “Planning” ────────────────────────────────────────────────────────────
elif page == "Planning":
    st.header("Planning des tâches")
    if not st.session_state["projects"]:
        st.warning("Aucun projet existant.")
    else:
        project_names = [proj["project_name"] for proj in st.session_state["projects"] if "project_name" in proj]
        selected_project = st.selectbox("Sélectionnez un projet", project_names)
        project_idx = next(i for i, proj in enumerate(st.session_state["projects"]) if proj["project_name"] == selected_project)
        project = st.session_state["projects"][project_idx]
        if not project["images"]:
            st.warning("Aucune image dans ce projet.")
        else:
            image_names = [img["image_name"] for img in project["images"]]
            selected_image = st.selectbox("Sélectionnez une image", image_names)
            image_idx = next(i for i, img in enumerate(project["images"]) if img["image_name"] == selected_image)
            image_data = project["images"][image_idx]
            if not image_data["annotations"]:
                st.warning("Aucune tâche planifiée pour cette image.")
            else:
                df = pd.DataFrame(image_data["annotations"])
                if "due_date" in df.columns:
                    df["due_date"] = pd.to_datetime(df["due_date"])
                    dr = st.date_input("Plage de dates", [], key="cal_range")
                    if len(dr) == 2:
                        start, end = dr
                        df = df[(df["due_date"] >= start) & (df["due_date"] <= end)]
                    st.dataframe(df[["timestamp","category","intervenant","comment","status","due_date"]])
                else:
                    st.info("Pas d’échéance disponible.")