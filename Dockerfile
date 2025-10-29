FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY sync_service.py .
COPY web_app.py .
COPY VERSION .
COPY templates/ templates/
COPY static/ static/

# Create directories for data and secrets
RUN mkdir -p /app/data /app/secrets

# Expose web interface port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8080/health')"

# Run the web application
CMD ["python", "web_app.py"]
