# Handoff — Zoom RTMS Plugin

## Current State (2026-03-09)

**Clean.** Three bugs fixed this session, committed to `main`, pushed to GitHub, and deployed to staging.

### Latest Commit
- `210dcc8` — fix: revert to 10-frame liveness model with proper timestamps

### Bugs Fixed This Session
1. **FRAME_COUNT 30→10** — The 30-frame hybrid-v2 switch had changed `FRAME_COUNT=30` in `h264-batch-decoder.ts`. Reverted to 10 to match the default model.
2. **Removed `model: "hybrid-v2-30"`** — Explicit model param removed from `fastCheck()` call in `orchestrator.ts`. SDK defaults to `"10"` which is correct.
3. **Reconstructed timestamps** — Replaced per-frame `Date.now()` with reconstructed timestamps using 100ms spacing (`baseTimestamp + (index * 100)`). Raw `Date.now()` produced near-identical timestamps when frames decode in quick succession, confusing the liveness model.

### Previous Features (still working)
1. Camera toggle auto-rescan
2. Continuous background re-scanning
3. Host self-exclusion
4. Sidebar Zoom Apps panel

## Active Context

- Node.js/TypeScript Express server
- `@zoom/rtms` SDK for RTMS streams, `@moveris/shared` SDK for liveness API
- Key files: `src/orchestrator.ts`, `src/h264-batch-decoder.ts`
- Sidebar: `src/sidebar/public/`
- Deployment: Fly.io staging at `zoom-rtms-plugin-staging.fly.dev`
- Clone to `/tmp/` before deploying (Google Drive FUSE causes timeouts)

## Blockers

None.

## Next Steps

- Await diagnostic results from the **meeting-liveness-bot** investigation (DSLR/other cameras getting near-zero scores). Findings about API warnings, aspect ratio, or face detection may apply to this plugin too.
- The timestamp fix is already applied here. If the meeting bot diagnostics reveal additional issues (e.g., aspect ratio problems, API warnings), apply the same fixes.

## Linear Project
- Project: "Zoom RTMS API integration with SDK"
- Issues: MOV-903 through MOV-916
