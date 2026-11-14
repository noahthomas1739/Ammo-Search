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

- **GREAT / EXCEPTIONAL**: alerts you right away. The same deal is not re-sent unless it drops another 0.5¢/rd, moves up a tier, or is still live a week later.
- **GOOD**: added to a daily digest, sent around 7:40 am Central and only if there's something in it.
- **Nothing qualifies**: nothing is sent.
- **Monthly** (1st of the month): for each caliber, whether prices went up or down versus the prior month, with suggested new thresholds if the market moved 5% or more.
- **Suspiciously low prices** (e.g. 7.62x54R under 30¢, 9mm under 15¢) are flagged **VERIFY**. They may be listing errors, wrong round counts, or scams.
- **Every alert includes** the product, retailer, delivered ¢/rd, total with shipping and tax, stock quantity (when the store shows it), corrosive/steel-core/Berdan flags for 7.62x54R, and a direct link.
- **12 ga alerts** say *buy ONE case only* until you set `a5_cycling_confirmed: true` in `config.yaml`.
- **Excluded**: out of stock, backorder, pre-order, marketplace sellers (GunBroker etc.), reloaded/remanufactured ammo, steel/aluminum-case 9mm, steel/bismuth/tungsten shot, 3" and 3-1/2" shells, and buckshot/slugs.
- **Small boxes are priced as a real order.** A 50-round box is priced as ten boxes plus shipping, so "cheap" small listings don't slip through. If buying one more unit unlocks free shipping and lowers the per-round cost, the alert says so.

## Setup (about 10 minutes)

### 1. Pick how you want to be alerted

Add these as repository secrets: **Settings → Secrets and variables → Actions → New repository secret**. You can set up more than one.

**Option A: ntfy push notifications (easiest, free).** Install the ntfy app on your phone and subscribe to a hard-to-guess topic name, e.g. `ammo-7f3k29x`. Then add the secret `NTFY_TOPIC` = that name. Anyone who knows the topic can read it, so make it random.

**Option B: real SMS through Twilio (about $1/month plus a fraction of a cent per text).** Add the secrets `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` (your Twilio number, `+1...`), and `ALERT_PHONE` (your cell, `+1...`). US numbers must be registered (A2P 10DLC or toll-free verification) before Twilio will deliver texts.

**Option C: email.** Add the secrets `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, and `ALERT_EMAIL`. With Gmail, use an app password. `ALERT_EMAIL` can be a carrier email-to-text address if your carrier still supports one.

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
- **GitHub schedules run late**, often by 5–30 minutes. GitHub also pauses scheduled workflows in repos with no activity for 60 days. If that happens, re-enable the workflow in the Actions tab.
