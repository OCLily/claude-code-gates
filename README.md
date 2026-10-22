# Claude Code harness: gates, hooks and review agents (excerpt)

An excerpt from a personal Claude Code setup. The idea behind it: a prompt that says "do not delete things" can be misread or overridden, while a hook that denies the action is much harder to talk around. This repository shows how that layer is built for an autonomous coding agent. It is a layer, not a sandbox: the gates match command text and can be worked around. Each gate documents what it does not catch:

- `destructive-command-gate.py`, `prune-gate.py`, `push-gate.py`, `merge-gate.py` and `git-add-commit-gate.py` define a `KNOWN_GAPS` list that their selftest prints (`erkannt` or `nicht erkannt` per entry; some entries pin cases that have since become detected, and a few comments next to them are stale).
- `stop-gate.py` and `autogate_state.py` describe their known gaps in the docstring.
- `commit-secret-sweep.py` has no such list; its limits are in comments and in the docstring of its `ask()` function.

**Requirements:** Python 3.9 or newer, standard library only (the selftests were run on 3.9, 3.12 and 3.15). `git` is needed by several gates and by the selftests. `scripts/run-selftests.sh` needs `bash` (the 3.2 that ships with macOS is enough). Developed and tested on macOS; some paths and case-folding rules assume macOS (`/private/tmp`, case-insensitive APFS). Not tested on Linux or Windows.

**Status: extract from a private working setup, not a drop-in package.**

- Placeholders: paths, names and project references were replaced with neutral ones. `/Users/alice` is a home directory, `~/vault` a notes vault, `~/project-x` a second project directory, and "Maintainer" in comments means the human who runs the setup. None of these exist in this repository.
- Layout: the scripts assume a layout like the original (`~/.claude`, a notes vault, a second project directory). They are meant to be read and adapted, not installed.
- Language: all scripts are written in German (comments, messages, docstrings). English summaries exist only where stated here. The three largest gates (`merge-gate.py`, `destructive-command-gate.py`, `prune-gate.py`) start with a short English summary. `git-add-commit-gate.py`, `push-gate.py`, `stop-gate.py`, `autogate_state.py`, `commit-secret-sweep.py` and `lib/gate_log.py` start with a 3-line English comment under the shebang. `lib/secret_patterns.py`, `run-selftests.sh` and everything below those summaries (docstrings, comments, messages) are German only. Of the prompt files, `agents/critic.md` and `agents/ultra-autonomous.md` are German; `agents/code-reviewer.md`, `agents/contrarian.md` and `skills/model-router/SKILL.md` are English.
- Parts of the original setup that are not included: other hooks (for example the separate write gate for configuration files that `merge-gate.py` mentions, and the hook that handles `gh pr merge`, see the table below) and the `/ultra` command that dispatches `ultra-autonomous`. Other agents and third-party plugin skills that the prompts mention (for example an implementer agent, `superpowers` skills, and the `Explore` and `general-purpose` subagents) are not included either.

## Where to start reading

1. `scripts/git-add-commit-gate.py`: the simplest gate (one decision, always deny). It opens with a 3-line English summary; the docstring below it is German and explains why it tokenizes the command instead of matching one big regex. `CASES` and the selftest at the bottom show the behaviour.
2. The English summary and docstring at the top of `scripts/destructive-command-gate.py`, then its `KNOWN_GAPS` list.
3. `prune-gate.py` and `push-gate.py` (same pattern, different target), then `merge-gate.py` (a long design record), then `stop-gate.py` with `autogate_state.py`.
4. `agents/` and `skills/model-router/` last; they are prompts, not enforcement.

## What is in here

