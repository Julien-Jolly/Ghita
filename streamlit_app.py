import streamlit as st
import pandas as pd
import numpy as np
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium
from datetime import datetime
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle, Image as RLImage
from reportlab.lib import colors
import io, os, json
from io import BytesIO
import os.path

# PDF → Image
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError
import fitz  # PyMuPDF fallback

# Amazon S3
import boto3

# OneDrive / Microsoft Graph
import msal, requests

# Configuration globale
st.set_page_config(page_title="BuildozAir Simplifié", layout="wide")

# Configuration AWS S3
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
S3_BUCKET_NAME = "jujul"
S3_PREFIX = "buildozair/"
S3_ANNOTATIONS_KEY = f"{S3_PREFIX}annotations.json"

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
        s3_key = S3_PREFIX + file_name
        s3_client.upload_fileobj(io.BytesIO(file_content), S3_BUCKET_NAME, s3_key)
        return s3_key
    except Exception as e:
        st.error(f"Erreur lors du téléversement de {file_name} sur S3 : {e}")
        return None


def download_from_s3(file_key):
    try:
        if file_key and not file_key.startswith(S3_PREFIX):
            file_key = S3_PREFIX + file_key
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=file_key)
        return response['Body'].read()
    except Exception as e:
        st.error(f"Erreur lors du téléchargement de {file_key} depuis S3 : {e}")
        return None


def load_projects_from_s3():
    try:
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=S3_ANNOTATIONS_KEY)
        projects = json.loads(response['Body'].read().decode('utf-8'))
        return projects
    except s3_client.exceptions.NoSuchKey:
        return []
    except Exception as e:
        st.error(f"Erreur lors du chargement des projets depuis S3 : {e}")
        return []


def save_projects_to_s3(projects):
    try:
        s3_client.upload_fileobj(
            io.BytesIO(json.dumps(projects, indent=2).encode('utf-8')),
            S3_BUCKET_NAME,
            S3_ANNOTATIONS_KEY
        )
    except Exception as e:
        st.error(f"Erreur lors de la sauvegarde des projets sur S3 : {e}")


# Charger les projets depuis S3 au démarrage
if "projects" not in st.session_state:
    st.session_state["projects"] = load_projects_from_s3()

# Migration des anciennes données
for project in st.session_state["projects"]:
    for image in project.get("images", []):
        if "image_key" in image and not image["image_key"].startswith(S3_PREFIX):
            old_key = image["image_key"]
            new_key = S3_PREFIX + old_key
            try:
                uploaded_bytes = download_from_s3(old_key)
                if uploaded_bytes:
                    upload_to_s3(old_key, uploaded_bytes)
                    s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=old_key)
                    image["image_key"] = new_key
            except Exception as e:
                st.error(f"Erreur lors de la migration de {old_key} : {e}")
        elif "image_path" in image and "image_key" not in image:
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
save_projects_to_s3(st.session_state["projects"])

if not st.session_state["projects"] or not any("project_name" in proj for proj in st.session_state["projects"]):
    st.session_state["projects"] = [{"project_name": "Projet par défaut", "images": []}]
if "selected_project" not in st.session_state:
    st.session_state["selected_project"] = st.session_state["projects"][0]["project_name"]
if "drawn_feats_count" not in st.session_state:
    st.session_state["drawn_feats_count"] = 0
if "current_annotation" not in st.session_state:
    st.session_state["current_annotation"] = None
if "last_drawings" not in st.session_state:
    st.session_state["last_drawings"] = []
if "map_state" not in st.session_state:
    st.session_state["map_state"] = {}

# OneDrive configuration
CLIENT_ID = "votre_client_id"
TENANT_ID = "votre_tenant_id"
CLIENT_SECRET = "votre_client_secret"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]


def get_onedrive_token():
    app = msal.ConfidentialClientApplication(CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET)
    res = app.acquire_token_for_client(scopes=SCOPES)
    return res.get("access_token")


def list_onedrive_files():
    token = get_onedrive_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get("https://graph.microsoft.com/v1.0/me/drive/root/children", headers=headers)
    return resp.json().get("value", [])


