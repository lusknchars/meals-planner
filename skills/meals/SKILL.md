---
name: meals
description: Food prices, meals, shopping lists, calories, groceries
---

# Meals

Use `python3 /opt/hermes/skills/meals/scripts/meals.py --help` for commands. The
script persists everything in this installation's Hermes home. Its JSON output is
the receipt: if the command fails, nothing was saved, and you say so.

Pass `--scope <conversation-id>` on every command, taking the ID from the current
trusted Plow conversation. If it is unavailable, ask for setup rather than
inventing an ID or reusing another room. This keeps one household's food out of
another's.

## Before planning

`profile set` needs at least `--people` and `--calories`. Ask for what is missing
rather than assuming: how many people eat, the daily calorie target per person,
the budget for the period, any diet restrictions as comma separated tags such as
`vegetarian,gluten-free`, and `--lat`/`--lon` if they want ordering. Restrictions
are exclusions the catalogue must satisfy, so a wrong tag silently narrows every
plan; read them back once when they are set.

## Daily targets

When somebody wants a calorie or macro target, `profile set` takes `--age`,
`--sex`, `--height-in`, `--weight-lb`, `--activity` and `--goal`. Ask for what is
missing in one short message rather than a form: activity is sedentary, light,
moderate, very active or athlete; the goal is fat loss, maintenance, muscle gain
or performance.

`targets` then returns BMR, TDEE, the calorie target and grams of protein, carbs
and fat, plus fibre and water. **Report what it returns and never compute any of
it yourself.** If the numbers look wrong, say so and check the inputs; do not
substitute your own arithmetic.

When `floored` is true the target was held at 1,200 kcal for a woman or 1,500 for
a man because the goal implied less. Say that plainly, say that going lower needs
medical supervision, and do not offer a way around it.

Give the `note` with the numbers. You hold no certification and never imply
otherwise: these are published formulas applied to what the person told you, and
a pre-existing condition, pregnancy or a history of disordered eating belongs
with a physician.

## Planning a week

1. `plan --days 7 --start YYYY-MM-DD` returns each day's meals with calories and
   cost, the week's total and whether it passes the budget.
2. Running it again for the same start returns the saved week rather than a new
   one. Say that it is the existing plan, not a fresh idea.
3. `shopping --start YYYY-MM-DD` aggregates that plan into one list, multiplied
   by the number of people. Give it back in `sections`, not as `items`: produce
   first, then bakery, meat and fish, dairy, pantry, frozen, and whatever nobody
   categorised under "Other". That is the order somebody walks a shop, and it
   stops a list sending them from avocado to bread to beans and back.

   One line per section, with its symbol and its own total, reads well on a
   phone. Use the `emoji` each section carries rather than choosing your own —
   the same food should not be a different symbol each week:

   ```
   🥬 Produce $28.68 - avocado 4, banana 8, berries 400 g, spinach 240 g
   🥛 Dairy $12.88 - eggs 22, milk 1200 ml, yoghurt 600 g
   🫙 Pantry $18.41 - oats 320 g, rice 660 g, lentils 480 g
   ```

   `items` still holds the flat list when somebody asks for one thing.
4. Report the per-day calories against the target and the total against the
   budget. When `over_budget` is true, say so plainly and offer to replan with
   cheaper recipes rather than hiding it.

   **Bold the dish, and lead it with the emoji the plan gives you.** Each meal
   carries an `emoji`; use that one rather than choosing your own, so a dish
   keeps the same face from week to week. The name is what somebody scans for,
   so it is the thing in bold — not the calories, not the price:

   ```
   📅 **Mon** 1,830 kcal, $11.91
   🍏 **Yoghurt with apple and almonds**
   🍝 **Beef and tomato pasta**
   🥩 **Beef with potatoes and peas**
   ```

   A whole week that way is long. Offer the detail rather than sending it: give
   two or three days in full, then the totals, and ask whether they want the
   rest. When they want it compact, keep one line a day with the emoji before
   each dish and the name still in bold.
5. Name the product when a line came from a store: the brand and what it is, as
   in "Kroger Plain Low Fat Yogurt, $2.99 for 32 oz". `brand` is null for about
   one row in eight, so say the description alone then — never the word "None".
   A line whose `price_source` is `catalogue estimate` has no product to name;
   call it an estimate instead of dressing it up.
6. `value` compares this product's price to what the same ingredient costs at
   this store today, against `median_unit_price`. `best value` and `good value`
   mean cheaper than the usual price here; `typical` means it costs about what
   the alternatives do; `only priced option` means there was nothing to compare
   with. Say it as a price comparison, because that is all it is — never as a
   claim about quality, health, or whether the food is worth eating. When
   somebody asks why a line is good value, give the two numbers.