| Path | What it does |
|---|---|
| `scripts/destructive-command-gate.py` | PreToolUse hook for Bash. Flags `rm -rf` outside `/tmp`, `/private/tmp` and `node_modules` directories, `git reset --hard`, `git clean -f`, `curl \| sh` pipes, `bash -c` and other shell inline code, interpreter inline code (`python3 -c`, heredocs) with a destructive payload or an unseen code source, package installs, `npx` runs that fetch new code, production deploys, mail and upload commands, and MCP/plugin installs. Asks in `default` and `plan` mode, denies in every other mode. It is mostly token-based: it splits the command into segments and dispatches on the command head and its arguments. A few categories use regexes on the raw command string or segment instead: `curl`/`wget` piped into a shell, `docker system prune` and `pbpaste` piped into `curl`/`wget` (whole command string); `npm publish`, `kubectl delete` and `dd of=` (segment regex after a command-head check); the `npm_config_yes=` form of `npx`; and the payload checks for inline interpreter code, including the head-independent check for delete and exec APIs. It is a blacklist over command text and lists its known gaps in `KNOWN_GAPS`. |
| `scripts/prune-gate.py` | Guards `rm` and `git rm` on protected roots: `~/.claude/agents`, `skills`, `plans` and `scripts`, `~/vault/memory`, `~/vault/projects` and `~/project-x/knowledge`. Asks in interactive mode, denies in auto-approve modes. `git mv` stays allowed. |
| `scripts/push-gate.py`, `scripts/merge-gate.py` | `push-gate` guards pushes to main/master (ask in interactive mode, deny in auto-approve modes). `merge-gate` guards ref-moving git commands in the main checkout of the setup directory (`~/.claude`) and allows a list of harmless verbs; it also blocks `gh pr checkout` (and its aliases) there and reads `~/.config/gh/config.yml` read-only to resolve `gh` aliases; worktrees and other repositories are out of scope. The design intent is that the human clicks the merge, and the deny messages say so. This export does not enforce it: `merge-gate` lets `gh pr merge` pass, so this export does not stop it (the original setup handled it in another hook that is not included), and `push-gate` looks only at pushes to main/master. |
| `scripts/git-add-commit-gate.py` | Forbids `git add -A`, `--all`, `.` and `*` and `git commit -a`/`--all` (always deny, not mode-aware). Does not cover `git add -u`, `git add ./` and `git add :/` (listed in its known gaps). |
| `scripts/commit-secret-sweep.py` | Asks before a commit if a secret-like value removed in the staged diff still exists elsewhere. Secret candidates are extracted from the removed lines of the staged diff, and each candidate is searched with `git grep` in the whole worktree and index, because fixing the flagged spot is not the same as removing the value everywhere. It is standalone on purpose and keeps its own copy of the secret patterns. It only ever asks, and an `ask` is not enforced in auto-approve modes (documented limit). It makes no network calls; the only thing it writes is the decision log (via `lib/gate_log.py`). |
| `scripts/lib/secret_patterns.py` | A second copy of the sweep's secret patterns and thresholds (17 high-confidence patterns plus the generic-secret heuristic; identical to the sweep at the time of export). The sweep does not import it; the only consumer is `scripts/lib/gate_log.py`, which uses it to mask secret-like values in logged hits. The drift check in the `gate_log.py` selftest is one-directional: it fails if a high-confidence regex or a generic-secret threshold of this module is missing from the sweep source. It does not compare function bodies and does not notice a pattern that was added only to the sweep. |
| `scripts/stop-gate.py`, `scripts/autogate_state.py` | Stop hook that blocks ending a turn while a plan file (`.md` written under a `plans/` directory) has no critic dispatch recorded for it, with per-session state. It checks that a critic was dispatched with the plan path in the prompt, not that the review finished. It blocks only in `acceptEdits`, `auto`, `dontAsk` and `bypassPermissions` mode. An emergency brake releases the plan after 3 blocks per plan path and caps a session at 4 blocks in total. It runs in shadow mode by default: the decision goes to a shadow log and nothing is blocked (the per-session counters are still updated). It enforces only with `AUTOGATE_STOP_LIVE=1`. |
| `scripts/lib/gate_log.py` | Structured decision log for the gates. Logging is on by default: every decision is written as one JSON line to `~/.claude/metrics/gates/` with a 90-day retention. For `deny` and `ask` decisions the command text is logged with secret-like values masked (pattern-based, so incomplete). For `allow` decisions the command text is logged unmasked, cut at 1000 characters. So by default every Bash command that passes one of the gates is stored in clear text in that directory. Set `CLAUDE_GATE_LOG_DISABLE=1` to switch logging off or `CLAUDE_GATE_LOG_DIR` to move it. |
| `scripts/run-selftests.sh` | Runner that executes every `--selftest` with the environment described under "Tests" and writes the result of the last run to `metrics/selftests/state.json` (gitignored). |
| `agents/` | Review roles: `critic`, `contrarian`, `code-reviewer`. `ultra-autonomous` is not a review role: it is an autonomous orchestrator and executor that plans, executes and dispatches those three reviewers. By default the maker and the checker are separate agents. |
| `skills/` | `model-router` (which Claude model alias for which job; names like `opus`, `sonnet` and `fable` are Claude model aliases). |
| `examples/settings.example.json` | How the gates are wired into `settings.json` hooks. |

## Input and output sample

A gate reads the hook payload as JSON on stdin and prints a `permissionDecision` as JSON on stdout, or nothing when it allows. The gates read `tool_input.command`, `permission_mode` and `cwd`; the Stop hook and the state hooks also read `session_id`, and `autogate_state.py` additionally reads `tool_input.file_path`, `subagent_type` and `prompt`. The decision log also stores `tool_name`, `session_id` and `cwd`. The gates call `git` read-only (list arguments, no shell) in the `cwd` of the hook payload, so they trust that repository's local git configuration.

```sh
echo '{"cwd":"/Users/alice/code/demo","permission_mode":"bypassPermissions","tool_input":{"command":"rm -rf /Users/alice/code/demo/build-old"}}' | HOME=/Users/alice CLAUDE_GATE_LOG_DISABLE=1 python3 scripts/destructive-command-gate.py
# {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "[Destructive-Command-Gate] rm -rf (rekursiv + force) erkannt. Im Auto-Approve/bypass-Modus hart geblockt. ..."}}
```

