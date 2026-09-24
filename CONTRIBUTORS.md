# Contributors

This is a rolling credit store, not a one-time thank-you page. It is
recounted from scratch at every tagged release, in the release commit itself.
Rank is by merged pull requests, with commit counts alongside for context,
since one PR can be a single commit or forty.

## Built on

This project would not exist without
[**blinkpy**](https://github.com/fronzbot/blinkpy) — the Python library that
does the actual Blink account login, token refresh, camera discovery, and
the live-view handshake this proxy extends across both the `immis://` and
`rtsps://` transports. Written and maintained by **Kevin Fronczak**
([@fronzbot](https://github.com/fronzbot)).

## Leaderboard

Counted from this repository's merged pull requests and `git log` on
2026-09-24, at the 0.9.1 release. Commits include merge commits, and the
several git identities some contributors have committed under are counted
together.

| Rank | Contributor | Merged PRs | Commits |
|---|---|---|---|
| 1 | [@Teethree89](https://github.com/Teethree89) | 28 | 148 |
| 2 | [@bbolinger](https://github.com/bbolinger) | 21 | 58 |
| 3 | [@fritzzetik](https://github.com/fritzzetik) | 5 | 10 |
| 4 | [@ivhazu](https://github.com/ivhazu) | 1 | 1 |

## Maintaining this file

Recount at each tagged release, as part of the release commit. Recount — do
not add to the table above, so a mistake corrects itself next release instead
of compounding.

1. **Merged PRs**, from GitHub rather than `git log`:

   ```
   gh pr list --state merged --limit 300 --json author \
     --jq '.[].author.login' | sort | uniq -c | sort -rn
   ```

2. **Commits**, from `git log main`, **including merge commits**. Counting
   `--no-merges` gives materially different numbers and will not reproduce any
   table published here.

3. **Fold each person's git identities together.** People here have committed
   under several name/email pairs — a GitHub noreply address, a personal one, a
   work one, and differing display names. Counting by raw email splits one
   person across several rows and understates them.

4. Rank by Merged PRs, ties broken by Commits. Anyone who has landed a pull
   request gets a row, starting at their first — there is no threshold to
   clear. Note the date the count was taken.

A count taken in the release commit cannot include that commit or the merge
that lands it, so this table trails the live numbers by a few commits — and, for
whoever cut the release, by one pull request. That is expected, and it corrects
itself at the next release. Chasing it with a follow-up commit only makes the
table stale again.

A commit pushed straight to `main` without a PR still counts toward "Commits",
since step 2 reads `git log` rather than the pull request list.
