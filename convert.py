#!/usr/bin/env python3
"""
spotify2apple — convert all your Spotify playlists to an Apple Music HTML site
Usage:
  python3 convert.py              # process ALL your playlists
  python3 convert.py <url>        # process a single playlist
"""

import os
import re
import sys
import time
import json
import tempfile
import subprocess
import urllib.parse
import urllib.request
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv

load_dotenv()

SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI          = "http://127.0.0.1:8888/callback"
CACHE_FILE            = os.path.join(os.path.dirname(__file__), ".itunes_cache.json")
RESULTS_FILE          = os.path.join(os.path.dirname(__file__), ".playlists_data.json")
OUTPUT_FILE           = os.path.join(os.path.dirname(__file__), "spotify2apple.html")


# ── Cache ─────────────────────────────────────────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)

def load_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            data = json.load(f)
        return {pl["id"]: pl for pl in data}
    return {}

def save_results(results_by_id):
    with open(RESULTS_FILE, "w") as f:
        json.dump(list(results_by_id.values()), f)


# ── Spotify ───────────────────────────────────────────────────────────────────

def get_spotify_client():
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope="playlist-read-private playlist-read-collaborative",
        cache_path=os.path.join(os.path.dirname(__file__), ".spotify_token_cache"),
        open_browser=True,
    ))

def get_all_playlists(sp):
    user_id = sp.current_user()["id"]
    playlists = []
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results.get("items", []):
            if pl and pl.get("owner", {}).get("id") == user_id:
                playlists.append({
                    "id":    pl["id"],
                    "name":  pl["name"],
                    "total": (pl.get("tracks") or {}).get("total", 0),
                })
        try:
            results = sp.next(results) if results.get("next") else None
        except Exception:
            break
    return playlists

def get_playlist_tracks(sp, playlist_id):
    tracks = []
    try:
        results = sp.playlist_tracks(playlist_id)
    except Exception:
        return None  # inaccessible playlist
    while results:
        for item in results.get("items", []):
            track = item.get("track") or item.get("item")
            if track and track.get("name"):
                tracks.append({
                    "name":   track["name"],
                    "artist": track["artists"][0]["name"] if track.get("artists") else "",
                })
        try:
            results = sp.next(results) if results.get("next") else None
        except Exception:
            break
    return tracks

def extract_playlist_id(url):
    match = re.search(r"playlist[/:]([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError(f"Could not extract playlist ID from: {url}")
    return match.group(1)


# ── iTunes search ─────────────────────────────────────────────────────────────

def clean_track_name(name):
    cleaned = re.sub(
        r'\s*[\(\[]([^\(\[\)\]]*?(mix|edit|remix|remaster(ed)?|version|radio|extended|original|album|single|vocal|instrumental|reprise)[^\(\[\)\]]*?)[\)\]]',
        '', name, flags=re.IGNORECASE)
    cleaned = re.sub(
        r'\s+-\s+.*(mix|edit|remix|remaster(ed)?|version).*$',
        '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip() or name

def core_track_name(name):
    return name.split(' - ')[0].strip() if ' - ' in name else name

def itunes_search(query):
    q = urllib.parse.quote(query)
    url = f"https://itunes.apple.com/search?term={q}&entity=song&limit=3&country=US"
    req = urllib.request.Request(url, headers={"User-Agent": "spotify2apple/1.0"})
    wait = 15
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                results = json.loads(resp.read()).get("results", [])
            if results or attempt == 3:
                return results
            time.sleep(1)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"\n  [rate limited — waiting {wait}s...]", end="", flush=True)
                time.sleep(wait)
                wait *= 2
            else:
                return []
        except Exception:
            time.sleep(1)
    return []

def search_apple_music(track_name, artist, cache):
    cache_key = f"{artist}||{track_name}"
    if cache_key in cache:
        return cache[cache_key]

    cleaned = clean_track_name(track_name)
    core    = core_track_name(track_name)
    first_word = artist.split()[0] if artist else ""

    queries = [
        f"{artist} {cleaned}",
        f"{artist} {core}" if core != cleaned else None,
        f"{first_word} {cleaned}" if first_word and first_word.lower() != artist.lower() else None,
        f"{artist} {track_name}",
        cleaned,
    ]

    for q in queries:
        if not q:
            continue
        results = itunes_search(q)
        if results:
            r = results[0]
            artwork = r.get("artworkUrl100", "")
            # Upscale artwork to 300x300
            artwork = re.sub(r'\d+x\d+bb', '300x300bb', artwork)
            result = {
                "apple_name":    r.get("trackName", track_name),
                "apple_artist":  r.get("artistName", artist),
                "apple_url":     r.get("trackViewUrl", ""),
                "artwork":       artwork,
            }
            cache[cache_key] = result
            save_cache(cache)  # save immediately after every hit
            return result

    # Don't cache misses — iTunes may have been rate-limited; retry next run
    return None


# ── HTML generation ───────────────────────────────────────────────────────────

def generate_html(playlists_data):
    data_json = json.dumps(playlists_data, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Music</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
  --bg:        #0d0d0d;
  --surface:   #1c1c1e;
  --surface2:  #2c2c2e;
  --accent:    #fc3c44;
  --text:      #ffffff;
  --muted:     #8e8e93;
  --sidebar-w: 260px;
}}

