# RAG Agents — Frontend

A premium, Apple-quality interface for the RAG Agents backend. A calm, precise
AI-workforce chat where the **Commander** plans each request, assigns
specialists, and synthesizes their work — with a live **Computer** (browser,
terminal, files) and a **Tasks** board alongside.

Built as a separate app from the backend on purpose.

## Stack

- Vite 6 + React 18 + TypeScript (strict)
- zustand (runs chat, connection, VM, drafts)
- lucide-react (icons)
- react-markdown + remark-gfm (streaming markdown)
- Web Speech API (voice dictation, graceful fallback)

## Run

Backend first (default runs on `127.0.0.1:8000`):

```powershell
cd backend
./run.ps1
```

Then the frontend:

```powershell
cd Frontend
npm.cmd install   # once (npm.ps1 is blocked by ExecutionPolicy on this machine)
npm.cmd run dev   # http://localhost:5173
```

Vite proxies `/ws`, `/threads`, `/vms`, `/sysinfo`, `/file`, `/cdp`, `/health`
to the backend, so no CORS setup is needed in dev.

Other useful commands:

```powershell
npm.cmd run typecheck   # tsc --noEmit
npm.cmd run build       # tsc -b && vite build → dist/
```

If you ever run the backend elsewhere, point the app at it directly:

```powershell
$env:VITE_BACKEND = "http://192.168.1.50:8000"
npm.cmd run dev
```

The backend already allows `*` CORS, so cross-origin works.

## Areas

- **Chat** — conversations, streaming bubbles, step cards, tool rows, a live
  run banner, composer with attachments, voice input, reply/edit/copy/share/
  pin/remove on every message, in-thread actions via hover, right-click and
  long-press.
- **Bots** — the four original agents with their states (ready / planning /
  working / synthesizing / finished). Click a bot to talk directly to it.
- **Tasks** — a live board (queued / running / completed) built from the
  Commander's latest plan.
- **Computer** — boot the microVM, watch the live screen, drive the browser
  from the address bar, run a terminal, and manage files with preview.
- **Files** — a standalone file browser over the same backend tools.
- **Settings** — light / dark / system themes, haptics, compact mode, and a
  full local-state reset.

## Architecture notes

- All messages go to the backend Commander; the *target* pill in the composer
  is a hint layer on top of that.
- There is no "run finished" event from the backend, so the frontend finalizes
  a run 1.6 s after the `synthesize` event (see `src/core.ts` `scheduleFinalize`).
- Tools called directly from the UI (browser goto, shell, file ops) are
  resolved through the same websocket `tool` → `tool_result` round-trip.
- Screenshots arriving on `tool_result` update the live `Computer` view.
- State that is local-only uses `localStorage` keys: `rag.prefs.v1`,
  `rag.ignores.v1` (pinned/archived conversations), `rag.pins` (message pins).