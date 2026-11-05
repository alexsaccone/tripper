# Tripper

Tripper checks the Google Flights calendar through SerpApi and sends a Telegram message each time a watch sets a lower observed price. The first price sets the baseline. A watch can combine departure airports, destination airports, a window of departure dates, and several trip lengths. A scan compares all combinations within the watch and records its cheapest returned price.

This version monitors economy calendar quotes in USD for adults. Calendar quotes can change before booking, and the feed does not expose stop, baggage, or seller filters. Alerts include the route, dates, and a search link so you can confirm the fare. Editing a watch's criteria or name starts a fresh baseline.

## Configure a watch

Use Python 3.11 or newer. The script uses the standard library.

1. Copy `watches.example.json` to `watches.json` and edit the example airports and dates. Use three-letter airport codes and YYYY-MM-DD dates. Set `trip_days` to a list such as `[6, 7, 8]` for three possible round-trip lengths or `[0]` for one way.
2. From this folder, run `py -3 tripper.py --plan 1` to estimate search usage at one scan per day. Change the final number to try a different cadence. This command needs no credentials.
3. Copy `.env.example` to `.env` next to `tripper.py`. Create a [free SerpApi account](https://serpapi.com/pricing) and put your API key after `SERPAPI_KEY=` in `.env`.
4. Create a bot with [BotFather](https://t.me/BotFather), open a private chat with it, and tap **Start**. Put the token after `TELEGRAM_BOT_TOKEN=` in `.env`. Run the commands below to get your private chat ID, then put it after `TELEGRAM_CHAT_ID=`.

```powershell
$tripperToken = Read-Host 'Telegram bot token'
$tripperUpdates = Invoke-RestMethod "https://api.telegram.org/bot${tripperToken}/getUpdates"
$tripperUpdates.result.message.chat | Where-Object type -eq private | Select-Object id, first_name -Unique
Remove-Variable tripperToken
```

Git ignores `.env`. Enter one `NAME=value` pair per line. Quotes around values and full-line comments starting with `#` are supported. Windows environment variables with the same names take precedence. Run `py -3 tripper.py --test-alert` to confirm your phone receives a message. Run `py -3 tripper.py` to scan and establish the baseline.

## Schedule scans

In Windows Task Scheduler, create a daily task with the Python executable path from `py -3 -c "import sys; print(sys.executable)"` as its program and the full path to `tripper.py` in quotes as its argument. Set **Start in** to this folder. Run it as your Windows user. Match the triggers to the cadence you checked with `--plan`. Enable **Run task as soon as possible after a scheduled start is missed** to catch up after a missed start. The local setup needs the computer available to check prices.

SerpApi's free plan includes 250 searches per month. Each scan uses one search per origin, destination, and trip length combination, covering the entire departure window in that search. For example, three combinations scanned twice daily take at most 186 requests over 31 days. Tripper caps itself at 240 attempts in any rolling 31 days, and skips watches it cannot scan in full within the remaining budget. Stay on the provider's free plan and count any other uses of the same API key separately.

The database in `%LOCALAPPDATA%\Tripper` keeps record prices, request counts, and unsent alerts between runs. Failed deliveries are retried at the next scan. If Telegram accepts a message immediately before a crash or connection loss, a retry can send it twice.
