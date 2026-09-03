"""The media stack's message kinds: every card the *arr / download-client / media-server services post, registered
for the Messages page with a sample to preview and customise it from.

Registering here is what lists a kind on the page. The send sites — `cogs/webhooks.py` for the feed cards,
`cogs/media.py` for the shared "Media stack" board — pass each embed through `bot.messages.apply(kind, embed, ctx)`
right before posting, with the same ctx a kind's sample returns here. Health issues, stalled downloads and
unreachable services go through `bot.alerts` and are customised as the core `core.alert` kind, so they are not
listed again.

Every kind carries the `media` prefix (one "Media stack" heading on the page) because the eight services post the
same cards through one hub; whichever service owns the app applies its customisation. The samples are fixed
webhook payloads shaped like the apps' own, run through the real renderer — no clocks, no random values.
"""

from __future__ import annotations

from typing import Any

import discord
from periscope.messages import MessageKind, register

from .cogs.media import BOARD_KIND, board_ctx, board_embed, parse_jellyfin_session, parse_plex_session
from .cogs.webhooks import OTHER_KIND, VARIABLES, event_ctx, event_embed, parse_event

LAB = "my-lab"   # the lab name previews carry; a real post carries the bot's
FEED_WHERE = "the media channel, else the owning service's alert channel"

# ----- sample data ----------------------------------------------------------------------------------------
SERIES = {
    "id": 12, "title": "The Expanse", "titleSlug": "the-expanse", "path": "/tv/The Expanse", "tvdbId": 280619,
    "tvMazeId": 1825, "imdbId": "tt3230854", "type": "standard", "year": 2015, "genres": ["Drama", "Science Fiction"],
    "images": [{"coverType": "banner", "remoteUrl": "https://artworks.thetvdb.com/banners/graphical/280619-g5.jpg"},
               {"coverType": "poster", "remoteUrl": "https://artworks.thetvdb.com/banners/posters/280619-2.jpg"}],
    "tags": [],
}
EPISODES = [{"id": 4321, "episodeNumber": 1, "seasonNumber": 6, "title": "Strange Dogs", "airDate": "2021-12-10",
             "airDateUtc": "2021-12-10T05:00:00Z", "seriesId": 12, "tvdbId": 8747091}]
MOVIE_POSTER = "https://image.tmdb.org/t/p/original/d5NXSklXo0qyIYkgV94XAgMIckC.jpg"
MOVIE = {
    "id": 7, "title": "Dune", "year": 2021, "releaseDate": "2021-10-22", "folderPath": "/movies/Dune (2021)",
    "tmdbId": 438631, "imdbId": "tt1160419", "genres": ["Science Fiction", "Adventure"],
    "images": [{"coverType": "poster", "remoteUrl": MOVIE_POSTER}], "tags": [],
}
ARTIST_MBID = "69158f97-4c07-4c4e-baf8-4e4ab1ed666e"
ARTIST_POSTER = f"https://assets.fanart.tv/fanart/music/{ARTIST_MBID}/artistthumb/boards-of-canada-4e2b4a1b8c1e7.jpg"
ARTIST = {
    "id": 3, "name": "Boards of Canada", "path": "/music/Boards of Canada", "mbId": ARTIST_MBID, "type": "Group",
    "genres": ["Electronic"], "images": [{"coverType": "poster", "remoteUrl": ARTIST_POSTER}], "tags": [],
}
OLD_FILE = {"id": 77, "relativePath": "Season 06/The Expanse - S06E01 - Strange Dogs WEBDL-1080p.mkv",
            "path": "/tv/The Expanse/Season 06/The Expanse - S06E01 - Strange Dogs WEBDL-1080p.mkv",
            "quality": "WEBDL-1080p", "qualityVersion": 1, "releaseGroup": "NTb", "size": 2147483648,
            "dateAdded": "2021-12-10T06:02:11Z"}
NEW_FILE = {"id": 98, "relativePath": "Season 06/The Expanse - S06E01 - Strange Dogs Bluray-1080p.mkv",
            "path": "/tv/The Expanse/Season 06/The Expanse - S06E01 - Strange Dogs Bluray-1080p.mkv",
            "quality": "Bluray-1080p", "qualityVersion": 1, "releaseGroup": "BORDURE",
            "sceneName": "The.Expanse.S06E01.1080p.BluRay.x264-BORDURE", "size": 4831838208,
            "dateAdded": "2026-09-02T21:14:03Z"}
GRAB_RELEASE = "The.Expanse.S06E01.Strange.Dogs.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb"

