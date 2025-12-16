from flask import Flask, render_template, request
import requests
import os
from azure.storage.blob import BlobServiceClient
from datetime import datetime
import pyodbc
import uuid
from urllib.parse import urlparse
from azure.core.exceptions import ResourceExistsError


app = Flask(__name__)

# ---- Computer Vision config ----
# In Azure App Service → Configuration:
#   CV_KEY = <your key>
#   CV_ENDPOINT = https://<your-resource>.cognitiveservices.azure.com/
SUBSCRIPTION_KEY = os.environ.get("CV_KEY")
ENDPOINT = os.environ.get("CV_ENDPOINT")

SQL_CONNECTION_STRING = os.environ.get("SQL_CONNECTION_STRING")

def save_prediction_to_db(image_url, email, sport, score, blob_url):
    """Insert one prediction row into Azure SQL Database, including blob URL."""
    if not SQL_CONNECTION_STRING:
        print("No SQL connection string configured, skipping DB save.")
        return

    try:
        conn = pyodbc.connect(SQL_CONNECTION_STRING)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO Predictions (ImageUrl, Email, Sport, Score.BlobUrl)
            VALUES (?, ?, ?, ?, ?)
            """,
            (image_url, blob_url, email, sport, float(score))
        )
        conn.commit()
        cursor.close()
        conn.close()
        print("Saved prediction to SQL DB with BlobUrl.")
    except Exception as e:
        print(f"Error saving prediction to SQL DB: {e}")



if ENDPOINT:
    ENDPOINT = ENDPOINT.rstrip("/")
    ANALYZE_URL = ENDPOINT + "/vision/v3.2/analyze"
else:
    ANALYZE_URL = None

# ---- Blob Storage config ----
# In Azure App Service → Configuration:
#   STORAGE_CONNECTION_STRING = <connection string>
#   STORAGE_CONTAINER_NAME = logs
STORAGE_CONNECTION_STRING = os.environ.get("STORAGE_CONNECTION_STRING")
STORAGE_CONTAINER_NAME = os.environ.get("STORAGE_CONTAINER_NAME", "logs")

IMAGE_CONTAINER_NAME = os.environ.get("IMAGE_CONTAINER_NAME", "sport-images")

blob_service_client = None
blob_container_client = None

if STORAGE_CONNECTION_STRING and STORAGE_CONTAINER_NAME:
    try:
        blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        blob_container_client = blob_service_client.get_container_client(STORAGE_CONTAINER_NAME)
    except Exception as e:
        print(f"Error initializing Blob Storage client: {e}")


def log_prediction_to_blob(image_url, prediction, score):
    """Append a log line to a text blob in Azure Blob Storage."""
    global blob_container_client

    if not blob_container_client:
        return  # logging is optional; don't crash if storage not configured

    try:
        timestamp = datetime.utcnow().isoformat()
        line = f"{timestamp},{image_url},{prediction},{score}\n"
        blob_name = "predictions.log"

        # Download existing content (if any)
        try:
            existing_blob = blob_container_client.download_blob(blob_name).readall().decode("utf-8")
        except Exception:
            existing_blob = ""

        new_content = existing_blob + line
        blob_container_client.upload_blob(
            name=blob_name,
            data=new_content,
            overwrite=True
        )
    except Exception as e:
        print(f"Error logging to blob: {e}")

def upload_image_to_blob_from_url(image_url):
    """Download image from the given URL and upload it to Azure Blob Storage.
       Returns the blob URL, or None if something failed.
    """
    if not blob_service_client:
        print("Blob service not configured, cannot upload image.")
        return None

    # 1) Download the image bytes
    try:
        img_response = requests.get(image_url, timeout=10)
        img_response.raise_for_status()
        image_bytes = img_response.content
        print(f"Downloaded image: {len(image_bytes)} bytes")
    except Exception as e:
        print(f"Error downloading image from URL: {e}")
        return None

    try:
        # 2) Get / create the 'sport-images' container
        image_container_client = blob_service_client.get_container_client(IMAGE_CONTAINER_NAME)
        try:
            image_container_client.create_container()
            print(f"Created container '{IMAGE_CONTAINER_NAME}'")
        except ResourceExistsError:
            pass  # already exists

        # 3) Generate a unique blob name
        path = urlparse(image_url).path
        ext = os.path.splitext(path)[1] or ".jpg"
        blob_name = f"{uuid.uuid4()}{ext}"

        blob_client = image_container_client.get_blob_client(blob_name)
        blob_client.upload_blob(image_bytes, overwrite=True)
        print(f"Uploaded image to blob: {blob_client.url}")

        return blob_client.url
    except Exception as e:
        print(f"Error uploading image to Blob Storage: {e}")
        return None



def predict_sport_from_tags(tags):
    SPORT_KEYWORDS = {
        "Football": ["soccer", "football", "football player", "soccer ball"],
        "Basketball": ["basketball", "basketball player", "basketball court"],
        "Tennis": ["tennis", "tennis racket", "tennis court"],
        "Swimming": ["swimming", "swimmer", "swimming pool"],
        "Athletics": ["running", "runner", "track", "athletics"],
    }
    scores = {sport: 0.0 for sport in SPORT_KEYWORDS}
    for tag in tags:
        name = tag["name"].lower()
        conf = tag.get("confidence", 0.0)
        for sport, keywords in SPORT_KEYWORDS.items():
            if name in keywords:
                scores[sport] = max(scores[sport], conf)
    best_sport = max(scores, key=scores.get)
    return best_sport, scores[best_sport], scores


@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    prediction = None
    score = None

    # Debug print to Azure Log Stream
    print("Request method:", request.method)

    if not SUBSCRIPTION_KEY or not ANALYZE_URL:
        error = "Computer Vision credentials are not configured correctly on the server."
        return render_template("index.html", prediction=prediction, score=score, error=error)

    if request.method == "POST":
        image_url = request.form.get("image_url")
        print("Received image_url:", image_url)

        try:
            params = {"visualFeatures": "Tags,Description,Objects", "language": "en"}
            headers = {
                "Ocp-Apim-Subscription-Key": SUBSCRIPTION_KEY,
                "Content-Type": "application/json"
            }
            data = {"url": image_url}

            response = requests.post(ANALYZE_URL, headers=headers, params=params, json=data)
            response.raise_for_status()
            result = response.json()

            tags = result.get("tags", [])
            print("Tags from CV:", tags)

            prediction, score, all_scores = predict_sport_from_tags(tags)
            print("Prediction:", prediction, "Score:", score)

            if request.method == "POST":
                image_url = request.form.get("image_url")
                email = request.form.get("email")

                try:
                    # ... call Computer Vision, get tags, prediction, score ...

                    prediction, score, all_scores = predict_sport_from_tags(tags)

                    # 1) Upload the image to Blob and get blob URL
                    blob_url = upload_image_to_blob_from_url(image_url)

                    # 2) Log to Blob log file (optional, you already do this)
                    log_prediction_to_blob(image_url, prediction, score)

                    # 3) Save to SQL with blob URL
                    save_prediction_to_db(image_url, blob_url, email, prediction, score)

                except Exception as e:
                    error = f"Error while calling Computer Vision API or saving to storage: {e}"


        
            log_prediction_to_blob(image_url, prediction, score)
            save_prediction_to_db(image_url, email, prediction, score)


        except Exception as e:
            error = f"Error while calling Computer Vision API: {e}"
            print("Exception:", e)



    return render_template("index.html", prediction=prediction, score=score, error=error)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
