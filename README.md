# chomik-tools

Tools for [chomikuj.pl](https://chomikuj.pl) accounts. Product page (Polish): https://chomik-tools.pawels-apps.workers.dev


| Folder | What it is |
|---|---|
| [`mcp/`](mcp) | **chomik-mcp**: self-hostable MCP server. Lets an AI client browse, download, upload and clone between your accounts |
| [`cloner/`](cloner) | **chomik-clone**: copies a whole account, one folder, or someone's public folder into another account you own, server-side |
| [`core/`](core) | **chomik-core**: the shared Python library (ChomikBox API, website session, profile/folder passwords) |

Related, self-hosted web apps for a NAS:
[chomik-web-downloader](https://github.com/pawisoon/chomik-web-downloader) and
[chomik-web-uploader](https://github.com/pawisoon/chomik-web-uploader).

## Quick start (Docker, MCP server)

```bash
git clone https://github.com/pawisoon/chomik-tools && cd chomik-tools
cp mcp/.env.example .env          # accounts + MCP_TOKEN
mkdir -p data/downloads data/uploads data/state
docker compose up -d              # pulls ghcr.io/pawisoon/chomik-mcp, serves http://<host>:8765/mcp
```

The image is built for `linux/amd64` and `linux/arm64` (Synology Intel and ARM, Raspberry Pi):
`docker pull ghcr.io/pawisoon/chomik-mcp:latest`.

Without cloning: copy the `docker-compose.yml` from [mcp/README.md](mcp/README.md#run-with-docker-no-clone-needed)
(works as a Synology / Portainer stack). Details in [mcp/README.md](mcp/README.md). The cloner also works on its own from
the command line, see [cloner/README.md](cloner/README.md).

## Safety

- Nothing here deletes, moves or renames anything on chomikuj.pl.
- The `CHOMIK_A_USER` account is delete-protected in the library itself.
- Passwords live in `.env` and `chomik-passwords.json` (both git-ignored), never in tool arguments.
- Captchas aren't solved or bypassed. A captcha stops a run, and rerunning resumes.

## Product page

`site/` is the Polish product page, served as static assets by a Cloudflare Worker:
`cd site && npx wrangler deploy`.

## Development

Each package has its own tests:

```bash
cd core && uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest -m 'not live'
cd cloner && uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest
cd mcp && uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest
```

## Support

If these tools save you time, you can buy me a coffee:

<a href="https://suppi.pl/pawels"><img src="https://img.shields.io/badge/Suppi-Wesprzyj-ff5a5f?style=for-the-badge" alt="Suppi" height="40"></a>
<a href="https://buymeacoffee.com/pawisoon"><img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black" alt="Buy Me a Coffee" height="40"></a>
