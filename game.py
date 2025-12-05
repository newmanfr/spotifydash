import os
import sys
import io
import json
import tempfile
import subprocess
import requests
import pygame
import librosa
import numpy as np
import spotipy
import random
import bisect
import webbrowser
from spotipy.oauth2 import SpotifyClientCredentials
from pytubefix import Search

# ============================================================
# CONFIG / VERSION / GITHUB
# ============================================================

APP_VERSION = "1.1.1"  # this build

# TODO: change these to your real GitHub info:
GITHUB_OWNER = "YOUR_GITHUB_USERNAME"
GITHUB_REPO = "YOUR_REPOSITORY_NAME"

FFMPEG_EXE = None
SPOTIFY_CLIENT_ID = None
SPOTIFY_CLIENT_SECRET = None

CREDENTIALS_PATH = os.path.join(os.path.expanduser("~"), ".spotify_dash_credentials.json")
FFMPEG_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".spotify_dash_ffmpeg.json")
STATS_PATH = os.path.join(os.path.expanduser("~"), ".spotify_dash_stats.json")

JUMP_BUFFER_TIME = 0.5          # how long jump input is buffered
JUMP_LEAD_TIME = 0.0            # spikes collide EXACTLY on the beat

STATS = None  # loaded at runtime
UPDATE_INFO = None  # set by GitHub check


# ============================================================
# VERSION / UPDATE CHECK
# ============================================================

def parse_version(v):
    """Parse '1.2.3' → (1,2,3). Unknown format → (0,) so it never crashes."""
    try:
        v = v.strip()
        if v.lower().startswith("v"):
            v = v[1:]
        parts = v.split(".")
        nums = []
        for p in parts:
            nums.append(int("".join(ch for ch in p if ch.isdigit()) or "0"))
        return tuple(nums)
    except Exception:
        return (0,)


def compare_versions(a, b):
    """Return -1 if a<b, 0 if =, 1 if > (semantic-ish)."""
    va = list(parse_version(a))
    vb = list(parse_version(b))
    n = max(len(va), len(vb))
    va.extend([0] * (n - len(va)))
    vb.extend([0] * (n - len(vb)))
    for x, y in zip(va, vb):
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


def check_for_update():
    """Check GitHub latest release; set UPDATE_INFO if we are out-of-date."""
    global UPDATE_INFO

    if (not GITHUB_OWNER) or (not GITHUB_REPO):
        return
    if "YOUR_GITHUB" in GITHUB_OWNER or "YOUR_REPOSITORY" in GITHUB_REPO:
        return

    try:
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        data = r.json()
        tag = data.get("tag_name") or data.get("name", "")
        latest_version = tag.lstrip("vV") if tag else None
        html_url = data.get("html_url") or f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

        if not latest_version:
            return

        if compare_versions(APP_VERSION, latest_version) < 0:
            UPDATE_INFO = {
                "current": APP_VERSION,
                "latest": latest_version,
                "url": html_url,
                "dismissed": False
            }
    except Exception:
        UPDATE_INFO = None


# ============================================================
# STATS HANDLING
# ============================================================

def load_stats():
    if not os.path.exists(STATS_PATH):
        return {
            "tracks": {},               # track_id -> {name, plays, best_percent}
            "total_songs_played": 0,
            "total_jumps": 0
        }
    try:
        with open(STATS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("tracks", {})
        data.setdefault("total_songs_played", 0)
        data.setdefault("total_jumps", 0)
        return data
    except Exception:
        return {
            "tracks": {},
            "total_songs_played": 0,
            "total_jumps": 0
        }


def save_stats():
    global STATS
    try:
        with open(STATS_PATH, "w", encoding="utf-8") as f:
            json.dump(STATS, f, indent=2)
    except Exception:
        pass


def ensure_stats_loaded():
    global STATS
    if STATS is None:
        STATS = load_stats()


def track_display_name(track):
    title = track.get("name", "Unknown Title")
    artists = ", ".join(a.get("name", "Unknown Artist") for a in track.get("artists", [])) or "Unknown Artist"
    return f"{title} – {artists}"


def update_stats_for_song(track, percent, jumps_this_run):
    global STATS
    ensure_stats_loaded()
    track_id = track.get("id") or track.get("uri") or track_display_name(track)
    disp = track_display_name(track)

    tdict = STATS["tracks"].setdefault(track_id, {
        "name": disp,
        "plays": 0,
        "best_percent": 0
    })

    tdict["name"] = disp
    tdict["plays"] = tdict.get("plays", 0) + 1
    if percent > tdict.get("best_percent", 0):
        tdict["best_percent"] = int(percent)

    STATS["total_songs_played"] = STATS.get("total_songs_played", 0) + 1
    STATS["total_jumps"] = STATS.get("total_jumps", 0) + int(jumps_this_run)

    save_stats()


# ============================================================
# SPOTIFY CREDENTIAL HANDLING
# ============================================================

def load_saved_credentials():
    try:
        if not os.path.exists(CREDENTIALS_PATH):
            return None, None
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cid = data.get("client_id") or data.get("SPOTIFY_CLIENT_ID")
        csec = data.get("client_secret") or data.get("SPOTIFY_CLIENT_SECRET")
        if cid and csec:
            return cid, csec
    except Exception:
        pass
    return None, None


def prompt_and_save_credentials():
    print("\n=== Spotify API Setup ===")
    print("Enter your Spotify API credentials.")
    print("They will be saved locally in:")
    print(f"  {CREDENTIALS_PATH}")
    print("and reused next time.\n")

    cid = input("Spotify Client ID: ").strip()
    csec = input("Spotify Client Secret: ").strip()

    data = {
        "client_id": cid,
        "client_secret": csec
    }

    try:
        os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)
        with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print("\nSaved Spotify credentials.\n")
    except Exception as e:
        print(f"[WARN] Could not save credentials: {e}")

    return cid, csec


