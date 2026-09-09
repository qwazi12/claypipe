#!/usr/bin/env bash
#
# ClayPipe auto-checkpoint (Rule 0 / P2, P3, P5).
#
# Runs on the Stop hook: every time Claude finishes a turn, any uncommitted work
# is logged to memory.md, committed, and pushed. The point is that work is never
# stranded on this machine — if a session dies mid-step, the tree is already on
# GitHub and memory.md already says what happened.
#
# It is deliberately conservative:
#   * does nothing when the tree is clean (a normal, already-committed checkpoint
#     costs nothing and produces no noise)
#   * REFUSES to commit if anything that looks like a secret is staged, and says
#     so loudly instead (Rules 1-8)
#   * never force-pushes, never rewrites history, never touches a branch it is
#     not already on
#   * never blocks the session: every failure path exits 0 with a message
#
# Auto-checkpoint commits are labelled as such so they are distinguishable from
# the deliberate step commits that P2 actually cares about.

set -uo pipefail

MARKER="auto-checkpoint"
SENTINEL="<!-- checkpoint-insert -->"

# Emit a systemMessage to the user and exit without blocking.
say() {
  python3 -c 'import json,sys; print(json.dumps({"systemMessage": sys.argv[1], "suppressOutput": True}))' "$1" 2>/dev/null \
    || printf '{"systemMessage": "auto-checkpoint: see terminal"}\n'
  exit 0
}

REPO="${CLAUDE_PROJECT_DIR:-}"
[ -n "$REPO" ] || REPO="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO" ] || exit 0
cd "$REPO" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
[ -n "$BRANCH" ] && [ "$BRANCH" != "HEAD" ] || say "auto-checkpoint skipped: detached HEAD."

DIRTY="$(git status --porcelain 2>/dev/null)"
UNPUSHED=0
if git rev-parse --abbrev-ref "@{u}" >/dev/null 2>&1; then
  UNPUSHED="$(git rev-list --count "@{u}..HEAD" 2>/dev/null || echo 0)"
fi

# Nothing to save and nothing to send: stay quiet.
[ -z "$DIRTY" ] && [ "$UNPUSHED" -eq 0 ] && exit 0

COMMITTED=""
if [ -n "$DIRTY" ]; then
  # ---- Secret guard (Rules 1-8). Filenames first, then staged content. -------
  RISKY_NAMES="$(printf '%s\n' "$DIRTY" | awk '{print $NF}' \
    | grep -Ei '(^|/)\.env($|\.)|\.key$|\.pem$|auth.*\.json$|service_account.*\.json$|credentials\.json$' || true)"
  if [ -n "$RISKY_NAMES" ]; then
    say "auto-checkpoint REFUSED: secret-shaped file(s) in the tree — $(printf '%s' "$RISKY_NAMES" | tr '\n' ' '). Nothing was committed. Check .gitignore before committing by hand."
  fi

  git add -A -- . >/dev/null 2>&1
  RISKY_CONTENT="$(git diff --cached -U0 2>/dev/null \
    | grep -E '^\+' \
    | grep -Eo 'sk_live_[A-Za-z0-9]+|AIza[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{10,}|xoxb-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----' \
    | head -3 || true)"
  if [ -n "$RISKY_CONTENT" ]; then
    git reset >/dev/null 2>&1
    say "auto-checkpoint REFUSED: staged content matches a credential pattern. Nothing was committed or pushed. Rotate anything real, then commit by hand."
  fi

  # ---- Log to memory.md, then include that log line in the same commit -------
  STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  FILES="$(printf '%s\n' "$DIRTY" | grep -c . || echo 0)"
  SUMMARY="$(printf '%s\n' "$DIRTY" | awk '{print $NF}' | head -4 | tr '\n' ' ')"
  [ "$FILES" -gt 4 ] && SUMMARY="$SUMMARY(+$((FILES - 4)) more)"

  if [ -f memory.md ]; then
    grep -q "$SENTINEL" memory.md 2>/dev/null || {
      printf '\n## Auto-checkpoint log (newest first)\n\nWritten by `.claude/hooks/checkpoint.sh` on the Stop hook. These are safety-net\ncommits, not the deliberate step checkpoints P2 asks for — those are the entries\nin the Log section above.\n\n%s\n' "$SENTINEL" >> memory.md
    }
    ENTRY="- $STAMP — $FILES file(s): $SUMMARY"
    awk -v sentinel="$SENTINEL" -v entry="$ENTRY" '
      $0 == sentinel && !done { print; print entry; done=1; next }
      { print }
    ' memory.md > memory.md.tmp && mv memory.md.tmp memory.md
    git add memory.md >/dev/null 2>&1
  fi

  git -c user.name="qwazi12" -c user.email="kyeboah@kymediamgmt.com" commit -q -m "chore: $MARKER — $FILES file(s) on $BRANCH

Automatic safety-net commit written by the Stop hook, not a deliberate
step checkpoint. Files: $SUMMARY

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>" >/dev/null 2>&1 \
    || say "auto-checkpoint: nothing committed (git commit declined). Tree left as-is."
  COMMITTED="$(git rev-parse --short HEAD 2>/dev/null)"
  UNPUSHED=$((UNPUSHED + 1))
fi

# ---- Push. Never force, never retry into a divergence. ----------------------
if [ "$UNPUSHED" -gt 0 ]; then
  if ! git remote get-url origin >/dev/null 2>&1; then
    say "auto-checkpoint: committed ${COMMITTED:-locally} but no 'origin' remote to push to."
  fi
  if GIT_TERMINAL_PROMPT=0 git push -q origin "$BRANCH" >/dev/null 2>&1; then
    say "auto-checkpoint: ${COMMITTED:+committed $COMMITTED, }pushed $UNPUSHED commit(s) to origin/$BRANCH."
  else
    say "auto-checkpoint: committed ${COMMITTED:-earlier} but PUSH FAILED (offline, or origin/$BRANCH has diverged). $UNPUSHED commit(s) are local only — GitHub does not match this tree."
  fi
fi
exit 0
