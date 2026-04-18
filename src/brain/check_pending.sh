#!/bin/bash
# Check for pending brain work. Called by SessionStart hook.

# Check pending session extractions
session_count=$(find ~/.brain/raw -name "session-*.md" 2>/dev/null | wc -l | tr -d ' ')
if [ "$session_count" -gt "0" ]; then
    echo "BRAIN: $session_count pending session(s) in ~/.brain/raw/ need extraction."
fi

# Check unresolved reconciliation items
recon_file=$(ls -t ~/.brain/timeline/*-reconcile-*.md 2>/dev/null | head -1)
if [ -n "$recon_file" ]; then
    if grep -q "Need your decision" "$recon_file" 2>/dev/null; then
        count=$(grep -c "^[0-9]" "$recon_file" 2>/dev/null || echo "0")
        echo "BRAIN: Reconciliation has unresolved items. Check $recon_file"
    fi
fi

# Check for new files in raw/ (non-session files: transcripts, emails)
raw_count=$(find ~/.brain/raw -name "*.md" -not -name "session-*" -newer ~/.brain/log.md 2>/dev/null | wc -l | tr -d ' ')
if [ "$raw_count" -gt "0" ]; then
    echo "BRAIN: $raw_count new file(s) in ~/.brain/raw/ ready for ingestion."
fi