# kind -> (app, the webhook payload as the app sends it)
SAMPLES: dict[str, tuple[str, dict[str, Any]]] = {
    "media.grab": ("sonarr", {
        "series": SERIES, "episodes": EPISODES,
        "release": {"quality": "WEBDL-1080p", "qualityVersion": 1, "releaseGroup": "NTb", "releaseTitle": GRAB_RELEASE,
                    "indexer": "NZBgeek (Prowlarr)", "size": 2147483648, "customFormatScore": 0, "customFormats": [],
                    "languages": [{"id": 1, "name": "English"}]},
        "downloadClient": "SABnzbd", "downloadClientType": "Sabnzbd", "downloadId": "SABnzbd_nzo_k3x9q2",
        "customFormatInfo": {"customFormats": [], "customFormatScore": 0},
        "eventType": "Grab", "instanceName": "Sonarr", "applicationUrl": "https://sonarr.example",
    }),
    "media.import": ("radarr", {
        "movie": MOVIE, "remoteMovie": {"tmdbId": 438631, "imdbId": "tt1160419", "title": "Dune", "year": 2021},
        "movieFile": {"id": 41, "relativePath": "Dune (2021) Bluray-1080p.mkv",
                      "path": "/movies/Dune (2021)/Dune (2021) Bluray-1080p.mkv", "quality": "Bluray-1080p",
                      "qualityVersion": 1, "releaseGroup": "FLUX", "indexerFlags": "0", "size": 15032385536,
                      "sceneName": "Dune.2021.1080p.BluRay.DDP7.1.x264-FLUX", "dateAdded": "2026-09-02T21:14:03Z"},
        "isUpgrade": False, "downloadClient": "qBittorrent", "downloadClientType": "qBittorrent",
        "downloadId": "3F1C0B8A9D4E5F6071829304A5B6C7D8E9F00112", "deletedFiles": [],
        "customFormatInfo": {"customFormats": [], "customFormatScore": 0},
        "release": {"releaseTitle": "Dune.2021.1080p.BluRay.DDP7.1.x264-FLUX", "indexer": "TorrentLeech (Prowlarr)",
                    "size": 15032385536},
        "eventType": "Download", "instanceName": "Radarr", "applicationUrl": "https://radarr.example",
    }),
    "media.upgrade": ("sonarr", {
        "series": SERIES, "episodes": EPISODES, "episodeFile": NEW_FILE, "isUpgrade": True,
        "downloadClient": "qBittorrent", "downloadClientType": "qBittorrent",
        "downloadId": "A1B2C3D4E5F60718293A4B5C6D7E8F9001122334", "deletedFiles": [OLD_FILE],
        "customFormatInfo": {"customFormats": [], "customFormatScore": 0},
        "release": {"releaseTitle": "The.Expanse.S06E01.1080p.BluRay.x264-BORDURE",
                    "indexer": "TorrentLeech (Prowlarr)", "size": 4831838208},
        "eventType": "Download", "instanceName": "Sonarr", "applicationUrl": "https://sonarr.example",
    }),
    "media.rename": ("sonarr", {
        "series": SERIES,
        "renamedEpisodeFiles": [
            {"id": 98, "relativePath": "Season 06/The Expanse - S06E01 - Strange Dogs [Bluray-1080p].mkv",
             "path": "/tv/The Expanse/Season 06/The Expanse - S06E01 - Strange Dogs [Bluray-1080p].mkv",
             "previousRelativePath": "Season 06/The.Expanse.S06E01.1080p.BluRay.x264-BORDURE.mkv",
             "previousPath": "/tv/The Expanse/Season 06/The.Expanse.S06E01.1080p.BluRay.x264-BORDURE.mkv"},
            {"id": 99, "relativePath": "Season 06/The Expanse - S06E02 - Azure Dragon [WEBDL-1080p].mkv",
             "path": "/tv/The Expanse/Season 06/The Expanse - S06E02 - Azure Dragon [WEBDL-1080p].mkv",
             "previousRelativePath": "Season 06/The.Expanse.S06E02.1080p.WEB.H264-GLHF.mkv",
             "previousPath": "/tv/The Expanse/Season 06/The.Expanse.S06E02.1080p.WEB.H264-GLHF.mkv"},
        ],
        "eventType": "Rename", "instanceName": "Sonarr", "applicationUrl": "https://sonarr.example",
    }),
    "media.added": ("radarr", {
        "movie": MOVIE, "addMethod": "manual",
        "eventType": "MovieAdded", "instanceName": "Radarr", "applicationUrl": "https://radarr.example",
    }),
    "media.deleted": ("sonarr", {
        "series": SERIES, "episodes": EPISODES, "episodeFile": OLD_FILE, "deleteReason": "upgrade",
        "eventType": "EpisodeFileDelete", "instanceName": "Sonarr", "applicationUrl": "https://sonarr.example",
    }),
    "media.manual": ("sonarr", {
        "series": SERIES, "episodes": EPISODES,
        "downloadInfo": {"quality": "WEBDL-1080p", "qualityVersion": 1, "title": GRAB_RELEASE, "size": 2147483648},
        "downloadClient": "qBittorrent", "downloadClientType": "qBittorrent",
        "downloadId": "A1B2C3D4E5F60718293A4B5C6D7E8F9001122334", "downloadStatus": "Warning",
        "downloadStatusMessages": [{"title": GRAB_RELEASE,
                                    "messages": ["Found matching series via grab history, but release was matched "
                                                 "to series by ID. Automatic import is not possible. See the FAQ "
                                                 "for details."]}],
        "customFormatInfo": {"customFormats": [], "customFormatScore": 0},
        "release": {"releaseTitle": GRAB_RELEASE, "indexer": "NZBgeek (Prowlarr)", "size": 2147483648},
        "eventType": "ManualInteractionRequired", "instanceName": "Sonarr", "applicationUrl": "https://sonarr.example",
    }),
    "media.health": ("radarr", {
        "level": "warning", "type": "IndexerLongTermStatusCheck",
        "message": "Indexers unavailable due to failures for more than 6 hours: NZBgeek (Prowlarr)",
        "wikiUrl": "https://wiki.servarr.com/radarr/system"
                   "#indexers-are-unavailable-due-to-failures-for-more-than-6-hours",
        "eventType": "HealthRestored", "instanceName": "Radarr", "applicationUrl": "https://radarr.example",
    }),
    "media.update": ("prowlarr", {
        "message": "Prowlarr updated from 1.24.3.4754 to 1.25.4.4818", "previousVersion": "1.24.3.4754",
        "newVersion": "1.25.4.4818",
        "eventType": "ApplicationUpdate", "instanceName": "Prowlarr", "applicationUrl": "https://prowlarr.example",
    }),
    "media.test": ("sonarr", {"eventType": "Test", "instanceName": "Sonarr",
                              "applicationUrl": "https://sonarr.example"}),
    OTHER_KIND: ("lidarr", {
        "artist": ARTIST,
        "trackFiles": [{"id": 610, "path": "/music/Boards of Canada/Geogaddi/01 - Ready Lets Go.flac",
                        "quality": "FLAC", "qualityVersion": 1, "size": 9437184, "dateAdded": "2026-08-30T19:02:44Z"}],
        "eventType": "Retag", "instanceName": "Lidarr", "applicationUrl": "https://lidarr.example",
    }),
}