def generate_planning_pdf(pil_img, df_all_annotations, df_plan, start_date, end_date):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=landscape(A4))
    width, height = landscape(A4)

    # --- PAGE 1 : plan annoté plein cadre ---
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(width/2, height - 40, "Plan annoté")
    c.setFont("Helvetica", 12)
    subtitle = f"Période : {start_date.strftime('%Y-%m-%d')} → {end_date.strftime('%Y-%m-%d')}"
    c.drawCentredString(width/2, height - 60, subtitle)

    # 1) On passe l'image PIL en buffer PNG (sans inversion)
    img_buffer = BytesIO()
    pil_img.save(img_buffer, format="PNG")
    img_buffer.seek(0)

    # 2) On calcule la taille dans le PDF (90% de la largeur, ratio conservé)
    img_w = width * 0.9
    img_h = img_w * (pil_img.height / pil_img.width)
    max_img_h = height - 120
    if img_h > max_img_h:
        img_h = max_img_h
        img_w = img_h * (pil_img.width / pil_img.height)
    x_img = (width - img_w) / 2
    y_img = height - 100 - img_h

    # 3) On dessine l'image (déjà annotée)
    RLImage(img_buffer, width=img_w, height=img_h).drawOn(c, x_img, y_img)

    # 4) Optionnel : cadre autour de l'image
    c.rect(x_img, y_img, img_w, img_h, stroke=1, fill=0)

    # pied de page
    c.setFont("Helvetica", 8)
    c.drawRightString(width - 20, 10, "Page 1/2")
    c.showPage()

    # --- PAGE 2 : tableaux ---
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(width/2, height - 40, "Détails des annotations et planning")

    margin = 20
    y = height - 80

    # 1) Toutes les annotations
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "1) Toutes les annotations")
    y -= 20

    data1 = [["Timestamp", "Catégorie", "Intervenant", "Commentaire", "Statut", "Échéance"]]
    for _, r in df_all_annotations.iterrows():
        data1.append([
            str(r["timestamp"]),
            r["category"],
            r["intervenant"],
            r["comment"],
            r["status"],
            r["due_date"].date().isoformat()
        ])
    tbl1 = Table(data1, colWidths=[80, 60, 60, 180, 60, 60])
    tbl1.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))
    w1, h1 = tbl1.wrapOn(c, width - 2 * margin, y)
    tbl1.drawOn(c, margin, y - h1)
    y -= h1 + 40

    # 2) Planning filtré
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "2) Planning des tâches")
    y -= 20

    data2 = [["Timestamp", "Catégorie", "Intervenant", "Commentaire", "Statut", "Échéance"]]
    for _, r in df_plan.iterrows():
        data2.append([
            str(r["timestamp"]),
            r["category"],
            r["intervenant"],
            r["comment"],
            r["status"],
            r["due_date"].date().isoformat()
        ])
    tbl2 = Table(data2, colWidths=[80, 60, 60, 180, 60, 60])
    tbl2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))
    w2, h2 = tbl2.wrapOn(c, width - 2 * margin, y)
    tbl2.drawOn(c, margin, y - h2)

    c.setFont("Helvetica", 8)
    c.drawRightString(width - 20, 10, "Page 2/2")

    c.save()
    buffer.seek(0)
    return buffer


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


def delete_project(project_name):
    st.session_state["projects"] = [p for p in st.session_state["projects"] if p["project_name"] != project_name]
    save_projects_to_s3(st.session_state["projects"])
    if st.session_state["selected_project"] == project_name:
        st.session_state["selected_project"] = st.session_state["projects"][0]["project_name"] if st.session_state[
            "projects"] else "Projet par défaut"
    st.rerun()


def generate_s3_url(file_key):
    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET_NAME, 'Key': file_key},
            ExpiresIn=3600
        )
        return url
    except Exception as e:
        st.error(f"Erreur lors de la génération du lien pour {file_key} : {e}")
        return None


# Pages
st.sidebar.title("Navigation")
page = st.sidebar.radio("Aller à", ["Annoter", "Gérer", "Planning"])

