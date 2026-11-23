ARG VITROBENCH_DEPS_IMAGE=python:3.11-slim
FROM ${VITROBENCH_DEPS_IMAGE} AS dependencies
USER root
WORKDIR /build/vitrobench
COPY requirements.txt .
RUN python -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: -r requirements.txt && python -m pip check

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8000 VITROBENCH_DATA=/var/lib/vitrobench
WORKDIR /opt/vitrobench
COPY --from=dependencies /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY vitrobench ./vitrobench
COPY static ./static
RUN useradd -U -u 11521 vitrobench && install -d -o vitrobench -g vitrobench /var/lib/vitrobench
USER vitrobench
EXPOSE 8000
CMD ["python", "-m", "vitrobench"]
