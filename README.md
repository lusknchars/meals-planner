# Meals Planner

A Hermes agent for Plow. Text it what you need and it plans the week's meals,
tracks calories against your target, keeps the cost inside your budget, and
prepares an order from places near you.

- **Plan** a week of meals with calories and cost per day, plus one shopping list.
- **Order** a meal: options ranked by price, distance and how they fit the
  calories you have left. The agent prepares the order; you confirm it.
- **Track** what you actually ate and see what's left for the day.

The skill's own store lives in the installation's Hermes home. Nothing is sent
anywhere except the conversation you are texting from.

## Layout

| Path | What it is |
| --- | --- |
| `skills/meals/` | The skill Hermes loads: its rules and the task script |
| `runtime/persona.md` | Who the agent is in conversation |
| `tests/` | Script tests, run without a model |
| `image/` | Supervised services for the container build |

## Tests

```sh
python3 -m unittest discover -s tests -v
```
