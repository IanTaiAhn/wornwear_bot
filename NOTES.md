# Bot Notes & TODOs

## Pending

- [ ] Add more search terms to `bot.py` that are specific to vintage and mens.
  Do this for: shirts, pants, shorts, hats, belts.
- [ ] For certain rare items, maintain a `rare_items.json` that signals the bot
  is allowed to bag them again even if they already exist in `seen_items.json`.

## Done

- [x] Added vintage/mens search URLs for shirts, pants, shorts, hats, and belts
      to `TARGET_URLS` in `bot.py`.
- [x] Created `rare_items.json` — style numbers and URL patterns listed there
      bypass the `seen_items.json` dedup check so the bot will re-bag them
      whenever they surface again.
- [x] Fixed `add_to_cart()` reporting "Item Bagged!" for items it never
      actually added: it now bails out (returns `False`) when no valid
      color/size combo could be selected, and no longer assumes success when
      no confirmation banner is found — it also checks whether the header
      cart count increased before declaring success.
- [x] Split into two concurrent loops sharing one browser (`run_general_loop`
      + `run_grail_loop` in `bot.py`, launched together from `run()`):
        - General loop: unchanged broad `TARGET_URLS` scan, current cadence.
        - Grail loop: watches only `rare_items.json` style numbers via narrow
          `?q=<style>` searches (no scroll/Load-More pagination needed for
          those), on a much tighter interval (`GRAIL_POLL_MIN/MAX`, default
          10-20s), across a few concurrent tabs (`GRAIL_TABS`). Uses a
          cooldown (`GRAIL_COOLDOWN_SECONDS` / `GRAIL_RETRY_COOLDOWN_SECONDS`)
          instead of `seen_items.json` so it doesn't spam re-attempts on the
          same still-listed item every cycle. The general loop no longer
          special-cases rare items — the grail loop owns that job.

### Bugs and problems:
- The bot still tries to bag grail items and gives me a false positive that they're in my cart.
- I have some perma-stuck items in the cart that the bot seems to continually bag even though they're not grails and not new.
- It takes 8-10 minutes to completely poll through all of the URLs I have. This is too slow for the grail hunt... meaning, I need to sift through the garbage, and have multiple tabs hitting the keywords I think?