body {{
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', sans-serif;
  background: var(--bg);
  color: var(--text);
  display: flex;
  height: 100vh;
  overflow: hidden;
}}

/* ── Sidebar ── */
.sidebar {{
  width: var(--sidebar-w);
  min-width: var(--sidebar-w);
  background: var(--surface);
  display: flex;
  flex-direction: column;
  border-right: 1px solid rgba(255,255,255,0.06);
  overflow-y: auto;
}}

.sidebar-title {{
  padding: 28px 20px 8px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--muted);
}}

.pl-item {{
  padding: 10px 14px;
  margin: 2px 8px;
  border-radius: 8px;
  cursor: pointer;
  transition: background .12s;
}}
.pl-item:hover {{ background: var(--surface2); }}
.pl-item.active {{ background: var(--accent); }}
.pl-item.active .pl-count {{ color: rgba(255,255,255,.7); }}

.pl-name {{
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.pl-count {{
  font-size: 12px;
  color: var(--muted);
  margin-top: 2px;
}}

/* ── Main ── */
.main {{
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}

.main-header {{
  padding: 32px 28px 20px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  flex-shrink: 0;
}}

.main-header h1 {{
  font-size: 28px;
  font-weight: 800;
  letter-spacing: -.02em;
}}

.main-header .subtitle {{
  font-size: 14px;
  color: var(--muted);
  margin-top: 4px;
}}

.search-bar {{
  margin-top: 14px;
  background: var(--surface2);
  border: none;
  border-radius: 8px;
  padding: 9px 14px;
  color: var(--text);
  font-size: 14px;
  width: 300px;
  outline: none;
}}
.search-bar::placeholder {{ color: var(--muted); }}

.song-scroll {{
  flex: 1;
  overflow-y: auto;
  padding: 24px 28px;
}}

.song-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(155px, 1fr));
  gap: 14px;
}}

/* ── Song card ── */
.song-card {{
  background: var(--surface);
  border-radius: 12px;
  overflow: hidden;
  cursor: pointer;
  transition: transform .18s ease, box-shadow .18s ease;
  position: relative;
}}
.song-card:hover {{
  transform: translateY(-3px) scale(1.02);
  box-shadow: 0 10px 30px rgba(0,0,0,.5);
}}
.song-card.not-found {{ opacity: .4; cursor: default; }}
.song-card.not-found:hover {{ transform: none; box-shadow: none; }}

.card-art {{
  width: 100%;
  aspect-ratio: 1;
  display: block;
  background: var(--surface2);
  object-fit: cover;
}}

