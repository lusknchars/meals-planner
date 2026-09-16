# Meals Planner

<p align="center">
  <img src="docs/logo.png" alt="Meals Planner: a calendar with two days ticked, resting in a bowl of greens and half an avocado" width="320">
</p>

A Hermes agent for Plow. Text it what you need and it plans the week's meals,
tracks calories against your target, keeps the cost inside your budget, and
prepares an order from places near you.

- **Plan** a week of meals with calories and cost per day, plus one shopping list.
- **Order** a meal: options ranked by price, distance and how they fit the
  calories you have left. The agent prepares the order; you confirm it.
- **Fill your Kroger cart** with the week's shopping, for pickup or delivery. You
  check out and pay in the Kroger or Ralphs app.
- **Track** what you actually ate and see what's left for the day.

The skill's own store lives in the installation's Hermes home. Your shopping list
leaves the installation only when you ask for it to go into your Kroger cart. The agent holds
no payment method and opens no delivery account, so it never places an order for
you.

## Install

Docker Desktop and a free Plow line are the only prerequisites.

1. Get a line for this agent and mint its credential with Plow's own CLI, then
   save the result as `plow-credentials` in this folder, mode 0600. It stays out
   of Git and out of the image.
2. Build and start it:

```sh
docker compose up --build -d
docker compose logs -f agent
```

3. Text the line. Tell it how many people eat and your daily calorie target, then
   ask for the week's plan.

`docker compose down` stops the agent and keeps its records. `docker compose
down -v` deletes them permanently, along with the installation's reporting
identity.

The container reports token usage to the Agent Index every five minutes. An agent
whose owner does not want that is one built without the `image/s6-overlay`
service.

## Restrictions and brands

Before the first plan the agent asks three things: anything you cannot eat, such
as gluten or lactose; whether you are vegan or vegetarian; and whether you always
buy a particular brand. This is a gate, not a suggestion: `plan` and `order`
refuse until the answer is saved, and "none" is an answer.

- **Restrictions** are `vegetarian`, `vegan`, `gluten-free` and `dairy-free`.
  Everyday words map onto them, so lactose plans dairy-free. Anything else, such
  as a nut allergy, is refused rather than saved and ignored.
- **The shipped recipes** are tagged vegan and dairy-free from their ingredients,
  and a test holds those tags to the ingredient lists.
- **Changing a restriction** rebuilds a saved week instead of replaying one you
  can no longer eat.
- **Brands** are per ingredient (`brand set --item yoghurt --brand Chobani`). The
  shopping list prices your brand when the store stocks it, even when dearer,
  and says so when it does not. On Instacart the brand filters that line.

## Real stores and real prices

`refresh.py` builds the snapshots the skill reads. It runs out of band — at build
time or on demand — never inside a chat turn, so replies stay fast, offline and
deterministic.

```sh
# Supermarkets within 5 km of a point, from OpenStreetMap
python3 refresh.py stores --lat 34.0522 --lon -118.2437 --radius-km 5

# This store's prices for every catalogue ingredient, from Kroger
python3 refresh.py prices --zip 90017
```

Stores land in `skills/meals/stores.json` and ship with the agent: names,
brands, street addresses, opening hours and coordinates. Ask the agent for
`stores` and it lists the nearest ones with distances.

Prices need a free app at developer.kroger.com, then `KROGER_CLIENT_ID` and
`KROGER_CLIENT_SECRET` in `.env`. They land in `skills/meals/prices.<locationId>.json`,
which Git ignores — Kroger's developer terms cover calling their API for your own
planning, not republishing their prices. Tell the agent which store to use with
`profile set --store <locationId>`.

Each shopping-list line then says where its figure came from:
`kroger:70100123 2026-09-16` for a real price, or `catalogue estimate` when the
package size can't be converted to the recipe's unit — a 32 oz bag has a per-kilo
price, "family pack" does not, and the agent admits the difference instead of
inventing one.

The product on a line is the one that costs least to buy for what the week
needs, not the cheapest per kilo. 380 g of chicken used to pick an 8 lb frozen
bag at $20.00, the best rate on the shelf; it now picks a 1 lb pack from the
counter at $2.50. On one real Ralphs week that took the shop from $162.35 to
$106.28. Each line says how many `packages` that is, and the Kroger cart adds
exactly those. Product names that say they are more than the ingredient
(overnight oats, a couscous mix, deli turkey) are not counted as it; rebuild
older snapshots with `refresh.py prices` to apply that.

### What "best value" means

