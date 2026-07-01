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
