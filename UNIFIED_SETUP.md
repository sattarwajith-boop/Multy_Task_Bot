# Unified WZML-X + Auto Monitor + MV Discovery

This build keeps WZML-X as the only download/upload engine and adds:

- Website and RSS monitoring stored in MongoDB
- MV-style release browsing with `/mv`
- Automatic magnet/torrent/direct-link dispatch into WZML-X
- Automatic OMDb/IMDb information cards
- Automatic copying of uploads to multiple destination channels
- MongoDB deduplication of discovered links
- Koyeb health port and free-instance-friendly queue defaults

## Configuration choices

All unified settings are also available in `config.py`. Configuration priority
is:

1. Koyeb/system environment variables
2. Non-empty values in `config.py`

This means Koyeb Secrets remain authoritative and cannot be accidentally
replaced by the checked-in files. For local or private-repository deployment,
you can edit `config.py`. Keep secret fields empty when using a public
repository.

Use it only with files and sources you are legally allowed to download and
redistribute.

## Security first

The supplied Auto Leech source contained credentials inside `config.py`.
Revoke that Telegram bot token in BotFather and rotate the MongoDB database
password. Do not reuse either value. This project reads secrets only from
environment variables.

## Koyeb deployment

1. Push this folder to a private GitHub repository.
2. Create a Koyeb Web Service from the Dockerfile and expose HTTP port `8000`.
3. Paste `KOYEB_ENV.txt` into Koyeb's environment-variable bulk editor, replace
   every `CHANGE_ME`, and store credentials as Koyeb Secrets where possible.
4. Keep `BASE_URL=https://{{ KOYEB_PUBLIC_DOMAIN }}` and
   `BASE_URL_PORT=8000`. Koyeb resolves its public-domain variable during
   deployment.
5. Make the bot an administrator in `AUTO_MONITOR_CHAT`, `LEECH_LOG_ID`, and
   every `AUTO_FORWARD_CHATS` destination.
6. MongoDB Atlas Network Access must permit Koyeb connections.

`AUTO_MONITOR_CHAT` should be a private supergroup used for bot task messages.
Add its ID to `AUTHORIZED_CHATS`. The owner account is used as the task owner.

Free Koyeb instances have limited disk, RAM, and CPU. Keep `BOT_MAX_TASKS` and
`QUEUE_ALL` at 1 or 2 and do not download files larger than the available
ephemeral disk.

If Koyeb reports `No module named bot.__main__`, verify that `Dockerfile`,
`start.sh`, and the `bot` directory are at the root of the GitHub repository,
then redeploy without a custom Run command. The Dockerfile should run
`bash start.sh`.

## Commands

- `/addsite URL [auto|rss|html|mv] [name]`
- `/autosites`
- `/delsite URL`
- `/checksites`
- `/mv` (uses `MV_SITE_URL`, but does not auto-monitor it)
- `/mv URL` (one-time MV-compatible site browse)

The management commands require sudo access. `/mv` requires normal bot
authorization.

`MV_SITE_URL` is only a default browse URL for `/mv`.  Sites are monitored only
after you add them with `/addsite`, and `/delsite` removes them permanently.

## Scraper-only mode

Set `SCRAPER_ONLY=true` when this bot should only watch sites and forward
`/qbleech magnet...` commands to `AUTO_MONITOR_CHAT` for a separate WZML-X bot.
In this mode qBittorrent, aria2, and local WZML-X leech handlers are not started.

For the separate WZML-X bot to react, add both bots to the same supergroup,
make the WZML-X bot admin, and turn off its BotFather group privacy if needed.
Set `SCRAPER_ONLY=false` to return to the all-in-one bot.

## Auto-forwarding

Put comma- or space-separated IDs/usernames in `AUTO_FORWARD_CHATS`, for
example:

`-1001111111111,-1002222222222`

The monitor registers these as WZML-X owner dump destinations and launches
automated tasks with `-ud all`. Existing owner dump destinations are preserved.

## Site behavior

- `rss`: reads feed enclosures and follows item pages when needed.
- `html`: extracts direct links and follows same-domain detail pages.
- `mv`: understands the `ipsType_break ipsContained` topic layout used by the
  supplied MV bot and falls back to same-domain topic links.
- `auto`: tries RSS first, then HTML.

By default, the first check records currently visible items as a baseline and
only future additions are leeched. Set `AUTO_LEECH_EXISTING=true` only if you
intentionally want the first check to queue existing results.

Some sites use Cloudflare, JavaScript challenges, login cookies, or frequently
change their HTML. Those sites need a dedicated scraper and cannot be promised
to work permanently from a generic parser.

The monitor retries transient failures and then uses a browser-compatible
fallback. If a site still blocks Koyeb:

- Set `AUTO_SITE_COOKIE` to a valid browser Cookie header copied from your own
  authorized session.
- Or set `AUTO_SITE_PROXY` to an HTTP/HTTPS proxy URL.
- `AUTO_FETCH_RETRIES` and `AUTO_FETCH_TIMEOUT` control retry behavior.

Site URLs are normalized, so forms such as `https://example.com` and
`https://example.com/` are merged instead of being monitored twice.

On Koyeb Free, keep `TELEGRAM_WORKERS=8`, `TELEGRAM_TRANSMISSIONS=2`,
`BOT_MAX_TASKS=2`, and `QUEUE_ALL=2`. Higher values can cause temporary
`service_unavailable` responses through memory or CPU exhaustion.

## Verification checklist

1. Run `/addsite` for one permitted test feed.
2. Run `/checksites` once to establish the baseline.
3. Add or wait for a new permitted test item and run `/checksites` again.
4. Confirm a task appears in the WZML-X status queue.
5. Confirm the file reaches `LEECH_LOG_ID`.
6. Confirm copies reach every `AUTO_FORWARD_CHATS` channel.
7. Run `/checksites` again and confirm the same link is not queued twice.
