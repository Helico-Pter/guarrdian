# guARRdian

Sync Guardian reviews directly to your Radarr and Sonarr libraries.

## 📁 File Management
The application stores all its persistent data in a single `/config` directory. This includes:
- `config/config.yml`: Your API keys and preferences.
- `config/reviews.db`: The local database of fetched reviews.

When deploying with Docker, you only need to mount this one folder to keep your settings safe.

## Features
- **Reviewer Filtering:** Filter reviews by specific Guardian critics (Lucy Mangan, Mark Kermode, Cath Clarke, etc.).
- **Smart Metadata:** Automatically resolves posters and direct links for IMDb, Letterboxd, and TVDb.
- **Library Sync:** One-click addition of movies to Radarr and TV shows to Sonarr.
- **Two-Way Sync:** Automatically detects and marks items already in your library.
- **Pagination:** Smooth browsing with 10 results per page.
- **Sync History:** Track everything you've added to your library in a dedicated history view.
- **Customizable UI:** Hide synced items, toggle Light/Dark/Retro90s modes.

## Setup Instructions

### 1. Prerequisites
- [Docker](https://www.docker.com/) and Docker Compose installed.
- Access to your Radarr and Sonarr instances.

### 2. Installation
1.  Clone this repository to your local machine.
2.  Ensure your critic images are present in the `static/` directory (e.g., `lucy.jpeg`, `mark.jpeg`).
3.  Start the application:
    ```bash
    docker compose up -d --build
    ```
4.  Open your browser and navigate to `http://localhost:9988`.
    - *Note:* The application port can be customized via the `PORT` environment variable (default: `9988`).

### 3. Initial Configuration
Once the UI is loaded, click the **⚙️ Configuration** tab:
- Enter your **Guardian API Key**.
- Enter your **Radarr/Sonarr URLs** and **API Keys**.
    - *Note:* If running in Docker, use your host IP (e.g., `http://192.168.1.50:7878`) rather than `localhost`.
- Configure your **Root Folder Path** and select a **Quality Profile**.

## General Usage

### Review Picker
- **Filtering:** Click on a reviewer's avatar at the top to see only their reviews.
- **Star Rating:** The picker defaults to showing **3+ star reviews**. You can click the stars in the header to adjust this filter.
- **Syncing:** Check the box next to the titles you want and click **"Send Selected to *arr"**.
- **Sync Status:** Once an item is added, its checkbox is replaced by a green checkmark, and it is labeled **"IN LIBRARY"**. It remains visible in the picker for easy reference.
- **External Links:** Hover over the links column to see brand icons for the Guardian article, IMDb page, and Letterboxd/TVDb.

### Automation
In the **Configuration** tab, you can enable **Auto-Sync**. When enabled, any new review discovered with a **5-star rating** will be added to your library automatically without requiring manual approval.

---
*Created with guARRdian Sync.*
