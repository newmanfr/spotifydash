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
from spotipy.oauth2 import SpotifyClientCredentials
from pytubefix import Search

# ============================================================
# CONFIG
# ============================================================

FFMPEG_EXE = r"ENTER PATH HERE"

# These will be filled at runtime from saved credentials / user input
SPOTIFY_CLIENT_ID = None
SPOTIFY_CLIENT_SECRET = None

# Where to store Spotify credentials (local, not committed)
CREDENTIALS_PATH = os.path.join(os.path.expanduser("~"), ".spotify_dash_credentials.json")

JUMP_BUFFER_TIME = 0.5          # how long jump input is buffered
JUMP_LEAD_TIME = 0.25           # time between beat (jump) and spike collision
PRE_BEAT_START_GAP = 2.0        # preferred: start 2s before first beat


# ============================================================
# SPOTIFY CREDENTIAL HANDLING
# ============================================================

def load_saved_credentials():
    """
    Try to load Spotify credentials from a JSON file in the user's home dir.
    Returns (client_id, client_secret) or (None, None) if not available/invalid.
    """
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
    """
    Ask the user for Spotify Client ID + Secret in the terminal,
    then save them to CREDENTIALS_PATH for future runs.
    """
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
        # Ensure directory exists (usually home already exists)
        os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)
        with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print("\nSaved credentials. You won't be asked again on this machine unless you delete that file.\n")
    except Exception as e:
        print(f"[WARN] Could not save credentials: {e}")

    return cid, csec


def ensure_spotify_credentials():
    """
    Ensure global SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are set,
    loading from file or prompting the user if needed.
    """
    global SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET

    cid, csec = load_saved_credentials()
    if not cid or not csec:
        cid, csec = prompt_and_save_credentials()

    SPOTIFY_CLIENT_ID = cid
    SPOTIFY_CLIENT_SECRET = csec


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
# UI SEARCH SCREEN
# ============================================================

