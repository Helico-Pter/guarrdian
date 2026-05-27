# Use an official, lightweight Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your Python script and other assets into the container
COPY . .

# Create config directory for persistence
RUN mkdir -p /app/config && chmod 777 /app/config

# Expose the port your Web UI uses
EXPOSE 9988

# Command to run the application
CMD ["python", "app.py"]