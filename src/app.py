import requests
import yaml
import argparse
import re
import urllib.parse
import time
import sqlite3
import os
import threading
from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory

# ==========================================
# 0. CONFIGURATION & CONSTANTS
# ==========================================
CONFIG_DIR = 'config'
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.yml')
DB_FILE = os.path.join(CONFIG_DIR, 'reviews.db')

# Ensure config directory exists
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR)

# --- AUTO-MIGRATION: Move old files to new /config folder if found ---
for old_file, new_path in [('config.yml', CONFIG_FILE), ('reviews.db', DB_FILE)]:
    if os.path.exists(old_file) and not os.path.exists(new_path):
        try:
            print(f"MIGRATION: Moving {old_file} to {new_path}")
            os.rename(old_file, new_path)
        except Exception as e:
            print(f"MIGRATION ERROR: Could not move {old_file}: {e}")

REVIEWERS = {
    "profile/lucymangan": {
        "name": "Lucy Mangan",
        "img": "lucy.jpeg"
    },
    "profile/peterbradshaw": {
        "name": "Peter Bradshaw",
        "img": "peter.jpeg"
    },
    "profile/mark-kermode": {
        "name": "Mark Kermode",
        "img": "mark.jpeg"
    },
    "profile/rebeccanicholson": {
        "name": "Rebecca Nicholson",
        "img": "rebecca.jpeg"
    },
    "profile/stuartheritage": {
        "name": "Stuart Heritage",
        "img": "stewart.jpeg"
    },
    "profile/jack-seale": {
        "name": "Jack Seale",
        "img": "jack.jpeg"
    },
    "profile/luke-buckmaster": {
        "name": "Luke Buckmaster",
        "img": "luke.jpeg"
    },
    "profile/cathclarke": {
        "name": "Cath Clarke",
        "img": "cath.jpeg"
    }
}


DEFAULT_CONFIG = {
    "guardian": {"api_key": "", "fetch_limit": 30},
    "radarr": {"url": "http://localhost:7878", "api_key": "", "quality_profile_id": 1, "root_folder_path": "/movies", "search_on_add": True},
    "sonarr": {"url": "http://localhost:8989", "api_key": "", "quality_profile_id": 1, "root_folder_path": "/tv", "search_on_add": True},
    "ui": {"theme": "dark", "hide_synced": False},
    "auto_sync": {"enabled": False, "min_stars": 5}
}

LAST_FETCH_TIME = 0
FETCH_COOLDOWN_SECONDS = 900 # 15 minutes

def load_config(config_path=CONFIG_FILE):
    if not os.path.exists(config_path):
        save_config(DEFAULT_CONFIG, config_path)
        return DEFAULT_CONFIG
    with open(config_path, 'r') as f:
        user_config = yaml.safe_load(f) or {}
        merged = DEFAULT_CONFIG.copy()
        for section in merged:
            if section in user_config:
                if isinstance(merged[section], dict):
                    merged[section].update(user_config[section])
                else:
                    merged[section] = user_config[section]
        return merged

def save_config(config_data, config_path=CONFIG_FILE):
    with open(config_path, 'w') as f:
        yaml.safe_dump(config_data, f, default_flow_style=False)

