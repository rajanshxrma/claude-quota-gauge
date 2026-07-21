# Pairing session: `--json` output flag

Feature is fully implemented and tested (11/11 passing). Nothing left to
design -- this session is pure mechanical execution: pull, verify, commit,
push, open PR.

## What it does

`bin/usage-statusline.py --json` prints the same numbers the terminal bar
shows (model, 5h/weekly %, tracked-model %, pending tasks) as JSON instead
of the rendered bar text, so other scripts/tools can consume the live quota
numbers without scraping the terminal string. Default (no-flag) behavior is
byte-for-byte unchanged.

## Steps

1. Accept the collaborator invite (push access) at
   `https://github.com/rajanshxrma/claude-quota-gauge/invitations`.
2. Clone and check out the branch:
   ```
   git clone https://github.com/rajanshxrma/claude-quota-gauge.git
   cd claude-quota-gauge
   git checkout pairing/json-output-flag
   ```
3. Run the tests -- confirm all 11 pass on your machine too:
   ```
   python3 -m unittest discover -s tests -v
   ```
4. Re-commit as a real joint commit (even though the code is unchanged, the
   commit that counts toward "Pair Extraordinaire" needs both of you on it).
   Simplest way: amend the existing commit to add yourself as co-author, or
   make a small no-op touch (e.g. add a line to this file) and commit that.
   Either way, use this exact trailer (blank line before it matters):
   ```
   git commit --allow-empty -m "confirm --json output flag with rohit" -m "" -m "Co-authored-by: Rohit <111145290+rohitN04@users.noreply.github.com>"
   ```
5. Push and open a PR back to `rajanshxrma/claude-quota-gauge`:
   ```
   git push origin pairing/json-output-flag
   gh pr create --base main --head pairing/json-output-flag --title "add --json output flag" --fill
   ```
6. Rajan reviews and merges the PR -- that's the point both accounts get
   commit credit.

## Files touched

- `bin/usage-statusline.py` -- the `--json` flag + `data` dict
- `tests/test_usage_statusline_json.py` -- 11 tests, all passing
- `PAIRING_SESSION_NOTES.md` -- this file (not committed as part of the
  feature; fine to leave in place or drop before merge, either way)

Currently sitting as a committed local branch `pairing/json-output-flag`,
not yet pushed to origin.
