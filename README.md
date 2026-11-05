# Tripper

Tripper checks Google Flights calendar quotes through SerpApi or Apify and sends a Telegram message each time a watch sets a lower observed price. The first price sets the baseline. A watch can combine departure airports, destination airports, a window of departure dates, and several trip lengths. A scan compares the eligible date pairs and records its cheapest returned price. Both providers use Google Flights, so the fallback can recover from a provider problem but shares Google's airline and seller coverage.

This version monitors economy calendar quotes in USD for adults. Calendar quotes can change before booking, and the feed does not expose stop, baggage, or seller filters. Alerts include the route, dates, and a search link so you can confirm the fare. Editing a watch's criteria or name starts a fresh baseline.

## Configure a watch

Use Python 3.11 or newer. The script uses the standard library.

1. Copy `watches.example.json` to `watches.json` and edit the example airports and dates. Use three-letter airport codes and YYYY-MM-DD dates. Set `trip_days` to a list such as `[6, 7, 8]` for three possible round-trip lengths or `[0]` for one way. A round trip can also set `"return_through": "2027-01-15"` to limit the return flight's departure date, inclusive. If you need to land by that day, check the arrival time before booking.
2. Copy `.env.example` to `.env` next to `tripper.py`. If you already have an `.env`, add any missing names from the example to it. Create a [free SerpApi account](https://serpapi.com/pricing) and put your API key after `SERPAPI_KEY=` in `.env`.
3. To compare providers, create an [Apify Free account](https://apify.com/pricing) and put an [Apify API token](https://console.apify.com/account/integrations) after `APIFY_TOKEN=`. Keep both accounts on their free plans.
4. Create a bot with [BotFather](https://t.me/BotFather), open a private chat with it, and tap **Start**. Put the token after `TELEGRAM_BOT_TOKEN=` in `.env`. Run the commands below to get your private chat ID, then put it after `TELEGRAM_CHAT_ID=`.

```powershell
$tripperToken = Read-Host 'Telegram bot token'
$tripperUpdates = Invoke-RestMethod "https://api.telegram.org/bot${tripperToken}/getUpdates"
$tripperUpdates.result.message.chat | Where-Object type -eq private | Select-Object id, first_name -Unique
Remove-Variable tripperToken
```

Git ignores `.env` and `watches.json`. Enter one `NAME=value` pair per line. Quotes around values and full-line comments starting with `#` are supported. Windows environment variables with the same names take precedence. Run `py -3 tripper.py --test-alert` to confirm your phone receives a message.

## Compare the providers before switching

From this folder, run:

```powershell
py -3 tripper.py --source apify --plan 2
py -3 tripper.py --pilot
```

The first command estimates the budget without contacting either provider. The pilot uses both providers' allowances. It compares the number of eligible date pairs each provider priced, their overlap, price agreement, and the cheapest quote from each. Open the printed links and check the route, dates, and final fare. The pilot leaves price records and queued alerts alone and works without Telegram credentials. If Apify cannot account for every eligible pair, the pilot reports the gap and exits with an error after showing the SerpApi comparison.

After a successful pilot, use `py -3 tripper.py --source apify` for routine scans. Tripper tries Apify first and uses a full SerpApi scan if Apify fails, misses eligible date pairs, or reaches its budget. An explicit no-fare row counts as checked. A missing or invalid row does not. Alerts identify the provider used. To use SerpApi alone, run `py -3 tripper.py`.

## Schedule scans

In Windows Task Scheduler, use the Python executable path from `py -3 -c "import sys; print(sys.executable)"` as the program. For Apify, set the arguments to the full path to `tripper.py` in quotes followed by `--source apify`. Set **Start in** to this folder and run the task as your Windows user. Use two daily triggers, such as 8 a.m. and 8 p.m., if the budget preview fits. Enable **Run task as soon as possible after a scheduled start is missed**. The computer needs to be available to check fares.

Apify Free includes $5 in monthly usage credit and pauses if it runs out. Tripper batches each watch into one [Actor run](https://apify.com/alom/google-flights-scraper), with a cap based on the [calendar row price](https://apify.com/alom/google-flights-scraper/pricing) and a $0.005 allowance for the run start. It reserves the full cap before starting the run and limits its own reservations to $4.50 in any rolling 31 days. The example watch reserves about $4.28 at two scans per day, before other use on the same Apify account. A watch with more than 100 route and length combinations or a calendar window over 330 days uses SerpApi instead.

SerpApi's free plan includes 250 searches per month. Tripper caps its use at 240 attempts in any rolling 31 days and skips a watch if the remaining quota cannot cover it in full. Each fallback uses one request per eligible airport pair and trip length. The example needs 32 requests, so only seven full fallbacks fit the rolling cap. Budget other use of the same keys separately.

The database in `%LOCALAPPDATA%\Tripper` keeps record prices, request counts, and unsent alerts between runs. Failed deliveries are retried at the next scan. If Telegram accepts a message immediately before a crash or connection loss, a retry can send it twice.

## Recheck an alerted fare with Viajante

[Viajante](https://github.com/felipebasurto/viajante) is optional and does not use an API key. After installing it, substitute the airport codes and dates from an alert to search that exact round trip:

```powershell
viajante flights JFK-LIS:2027-04-03:2027-04-12 --trip rt --fetch sweep --currency USD --max-stops 2 --sort fare
```

Match the number of adults with `--adults`. This example allows up to two stops. A quote can change between checks, so confirm the final fare on the seller's page.
