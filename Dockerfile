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

# Pre-download the multilingual turn-detector model into the image.
# This means the container starts instantly without a network fetch on boot.
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download(repo_id='livekit/turn-detector', local_files_only=False)"

# Copy agent source
COPY agent.py .
COPY .env* ./

# LiveKit agents use the 'start' sub-command to connect to the cloud
CMD ["python", "agent.py", "start"]
