"""Spotify -> YouTube Music playlist copier (Flet app, builds to an Android APK).

Uses the same libraries as sigma67/spotify_to_ytmusic (spotipy + ytmusicapi),
wrapped in a phone-friendly UI with no terminal needed.
"""
import json
import time

import flet as ft
import spotipy
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.oauth2 import SpotifyOAuth
from ytmusicapi import YTMusic
from ytmusicapi.auth.oauth import OAuthCredentials, RefreshingToken

# Spotify only allows loopback redirects. The page will fail to load on your
# phone after login; that is expected. You copy its address into the app.
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "playlist-read-private playlist-read-collaborative"


# ---------------------------------------------------------------- Spotify ---
def _items_page(sp, playlist_id, offset):
    """Spotify renamed /tracks to /items in Feb 2026; try the new one first."""
    last = None
    for path in ("items", "tracks"):
        try:
            return sp._get(
                f"playlists/{playlist_id}/{path}",
                limit=50,
                offset=offset,
                additional_types="track",
            )
        except spotipy.SpotifyException as err:
            last = err
    raise last


def spotify_tracks(sp, playlist_id):
    """Yield (title, [artists]) for every track in a playlist."""
    offset = 0
    while True:
        page = _items_page(sp, playlist_id, offset)
        for entry in page.get("items", []):
            track = entry.get("item") or entry.get("track")  # new/old field name
            if track and track.get("name"):
                yield track["name"], [a["name"] for a in track.get("artists", [])]
        if not page.get("next"):
            break
        offset += 50


# ---------------------------------------------------------- YouTube Music ---
def find_video_id(yt, title, artists):
    query = f"{title} {' '.join(artists[:2])}".strip()
    for kind in ("songs", "videos"):
        results = yt.search(query, filter=kind, limit=3)
        for r in results:
            if r.get("videoId"):
                return r["videoId"]
    return None


def copy_playlist(sp, yt, playlist_id, new_name, public, progress):
    tracks = list(spotify_tracks(sp, playlist_id))
    if not tracks:
        raise RuntimeError(
            "No tracks were returned. Spotify only shares tracks for playlists "
            "you own or collaborate on."
        )
    ids, missing = [], []
    for i, (title, artists) in enumerate(tracks, 1):
        vid = find_video_id(yt, title, artists)
        if vid:
            ids.append(vid)
        else:
            missing.append(f"{title} - {', '.join(artists)}")
        progress(i, len(tracks))
        time.sleep(0.15)
    if not ids:
        raise RuntimeError("None of the tracks could be matched on YouTube Music.")
    playlist = yt.create_playlist(
        new_name,
        "Copied from Spotify",
        privacy_status="PUBLIC" if public else "PRIVATE",
    )
    if not isinstance(playlist, str):
        raise RuntimeError(f"Could not create the playlist: {playlist}")
    for start in range(0, len(ids), 50):
        yt.add_playlist_items(playlist, ids[start : start + 50], duplicates=True)
    return len(ids), len(tracks), missing


