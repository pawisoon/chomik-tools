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
| `download` | Download a folder (recursively) to the server's download folder |
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

Passwords are only ever read from this configuration. No tool takes a password
as an argument, so they never pass through the model.

Uploads only read from `CHOMIK_UPLOAD_DIR`, and downloads only write inside
`CHOMIK_DOWNLOAD_DIR`.

## Support

If these tools save you time, you can buy me a coffee:

<a href="https://suppi.pl/pawels"><img src="https://img.shields.io/badge/Suppi-Wesprzyj-ff5a5f?style=for-the-badge" alt="Suppi" height="40"></a>
<a href="https://buymeacoffee.com/pawisoon"><img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black" alt="Buy Me a Coffee" height="40"></a>
