@echo off
cd /d C:\Users\zgj\Documents\Claude\Projects\summer\prompt-refine-agent
echo === staging all changes ===
git add -A
git status --short
echo.
echo === committing ===
git commit -m "feat: v5 collection design modes + v6 pattern-library input + billing/cache/public-access" -m "- v5 CONVERGE graph: SINGLE_TOPIC_STRONG / COLLECTION_2SKU modes (nodes 2.4s / 2.4.5 blueprint / 2.5L look design), look preselect UI, auto-sync paired looks to fitting room" -m "- v6 pattern-library input source: skip topic selection, core reference images feed blueprint directly" -m "- billing tab: per-delivery & per-node token/elapsed stats from trace, gen-image usage tokens, avg tokens per image" -m "- style-level persistent cache for 2.1/2.2 (+ backfill script), LLM image downscale (768/1536), color-subset picker" -m "- public access guard: admin/guest roles behind tunnel, SPA single-port serving, misc fixes (task-id collision, gallery role category, csv filename-only refs)"
echo.
echo === latest commits ===
git log --oneline -3
echo.
echo === pushing to GitHub ===
git remote add origin https://github.com/summerinwine04/design-agent-prompt-refine.git 2>nul
git remote set-url origin https://github.com/summerinwine04/design-agent-prompt-refine.git
git push -u origin HEAD
echo.
echo Done. If a GitHub login window popped up, complete it and this will continue.
pause
