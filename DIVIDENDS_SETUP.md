# Dividend auto-capture — one-time IB Flex setup (~2 minutes)

Dividends arrive as cash credits, not trades, so the bot's execution sweep
cannot see them. IB's **Flex Web Service** exposes them read-only; once
connected, the VM pulls the statement daily inside the normal publish cycle
and the dashboard's Tax page gains a Dividends section automatically
(payment date, gross, foreign tax withheld, GBP at payment-date ECB rate,
£500-allowance tracker, third CSV in the accountant export).

## Step 1 — create the Flex Query (IB Client Portal)

1. Log in to Client Portal → **Performance & Reports → Flex Queries**.
2. Under **Activity Flex Query**, click **+** (create).
3. Name: `dividends` (anything works).
4. In **Sections**, tick **Cash Transactions** only, and inside it select at
   least: *Dividends*, *Payment In Lieu Of Dividends*, *Withholding Tax*.
   Fields: tick All (or at minimum: Symbol, Conid, Currency, Amount, Type,
   SettleDate, Report Date, Description, Transaction ID).
5. Delivery configuration: Period = **Last 365 Calendar Days**,
   Format = XML.
6. Save. Note the **Query ID** number shown in the list.

## Step 2 — enable Flex Web Service + token

1. Client Portal → **Settings → Account Settings → Flex Web Service** (under
   Reporting) → toggle **ON**.
2. Click **Generate token** (choose a long expiry, e.g. 1 year — set a
   calendar reminder to renew). Copy the token.

## Step 3 — put both values on the VM (you type this, not Claude)

Open Oracle Cloud Shell and run (replace the two placeholders):

    ssh -i ~/mp_vm_key opc@152.67.158.175 'sudo sh -c "printf \"FLEX_TOKEN=YOUR_TOKEN\nFLEX_QUERY_ID=YOUR_QUERY_ID\n\" > /root/flex.conf && chmod 600 /root/flex.conf && echo FLEX_CONF_SAVED"'

That's all. The next hourly publish runs `flex_dividends.capture_if_configured()`,
seeds `data/dividends_ledger.jsonl`, and the Tax page fills in. The token never
enters the repo or leaves the VM (`/root/flex.conf`, mode 600).

## Notes

- The 365-day query window means past dividends (including WEN/BEN payments
  from before capture existed) backfill automatically on the first pull.
- GBP conversion uses the ECB reference rate **on the payment date**
  (frankfurter.dev). Payments whose rate lookup fails are flagged
  "AWAITING_FX_RATE" and excluded from headline totals until a later run
  fills them in.
- UK treatment (shown on the page): the **gross** dividend is the taxable
  income; £500/yr allowance, then 10.75% / 35.75% / 39.35% by band (2026/27
  rates). Foreign withholding (US 15% via W-8BEN) may be claimable as Foreign
  Tax Credit Relief, capped at the LOWER of the treaty rate and the UK tax
  actually due on that dividend — dividends inside the £500 allowance bear 0%
  UK tax, so their withholding earns no credit. Payments-in-lieu from stock
  lending are typed `pil` and are NOT treaty dividends. Foreign dividends over
  £2,000/yr usually need the SA106 foreign pages — accountant confirms.
