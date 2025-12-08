# Use official Python base image
FROM python:3.13-slim

# Create app directory
WORKDIR /app

# Install system deps (optional but good for pip)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy the rest of the code
COPY . .

# Expose the port Cloud Run will use
ENV PORT=8080

# Command to run the app with Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]