def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reviews (
            guardian_url TEXT PRIMARY KEY,
            title TEXT,
            type TEXT,
            rating INTEGER,
            reviewer TEXT,
            date TEXT,
            imdb_url TEXT,
            review_text TEXT,
            status TEXT DEFAULT 'new'
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reviewers (
            tag TEXT PRIMARY KEY,
            name TEXT,
            image_url TEXT,
            review_count INTEGER DEFAULT 0,
            last_review_date TEXT
        )
    ''')
    
    cursor.execute("PRAGMA table_info(reviews)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'reviewer_image' not in columns:
        cursor.execute("ALTER TABLE reviews ADD COLUMN reviewer_image TEXT")
    if 'reviewer_tag' not in columns:
        cursor.execute("ALTER TABLE reviews ADD COLUMN reviewer_tag TEXT")
    if 'letterboxd_url' not in columns:
        cursor.execute("ALTER TABLE reviews ADD COLUMN letterboxd_url TEXT")
    if 'tvdb_url' not in columns:
        cursor.execute("ALTER TABLE reviews ADD COLUMN tvdb_url TEXT")
    if 'poster_url' not in columns:
        cursor.execute("ALTER TABLE reviews ADD COLUMN poster_url TEXT")
    if 'metadata_attempts' not in columns:
        cursor.execute("ALTER TABLE reviews ADD COLUMN metadata_attempts INTEGER DEFAULT 0")
        
    # --- NEW FIX: Backfill older entries with missing tags so filtering works ---
    for tag, name in REVIEWERS.items():
        cursor.execute('''
            UPDATE reviews 
            SET reviewer_tag = ? 
            WHERE reviewer_tag IS NULL AND reviewer LIKE ?
        ''', (tag, f"%{name}%"))
        
    # --- NEW FIX: Migrate existing reviewers to the new table ---
    cursor.execute('''
        INSERT OR REPLACE INTO reviewers (tag, name, image_url, review_count, last_review_date)
        SELECT 
            reviewer_tag, 
            MIN(reviewer), 
            MIN(reviewer_image), 
            COUNT(*), 
            MAX(date)
        FROM reviews 
        WHERE reviewer_tag IS NOT NULL
        GROUP BY reviewer_tag
    ''')
    
    # --- NEW FIX: Backfill missing reviewer images with local versions ---
    for tag, data in REVIEWERS.items():
        cursor.execute('''
            UPDATE reviews 
            SET reviewer_image = ?, reviewer = ?
            WHERE (reviewer_image IS NULL OR reviewer_image LIKE '%random%' OR reviewer_image LIKE '/static/%') AND reviewer_tag = ?
        ''', (data["img"], data["name"], tag))
        
        # Also fix the reviewers table names
        cursor.execute("UPDATE reviewers SET name = ?, image_url = ? WHERE tag = ?", (data['name'], data['img'], tag))
        
    # --- NEW FIX: Backfill missing external links (IMDb, Letterboxd, TVDb) ---
    cursor.execute("SELECT guardian_url, title, type FROM reviews WHERE (letterboxd_url IS NULL AND type = 'movie') OR (tvdb_url IS NULL AND type = 'tv')")
    to_fix = cursor.fetchall()
    if to_fix:
        config = load_config()
        for g_url, title, m_type in to_fix:
            ext = resolve_external_links(title, m_type, config)
            # Only update if we found something new or to improve IMDb link
            cursor.execute('''
                UPDATE reviews 
                SET imdb_url = ?, letterboxd_url = ?, tvdb_url = ?
                WHERE guardian_url = ?
            ''', (ext['imdb'], ext['letterboxd'], ext['tvdb'], g_url))
    # --------------------------------------------------------------------------
        
    conn.commit()
    conn.close()

def backfill_missing_metadata(config, limit=20):
    """Gradually backfills missing posters and links, prioritizing newest items."""
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cursor = conn.cursor()
    # Find items missing metadata that haven't been tried too many times
    cursor.execute('''
        SELECT guardian_url, title, type 
        FROM reviews 
        WHERE ((letterboxd_url IS NULL AND type = 'movie') 
           OR (tvdb_url IS NULL AND type = 'tv') 
           OR (poster_url IS NULL))
           AND metadata_attempts < 3
        ORDER BY date DESC
        LIMIT ?
    ''', (limit,))
    
    to_fix = cursor.fetchall()
    if not to_fix:
        conn.close()
        return 0
        
    print(f"DEBUG: Starting background backfill for {len(to_fix)} items...")
    fixed_count = 0
    for g_url, title, m_type in to_fix:
        try:
            ext = resolve_external_links(title, m_type, config)
            # Check if we found anything new
            has_new = ext['poster'] or (m_type == 'movie' and ext['letterboxd']) or (m_type == 'tv' and ext['tvdb'])
            
            cursor.execute('''
                UPDATE reviews 
                SET imdb_url = ?, letterboxd_url = ?, tvdb_url = ?, poster_url = ?, metadata_attempts = metadata_attempts + 1
                WHERE guardian_url = ?
            ''', (ext['imdb'], ext['letterboxd'], ext['tvdb'], ext['poster'], g_url))
            
            if has_new:
                fixed_count += 1
                
            # Commit every 5 items to keep transactions short
            if (to_fix.index((g_url, title, m_type)) + 1) % 5 == 0:
                conn.commit()
        except Exception as e:
            print(f"Error backfilling {title}: {e}")
        
    conn.commit()
    conn.close()
    print(f"DEBUG: Backfill complete. Added metadata for {fixed_count} items.")
    return fixed_count

def resolve_external_links(title, media_type, config):
    """Attempts to find direct IDs for IMDb, Letterboxd, and TVDb using Radarr/Sonarr."""
    # Clean title: remove "review", "– ...", and trailing punctuation
    clean_title = re.sub(r'\s+review.*', '', title, flags=re.IGNORECASE)
    clean_title = clean_title.split(' – ')[0].split(' - ')[0].strip()
    
    print(f"DEBUG: Resolving links for {media_type.upper()}: '{clean_title}' (Original: '{title}')")
    
    links = {
        "imdb": f"https://www.imdb.com/find?q={urllib.parse.quote(clean_title)}",
        "letterboxd": None,
        "tvdb": None,
        "poster": None
    }
    
    if media_type == 'movie' and config['radarr']['api_key'] and config['radarr']['url']:
        headers = {"X-Api-Key": config['radarr']['api_key']}
        base_url = config['radarr']['url'].rstrip('/')
        try:
            res = requests.get(f"{base_url}/api/v3/movie/lookup?term={urllib.parse.quote(clean_title)}", headers=headers, timeout=5)
            if res.status_code == 200:
                movies = res.json()
                if movies:
                    movie = movies[0]
                    imdb_id = movie.get('imdbId')
                    tmdb_id = movie.get('tmdbId')
                    if imdb_id:
                        links["imdb"] = f"https://www.imdb.com/title/{imdb_id}/"
                    if tmdb_id:
                        links["letterboxd"] = f"https://letterboxd.com/tmdb/{tmdb_id}/"
                    
                    # Extract poster URL
                    images = movie.get('images', [])
                    for img in images:
                        if img.get('coverType') == 'poster':
                            poster_url = img.get('remoteUrl') or img.get('url')
                            if poster_url and poster_url.startswith('/'):
                                poster_url = f"{base_url}{poster_url}"
                            links["poster"] = poster_url
                            break
                    print(f"  -> Found Movie metadata (Poster: {'Yes' if links['poster'] else 'No'})")
            else:
                print(f"  -> Radarr lookup failed: HTTP {res.status_code}")
        except Exception as e: 
            print(f"  -> Radarr lookup error: {e}")
            
    elif media_type == 'tv' and config['sonarr']['api_key'] and config['sonarr']['url']:
        headers = {"X-Api-Key": config['sonarr']['api_key']}
        base_url = config['sonarr']['url'].rstrip('/')
        try:
            res = requests.get(f"{base_url}/api/v3/series/lookup?term={urllib.parse.quote(clean_title)}", headers=headers, timeout=5)
            if res.status_code == 200:
                series = res.json()
                if series:
                    s = series[0]
                    imdb_id = s.get('imdbId')
                    tvdb_id = s.get('tvdbId')
                    if imdb_id:
                        links["imdb"] = f"https://www.imdb.com/title/{imdb_id}/"
                    if tvdb_id:
                        links["tvdb"] = f"https://www.thetvdb.com/?tab=series&id={tvdb_id}"
                    
                    # Extract poster URL
                    images = s.get('images', [])
                    for img in images:
                        if img.get('coverType') == 'poster':
                            poster_url = img.get('remoteUrl') or img.get('url')
                            if poster_url and poster_url.startswith('/'):
                                poster_url = f"{base_url}{poster_url}"
                            links["poster"] = poster_url
                            break
                    print(f"  -> Found TV metadata (Poster: {'Yes' if links['poster'] else 'No'})")
            else:
                print(f"  -> Sonarr lookup failed: HTTP {res.status_code}")
        except Exception as e: 
            print(f"  -> Sonarr lookup error: {e}")
            
    return links

def process_auto_sync(config):
    """Automatically syncs 'new' reviews that meet the auto-sync criteria."""
    if not config.get('auto_sync', {}).get('enabled'):
        return 0
        
    min_stars = config['auto_sync'].get('min_stars', 5)
    # Fetch all 'new' reviews that meet the star rating
    data = get_cached_reviews(min_stars, 'all', 'all', hide_synced=True, per_page=100)
    pending = data.get("reviews", [])
    
    synced_count = 0
    for r in pending:
        if r['type'] == 'movie':
            success, _ = add_to_radarr(r['title'], config)
        else:
            success, _ = add_to_sonarr(r['title'], config)
            
        if success:
            mark_as_synced(r['guardian_url'])
            synced_count += 1
    return synced_count

def fetch_and_cache_reviews(config, selected_reviewer='all'):
    api_key = config['guardian']['api_key']
    fetch_limit = config['guardian'].get('fetch_limit', 30)
    url = "https://content.guardianapis.com/search"
    
    if not api_key:
        return 0 
        
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cursor = conn.cursor()
    new_count = 0
    
    # We fetch globally from the film and tv-and-radio sections
    # This automatically discovers all contributors
    params = {
        "api-key": api_key,
        "show-fields": "starRating,headline,byline,bodyText,bylineImageUrl",
        "show-tags": "contributor",
        "page-size": 200, # Large fetch to discover all active contributors
        "section": "tv-and-radio|film",
        "star-rating": "1|2|3|4|5" # Ensure we only get rated reviews
    }
    
    # If a specific reviewer is requested, filter by their tag
    if selected_reviewer != 'all':
        params["tag"] = selected_reviewer

    try:
        response = requests.get(url, params=params).json()
        if "response" not in response or "results" not in response["response"]:
            return 0
            
        for article in response["response"]["results"]:
            fields = article.get("fields", {})
            tags = article.get("tags", [])
            
            # Find the contributor tag
            contributor = next((t for t in tags if t['type'] == 'contributor'), None)
            if not contributor: continue
            
            tag = contributor['id']
            # Prioritize our name, then API name
            reviewer_name = REVIEWERS.get(tag, {}).get("name") or contributor['webTitle']
            
            # Rating and title logic
            raw_rating = fields.get("starRating")
            rating_val = int(raw_rating) if raw_rating else 0
            
            headline = fields.get("headline", "")
            match = re.search(r"^(.*?)(?:\s+review\b)", headline, re.IGNORECASE)
            title = match.group(1).strip() if match else headline.split(' – ')[0].split(' - ')[0].strip()
            
            media_type = "movie" if article.get("sectionId") == "film" else "tv"
            raw_date = article.get("webPublicationDate", "")
            pub_date = raw_date[:10] if raw_date else "1970-01-01"
            
            # Reviewer image logic: check REVIEWERS for local, then API
            reviewer_image = REVIEWERS.get(tag, {}).get("img")
            if not reviewer_image:
                reviewer_image = contributor.get("bylineImageUrl") or fields.get("bylineImageUrl")
            if not reviewer_image:
                reviewer_image = f"https://ui-avatars.com/api/?name={urllib.parse.quote(reviewer_name)}&background=0d6efd&color=fff"
            
            # 1. Upsert Reviewer (Basic data, counts updated at end)
            cursor.execute('''
                INSERT INTO reviewers (tag, name, image_url, review_count, last_review_date)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(tag) DO UPDATE SET
                last_review_date = MAX(last_review_date, EXCLUDED.last_review_date),
                image_url = COALESCE(NULLIF(image_url, ''), EXCLUDED.image_url)
            ''', (tag, reviewer_name, reviewer_image, pub_date))
            
            # 2. Insert Review (Basic data only, metadata filled by background thread)
            imdb_link = f"https://www.imdb.com/find?q={urllib.parse.quote(title)}"
            
            guardian_url = article.get("webUrl", "#")
            review_text = fields.get("bodyText", "No review text available.")[:1500] + "..."
            
            cursor.execute("SELECT guardian_url FROM reviews WHERE guardian_url=?", (guardian_url,))
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO reviews 
                    (guardian_url, title, type, rating, reviewer, date, imdb_url, letterboxd_url, tvdb_url, poster_url, review_text, status, reviewer_image, reviewer_tag)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, 'new', ?, ?)
                ''', (guardian_url, title, media_type, rating_val, reviewer_name, pub_date, imdb_link, review_text, reviewer_image, tag))
                new_count += 1
                
        # 3. Update all review counts to be accurate based on database
        cursor.execute('''
            UPDATE reviewers SET review_count = (
                SELECT COUNT(*) FROM reviews WHERE reviewer_tag = reviewers.tag
            )
        ''')
                
    except Exception as e:
        print(f"Error fetching reviews: {e}")
        
    conn.commit()
    conn.close()
    
    if new_count > 0:
        process_auto_sync(config)
        # Immediately trigger metadata backfill for the new items
        threading.Thread(target=backfill_missing_metadata, args=(config, 20)).start()
    
    # Refresh library status
    sync_existing_library(config)
        
    return new_count

def get_cached_reviews(min_stars, reviewer_tag='all', media_type='all', hide_synced=False, page=1, per_page=10):
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT * FROM reviews WHERE rating >= ?"
    params = [min_stars]
    
    if hide_synced:
        query += " AND status = 'new'"
    
    if reviewer_tag and reviewer_tag != 'all':
        # Prioritize tag matching. Fallback to name only if we have a hardcoded name.
        name = REVIEWERS.get(reviewer_tag, {}).get("name", "")
        if name:
            query += " AND (reviewer_tag = ? OR reviewer LIKE ?)"
            params.extend([reviewer_tag, f"%{name}%"])
        else:
            query += " AND reviewer_tag = ?"
            params.append(reviewer_tag)
        
    if media_type and media_type != 'all':
        query += " AND type = ?"
        params.append(media_type)
        
    # Count total for pagination
    count_query = f"SELECT COUNT(*) FROM ({query})"
    cursor.execute(count_query, params)
    total_count = cursor.fetchone()[0]
    
    # Add pagination
    query += " ORDER BY date DESC LIMIT ? OFFSET ?"
    params.extend([per_page, (page - 1) * per_page])
    
    cursor.execute(query, params)
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"reviews": results, "total": total_count}

