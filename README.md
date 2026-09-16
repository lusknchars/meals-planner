# Meals Planner

A Hermes agent for Plow. Text it what you need and it plans the week's meals,
tracks calories against your target, keeps the cost inside your budget, and
prepares an order from places near you.

- **Plan** a week of meals with calories and cost per day, plus one shopping list.
- **Order** a meal: options ranked by price, distance and how they fit the
  calories you have left. The agent prepares the order; you confirm it.
- **Track** what you actually ate and see what's left for the day.

The skill's own store lives in the installation's Hermes home. Nothing is sent
anywhere except the conversation you are texting from. The agent holds no payment
method and opens no delivery account, so it never places an order for you.

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