A priced line carries `unit_price`, `median_unit_price` and a `value` label
comparing the two: `best value` at 70% of the median or below, `good value` to
92%, `typical` above that, and `only priced option` when there was nothing to
compare with. On a real basket that spread came out as 13 best, 11 good, 6
typical across 33 lines.

It is a price comparison at one store on one day. It says nothing about quality,
nutrition or whether the food is worth buying, and both numbers are on the line
so the claim can be checked. Sale prices count: Kroger cage-free eggs at $4.39
with a $2.79 promo are compared at $0.155 each, not $0.24.

### Prices you confirm yourself

Some prices no API will sell you. Ralphs weighs bananas, the recipe counts them,
and no free API publishes a per-piece produce price — so the only honest source
is a person who looked.

```sh
# in a conversation with the agent, or directly:
meals.py --scope <chat> override set --item banana --price 0.22 --unit un \
  --source "Trader Joe's (web, 16 Sep)"
```

That line then reads `you confirmed 2026-09-16 (Trader Joe's (web, 16 Sep))`
instead of `catalogue estimate`, and counts in the total. Three rules keep it
honest:

- **It expires.** Fourteen days by default, `MEALS_OVERRIDE_DAYS` to change it.
  A stale one is refused, listed in `stale_overrides`, and the line falls back to
  the estimate rather than quietly reusing an old number.
- **The unit must match the recipe's.** A per-kilo price cannot pay for a recipe
  that counts bananas; it is reported in `mismatched_overrides`, never converted.
- **A web price is not a price.** The agent may look one up and say it, marked as
  not from the catalogue, but it never enters a plan or a total until you confirm
  it as an override. Every number in a total can name its origin: a store's API,
  a price you confirmed, or the catalogue.

## Your Kroger cart

Ask for the week's shopping in your cart, say pickup or delivery, and the agent
adds the products it priced, your preferred brands included, to your own Kroger
or Ralphs cart. Kroger's cart API only adds: it cannot read the cart or check
out, so you choose the time and pay in the Kroger or Ralphs app. The agent holds
no card and nothing is bought until you check out.

**Setup, once, in your app at [developer.kroger.com](https://developer.kroger.com):**

1. Make sure the app includes the **Cart** API alongside Products and Locations.
2. Add this redirect URI: `https://lusknchars.github.io/meals-planner/kroger/`

**Each person, once:** the agent texts a Kroger login link. After logging in they
land on that page, which shows a code starting `kroger:` to copy back into the
chat. The agent has no public address for Kroger to send them back to, which is
why the page exists. It is static, loads nothing from anywhere else, sends no
referrer, and clears the code from the address bar. A code alone is useless: it
works once, within ten minutes, and only with your client secret and the PKCE
verifier the installation kept.

What `skills/meals/scripts/cart.py` guards, tested in `tests/test_cart.py`:

- **Packages, not ingredients.** Five avocados from bags of four is two bags.
  Food sold by weight goes in by the pound and is flagged to check in the app.
- **No doubled cart.** Adding the same list again sends nothing; a bigger list
  sends only the difference. It cannot see what you removed in the app.
- **Named gaps.** A line priced from the catalogue or by you has no store
  product, so it is listed for you to add rather than guessed.
- **Logins stay put.** A code from another conversation, a stale one, or one
  with characters no login produces is refused. Tokens never appear in replies,
  refresh on their own, and `disconnect` forgets them.

To host the page yourself, publish `pages/kroger/` anywhere static and set
`KROGER_REDIRECT_URI` to it. This repository publishes it from the `gh-pages`
branch.

## Delivery through Instacart

Instacart's developer program is not accepting new applications (September
2026), and it is open only to residents or registered businesses of the US and
Canada, so most installations will leave this off. Without a key the agent says
Instacart is not connected and offers the list instead.

Ask for the week's shopping on Instacart and the agent sends one link built from
the saved list. You open it, pick a store and check out in Instacart with your
own account. The agent holds no card, never sees that cart and places nothing.

