# Build context is the repo root so the shared common/ package can be copied in.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY services/__init__.py ./services/__init__.py
COPY services/chat ./services/chat
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8001/health', timeout=2)" || exit 1
CMD ["uvicorn", "services.chat.main:app", "--host", "0.0.0.0", "--port", "8001"]
