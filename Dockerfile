# Use official Python image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install dependencies + supervisor
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt && pip install supervisor

# Copy project
COPY . .
COPY supervisord.conf /etc/supervisord.conf

# Expose port
EXPOSE 8000

CMD ["supervisord", "-c", "/etc/supervisord.conf"]