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

## Venues are sample data

The shipped catalogue's restaurants are samples placed around one city so the
ranking can be demonstrated offline. They are not a real directory, and the agent
says so whenever it uses them. Point `MEALS_CATALOGUE` at your own JSON file to
plan against real recipes, prices and places.

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

Registering updates this agent's page on the Agent Index. Run it against the
installation's real Hermes home, with the demonstration video and screenshots the
listing shows:

```sh
python3 index.py register --hermes-home /var/lib/hermes \
  --credentials ./plow-credentials \
  --video https://example.com/your-demo \
  --image https://example.com/your-screenshot.png
```

`status` says whether this installation is registered, and `dry-run` shows what
would be reported without sending it.