# what the board's probes answer: queues, health, transfer info, sessions and disk rows in the apps' own shapes
SONARR_QUEUE = [
    {"id": 501, "title": "The.Expanse.S06E02.1080p.WEB.H264-GLHF", "size": 2469606195, "sizeleft": 1234803097,
     "status": "downloading", "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading",
     "timeleft": "00:06:30", "downloadClient": "qBittorrent", "indexer": "NZBgeek (Prowlarr)",
     "series": {"title": "The Expanse"}, "episode": {"seasonNumber": 6, "episodeNumber": 2}},
    {"id": 502, "title": "Severance.S02E05.1080p.WEB.H264-ETHEL", "size": 3116367052, "sizeleft": 3116367052,
     "status": "queued", "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading", "timeleft": "00:00:00",
     "downloadClient": "qBittorrent", "indexer": "NZBgeek (Prowlarr)",
     "series": {"title": "Severance"}, "episode": {"seasonNumber": 2, "episodeNumber": 5}},
]
RADARR_QUEUE = [
    {"id": 88, "title": "Dune.Part.Two.2024.1080p.BluRay.x264-VETO", "size": 16106127360, "sizeleft": 0,
     "status": "completed", "trackedDownloadStatus": "ok", "trackedDownloadState": "importPending",
     "timeleft": "00:00:00", "downloadClient": "qBittorrent", "indexer": "TorrentLeech (Prowlarr)",
     "movie": {"title": "Dune: Part Two", "year": 2024}},
]
PROWLARR_HEALTH = [{"source": "IndexerStatusCheck", "type": "warning",
                    "message": "Indexers unavailable due to failures: NZBgeek",
                    "wikiUrl": "https://wiki.servarr.com/prowlarr/system#indexers-are-unavailable-due-to-failures"}]
QBIT_TRANSFER = {"dl_info_speed": 12582912, "dl_info_data": 48318382080, "up_info_speed": 1310720,
                 "up_info_data": 6442450944, "connection_status": "connected", "dht_nodes": 384}
SAB_QUEUE = {"status": "Downloading", "kbpersec": "3072.51", "mbleft": "1843.22", "mb": "2048.00", "noofslots": 2,
             "paused": False, "diskspace1": "812.33", "diskspacetotal1": "1863.02", "speedlimit": "0"}