# --------------------------------------------------------------------- UI ---
def main(page: ft.Page):
    page.title = "Spotify to YT Music"
    page.scroll = ft.ScrollMode.AUTO
    page.padding = ft.padding.only(left=16, right=16, top=40, bottom=24)

    state = {"sp": None, "sp_auth": None, "yt": None, "yt_creds": None, "yt_code": None}
    names = {}

    def load(key):
        try:
            return page.client_storage.get(key) or ""
        except Exception:
            return ""

    def save(key, value):
        try:
            page.client_storage.set(key, value)
        except Exception:
            pass

    # Spotify controls
    sp_id = ft.TextField(label="Spotify Client ID", value=load("sp_id"))
    sp_secret = ft.TextField(
        label="Spotify Client Secret",
        value=load("sp_secret"),
        password=True,
        can_reveal_password=True,
    )
    sp_url = ft.TextField(label="Paste the 127.0.0.1 address here")
    sp_status = ft.Text("Spotify: not connected")

    # YouTube Music controls
    g_id = ft.TextField(label="Google Client ID", value=load("g_id"))
    g_secret = ft.TextField(
        label="Google Client Secret",
        value=load("g_secret"),
        password=True,
        can_reveal_password=True,
    )
    yt_status = ft.Text("YouTube Music: not connected")

    # Convert controls
    playlist_dd = ft.Dropdown(label="Spotify playlist", options=[])
    new_name = ft.TextField(label="New playlist name on YouTube Music")
    public_sw = ft.Switch(label="Make public (default is private)", value=False)
    bar = ft.ProgressBar(value=0, visible=False)
    status = ft.Text("")
    missing_box = ft.Text("", selectable=True)

    def guarded(fn, target):
        def run(e):
            try:
                fn(e)
            except Exception as err:  # show any problem on screen
                target.value = f"Error: {err}"
                bar.visible = False
                page.update()

        return run

    # ---- Spotify login
    def make_sp_auth(token_info=None):
        return SpotifyOAuth(
            client_id=sp_id.value.strip(),
            client_secret=sp_secret.value.strip(),
            redirect_uri=REDIRECT_URI,
            scope=SCOPE,
            open_browser=False,
            cache_handler=MemoryCacheHandler(token_info=token_info),
        )

    def finish_spotify(auth):
        sp = spotipy.Spotify(auth_manager=auth)
        me = sp.me()["id"]
        rows = []
        res = sp.current_user_playlists(limit=50)
        while res:
            for p in res["items"]:
                if p and (p["owner"]["id"] == me or p.get("collaborative")):
                    rows.append((p["id"], p["name"]))
            res = sp.next(res) if res.get("next") else None
        names.clear()
        names.update(dict(rows))
        playlist_dd.options = [ft.dropdown.Option(key=k, text=v) for k, v in rows]
        state["sp"] = sp
        save("sp_token", json.dumps(auth.cache_handler.get_cached_token()))
        sp_status.value = f"Spotify: connected ({len(rows)} playlists)"
        page.update()

    def sp_login(e):
        save("sp_id", sp_id.value.strip())
        save("sp_secret", sp_secret.value.strip())
        auth = make_sp_auth()
        state["sp_auth"] = auth
        sp_status.value = (
            "Log in and approve. The page that opens will fail to load. Copy its "
            "full address (starts with http://127.0.0.1), come back and paste it below."
        )
        page.update()
        page.launch_url(auth.get_authorize_url())

    def sp_connect(e):
        auth = state["sp_auth"]
        if auth is None:
            sp_status.value = "Tap 'Open Spotify login' first."
            page.update()
            return
        code = auth.parse_response_code(sp_url.value.strip())
        if not code:
            sp_status.value = "That doesn't look like the right address. Copy the whole thing."
            page.update()
            return
        auth.get_access_token(code, as_dict=False, check_cache=False)
        finish_spotify(auth)

    # ---- YouTube Music login (Google device-code flow)
    def make_yt(token):
        creds = OAuthCredentials(
            client_id=g_id.value.strip(), client_secret=g_secret.value.strip()
        )
        return YTMusic(token, oauth_credentials=creds)

    def yt_login(e):
        save("g_id", g_id.value.strip())
        save("g_secret", g_secret.value.strip())
        creds = OAuthCredentials(
            client_id=g_id.value.strip(), client_secret=g_secret.value.strip()
        )
        code = creds.get_code()
        state["yt_creds"], state["yt_code"] = creds, code
        yt_status.value = (
            f"Approve in the page that opens (code: {code['user_code']}), "
            "then tap 'I approved it'."
        )
        page.update()
        page.launch_url(f"{code['verification_url']}?user_code={code['user_code']}")

    def yt_done(e):
        creds, code = state["yt_creds"], state["yt_code"]
        if creds is None:
            yt_status.value = "Tap 'Open YouTube Music login' first."
            page.update()
            return
        raw = creds.token_from_code(code["device_code"])
        token = RefreshingToken(credentials=creds, **raw)
        token.update(token.as_dict())
        token_dict = token.as_dict()
        save("yt_token", json.dumps(token_dict))
        state["yt"] = make_yt(token_dict)
        yt_status.value = "YouTube Music: connected"
        page.update()

    # ---- Convert
    def on_pick(e):
        new_name.value = names.get(playlist_dd.value, "")
        page.update()

    def convert(e):
        if not state["sp"] or not state["yt"]:
            status.value = "Connect both Spotify and YouTube Music first."
            page.update()
            return
        if not playlist_dd.value:
            status.value = "Pick a playlist first."
            page.update()
            return
        name = new_name.value.strip() or names[playlist_dd.value]
        bar.value, bar.visible = 0, True
        missing_box.value = ""
        status.value = "Matching tracks on YouTube Music..."
        page.update()

        def progress(done, total):
            bar.value = done / total
            status.value = f"Matching tracks: {done}/{total}"
            page.update()

        found, total, missing = copy_playlist(
            state["sp"], state["yt"], playlist_dd.value, name, public_sw.value, progress
        )
        bar.visible = False
        status.value = f"Done. {found}/{total} tracks added to '{name}'."
        if missing:
            missing_box.value = "Not found:\n" + "\n".join(missing)
        page.update()

    playlist_dd.on_change = on_pick

    page.add(
        ft.Text("Spotify to YT Music", size=22, weight=ft.FontWeight.BOLD),
        ft.Text("1. Spotify", weight=ft.FontWeight.BOLD),
        sp_id,
        sp_secret,
        ft.ElevatedButton(text="Open Spotify login", on_click=guarded(sp_login, sp_status)),
        sp_url,
        ft.ElevatedButton(text="Connect Spotify", on_click=guarded(sp_connect, sp_status)),
        sp_status,
        ft.Divider(),
        ft.Text("2. YouTube Music", weight=ft.FontWeight.BOLD),
        g_id,
        g_secret,
        ft.ElevatedButton(text="Open YouTube Music login", on_click=guarded(yt_login, yt_status)),
        ft.ElevatedButton(text="I approved it", on_click=guarded(yt_done, yt_status)),
        yt_status,
        ft.Divider(),
        ft.Text("3. Convert", weight=ft.FontWeight.BOLD),
        playlist_dd,
        new_name,
        public_sw,
        ft.ElevatedButton(text="Convert playlist", on_click=guarded(convert, status)),
        bar,
        status,
        missing_box,
    )

    # Restore saved logins so you don't repeat the steps every time.
    token = load("sp_token")
    if token and sp_id.value and sp_secret.value:
        try:
            finish_spotify(make_sp_auth(json.loads(token)))
        except Exception:
            sp_status.value = "Spotify: not connected"
    token = load("yt_token")
    if token and g_id.value and g_secret.value:
        try:
            state["yt"] = make_yt(json.loads(token))
            yt_status.value = "YouTube Music: connected (saved login)"
        except Exception:
            pass
    page.update()


ft.app(target=main)
