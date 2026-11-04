FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV BRN_DB_PATH=/data/pipeline.db
VOLUME /data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
