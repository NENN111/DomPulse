FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
RUN useradd --uid 10001 --create-home appuser && mkdir /data && chown appuser /data
USER appuser
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