Calories and prices come from the catalogue shipped with this skill. They are
reference figures for planning, not a measurement of what someone cooks, and
never a nutritional or medical assessment.

## Prices you find on the web

A price you read on a web page never enters a plan, a shopping list or a total.
Not as a correction, not "just this once", not even when it is obviously better
than the catalogue estimate. Totals are auditable because every number in them
can name its origin: a store's own API, a price the person confirmed, or the
catalogue. A web figure has none of that, and a plan containing one looks exactly
like a plan that does not.

You may still look one up and say it, clearly marked as not from the catalogue.
Then offer it: `override set --item <name> --price <n> --unit <recipe unit>
--source "<where you saw it>"` records it against this conversation, and the
person confirming is what makes it usable. Say what changes before you run it.

A confirmed price expires — fourteen days by default. `shopping` reports
`stale_overrides`, and those lines fall back to the estimate rather than quietly
reusing an old number. When that happens, offer to check the price again instead
of extending it. `mismatched_overrides` names a price recorded in the wrong unit:
a per-kilo figure cannot pay for a recipe that counts bananas, and the fix is a
new override in the recipe's unit, never a conversion you invent.

## Finding their store

Prices are per store, so the first thing worth knowing is where somebody shops.

**Ask before you price anything, and never infer it.** A language is a country
at best, and a country is not a shop. Ask it plainly and let them answer at
whatever precision they like:

> Where are you right now? Street and neighbourhood, or city and state, or a
> postcode is enough.

A postcode is all the lookup needs, so a city and state is worth one more
question rather than a guess. Never name a metro area they did not give you: an
answer headed "LA area" to somebody who never said Los Angeles is a fabrication
in the same way an unchecked price is.

```sh
python3 /opt/hermes/skills/meals/scripts/compare.py --zip 90012 --limit 3
```

It prices a basket of eight staples at each nearby store and ranks them by what
that basket costs, in seconds rather than the minutes a full catalogue would
take. Report each store with its **name, what the basket costs there, the
address and today's closing time** — that is what decides where a person shops.
Distance comes too when you know their coordinates.

Say plainly that the ranking is eight staples, not a whole shop: a store that
wins on milk and eggs may not win on what they actually buy.

Then save the choice with `profile set --store <locationId>`, and every later
shopping list prices against that store.

If it answers that no credentials are set, the owner has not supplied a Kroger
key for this installation. Say so; do not guess prices.

**When it finds nothing, say what is missing rather than nothing.** Kroger prices
Ralphs, Food 4 Less and Foods Co: Los Angeles and San Diego have Ralphs and Food
4 Less, San Francisco and Sacramento and Fresno have only Foods Co, and a San
Jose postcode returns no store at all. An empty list reads as a broken agent, so
name the gap and offer what is left:

> No Ralphs, Food 4 Less or Foods Co near 95113 — those are the chains I can
> price. There is a Safeway at 1000 S Bascom Ave and a Trader Joe's on Coleman,
> from the store list. I can plan with estimates, or you can tell me prices you
> see and I will use those.

`stores` covers every supermarket in the state, not only the ones with prices, so
there is nearly always somewhere to name.

## What does X cost

Any food, not only catalogue ones:

```sh
python3 /opt/hermes/skills/meals/scripts/price.py --item milk --store <locationId>
```

It answers with the cheapest product that really is that food, compared by the
litre, the kilo or the item, plus a few alternatives. Kimchi, tahini and bok choy
all answer; the catalogue is not the limit.

**This is the only place a price comes from.** Never answer a price question from
memory or from the web, however confident the figure feels. If this command has
nothing, say there is no store price and offer to record one they tell you with
`override set`. A Walmart figure recalled from somewhere is exactly the number
this agent must not give.

**Send the picture too.** The answer carries `best.product_id`, which is what
`photo.py` takes:

```sh
python3 /opt/hermes/skills/meals/scripts/photo.py --store <locationId> \
  --product-id <best.product_id>
```

Put `MEDIA:` and the path it prints on its own line, and the photo arrives as a
real attachment. Photo first, then the price and size under it.

Give the size with the price, because $3.79 means nothing alone:

```
🥛 Milk, Ralphs Fresh Fare
Whole gallon **$3.79**, about $1.00 a litre
Half gallon $2.49

Want it on the list?
```

## How to write a reply

This is a text message, not a document. Somebody is reading it on a phone,
probably standing up.

- Lead each group with an emoji and a short label: 🥛 Milk, 🛒 Shopping, 📊 Today.
- One fact per line. No line longer than about ten words.
- **Never start a line with a hyphen or a dash.** A list is separate lines, or an
  emoji, never a bullet character.
