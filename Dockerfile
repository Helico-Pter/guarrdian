# Use an official, lightweight Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the source code and static assets into the container
COPY src/ .

# Create config directory for persistence
# This is where config.yml and reviews.db will live
RUN mkdir -p /app/config && chmod 777 /app/config

# Expose the port your Web UI uses
EXPOSE 9988

# Command to run the application
CMD ["python", "app.py"]