def live_search_screen():
    """
    Returns a Spotify track dict, or None if ESC is pressed (to quit).
    """
    pygame.init()
    scr = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
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
                pygame.quit()
                sys.exit()

            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    # ESC at picker = exit whole game
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

        scr.fill((20, 20, 30))

        title = font_big.render("Search Song", True, (255, 255, 255))
        scr.blit(title, (W // 2 - title.get_width() // 2, 70))

        bar = pygame.Rect(W // 2 - 400, 150, 800, 50)
        pygame.draw.rect(scr, (50, 50, 70), bar)
        txt = font_small.render(query, True, (255, 255, 255))
        scr.blit(txt, (bar.x + 10, bar.y + 10))

        sy = 230
        for i, t in enumerate(results):
            name = t["name"]
            artists = ", ".join(a["name"] for a in t["artists"])
            # No preview info shown
            line = f"{i+1}. {name} – {artists}"
            rtxt = font_result.render(line, True, (220, 220, 220))
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
    W, H = scr.get_size()
    font = pygame.font.SysFont("Arial", 40)

    scr.fill((10, 10, 20))
    txt = font.render(message, True, (255, 255, 255))
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
            url = images[0]["url"]  # largest image
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            img_bytes = io.BytesIO(resp.content)
            cover_surf = pygame.image.load(img_bytes).convert_alpha()
    except Exception:
        cover_surf = None

    return title, artist_str, cover_surf


# ============================================================
# AUDIO DOWNLOAD (we always use YouTube for audio)
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
# BEAT ANALYSIS
# ============================================================

def analyze_beats(path):
    wav = ensure_wav(path)
    print("\nAnalyzing beats…")
    y, sr = librosa.load(wav, sr=None, mono=True)
    tempo, frames = librosa.beat.beat_track(y=y, sr=sr)

    tempo = float(np.array(tempo).flatten()[0])
    beats = librosa.frames_to_time(frames, sr=sr)
    dur = librosa.get_duration(y=y, sr=sr)

    print(f"Tempo {tempo:.1f} BPM | Beats: {len(beats)}")
    return beats.tolist(), dur, wav


# ============================================================
# DIFFICULTY SCREEN
# ============================================================

def select_difficulty(all_beats):
    """
    Returns filtered beats list (the beat times you jump on).
    If ESC pressed, returns None to go back to song picker.
    """
    scr = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    W, H = scr.get_size()
    clock = pygame.time.Clock()
    big = pygame.font.SysFont("Arial", 48)
    small = pygame.font.SysFont("Arial", 28)

    choice_beats = None

    while choice_beats is None:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
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

        scr.fill((15, 15, 25))

        t = big.render("Select Difficulty", True, (255, 255, 255))
        scr.blit(t, (W // 2 - t.get_width() // 2, 100))

        o1 = small.render("1 — Easy (1/3 beats)", True, (200, 255, 200))
        o2 = small.render("2 — Normal (1/2 beats)", True, (255, 255, 200))
        o3 = small.render("3 — Hard (all beats)", True, (255, 200, 200))

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
    """
    Spike is defined by the time at which it collides with the player
    if the player does nothing.

    height_blocks:
      0 -> ground spike (points UP from ground)
      1 -> overhead spike (points DOWN above player path)
    """
    def __init__(self, collision_time, height_blocks=0):
        self.collision_time = collision_time
        self.height_blocks = height_blocks

    def x(self, now, speed):
        return (self.collision_time - now) * speed + 250


class Platform:
    """
    Platform the player can land on.

    Always: 1 block tall, touching the ground,
    so player must jump ONTO it or collide with the front.
    """
    def __init__(self, collision_time):
        self.collision_time = collision_time

    def x(self, now, speed):
        return (self.collision_time - now) * speed + 250


def build_level(jump_beats):
    """
    jump_beats: times when the PLAYER should press jump (on beat).

    Pattern:
      - Some beats: ground spikes
      - Some beats: overhead spikes (player must go under)
      - Some beats: ground platforms (1 block tall touching ground, must jump onto)
    """
    spikes = []
    platforms = []

    for i, b in enumerate(jump_beats):
        collision_t = b + JUMP_LEAD_TIME

        if i % 6 in (2, 5):
            # ground platform 1-block tall
            platforms.append(Platform(collision_t))
        elif i % 8 == 4:
            # overhead spike above player: drawn DOWNWARDS
            spikes.append(Spike(collision_t, height_blocks=1))
        else:
            # normal ground spike
            spikes.append(Spike(collision_t, height_blocks=0))

    return spikes, platforms


def draw_spike(screen, x, ground_y, height_blocks):
    """
    Ground spikes (height_blocks==0): point UP from the ground.
    Overhead spikes (height_blocks>0): point DOWN from above the player.
    """
    if height_blocks <= 0:
        # UPWARD spike from ground
        base_y = ground_y
        pts = [(x, base_y), (x + 45, base_y), (x + 23, base_y - 50)]
        pygame.draw.polygon(screen, (255, 70, 70), pts)
        return pygame.Rect(x, base_y - 50, 45, 50)
    else:
        # DOWNWARD spike above player
        # bottom of spike sits height_blocks*60 above the ground
        bottom_y = ground_y - height_blocks * 60
        top_y = bottom_y - 50
        pts = [(x, top_y), (x + 45, top_y), (x + 23, top_y + 50)]
        pygame.draw.polygon(screen, (255, 70, 70), pts)
        return pygame.Rect(x, top_y, 45, 50)


def get_platform_rect(x, ground_y):
    """
    Platform: 1 block tall touching the ground.
    So it goes from ground_y - 60 up to ground_y.
    """
    height = 60
    width = 120
    top = ground_y - height
    return pygame.Rect(x, top, width, height)


def draw_platform(screen, rect):
    pygame.draw.rect(screen, (80, 200, 120), rect)
    pygame.draw.rect(screen, (40, 120, 70), rect, 2)


# ============================================================
# GAME LOOP
# ============================================================

def run_game(path, jump_beats, dur, time_offset, song_title, song_artist, cover_surf):
    """
    Returns:
      "esc"          → ESC pressed (go back to song picker)
      "change_diff"  → T pressed after death (change difficulty)
      "restart"      → R pressed after death (restart same diff)
      "quit"         → window closed (exit)
    """
    pygame.init()
    pygame.mixer.init()

    scr = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
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

    base_bg = (25, 25, 32)
    bg = base_bg

    spikes, platforms = build_level(jump_beats)

    # Prepare metadata visuals
    if cover_surf is not None:
        cover_surf = pygame.transform.smoothscale(cover_surf, (128, 128))
    title_surf = meta_title_font.render(song_title, True, (255, 255, 255))
    artist_surf = meta_artist_font.render(song_artist, True, (200, 200, 200))

    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    start_ms = pygame.time.get_ticks()

    game_over = False
    jump_req = None

    while True:
        dt = clock.tick(120) / 1000.0
        now = (pygame.time.get_ticks() - start_ms) / 1000.0 + time_offset
        py_prev = py

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                pygame.quit()
                return "quit"

            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    return "esc"

                if e.key in (pygame.K_SPACE, pygame.K_UP):
                    jump_req = now

                if game_over:
                    if e.key == pygame.K_r:
                        return "restart"
                    if e.key == pygame.K_t:
                        return "change_diff"

            if e.type == pygame.MOUSEBUTTONDOWN and not game_over:
                if e.button == 1:
                    jump_req = now

        if jump_req is not None:
            if now - jump_req <= JUMP_BUFFER_TIME:
                if py >= ground_y - player_h - 1:
                    vy = jump
                    jump_req = None
            else:
                jump_req = None

        if not game_over:
            vy += gravity
            py += vy

        # Platform positions for this frame
        platform_draw_data = []
        new_platforms = []
        for pf in platforms:
            x = pf.x(now, speed)
            if x > -140:
                rect = get_platform_rect(x, ground_y)
                platform_draw_data.append((pf, rect))
                new_platforms.append(pf)
        platforms = new_platforms

        # Landing on platforms
        effective_ground = ground_y
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
                        break

        # Base ground
        if py >= effective_ground - player_h:
            py = effective_ground - player_h
            vy = 0

        scr.fill(bg)
        pygame.draw.line(scr, (200, 200, 200), (0, ground_y), (W, ground_y), 4)

        player = pygame.Rect(px, int(py), 50, player_h)
        pygame.draw.rect(scr, (0, 180, 255), player)

        # Draw platforms and check side collisions (death if hit from side/bottom)
        for pf, rect in platform_draw_data:
            draw_platform(scr, rect)
            if player.colliderect(rect):
                # standing safely on top if feet are near top
                if player.bottom <= rect.top + 2:
                    pass
                else:
                    game_over = True

        # Spikes (ground up, overhead down)
        new_spikes = []
        for sp in spikes:
            x = sp.x(now, speed)
            if x > -80:
                hit_rect = draw_spike(scr, x, ground_y, sp.height_blocks)
                new_spikes.append(sp)
                if player.colliderect(hit_rect):
                    game_over = True
        spikes = new_spikes

        # Song metadata overlay (top-left)
        meta_rect = pygame.Rect(10, 10, 360, 140)
        pygame.draw.rect(scr, (15, 15, 25), meta_rect)
        pygame.draw.rect(scr, (60, 60, 80), meta_rect, 2)

        if cover_surf is not None:
            scr.blit(cover_surf, (meta_rect.x + 8, meta_rect.y + 6))
            text_x = meta_rect.x + 8 + 128 + 12
        else:
            text_x = meta_rect.x + 12

        scr.blit(title_surf, (text_x, meta_rect.y + 20))
        scr.blit(artist_surf, (text_x, meta_rect.y + 60))

        if game_over:
            t1 = font.render("GAME OVER", True, (255, 80, 80))
            t2 = font.render("R = Restart, T = Change Difficulty, ESC = Song Picker", True, (255, 255, 255))
            scr.blit(t1, (W // 2 - t1.get_width() // 2, H // 2 - 60))
            scr.blit(t2, (W // 2 - t2.get_width() // 2, H // 2))

        pygame.display.flip()


# ============================================================
# MAIN
# ============================================================

def main():
    pygame.init()

    # Ensure Spotify credentials are set up and stored
    ensure_spotify_credentials()

    while True:
        track = live_search_screen()
        if track is None:
            pygame.quit()
            sys.exit()

        song_title, song_artist, cover_surf = get_track_metadata(track)

        show_loading_screen("Loading song...")

        # Always use YouTube for audio based on Spotify track name + artists
        name = track["name"] + " " + " ".join(a["name"] for a in track["artists"])
        print("\nUsing YouTube audio for:", name)
        audio = download_youtube_audio(name)
        if not audio:
            continue

        show_loading_screen("Analyzing beat...")

        all_beats, dur, wav = analyze_beats(audio)
        if not all_beats:
            continue

        while True:
            jump_beats = select_difficulty(all_beats)
            if jump_beats is None:
                break

            first_beat = jump_beats[0]
            if first_beat >= PRE_BEAT_START_GAP:
                time_offset = first_beat - PRE_BEAT_START_GAP
            else:
                time_offset = max(0.0, first_beat - 1.0)

            while True:
                result = run_game(wav, jump_beats, dur, time_offset, song_title, song_artist, cover_surf)

                if result == "quit":
                    pygame.quit()
                    sys.exit()

                if result == "esc":
                    break

                if result == "change_diff":
                    break

                if result == "restart":
                    continue

                break

            if result in ("esc",):
                break

            if result == "change_diff":
                continue

            break


if __name__ == "__main__":
    main()
