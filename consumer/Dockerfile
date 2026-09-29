FROM python:3.13-slim
RUN useradd --create-home --uid 10001 ingest
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY log_ingest ./log_ingest
USER ingest
# Mount your config at /config/sources.yaml
ENTRYPOINT ["python", "-m", "log_ingest"]
CMD ["--config", "/config/sources.yaml"]