def get_ui_reviewers():
    """Returns Featured reviewers (Lucy, Peter + Top 5) and alphabetical list of others."""
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Pre-populate reviewers table if empty using our hardcoded list
    cursor.execute("SELECT COUNT(*) FROM reviewers")
    if cursor.fetchone()[0] == 0:
        for tag, data in REVIEWERS.items():
            cursor.execute("INSERT OR IGNORE INTO reviewers (tag, name, image_url) VALUES (?, ?, ?)", 
                         (tag, data['name'], data['img']))
        conn.commit()

    permanent_tags = ['profile/lucymangan', 'profile/peterbradshaw']
    
    # 1. Get the permanent members
    cursor.execute(f"SELECT * FROM reviewers WHERE tag IN ({','.join(['?']*len(permanent_tags))})", permanent_tags)
    permanent = {row['tag']: dict(row) for row in cursor.fetchall()}
    
    # Order them correctly: Lucy first, then Peter
    top_list = []
    for tag in permanent_tags:
        if tag in permanent:
            top_list.append(permanent[tag])
    
    # 2. Get the next top 5 (excluding permanent members)
    cursor.execute(f"SELECT * FROM reviewers WHERE tag NOT IN ({','.join(['?']*len(permanent_tags))}) ORDER BY review_count DESC, last_review_date DESC LIMIT 5", permanent_tags)
    top_list.extend([dict(row) for row in cursor.fetchall()])
    
    # 3. Get all others alphabetically
    all_featured_tags = [r['tag'] for r in top_list]
    cursor.execute(f"SELECT * FROM reviewers WHERE tag NOT IN ({','.join(['?']*len(all_featured_tags))}) ORDER BY name ASC", all_featured_tags)
    others = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return {"top": top_list, "others": others}

def mark_as_synced(guardian_url):
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cursor = conn.cursor()
    cursor.execute("UPDATE reviews SET status = 'synced' WHERE guardian_url = ?", (guardian_url,))
    conn.commit()
    conn.close()

def get_history(reviewer_tag='all', media_type='all', page=1, per_page=10):
    """Retrieves all reviews that have been successfully synced, with optional filtering."""
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT * FROM reviews WHERE status = 'synced'"
    params = []
    
    if reviewer_tag and reviewer_tag != 'all':
        # Prioritize tag matching. Fallback to name only if we have a hardcoded name.
        name = REVIEWERS.get(reviewer_tag, {}).get("name", "")
        if name:
            query += " AND (reviewer_tag = ? OR reviewer LIKE ?)"
            params.extend([reviewer_tag, f"%{name}%"])
        else:
            query += " AND reviewer_tag = ?"
            params.append(reviewer_tag)
        
    if media_type and media_type != 'all':
        query += " AND type = ?"
        params.append(media_type)
        
    # Count total for pagination
    count_query = f"SELECT COUNT(*) FROM ({query})"
    cursor.execute(count_query, params)
    total_count = cursor.fetchone()[0]
    
    # Add pagination
    query += " ORDER BY date DESC LIMIT ? OFFSET ?"
    params.extend([per_page, (page - 1) * per_page])
    
    cursor.execute(query, params)
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"reviews": results, "total": total_count}

def sync_existing_library(config):
    """Refined two-way sync: marks owned items as 'synced' and missing items as 'new'."""
    radarr_imdb, radarr_tmdb = set(), set()
    sonarr_imdb, sonarr_tvdb = set(), set()
    
    # 1. Fetch Radarr Library
    if config['radarr']['api_key'] and config['radarr']['url']:
        headers = {"X-Api-Key": config['radarr']['api_key']}
        base_url = config['radarr']['url'].rstrip('/')
        try:
            res = requests.get(f"{base_url}/api/v3/movie", headers=headers, timeout=15)
            if res.status_code == 200:
                for movie in res.json():
                    if movie.get('imdbId'): radarr_imdb.add(movie['imdbId'])
                    if movie.get('tmdbId'): radarr_tmdb.add(movie['tmdbId'])
            else: print(f"Radarr sync failed: HTTP {res.status_code}")
        except Exception as e: print(f"Radarr connection error during sync: {e}")

    # 2. Fetch Sonarr Library
    if config['sonarr']['api_key'] and config['sonarr']['url']:
        headers = {"X-Api-Key": config['sonarr']['api_key']}
        base_url = config['sonarr']['url'].rstrip('/')
        try:
            res = requests.get(f"{base_url}/api/v3/series", headers=headers, timeout=15)
            if res.status_code == 200:
                for s in res.json():
                    if s.get('imdbId'): sonarr_imdb.add(s['imdbId'])
                    if s.get('tvdbId'): sonarr_tvdb.add(s['tvdbId'])
            else: print(f"Sonarr sync failed: HTTP {res.status_code}")
        except Exception as e: print(f"Sonarr connection error during sync: {e}")

    # 3. Process Database
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cursor = conn.cursor()
    cursor.execute("SELECT guardian_url, type, imdb_url, letterboxd_url, tvdb_url, status FROM reviews")
    rows = cursor.fetchall()
    
    updates = []
    for g_url, m_type, imdb_url, lb_url, tvdb_url, current_status in rows:
        in_library = False
        
        # Extract IDs
        imdb_match = re.search(r'tt\d+', imdb_url) if imdb_url else None
        imdb_id = imdb_match.group(0) if imdb_match else None
        
        if m_type == 'movie':
            tmdb_match = re.search(r'tmdb/(\d+)', lb_url) if lb_url else None
            tmdb_id = int(tmdb_match.group(1)) if tmdb_match else None
            if (imdb_id and imdb_id in radarr_imdb) or (tmdb_id and tmdb_id in radarr_tmdb):
                in_library = True
        else:
            tvdb_match = re.search(r'id=(\d+)', tvdb_url) if tvdb_url else None
            tvdb_id = int(tvdb_match.group(1)) if tvdb_match else None
            if (imdb_id and imdb_id in sonarr_imdb) or (tvdb_id and tvdb_id in sonarr_tvdb):
                in_library = True
        
        new_status = 'synced' if in_library else 'new'
        if new_status != current_status:
            updates.append((new_status, g_url))
            
    if updates:
        cursor.executemany("UPDATE reviews SET status = ? WHERE guardian_url = ?", updates)
        conn.commit()
    conn.close()
    print(f"Library sync complete. Updated {len(updates)} items.")

def get_radarr_profiles(config):
    """Fetches quality profiles from Radarr."""
    if not (config['radarr']['api_key'] and config['radarr']['url']): return []
    headers = {"X-Api-Key": config['radarr']['api_key']}
    base_url = config['radarr']['url'].rstrip('/')
    try:
        res = requests.get(f"{base_url}/api/v3/qualityprofile", headers=headers, timeout=5)
        if res.status_code == 200:
            return [{"id": p['id'], "name": p['name']} for p in res.json()]
    except: pass
    return []

def get_sonarr_profiles(config):
    """Fetches quality profiles from Sonarr."""
    if not (config['sonarr']['api_key'] and config['sonarr']['url']): return []
    headers = {"X-Api-Key": config['sonarr']['api_key']}
    base_url = config['sonarr']['url'].rstrip('/')
    try:
        res = requests.get(f"{base_url}/api/v3/qualityprofile", headers=headers, timeout=5)
        if res.status_code == 200:
            return [{"id": p['id'], "name": p['name']} for p in res.json()]
    except: pass
    return []

def parse_arr_error(res):
    try:
        data = res.json()
        if isinstance(data, list) and len(data) > 0:
            err = data[0].get('errorMessage', '').lower()
        else:
            err = str(data).lower()
            
        if "exist" in err:
            return True, "Already in library"
        return False, f"Failed: {data[0].get('errorMessage', 'Unknown error') if isinstance(data, list) else err}"
    except:
        return False, f"Failed: HTTP {res.status_code}"

def add_to_radarr(title, config):
    headers = {"X-Api-Key": config['radarr']['api_key']}
    base_url = config['radarr']['url'].rstrip('/')
    # Clean title before lookup
    clean_title = re.sub(r'\s+review.*', '', title, flags=re.IGNORECASE)
    clean_title = clean_title.split(' – ')[0].split(' - ')[0].strip()
    
    lookup_url = f"{base_url}/api/v3/movie/lookup?term={urllib.parse.quote(clean_title)}"
    try:
        lookup_res = requests.get(lookup_url, headers=headers).json()
        if not lookup_res: return False, "Not found in TMDB"
        movie = lookup_res[0]
        payload = {
            "title": movie['title'], "tmdbId": movie['tmdbId'],
            "qualityProfileId": int(config['radarr']['quality_profile_id']),
            "rootFolderPath": config['radarr']['root_folder_path'],
            "monitored": True, "addOptions": {"searchForMovie": config['radarr'].get('search_on_add', True)}
        }
        res = requests.post(f"{base_url}/api/v3/movie", json=payload, headers=headers)
        if res.status_code == 201: return True, "Added successfully"
        elif res.status_code == 400: return parse_arr_error(res)
        return False, f"Failed: HTTP {res.status_code}"
    except Exception as e: return False, f"Error: {str(e)}"

