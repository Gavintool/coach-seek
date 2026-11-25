# Coach Seek

Coach Seek is a Python 3.11+ Discord bot that gives practical FIRST Tech Challenge (FTC) advice about rules, strategy, robot design, the FTC SDK, and event prep. Treat its answers as informal advice and verify rules decisions against official FIRST materials.

## Discord setup

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create an application named **Coach Seek**.
2. Open **Bot**. Add the bot user if needed, then copy its token. Use **Reset Token** to generate a token if one isn't displayed.
3. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent** for message-based questions. Server Members Intent and Presence Intent can stay off.
4. Under **Installation**, enable **Guild Install**, select the Discord-provided install link, and configure the Guild Install scopes `bot` and `applications.commands`. Select **View Channels**, **Send Messages**, **Read Message History**, **Embed Links**, and **Send Messages in Threads** if the bot should reply in threads. Make sure members who use `/ask` have **Use Application Commands** in the channel. Use the generated install URL to add Coach Seek to your server.

## Local setup

Create an API key on the [DeepSeek Platform](https://platform.deepseek.com). From the project root, create a virtual environment and install the pinned dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, use `py -3 -m venv .venv`, `.\.venv\Scripts\Activate.ps1`, and `Copy-Item .env.example .env` for the corresponding steps.

**Start Coach Seek only after setting both `DISCORD_TOKEN` and `DEEPSEEK_API_KEY`.** Fill them in privately in your local `.env`. Keep that file private and never commit it. Environment variables take precedence over values from `.env`.

Run from the project root with the virtual environment active.

```bash
python -m coach_seek
```

Coach Seek syncs its global slash commands at startup. Use Ctrl+C to stop the process.

## Asking Coach Seek

Anyone with access to Coach Seek can ask a question by DM, server @mention, or `/ask`.

- **Direct message** Coach Seek with a question. DMs require no mention and use no server memory or fact extraction.
- **Mention** Coach Seek in a server channel or accessible thread and include a question, for example `@Coach Seek How should we tune our drivetrain?`
- **Designated channel** messages with text go to Coach Seek even without a mention when either channel ID setting is present. Configure the exact channel or thread ID. A parent channel ID covers only the parent itself.
- **Slash command** `/ask question:How can we reduce intake jams?` works in servers and in DMs with Coach Seek wherever Discord makes the command available. It has no bot-imposed role requirement. Coach Seek acknowledges the interaction immediately and posts the answer where you asked.

Outside the optional designated channel, ordinary server messages get no automatic reply. A reply ping alone does not count as an explicit mention. Coach Seek ignores bot and webhook triggers and mentions with no question. Long answers are posted in multiple messages. In a designated channel, ordinary conversation counts as API usage.

### Chat history in answers

For a server question, Coach Seek can use recent chat from any mutually visible text channel or active thread in that server, including threads under forums. It takes up to 30 eligible messages from the asking channel or thread, including when the question comes from an archived thread. It also takes up to 15 each from up to 12 other channels or active threads, ordered by most recent known activity. To fill those caps it scans at most 150 recent messages per source, not the entire channel. Both Coach Seek and the asker need **View Channel** and **Read Message History** in each source. Private threads additionally require each to be a member or have **Manage Threads** there. Voice, stage, and category channels are excluded.

Each history line has a UTC timestamp, channel name, and author display name. Coach Seek includes prior Coach Seek messages and skips other bots, webhooks, empty messages, and the triggering message. It redacts credentials before sending history to DeepSeek as untrusted reference material, alongside the question and relevant server memories. The history block is capped at 16,000 characters, with the asking channel first and newer messages first within each source. History fetches run at most three at a time, with up to five seconds per source. A failed fetch leaves the rest of the available history usable.

Replies are posted where the question was asked. An answer can draw on chat the asker can see elsewhere in the server, even if other readers of the reply lack access to that source. Server requests use only history from the same server. DM requests use up to 30 messages from that same DM and use no server memory or fact extraction.

The FTC digest still posts only to `#general` or the configured `DISCORD_DIGEST_CHANNEL_ID`.

## Team memory

The memory commands are server-only.

- `/remember fact:<text>` saves a team robotics fact of up to 400 characters and returns its ID.
- `/memories` shows the six most recent facts and their IDs.
- `/forget query:<ID or text>` removes a fact by ID (for example, `#12`) or removes every case-insensitive substring match. It confirms the count and IDs. This command requires **Manage Server**.

Each Discord server has its own memory, shared by members across channels and threads in that server. Command confirmations and lists are ephemeral. DMs use no server memory. The RUR and HAX context and the team-provided BIOBUZZ schedule come from the built-in prompt.

After sending a reply, Coach Seek can make a short second DeepSeek request to extract durable robotics facts explicitly stated by the user. It skips credential-like input and asks the model to omit chit-chat, personal information, and secrets. Keep credentials and private information out of questions and memory commands. For each answer, up to six relevant facts are selected by keyword overlap from the server's latest 250 facts. Facts can be recalled in channel replies, so save only information you intend to share with the server.

SQLite initializes its versioned schema on first run. The database defaults to `/workspace/coach-seek/data/memory.sqlite`. The `memories` table stores an ID, guild ID, fact, and a normalized deduplication key. The `memory_revisions` table prevents an in-flight extraction from restoring a deleted fact. The configured directory must be writable by the bot user.

## FTC BIOBUZZ digest

Coach Seek uses DeepSeek’s official web tools once a day by default. The task waits 60 seconds after Discord is ready, then checks the last completed run. If it is still within the configured interval, Coach Seek logs the skip and waits out the remaining time before searching.

The main focus is recent 2026–27 BIOBUZZ YouTube videos showing a distinctive robot design or mechanism. A specific official rule, Q&A clarification, or design write-up can also qualify. Coach Seek posts up to three findings, with videos first. Each finding names a concrete detail and links directly to a source returned by the web tools. Vague findings are logged and skipped. The digest sends no pings.

For each server, Coach Seek uses `DISCORD_DIGEST_CHANNEL_ID` if it identifies a text channel in that server. Otherwise it uses a text channel called `#general`, preferring an exact lowercase match. Coach Seek skips servers without a matching channel or without **View Channel** and **Send Messages** permissions there. It also skips the check when web search is disabled or there are no eligible servers.

All eligible servers receive the same digest. After at least one successful post, Coach Seek records those URLs in a shared local SQLite database so later runs skip those stories, including after a restart. The database also stores the last completed run time. If every delivery attempt fails, the items can be retried on the next run. Empty digests are logged and produce no Discord message.

## Long-lived service

On this box, use the Bash supervisor with Linux's `flock`. From `/workspace/coach-seek`, once the project `.venv` is installed and both credentials are set, run:

```bash
./scripts/run-forever.sh start
./scripts/run-forever.sh status
tail -f logs/coach-seek.log
./scripts/run-forever.sh stop
```

The supervisor uses the project directory and its virtual environment. Coach Seek loads `.env` from that directory. The supervisor detaches from the terminal, appends logs under `logs/`, tracks its PID in `logs/coach-seek.pid`, and restarts failed exits with backoff from 2 to 60 seconds. It survives a terminal disconnect while this box stays running. After a box reboot, run `start` again. The SQLite database persists on this box's disk.

Use the supplied user unit on a machine with a working systemd user manager. The unit reads `/workspace/coach-seek/.env`, restarts on failure, and appends logs to `/workspace/coach-seek/logs/coach-seek.log`. Stop the shell supervisor before using the unit.

```bash
mkdir -p ~/.config/systemd/user
install -m 644 deploy/coach-seek.service ~/.config/systemd/user/coach-seek.service
systemctl --user daemon-reload
loginctl enable-linger "$(id -un)"
systemctl --user enable --now coach-seek.service
journalctl --user -u coach-seek.service -f
```

Lingering lets the user service run at boot and after logout. The host may require an administrator to enable it. To disable the service and stop it, use `systemctl --user disable --now coach-seek.service`.

## Deploy on Render

Coach Seek runs as a Render background worker from this repository's `main` branch, in the same workspace as Coach Sol but as a separate service. The Blueprint in `render.yaml` defines only that worker.

- Runtime: Python 3.13.5 (`.python-version`)
- Start command: `python -m coach_seek`
- Plan and region: `0.5c-512mb` in Virginia
- Persistent disk: mounted at `/var/data` for `memory.sqlite` and `digest.sqlite` (`MEMORY_DB_PATH` and `DIGEST_DB_PATH`)
- Secrets: set `DISCORD_TOKEN` and `DEEPSEEK_API_KEY` on the service in the Render Dashboard or API. Do not commit them.

A free web service would spin down and is a poor fit for a long-running Discord bot. This worker stays running and does not receive incoming HTTP traffic.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | Required | Bot token from the Discord Developer Portal |
| `DEEPSEEK_API_KEY` | Required | Key from the DeepSeek Platform |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Fixed API model ID |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | Trusted HTTPS API base URL |
| `DISCORD_CHANNEL_ID` | Unset | Numeric ID of the automatic-reply channel |
| `ADVISOR_CHANNEL_ID` | Unset | Alias used when `DISCORD_CHANNEL_ID` is blank |
| `DEEPSEEK_THINKING` | `enabled` | `enabled` or `disabled` |
| `DEEPSEEK_REASONING_EFFORT` | `high` | `low`, `medium`, `high`, or `max` for thinking mode. DeepSeek maps `medium` to `high`. |
| `DEEPSEEK_MAX_TOKENS` | `16000` | Output token limit per request, including thinking tokens |
| `DEEPSEEK_WEB_SEARCH` | `enabled` | Set to `disabled` to omit both web tools |
| `DEEPSEEK_SAFETY_LEVEL` | `high` | Server-side content safety level, `high` or `low` |
| `MEMORY_DB_PATH` | `data/memory.sqlite` | SQLite database path, relative to the working directory when specified as a relative path |
| `DISCORD_DIGEST_CHANNEL_ID` | Unset | Optional numeric ID for the digest text channel in its server |
| `DIGEST_ENABLED` | `enabled` | `enabled` or `disabled` |
| `DIGEST_INTERVAL_HOURS` | `24` | Positive hours to wait between digest runs |
| `DIGEST_INITIAL_DELAY_SECONDS` | `60` | Nonnegative delay after Discord is ready |
| `DIGEST_DB_PATH` | `data/digest.sqlite` | Separate SQLite database for seen URLs and the last completed run, relative to the working directory when specified as a relative path |
| `LOG_LEVEL` | `INFO` | Python logging level |

Enable Discord Developer Mode and use **Copy Channel ID** to find a channel's numeric ID.

## DeepSeek model and pricing

The API model ID `deepseek-flash` maps to **DeepSeek-V4.1-Flash-1017**. Coach Seek uses the OpenAI-compatible `POST /chat/completions` endpoint with thinking enabled and high effort by default. Discord receives only the final answer. DeepSeek lists a 1M-token context and a 384K-token maximum output. [Your First API Call](https://api-docs.deepseek.com/) · [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/) · [October 17 release notes](https://api-docs.deepseek.com/news/news261017/) · [Changelog](https://api-docs.deepseek.com/updates/)

| USD per 1M tokens | Off-peak | Peak |
| --- | ---: | ---: |
| Cached input | $0.003 | $0.006 |
| Uncached input | $0.15 | $0.30 |
| Output | $0.60 | $1.20 |

Peak hours are 01:00–04:00 and 06:00–10:00 UTC on weekdays, excluding Chinese public holidays. DeepSeek documents an account-wide limit of 2,500 concurrent requests for Flash and returns HTTP 429 when the limit is exceeded. Coach Seek retries HTTP 429, 500, 502, and 503 twice with backoff. [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/) · [Rate Limit & Isolation](https://api-docs.deepseek.com/quick_start/rate_limit/)

## Official DeepSeek web tools

Coach Seek declares `deepseek_web_search` and `deepseek_read_url` in Chat Completions using the existing DeepSeek API key. There is no additional search key. DeepSeek decides when to search or read links and runs the tools on its servers. Thinking mode enables deep thinking search. In that mode, when both tools are declared, DeepSeek handles pages through deep thinking search and skips the separate URL-reading pass. Discord replies include a Sources list from DeepSeek's URL annotations when present. [DeepSeek Web Tools](https://api-docs.deepseek.com/guides/web_tools/) · [November 13 release notes](https://api-docs.deepseek.com/news/news261113/)

The Web Tools guide's Usage and Billing section states that built-in text search, image search, and page fetch have no separate tool fees. Model tokens are billed, including those used for extraction. DeepSeek prices its standalone Search & Fetch APIs per call. Coach Seek uses the built-in tools. Safety checks use the Chat Completions `safety_check` field and stay enabled. [Web Tools usage and billing](https://api-docs.deepseek.com/guides/web_tools/#usage-and-billing) · [Standalone Search & Fetch billing](https://api-docs.deepseek.com/guides/search/#billing)

## Troubleshooting

- **Missing configuration** — Set both required credentials, remove any placeholder values, and start from the directory containing `.env`.
- **Coach Seek ignores messages** — Enable Message Content Intent in the portal and restart Coach Seek. Check channel permissions and verify that the configured ID matches the channel or thread.
- **DMs are unavailable** — Users typically need to share a server with Coach Seek or have the app available to DM. Check Discord’s DM privacy settings.
- **No digest appears** — Verify that Coach Seek is in a server with `#general` or a valid digest override, and has **View Channel** and **Send Messages** there. Check that the digest and DeepSeek web search are enabled and the digest database directory is writable. Runs with nothing new only write a log entry.
- **`/ask` is missing** — Check the `applications.commands` scope, the channel's Use Application Commands permission, and the startup log for successful command sync. Reopen Discord if it has cached the command list.
- **DeepSeek 401 or 402** — Check the API key for 401 and the account balance for 402.
- **Web answers take longer** — Deep thinking search can take several minutes. Discord acknowledges `/ask` before calling DeepSeek and shows a typing indicator for message-based questions. If a search fails, consult the official FTC source directly or retry shortly.
- **Memory database error** — Check the path in `MEMORY_DB_PATH` and write permissions on the file and its parent directory.
- **DeepSeek 429, 5xx, or timeout** — Try again after a short wait. If 429 persists, check concurrent usage across the DeepSeek account. [DeepSeek error codes](https://api-docs.deepseek.com/quick_start/error_codes/)
