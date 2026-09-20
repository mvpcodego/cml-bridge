# Образ без зависимостей: только стандартная библиотека Python.
FROM python:3.12-slim

WORKDIR /app
COPY bridge/ ./bridge/
COPY run.py ./
COPY tests/ ./tests/

# Каталог для частей файлов, которые присылает 1С, и для базы
RUN mkdir -p /data/spool
ENV PYTHONUNBUFFERED=1 \
    CML_DB=/data/cml.db \
    CML_PORT=8021

EXPOSE 8021
CMD ["python3", "run.py", "serve"]
