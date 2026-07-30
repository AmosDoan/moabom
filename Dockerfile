FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY static ./static
# Copies seed_positions.example.csv always; your real seed_positions.csv too if present (gitignored)
COPY seed_positions*.csv ./

# data/ is a bind mount (SQLite persists on the host/NAS)
ENV ASSET_DB=/srv/data/asset.db
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