DISKSPACE = [  # Sonarr and Radarr share the media disk, so both report it; the board counts it once
    {"path": "/data", "label": "media", "freeSpace": 872415232000, "totalSpace": 4000787030016},
    {"path": "/", "label": "", "freeSpace": 21474836480, "totalSpace": 107374182400},
    {"path": "/data", "label": "media", "freeSpace": 872415232000, "totalSpace": 4000787030016},
]
PLEX_SESSION = {
    "type": "episode", "grandparentTitle": "Severance", "parentIndex": 2, "index": 4, "title": "Woe's Hollow",
    "year": 2025, "duration": 3364000, "viewOffset": 1497000, "User": {"title": "alice"},
    "Player": {"product": "Plex for Apple TV", "title": "Living room", "state": "playing"},
    "TranscodeSession": {"videoDecision": "copy", "audioDecision": "transcode"},
}
JELLYFIN_SESSION = {
    "UserName": "bob", "Client": "Jellyfin Media Player", "DeviceName": "study-pc",
    "NowPlayingItem": {"Type": "Movie", "Name": "Heat", "ProductionYear": 1995, "RunTimeTicks": 102060000000},
    "PlayState": {"PositionTicks": 25515000000, "IsPaused": True, "PlayMethod": "DirectPlay"},
}


# ----- samples --------------------------------------------------------------------------------------------
def _feed_sample(key: str):
    def sample() -> tuple[discord.Embed | None, dict[str, Any]]:
        app, payload = SAMPLES[key]
        ev = parse_event(app, payload)
        return event_embed(ev, LAB), event_ctx(ev)
    return sample


def _sample_board() -> tuple[discord.Embed | None, dict[str, Any]]:
    results = {"sonarr": (True, SONARR_QUEUE), "radarr": (True, RADARR_QUEUE), "lidarr": (True, []),
               "prowlarr": (True, PROWLARR_HEALTH), "qbittorrent": (True, QBIT_TRANSFER), "sabnzbd": (True, SAB_QUEUE)}
    streams = [s for s in (parse_plex_session(PLEX_SESSION), parse_jellyfin_session(JELLYFIN_SESSION)) if s]
    data = board_ctx(results, streams, [], DISKSPACE, plex=True, jellyfin=True)
    return board_embed(data, LAB), data


# ----- the kinds -------------------------------------------------------------------------------------------
BOARD_VARIABLES = {
    "services": "every media service in board order: item.name · item.ok · item.error · item.issues (Prowlarr's health "
                "messages)",
    "down": "the names of the services that did not answer",
    "queues": "the download queues: item.app · item.queued · item.downloading",
    "qbittorrent": "qBittorrent's speeds in bytes per second: down · up (empty when not configured or not answering)",
    "sabnzbd": "SABnzbd's down speed in bytes per second and active count: down · active (empty when not configured or "
               "not answering)",
    "streams": "who is watching: item.server · item.user · item.title · item.player · item.pct · item.method (direct · "
               "transcode) · item.paused",
    "disk": "free · total (bytes) · used_pct across the apps' root folders (empty when none reported)",
}

# kind -> (title, when it is posted); the variables each carries are `VARIABLES` next to the renderer
CARDS: dict[str, tuple[str, str]] = {
    "media.grab": ("Grabbed", "posted when Sonarr, Radarr or Lidarr sends a release to the download client"),
    "media.import": ("Imported", "posted when a finished download has been imported into the library"),
    "media.upgrade": ("Upgraded", "posted when an import replaced an existing file with a better one (a Download "
                                  "event flagged as an upgrade, or an Upgrade event)"),
    "media.rename": ("Renamed", "posted when an app renamed files on disk"),
    "media.added": ("Added", "posted when a series, movie or artist was added to the library"),
    "media.deleted": ("Deleted", "posted when a series, movie, artist or album was removed from the library, or a "
                                 "file was deleted from disk"),
    "media.manual": ("Manual interaction required", "posted when a download needs someone to look at it before it "
                                                     "can be imported"),
    "media.health": ("Health restored", "posted when an app says a health problem is gone and this bot had no alert "
                                        "open for it (new health problems are alerts, see core.alert)"),
    "media.update": ("Application updated", "posted when an app updated itself"),
    "media.test": ("Test notification", "posted when you press Test on the webhook in the app"),
    OTHER_KIND: ("Any other event", "the generic card for events without one of their own (a Lidarr retag, a "
                                    "failed import, …)"),
}

register(
    MessageKind(BOARD_KIND, "Media stack board",
                "the pinned board: every media service up or down, the download queues and speeds, who is watching, "
                "disk space; refreshed every STATUS_INTERVAL_S and by its 🔄 button",
                where="the status channel", where_env="STATUS_CHANNEL_ID", sample=_sample_board, group="boards",
                variables=BOARD_VARIABLES),
    *[MessageKind(key, title, description, where=FEED_WHERE, where_env="MEDIA_CHANNEL_ID", sample=_feed_sample(key),
                  variables=VARIABLES[key], group="feed")
      for key, (title, description) in CARDS.items()],
)