def add_to_sonarr(title, config):
    headers = {"X-Api-Key": config['sonarr']['api_key']}
    base_url = config['sonarr']['url'].rstrip('/')
    # Clean title before lookup
    clean_title = re.sub(r'\s+review.*', '', title, flags=re.IGNORECASE)
    clean_title = clean_title.split(' – ')[0].split(' - ')[0].strip()
    
    lookup_url = f"{base_url}/api/v3/series/lookup?term={urllib.parse.quote(clean_title)}"
    try:
        lookup_res = requests.get(lookup_url, headers=headers).json()
        if not lookup_res: return False, "Not found in TVDB"
        series = lookup_res[0]
        payload = {
            "title": series['title'], "tvdbId": series['tvdbId'],
            "qualityProfileId": int(config['sonarr']['quality_profile_id']),
            "rootFolderPath": config['sonarr']['root_folder_path'],
            "monitored": True, "languageProfileId": 1, 
            "addOptions": {"searchForMissingEpisodes": config['sonarr'].get('search_on_add', True)}
        }
        res = requests.post(f"{base_url}/api/v3/series", json=payload, headers=headers)
        if res.status_code == 201: return True, "Added successfully"
        elif res.status_code == 400: return parse_arr_error(res)
        return False, f"Failed: HTTP {res.status_code}"
    except Exception as e: return False, f"Error: {str(e)}"

init_db()

# ==========================================
# 2. WEB UI (FLASK)
# ==========================================

# Use absolute path for static files to ensure reliability across environments
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'))
# Optimise static file delivery: Cache local images for 1 year in the browser
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000 

