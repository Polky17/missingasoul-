# Agent HQ

One pinned page that shows every Claude agent chat and scheduled task on the account, grouped by client or area.

Live page: https://claude.ai/artifact/2h5KQ1YfzAioCWhBJXks8P (private until shared from its Share menu)

## What it shows

1. Waiting on you, the chats stopped until you reply, with the exact ask and a link to the chat
2. By client or area, what each chat did and where it saved the work, with status filters and search
3. Where it's saved, how many chats wrote to the CRM, Google Docs, laptop folders, LinkedIn and the rest
4. Scheduled tasks, when each runs, whether it is on, and its last result

## How it stays current

The scheduled task "Agent HQ sweep" runs at 6:47am and 5:47pm UK time. It reads the session list and the scheduled task list, rebuilds three data files (agents, scheduled and places) and swaps them into the dashboard. Its full instructions are in `sweep-prompt.md`.

## Files

- `index.html`, the page as stored in the dashboard (it reads the three datasets; no client data lives in this repo)
- `sweep-prompt.md`, the instructions the sweep runs each time
