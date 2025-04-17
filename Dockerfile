
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 👇 Install git for subprocess usage
RUN apt-get update && apt-get install -y git

# Copy the application files
COPY . /app/

# Expose port for the Flask app
EXPOSE 5000

# Set environment variables
ENV FLASK_APP=app/routes.py
ENV FLASK_ENV=development

# Run the Flask app
CMD ["flask", "run", "--host=0.0.0.0"]