HTML_TEMPLATE = """
<!DOCTYPE html>
<html data-theme="{{ config.ui.theme }}">
<head>
    <title>guARRdian Sync</title>
    <!-- Favicon Assets -->
    <link rel="apple-touch-icon" sizes="180x180" href="{{ url_for('apple_touch_icon') }}">
    <link rel="icon" type="image/png" sizes="32x32" href="{{ url_for('static', filename='favicons/favicon-32x32.png') }}">
    <link rel="icon" type="image/png" sizes="16x16" href="{{ url_for('static', filename='favicons/favicon-16x16.png') }}">
    <link rel="manifest" href="{{ url_for('static', filename='favicons/site.webmanifest') }}">
    <link rel="icon" type="image/x-icon" href="{{ url_for('favicon') }}">
    
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root[data-theme="dark"] {
            --bg-color: #121212; --card-bg: #1e1e1e; --text-color: #e0e0e0;
            --border-color: #333; --th-bg: #2c2c2c; --btn-bg: #0d6efd;
            --btn-hover: #0b5ed7; --btn-sync: #198754; --btn-sync-hover: #157347;
            --link-color: #58a6ff; --star-active: #ffc107; --star-inactive: #555;
            --brand-accent: #00e054;
        }
        :root[data-theme="light"] {
            --bg-color: #f4f4f9; --card-bg: #ffffff; --text-color: #333333;
            --border-color: #dddddd; --th-bg: #f8f9fa; --btn-bg: #0d6efd;
            --btn-hover: #0b5ed7; --btn-sync: #28a745; --btn-sync-hover: #218838;
            --link-color: #0056b3; --star-active: #ffc107; --star-inactive: #ccc;
            --brand-accent: #198754;
        }
        :root[data-theme="retro90s"] {
            --bg-color: #008080; --card-bg: #c0c0c0; --text-color: #000000;
            --border-color: #808080; --th-bg: #c0c0c0; --btn-bg: #c0c0c0;
            --btn-hover: #dfdfdf; --btn-sync: #c0c0c0; --btn-sync-hover: #dfdfdf;
            --link-color: #0000ee; --star-active: #ff0000; --star-inactive: #808080;
            --brand-accent: #ff0000;
        }
        body { font-family: 'Segoe UI', Tahoma, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: var(--bg-color); color: var(--text-color); transition: 0.3s; display: flex; flex-direction: column; min-height: 100vh; }
        
        [data-theme="retro90s"] body { font-family: "Times New Roman", Times, serif; }
        [data-theme="retro90s"] .card, [data-theme="retro90s"] .nav-bar, [data-theme="retro90s"] .settings-section, [data-theme="retro90s"] .controls-header { border: 3px outset #fff !important; box-shadow: 2px 2px 0 #000 !important; border-radius: 0 !important; }
        [data-theme="retro90s"] button, [data-theme="retro90s"] .nav-link, [data-theme="retro90s"] .filter-btn, [data-theme="retro90s"] .other-reviewers-select { border: 2px outset #fff !important; color: #000 !important; border-radius: 0 !important; background: #c0c0c0 !important; text-transform: uppercase; font-weight: bold; }
        [data-theme="retro90s"] .nav-link.active, [data-theme="retro90s"] .filter-btn.active { border: 2px inset #fff !important; background: #808080 !important; color: #fff !important; }
        [data-theme="retro90s"] th { border: 2px outset #fff !important; background: #c0c0c0 !important; color: #000 !important; text-align: center; }
        [data-theme="retro90s"] td { border: 1px inset #fff !important; background: #fff !important; color: #000 !important; }
        [data-theme="retro90s"] .reviewer-img { border-radius: 0 !important; border: 2px inset #fff !important; }
        [data-theme="retro90s"] .icon-guardian { border-radius: 0 !important; }
        
        /* Branding */
        .brand-logo { font-size: 28px; font-weight: 800; letter-spacing: -1px; }
        .brand-logo span { color: var(--brand-accent); }

        .nav-bar { display: flex; align-items: center; gap: 15px; margin-bottom: 20px; padding: 10px 15px; background: var(--card-bg); border-radius: 8px; border: 1px solid var(--border-color); }
        .nav-link { color: var(--text-color); text-decoration: none; font-weight: bold; padding: 8px 15px; border-radius: 5px; }
        .nav-link:hover { background: var(--border-color); }
        .nav-link.active { background: var(--btn-bg); color: white; }
        
        .theme-btn { background: transparent; border: none; color: var(--text-color); font-size: 18px; cursor: pointer; padding: 8px; border-radius: 50%; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center; transition: 0.2s; margin-left: auto; }
        .theme-btn:hover { background: var(--border-color); transform: rotate(15deg); }
        .theme-btn i { transition: 0.3s; }
        
        .card { background: var(--card-bg); padding: 25px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.2); margin-bottom: 20px; border: 1px solid var(--border-color); }
        button, select, input[type="text"], input[type="number"] { background: var(--card-bg); color: var(--text-color); border: 1px solid var(--border-color); padding: 10px; border-radius: 5px; }
        button { background: var(--btn-bg); color: white; border: none; font-weight: bold; cursor: pointer; padding: 10px 20px; }
        button:hover { background: var(--btn-hover); }
        button.sync-btn { background: var(--btn-sync); margin-top: 15px; }
        button.sync-btn:hover { background: var(--btn-sync-hover); }
        
        table { width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 14px; }
        th, td { border: 1px solid var(--border-color); padding: 12px; text-align: left; vertical-align: top;}
        th { background-color: var(--th-bg); }
        a { color: var(--link-color); text-decoration: none; }
        
        .poster-img { width: 60px; height: 90px; object-fit: cover; border-radius: 4px; box-shadow: 0 2px 4px rgba(0,0,0,0.5); background: #333; display: flex; align-items: center; justify-content: center; font-size: 10px; color: #666; }
        
        .link-icons { display: flex; gap: 16px; font-size: 1.6em; align-items: center; margin-top: 5px; }
        .link-icons a { transition: 0.2s; display: inline-flex; align-items: center; justify-content: center; }
        .link-icons a:hover { transform: scale(1.2); filter: brightness(1.2); }
        
        .icon-guardian { color: #052962; font-family: serif; font-weight: 900; font-size: 0.9em; background: white; width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; border: 1px solid #052962; }
        .icon-imdb { color: #f5c518; }
        .icon-letterboxd { color: #00e054; }
        .icon-tvdb { color: #3eb4e4; }
        
        [data-theme="dark"] .icon-guardian { background: #052962; color: white; border-color: #fff; }
        
        .checkbox-cell { text-align: center; width: 40px; }
        input[type="checkbox"] { transform: scale(1.3); cursor: pointer; accent-color: var(--btn-sync); }
        
        details.review-details { margin-top: 8px; font-size: 13px; color: inherit; opacity: 0.8;}
        details.review-details summary { cursor: pointer; font-weight: bold; color: var(--link-color); outline: none; margin-bottom: 5px; }
        .review-body { 
            background: var(--th-bg); 
            padding: 15px; 
            border-left: 3px solid var(--border-color); 
            border-radius: 4px; 
            line-height: 1.6; 
            margin-top: 5px; 
            white-space: pre-wrap; 
            font-family: 'Georgia', serif; 
            font-size: 15px; 
        }
        
        .controls-header { display: flex; justify-content: space-between; align-items: center; background: var(--th-bg); padding: 15px; border-radius: 5px; border: 1px solid var(--border-color); margin-bottom: 15px; flex-wrap: wrap; gap: 15px;}
        .star-widget-container { display: flex; align-items: center; gap: 10px; }
        .stars-container { font-size: 28px; cursor: pointer; display: inline-flex; }
        .star { color: var(--star-inactive); transition: color 0.2s; user-select: none; }
        .star.active, .star.hover { color: var(--star-active); text-shadow: 0 0 5px rgba(255,193,7,0.5); }
        
        .filter-group { display: flex; align-items: center; gap: 10px; }
        .filter-btn { background: var(--card-bg); color: var(--text-color); border: 1px solid var(--border-color); padding: 5px 12px; border-radius: 20px; text-decoration: none; font-size: 13px; font-weight: bold; }
        .filter-btn.active { background: var(--btn-bg); color: white; border-color: var(--btn-bg); }
        
        .reviewer-list { display: flex; gap: 20px; overflow-x: auto; padding: 15px 10px; margin-bottom: 10px; scrollbar-width: thin; align-items: flex-start; justify-content: space-between; }
        .reviewer-item { display: flex; flex-direction: column; align-items: center; text-decoration: none; color: var(--text-color); opacity: 0.5; transition: 0.2s; cursor: pointer; min-width: 90px; padding-bottom: 10px; }
        .reviewer-item:hover, .reviewer-item.active { opacity: 1; transform: scale(1.05); }
        .reviewer-img { width: 80px; height: 80px; border-radius: 50%; object-fit: cover; border: 3px solid transparent; margin-bottom: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); background: #2c2c2c; }
        .reviewer-item.active .reviewer-img { border-color: var(--btn-sync); }
        
        /* Local 'All' Icon */
        .all-icon { background: var(--btn-bg); color: white; display: flex; align-items: center; justify-content: center; font-size: 28px; font-weight: bold; }
        .reviewer-item span { font-size: 13px; font-weight: bold; text-align: center; line-height: 1.2; }
        
        .top-contributors-label { font-size: 14px; font-weight: bold; color: var(--text-color); opacity: 0.8; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
        .top-contributors-label i { color: var(--star-active); }
        
        .other-reviewers-container { align-self: flex-start; margin-left: auto; margin-top: 10px; min-width: 220px; flex-shrink: 0; }
        .other-reviewers-select { width: 100%; padding: 12px 20px; border-radius: 30px; background: var(--th-bg); border: 2px solid var(--border-color); color: var(--text-color); font-weight: bold; cursor: pointer; font-size: 14px; box-shadow: 0 4px 8px rgba(0,0,0,0.2); appearance: none; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='currentColor' viewBox='0 0 16 16'%3E%3Cpath d='M7.247 11.14 2.451 5.658C1.885 5.013 2.345 4 3.204 4h9.592a1 1 0 0 1 .753 1.659l-4.796 5.48a1 1 0 0 1-1.506 0z'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: calc(100% - 15px) center; }
        .other-reviewers-select:hover { border-color: var(--btn-bg); transform: translateY(-2px); transition: 0.2s; }
        
        .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .settings-section { background: var(--th-bg); padding: 20px; border-radius: 5px; border: 1px solid var(--border-color); }
        .form-group { margin-bottom: 15px; display: flex; flex-direction: column; }
        .form-group label { font-weight: bold; margin-bottom: 5px; font-size: 14px; }
        .form-group input[type="text"], .form-group input[type="number"], .form-group select { width: 100%; box-sizing: border-box; }
        .form-row { display: flex; align-items: center; gap: 10px; margin-bottom: 15px; }
        
        .refresh-btn { background: transparent; color: var(--text-color); border: 1px solid var(--border-color); padding: 5px 10px; border-radius: 4px; cursor: pointer; font-size: 14px; display: inline-flex; align-items: center; gap: 5px;}
        .refresh-btn:hover { background: var(--border-color); }
        
        .pagination { display: flex; justify-content: center; align-items: center; gap: 10px; margin-top: 30px; }
        .page-link { background: var(--card-bg); color: var(--text-color); border: 1px solid var(--border-color); padding: 8px 15px; border-radius: 5px; text-decoration: none; font-weight: bold; }
        .page-link:hover { background: var(--border-color); }
        .page-link.active { background: var(--btn-bg); color: white; border-color: var(--btn-bg); }
        .page-info { font-size: 14px; opacity: 0.8; }

        /* Mobile View Fixes */
        @media (max-width: 768px) {
            body { padding: 10px; }
            .nav-bar { flex-wrap: wrap; }
            .settings-grid { grid-template-columns: 1fr; }
            .settings-section { grid-column: span 1 !important; }
            .reviewer-list { gap: 15px; }
            .reviewer-item { min-width: 80px; }
            .reviewer-img { width: 70px; height: 70px; }
            .other-reviewers-container { min-width: 100%; margin-left: 0; margin-top: 10px; }
            .controls-header { flex-direction: column; align-items: flex-start; }
        }
    </style>
    {% if view == 'home' or view == 'history' %}
    <script>
        function toggleAll(source) {
            let checkboxes = document.getElementsByName('selected_items');
            for(let i=0, n=checkboxes.length; i<n; i++) { checkboxes[i].checked = source.checked; }
        }
        document.addEventListener("DOMContentLoaded", function() {
            const stars = document.querySelectorAll('.star');
            const minStarsInput = document.getElementById('min_stars_input');
            const displayLabel = document.getElementById('star-label-text');
            const filterForm = document.getElementById('filter-form');

            function fillStars(rating, type) {
                stars.forEach(star => {
                    let starVal = parseInt(star.getAttribute('data-value'));
                    if (type === 'hover') {
                        if (starVal <= rating) star.classList.add('hover'); else star.classList.remove('hover');
                    } else {
                        if (starVal <= rating) star.classList.add('active'); else star.classList.remove('active');
                    }
                });
                if(type === 'hover') displayLabel.innerText = rating + "+ Stars";
            }

            stars.forEach(star => {
                star.addEventListener('mouseover', function() { fillStars(this.getAttribute('data-value'), 'hover'); });
                star.addEventListener('mouseout', function() {
                    stars.forEach(s => s.classList.remove('hover'));
                    fillStars(minStarsInput.value, 'active');
                    displayLabel.innerText = minStarsInput.value + "+ Stars";
                });
                star.addEventListener('click', function() {
                    minStarsInput.value = this.getAttribute('data-value');
                    fillStars(minStarsInput.value, 'active');
                    filterForm.submit();
                });
            });
            fillStars(minStarsInput.value, 'active');
        });
    </script>
    {% endif %}
</head>
<body>
    {% set base_route = 'index' if view == 'home' else 'history' %}
    
    <nav class="nav-bar">
        <a href="{{ url_for('index') }}" class="nav-link {% if view == 'home' %}active{% endif %}">📰 Review Picker</a>
        <a href="{{ url_for('history') }}" class="nav-link {% if view == 'history' %}active{% endif %}">📜 Sync History</a>
        <a href="{{ url_for('settings') }}" class="nav-link {% if view == 'settings' %}active{% endif %}">⚙️ Configuration</a>
        <form method="POST" action="{{ url_for('toggle_theme') }}" style="margin:0; margin-left: auto; display: flex;">
            <button type="submit" class="theme-btn" title="Toggle Light/Dark Mode">
                {% if config.ui.theme == 'dark' %}<i class="fas fa-sun"></i>{% else %}<i class="fas fa-moon"></i>{% endif %}
            </button>
        </form>
    </nav>

    {% if view == 'home' or view == 'history' %}
        <header class="theme-toggle-container">
            <div style="flex-grow: 1;">
                <div class="top-contributors-label"><i class="fas fa-award"></i> TOP CONTRIBUTORS</div>
                <div class="reviewer-list" style="margin-bottom: 0;">
                    <a href="{{ url_for(base_route, reviewer='all', min_stars=min_stars, media_type=selected_media_type) }}" class="reviewer-item {% if selected_reviewer == 'all' %}active{% endif %}">
                        <div class="reviewer-img all-icon">ALL</div>
                        <span>All</span>
                    </a>
                    {% for rev in ui_reviewers.top %}
                    <a href="{{ url_for(base_route, reviewer=rev.tag, min_stars=min_stars, media_type=selected_media_type) }}" class="reviewer-item {% if selected_reviewer == rev.tag %}active{% endif %}" title="{{ rev.name }} ({{ rev.review_count }} reviews)">
                        {% set img_path = rev.image_url if rev.image_url.startswith('http') else url_for('static', filename=rev.image_url) %}
                        <img src="{{ img_path }}" class="reviewer-img" alt="" width="90" height="90" loading="eager">
                        <span>{{ rev.name }}</span>
                    </a>
                    {% endfor %}
                    
                    <div class="other-reviewers-container">
                        <select class="other-reviewers-select" onchange="if(this.value) window.location.href=this.value">
                            <option value="{{ url_for(base_route, reviewer='all', min_stars=min_stars, media_type=selected_media_type) }}">-- All Reviewers --</option>
                            {% for rev in ui_reviewers.others %}
                            <option value="{{ url_for(base_route, reviewer=rev.tag, min_stars=min_stars, media_type=selected_media_type) }}" {% if selected_reviewer == rev.tag %}selected{% endif %}>
                                {{ rev.name }}
                            </option>
                            {% endfor %}
                        </select>
                    </div>
                </div>
            </div>
        </header>

        <main class="card">
            <div class="controls-header">
                <div style="display: flex; align-items: center; gap: 15px;">
                    <div class="brand-logo">gu<span>ARR</span>dian</div>
                    {% if view == 'home' %}
                    <form method="POST" style="margin:0; display: flex; gap: 10px;">
                        <input type="hidden" name="action" value="force_fetch">
                        <button type="submit" class="refresh-btn" title="Force Refresh API now">↻ Force Refresh</button>
                    </form>
                    <form method="POST" style="margin:0;">
                        <input type="hidden" name="action" value="force_sync">
                        <button type="submit" class="refresh-btn" title="Sync with *arr libraries now">🔗 Sync Library</button>
                    </form>
                    {% if fetch_msg %}<span style="color:var(--btn-sync); font-weight:bold; font-size: 14px;">{{ fetch_msg }}</span>{% endif %}
                    {% else %}
                    <span style="opacity:0.7; font-size: 14px;">Total Synced: {{ reviews|length }}</span>
                    {% endif %}
                </div>

                <div class="filter-group">
                    <span>Filter:</span>
                    <a href="{{ url_for(base_route, reviewer=selected_reviewer, min_stars=min_stars, media_type='all') }}" class="filter-btn {% if selected_media_type == 'all' %}active{% endif %}">All</a>
                    <a href="{{ url_for(base_route, reviewer=selected_reviewer, min_stars=min_stars, media_type='movie') }}" class="filter-btn {% if selected_media_type == 'movie' %}active{% endif %}">Movies</a>
                    <a href="{{ url_for(base_route, reviewer=selected_reviewer, min_stars=min_stars, media_type='tv') }}" class="filter-btn {% if selected_media_type == 'tv' %}active{% endif %}">TV Shows</a>
                </div>
                
                <form method="GET" id="filter-form" style="margin:0;">
                    <input type="hidden" name="reviewer" value="{{ selected_reviewer }}">
                    <input type="hidden" name="media_type" value="{{ selected_media_type }}">
                    <div class="star-widget-container">
                        <span>{% if view == 'home' %}Min Rating:{% else %}Filter Rating:{% endif %}</span>
                        <div class="stars-container">
                            <span class="star" data-value="1">★</span><span class="star" data-value="2">★</span>
                            <span class="star" data-value="3">★</span><span class="star" data-value="4">★</span>
                            <span class="star" data-value="5">★</span>
                        </div>
                        <span id="star-label-text" style="font-weight:bold; width: 70px;">{{ min_stars }}+ Stars</span>
                        <input type="hidden" name="min_stars" id="min_stars_input" value="{{ min_stars }}">
                    </div>
                </form>
            </div>

            {% if view == 'home' %}
                {% if sync_results %}
                <div style="background: var(--th-bg); border: 1px solid var(--border-color); padding: 15px; border-radius: 5px; margin-bottom: 20px;">
                    <h3 style="margin-top:0;">Sync Progress Results:</h3>
                    <table style="margin-top:0;">
                        <tr><th>Title</th><th>Type</th><th>Status</th></tr>
                        {% for item in sync_results %}
                        <tr>
                            <td>{{ item.title }}</td><td>{{ item.type | upper }}</td>
                            <td style="font-weight: bold; color: {% if 'Added' in item.status or 'Already' in item.status %}var(--btn-sync){% else %}#dc3545{% endif %}">
                                {{ item.status }}
                            </td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>
                {% endif %}

                <form method="POST">
                    <input type="hidden" name="action" value="sync">
                    <table id="reviews-table">
                        <tr>
                            <th class="checkbox-cell"><input type="checkbox" onClick="toggleAll(this)" title="Select All" /></th>
                            <th style="width: 70px;">Poster</th>
                            <th style="width: 100px;">Date</th>
                            <th>Details</th>
                            <th>Reviewer</th>
                            <th style="width: 80px;">Rating</th>
                            <th style="width: 100px;">Links</th>
                        </tr>
                        {% for item in reviews %}
                        <tr>
                            <td class="checkbox-cell">
                                {% if item.status == 'new' %}
                                    <input type="checkbox" name="selected_items" value="{{ item.guardian_url }}:::{{ item.type }}:::{{ item.title }}">
                                {% else %}
                                    <i class="fas fa-check-circle" style="color: var(--btn-sync); font-size: 1.2em;" title="Already Synced"></i>
                                {% endif %}
                            </td>
                            <td>
                                {% if item.poster_url %}
                                    <img src="{{ item.poster_url }}" class="poster-img" loading="lazy">
                                {% else %}
                                    <div class="poster-img">No Poster</div>
                                {% endif %}
                            </td>
                            <td>{{ item.date }}</td>
                            <td>
                                <strong>{{ item.title }}</strong> ({{ item.type | upper }})
                                {% if item.status == 'synced' %}
                                    <div style="margin-top: 5px; font-size: 11px; color: var(--btn-sync); font-weight: bold;">
                                        <i class="fas fa-hdd"></i> IN LIBRARY
                                    </div>
                                {% endif %}
                                <details class="review-details">
                                    <summary>Read Review Extract</summary>
                                    <div class="review-body">{{ item.review_text }}</div>
                                </details>
                            </td>
                            <td>
                                <div style="display: flex; align-items: center; gap: 10px;">
                                    <img src="{% if item.reviewer_image.startswith('http') %}{{ item.reviewer_image }}{% else %}{{ url_for('static', filename=item.reviewer_image) }}{% endif %}" style="width: 30px; height: 30px; border-radius: 50%; object-fit: cover; background: #2c2c2c;" width="30" height="30">
                                    {{ item.reviewer }}
                                </div>
                            </td>
                            <td style="color:var(--star-active); font-weight:bold; font-size: 16px;">
                                {% if item.rating > 0 %}{{ '★' * item.rating }}{% else %}Unrated{% endif %}
                            </td>
                            <td>
                                <div class="link-icons">
                                    <a href="{{ item.guardian_url }}" target="_blank" title="Guardian" class="icon-guardian">G</a>
                                    <a href="{{ item.imdb_url }}" target="_blank" title="IMDb" class="icon-imdb"><i class="fab fa-imdb"></i></a>
                                    {% if item.letterboxd_url %}<a href="{{ item.letterboxd_url }}" target="_blank" title="Letterboxd" class="icon-letterboxd"><i class="fab fa-square-letterboxd"></i></a>{% endif %}
                                    {% if item.tvdb_url %}<a href="{{ item.tvdb_url }}" target="_blank" title="TVDb" class="icon-tvdb"><i class="fas fa-tv"></i></a>{% endif %}
                                </div>
                            </td>
                        </tr>
                        {% else %}
                        <tr><td colspan="7" style="text-align:center; padding: 20px;">No reviews found matching your filter. Try adjusting your stars or reviewer selection!</td></tr>
                        {% endfor %}
                    </table>
                    {% if reviews %}
                    <button type="submit" class="sync-btn">Send Selected to *arr</button>
                    {% endif %}
                </form>
            {% else %}
                <table>
                    <tr>
                        <th style="width: 70px;">Poster</th>
                        <th style="width: 100px;">Sync Date</th>
                        <th>Details</th>
                        <th>Reviewer</th>
                        <th style="width: 80px;">Rating</th>
                        <th style="width: 100px;">Links</th>
                    </tr>
                    {% for item in reviews %}
                    <tr>
                        <td>
                            {% if item.poster_url %}
                                <img src="{{ item.poster_url }}" class="poster-img" loading="lazy">
                            {% else %}
                                <div class="poster-img">No Poster</div>
                            {% endif %}
                        </td>
                        <td>{{ item.date }}</td>
                        <td>
                            <strong>{{ item.title }}</strong> ({{ item.type | upper }})
                            <div style="margin-top: 5px; font-size: 12px; opacity: 0.8;">
                                <i class="fas fa-check-circle" style="color: var(--btn-sync); font-size: 1.1em;"></i> Successfully added to {{ 'Radarr' if item.type == 'movie' else 'Sonarr' }}
                            </div>
                        </td>
                        <td>
                            <div style="display: flex; align-items: center; gap: 10px;">
                                <img src="{% if item.reviewer_image.startswith('http') %}{{ item.reviewer_image }}{% else %}{{ url_for('static', filename=item.reviewer_image) }}{% endif %}" style="width: 30px; height: 30px; border-radius: 50%; object-fit: cover; background: #2c2c2c;" width="30" height="30">
                                {{ item.reviewer }}
                            </div>
                        </td>
                        <td style="color:var(--star-active); font-weight:bold; font-size: 16px;">
                            {% if item.rating > 0 %}{{ '★' * item.rating }}{% else %}Unrated{% endif %}
                        </td>
                        <td>
                            <div class="link-icons">
                                <a href="{{ item.guardian_url }}" target="_blank" title="Guardian" class="icon-guardian">G</a>
                                <a href="{{ item.imdb_url }}" target="_blank" title="IMDb" class="icon-imdb"><i class="fab fa-imdb"></i></a>
                                {% if item.letterboxd_url %}<a href="{{ item.letterboxd_url }}" target="_blank" title="Letterboxd" class="icon-letterboxd"><i class="fab fa-square-letterboxd"></i></a>{% endif %}
                                {% if item.tvdb_url %}<a href="{{ item.tvdb_url }}" target="_blank" title="TVDb" class="icon-tvdb"><i class="fas fa-tv"></i></a>{% endif %}
                            </div>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="6" style="text-align:center; padding: 40px; opacity: 0.6;">Your sync history is empty. Go to the Review Picker to start adding content!</td></tr>
                    {% endfor %}
                </table>
            {% endif %}
        </main>
    {% elif view == 'settings' %}
        <main class="card">
            <h2>⚙️ System Configuration</h2>
            <p>Update your connection details below. These settings are saved to <code>config/config.yml</code>.</p>
            {% if save_msg %}
                <div style="background: var(--btn-sync); color: white; padding: 10px; border-radius: 5px; margin-bottom: 15px;">{{ save_msg }}</div>
            {% endif %}
            
            <form method="POST">
                <input type="hidden" name="action" value="save_settings">
                <div class="settings-grid">
                    <div class="settings-section">
                        <h3 style="margin-top:0;">📰 The Guardian API</h3>
                        <div class="form-group"><label>API Key</label><input type="text" name="guardian_api_key" value="{{ config.guardian.api_key }}"></div>
                        <div class="form-group"><label>Max Reviews to Fetch (per Critic)</label><input type="number" name="guardian_fetch_limit" value="{{ config.guardian.get('fetch_limit', 30) }}"></div>
                    </div>
                    <div class="settings-section">
                        <h3 style="margin-top:0;">🎨 Appearance</h3>
                        <div class="form-group">
                            <label>Theme</label>
                            <select name="ui_theme">
                                <option value="dark" {% if config.ui.theme == 'dark' %}selected{% endif %}>Dark Mode</option>
                                <option value="light" {% if config.ui.theme == 'light' %}selected{% endif %}>Light Mode</option>
                                <option value="retro90s" {% if config.ui.theme == 'retro90s' %}selected{% endif %}>Retro 90s</option>
                            </select>
                        </div>
                        <div class="form-row">
                            <input type="checkbox" name="ui_hide_synced" value="1" {% if config.ui.hide_synced %}checked{% endif %}>
                            <label style="margin:0;">Hide Already Synced Items</label>
                        </div>
                    </div>
                    <div class="settings-section">
                        <h3 style="margin-top:0;">🎬 Radarr (Movies)</h3>
                        <div class="form-group"><label>URL (e.g., http://192.168.1.10:7878)</label><input type="text" name="radarr_url" value="{{ config.radarr.url }}"></div>
                        <div class="form-group"><label>API Key</label><input type="text" name="radarr_api_key" value="{{ config.radarr.api_key }}"></div>
                        <div class="form-group"><label>Root Folder Path</label><input type="text" name="radarr_root_folder_path" value="{{ config.radarr.root_folder_path }}" placeholder="As radarr sees it"></div>
                        <div class="form-group">
                            <div style="display:flex; justify-content: space-between; align-items: center; margin-bottom: 5px;">
                                <label style="margin-bottom:0;">Quality Profile</label>
                                <button type="submit" name="action" value="load_profiles" class="refresh-btn" style="padding: 2px 8px; font-size: 11px;">🔄 Load</button>
                            </div>
                            {% if radarr_profiles %}
                            <select name="radarr_quality_profile_id">
                                {% for p in radarr_profiles %}
                                <option value="{{ p.id }}" {% if config.radarr.quality_profile_id == p.id %}selected{% endif %}>{{ p.name }}</option>
                                {% endfor %}
                            </select>
                            {% else %}
                            <input type="number" name="radarr_quality_profile_id" value="{{ config.radarr.quality_profile_id }}">
                            <small style="opacity:0.7; font-size: 10px;">Enter ID manually or click 'Load'.</small>
                            {% endif %}
                        </div>
                        <div class="form-row"><input type="checkbox" name="radarr_search_on_add" value="1" {% if config.radarr.get('search_on_add', True) %}checked{% endif %}><label style="margin:0;">Trigger Search on Add</label></div>
                    </div>
                    
                    <div class="settings-section">
                        <h3 style="margin-top:0;">📺 Sonarr (TV Shows)</h3>
                        <div class="form-group"><label>URL (e.g., http://192.168.1.10:8989)</label><input type="text" name="sonarr_url" value="{{ config.sonarr.url }}"></div>
                        <div class="form-group"><label>API Key</label><input type="text" name="sonarr_api_key" value="{{ config.sonarr.api_key }}"></div>
                        <div class="form-group"><label>Root Folder Path</label><input type="text" name="sonarr_root_folder_path" value="{{ config.sonarr.root_folder_path }}" placeholder="As sonarr sees it"></div>
                        <div class="form-group">
                            <div style="display:flex; justify-content: space-between; align-items: center; margin-bottom: 5px;">
                                <label style="margin-bottom:0;">Quality Profile</label>
                                <button type="submit" name="action" value="load_profiles" class="refresh-btn" style="padding: 2px 8px; font-size: 11px;">🔄 Load</button>
                            </div>
                            {% if sonarr_profiles %}
                            <select name="sonarr_quality_profile_id">
                                {% for p in sonarr_profiles %}
                                <option value="{{ p.id }}" {% if config.sonarr.quality_profile_id == p.id %}selected{% endif %}>{{ p.name }}</option>
                                {% endfor %}
                            </select>
                            {% else %}
                            <input type="number" name="sonarr_quality_profile_id" value="{{ config.sonarr.quality_profile_id }}">
                            <small style="opacity:0.7; font-size: 10px;">Enter ID manually or click 'Load'.</small>
                            {% endif %}
                        </div>
                        <div class="form-row"><input type="checkbox" name="sonarr_search_on_add" value="1" {% if config.sonarr.get('search_on_add', True) %}checked{% endif %}><label style="margin:0;">Trigger Search on Add</label></div>
                    </div>

                    <div class="settings-section" style="grid-column: span 2;">
                        <h3 style="margin-top:0;">🤖 Automation (Auto-Sync)</h3>
                        <div style="display: flex; gap: 30px; align-items: flex-end;">
                            <div class="form-row" style="margin-bottom: 0;">
                                <input type="checkbox" name="auto_sync_enabled" value="1" {% if config.auto_sync.enabled %}checked{% endif %}>
                                <label style="margin:0;">Enable Auto-Sync</label>
                            </div>
                            <div class="form-group" style="margin-bottom: 0; flex-grow: 0; width: 120px;">
                                <label style="white-space: nowrap;">Min Stars</label>
                                <input type="number" name="auto_sync_min_stars" value="{{ config.auto_sync.get('min_stars', 5) }}" min="1" max="5" style="text-align: center; font-weight: bold; font-size: 1.1em;">
                            </div>
                        </div>
                        <small style="opacity:0.7; font-size: 11px; margin-top: 10px; display: block;">When enabled, new reviews with {{ config.auto_sync.get('min_stars', 5) }} or more stars will be added to *arr immediately upon discovery.</small>
                    </div>
                </div>
                <button type="submit" name="action" value="save_settings" class="sync-btn" style="width: 100%; font-size: 16px; margin-top: 25px;">Save Settings</button>
            </form>
        </main>
    {% endif %}

    {% if total_pages and total_pages > 1 %}
    <footer class="pagination" style="margin: auto auto 80px auto; padding: 20px; border-top: 2px solid var(--border-color); text-align: center; width: 100%; max-width: 1000px; display: block !important;">
        {% if page > 1 %}
        <a href="{{ url_for(base_route, page=page-1, reviewer=selected_reviewer, min_stars=min_stars, media_type=selected_media_type) }}" class="page-link">&laquo; Prev</a>
        {% endif %}

        <span class="page-info" style="margin: 0 15px;">Page {{ page }} of {{ total_pages }} ({{ total_count }} total)</span>

        {% if page < total_pages %}
        <a href="{{ url_for(base_route, page=page+1, reviewer=selected_reviewer, min_stars=min_stars, media_type=selected_media_type) }}" class="page-link">Next &raquo;</a>
        {% endif %}
    </footer>
    {% endif %}
</body>
</html>
"""