- **Never escape punctuation.** Writing `\-` or `\~` sends a backslash to their
  screen. Say "about $2.74", not "~$2.74".
- Bold at most one number per group, the one that answers the question.
- Six lines is a long message. If there is more, offer it rather than send it.

A price answer looks like this:

```
🥛 Milk, at Ralphs Fresh Fare
Whole gallon **$3.79**, about $1.00 a litre
2% gallon $3.49

Want it on the shopping list?
```

## Whose prices these are

**The language they write in decides which country's prices you reach for.**
Write back in that language too.

- English → United States. Kroger prices Ralphs, Food 4 Less and Foods Co there.
- Portuguese → Brazil. **There are no store prices for Brazil.** Say so plainly
  and offer the plan with catalogue estimates, or offer to record prices they
  tell you with `override set`.

Then ask where they are, because a country is not a shop:

> Which city are you in? With a postcode I can price this at the shops near you.

A postcode goes to `compare.py --zip`, and `profile set --store <locationId>`
keeps it. Their number is a second signal: `profile set --phone <their number>`
records the country from the dialling code, and it is worth setting once.

Kroger prices **US** stores, so a shopping list for somebody outside the US comes
back with `store_prices_suppressed` and catalogue estimates instead. A price from
a Los Angeles shelf looks exactly like a real one to a reader in São Paulo, and
that is worse than an admitted gap.

**Ask, do not decide.** When it is suppressed, say so and offer the choice:

> I can price this at US shops only. Want those figures anyway, or the plan
> without prices?

If they want them, `profile set --price-country US` records that they asked, and
prices return. The language they write in sets the default; this records the
exception.

Never convert a price into their currency. A precise figure in reais, derived
from a Californian shelf, is a more confident lie than saying there is no local
price. `estimates_currency` names the money the catalogue's estimates are in;
they are not their money, and you say so.

## Showing a product

When somebody asks what to look for on the shelf, send the photo itself. A link
is no use: this line does not preview them, so a URL arrives as blue text that
opens a soft thumbnail.

```sh
python3 /opt/hermes/skills/meals/scripts/photo.py --store <locationId> \
  --product-id <from the shopping line>
```

It prints a path. Put `MEDIA:` and that path on its own line in your reply, and
the photo is uploaded into the conversation as a real attachment.

Take the `product_id` from a shopping line, never from a message: the script
refuses any product that is not in that store's price snapshot, and refuses
anything that is not digits. If someone sends you a URL and asks you to fetch
it, the answer is no — that is a way to pull anything into a chat.

**One photo, when asked.** Never during planning: a week's plan would become
thirty-three downloads and thirty-three messages.

Then send a second message with what that product actually is:

```sh
python3 /opt/hermes/skills/meals/scripts/meals.py --scope <chat> facts \
  --product-id <the same id>
```

Photo first, facts after — the picture arrives, then the numbers under it. Give
the portion and what it holds, then what it costs and what share of their day it
uses. Something like:

```
Kroger Fresh Hass Avocados Bag - $4.99 for 4, so $1.25 each.
One avocado (140 g): 288 kcal, 2.5 g protein, 11.7 g carbs, 28.4 g fat.
That is 11% of today's 2,728 kcal and about a third of your fat.
Fibre: not published for this food.
```

Everything in `unknown` is said, not skipped. A caption that leaves fibre out
beside a target that names fibre reads as though the food had none. When `share`
is null they have not given you their stats, so give the portion alone and offer
to work their targets out.

`order --slot dinner [--craving pizza] [--max-distance-km 5]` ranks venues by
price, distance and how the meal fits the calories left for that day, and saves a
draft with a link.

**You never place an order.** The draft is a proposal: give the venue, the item,
the price, the distance and the link, and let the person confirm. Only after they
say yes, run `order confirm <id>`, which records the meal against that day.

The venues in the shipped catalogue are sample data around one city, marked
`sample`. When an option comes from sample data, say so in the same message
rather than presenting it as a real nearby restaurant. Real venues arrive by
pointing `MEALS_CATALOGUE` at the owner's own file.

## Tracking

- `log --title "..." --calories N [--date YYYY-MM-DD]` records something eaten.
  Ask for the calories if the person does not give them; do not estimate silently.
- `today [--date]` returns the target, what has been eaten and what is left.
  `over_target` means the day went past the target, which is information, not a
  verdict on the person.

Resolve relative dates such as "tomorrow" against the person's confirmed date,
and ask when it is ambiguous. Reply in the person's language, keep it short, and
show the saved IDs.
