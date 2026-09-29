FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY fuelops ./fuelops
COPY mocksim ./mocksim
RUN useradd -r -u 10001 app && chown -R app /app
USER app
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz',timeout=2).status==200 else 1)"
CMD ["uvicorn", "fuelops.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