It needs a key from the [Instacart Developer Dashboard](https://dashboard.instacart.com)
in `.env`:

```sh
INSTACART_API_KEY=keys.xxxxxxxx
INSTACART_ENV=development   # production once Instacart approves your production key
```

A development key works at once against Instacart's test server. A production
key stays pending until Instacart reviews the app. Instacart delivers in the US
and Canada, so a number from anywhere else gets the shopping list and no link.

What the script does on the way, tested in `tests/test_instacart.py`:

- **Units Instacart matches.** Grams, millilitres and whole counts go in as
  measured; a slice count has no Instacart unit, so it goes in unmeasured and the
  agent names the amount the plan needs.
- **American names.** Courgette is searched as zucchini, yoghurt as yogurt.
- **Instacart's link or none.** A returned address that is not on Instacart's own
  domains is refused rather than sent to somebody's phone.
- **One page per list.** The same list reuses its saved link until a day before
  it expires, as Instacart asks; a changed list gets a new one.
- **No total.** Instacart prices its own shelves at the store you pick, so the
  agent gives no figure for that cart.

## Targets and macros

Give the agent your stats and it works out the day's numbers — in code, not in a
model's head:

```sh
meals.py --scope <chat> profile set --age 30 --sex male --height-in 70 \
  --weight-lb 175 --activity moderate --goal maintenance
meals.py --scope <chat> targets
```

Mifflin-St Jeor for BMR, an activity multiplier for TDEE, a goal adjustment, then
macro grams: protein by bodyweight, fat at 25% of intake, carbohydrate the
remainder, fibre at 14 g per 1,000 kcal with a 25 g floor. A target that would
fall below **1,200 kcal for a woman or 1,500 for a man is held there**, flagged
as `floored`, and the note says that going lower needs medical supervision. That
floor is enforced in the script, not requested in a prompt.

Plans then report what they actually contain:

```sh
python3 refresh.py nutrition          # macros for every catalogue ingredient
```

That snapshot comes from USDA FoodData Central (public domain), keyed by the
catalogue's own ingredient names. Each plan day carries `protein_g`, `carb_g`,
`fat_g` and `fiber_g`, plus `macros_unknown` naming anything that could not
contribute — and `macros_complete` is false whenever that list is non-empty.

Two rules worth knowing:

- **Cost scales with household size; macros do not.** Quantities multiply by
  `people` for the shopping list, but calories and macros are what one person eats.
- **A counted ingredient needs USDA's published portion weight.** A banana is
  126 g as an NLEA serving, which is a citable figure; without one, the
  ingredient is named unknown rather than converted by a number I made up.

A free key from [fdc.nal.usda.gov](https://fdc.nal.usda.gov/api-key-signup.html)
goes in `.env` as `USDA_API_KEY`. `DEMO_KEY` allows roughly thirty calls an hour
and a full catalogue needs seventy-six, so a rate-limited run keeps every lookup
that succeeded and names the rest in `pending`. There is no resume: a re-run
starts from the top.

**This is not medical advice.** The agent holds no certification, says so, and
points at a physician for a pre-existing condition, pregnancy, or a history of
disordered eating.

## Venues are sample data

The shipped catalogue's restaurants are samples placed around one city so the
ranking can be demonstrated offline. They are not a real directory, and the agent
says so whenever it uses them. Point `MEALS_CATALOGUE` at your own JSON file to
plan against your own recipes.

## Layout

| Path | What it is |
| --- | --- |
| `skills/meals/` | The skill Hermes loads: its rules, the task script, the catalogue |
| `runtime/persona.md` | Who the agent is in conversation |
| `index.py` | The pinned Agent Index client, for publishing and reporting |
| `tests/` | Script tests, run without a model |
| `image/` | Supervised services for the container build |
| `pages/kroger/` | The static page Kroger sends people to after login |

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Publish

The running container registers itself and reports usage every five minutes. That
first registration carries only the agent id, so the page starts bare; this fills
it in, including the demonstration video and screenshots.

The Hermes home lives inside the container's volume, so registration runs there,
not on the host. `with-contenv` is an execline script that a plain `exec` cannot
run, so read the token from the container environment directly:

```sh
docker compose exec -T agent /bin/sh -c '
TOKEN=$(cat /run/s6/container_environment/PLOW_AGENT_TOKEN)
exec /command/s6-setuidgid hermes env HOME=/var/lib/hermes HERMES_HOME=/var/lib/hermes \
  AGENT_ID=meals-planner PLOW_AGENT_TOKEN="$TOKEN" \
  /opt/hermes/.venv/bin/python3 /opt/plow/agent-index-client.py \
  --register --agent meals-planner --name "Meals Planner" \
  --blurb "..." --repo https://github.com/lusknchars/meals-planner --runtime Hermes \
  --install-url https://github.com/lusknchars/meals-planner/blob/main/README.md \
  --video https://example.com/your-demo --image https://example.com/your-screenshot.png
'
```

An `--install-url` with a `#fragment` is rejected: the client reports
`some values were not stored: {'install_url': 1}` and keeps the rest.

Swap `--register …` for `--dry-run` to see the usage that would be reported, or
`status` to check whether this installation is registered.

`index.py` wraps the same pinned client for a Hermes home on the host, which is
the path to use when you run Hermes outside Docker.