if page == "Annoter":
    st.header("Annoter le plan")
    project_names = [proj["project_name"] for proj in st.session_state["projects"] if "project_name" in proj]
    project_names.append("Nouveau projet")
    selected_project = st.selectbox("Sélectionnez un projet", project_names,
                                    index=project_names.index(st.session_state["selected_project"]))
    st.session_state["selected_project"] = selected_project

    uploaded_bytes = None
    name = None
    image_key = None

    if selected_project == "Nouveau projet":
        new_project_name = st.text_input("Nom du nouveau projet")
        if st.button("Créer le projet") and new_project_name:
            st.session_state["projects"].append({"project_name": new_project_name, "images": []})
            save_projects_to_s3(st.session_state["projects"])
            st.session_state["selected_project"] = new_project_name
            st.rerun()
        else:
            st.write("Veuillez uploader une nouvelle image pour ce projet.")
            source = st.radio("Source du plan", ["Local", "OneDrive"], disabled=not new_project_name)
            if source == "Local" and new_project_name:
                up = st.file_uploader("Uploadez PNG/JPG/PDF", type=["png", "jpg", "jpeg", "pdf"])
                if up:
                    uploaded_bytes = up.read()
                    name = up.name
            elif source == "OneDrive" and new_project_name:
                files = list_onedrive_files()
                names = [f["name"] for f in files if f["name"].lower().endswith((".png", ".jpg", ".jpeg", ".pdf"))]
                name = st.selectbox("Sélectionnez un fichier OneDrive", names)
                if name:
                    token = get_onedrive_token()
                    fid = next(f["id"] for f in files if f["name"] == name)
                    headers = {"Authorization": f"Bearer {token}"}
                    resp = requests.get(f"https://graph.microsoft.com/v1.0/me/drive/items/{fid}/content",
                                        headers=headers)
                    uploaded_bytes = resp.content

            if uploaded_bytes and name and new_project_name:
                image_key = upload_to_s3(name, uploaded_bytes)
                if image_key:
                    project_idx = next(i for i, proj in enumerate(st.session_state["projects"]) if
                                       proj["project_name"] == new_project_name)
                    st.session_state["projects"][project_idx]["images"].append(
                        {"image_name": name, "image_key": image_key, "annotations": []})
                    save_projects_to_s3(st.session_state["projects"])
                    st.rerun()

    else:
        project_idx = next(
            i for i, proj in enumerate(st.session_state["projects"]) if proj["project_name"] == selected_project)
        project = st.session_state["projects"][project_idx]
        image_names = [img["image_name"] for img in project["images"]]
        image_names.append("Ajouter une nouvelle image")
        selected_image = st.selectbox("Sélectionnez une image", image_names, key=f"select_image_{selected_project}")

        if selected_image == "Ajouter une nouvelle image":
            source = st.radio("Source du plan", ["Local", "OneDrive"])
            if source == "Local":
                up = st.file_uploader("Uploadez PNG/JPG/PDF", type=["png", "jpg", "jpeg", "pdf"])
                if up:
                    uploaded_bytes = up.read()
                    name = up.name
            else:
                files = list_onedrive_files()
                names = [f["name"] for f in files if f["name"].lower().endswith((".png", ".jpg", ".jpeg", ".pdf"))]
                name = st.selectbox("Sélectionnez un fichier OneDrive", names)
                if name:
                    token = get_onedrive_token()
                    fid = next(f["id"] for f in files if f["name"] == name)
                    headers = {"Authorization": f"Bearer {token}"}
                    resp = requests.get(f"https://graph.microsoft.com/v1.0/me/drive/items/{fid}/content",
                                        headers=headers)
                    uploaded_bytes = resp.content

            if uploaded_bytes and name:
                image_key = upload_to_s3(name, uploaded_bytes)
                if image_key:
                    image_exists = any(img["image_name"] == name for img in project["images"])
                    if not image_exists:
                        project["images"].append({"image_name": name, "image_key": image_key, "annotations": []})
                        save_projects_to_s3(st.session_state["projects"])
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
                            save_projects_to_s3(st.session_state["projects"])
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

        if uploaded_bytes and name:
            image = load_image_from_bytes(uploaded_bytes, name)
            if image:
                arr = np.array(image)
                h, w = arr.shape[:2]
                m = folium.Map(location=[h / 2, w / 2], zoom_start=0, crs="Simple", min_zoom=-1, max_zoom=4,
                               width="100%", height=600)
                folium.raster_layers.ImageOverlay(image=arr, bounds=[[0, 0], [h, w]], interactive=True,
                                                  cross_origin=False, opacity=1).add_to(m)
                image_idx = next(i for i, img in enumerate(project["images"]) if img["image_name"] == name)
                annotations = project["images"][image_idx]["annotations"]
                for ann in annotations:
                    x_pix = ann["x"] * w
                    y_pix = ann["y"] * h

                    if ann["type"] == "point":
                        folium.Marker(
                            location=[y_pix, x_pix],
                            popup=ann["comment"],
                            icon=folium.Icon(color="red", icon="circle")
                        ).add_to(m)
                    else:
                        w_px = ann["width"] * w
                        h_px = ann["height"] * h
                        bounds = [
                            [y_pix, x_pix],  # coin supérieur-gauche
                            [y_pix + h_px, x_pix + w_px]  # coin inférieur-droit
                        ]
                        folium.Rectangle(
                            bounds=bounds,
                            color="blue", fill=True, fill_opacity=0.2,
                            popup=ann["comment"]
                        ).add_to(m)
                Draw(export=False,
                     draw_options={"polyline": False, "polygon": False, "circle": False, "circlemarker": False,
                                   "marker": True, "rectangle": True}, edit_options={"edit": True}).add_to(m)
                st.subheader("Zoomer, déplacer et dessiner")
                out = st_folium(m, width=800, height=600, returned_objects=["all_drawings"],
                                key=f"folium_map_{selected_project}_{selected_image}")
                feats = []
                if out and "all_drawings" in out:
                    drawings = out.get("all_drawings", {})
                    if isinstance(drawings, dict):
                        feats = drawings.get("features", [])
                    else:
                        feats = drawings if isinstance(drawings, list) else []

                current_drawings = sorted([(f["geometry"]["type"], tuple(
                    f["geometry"]["coordinates"][0] if f["geometry"]["type"] != "Point" else f["geometry"][
                        "coordinates"])) for f in feats])
                if current_drawings != st.session_state["last_drawings"]:
                    if feats and len(feats) > len(
                            [d for d in st.session_state["last_drawings"] if d[0] in ["Point", "Rectangle"]]):
                        feat = feats[-1]
                        geom = feat["geometry"]
                        if geom["type"] == "Point":
                            # GeoJSON Point: [x, y]
                            x_pix, y_pix = geom["coordinates"]
                            x_norm = x_pix / w
                            y_norm = y_pix / h
                            width_norm = height_norm = 0.0
                            ann_type = "point"
                        else:
                            # GeoJSON Polygon: [[[x1,y1], [x2,y2], …]]
                            coords = geom["coordinates"][0][:-1]
                            xs = [pt[0] for pt in coords]
                            ys = [pt[1] for pt in coords]
                            x_min, x_max = min(xs), max(xs)
                            y_min, y_max = min(ys), max(ys)
                            x_norm = x_min / w
                            y_norm = y_min / h
                            width_norm = (x_max - x_min) / w
                            height_norm = (y_max - y_min) / h
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
                        st.session_state["last_drawings"] = current_drawings
                        st.rerun()
                    else:
                        st.session_state["last_drawings"] = current_drawings

                if st.session_state["current_annotation"]:
                    st.sidebar.header("Détails de la nouvelle annotation")
                    category = st.sidebar.selectbox("Catégorie", ["QHSE", "Qualité", "Planning", "Autre"],
                                                    index=["QHSE", "Qualité", "Planning", "Autre"].index(
                                                        st.session_state["current_annotation"]["category"]))
                    intervenant = st.sidebar.selectbox("Intervenant",
                                                       ["Architecte", "Électricien", "Client", "Assistante"],
                                                       index=0 if not st.session_state["current_annotation"][
                                                           "intervenant"] else ["Architecte", "Électricien", "Client",
                                                                                "Assistante"].index(
                                                           st.session_state["current_annotation"]["intervenant"]))
                    comment = st.sidebar.text_area("Commentaire",
                                                   value=st.session_state["current_annotation"]["comment"])
                    photo_file = st.sidebar.file_uploader("Ajouter une photo", type=["png", "jpg", "jpeg"])
                    status = st.sidebar.selectbox("Statut", ["À faire", "En cours", "Résolu"],
                                                  index=["À faire", "En cours", "Résolu"].index(
                                                      st.session_state["current_annotation"]["status"]))
                    due_date = st.sidebar.date_input("Échéance", value=datetime.strptime(
                        st.session_state["current_annotation"]["due_date"], "%Y-%m-%d") if
                    st.session_state["current_annotation"]["due_date"] else datetime.today())
                    photo_path = None
                    if photo_file:
                        photo_name = f"photos/{photo_file.name}"
                        photo_key = upload_to_s3(photo_name, photo_file.read())
                        if photo_key:
                            photo_path = photo_key
                    if st.sidebar.button("Enregistrer l'annotation"):
                        ann = st.session_state["current_annotation"].copy()
                        ann.update(
                            {"category": category, "intervenant": intervenant, "comment": comment, "photo": photo_path,
                             "status": status, "due_date": due_date.strftime("%Y-%m-%d")})
                        image_idx = next(i for i, img in enumerate(project["images"]) if img["image_name"] == name)
                        project["images"][image_idx]["annotations"].append(ann)
                        save_projects_to_s3(st.session_state["projects"])
                        st.session_state["current_annotation"] = None
                        st.session_state["last_drawings"] = current_drawings
                        st.rerun()

