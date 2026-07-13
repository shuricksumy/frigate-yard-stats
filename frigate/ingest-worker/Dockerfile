FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt --break-system-packages
COPY . .
EXPOSE 8080
CMD ["python", "main.py"]
