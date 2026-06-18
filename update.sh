#!/bin/bash
# Brisbane Rental — Auto-update script
# Runs scraper and pushes to GitHub if data changed

cd "/Users/lan/Desktop/订单/B/brisbane-rental" || exit 1

# Activate SSH agent and load key
eval "$(ssh-agent -s)" 2>/dev/null
ssh-add ~/.ssh/id_ed25519 2>/dev/null

# Pull latest first
git pull origin main --rebase 2>/dev/null

# Run scraper
python3 scraper.py
exit_code=$?

if [ $exit_code -ne 0 ]; then
    echo "[$(date)] Scraper failed (exit $exit_code) — skipping push"
    exit 1
fi

# Commit and push if changed
git add index.html
if git diff --staged --quiet; then
    echo "[$(date)] No changes — skipping push"
else
    git commit -m "Auto update $(date -u +'%Y-%m-%d %H:%M UTC')"
    git push origin main
    echo "[$(date)] Pushed successfully!"
fi
