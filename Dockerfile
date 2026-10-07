FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt requirements-demo.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-demo.txt

COPY . .

EXPOSE 8765
# Railway/Render inyectan PORT; local usa 8765
CMD python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8765}
