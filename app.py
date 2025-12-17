from flask import Flask, render_template, request
import requests
import os
from azure.storage.blob import BlobServiceClient
from datetime import datetime
import pyodbc
import uuid
from urllib.parse import urlparse
from azure.core.exceptions import ResourceExistsError
from azure.communication.email import EmailClient
import mimetypes
from azure.storage.blob import ContentSettings



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
            INSERT INTO Predictions (ImageUrl, Email, Sport, Score, BlobUrl)
            VALUES (?, ?, ?, ?, ?)
            """,
            (image_url, email, sport, float(score), blob_url)  
        )
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Saved prediction to SQL DB with BlobUrl.")
    except Exception as e:
        print(f"❌ Error saving prediction to SQL DB: {e}")




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
    if not blob_service_client or not IMAGE_CONTAINER_NAME:
        return None

    try:
        container_client = blob_service_client.get_container_client(IMAGE_CONTAINER_NAME)

        # Create container if it does not exist
        try:
            container_client.create_container()
        except Exception:
            pass  # probably already exists

        # Get image bytes from the URL
        response = requests.get(image_url, stream=True)
        response.raise_for_status()
        image_bytes = response.content

        # Choose a blob name
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        extension = os.path.splitext(image_url.split("?")[0])[1] or ".jpg"
        blob_name = f"image_{timestamp}{extension}"

        blob_client = container_client.get_blob_client(blob_name)

        # Detect content type (from HTTP header or from file extension)
        content_type = (
            response.headers.get("Content-Type")
            or mimetypes.guess_type(image_url)[0]
            or "image/jpeg"
        )

        blob_client.upload_blob(
            image_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

        return blob_client.url

    except Exception as e:
        print(f"Error uploading image to Blob Storage: {e}")
        return None


ACS_CONNECTION_STRING = os.environ.get("ACS_CONNECTION_STRING")
ACS_SENDER_EMAIL = os.environ.get("ACS_SENDER_EMAIL")

email_client = None
if ACS_CONNECTION_STRING:
    try:
        email_client = EmailClient.from_connection_string(ACS_CONNECTION_STRING)
        print("EmailClient initialized successfully.")
    except Exception as e:
        print(f"Error initializing EmailClient: {e}")


def send_prediction_email(to_email, image_url, prediction, score):
    """Send an email with the prediction result using Azure Communication Services."""
    if not email_client:
        print("Email client not configured, skipping email send.")
        return
    if not ACS_SENDER_EMAIL:
        print("No sender email configured, skipping email send.")
        return
    if not to_email:
        print("No recipient email provided, skipping email send.")
        return

    subject = "Your Sport Prediction Result"
    body_text = (
        f"Hello,\n\n"
        f"Here is the result of your image analysis:\n\n"
        f"Image URL: {image_url}\n"
        f"Predicted sport: {prediction}\n"
        f"Confidence score: {score:.2f}\n\n"
        f"Thank you for using the Sport-Type Image Classifier.\n"
                )

    message = {
        "senderAddress": ACS_SENDER_EMAIL,
        "recipients": {
            "to": [
                {"address": to_email}
                                        ]
        },
        "content": {
            "subject": subject,
            "plainText": body_text
        }
    }

    try:
        poller = email_client.begin_send(message)
        result = poller.result()
        print(f"Email send status: {result['status']}")
    except Exception as e:
        print(f"Error sending email: {e}")

def get_predictions_history(limit=50):
    """Return the last `limit` predictions from the SQL table as a list of dicts."""
    records = []

    if not SQL_CONNECTION_STRING:
        print("No SQL connection string configured, cannot load history.")
        return records

    try:
        conn = pyodbc.connect(SQL_CONNECTION_STRING)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT TOP (?) CreatedAt, Email, ImageUrl, BlobUrl, Sport, Score
            FROM Predictions
            ORDER BY CreatedAt DESC
            """,
            (limit,)
        )
        rows = cursor.fetchall()

        for r in rows:
            records.append({
                "created_at": r.CreatedAt,
                "email": r.Email,
                "image_url": r.ImageUrl,
                "blob_url": r.BlobUrl,
                "sport": r.Sport,
                "score": float(r.Score),
            })

        cursor.close()
        conn.close()
    except Exception as e:
        print("Error loading history from SQL:", e)

    return records


