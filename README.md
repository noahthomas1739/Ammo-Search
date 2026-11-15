# Ammo Finder

An ammo deal monitor that runs for free on GitHub Actions. Every 6 hours it checks 11 retailers plus AmmoSeek and gun.deals. It works out the **delivered** cost per round (item + shipping + Texas sales tax) and alerts you only when a deal meets your thresholds.

| Caliber | What counts | GOOD | GREAT | EXCEPTIONAL |
|---|---|---|---|---|
| 9mm | 115/124gr FMJ, brass case, 500+ rds | ≤25¢ | ≤23¢ | ≤21¢ (buy 2,000+) |
| 12ga Priority A | 2-3/4" lead 1-1/4 oz, #6/7.5/8/9, ~1300+ fps, 250 | ≤58¢ | ≤50¢ | ≤45¢ |
| 12ga Priority B | 2-3/4" lead 1-1/8 oz, 1250+ fps, 250 | ≤42¢ | ≤36¢ | — |
| 7.62x54R | new brass FMJ, or surplus 440/880 sealed cans | ≤55¢ | ≤45¢ | ≤40¢ |

A 440-round 7.62x54R can also counts as GOOD at ≤$240 delivered and GREAT at ≤$200.

## How it behaves

- **GREAT / EXCEPTIONAL**: emailed right away. The same deal is not re-sent unless it drops another 0.5¢/rd, moves up a tier, or is still live a week later.
- **GOOD**: added to a daily digest, sent around 7:40 am Central and only if there's something in it.
- **Nothing qualifies**: nothing is sent.
- **Monthly** (1st of the month): for each caliber, whether prices went up or down versus the prior month, with suggested new thresholds if the market moved 5% or more.
- **Suspiciously low prices** (e.g. 7.62x54R under 30¢, 9mm under 15¢) are flagged **VERIFY**. They may be listing errors, wrong round counts, or scams.
- **Every alert includes** the product, retailer, delivered ¢/rd, total with shipping and tax, stock quantity (when the store shows it), corrosive/steel-core/Berdan flags for 7.62x54R, and a direct link.
- **12 ga alerts** say *buy ONE case only* until you set `a5_cycling_confirmed: true` in `config.yaml`.
- **Excluded**: out of stock, backorder, pre-order, marketplace sellers (GunBroker etc.), reloaded/remanufactured ammo, steel/aluminum-case 9mm, steel/bismuth/tungsten shot, 3" and 3-1/2" shells, and buckshot/slugs.
- **Small boxes are priced as a real order.** A 50-round box is priced as ten boxes plus shipping, so "cheap" small listings don't slip through. If buying one more unit unlocks free shipping and lowers the per-round cost, the alert says so.

## Setup (about 10 minutes)

### 1. Set up email alerts (Gmail)

1. Turn on 2-Step Verification for your Google account if it isn't already on (myaccount.google.com → Security).
2. Create an **App Password** at myaccount.google.com/apppasswords. Name it "ammo monitor" and copy the 16-character password. Your normal Gmail password will not work.
3. In this repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `SMTP_USER` = your Gmail address
   - `SMTP_PASSWORD` = the 16-character App Password

Alerts are emailed to that same Gmail address. To send them somewhere else (or to several addresses, comma-separated), also add `ALERT_EMAIL`. For a provider other than Gmail, add `SMTP_HOST` (and `SMTP_PORT` if it isn't 587).

Secrets are encrypted and never appear in the public repo or the run logs.

*Optional extra channels:* `NTFY_TOPIC` for ntfy phone push notifications, or `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM` / `ALERT_PHONE` for real SMS through Twilio (US numbers must be registered with Twilio first). Every configured channel gets every alert.

### 2. Test the alerts

Go to **Actions → Ammo deal monitor → Run workflow** and choose `test-alert`. You should get a message.

### 3. Check which store pages work

Run the workflow again with `doctor`. The run summary shows how many products each store page returned, with samples. Some pages will need fixing:

- A page with **0 products or a failure** means the URL moved or the store blocks automated requests. Open the store, find its 9mm / 12 ga / 7.62x54R listing or search page, and paste that URL into `config.yaml` under that store's `pages`.
- Some stores (often behind Cloudflare) block GitHub's servers no matter what. AmmoSeek and gun.deals still cover most of those stores.

### 4. Correct the shipping estimates

Little shipping-policy information could be confirmed online, so most stores use a placeholder of **$14.99 flat** (PSA $19.99). Brownells is set to free over $199, and Target Sports USA to free on case quantities. Alerts say "Shipping is an estimate" until you put a cart through checkout at each store, fix the `shipping:` numbers in `config.yaml`, and set `verified: true`.

Also set `sales_tax_rate` to your exact local rate. The default is 8.25%.

After that it runs on its own.

## Running it on a Mac instead of GitHub

Most stores refuse requests from GitHub's servers. From a home internet connection, more of them answer normally. On the Mac, the monitor runs when you log in, every 6 hours while the Mac is awake, and right after it wakes if a check was missed. The daily digest and monthly report go out on the first run after they're due. A week with the laptop closed is fine.

1. Open **Terminal** and paste:
   ```bash
   git clone https://github.com/noahthomas1739/Ammo-Search.git ~/ammo-search && bash ~/ammo-search/mac/install.sh
   ```
   If macOS asks to install "command line developer tools", click Install, then paste the line again.
2. Enter your Gmail address and App Password when asked. A test email is sent.
3. Turn off the GitHub schedule so you don't get duplicate emails: **Actions → Ammo deal monitor → ⋯ → Disable workflow**.

Change thresholds by editing `config.yaml` on GitHub. The Mac pulls the change before each run. To see what the monitor is doing, run `tail -f ~/Library/Application\ Support/AmmoFinder/monitor.log`. To remove it, run `bash ~/ammo-search/mac/uninstall.sh`.

Stores that show a bot check ("Just a moment...") are reported as blocked and skipped, not worked around.

## Files

- `config.yaml`: thresholds, retailers, shipping rules, and page URLs. Edit thresholds here when the monthly report suggests it.
- `state/state.json`: the bot's memory (what was already sent, the digest queue, daily price history). The workflow commits it back to the repo after each run. Don't edit it by hand.
- `ammo_finder/`: the code. `parse.py` reads product titles, `rules.py` holds the deal rules, `pricing.py` computes delivered cost, and `sources/extract.py` does the page scraping.

## Running locally

```bash
pip install -r requirements.txt
pytest -q                                  # offline tests with sample pages
python -m ammo_finder doctor --only Lucky  # check one store's pages
python -m ammo_finder check --dry-run      # full run, print alerts instead of sending
```

## Known limits

- **Scraping is best effort.** Store pages are read generically (embedded product data, tables, product cards) rather than with per-store code, so layout changes break less often. Some stores will still block automated requests or need URL fixes. Re-run `doctor` if alerts go quiet. If no page returns products for 36 hours, the bot sends a "no data" warning (at most every 3 days).
- **Stock quantity** is only shown when the store displays it. Otherwise the alert says "qty not shown".
- **Titles must state the specs.** A 12 ga load whose title doesn't state velocity is still alerted, with a "verify velocity" note. A 1-1/8 oz load must state 1250+ fps.
- **GitHub schedules run late**, often by 5–30 minutes.
- **The 60-day pause is handled.** GitHub pauses scheduled workflows in public repos after 60 days without activity. Each run re-enables the workflow through GitHub's API, which resets that timer. If it ever does get paused (for example, Actions was turned off), turn it back on under **Actions → Ammo deal monitor → Enable workflow**.