@app.route("/toggle_theme", methods=["POST"])
def toggle_theme():
    config = load_config()
    themes = ["dark", "light"]
    current = config['ui'].get('theme', 'dark')
    # If currently in retro90s, toggle to light. Otherwise cycle between dark/light.
    if current == "retro90s":
        next_theme = "light"
    else:
        next_theme = themes[(themes.index(current) + 1) % len(themes)] if current in themes else "dark"
    config['ui']['theme'] = next_theme
    save_config(config)
    return redirect(request.referrer or url_for('index'))

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.static_folder, 'favicons'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/apple-touch-icon.png')
def apple_touch_icon():
    return send_from_directory(os.path.join(app.static_folder, 'favicons'),
                               'apple-touch-icon.png', mimetype='image/png')

@app.route("/", methods=["GET", "POST"])
def index():
    global LAST_FETCH_TIME
    config = load_config()
    sync_results = []
    fetch_msg = ""
    
    current_time = time.time()
    
    # Auto fetch check (cooldown prevents hitting rate limits on page refresh)
    if current_time - LAST_FETCH_TIME > FETCH_COOLDOWN_SECONDS:
        threading.Thread(target=fetch_and_cache_reviews, args=(config, 'all')).start()
        # Also sync existing library when we do a full fetch
        threading.Thread(target=sync_existing_library, args=(config,)).start()
        LAST_FETCH_TIME = current_time
    else:
        # If we aren't fetching new reviews, use this "quiet time" to backfill 20 missing posters
        threading.Thread(target=backfill_missing_metadata, args=(config, 20)).start()

    # Default to 3 stars if not specified
    min_stars = int(request.args.get("min_stars", 3))
    selected_reviewer = request.args.get("reviewer", "all")
    selected_media_type = request.args.get("media_type", "all")
    page = int(request.args.get("page", 1))
    per_page = 10

    if request.method == "POST":
        action = request.form.get("action")
        
        # New Force Fetch Logic
        if action == "force_fetch":
            added = fetch_and_cache_reviews(config, 'all')
            fetch_msg = f"✓ Found {added} new reviews."
            LAST_FETCH_TIME = time.time() # Reset background timer
            
        elif action == "force_sync":
            sync_existing_library(config)
            fetch_msg = "✓ Library sync complete."

        elif action == "sync":
            selected_items = request.form.getlist("selected_items")
            for item in selected_items:
                guardian_url, media_type, title = item.split(":::", 2)
                if media_type == 'movie':
                    success, msg = add_to_radarr(title, config)
                else:
                    success, msg = add_to_sonarr(title, config)
                
                if success:
                    mark_as_synced(guardian_url)
                sync_results.append({"title": title, "type": media_type, "status": msg})

    hide_synced = config['ui'].get('hide_synced', False)
    data = get_cached_reviews(min_stars, selected_reviewer, selected_media_type, hide_synced=hide_synced, page=page, per_page=per_page)
    reviews = data["reviews"]
    total_count = data["total"]
    total_pages = (total_count + per_page - 1) // per_page
    
    ui_reviewers = get_ui_reviewers()

    return render_template_string(HTML_TEMPLATE, 
                                  view='home',
                                  config=config,
                                  reviews=reviews, 
                                  ui_reviewers=ui_reviewers,
                                  sync_results=sync_results, 
                                  fetch_msg=fetch_msg,
                                  selected_reviewer=selected_reviewer,
                                  selected_media_type=selected_media_type,
                                  min_stars=min_stars,
                                  page=page,
                                  total_pages=total_pages,
                                  total_count=total_count)