def predict_sport_from_tags(tags):
    # Expanded list of sports + keywords that are likely to appear
    # in Azure Computer Vision tags / descriptions.
    SPORT_KEYWORDS = {
        "Football (Soccer)": [
            "soccer", "football", "soccer ball", "football player",
            "soccer player", "goalkeeper", "goal post", "football stadium"
        ],
        "Basketball": [
            "basketball", "basketball player", "basketball court",
            "basketball hoop", "basketball uniform"
        ],
        "Tennis": [
            "tennis", "tennis player", "tennis racket",
            "tennis court", "tennis ball"
        ],
        "Swimming": [
            "swimming", "swimmer", "swimming pool",
            "swimwear", "diving platform"
        ],
        "Athletics / Running": [
            "running", "runner", "track", "athletics",
            "sprinter", "hurdles", "relay race", "marathon"
        ],
        "Volleyball": [
            "volleyball", "volleyball player", "volleyball net",
            "beach volleyball"
        ],
        "Rugby": [
            "rugby", "rugby ball", "rugby player"
        ],
        "Baseball": [
            "baseball", "baseball bat", "baseball glove",
            "baseball player", "baseball field"
        ],
        "Cricket": [
            "cricket", "cricket bat", "cricket player",
            "cricket ball", "wicket"
        ],
        "American Football": [
            "american football", "football helmet",
            "american football player", "football pads", "nfl"
        ],
        "Golf": [
            "golf", "golf course", "golf club", "golfer",
        ],
        "Boxing": [
            "boxing", "boxer", "boxing ring", "boxing gloves"
        ],
        "Martial Arts / MMA": [
            "martial arts", "karate", "judo", "taekwondo",
            "mma", "mixed martial arts", "kickboxing"
        ],
        "Cycling": [
            "cycling", "cyclist", "bicycle race",
            "bike race", "mountain biking", "road cycling"
        ],
        "Ski / Snowboard": [
            "skiing", "skier", "ski slope", "snowboard", "snowboarding"
        ],
        "Surfing": [
            "surfing", "surfer", "surfboard", "wave riding"
        ],
        "Gymnastics": [
            "gymnastics", "gymnast", "balance beam",
            "uneven bars", "pommel horse", "floor exercise"
        ],
        "Ice Hockey": [
            "ice hockey", "hockey stick", "hockey player",
            "hockey puck", "ice rink"
        ],
    }

    # Initialize scores
    scores = {sport: 0.0 for sport in SPORT_KEYWORDS}

    # Go through all returned tags from Computer Vision
    for tag in tags:
        name = tag["name"].lower()
        conf = tag.get("confidence", 0.0)

        for sport, keywords in SPORT_KEYWORDS.items():
            for kw in keywords:
                # Use substring match so "soccer player" matches "soccer", etc.
                if kw in name:
                    scores[sport] = max(scores[sport], conf)

    # Choose the sport with the highest score
    best_sport = max(scores, key=scores.get)
    return best_sport, scores[best_sport], scores



@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    prediction = None
    score = None

    print("Request method:", request.method)

    if not SUBSCRIPTION_KEY or not ANALYZE_URL:
        error = "Computer Vision credentials are not configured correctly on the server."
        return render_template("index.html", prediction=prediction, score=score, error=error, history=None)

    if request.method == "POST":
        image_url = request.form.get("image_url")
        email = request.form.get("email")
        print("Received image_url:", image_url)
        print("Received email:", email)

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

            # 1) Upload the image to Blob and get blob URL
            blob_url = upload_image_to_blob_from_url(image_url)

            # 2) Log to Blob log file
            log_prediction_to_blob(image_url, prediction, score)

            # 3) Save to SQL with blob URL
            save_prediction_to_db(image_url, email, prediction, score, blob_url)


            send_prediction_email(email, image_url, prediction, score)

        except Exception as e:
            error = f"Error while calling Computer Vision API or saving to storage: {e}"
            print("Exception:", e)

    return render_template("index.html", prediction=prediction, score=score, error=error, history=None)



@app.route("/history", methods=["GET"])
def history():
    error = None
    history = []

    try:
        history = get_predictions_history(limit=50)
    except Exception as e:
        error = f"Error loading history from database: {e}"
        print(error)

    # We reuse the same index.html, just without a prediction
    return render_template(
        "index.html",
        prediction=None,
        score=None,
        error=error,
        history=history,
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
