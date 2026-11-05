# Tripper

Tripper checks the Google Flights calendar through SerpApi and sends a Telegram message each time a watch sets a lower observed price. The first price sets the baseline. A watch can combine departure airports, destination airports, a window of departure dates, and several trip lengths. A scan compares all combinations within the watch and records its cheapest returned price.

This version monitors economy calendar quotes in USD for adults. Calendar quotes can change before booking, and the feed does not expose stop, baggage, or seller filters. Alerts include the route, dates, and a search link so you can confirm the fare. Editing a watch's criteria or name starts a fresh baseline.

## Configure a watch

Use Python 3.11 or newer. The script uses the standard library.

1. Copy `watches.example.json` to `watches.json` and edit the example airports and dates. Use three-letter airport codes and YYYY-MM-DD dates. Set `trip_days` to a list such as `[6, 7, 8]` for three possible round-trip lengths or `[0]` for one way.
2. From this folder, run `python tripper.py --plan 1` to estimate search usage at one scan per day. Change the final number to try a different cadence. This command needs no credentials.
3. Create a [free SerpApi account](https://serpapi.com/pricing) and set your API key as the Windows user environment variable `SERPAPI_KEY`.
4. Create a bot with [BotFather](https://t.me/BotFather), open a private chat with it, and tap **Start**. Save the token as the Windows user environment variable `TELEGRAM_BOT_TOKEN`. Open a new PowerShell session and run the command below to see the chat ID from your private message. Save it as the Windows user environment variable `TELEGRAM_CHAT_ID`.

```powershell
$tripperUpdates = Invoke-RestMethod "https://api.telegram.org/bot$($env:TELEGRAM_BOT_TOKEN)/getUpdates"
$tripperUpdates.result.message.chat | Where-Object type -eq private | Select-Object id, first_name -Unique
```

Keep credentials in your environment. You can add user variables under **Edit environment variables for your account** in Windows and then open a new terminal. Run `python tripper.py --test-alert` to confirm your phone receives a message. Run `python tripper.py` to scan and establish the baseline.

## Schedule scans

In Windows Task Scheduler, create a daily task with the full path from `(Get-Command python).Source` as its program and the full path to `tripper.py` in quotes as its argument. Set **Start in** to this folder. Run it as the same Windows user who set the environment variables. Match the triggers to the cadence you checked with `--plan`. Enable **Run task as soon as possible after a scheduled start is missed** to catch up after a missed start. The local setup needs the computer available to check prices.

SerpApi's free plan includes 250 searches per month. Each scan uses one search per origin, destination, and trip length combination, covering the entire departure window in that search. For example, three combinations scanned twice daily take at most 186 requests over 31 days. Tripper caps itself at 240 attempts in any rolling 31 days, and skips watches it cannot scan in full within the remaining budget. Stay on the provider's free plan and count any other uses of the same API key separately.

The database in `%LOCALAPPDATA%\Tripper` keeps record prices, request counts, and unsent alerts between runs. Failed deliveries are retried at the next scan. If Telegram accepts a message immediately before a crash or connection loss, a retry can send it twice.
