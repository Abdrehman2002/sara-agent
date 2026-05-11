# Sara — Daewoo Express voice agent
# Build:   docker build -t sara-agent .
# Run:     docker run --env-file .env sara-agent

FROM python:3.11-slim

# System deps: libsndfile for audio, ffmpeg for codec support
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer caches unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy agent source first so download-files can run
COPY agent.py .
COPY .env* ./

# Pre-download turn-detector model files into the image layer
# so the container never needs to fetch them at runtime
RUN python agent.py download-files

# LiveKit agents use the 'start' sub-command to connect to the cloud
CMD ["python", "agent.py", "start"]