elif page == "Gérer":
    st.header("Gérer les annotations")
    if not st.session_state["projects"]:
        st.warning("Aucun projet existant.")
    else:
        project_names = [proj["project_name"] for proj in st.session_state["projects"] if "project_name" in proj]
        selected_project = st.selectbox("Sélectionnez un projet", project_names,
                                        index=project_names.index(st.session_state["selected_project"]))
        st.session_state["selected_project"] = selected_project
        project_idx = next(
            i for i, proj in enumerate(st.session_state["projects"]) if proj["project_name"] == selected_project)
        project = st.session_state["projects"][project_idx]
        if st.button("Supprimer ce projet"):
            delete_project(selected_project)
        if not project["images"]:
            st.warning("Aucune image dans ce projet.")
        else:
            for i, image in enumerate(project["images"]):
                st.subheader(f"Image : {image['image_name']}")
                if "image_key" in image:
                    image_url = generate_s3_url(image["image_key"])
                    if image_url:
                        # Affiche une miniature 200px cliquable
                        st.markdown(
                            f'''
                            <a href="{image_url}" target="_blank">
                              <img src="{image_url}" width="200" style="object-fit:cover; border:1px solid #ccc; margin-bottom:4px;" />
                            </a>
                            ''',
                            unsafe_allow_html=True
                        )
                        st.write(f"[Ouvrir le plan en grand]({image_url})")
                    else:
                        st.warning("Impossible de générer le lien pour cette image.")
                if image["annotations"]:
                    df = pd.DataFrame(image["annotations"])
                    st.write("### Annotations")
                    for idx, row in df.iterrows():
                        st.write(f"**Annotation {idx + 1} : {row['comment']}**")
                        st.write(
                            f"Type: {row['type']}, Statut: {row['status']}, Catégorie: {row['category']}, Intervenant: {row['intervenant']}, Échéance: {row['due_date']}")
                        if row['photo']:
                            photo_url = generate_s3_url(row['photo'])
                            if photo_url:
                                # Affiche une miniature 100px cliquable
                                st.markdown(
                                    f'''
                                    <a href="{photo_url}" target="_blank">
                                      <img src="{photo_url}" width="100" style="object-fit:cover; border:1px solid #ccc; margin-bottom:4px;" />
                                    </a>
                                    ''',
                                    unsafe_allow_html=True,
                                )
                                st.write(f"[Voir la photo]({photo_url})")
                            else:
                                st.warning("Impossible de charger la photo associée.")
                    st.dataframe(df)
                    # Carte interactive pour les annotations
                    uploaded_bytes = download_from_s3(image["image_key"]) if "image_key" in image else None
                    if uploaded_bytes:
                        img = load_image_from_bytes(uploaded_bytes, image["image_name"])
                        if img:
                            arr = np.array(img)
                            h, w = arr.shape[:2]
                            m = folium.Map(location=[h / 2, w / 2], zoom_start=0, crs="Simple", min_zoom=-1, max_zoom=4,
                                           width="100%", height=400)
                            folium.raster_layers.ImageOverlay(image=arr, bounds=[[0, 0], [h, w]], interactive=True,
                                                              cross_origin=False, opacity=1).add_to(m)
                            for ann in image["annotations"]:
                                x = ann["x"] * w
                                y = ann["y"] * h
                                if ann["type"] == "point":
                                    folium.Marker(location=[y, x], popup=f"{ann['comment']} (Statut: {ann['status']})",
                                                  icon=folium.Icon(color="red", icon="circle")).add_to(m)
                                elif ann["type"] == "rectangle":
                                    width = ann["width"] * w
                                    height = ann["height"] * h
                                    y0 = ann["y"] * h
                                    x0 = ann["x"] * w
                                    h_px = ann["height"] * h
                                    w_px = ann["width"] * w
                                    bounds = [
                                        [y0, x0],  # coin supérieur gauche
                                        [y0 + h_px, x0 + w_px]  # coin inférieur droit
                                    ]
                                    folium.Rectangle(bounds=bounds, color="blue", fill=True, fill_opacity=0.2,
                                                     popup=f"{ann['comment']} (Statut: {ann['status']})").add_to(m)
                            st_folium(m, width=800, height=400,
                                      key=f"manage_map_{selected_project}_{image['image_name']}")
                else:
                    st.write("Aucune annotation pour cette image.")
            st.sidebar.header("Filtres")
            if project["images"]:
                all_annotations = pd.concat(
                    [pd.DataFrame(img["annotations"]) for img in project["images"] if img["annotations"]],
                    ignore_index=True)
                if not all_annotations.empty:
                    cats = all_annotations["category"].unique().tolist()
                    ivts = all_annotations["intervenant"].unique().tolist()
                    stats = all_annotations["status"].unique().tolist()
                    f_cats = st.sidebar.multiselect("Catégorie", options=cats, default=cats)
                    f_ivts = st.sidebar.multiselect("Intervenant", options=ivts, default=ivts)
                    f_stats = st.sidebar.multiselect("Statut", options=stats, default=stats)
                    filt = all_annotations[
                        all_annotations["category"].isin(f_cats) & all_annotations["intervenant"].isin(f_ivts) &
                        all_annotations["status"].isin(f_stats)]
                    st.write("### Résultats filtrés")
                    st.dataframe(filt)
                    st.write("### Mettre à jour statut")
                    idx = st.selectbox("Sélectionner une annotation", filt.index, format_func=lambda
                        i: f"{filt.loc[i, 'timestamp']} – {filt.loc[i, 'comment'][:20]}")
                    image_idx = next(i for i, img in enumerate(project["images"]) if
                                     any(ann["timestamp"] == filt.loc[idx, "timestamp"] for ann in img["annotations"]))
                    new_stat = st.selectbox("Nouveau statut", ["À faire", "En cours", "Résolu"], key="upd_status")
                    if st.button("Mettre à jour"):
                        for ann in project["images"][image_idx]["annotations"]:
                            if ann["timestamp"] == filt.loc[idx, "timestamp"]:
                                ann["status"] = new_stat
                                break
                        save_projects_to_s3(st.session_state["projects"])
                        st.rerun()

