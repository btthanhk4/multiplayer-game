FROM python:3.12-slim
WORKDIR /opt/ott
COPY adapter.py /opt/ott/adapter.py
USER 65532:65532
ENTRYPOINT ["python", "-I", "-B", "/opt/ott/adapter.py"]
