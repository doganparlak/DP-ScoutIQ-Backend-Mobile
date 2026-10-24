# Referral subscription tracking

Backend-only; no mobile build is required. Analytics never changes entitlements.

## Database

The referral billing database changes were applied on 24 October 2026.
They extend existing tables; they create no tables. They also create active referral
code TCX with the same iOS/Android Plus Monthly, Pro Monthly and Pro Yearly offers
as SWX. No store-side offer creation is necessary for TCX.

Historical referral attachment dates were unavailable. Existing accounts start
at the migration timestamp. Earlier purchases are deliberately not retroactively
assigned to partners. Their subsequent renewals remain renewals, not new users.

Each subscription's referral code is frozen in `subscription_entitlements`.
Verified Google replacement tokens inherit the previous subscription's code.
Manual Pro grants are excluded. Sandbox transactions are retained with
`is_test=true` and excluded from production reports.

## Runtime

After `/me/subscription/iap` commits activation, a best-effort background task
collects verified store transactions. It uses a separate database session.
Store/analytics outages cannot change the activation response or the user's plan.
The existing daily `scripts/sync_subscriptions.py` cron reconciles transactions
again after the existing entitlement/user sync has committed. Missed runs recover
Apple history and Google successful renewal order IDs. Existing transaction IDs
are deduplicated by the database's unique index, including webhook/cron races.

Report timezone: `Europe/Istanbul`. `referral_monthly_reports` is rebuilt from the
live, non-test ledger. New paid users require a first positive payment (or a
recorded zero-price period followed by a positive payment). Restores, tests,
manual grants and zero-price periods don't count as new paid users. Renewals,
reactivations and plan changes have separate counters. Refunds don't erase the
original charge. Refund amounts and gross charges are reported separately by
currency, never summed across currencies. `unpriced_charges` explicitly identifies
transactions where the store did not supply a price. Unknown prices aren't zero.
These are gross customer payments, not net store payouts or accounting statements.

## Enable production notifications after backend deployment

Endpoints are ready but must be registered in the stores after deployment:

- Apple App Store Server Notifications V2:
  `https://dp-scoutiq-backend-mobile.onrender.com/billing/notifications/apple`
  Configure production and sandbox URLs in App Store Connect. The handler verifies
  Apple's public certificate chain, signed payload, bundle and app identity.
  Official Apple root certificates are bundled under `api_module/certificates`.
  `APPLE_APP_ID` defaults to the existing ScoutWise app ID `6755595849`.
  Existing `APPLE_BUNDLE_ID` and IAP API credentials are reused.
- Google Play real-time developer notifications: select a Pub/Sub topic in Play
  Console. Configure an **authenticated** Pub/Sub push subscription targeting
  `https://dp-scoutiq-backend-mobile.onrender.com/billing/notifications/google` and set:
  - `GOOGLE_RTDN_AUDIENCE`: the exact push endpoint URL / configured OIDC audience.
  - `GOOGLE_RTDN_SERVICE_ACCOUNT_EMAIL`: the service account selected for Pub/Sub
    push authentication (not the publishing service account).
  - Existing `GOOGLE_PLAY_PACKAGE_NAME` and Google Play service account JSON are
    reused for read-only Developer API calls. They need subscription/order reads.

The Google endpoint fails closed while its authentication settings are absent.
Notifications retry on analytics outages. Unknown subscriptions return retryable
responses until the app's verified purchase links them to an account. Daily sync
works without notifications once this backend is deployed.

`REFERRAL_MAX_GOOGLE_ORDERS` defaults to 120 orders per subscription history walk;
increase it if logs report a history-limit failure. Errors log exception types,
never purchase tokens, receipts or customer emails.

## Source field definitions

- Apple transaction price is in milliunits:
  https://developer.apple.com/documentation/appstoreservernotifications/price
- Google Orders API supplies actual transaction totals and refund history:
  https://developers.google.com/android-publisher/api-ref/rest/v3/orders
- Subscription renewal order IDs use `..0`, `..1`, etc.:
  https://support.google.com/googleplay/android-developer/answer/6135870

No repository test files were added. Internal integration checks ran separately,
with synthetic database mutations rolled back.