def ensure_spotify_credentials():
    global SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET

    cid, csec = load_saved_credentials()
    if not cid or not csec:
        cid, csec = prompt_and_save_credentials()

    SPOTIFY_CLIENT_ID = cid
    SPOTIFY_CLIENT_SECRET = csec


# ============================================================
# FFMPEG PATH HANDLING
# ============================================================

def load_saved_ffmpeg_path():
    try:
        if not os.path.exists(FFMPEG_CONFIG_PATH):
            return None
        with open(FFMPEG_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        path = data.get("ffmpeg_path")
        if path:
            return path
    except Exception:
        pass
    return None


def ffmpeg_looks_valid(path):
    if not path:
        return False
    if not os.path.isfile(path):
        return False
    try:
        subprocess.run(
            [path, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False
        )
        return True
    except Exception:
        return False


def prompt_and_save_ffmpeg_path():
    print("\n=== FFmpeg Setup ===")
    print("FFmpeg is required to convert audio to WAV.")
    print("Enter the full path to your ffmpeg.exe (for example: C:\\ffmpeg\\bin\\ffmpeg.exe).")
    print("This will be saved locally in:")
    print(f"  {FFMPEG_CONFIG_PATH}")
    print("and reused next time.\n")

    while True:
        path = input("Full path to ffmpeg.exe: ").strip().strip('"')
        if ffmpeg_looks_valid(path):
            break
        print("That path doesn't look valid. Please try again.\n")

    data = {"ffmpeg_path": path}
    try:
        os.makedirs(os.path.dirname(FFMPEG_CONFIG_PATH), exist_ok=True)
        with open(FFMPEG_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print("\nSaved FFmpeg path.\n")
    except Exception as e:
        print(f"[WARN] Could not save FFmpeg path: {e}")

    return path


def ensure_ffmpeg_path():
    global FFMPEG_EXE

    saved = load_saved_ffmpeg_path()
    if ffmpeg_looks_valid(saved):
        FFMPEG_EXE = saved
        return

    FFMPEG_EXE = prompt_and_save_ffmpeg_path()


# ============================================================
# SPOTIFY CLIENT
# ============================================================

def get_spotify_client():
    return spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET
        )
    )


# ============================================================
# MAIN MENU (START / STATS) + UPDATE UI
# ============================================================

def main_menu():
    scr = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Spotify Dash")
    W, H = scr.get_size()
    clock = pygame.time.Clock()

    title_font = pygame.font.SysFont("Arial", 64, bold=True)
    btn_font = pygame.font.SysFont("Arial", 36)
    small_font = pygame.font.SysFont("Arial", 22)

    start_btn = pygame.Rect(W // 2 - 180, H // 2 - 40, 360, 70)
    stats_btn = pygame.Rect(W // 2 - 180, H // 2 + 50, 360, 70)

    while True:
        # Precompute update panel/button rects so events use the same ones
        update_btn = None
        know_btn = None
        panel_rect = None

        show_update = UPDATE_INFO and not UPDATE_INFO.get("dismissed", False)
        if show_update:
            panel_w, panel_h = 520, 180
            panel_rect = pygame.Rect(
                W // 2 - panel_w // 2,
                H // 2 + 150,
                panel_w,
                panel_h
            )
            update_btn = pygame.Rect(panel_rect.x + 40, panel_rect.y + panel_h - 70, 180, 50)
            know_btn = pygame.Rect(panel_rect.x + panel_w - 40 - 180, panel_rect.y + panel_h - 70, 180, 50)

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
                return "quit"

            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    try:
                        pygame.mixer.music.stop()
                    except Exception:
                        pass
                    return "quit"
                if e.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_s):
                    return "start"
                if e.key == pygame.K_t:
                    return "stats"

            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                mx, my = e.pos
                if start_btn.collidepoint(mx, my):
                    return "start"
                if stats_btn.collidepoint(mx, my):
                    return "stats"

                if show_update:
                    if update_btn and update_btn.collidepoint(mx, my):
                        url = UPDATE_INFO.get("url")
                        if url:
                            webbrowser.open(url)
                    if know_btn and know_btn.collidepoint(mx, my):
                        UPDATE_INFO["dismissed"] = True

        scr.fill((10, 10, 18))

        title = title_font.render("Spotify Dash", True, (240, 240, 255))
        scr.blit(title, (W // 2 - title.get_width() // 2, H // 2 - 190))

        vtxt = small_font.render(f"Version {APP_VERSION}", True, (180, 180, 200))
        scr.blit(vtxt, (10, H - 30))

        pygame.draw.rect(scr, (40, 140, 240), start_btn, border_radius=12)
        pygame.draw.rect(scr, (20, 90, 180), start_btn, 3, border_radius=12)
        s_txt = btn_font.render("Start", True, (255, 255, 255))
        scr.blit(s_txt, (start_btn.centerx - s_txt.get_width() // 2,
                         start_btn.centery - s_txt.get_height() // 2))

        pygame.draw.rect(scr, (60, 180, 120), stats_btn, border_radius=12)
        pygame.draw.rect(scr, (30, 120, 80), stats_btn, 3, border_radius=12)
        t_txt = btn_font.render("Stats", True, (255, 255, 255))
        scr.blit(t_txt, (stats_btn.centerx - t_txt.get_width() // 2,
                         stats_btn.centery - t_txt.get_height() // 2))

        if show_update and panel_rect:
            pygame.draw.rect(scr, (25, 25, 40), panel_rect, border_radius=10)
            pygame.draw.rect(scr, (120, 80, 40), panel_rect, 2, border_radius=10)

            txt_lines = [
                "You are using an old version of Spotify Dash.",
                f"Current: {UPDATE_INFO.get('current', '?')}   Latest: {UPDATE_INFO.get('latest', '?')}"
            ]
            y = panel_rect.y + 10
            for line in txt_lines:
                line_surf = small_font.render(line, True, (240, 230, 220))
                scr.blit(line_surf, (panel_rect.x + 20, y))
                y += 26

            pygame.draw.rect(scr, (80, 160, 80), update_btn, border_radius=10)
            pygame.draw.rect(scr, (40, 110, 40), update_btn, 2, border_radius=10)
            u_txt = small_font.render("Update", True, (255, 255, 255))
            scr.blit(u_txt, (update_btn.centerx - u_txt.get_width() // 2,
                             update_btn.centery - u_txt.get_height() // 2))

            pygame.draw.rect(scr, (120, 120, 120), know_btn, border_radius=10)
            pygame.draw.rect(scr, (80, 80, 80), know_btn, 2, border_radius=10)
            k_txt = small_font.render("I Know", True, (255, 255, 255))
            scr.blit(k_txt, (know_btn.centerx - k_txt.get_width() // 2,
                             know_btn.centery - k_txt.get_height() // 2))

        pygame.display.flip()
        clock.tick(60)


# ============================================================
# STATS SCREEN
# ============================================================

def show_stats_screen():
    ensure_stats_loaded()
    scr = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Spotify Dash – Stats")
    W, H = scr.get_size()
    clock = pygame.time.Clock()

    title_font = pygame.font.SysFont("Arial", 56, bold=True)
    header_font = pygame.font.SysFont("Arial", 32, bold=True)
    text_font = pygame.font.SysFont("Arial", 24)

    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
                return
            if e.type == pygame.KEYDOWN:
                if e.key in (pygame.K_ESCAPE, pygame.K_RETURN, pygame.K_SPACE):
                    return
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                return

        scr.fill((8, 8, 14))

        title = title_font.render("Stats", True, (240, 240, 255))
        scr.blit(title, (W // 2 - title.get_width() // 2, 40))

        ts = STATS.get("total_songs_played", 0)
        tj = STATS.get("total_jumps", 0)

        overall_y = 130
        overall1 = text_font.render(f"Total songs played: {ts}", True, (220, 220, 240))
        overall2 = text_font.render(f"Total jumps: {tj}", True, (220, 220, 240))
        scr.blit(overall1, (60, overall_y))
        scr.blit(overall2, (60, overall_y + 35))

        tracks = list(STATS.get("tracks", {}).values())

        most_played = sorted(tracks, key=lambda t: t.get("plays", 0), reverse=True)[:7]
        best_percent = sorted(tracks, key=lambda t: t.get("best_percent", 0), reverse=True)[:7]

        left_x = 60
        left_y = 210
        h1 = header_font.render("Most played songs", True, (230, 230, 255))
        scr.blit(h1, (left_x, left_y))
        y = left_y + 40
        if most_played:
            for t in most_played:
                line = f"{t.get('name','Unknown')}  (plays: {t.get('plays',0)})"
                surf = text_font.render(line, True, (210, 210, 230))
                scr.blit(surf, (left_x, y))
                y += 30
        else:
            surf = text_font.render("No data yet. Play some songs!", True, (210, 210, 230))
            scr.blit(surf, (left_x, y))

        right_x = W // 2 + 40
        right_y = 210
        h2 = header_font.render("Best percentages", True, (230, 230, 255))
        scr.blit(h2, (right_x, right_y))
        y2 = right_y + 40
        if best_percent:
            for t in best_percent:
                bp = int(t.get("best_percent", 0))
                line = f"{t.get('name','Unknown')}  ({bp}%)"
                surf = text_font.render(line, True, (210, 210, 230))
                scr.blit(surf, (right_x, y2))
                y2 += 30
        else:
            surf = text_font.render("No data yet. Play some songs!", True, (210, 210, 230))
            scr.blit(surf, (right_x, y2))

        hint = text_font.render("Press ESC / Enter / Click to go back", True, (180, 180, 200))
        scr.blit(hint, (W // 2 - hint.get_width() // 2, H - 60))

        pygame.display.flip()
        clock.tick(60)


# ============================================================
# UI SEARCH SCREEN
# ============================================================

def live_search_screen():
    pygame.init()
    scr = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Spotify Dash – Song Picker")
    W, H = scr.get_size()
    clock = pygame.time.Clock()

    font_big = pygame.font.SysFont("Arial", 48)
    font_small = pygame.font.SysFont("Arial", 32)
    font_result = pygame.font.SysFont("Arial", 26)

    sp = get_spotify_client()

    query = ""
    results = []

    while True:
        selected = None

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
                pygame.quit()
                sys.exit()

            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    return None

                if ev.key == pygame.K_BACKSPACE:
                    query = query[:-1]
                else:
                    if ev.unicode.isprintable():
                        query += ev.unicode

                try:
                    if query.strip():
                        res = sp.search(q=query, type="track", limit=10)
                        results = res["tracks"]["items"]
                    else:
                        results = []
                except Exception:
                    results = []

            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                mx, my = ev.pos
                sy = 230
                for i, t in enumerate(results):
                    ry = sy + i * 55
                    if ry <= my <= ry + 50:
                        selected = t

        scr.fill((12, 12, 18))

        title = font_big.render("Search Song", True, (240, 240, 255))
        scr.blit(title, (W // 2 - title.get_width() // 2, 70))

        bar = pygame.Rect(W // 2 - 400, 150, 800, 50)
        pygame.draw.rect(scr, (35, 35, 55), bar)
        txt = font_small.render(query, True, (230, 230, 245))
        scr.blit(txt, (bar.x + 10, bar.y + 10))

        sy = 230
        for i, t in enumerate(results):
            name = t["name"]
            artists = ", ".join(a["name"] for a in t["artists"])
            line = f"{i+1}. {name} – {artists}"
            rtxt = font_result.render(line, True, (210, 210, 225))
            scr.blit(rtxt, (W // 2 - 380, sy + i * 55))

        pygame.display.flip()
        clock.tick(60)

        if selected:
            return selected


# ============================================================
# LOADING SCREEN
# ============================================================

def show_loading_screen(message="Loading..."):
    scr = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Spotify Dash – Loading")
    W, H = scr.get_size()
    font = pygame.font.SysFont("Arial", 40)

    scr.fill((6, 6, 10))
    txt = font.render(message, True, (235, 235, 250))
    scr.blit(txt, (W // 2 - txt.get_width() // 2, H // 2 - txt.get_height() // 2))
    pygame.display.flip()


# ============================================================
# TRACK METADATA (TITLE / ARTIST / COVER)
# ============================================================

def get_track_metadata(track):
    title = track.get("name", "Unknown Title")
    artist_str = ", ".join(a.get("name", "Unknown Artist") for a in track.get("artists", [])) or "Unknown Artist"
    cover_surf = None

    try:
        images = track.get("album", {}).get("images", [])
        if images:
            url = images[0]["url"]
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            img_bytes = io.BytesIO(resp.content)
            cover_surf = pygame.image.load(img_bytes).convert_alpha()
    except Exception:
        cover_surf = None

    return title, artist_str, cover_surf


# ============================================================
# AUDIO DOWNLOAD (always YouTube)
# ============================================================

def download_youtube_audio(query):
    print("\nSearching YouTube…")
    s = Search(query)
    if not s.results:
        return None
    v = s.results[0]
    print("Using:", v.watch_url)
    stream = (
        v.streams.filter(only_audio=True)
        .order_by("abr").desc().first()
    )
    if not stream:
        return None
    return stream.download(output_path=tempfile.gettempdir())


# ============================================================
# WAV CONVERSION
# ============================================================

def ensure_wav(path):
    base, ext = os.path.splitext(path)
    if ext.lower() == ".wav":
        return path

    out = base + "_conv.wav"
    print("\nConverting → WAV…")
    cmd = [FFMPEG_EXE, "-y", "-i", path, "-ac", "1", "-ar", "44100", out]

    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return out
    except FileNotFoundError:
        print(f"[WARN] FFmpeg not found at:\n  {FFMPEG_EXE}")
        print("[WARN] Using original file (may fail for non-WAV).")
        return path
    except subprocess.CalledProcessError:
        print("[WARN] FFmpeg failed; using original file.")
        return path


# ============================================================
# BEAT + ENVELOPE ANALYSIS
# ============================================================

def analyze_beats(path):
    wav = ensure_wav(path)
    print("\nAnalyzing beats…")
    y, sr = librosa.load(wav, sr=None, mono=True)
    tempo, frames = librosa.beat.beat_track(y=y, sr=sr)

    tempo = float(np.array(tempo).flatten()[0])
    beats = librosa.frames_to_time(frames, sr=sr)
    dur = librosa.get_duration(y=y, sr=sr)

    hop_length = 1024
    frame_length = 2048
    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop_length)[0]
    env_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

    rms = rms.astype(float)
    if rms.max() > 0:
        rms = (rms - rms.min()) / (rms.max() - rms.min() + 1e-9)
    else:
        rms[:] = 0.0

    print(f"Tempo {tempo:.1f} BPM | Beats: {len(beats)}")
    return beats.tolist(), dur, wav, env_times.tolist(), rms.tolist(), tempo


def sample_envelope(env_times, env_vals, t):
    if not env_times:
        return 0.0
    if t <= env_times[0]:
        return env_vals[0]
    if t >= env_times[-1]:
        return env_vals[-1]

    i = bisect.bisect_left(env_times, t)
    if i <= 0:
        return env_vals[0]
    if i >= len(env_times):
        return env_vals[-1]

    t0, t1 = env_times[i - 1], env_times[i]
    v0, v1 = env_vals[i - 1], env_vals[i]
    if t1 <= t0:
        return v1
    alpha = (t - t0) / (t1 - t0)
    return v0 + alpha * (v1 - v0)


# ============================================================
# DIFFICULTY SCREEN
# ============================================================

def select_difficulty(all_beats):
    scr = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Spotify Dash – Difficulty")
    W, H = scr.get_size()
    clock = pygame.time.Clock()
    big = pygame.font.SysFont("Arial", 48)
    small = pygame.font.SysFont("Arial", 28)

    choice_beats = None

    while choice_beats is None:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
                pygame.quit(); sys.exit()
            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    return None
                if e.key == pygame.K_1:
                    choice_beats = all_beats[::3] or all_beats
                elif e.key == pygame.K_2:
                    choice_beats = all_beats[::2] or all_beats
                elif e.key == pygame.K_3:
                    choice_beats = all_beats

        scr.fill((8, 8, 14))

        t = big.render("Select Difficulty", True, (240, 240, 255))
        scr.blit(t, (W // 2 - t.get_width() // 2, 100))

        o1 = small.render("1 — Easy (1/3 beats)", True, (180, 230, 180))
        o2 = small.render("2 — Normal (1/2 beats)", True, (230, 230, 180))
        o3 = small.render("3 — Hard (all beats)", True, (230, 180, 180))

        scr.blit(o1, (W // 2 - o1.get_width() // 2, 220))
        scr.blit(o2, (W // 2 - o2.get_width() // 2, 270))
        scr.blit(o3, (W // 2 - o3.get_width() // 2, 320))

        pygame.display.flip()
        clock.tick(60)

    return choice_beats


# ============================================================
# LEVEL OBJECTS (SPIKES + PLATFORMS)
# ============================================================

class Spike:
    def __init__(self, collision_time, height_blocks=0):
        self.collision_time = collision_time
        self.height_blocks = height_blocks

    def x(self, now, speed):
        return (self.collision_time - now) * speed + 250


class Platform:
    def __init__(self, collision_time):
        self.collision_time = collision_time

    def x(self, now, speed):
        return (self.collision_time - now) * speed + 250


def build_level(jump_beats):
    spikes = []
    platforms = []

    for i, b in enumerate(jump_beats):
        collision_t = b + JUMP_LEAD_TIME

        if i % 6 in (2, 5):
            platforms.append(Platform(collision_t))
        elif i % 8 == 4:
            spikes.append(Spike(collision_t, height_blocks=1))  # overhead
        else:
            spikes.append(Spike(collision_t, height_blocks=0))  # ground

    return spikes, platforms


def draw_spike(screen, x, ground_y, height_blocks):
    if height_blocks <= 0:
        base_y = ground_y
        pts = [(x, base_y), (x + 45, base_y), (x + 23, base_y - 50)]
        pygame.draw.polygon(screen, (230, 60, 60), pts)
        return pygame.Rect(x, base_y - 50, 45, 50)
    else:
        bottom_y = ground_y - height_blocks * 60
        top_y = bottom_y - 50
        pts = [(x, top_y), (x + 45, top_y), (x + 23, top_y + 50)]
        pygame.draw.polygon(screen, (230, 60, 60), pts)
        return pygame.Rect(x, top_y, 45, 50)


def get_platform_rect(x, ground_y):
    height = 60
    width = 120
    top = ground_y - height
    return pygame.Rect(x, top, width, height)


def draw_platform(screen, rect):
    pygame.draw.rect(screen, (60, 150, 100), rect)
    pygame.draw.rect(screen, (30, 90, 60), rect, 2)


# ============================================================
# BACKGROUND (DARK COLORED SQUARE GRID)
# ============================================================

def draw_background(scr, W, H, scroll_time, speed, base_color, dark_color):
    tile_size = 80
    gap = 6

    scr.fill(dark_color)
    offset_x = int((scroll_time * speed * 0.4) % tile_size)

    for x in range(-tile_size, W + tile_size, tile_size):
        for y in range(0, H + tile_size, tile_size):
            rect = pygame.Rect(
                x - offset_x + gap,
                y + gap,
                tile_size - 2 * gap,
                tile_size - 2 * gap,
            )
            pygame.draw.rect(scr, base_color, rect)


# ============================================================
# PAUSE MENU
# ============================================================

def pause_menu(scr, W, H):
    clock = pygame.time.Clock()
    title_font = pygame.font.SysFont("Arial", 48, bold=True)
    btn_font = pygame.font.SysFont("Arial", 32)

    overlay = pygame.Surface((W, H), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, 150))

    resume_btn = pygame.Rect(W // 2 - 150, H // 2 - 40, 300, 60)
    exit_btn = pygame.Rect(W // 2 - 150, H // 2 + 40, 300, 60)

    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                return "exit"
            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    return "resume"
                if e.key in (pygame.K_r, pygame.K_RETURN, pygame.K_SPACE):
                    return "resume"
                if e.key == pygame.K_x:
                    return "exit"
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                mx, my = e.pos
                if resume_btn.collidepoint(mx, my):
                    return "resume"
                if exit_btn.collidepoint(mx, my):
                    return "exit"

        scr.blit(overlay, (0, 0))

        title = title_font.render("Paused", True, (255, 255, 255))
        scr.blit(title, (W // 2 - title.get_width() // 2, H // 2 - 120))

        pygame.draw.rect(scr, (60, 160, 240), resume_btn, border_radius=10)
        pygame.draw.rect(scr, (30, 100, 180), resume_btn, 3, border_radius=10)
        r_txt = btn_font.render("Resume", True, (255, 255, 255))
        scr.blit(r_txt, (resume_btn.centerx - r_txt.get_width() // 2,
                         resume_btn.centery - r_txt.get_height() // 2))

        pygame.draw.rect(scr, (240, 90, 90), exit_btn, border_radius=10)
        pygame.draw.rect(scr, (180, 40, 40), exit_btn, 3, border_radius=10)
        e_txt = btn_font.render("Exit to Song Picker", True, (255, 255, 255))
        scr.blit(e_txt, (exit_btn.centerx - e_txt.get_width() // 2,
                         exit_btn.centery - e_txt.get_height() // 2))

        pygame.display.flip()
        clock.tick(60)


# ============================================================
# GAME LOOP
# ============================================================

def run_game(path, jump_beats, dur, start_delay,
             song_title, song_artist, cover_surf,
             env_times, env_vals):
    pygame.init()
    pygame.mixer.init()

    scr = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Spotify Dash – Playing")
    W, H = scr.get_size()
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("Arial", 32)
    meta_title_font = pygame.font.SysFont("Arial", 24, bold=True)
    meta_artist_font = pygame.font.SysFont("Arial", 20)

    ground_y = H - 120
    px = 250
    py = H - 160
    vy = 0
    gravity = 0.38
    jump = -11
    speed = 330
    player_h = 50

    base_color = (
        random.randint(40, 140),
        random.randint(40, 140),
        random.randint(40, 140),
    )
    dark_color = tuple(int(c * 0.35) for c in base_color)

    cover_thumb = None
    if cover_surf is not None:
        cover_thumb = pygame.transform.smoothscale(cover_surf, (128, 128))

    spikes, platforms = build_level(jump_beats)

    title_surf = meta_title_font.render(song_title, True, (240, 240, 255))
    artist_surf = meta_artist_font.render(song_artist, True, (210, 210, 230))

    pygame.mixer.music.load(path)

    level_start_ms = pygame.time.get_ticks()
    audio_started = False
    music_start_ms = None

    n_bars = 40
    bar_factors_start = [random.uniform(0.4, 1.3) for _ in range(n_bars)]
    bar_factors_target = bar_factors_start[:]
    randomize_interval = 0.4
    morph_duration = 0.2
    last_randomize_time = -999.0
    morph_start_time = 0.0

    game_over = False
    jump_req = None
    jumps_this_run = 0
    last_percent = 0

    while True:
        now_ms = pygame.time.get_ticks()
        dt = clock.tick(120) / 1000.0

        game_elapsed = (now_ms - level_start_ms) / 1000.0

        if (not audio_started) and game_elapsed >= start_delay:
            pygame.mixer.music.play()
            audio_started = True
            music_start_ms = pygame.time.get_ticks()

        if audio_started:
            elapsed_audio = (pygame.time.get_ticks() - music_start_ms) / 1000.0
        else:
            elapsed_audio = 0.0

        song_time = game_elapsed - start_delay
        logic_time = max(song_time, 0.0)

        py_prev = py

        if audio_started and (not pygame.mixer.music.get_busy()) and not game_over:
            return "song_end", 100, jumps_this_run

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
                pygame.quit()
                return "quit", last_percent, jumps_this_run

            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE and not game_over:
                    pygame.mixer.music.pause()
                    choice = pause_menu(scr, W, H)
                    if choice == "resume":
                        if audio_started:
                            music_start_ms = pygame.time.get_ticks() - int(elapsed_audio * 1000)
                            pygame.mixer.music.unpause()
                        continue
                    elif choice == "exit":
                        pygame.mixer.music.stop()
                        return "esc", last_percent, jumps_this_run

                if e.key in (pygame.K_SPACE, pygame.K_UP):
                    jump_req = logic_time

                if game_over:
                    if e.key == pygame.K_r:
                        pygame.mixer.music.stop()
                        return "restart", last_percent, jumps_this_run
                    if e.key == pygame.K_t:
                        pygame.mixer.music.stop()
                        return "change_diff", last_percent, jumps_this_run

            if e.type == pygame.MOUSEBUTTONDOWN and not game_over:
                if e.button == 1:
                    jump_req = logic_time

        if not game_over:
            vy += gravity
            py += vy

        platform_draw_data = []
        new_platforms = []
        for pf in platforms:
            x = pf.x(song_time, speed)
            if x > -140:
                rect = get_platform_rect(x, ground_y)
                platform_draw_data.append((pf, rect))
                new_platforms.append(pf)
        platforms = new_platforms

        effective_ground = ground_y
        standing = False

        player_bottom_prev = py_prev + player_h
        player_bottom_now = py + player_h
        player_rect_for_check = pygame.Rect(px, int(py), 50, player_h)

        if vy >= 0:
            for pf, rect in platform_draw_data:
                if player_bottom_prev <= rect.top <= player_bottom_now:
                    if (player_rect_for_check.right > rect.left) and (player_rect_for_check.left < rect.right):
                        py = rect.top - player_h
                        vy = 0
                        effective_ground = rect.top
                        standing = True
                        break

        if py >= effective_ground - player_h:
            py = effective_ground - player_h
            vy = 0
            standing = True

        if jump_req is not None and not game_over:
            if (logic_time - jump_req) <= JUMP_BUFFER_TIME:
                if standing:
                    vy = jump
                    py = effective_ground - player_h
                    jumps_this_run += 1
                    jump_req = None
            else:
                jump_req = None

        scroll_time = song_time
        draw_background(scr, W, H, scroll_time, speed, base_color, dark_color)

        env_t = max(song_time, 0.0)
        amp = sample_envelope(env_times, env_vals, env_t)

        if env_t - last_randomize_time >= randomize_interval:
            bar_factors_start = bar_factors_target[:]
            bar_factors_target = [random.uniform(0.4, 1.3) for _ in range(n_bars)]
            morph_start_time = env_t
            last_randomize_time = env_t

        if morph_duration > 0:
            progress = (env_t - morph_start_time) / morph_duration
            progress = max(0.0, min(1.0, progress))
        else:
            progress = 1.0

        bar_width = max(4, W // n_bars)
        max_height = H - ground_y - 8

        for i in range(n_bars):
            factor = (bar_factors_start[i] * (1.0 - progress) +
                      bar_factors_target[i] * progress)

            bar_max_height = factor * max_height
            bar_height = int(amp * bar_max_height)

            top = H - bar_height
            if top < ground_y + 2:
                top = ground_y + 2
                bar_height = H - top
            x = i * bar_width
            if bar_height > 0:
                c0 = min(base_color[0] + 40, 255)
                c1 = min(base_color[1] + 40, 255)
                c2 = min(base_color[2] + 40, 255)
                rect = pygame.Rect(x, top, bar_width - 2, bar_height)
                pygame.draw.rect(scr, (c0, c1, c2), rect)

        pygame.draw.line(scr, (180, 180, 190), (0, ground_y), (W, ground_y), 4)

        player = pygame.Rect(px, int(py), 50, player_h)
        pygame.draw.rect(scr, (0, 150, 210), player)

        new_spikes = []
        for sp in spikes:
            x = sp.x(song_time, speed)
            if x > -80:
                hit_rect = draw_spike(scr, x, ground_y, sp.height_blocks)
                new_spikes.append(sp)
                if player.colliderect(hit_rect):
                    game_over = True
        spikes = new_spikes

        for pf, rect in platform_draw_data:
            draw_platform(scr, rect)
            if player.colliderect(rect):
                if player.bottom <= rect.top + 2:
                    pass
                else:
                    game_over = True

        meta_rect = pygame.Rect(10, 10, 360, 140)
        pygame.draw.rect(scr, (10, 10, 18), meta_rect)
        pygame.draw.rect(scr, (60, 60, 90), meta_rect, 2)

        if cover_thumb is not None:
            scr.blit(cover_thumb, (meta_rect.x + 8, meta_rect.y + 6))
            text_x = meta_rect.x + 8 + 128 + 12
        else:
            text_x = meta_rect.x + 12

        scr.blit(title_surf, (text_x, meta_rect.y + 20))
        scr.blit(artist_surf, (text_x, meta_rect.y + 60))

        if dur > 0:
            frac = env_t / dur
            frac = max(0.0, min(1.0, frac))
            percent = int(frac * 100)
        else:
            percent = 0
        last_percent = percent

        percent_surf = font.render(f"{percent}%", True, (200, 255, 200))
        scr.blit(percent_surf, (W // 2 - percent_surf.get_width() // 2, 10))

        if game_over:
            t1 = font.render("GAME OVER", True, (255, 90, 90))
            t2 = font.render("R = Restart, T = Change Difficulty", True, (235, 235, 245))
            scr.blit(t1, (W // 2 - t1.get_width() // 2, H // 2 - 60))
            scr.blit(t2, (W // 2 - t2.get_width() // 2, H // 2))

        pygame.display.flip()


# ============================================================
# MAIN
# ============================================================

def main():
    pygame.init()

    ensure_spotify_credentials()
    ensure_ffmpeg_path()
    ensure_stats_loaded()
    check_for_update()

    while True:
        menu_choice = main_menu()
        if menu_choice == "quit":
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
            pygame.quit()
            sys.exit()
        if menu_choice == "stats":
            show_stats_screen()
            continue
        if menu_choice != "start":
            continue

        while True:
            track = live_search_screen()
            if track is None:
                break

            song_title, song_artist, cover_surf = get_track_metadata(track)

            show_loading_screen("Loading song...")

            name = track_display_name(track)
            print("\nUsing YouTube audio for:", name)
            audio = download_youtube_audio(name)
            if not audio:
                continue

            show_loading_screen("Analyzing beat...")

            all_beats, dur, wav, env_times, env_vals, tempo = analyze_beats(audio)
            if not all_beats:
                continue

            if tempo <= 0:
                beat_duration = 0.5
            else:
                beat_duration = 60.0 / tempo

            start_delay = 2.0 * beat_duration

            while True:
                jump_beats = select_difficulty(all_beats)
                if jump_beats is None:
                    break

                while True:
                    result, percent, jumps = run_game(
                        wav,
                        jump_beats,
                        dur,
                        start_delay,
                        song_title,
                        song_artist,
                        cover_surf,
                        env_times,
                        env_vals
                    )

                    update_stats_for_song(track, percent, jumps)

                    if result == "quit":
                        try:
                            pygame.mixer.music.stop()
                        except Exception:
                            pass
                        pygame.quit()
                        sys.exit()

                    if result == "song_end":
                        result = "esc"

                    if result == "esc":
                        break

                    if result == "change_diff":
                        break

                    if result == "restart":
                        continue

                    break

                if result == "esc":
                    break

                if result == "change_diff":
                    continue

                break

            break


if __name__ == "__main__":
    main()
