# chomik-mcp

An MCP server for chomikuj.pl. It lets any MCP client (Cursor, Open WebUI, LM Studio,
your own agent) browse your accounts, download folders to your NAS, upload
files, and copy one account into another, all with plain-language requests.

Self-hosted like [chomik-web-downloader](https://github.com/pawisoon/chomik-web-downloader)
and [chomik-web-uploader](https://github.com/pawisoon/chomik-web-uploader): one
Docker container on your NAS serving MCP over HTTP with a bearer token. It can
also run locally over stdio.

## Tools

| Tool | What it does |
|---|---|
| `accounts` | Configured accounts, and whether profile/folder passwords are set (never the values) |
| `list_folders` | Subfolders in one of your accounts |
| `list_files` | Files in a folder: `A:/Folder` or a chomikuj.pl URL |
| `search` | Search files across all of chomikuj.pl (filters: type, size; 25 hits per page) |
| `download` | Download a folder (recursively) to the server's download folder |
| `download_file` | Download one file by its chomikuj.pl link, e.g. a `search` hit |
| `upload` | Upload a file or folder from the server's upload folder |
| `clone_account` | Copy a whole account, or one folder of it, into another account, server-side |
| `clone_public` | Copy someone's public folder into your account |
| `job_status`, `list_jobs` | Progress of the background jobs above |

Downloads, uploads and clones run as background jobs, so a client can start a
multi-hour clone and check on it later.

Nothing is ever deleted, moved or renamed on chomikuj.pl. There is no tool for it,
and chomik-core additionally delete-protects the `CHOMIK_A_USER` account.

## Run with Docker (no clone needed)

Create a folder for the data, e.g. `/volume1/docker/chomik-mcp`, and put this
`docker-compose.yml` in it:

```yaml
services:
  chomik-mcp:
    image: ghcr.io/pawisoon/chomik-mcp:latest
    container_name: chomik-mcp
    ports:
      - "8765:8765"
    environment:
      - PANEL_PASSWORD=choose_a_panel_password
    volumes:
      - ./data:/data
    restart: unless-stopped
```

```bash
docker compose up -d
```

Then open **http://<nas>:8765/**, log in with `PANEL_PASSWORD`, and fill in:

1. **Your chomikuj.pl accounts** (login, password, optional profile password). "Sprawdź logowanie" tests each one.
2. **Passwords of other users' chomiki**: profile passwords, and folder passwords (paste the folder link).
3. **Connect your AI assistant**: the MCP address, the access token and a ready-to-copy client config.

Everything is saved in `data/` (`settings.json` and `chomik-passwords.json`, both readable only by the container user).

- **Synology / Portainer:** paste the file as a new stack, set the panel password, deploy.
- **Images:** `linux/amd64` and `linux/arm64` (Synology Intel and ARM, Raspberry Pi).
- **Uploads:** put files to upload in `data/uploads`. Downloads land in `data/downloads`.
- **Run as your NAS user:** add `user: "1026:100"` (your uid:gid, see `id` over SSH) so files in `data/` belong to you.
- **Port taken?** Change `"8765:8765"` to e.g. `"8766:8765"`.
- **No panel?** Accounts can also come from environment variables (`CHOMIK_A_USER`, `CHOMIK_A_PASSWORD`, ...) and the token from `MCP_TOKEN`; see Configuration below.

### From a checkout

```bash
cp mcp/.env.example .env      # set PANEL_PASSWORD (accounts can go in the panel)
docker compose up -d          # panel at http://localhost:8765/
```

The settings panel is at `http://<nas>:8765/` and MCP at `http://<nas>:8765/mcp`. Point your MCP
client at it with the header `Authorization: Bearer <token>` (shown in the panel). `GET /health` answers `ok` without a token.

Example client config (clients that support remote MCP over HTTP):

```json
{
  "mcpServers": {
    "chomik": {
      "type": "http",
      "url": "http://nas.local:8765/mcp",
      "headers": { "Authorization": "Bearer YOUR_MCP_TOKEN" }
    }
  }
}
```

## Sonarr / Radarr fallback

In HTTP mode the same container also acts as an indexer and a download client
for Sonarr and Radarr, so they can fall back to chomikuj.pl when no torrent is
found. It pretends to be usenet (that is the only protocol besides torrents
the arr apps know): a "Generic Newznab" indexer whose results are chomikuj
files, and a "SABnzbd" client that downloads them over the ChomikBox API,
paid from your account's transfer.

The panel's **Sonarr i Radarr** card shows the addresses and the API key.

1. **Shared folder.** Sonarr/Radarr must see the downloads under the same path
   as this container. Mount your downloads share and point `ARR_COMPLETE_DIR`
   at it, e.g. add to the compose file:
   ```yaml
       environment:
         - ARR_COMPLETE_DIR=/downloads/chomik
       volumes:
         - /volume1/downloads:/downloads   # same mount as in the Sonarr/Radarr containers
   ```
   If the paths differ, add a Remote Path Mapping in Sonarr/Radarr instead.
2. **Indexer.** Settings → Indexers → + → *Newznab* → *Custom*:
   URL `http://<nas>:8765/arr/newznab`, API Path `/api`, API Key from the panel,
   categories 2000/2040 (Radarr) or 5000/5040 (Sonarr). Turn **Enable RSS** off:
   chomikuj.pl has no "latest uploads" feed, only search. Prowlarr's
   *Generic Newznab* works the same way.
3. **Download client.** Settings → Download Clients → + → *SABnzbd*:
   host `<nas>`, port `8765`, URL Base `/arr/sabnzbd`, API Key from the panel,
   category `radarr` or `sonarr`.
4. **Torrents first.** Settings → Profiles → Delay Profiles: preferred protocol
   *Torrent*, usenet delay e.g. 120 minutes. The arr then only grabs from
   chomikuj.pl when no acceptable torrent showed up within the delay.

How it behaves:

- Search sends the title the arr asks for to chomikuj.pl's site search (videos
  only), and keeps hits whose file or folder name has every word of the title,
  and the right year (movies) or season/episode (`S01E02`, `1x02`, `Sezon 1/odc 2`).
- Each hit gets a scene-style name the arr can parse, e.g.
  `Big.Buck.Bunny.2008.1080p.WEBRip-Chomikuj`. Quality comes from the file name;
  when the name has none it is guessed from container and size. Polish audio or
  subtitles (`lektor`, `dubbing`, `napisy`, `PL`) add `Polish`, so set the
  languages you accept in your profiles.
- Downloads run one at a time (about 1 MB/s per file), resume after a restart,
  and land in `ARR_COMPLETE_DIR/<category>/<release name>/`. A failure (not
  enough transfer, file gone, captcha) is reported as a failed download, so the
  arr blocklists it and tries the next release.
- The arr sends English or original titles. Files named only by their Polish
  title won't be found.
- Archives (`.rar`, `.zip`, split parts) are skipped.

`GET /arr/health?apikey=...` shows the queue, the last search error and this month's total.

## Run locally (stdio)

```bash
cd mcp && uv venv && uv pip install -e .
.venv/bin/chomik-mcp --env ../.env
```

```json
{
  "mcpServers": {
    "chomik": { "command": "/path/to/chomik-tools/mcp/.venv/bin/chomik-mcp", "args": ["--env", "/path/to/.env"] }
  }
}
```

## Configuration

| Variable | Meaning |
|---|---|
| `CHOMIK_A_USER`, `CHOMIK_A_PASSWORD` | First account (add `B`, `C`, ... the same way). `_PASSWORD_HASH` (md5) works instead of the password for everything except server-side copies |
| `CHOMIK_A_PROFILE_PASSWORD` | Password of a protected profile (optional) |
| `CHOMIK_PASSWORDS_FILE` | JSON with profile and folder passwords for other users' protected chomiki (default `/data/chomik-passwords.json`) |
| `CHOMIK_DOWNLOAD_DIR`, `CHOMIK_UPLOAD_DIR`, `CHOMIK_STATE_DIR` | Where downloads go, where uploads are read from, where clone state and job logs live |
| `PANEL_PASSWORD` | Password for the settings panel (HTTP mode) |
| `MCP_TOKEN` | Optional fixed bearer token (16+ characters). Without it the panel creates and shows one |
| `CHOMIK_SETTINGS_FILE` | Where the panel stores accounts and the token (default `/data/settings.json`) |
| `MCP_PORT` | HTTP port (default 8765) |
| `MCP_ALLOWED_HOSTS` | Optional comma-separated `host:port` list for DNS-rebinding protection |
| `ARR_ENABLED` | `0` turns the Sonarr/Radarr endpoints off (default on in HTTP mode) |
| `ARR_API_KEY` | Optional fixed API key for `/arr/*` (16+ characters). Without it the panel creates and shows one |
| `ARR_ACCOUNT` | Account whose transfer pays for arr downloads (letter or username; default the first) |
| `ARR_COMPLETE_DIR` | Where arr downloads go (default `$CHOMIK_DOWNLOAD_DIR/arr`) |
| `ARR_MIN_SIZE_MB`, `ARR_MAX_SIZE_MB` | Ignore files smaller / larger than this (defaults 100 and 0 = no limit) |
| `ARR_MONTHLY_LIMIT_GB` | Stop arr downloads once this much was downloaded this month (0 = no limit) |
| `ARR_TRANSFER_RESERVE_MB` | Transfer to leave untouched on the account |
| `ARR_SEARCH_PAGES` | Result pages fetched per search, 25 hits each (default 2) |
| `ARR_CACHE_SECONDS` | How long identical searches are answered from cache (default 900) |

Passwords are only ever read from this configuration. No tool takes a password
as an argument, so they never pass through the model.

Uploads only read from `CHOMIK_UPLOAD_DIR`, and downloads only write inside
`CHOMIK_DOWNLOAD_DIR`.

## Support

If these tools save you time, you can buy me a coffee:

<a href="https://suppi.pl/pawels"><img src="https://img.shields.io/badge/Suppi-Wesprzyj-ff5a5f?style=for-the-badge" alt="Suppi" height="40"></a>
<a href="https://buymeacoffee.com/pawisoon"><img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black" alt="Buy Me a Coffee" height="40"></a>
