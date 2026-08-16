# Self-contained image for the hypothesis-highlander node.
# Builds and runs with NO dependency on any sibling LABrador node.
FROM python:3.11-slim

WORKDIR /app

# Install deps first for layer caching. Copy only what the package needs.
COPY pyproject.toml README.md COMPOSE.md ./
COPY highlander/ ./highlander/
COPY tests/ ./tests/

RUN pip install --no-cache-dir . && pip install --no-cache-dir pytest

# Default: run the offline end-to-end demo (no keys, no sibling nodes) — the "it's learning" story.
# For the dashboard instead:  docker run -p 8501:8501 hypothesis-highlander \
#   streamlit run highlander/app.py --server.address 0.0.0.0
CMD ["python", "-m", "highlander.demo"]