.card-info {{
  padding: 10px 11px 12px;
}}
.card-title {{
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.card-artist {{
  font-size: 12px;
  color: var(--muted);
  margin-top: 3px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.card-badge {{
  font-size: 10px;
  color: var(--muted);
  margin-top: 4px;
  font-style: italic;
}}

/* ── Modal ── */
.overlay {{
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,.75);
  backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
  animation: fadeIn .15s ease;
}}
@keyframes fadeIn {{ from {{ opacity:0 }} to {{ opacity:1 }} }}

.modal {{
  background: var(--surface);
  border-radius: 18px;
  padding: 20px;
  width: 480px;
  max-width: 92vw;
  box-shadow: 0 24px 64px rgba(0,0,0,.6);
  animation: slideUp .2s ease;
}}
@keyframes slideUp {{ from {{ transform: translateY(16px); opacity:0 }} to {{ transform:none; opacity:1 }} }}

.modal iframe {{
  width: 100%;
  height: 175px;
  border: none;
  border-radius: 12px;
  display: block;
}}

.modal-actions {{
  display: flex;
  gap: 10px;
  margin-top: 14px;
}}

.btn {{
  flex: 1;
  padding: 11px;
  border-radius: 10px;
  border: none;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
  text-align: center;
  transition: opacity .12s;
}}
.btn:hover {{ opacity: .85; }}
.btn-primary {{ background: var(--accent); color: #fff; }}
.btn-secondary {{ background: var(--surface2); color: var(--text); }}

/* ── Scrollbar ── */
::-webkit-scrollbar {{ width: 6px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: var(--surface2); border-radius: 3px; }}
</style>
</head>
<body>

<aside class="sidebar">
  <div class="sidebar-title">Playlists</div>
  <div id="pl-list"></div>
</aside>

<div class="main">
  <div class="main-header">
    <h1 id="pl-title">Select a playlist</h1>
    <div class="subtitle" id="pl-subtitle"></div>
    <input class="search-bar" id="search" type="text" placeholder="Filter songs..." oninput="filterSongs()" style="display:none">
  </div>
  <div class="song-scroll">
    <div class="song-grid" id="song-grid"></div>
  </div>
</div>

<script>
const DATA = {data_json};
let current = 0;

function renderSidebar() {{
  document.getElementById('pl-list').innerHTML = DATA.map((pl, i) => `
    <div class="pl-item ${{i===current?'active':''}}" onclick="selectPlaylist(${{i}})">
      <div class="pl-name">${{esc(pl.name)}}</div>
      <div class="pl-count">${{pl.found}} / ${{pl.total}} on Apple Music</div>
    </div>
  `).join('');
}}

function selectPlaylist(i) {{
  current = i;
  renderSidebar();
  renderSongs(DATA[i].tracks);
  document.getElementById('pl-title').textContent = DATA[i].name;
  document.getElementById('pl-subtitle').textContent =
    DATA[i].found + ' songs found on Apple Music · ' + (DATA[i].total - DATA[i].found) + ' not found';
  const s = document.getElementById('search');
  s.style.display = '';
  s.value = '';
}}

function renderSongs(tracks) {{
  document.getElementById('song-grid').innerHTML = tracks.map((t, i) => {{
    if (!t.apple_url) return `
      <div class="song-card not-found">
        <div class="card-art" style="background:#2c2c2e;display:flex;align-items:center;justify-content:center">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="#555"><path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3h-6z"/></svg>
        </div>
        <div class="card-info">
          <div class="card-title">${{esc(t.name)}}</div>
          <div class="card-artist">${{esc(t.artist)}}</div>
          <div class="card-badge">Not on Apple Music</div>
        </div>
      </div>`;
    return `
      <div class="song-card" onclick="openSong(${{JSON.stringify(t).replace(/"/g,'&quot;')}})">
        <img class="card-art" src="${{t.artwork}}" loading="lazy" onerror="this.style.background='#2c2c2e'">
        <div class="card-info">
          <div class="card-title">${{esc(t.apple_name)}}</div>
          <div class="card-artist">${{esc(t.apple_artist)}}</div>
        </div>
      </div>`;
  }}).join('');
}}

function filterSongs() {{
  const q = document.getElementById('search').value.toLowerCase();
  const pl = DATA[current];
  const filtered = q ? pl.tracks.filter(t =>
    (t.name + t.artist + (t.apple_name||'') + (t.apple_artist||'')).toLowerCase().includes(q)
  ) : pl.tracks;
  renderSongs(filtered);
}}

function openSong(t) {{
  if (!t.apple_url) return;
  const embedUrl = t.apple_url.replace('music.apple.com', 'embed.music.apple.com');
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.innerHTML = `
    <div class="modal">
      <iframe src="${{embedUrl}}"
        allow="autoplay *; encrypted-media *; fullscreen *; clipboard-write"
        sandbox="allow-forms allow-popups allow-same-origin allow-scripts allow-storage-access-by-user-activation allow-top-navigation-by-user-activation">
      </iframe>
      <div class="modal-actions">
        <a class="btn btn-primary" href="${{t.apple_url}}" target="_blank">Open in Music</a>
        <button class="btn btn-secondary" onclick="this.closest('.overlay').remove()">Close</button>
      </div>
    </div>`;
  overlay.addEventListener('click', e => {{ if (e.target === overlay) overlay.remove(); }});
  document.body.appendChild(overlay);
}}

function esc(s) {{
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

// Select first playlist on load
if (DATA.length) selectPlaylist(0);
</script>
</body>
</html>"""
    return html


# ── Main ──────────────────────────────────────────────────────────────────────

def process_playlist(sp, playlist_id, playlist_name, cache, existing_tracks=None):
    print(f"\n  Processing: {playlist_name}")

    # Build a lookup of already-found tracks so we only search the missing ones
    already = {}
    if existing_tracks:
        for t in existing_tracks:
            if t.get("apple_url"):
                already[f"{t['artist']}||{t['name']}"] = t

    tracks = get_playlist_tracks(sp, playlist_id)
    if tracks is None:
        print(f"  Skipped (no access)")
        return [], 0

    missing = [t for t in tracks if f"{t['artist']}||{t['name']}" not in already]
    print(f"  {len(tracks)} tracks — {len(already)} already found, searching {len(missing)} missing", end="", flush=True)

    result_tracks = []
    found = 0
    for track in tracks:
        key = f"{track['artist']}||{track['name']}"
        if key in already:
            result_tracks.append(already[key])
            found += 1
            continue

        res = search_apple_music(track["name"], track["artist"], cache)
        if res:
            found += 1
            result_tracks.append({
                "name":        track["name"],
                "artist":      track["artist"],
                "apple_name":  res["apple_name"],
                "apple_artist":res["apple_artist"],
                "apple_url":   res["apple_url"],
                "artwork":     res["artwork"],
            })
        else:
            result_tracks.append({
                "name":   track["name"],
                "artist": track["artist"],
                "apple_url": None,
            })
        print(".", end="", flush=True)
        time.sleep(1.2)

    print(f" {found}/{len(tracks)} matched")
    return result_tracks, found


def main():
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("ERROR: SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    saved = load_results()
    print("Connecting to Spotify...")
    sp = get_spotify_client()
    cache = load_cache()

    # Single playlist mode
    if len(sys.argv) >= 2:
        playlist_id = extract_playlist_id(sys.argv[1])
        playlist_name = sp.playlist(playlist_id, fields="name")["name"]
        tracks, found = process_playlist(sp, playlist_id, playlist_name, cache)
        saved[playlist_id] = {"id": playlist_id, "name": playlist_name,
                               "total": len(tracks), "found": found, "tracks": tracks}
        save_results(saved)
        save_cache(cache)

    else:
        print("Fetching your playlists from Spotify...")
        playlists_meta = get_all_playlists(sp)

        # Figure out what needs work
        todo = [pl for pl in playlists_meta if
                pl["id"] not in saved or saved[pl["id"]].get("found", 0) < saved[pl["id"]].get("total", 1)]

        done_count = len(playlists_meta) - len(todo)
        print(f"{done_count} playlists already complete, {len(todo)} need processing.\n")

        if not todo:
            print("All playlists are up to date!")
        else:
            for pl in todo:
                existing = saved.get(pl["id"])
                existing_tracks = existing.get("tracks") if existing else None
                tracks, found = process_playlist(sp, pl["id"], pl["name"], cache, existing_tracks)
                saved[pl["id"]] = {"id": pl["id"], "name": pl["name"],
                                   "total": len(tracks), "found": found, "tracks": tracks}
                save_results(saved)
                save_cache(cache)
                time.sleep(5)

    playlists_data = list(saved.values())

    print(f"\nGenerating site → {OUTPUT_FILE}")
    html = generate_html(playlists_data)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print("Done! Opening in browser...")
    subprocess.run(["open", OUTPUT_FILE])


if __name__ == "__main__":
    main()
