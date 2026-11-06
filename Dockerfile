FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    SFG_CONFIG=/config/config.yaml

# tzdata for local-time axis labels; fonts for matplotlib
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import eccodes; print('ecCodes', eccodes.codes_get_api_version())"

COPY skiforecast ./skiforecast

RUN mkdir -p /data /config

ENTRYPOINT ["python", "-m", "skiforecast"]
CMD ["run"]
