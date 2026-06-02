# Roadmap Notes

## Price-drop monitoring

Current sprint priority: turn saved searches into real monitoring, not daily report spam.

- Ask the user for a percentage threshold during search setup.
- Store the last observed best route price as the monitoring baseline.
- Send Telegram alerts only when the fresh best price drops by at least the configured percentage.
- Do not update baseline from partial API data or empty results.
- Keep manual refresh available for "ready to buy now" checks.

## Baggage-aware buffers

The solver already applies conservative self-transfer buffers:

- carry-on: minimum 2 hours;
- checked baggage: minimum 4 hours;
- airport change: minimum 6 hours;
- manual ground legs: use configured duration, then apply the next-transfer buffer.

Future improvements:

- store `arrival_at` from providers when available;
- detect airport changes inside city codes such as MOW, TYO, LON;
- show a clear risk label for estimated timings;
- let the user choose baggage profile per route or per search;
- add tests for airport-change buffers and missing-duration fallbacks.

## Deferred

- hotels and lodging APIs;
- package tours and charters;
- round-trip search;
- full visa engine beyond blacklist/warnings.