@app.route("/history")
def history():
    config = load_config()
    selected_reviewer = request.args.get("reviewer", "all")
    selected_media_type = request.args.get("media_type", "all")
    min_stars = int(request.args.get("min_stars", 1)) # History usually shows all, but we'll allow filtering
    page = int(request.args.get("page", 1))
    per_page = 10
    
    data = get_history(selected_reviewer, selected_media_type, page=page, per_page=per_page)
    reviews = data["reviews"]
    total_count = data["total"]
    total_pages = (total_count + per_page - 1) // per_page
    
    ui_reviewers = get_ui_reviewers()
    
    return render_template_string(HTML_TEMPLATE, 
                                  view='history', 
                                  config=config, 
                                  reviews=reviews,
                                  ui_reviewers=ui_reviewers,
                                  selected_reviewer=selected_reviewer,
                                  selected_media_type=selected_media_type,
                                  min_stars=min_stars,
                                  page=page,
                                  total_pages=total_pages,
                                  total_count=total_count)

@app.route("/settings", methods=["GET", "POST"])
def settings():
    config = load_config()
    save_msg = ""
    
    # We fetch profiles to show in dropdowns if available
    radarr_profiles = get_radarr_profiles(config)
    sonarr_profiles = get_sonarr_profiles(config)
    
    if request.method == "POST":
        action = request.form.get("action")
        
        if action == "save_settings":
            config['guardian']['api_key'] = request.form.get('guardian_api_key', '')
            config['guardian']['fetch_limit'] = int(request.form.get('guardian_fetch_limit', 30))
            config['ui']['theme'] = request.form.get('ui_theme', 'dark')
            config['ui']['hide_synced'] = request.form.get('ui_hide_synced') == "1"
            config['auto_sync']['enabled'] = request.form.get('auto_sync_enabled') == "1"
            config['auto_sync']['min_stars'] = int(request.form.get('auto_sync_min_stars', 5))
            config['radarr']['url'] = request.form.get('radarr_url', '')
            config['radarr']['api_key'] = request.form.get('radarr_api_key', '')
            config['radarr']['root_folder_path'] = request.form.get('radarr_root_folder_path', '')
            config['radarr']['quality_profile_id'] = int(request.form.get('radarr_quality_profile_id', 1))
            config['radarr']['search_on_add'] = request.form.get('radarr_search_on_add') == "1"
            config['sonarr']['url'] = request.form.get('sonarr_url', '')
            config['sonarr']['api_key'] = request.form.get('sonarr_api_key', '')
            config['sonarr']['root_folder_path'] = request.form.get('sonarr_root_folder_path', '')
            config['sonarr']['quality_profile_id'] = int(request.form.get('sonarr_quality_profile_id', 1))
            config['sonarr']['search_on_add'] = request.form.get('sonarr_search_on_add') == "1"
            
            save_config(config)
            save_msg = "Settings saved successfully!"
            # Refresh profiles after saving (in case credentials changed)
            radarr_profiles = get_radarr_profiles(config)
            sonarr_profiles = get_sonarr_profiles(config)

        elif action == "load_profiles":
            radarr_profiles = get_radarr_profiles(config)
            sonarr_profiles = get_sonarr_profiles(config)
            if not radarr_profiles and not sonarr_profiles:
                save_msg = "⚠️ Could not load profiles. Check your URLs and API keys."
            else:
                save_msg = "✓ Profiles loaded successfully."

    # Trigger full library sync in background on load
    threading.Thread(target=sync_existing_library, args=(config,)).start()

    return render_template_string(HTML_TEMPLATE, 
                                  view='settings', 
                                  config=config, 
                                  save_msg=save_msg,
                                  radarr_profiles=radarr_profiles,
                                  sonarr_profiles=sonarr_profiles)

