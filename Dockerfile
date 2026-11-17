FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py storage.py family_features.py family_storage.py family_rewards.py ./
COPY assets ./assets
RUN mkdir -p /app/data
CMD ["python", "bot.py"]