With `"permission_mode": "default"` the same command returns `"ask"` and a shorter reason. `rm -rf /tmp/scratch-1` prints nothing (exit 0 = allow). Wiring: see `examples/settings.example.json`. The shadow-mode Stop hook becomes enforcing with `AUTOGATE_STOP_LIVE=1`.

**Before wiring anything up:** replace `/path/to/scripts` in `examples/settings.example.json` with the absolute path of the scripts and run each gate once by hand (`echo '{}' | python3 <path>/scripts/<gate>.py` must exit 0 and print nothing). A wrong path makes `python3` exit with code 2, which Claude Code treats as a blocking error, so every Bash call (or every turn end, for the Stop hook) would be blocked. The example enables all six Bash gates at once; start with fewer if in doubt.

## Files the scripts create, and switches

- `~/.claude/metrics/gates/YYYY-MM-DD.jsonl`: the decision log of all gates (90-day retention, see `lib/gate_log.py` above).
- `~/.claude/.autogate-shadow.log`: appended by `stop-gate.py` on every Stop event in shadow mode (session id and mode, no command text); not rotated.
- `~/.claude/.autogate-pending.<session_id>.json`: per-session state of `autogate_state.py` (plan paths, counters); entries expire after 12 hours and stale files are swept after 24 hours.
- `metrics/selftests/state.json` inside the repository (gitignored): written by `scripts/run-selftests.sh`.
- Environment switches: `CLAUDE_GATE_LOG_DISABLE=1` and `CLAUDE_GATE_LOG_DIR` (decision log), `AUTOGATE_STOP_LIVE=1` (Stop hook enforces), `AUTOGATE_STOP_OFF=1` (Stop hook off, also stops its shadow logging), `COMMIT_SECRET_SWEEP=off` (commit sweep off).

About the `timeout` values in the example:

- `5` for `destructive-command-gate.py` matches the 5 second hook timeout that its source comments refer to.
- `10` for `commit-secret-sweep.py` is not stated in any script. The sweep has an internal budget of 9 seconds (`TOTAL_BUDGET`), and its docstring says that budget must stay below the hook timeout; 10 is simply a value above 9.

## Design principles

- Keys, not prompts: an action with outside effect is blocked by a hook, not by an instruction.
- A gate enforces a property (never lose unpushed work), not a sequence.
- Every gate carries a selftest and documents what it does not catch (see the list of known-gap locations above).
- By default the author of an artifact does not review it; the review goes to a separate agent. The one exception in this export is the documented trivial-scope inline review in `agents/ultra-autonomous.md`, which applies only when all of its hard gates hold and is never used for the final pass.

## Tests

Each gate has a `--selftest`. The selftests contain literal `/Users/alice/...` paths, so they run with `HOME=/Users/alice` (the directory does not need to exist). Run results for this export, taken on the final tree with `HOME=/Users/alice`, a disabled gate log (`CLAUDE_GATE_LOG_DISABLE=1`) and the exceptions noted in the table:

| Script | Result |
|---|---|
| `destructive-command-gate.py` | 491/491 |
| `prune-gate.py` | 353/353 |
| `push-gate.py` | 148/148 |
| `autogate_state.py` | 47/47 |
| `git-add-commit-gate.py` | 68/68 main suite plus 11/11 JSON-shape suite (79 checks) |
| `commit-secret-sweep.py` | 56 checks, 0 failures |
| `stop-gate.py` | 61 PASS, 0 FAIL |
| `gate_log.py` | 67 PASS, 0 FAIL (the selftest clears `CLAUDE_GATE_LOG_DISABLE` itself, so it gives the same result with the switch set or unset; it writes only to temp folders) |
| `merge-gate.py` | 647/647. Run with a throwaway `HOME` (not `/Users/alice`) that holds an empty git repository as `~/.claude`, outside `/tmp` (recipe below). |

```sh
HOME=/Users/alice CLAUDE_GATE_LOG_DISABLE=1 python3 scripts/prune-gate.py --selftest
```

`merge-gate.py` reads the real `~/.claude` checkout, so it needs a throwaway home with an empty git repository as `~/.claude`. That directory must not be below `/tmp`: six cases use `/tmp` as the "foreign" directory and fail if `~/.claude` lives inside it (with a throwaway `HOME` under `/tmp` the result is 641/647). On macOS `TMPDIR` points to `/var/folders/...`, and the recipe falls back to `/var/tmp` when `TMPDIR` is unset; if your `TMPDIR` is below `/tmp`, use another directory.

```sh
H=$(mktemp -d "${TMPDIR:-/var/tmp}/gate-home.XXXXXX")
git init -q "$H/.claude"
HOME="$H" CLAUDE_GATE_LOG_DISABLE=1 python3 scripts/merge-gate.py --selftest
rm -r "$H"
```

`scripts/run-selftests.sh` does all of this in one go (it needs `bash`, `python3` and `git`; `timeout` is optional) and exits 1 if any selftest fails.

## What this does not show

There is no end-to-end demo of an agent solving a real task. The gates are evidence for control and safety design, not for output quality.

## License

MIT, see `LICENSE`.