elif page == "Planning":
    st.header("Planning des tâches")
    if not st.session_state["projects"]:
        st.warning("Aucun projet existant.")
    else:
        project_names = [proj["project_name"] for proj in st.session_state["projects"] if "project_name" in proj]
        selected_project = st.selectbox("Sélectionnez un projet", project_names,
                                        index=project_names.index(st.session_state["selected_project"]))
        st.session_state["selected_project"] = selected_project
        project_idx = next(
            i for i, proj in enumerate(st.session_state["projects"]) if proj["project_name"] == selected_project)
        project = st.session_state["projects"][project_idx]
        if not project["images"]:
            st.warning("Aucune image dans ce projet.")
        else:
            annotations_list = [pd.DataFrame(img["annotations"]) for img in project["images"] if img["annotations"]]
            if annotations_list:
                all_annotations = pd.concat(annotations_list, ignore_index=True)
                if "due_date" in all_annotations.columns:
                    all_annotations["due_date"] = pd.to_datetime(all_annotations["due_date"], errors='coerce')
                    dr = st.date_input("Plage de dates", [], key="cal_range")
                    if len(dr) == 2:
                        start, end = dr
                        start = pd.to_datetime(start)
                        end = pd.to_datetime(end)
                        filt = all_annotations[
                            all_annotations["due_date"].notna() & (all_annotations["due_date"] >= start) & (
                                        all_annotations["due_date"] <= end)]
                        if not filt.empty:
                            st.dataframe(
                                filt[["timestamp", "category", "intervenant", "comment", "status", "due_date"]])
                            # Génération du PDF avec l'image annotée
                            if st.button("Générer PDF"):
                                # 1) Récupérez d’abord l’octet de l’image depuis S3
                                image_data = project["images"][0]  # ou l’index que vous voulez
                                uploaded_bytes = download_from_s3(image_data["image_key"])
                                # 2) Chargez vraiment l’image en PIL
                                pil_img = load_image_from_bytes(uploaded_bytes, image_data["image_name"])
                                if pil_img is None:
                                    st.error("Impossible de charger le plan pour PDF")
                                else:
                                    # 3) Convertissez-la en RGB et dessinez vos annotations
                                    img = pil_img.convert("RGB")
                                    draw = ImageDraw.Draw(img)
                                    w, h = img.size
                                    for _, ann in filt.iterrows():
                                        x_pix = ann["x"] * w
                                        # Inverser la coordonnée Y pour correspondre au système ReportLab
                                        y_pix = (1 - ann["y"]) * h
                                        if ann["type"] == "point":
                                            r = 10
                                            draw.ellipse(
                                                [x_pix - r, y_pix - r, x_pix + r, y_pix + r],
                                                fill="red",
                                                outline="black"
                                            )
                                        else:
                                            w_px = ann["width"] * w
                                            h_px = ann["height"] * h
                                            # Ajuster le rectangle avec l'inversion Y correcte
                                            y_top = y_pix  # Coin supérieur (inversé)
                                            y_bottom = y_pix - h_px  # Coin inférieur
                                            draw.rectangle([x_pix, y_bottom, x_pix + w_px, y_top],
                                                           outline="blue", width=2)

                                    # 4) Puis passez img à votre générateur ReportLab
                                    # Construisez d’abord annotation_desc avant d’appeler le PDF :
                                    annotation_desc = []
                                    for _, ann in filt.iterrows():
                                        pos = f"x={ann['x']:.2f}, y={ann['y']:.2f}"
                                        if ann['type'] == "rectangle":
                                            pos += f", largeur={ann['width']:.2f}, hauteur={ann['height']:.2f}"
                                        annotation_desc.append(
                                            f"{ann['type'].capitalize()} à {pos} – {ann['comment']} (Statut : {ann['status']})"
                                        )

                                    # Puis :
                                    pdf_buffer = generate_planning_pdf(img, all_annotations, filt, start, end)
                                    st.download_button("Télécharger le PDF", data=pdf_buffer,
                                                       file_name="planning.pdf", mime="application/pdf")
                        else:
                            st.info("Aucune tâche dans cette plage de dates.")
                else:
                    st.info("Pas d’échéance disponible.")
            else:
                st.info("Aucune annotation enregistrée dans ce projet.")