FROM python:3.14-slim
WORKDIR /srv
# pg_dump for the nightly backup (same major version as Supabase's Postgres).
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Claude Code: sorts the scratchpad into todos (`claude -p`). Auth: CLAUDE_CODE_OAUTH_TOKEN in .env.
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:$PATH" DISABLE_AUTOUPDATER=1
# Fewer malloc arenas: threaded Python on a 1 GB box otherwise keeps a lot of freed memory.
ENV MALLOC_ARENA_MAX=2
# Face models (OpenCV zoo), checked against the hashes they had when chosen.
RUN mkdir -p /srv/models \
    && curl -fsSL -o /srv/models/yunet.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    && curl -fsSL -o /srv/models/sface.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx \
    && echo "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  /srv/models/yunet.onnx" | sha256sum -c \
    && echo "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79  /srv/models/sface.onnx" | sha256sum -c
ENV FACE_MODEL_DIR=/srv/models
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY migrations migrations
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