# ==========================================
# 3. CLI & DAEMON HANDLER
# ==========================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Guardian reviews to Radarr/Sonarr.")
    parser.add_argument('--cli', action='store_true', help="Run automatically via CLI once.")
    parser.add_argument('--daemon', type=int, metavar='HOURS', help="Run continuously in the background, checking Guardian API every X hours.")
    parser.add_argument('--config', default=CONFIG_FILE, help="Path to configuration file.")
    parser.add_argument('--reviewer', default='all', help="Reviewer tag to pull (default: all)")
    args = parser.parse_args()

    if args.daemon:
        print(f"Starting Daemon mode. Fetching {args.reviewer} to SQLite database every {args.daemon} hours...")
        while True:
            print(f"\n--- Running scheduled check at {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
            config = load_config(args.config)
            added = fetch_and_cache_reviews(config, args.reviewer)
            print(f"Check complete. {added} new reviews cached to database.")
            time.sleep(args.daemon * 3600)
            
    elif args.cli:
        print(f"Running automated CLI fetch for {args.reviewer}...")
        config = load_config(args.config)
        added = fetch_and_cache_reviews(config, args.reviewer)
        print(f"Cached {added} new reviews. Processing sync...")
        
        # CLI defaults to 3+ stars
        data = get_cached_reviews(3, args.reviewer)
        pending_reviews = data["reviews"]
        
        for r in pending_reviews:
            print(f"[{r['date']}] Found {r['type'].upper()} ({r['rating']} Stars): {r['title']}")
            if r['type'] == 'movie': success, msg = add_to_radarr(r['title'], config)
            else: success, msg = add_to_sonarr(r['title'], config)
                
            if success: mark_as_synced(r['guardian_url'])
            print(f"  -> {msg}")
            
    else:
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", 9988))
        print(f"Starting Web UI on http://{host}:{port} ...")
        app.run(host=host, port=port)
