# guARRdian 🛡️

![main-program](https://i.ibb.co/TB5XVHgM/firefox-k-N4-Vjp-Ohv-V.png)

Sync Guardian reviews directly to your Radarr and Sonarr libraries. guARRdian acts as a bridge between the Guardian's expert film and TV critics and your automated media collection.

## 🚀 Quick Start with Docker

The easiest way to run guARRdian is using Docker.

### 1. Create a `docker-compose.yml`
```yaml
services:
  guarrdian:
    image: ghcr.io/helico-pter/guarrdian:latest
    container_name: guarrdian
    restart: unless-stopped
    ports:
      - "9988:9988"
    volumes:
      - ./config:/app/config
```

### 2. Launch the Application
```bash
docker compose up -d
```

### 3. Configure
Navigate to `http://YOUR_IP:9988` and go to the **⚙️ Configuration** tab to set up your Guardian API key and *arr instances.

---

## 📂 Project Structure

guARRdian has been designed with a clean, standard layout to ensure ease of deployment and maintenance.

- **`/src`**: Contains the core application logic and static assets (baked into the Docker image).
- **`/config`**: The only persistent directory you need to manage. It contains:
  - `config.yml`: Your settings and API keys.
  - `reviews.db`: The local cache of reviews and sync status.

## ✨ Features

- **Expert Curation:** Filter reviews by specific Guardian critics (Lucy Mangan, Peter Bradshaw, Mark Kermode, etc.).
- **Rich Metadata:** Automatically resolves posters and direct links for IMDb, Letterboxd, and TVDb using your *arr libraries.
- **Smart Library Sync:** One-click addition to Radarr/Sonarr with automatic detection of existing content.
- **Automation:** Optional "Auto-Sync" for highly-rated (5-star) reviews.
- **Modern UI:** Responsive design with Light, Dark, and Retro 90s themes.
- **Privacy First:** No external telemetry; everything is stored locally in your `config` folder.

## 🛠️ Development & Building

If you wish to build the image locally:

```bash
docker build -t ghcr.io/helico-pter/guarrdian:latest .
```

To run locally without Docker:

```bash
cd src
pip install -r requirements.txt
python app.py
```

---
*Created with guARRdian Sync.*
