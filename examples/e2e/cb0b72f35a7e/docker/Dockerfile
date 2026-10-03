FROM python@sha256:f2cdc43fcddbabe870f53750cbdcc01ae4aa75b1959351252457fde88f91d20f
WORKDIR /app
COPY missing.py app.py
RUN python -m py_compile app.py
USER 10001
EXPOSE 8080
CMD ["python", "app.py"]
